#!/usr/bin/env python3
"""i-MSCP -> ISPConfig migration v9.12 (Debian 10, Python >=3.7).

--inventory/--plan are read-only. --apply uses the ISPConfig remote JSON API to
provision websites, mail domains, mailboxes, forwards, SQL users and databases,
then transfers content. SQL dumps can be imported with explicit opt-in into empty destination databases; DNS provisioning and mail-domain activation are separately opt-in. Explicit confirmation required. No public DNS cutover is performed.
"""
import argparse
import getpass
import urllib.request
import urllib.error
import ssl
import time
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path('/root/imscp-migration')
SSH_KEY = '/root/.ssh/id_rsa'
SSH_USER = 'imscp'
SSH_TRANSPORT = 'ssh -i '+SSH_KEY+' -o IdentitiesOnly=yes -o BatchMode=yes'
EXCLUDED_DOMAINS = set()
EXCLUDED_DATABASES = set()
SKIP_WEB_DOMAINS = set()
MAIL_MERGES = {}
IDENT = re.compile(r'^[A-Za-z0-9_]{2,64}$')
HOSTNAME = re.compile(r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$')
# DNS owner labels can contain underscores (_domainkey, _dmarc, _sip._tcp).
DNS_OWNER_LABEL = re.compile(r'^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?$')


def run(argv, capture=True, input_data=None):
    p = subprocess.run(argv, input=input_data, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None)
    if p.returncode:
        # Never log commands containing credentials or SQL hashes.
        raise RuntimeError('%s failed (exit %s): %s' %
                           (argv[0], p.returncode, (p.stderr or b'').decode(errors='replace')[-400:]))
    return (p.stdout or b'').decode(errors='replace')


def mysql(sql):
    return [line.split('\t') for line in run(['mysql', '-N', '-B', '-e', sql]).splitlines()]


def quote_sql(s):
    return "'" + s.replace('\\', '\\\\').replace("'", "''") + "'"


def valid_host(s):
    if not HOSTNAME.fullmatch(s) or '..' in s or '/' in s:
        raise RuntimeError('Invalid hostname: '+s)
    return s


def discover():
    rows = mysql("SELECT domain_id,domain_name,domain_dns,domain_external_mail FROM imscp.domain WHERE domain_status='ok'")
    domains = [{'id': int(i), 'name': valid_host(n), 'dns': dns, 'external_mail': ext}
               for i,n,dns,ext in rows]
    by_id = {d['id']: d for d in domains}
    websites = []
    for d in domains:
        websites.append({'domain': d['name'], 'hostname': d['name'], 'kind': 'main',
                         'source': '/var/www/virtual/'+d['name']+'/htdocs',
                         'skip': d['name'] in SKIP_WEB_DOMAINS})
    for did, sub, status, forward in mysql('SELECT domain_id,subdomain_name,subdomain_status,subdomain_url_forward FROM imscp.subdomain'):
        if int(did) not in by_id or status != 'ok':
            continue
        d = by_id[int(did)]['name']
        sub = sub.strip().lower()
        if not HOSTNAME.fullmatch(sub) or '.' in sub:
            raise RuntimeError('Subdomain label needs review: '+sub)
        websites.append({'domain': d, 'hostname': sub+'.'+d, 'kind': 'subdomain',
                         'source': '/var/www/virtual/'+d+'/'+sub+'/htdocs',
                         'skip': forward not in ('', 'no')})
    mails = []
    for did, address, typ, status, enabled, forward, quota in mysql(
            "SELECT domain_id,mail_addr,mail_type,status,po_active,COALESCE(mail_forward,''),COALESCE(quota,0) FROM imscp.mail_users"):
        if not did or int(did) not in by_id or status != 'ok':
            continue
        if '@' not in address:
            continue
        local, dom = address.rsplit('@', 1)
        if dom != by_id[int(did)]['name']:
            continue
        mailbox = 'normal_mail' in typ.split(',')
        merge_target = MAIL_MERGES.get(address)
        mails.append({'domain': dom, 'address': address, 'type': typ,
                      'enabled': enabled == 'yes', 'mailbox': mailbox and not merge_target,
                      'forward': forward, 'quota': quota,
                      'source': '/var/mail/virtual/'+dom+'/'+local,
                      'merge_into': merge_target})
    physical = {r[0] for r in mysql('SHOW DATABASES')}
    assoc = {}
    for did, user, host in mysql('SELECT sqld_id,sqlu_name,sqlu_host FROM imscp.sql_user'):
        assoc.setdefault(did, []).append({'user': user, 'host': host})
    dbs = []
    for dbid, did, name in mysql('SELECT sqld_id,domain_id,sqld_name FROM imscp.sql_database'):
        if int(did) not in by_id:
            continue
        dbs.append({'domain': by_id[int(did)]['name'], 'name': name,
                    'exists': name in physical, 'users': assoc.get(dbid, [])})
    dns = []
    for did, aliasid, name, typ, data, status in mysql(
            'SELECT domain_id,alias_id,domain_dns,domain_type,domain_text,domain_dns_status FROM imscp.domain_dns'):
        if int(did) in by_id:
            dns.append({'domain': by_id[int(did)]['name'], 'alias_id': aliasid,
                        'name': name, 'type': typ, 'data': data, 'status': status})
    aliases = []
    for did, alias, status in mysql('SELECT domain_id,alias_name,alias_status FROM imscp.domain_aliasses'):
        if int(did) in by_id:
            aliases.append({'domain': by_id[int(did)]['name'], 'alias': alias, 'status': status})
    return {'domains': domains, 'websites': websites, 'mail': mails,
            'databases': dbs, 'dns_records': dns, 'aliases': aliases}


def ssh(host, script, tty=False, capture=True):
    return run(['ssh', '-i', SSH_KEY, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes'] + (['-tt'] if tty else []) + [SSH_USER+'@'+host, script], capture=capture)


def sudo(host, command, capture=True):
    # sudo -n is intentional: safe for noninteractive automation; never consume
    # stdin intended for SQL as a password. User can first run ssh -t ... sudo -v,
    # or configure narrowly scoped NOPASSWD privileges.
    return ssh(host, 'sudo -n sh -c '+shlex.quote(command), capture=capture)


def remote_mysql(host, sql):
    # Query uses stdin? For read-only queries only, base64-free shell quoting.
    return sudo(host, 'mysql -N -B --raw dbispconfig -e '+shlex.quote(sql))


def remote_rows(host, sql):
    return [line.split('\t') for line in remote_mysql(host, sql).splitlines()]


def stage_dir(host, source, rel):
    target = '/home/imscp/imscp-migration-staging/'+rel
    ssh(host, 'mkdir -p '+shlex.quote(target))
    run(['rsync', '--info=progress2', '--no-inc-recursive', '-e', SSH_TRANSPORT, '-a', '--no-owner', '--no-group', '--no-perms', '--partial',
         source.rstrip('/')+'/', SSH_USER+'@'+host+':'+target.rstrip('/')+'/'], capture=False)
    return target


def stage_file(host, source, rel):
    target = '/home/imscp/imscp-migration-staging/'+rel
    ssh(host, 'mkdir -p '+shlex.quote(str(Path(target).parent)))
    run(['rsync', '--info=progress2', '--no-inc-recursive', '-e', SSH_TRANSPORT, '-a', '--partial', source, SSH_USER+'@'+host+':'+target], capture=False)
    return target


def import_sql(host, name, staged, local_archive, replace=False):
    """Import SQL; explicit replace makes a remote backup and restores on failure."""
    if not IDENT.fullmatch(name):
        raise RuntimeError('Unsafe SQL database name: '+name)
    if not staged.startswith('/home/imscp/imscp-migration-staging/sql/') or not staged.endswith('.sql.gz'):
        raise RuntimeError('Unsafe staged SQL path')
    # ISPConfig registration alone does not guarantee the database was created.
    exists = remote_rows(host, 'SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='+quote_sql(name))
    if len(exists) != 1:
        raise RuntimeError('Destination SQL database not yet physically created: '+name)
    hasher = hashlib.sha256()
    with local_archive.open('rb') as infile:
        for block in iter(lambda: infile.read(1024 * 1024), b''):
            hasher.update(block)
    digest = hasher.hexdigest()
    marker = '/var/lib/imscp-migration/sql/'+name+'.sha256'
    check = 'if test -f {m}; then cat {m}; else echo ABSENT; fi'.format(m=shlex.quote(marker))
    previous = sudo(host, check).strip()
    if previous == digest:
        print('EXISTS SQL IMPORT', name, '(matching checksum)')
        return
    tables = remote_rows(host, 'SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='+quote_sql(name))
    if len(tables) != 1:
        raise RuntimeError('Cannot inspect destination database: '+name)
    table_count = int(tables[0][0])
    if table_count and not replace:
        if previous != 'ABSENT':
            raise RuntimeError('Existing SQL import marker differs; use --replace-sql explicitly: '+name)
        raise RuntimeError('Destination database is not empty; SQL import refused: '+name)
    if previous != 'ABSENT' and not replace:
        raise RuntimeError('Existing SQL import marker differs; use --replace-sql explicitly: '+name)
    if replace and table_count:
        # Back up before any destructive operation. Never overwrite an earlier backup.
        # MariaDB/MySQL privileges for the schema remain valid after recreation.
        # Keep original character set/collation for the replacement database.
        settings = remote_rows(host, 'SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='+quote_sql(name))
        if len(settings) != 1 or not all(re.fullmatch(r'[A-Za-z0-9_]+', x) for x in settings[0]):
            raise RuntimeError('Cannot determine destination schema charset/collation: '+name)
        charset, collation = settings[0]
        backup = '/var/lib/imscp-migration/sql/backups/'+name+'-'+time.strftime('%Y%m%d-%H%M%S')+'.sql.gz'
        print('BACKUP SQL', name, '->', backup, flush=True)
        # A complete backup is required. Refuse replacement if backup fails.
        backup_cmd = ('set -euo pipefail; mkdir -p {directory}; '
                      'test ! -e {backup}; '
                      'mysqldump --single-transaction --quick --routines --triggers '
                      '--skip-events --no-tablespaces --default-character-set=utf8mb4 {db} '
                      '| gzip -c > {backup}; gzip -t {backup}').format(
                          directory=shlex.quote('/var/lib/imscp-migration/sql/backups'),
                          backup=shlex.quote(backup), db=shlex.quote(name))
        sudo(host, 'bash -c '+shlex.quote(backup_cmd))
        print('REPLACE SQL', name, '(backup verified; destination will be recreated)', flush=True)
        # Do not update marker until successful import. If import fails, attempt
        # automatic restoration of the previous database from the backup.
        create_sql = 'CREATE DATABASE `'+name+'` CHARACTER SET '+charset+' COLLATE '+collation
        drop_sql = 'DROP DATABASE `'+name+'`'
        restore = ('mysql -e '+shlex.quote(drop_sql+'; '+create_sql)+'; '
                   'gzip -dc '+shlex.quote(backup)+' | mysql --binary-mode=1 '+shlex.quote(name))
        replace_cmd = ("set -euo pipefail; test -s {archive}; gzip -t {archive}; "
                       "mysql -e {recreate}; "
                       "if ! gzip -dc {archive} | mysql --binary-mode=1 --default-character-set=utf8mb4 {db}; then "
                       "echo 'SQL replacement failed; restoring prior database' >&2; "
                       "{restore} || {{ echo 'CRITICAL: automatic restore failed; backup: {backup}' >&2; exit 97; }}; "
                       "exit 98; fi; "
                       "mkdir -p /var/lib/imscp-migration/sql; "
                       "printf '%s\n' {digest} > {marker}").format(
                           archive=shlex.quote(staged),
                           recreate=shlex.quote(drop_sql+'; '+create_sql),
                           db=shlex.quote(name), restore=restore,
                           backup=shlex.quote(backup), digest=shlex.quote(digest),
                           marker=shlex.quote(marker))
        sudo(host, 'bash -c '+shlex.quote(replace_cmd))
        print('REPLACED SQL', name, 'sha256='+digest[:12], 'backup='+backup)
        return
    command = ("set -e; test -f {archive}; "
               "gzip -t {archive}; "
               "gzip -dc {archive} | mysql --binary-mode=1 --default-character-set=utf8mb4 {database}; "
               "mkdir -p /var/lib/imscp-migration/sql; "
               "printf '%s\\n' {digest} > {marker}").format(
                   archive=shlex.quote(staged), database=shlex.quote(name),
                   digest=shlex.quote(digest), marker=shlex.quote(marker))
    # bash pipefail makes gzip failures fail the operation, even if mysql exits 0.
    sudo(host, 'bash -o pipefail -c '+shlex.quote(command))
    print('IMPORTED SQL', name, 'sha256='+digest[:12])


def maildir_path(source):
    p = Path(source)
    for candidate in (p, p/'Maildir'):
        if (candidate/'cur').is_dir() and (candidate/'new').is_dir() and (candidate/'tmp').is_dir():
            return str(candidate)
    return None


def web_dest(host, web):
    """Resolve the ISPConfig-owned web path; never guess /var/www/clientN/webM."""
    hostname = web['hostname']
    rows = remote_rows(host, 'SELECT domain_id,document_root,system_user,system_group,type,parent_domain_id,web_folder '
                       'FROM web_domain WHERE domain='+quote_sql(hostname))
    if len(rows) != 1:
        raise RuntimeError('Web missing or ambiguous in ISPConfig: '+hostname)
    ident, root, user, group, typ, parent_id, folder = rows[0]
    if web['kind'] == 'subdomain':
        if typ != 'vhostsubdomain':
            # A legacy independent website must not be overwritten or silently converted.
            print('REVIEW WEB: existing independent site, skipping transfer:', hostname, 'type='+typ)
            return None
        parents = remote_rows(host, 'SELECT domain_id,document_root,system_user,system_group,type '
                              'FROM web_domain WHERE domain='+quote_sql(web['domain']))
        if len(parents) != 1 or parents[0][4] != 'vhost' or str(parent_id) != str(parents[0][0]):
            raise RuntimeError('Subdomain has wrong ISPConfig parent: '+hostname)
        _, parent_root, parent_user, parent_group, _ = parents[0]
        expected_folder = hostname[:-(len(web['domain'])+1)]
        if '.' in expected_folder or folder != expected_folder:
            raise RuntimeError('Subdomain web_folder mismatch for '+hostname+': '+str(folder))
        if (root, user, group) != (parent_root, parent_user, parent_group):
            raise RuntimeError('Subdomain root/ownership mismatch with parent: '+hostname)
        dest = parent_root.rstrip('/')+'/web/'+folder
    else:
        if typ != 'vhost':
            raise RuntimeError('Expected main vhost for '+hostname+', found '+typ)
        dest = root.rstrip('/')+'/web'
    if not root.startswith('/var/www/') or not user.startswith('web') or not group.startswith('client'):
        raise RuntimeError('Unsafe ISPConfig web ownership/path: '+hostname)
    if '..' in Path(dest).parts or not dest.startswith('/var/www/'):
        raise RuntimeError('Unsafe ISPConfig destination path: '+dest)
    return dest, user, group


def mail_dest(host, address):
    # The maildir field is the mailbox HOME, not necessarily the actual
    # Maildir storage directory. ISPConfig/Dovecot uses maildir/Maildir
    # when maildir_format='maildir' (check the destination Dovecot config).
    rows = remote_rows(host, 'SELECT maildir,maildir_format FROM mail_user WHERE email='+quote_sql(address))
    if len(rows) != 1 or len(rows[0]) != 2:
        raise RuntimeError('Mailbox missing or ambiguous in ISPConfig: '+address)
    home, fmt = rows[0]
    if not home.startswith('/var/vmail/') or '..' in Path(home).parts:
        raise RuntimeError('Unsafe ISPConfig maildir path: '+address)
    if fmt != 'maildir':
        raise RuntimeError('Unsupported destination maildir_format for '+address+': '+fmt)
    return home.rstrip('/')+'/Maildir'


def install_web(host, staged, dest, user, group):
    command = ('test -d {d} && test ! -L {d} && '
               'rsync -a --info=progress2 --no-inc-recursive --ignore-existing --no-owner --no-group --chown={owner} {s}/ {d}/').format(
                   d=shlex.quote(dest), s=shlex.quote(staged), owner=shlex.quote(user+':'+group))
    sudo(host, command, capture=False)


def install_mail(host, staged, dest):
    # Only migrate Maildir message folders, not Courier/Dovecot indices,
    # Sieve scripts, ISPConfig metadata or a nested source Maildir.
    # Keep existing destination messages untouched.
    excludes = [
        '--exclude=Maildir/', '--exclude=dovecot*',
        '--exclude=courier*', '--exclude=maildirsize',
        '--exclude=subscriptions', '--exclude=sieve/',
        '--exclude=.ispconfig*', '--exclude=quotawarn',
        '--exclude=*.svbin',
    ]
    command = ('test -d {d} && test ! -L {d} && '
               'rsync -a --info=progress2 --no-inc-recursive --ignore-existing '
               '--chown=vmail:vmail {excludes} {s}/ {d}/').format(
                   d=shlex.quote(dest), s=shlex.quote(staged),
                   excludes=' '.join(shlex.quote(e) for e in excludes))
    sudo(host, command, capture=False)



def sync_mail_incremental(host, source, dest, address, dry_run=False):
    """Direct source -> destination Maildir sync; no staging, API, or deletions.

    Use incremental recursion and filename/existence checks only; never checksum
    existing messages. Progress totals can grow while directories are discovered.
    Requires passwordless sudo for remote rsync, and rsync on both hosts.
    """
    if not source.startswith('/var/mail/virtual/') or not dest.startswith('/var/vmail/'):
        raise RuntimeError('Unsafe mail sync path for '+address)
    if not Path(source).is_dir():
        raise RuntimeError('Missing source Maildir for '+address)
    # Never create a mailbox implicitly or follow a symlinked destination.
    sudo(host, 'test -d {d} && test ! -L {d} && test -d {d}/cur && test -d {d}/new'.format(
        d=shlex.quote(dest)))
    excludes = [
        '--exclude=Maildir/', '--exclude=dovecot*', '--exclude=courier*',
        '--exclude=maildirsize', '--exclude=subscriptions', '--exclude=sieve/',
        '--exclude=.ispconfig*', '--exclude=quotawarn', '--exclude=*.svbin',
    ]
    command = [
        'rsync', '--info=progress2,flist2', '--inc-recursive', '-a',
        '--ignore-existing', '--whole-file', '--no-compress',
        # --chown requires owner/group preservation; --no-owner/--no-group
        # would cancel it and leave new Maildir directories owned by root.
        '--chown=vmail:vmail',
        '--partial', '-e', SSH_TRANSPORT, '--rsync-path=sudo -n rsync',
    ]
    if dry_run:
        command += ['--dry-run', '--itemize-changes']
    command += excludes + [source.rstrip('/')+'/', SSH_USER+'@'+host+':'+dest.rstrip('/')+'/']
    print(('DRY RUN' if dry_run else 'SYNC MAIL'), address, source, '->', dest, flush=True)
    run(command, capture=False)


def dump_db(name):
    if not IDENT.fullmatch(name):
        raise RuntimeError('Invalid SQL database name: '+name)
    directory = ROOT/'dumps'
    directory.mkdir(exist_ok=True)
    path = directory/(name+'.sql.gz')
    tmp = Path(str(path)+'.part')
    with tmp.open('wb') as output:
        p1 = subprocess.Popen(['mysqldump', '--single-transaction', '--quick',
                               '--routines', '--triggers', '--skip-events', '--no-tablespaces',
                               '--default-character-set=utf8mb4', name],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(['gzip', '-c'], stdin=p1.stdout, stdout=output, stderr=subprocess.PIPE)
        p1.stdout.close()
        err2 = p2.communicate()[1]
        err1 = p1.stderr.read()
        if p1.wait() or p2.returncode:
            if tmp.exists():
                tmp.unlink()
            raise RuntimeError('mysqldump failed: '+(err1+err2).decode(errors='replace')[-300:])
    tmp.replace(path)
    return path



# ISPConfig JSON remote API: POST /remote/json.php?method with a JSON object.
class ISPApi:
    def __init__(self, endpoint, username, password, verify_tls=True):
        if not endpoint.startswith('https://'):
            raise RuntimeError('ISPConfig API endpoint must use HTTPS')
        self.endpoint = endpoint
        self.context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        self.session = None
        self.session = self.call('login', {'username': username, 'password': password}, auth=False)
        if not self.session:
            raise RuntimeError('ISPConfig login returned no session ID')

    def call(self, method, params, auth=True):
        payload = dict(params)
        if auth:
            payload['session_id'] = self.session
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(self.endpoint+'?'+method, data=data,
                                     headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=45, context=self.context) as response:
                result = json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, ValueError) as exc:
            raise RuntimeError('ISPConfig API transport/JSON error in '+method+': '+str(exc))
        if not isinstance(result, dict) or result.get('code') != 'ok':
            message = str(result.get('message', 'Unknown API error')) if isinstance(result, dict) else 'Invalid API response'
            message = re.split(r'\s+(?:INSERT|UPDATE|REPLACE)\s+', message, maxsplit=1, flags=re.I)[0]
            message = re.sub(r'\*[0-9a-fA-F]{40}', '[SQL HASH REDACTED]', message)
            message = re.sub(r'\$[156]\$[^\s,;]+', '[MAIL HASH REDACTED]', message)
            message = re.sub(r'(?i)(password|passwd|secret)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', message)
            raise RuntimeError('ISPConfig API rejected '+method+': '+message[:500])
        return result.get('response')

    def close(self):
        if self.session:
            try:
                self.call('logout', {})
            finally:
                self.session = None


def source_columns(table):
    if not re.fullmatch(r'[A-Za-z_]+', table):
        raise RuntimeError('Unsafe table name')
    return {r[0] for r in mysql('SHOW COLUMNS FROM imscp.'+table)}


def source_mail_hash(address):
    columns = source_columns('mail_users')
    field = next((x for x in ('mail_pass', 'mail_password', 'password') if x in columns), None)
    if not field:
        raise RuntimeError('Cannot find i-MSCP mail password column; refusing to create mailbox '+address)
    rows = mysql('SELECT '+field+' FROM imscp.mail_users WHERE mail_addr='+quote_sql(address))
    if len(rows) != 1 or not rows[0][0] or rows[0][0] == 'NULL':
        raise RuntimeError('Missing/ambiguous source password hash for '+address)
    return rows[0][0]


def source_sql_hash(user, host):
    # MariaDB 10.3 stores mysql_native_password hashes in mysql.user.Password.
    rows = mysql('SELECT Password,plugin FROM mysql.user WHERE User='+quote_sql(user)+
                 ' AND Host='+quote_sql(host))
    if len(rows) != 1 or len(rows[0]) != 2:
        raise RuntimeError('Missing or ambiguous SQL authentication for user '+user+'@'+host)
    value, plugin = rows[0]
    plugin = (plugin or '').strip()
    # On some MariaDB 10.3 installations mysql.user.plugin is empty even
    # though Password contains a valid mysql_native_password hash.
    if plugin not in ('', 'mysql_native_password'):
        raise RuntimeError('Unsupported SQL authentication for user '+user+'@'+host)
    if not re.fullmatch(r'\*[0-9A-Fa-f]{40}', value or ''):
        raise RuntimeError('Unexpected SQL password hash for '+user+'@'+host)
    return value


def db_rows_by(host, table, key, value, columns):
    if not all(re.fullmatch(r'[a-z_]+', x) for x in [table, key]+columns):
        raise RuntimeError('Invalid destination SQL identifier')
    return remote_rows(host, 'SELECT '+','.join(columns)+' FROM '+table+' WHERE '+key+'='+quote_sql(value))


def provision_web(api, host, web, client_id, server_id, ip):
    hostname = web['hostname']
    is_sub = web['kind'] == 'subdomain'
    rows = db_rows_by(host, 'web_domain', 'domain', hostname,
                      ['domain_id', 'type', 'parent_domain_id', 'web_folder'])
    if rows:
        if len(rows) != 1:
            raise RuntimeError('Conflicting existing website: '+hostname)
        typ = rows[0][1]
        if is_sub and typ != 'vhostsubdomain':
            print('REVIEW WEB: existing site is not a vhost subdomain; leaving unchanged:', hostname, 'type='+typ)
            return
        if not is_sub and typ != 'vhost':
            raise RuntimeError('Conflicting existing main website: '+hostname+' type='+typ)
        print('EXISTS WEB', hostname, 'type='+typ)
        return
    if is_sub:
        parents = db_rows_by(host, 'web_domain', 'domain', web['domain'],
                             ['domain_id', 'type', 'server_id'])
        if len(parents) != 1 or parents[0][1] != 'vhost' or int(parents[0][2]) != server_id:
            raise RuntimeError('Missing/invalid parent website for subdomain '+hostname)
        folder = hostname[:-(len(web['domain'])+1)]
        if not re.fullmatch(r'[a-z0-9-]+', folder):
            raise RuntimeError('Unsafe subdomain web folder: '+folder)
        params = {'server_id': server_id, 'ip_address': ip, 'domain': hostname,
                  'parent_domain_id': int(parents[0][0]), 'web_folder': folder,
                  'type': 'vhostsubdomain', 'vhost_type': 'name',
                  'php': 'php-fpm', 'active': 'y', 'suexec': 'y',
                  'http_port': '80', 'https_port': '443',
                  'ssl': 'n', 'ssl_letsencrypt': 'n'}
        method = 'sites_web_vhost_subdomain_add'
    else:
        params = {'server_id': server_id, 'ip_address': ip, 'domain': hostname,
                  'type': 'vhost', 'parent_domain_id': 0, 'vhost_type': 'name',
                  'hd_quota': -1, 'traffic_quota': -1, 'cgi': 'n', 'ssi': 'n',
                  'suexec': 'y', 'errordocs': 1, 'is_subdomainwww': 1,
                  'subdomain': 'www', 'php': 'php-fpm', 'active': 'y',
                  'allow_override': 'All', 'http_port': '80', 'https_port': '443',
                  'pm': 'dynamic', 'pm_max_children': 10,
                  'pm_start_servers': 2, 'pm_min_spare_servers': 1,
                  'pm_max_spare_servers': 5, 'pm_process_idle_timeout': 10,
                  'pm_max_requests': 0, 'ssl': 'n', 'lets_encrypt': 'n'}
        method = 'sites_web_domain_add'
    result = api.call(method, {'client_id': client_id, 'params': params})
    print('CREATED WEB', hostname, 'type='+params['type'], 'id='+str(result))


def provision_mail_domain(api, host, domain, client_id, server_id, activate=False):
    rows = db_rows_by(host, 'mail_domain', 'domain', domain, ['domain_id', 'active', 'server_id'])
    if rows:
        if len(rows) != 1:
            raise RuntimeError('Ambiguous mail domain: '+domain)
        if activate and rows[0][1] != 'y':
            if int(rows[0][2]) != server_id:
                raise RuntimeError('Mail domain server mismatch: '+domain)
            # Only change active; API behavior with partial updates must be validated.
            params = {'server_id': server_id, 'domain': domain, 'active': 'y'}
            api.call('mail_domain_update', {'client_id': client_id,
                                           'primary_id': int(rows[0][0]), 'params': params})
            print('ACTIVATED MAIL DOMAIN', domain)
        else:
            print('EXISTS MAIL DOMAIN', domain, 'active='+rows[0][1])
        return
    params = {'server_id': server_id, 'domain': domain, 'active': 'y' if activate else 'n',
              'dkim': 'n', 'local_delivery': 'y'}
    result = api.call('mail_domain_add', {'client_id': client_id, 'params': params})
    print('CREATED MAIL DOMAIN', domain, 'active='+params['active'], 'id='+str(result))


def provision_mailbox(api, host, mail, client_id, server_id):
    address = mail['address']
    rows = db_rows_by(host, 'mail_user', 'email', address, ['mailuser_id'])
    if rows:
        if len(rows) != 1:
            raise RuntimeError('Ambiguous existing mailbox: '+address)
        print('EXISTS MAILBOX', address)
        return
    # A forwarder occupying this address must not be replaced by a mailbox.
    aliases = db_rows_by(host, 'mail_forwarding', 'source', address, ['forwarding_id'])
    if aliases:
        raise RuntimeError('Existing forwarding conflicts with mailbox '+address)
    hashed = source_mail_hash(address)
    if not hashed.startswith(('$1$', '$5$', '$6$', '$2y$', '$2a$')):
        raise RuntimeError('Unsupported source mailbox password hash for '+address)
    params = {'server_id': server_id, 'email': address, 'login': address,
              'password': hashed, '_ispconfig_pw_crypted': 1,
              'name': '', 'quota': 0,
              'autoresponder': 'n', 'postfix': 'y',
              'disableimap': 'n', 'disablepop3': 'n', 'disabledeliver': 'n',
              'disablesmtp': 'n', 'move_junk': 'n', 'purge_trash_days': 0,
              'purge_junk_days': 0, 'backup_interval': 'none', 'backup_copies': 1, 'active': 'y'}
    result = api.call('mail_user_add', {'client_id': client_id, 'params': params})
    print('CREATED MAILBOX', address, 'id='+str(result))


def provision_forward(api, host, address, target, client_id, server_id):
    if not target or '@' not in target or target == address:
        raise RuntimeError('Invalid forwarding for '+address)
    existing = db_rows_by(host, 'mail_forwarding', 'source', address,
                          ['forwarding_id', 'destination'])
    if existing:
        if len(existing) == 1 and existing[0][1].strip() == target:
            print('EXISTS FORWARD', address)
            return
        raise RuntimeError('Conflicting forwarding: '+address)
    if db_rows_by(host, 'mail_user', 'email', address, ['mailuser_id']):
        raise RuntimeError('Mailbox conflicts with forwarding: '+address)
    result = api.call('mail_forward_add', {'client_id': client_id,
                       'params': {'server_id': server_id, 'source': address,
                                  'destination': target, 'type': 'forward', 'active': 'y'}})
    print('CREATED FORWARD', address, '->', target, 'id='+str(result))


def provision_sql(api, host, db, client_id, server_id):
    name = db['name']
    if not IDENT.fullmatch(name):
        raise RuntimeError('Invalid SQL database name: '+name)
    existing = db_rows_by(host, 'web_database', 'database_name', name,
                          ['database_id', 'database_user_id'])
    if existing:
        if len(existing) != 1:
            raise RuntimeError('Ambiguous existing SQL database: '+name)
        print('EXISTS SQL DATABASE', name)
        return
    users = [x for x in db['users'] if x['user'] and x['user'] != 'NULL']
    if len(users) != 1:
        raise RuntimeError('SQL database requires exactly one associated user: '+name)
    user, userhost = users[0]['user'], users[0]['host']
    if not IDENT.fullmatch(user):
        raise RuntimeError('Invalid SQL user: '+user)
    userrows = db_rows_by(host, 'web_database_user', 'database_user', user,
                          ['database_user_id'])
    if len(userrows) > 1:
        raise RuntimeError('Ambiguous existing SQL user '+user)
    if userrows:
        uid = int(userrows[0][0])
    else:
        hashed = source_sql_hash(user, userhost)
        uid = int(api.call('sites_database_user_add', {'client_id': client_id,
                  'params': {'server_id': server_id, 'database_user': user,
                             'database_user_prefix': '', 'database_password': hashed,
                             '_ispconfig_pw_crypted': 1}}))
        print('CREATED SQL USER', user, 'id='+str(uid))
    params = {'server_id': server_id, 'parent_domain_id': 0, 'type': 'mysql',
              'database_name': name, 'database_name_prefix': '', 'database_quota': -1,
              'database_user_id': uid, 'database_ro_user_id': 0,
              'database_charset': 'utf8mb4', 'remote_access': 'n', 'remote_ips': '',
              'backup_interval': 'none', 'backup_copies': 1, 'active': 'y'}
    result = api.call('sites_database_add', {'client_id': client_id, 'params': params})
    print('CREATED SQL DATABASE', name, 'id='+str(result))



# DNS import: do not infer ambiguous records. API operations require --include-dns
# AND --dns-apply. Existing DNS zones must have been created via ISPConfig wizard.
DNS_SUPPORTED = {'A', 'AAAA', 'CNAME', 'MX', 'TXT', 'NS', 'SRV', 'CAA'}


def dns_fqdn(name, domain):
    name = name.strip().lower()
    domain = domain.lower().rstrip('.')
    if name in ('', '@', domain, domain+'.'):
        return domain+'.'
    if name.endswith('.'):
        result = name
    elif name.endswith('.'+domain):
        result = name+'.'
    else:
        result = name+'.'+domain+'.'
    labels = result.rstrip('.').split('.')
    if any(len(label) > 63 or not DNS_OWNER_LABEL.fullmatch(label) for label in labels) or len(result) > 254:
        raise RuntimeError('Invalid DNS owner name: '+name)
    if not (result == domain+'.' or result.endswith('.'+domain+'.')):
        raise RuntimeError('DNS owner outside zone: '+name)
    return result


def parse_dns_record(rec):
    import ipaddress
    typ = rec['type'].strip().upper()
    if typ not in DNS_SUPPORTED:
        raise RuntimeError('Unsupported DNS record type '+repr(typ))
    domain = rec['domain'].lower()
    # i-MSCP domain_dns.domain_dns may encode the TTL after the owner,
    # e.g. 'mail._domainkey 60'. Keep the owner and TTL separate.
    source_owner = rec['name'].strip()
    # i-MSCP may store a literal backslash-t sequence (\\t) rather than
    # an actual tab between the DNS owner and TTL. Normalize only that
    # separator before parsing; do not weaken DNS owner validation.
    source_owner = source_owner.replace('\\t', '\t')
    ttl = 3600
    ttl_match = re.fullmatch(r'(\S+)\s+(\d{1,10})', source_owner)
    if ttl_match:
        source_owner = ttl_match.group(1)
        ttl = int(ttl_match.group(2))
        if not 1 <= ttl <= 2147483647:
            raise RuntimeError('Invalid DNS TTL for '+source_owner)
    owner = dns_fqdn(source_owner, domain)
    raw = rec['data'].strip()
    if not raw or raw.upper() == 'NULL' or '\n' in raw or '\r' in raw:
        raise RuntimeError('Empty or multiline DNS data for '+owner)
    priority = 0
    if typ in ('A', 'AAAA'):
        address = ipaddress.ip_address(raw)
        if address.version != (4 if typ == 'A' else 6):
            raise RuntimeError('Wrong IP version for '+owner)
        data = str(address)
    elif typ in ('CNAME', 'NS'):
        if not HOSTNAME.fullmatch(raw.rstrip('.')):
            raise RuntimeError('Invalid DNS target for '+owner)
        data = raw.rstrip('.')+'.'
    elif typ == 'MX':
        m = re.fullmatch(r'(\d{1,5})\s+([^\s]+)', raw)
        if not m:
            raise RuntimeError('MX must be priority and hostname: '+owner)
        priority, data = int(m.group(1)), m.group(2).rstrip('.')+'.'
        if priority > 65535 or not HOSTNAME.fullmatch(data.rstrip('.')):
            raise RuntimeError('Invalid MX value: '+owner)
    elif typ == 'TXT':
        # Keep text as exported. No automatic quote stripping or SPF rewrites.
        data = raw
    elif typ == 'SRV':
        m = re.fullmatch(r'(\d{1,5})\s+(\d{1,5})\s+(\d{1,5})\s+([^\s]+)', raw)
        if not m:
            raise RuntimeError('SRV must be priority weight port target: '+owner)
        priority = int(m.group(1))
        data = ' '.join(m.group(i) for i in (2,3))+' '+m.group(4).rstrip('.')+'.'
    elif typ == 'CAA':
        m = re.fullmatch(r'(\d{1,3})\s+(issue|issuewild|iodef)\s+(.+)', raw, re.I)
        if not m or int(m.group(1)) > 255:
            raise RuntimeError('CAA must be flags tag value: '+owner)
        data = raw
    return {'type': typ, 'name': owner, 'data': data, 'aux': priority, 'ttl': ttl}


def dns_records_for(inv, selected):
    result = []
    errors = []
    for rec in inv['dns_records']:
        if rec['domain'] not in selected:
            continue
        if rec['status'].lower() not in ('ok', 'toadd', 'tochange'):
            continue
        # Legacy DKIM/ADSP records are intentionally not migrated.  Filter
        # them BEFORE validating zone ownership: old i-MSCP inventories can
        # associate an ADSP owner with a different domain.  Do not weaken
        # zone validation for any other DNS record.
        raw_owner = rec['name'].replace('\\t', '\t').split()[0].lower().rstrip('.')
        if rec['type'].strip().upper() == 'TXT' and (
                raw_owner.startswith('_adsp._domainkey.')
                or '._domainkey.' in raw_owner
                or raw_owner.startswith('_domainkey.')):
            print('REVIEW DNS MAIL AUTH (not imported)', rec['domain'], raw_owner + '.')
            continue
        try:
            result.append((rec['domain'], parse_dns_record(rec)))
        except (RuntimeError, ValueError) as exc:
            errors.append(rec['domain']+' '+rec['name']+' '+rec['type']+': '+str(exc))
    return result, errors


def dns_zone_id(host, domain):
    rows = db_rows_by(host, 'dns_soa', 'origin', domain+'.', ['id', 'active'])
    if len(rows) > 1:
        raise RuntimeError('Ambiguous ISPConfig DNS zone '+domain)
    return (int(rows[0][0]), rows[0][1]) if rows else (None, None)


def ensure_dns_zone(api, host, domain, args):
    """Create a zone and baseline records equivalent to the tested Default wizard.

    Uses documented remote API methods rather than assuming a DNS wizard API.
    A partially created zone is intentionally left for safe retry.
    """
    zone_id, active = dns_zone_id(host, domain)
    if zone_id is not None:
        return zone_id, active, False
    if not args.create_dns_zones:
        raise RuntimeError('DNS zone missing for '+domain+'; use --create-dns-zones with --dns-apply')
    if not args.dns_ns1 or not args.dns_ns2:
        raise RuntimeError('New DNS zones require --dns-ns1 and --dns-ns2')
    ns1 = valid_host(args.dns_ns1.rstrip('.'))+'.'
    ns2 = valid_host(args.dns_ns2.rstrip('.'))+'.'
    origin = domain+'.'
    params = {'server_id': args.server_id, 'origin': origin, 'ns': ns1,
              'mbox': 'webmaster.'+origin, 'refresh': 7200, 'retry': 540,
              'expire': 604800, 'minimum': 3600, 'ttl': 3600,
              'active': 'y', 'xfer': '', 'also_notify': '', 'update_acl': ''}
    result = api.call('dns_zone_add', {'client_id': args.client_id, 'params': params})
    print('CREATED DNS ZONE', domain, 'id='+str(result))
    zone_id, active = dns_zone_id(host, domain)
    if zone_id is None:
        raise RuntimeError('DNS zone created but not visible yet: '+domain+'; rerun after synchronization')
    return zone_id, active, True


def default_dns_records(domain, args):
    origin = domain+'.'
    mail = 'mail.'+origin
    ns1 = valid_host(args.dns_ns1.rstrip('.'))+'.'
    ns2 = valid_host(args.dns_ns2.rstrip('.'))+'.'
    return [
        {'type':'A','name':origin,'data':args.ip,'aux':0,'ttl':3600},
        {'type':'A','name':mail,'data':args.ip,'aux':0,'ttl':3600},
        {'type':'A','name':'www.'+origin,'data':args.ip,'aux':0,'ttl':3600},
        {'type':'MX','name':origin,'data':mail,'aux':10,'ttl':3600},
        {'type':'NS','name':origin,'data':ns1,'aux':0,'ttl':3600},
        {'type':'NS','name':origin,'data':ns2,'aux':0,'ttl':3600},
        {'type':'TXT','name':origin,'data':'v=spf1 mx a ~all','aux':0,'ttl':3600},
    ]


def normalize_txt_for_ispconfig(value):
    """Remove one pair of DNS master-file outer quotes, if present.

    Preserve the TXT payload itself (including internal quotes and escapes).
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def dns_record_identity(typ, name, data, aux):
    """Compare TXT records independently of master-file outer quoting."""
    typ = str(typ).upper()
    if typ == 'TXT':
        data = normalize_txt_for_ispconfig(str(data))
    return (typ, str(name).lower().rstrip('.') + '.', str(data), str(aux))


def add_dns_record(api, domain, zone_id, rec, args):
    method = 'dns_'+rec['type'].lower()+'_add'
    params = {'server_id': args.server_id, 'zone': zone_id,
              'name': rec['name'], 'type': rec['type'].lower(),
              'data': normalize_txt_for_ispconfig(rec['data']) if rec['type'] == 'TXT' else rec['data'], 'aux': rec['aux'],
              'ttl': rec['ttl'], 'active': 'y'}
    try:
        rid = api.call(method, {'client_id': args.client_id, 'params': params})
    except Exception as exc:
        raise RuntimeError('DNS '+domain+' '+rec['type']+' '+rec['name']+': '+str(exc)) from exc
    print('CREATED DNS ACTIVE', domain, rec['type'], rec['name'], 'id='+str(rid))


def provision_dns(api, host, inv, selected, args):
    records, errors = dns_records_for(inv, selected)
    if errors:
        raise RuntimeError('DNS import blocked; review source records:\n'+'\n'.join(errors[:30]))
    # New zones are created through the documented API; baseline records are
    # provisioned separately. This does not change registrar delegation.
    by_domain = {}
    for domain, record in records:
        by_domain.setdefault(domain, []).append(record)
    for domain in sorted(selected):
        zone_id, active, newly_created = ensure_dns_zone(api, host, domain, args)
        print('EXISTS DNS ZONE', domain, 'id='+str(zone_id), 'active='+active)
        if str(active).strip().lower() != 'y':
            raise RuntimeError('DNS zone '+domain+' is inactive; activate it in ISPConfig before importing records')
        # Query destination for exact record identity; never overwrite existing data.
        rows = remote_rows(host, 'SELECT type,name,data,aux FROM dns_rr WHERE zone='+str(zone_id))
        existing = {dns_record_identity(r[0], r[1], r[2], r[3]) for r in rows if len(r) == 4}
        occupied = {(r[0].upper(), r[1].lower().rstrip('.')+'.') for r in rows if len(r) == 4}
        if newly_created:
            # Populate only newly created zones; never inject default records
            # into a pre-existing, potentially custom zone.
            for baseline in default_dns_records(domain, args):
                bkey = dns_record_identity(baseline['type'], baseline['name'], baseline['data'], baseline['aux'])
                if bkey in existing:
                    continue
                if baseline['type'] != 'NS' and (baseline['type'], baseline['name'].lower()) in occupied:
                    print('REVIEW DNS DEFAULT CONFLICT', domain, baseline['type'], baseline['name'])
                    continue
                add_dns_record(api, domain, zone_id, baseline, args)
                existing.add(bkey)
                occupied.add((baseline['type'], baseline['name'].lower()))
        for rec in by_domain.get(domain, []):
            key = dns_record_identity(rec['type'], rec['name'], rec['data'], rec['aux'])
            if key in existing:
                print('EXISTS DNS', domain, rec['type'], rec['name'])
                continue
            # Avoid replacing SPF/DKIM generated by ISPConfig. ADSP is obsolete;
            # flag these records for review rather than blindly publishing them.
            owner = rec['name'].lower()
            if rec['type'] == 'TXT' and ('._domainkey.' in owner or owner.startswith('_adsp.')):
                print('REVIEW DNS MAIL AUTH (not imported)', domain, rec['name'])
                continue
            if (rec['type'], rec['name'].lower()) in occupied:
                print('REVIEW DNS CONFLICT (not overwritten)', domain, rec['type'], rec['name'])
                continue
            # Some ISPConfig releases do not expose dns_caa_add. Stop explicitly.
            method = 'dns_'+rec['type'].lower()+'_add'
            params = {'server_id': args.server_id, 'zone': zone_id,
                      'name': rec['name'], 'type': rec['type'],
                      'data': rec['data'], 'aux': rec['aux'],
                      'ttl': rec['ttl'], 'active': 'y'}
            add_dns_record(api, domain, zone_id, rec, args)
            occupied.add((rec['type'], rec['name'].lower()))
            existing.add(key)

def mail_forwards(mail):
    # Forwarders in i-MSCP may have comma/newline-separated destinations.
    value = mail.get('forward', '').strip()
    if not value or value.lower() in ('null', 'no'):
        return []
    return [v.strip() for v in re.split(r'[,;\s]+', value) if '@' in v]


def provision_resources(api, args, inv, selected):
    # Only API operations in this phase. DNS is handled separately.
    for web in sorted(inv['websites'], key=lambda w: (w['kind'] != 'main', w['hostname'])):
        if web['domain'] in selected and not web['skip'] and Path(web['source']).is_dir() and not args.skip_web:
            provision_web(api, args.host, web, args.client_id,
                          args.server_id, args.ip)
    if not args.skip_mail:
        for domain in sorted(selected):
            provision_mail_domain(api, args.host, domain, args.client_id, args.server_id, args.activate_mail)
        for mail in inv['mail']:
            if mail['domain'] not in selected:
                continue
            if mail['merge_into']:
                provision_forward(api, args.host, mail['address'], mail['merge_into'],
                                  args.client_id, args.server_id)
            elif mail['mailbox']:
                provision_mailbox(api, args.host, mail, args.client_id, args.server_id)
            else:
                targets = mail_forwards(mail)
                if targets:
                    provision_forward(api, args.host, mail['address'], ','.join(targets),
                                      args.client_id, args.server_id)
                else:
                    print('REVIEW MAIL ENTRY (no destination)', mail['address'], mail['type'])
    if args.include_sql:
        for db in inv['databases']:
            if db['domain'] in selected and db['exists']:
                provision_sql(api, args.host, db, args.client_id, args.server_id)


def main():
    # Load non-secret environment settings before parsing normal CLI arguments.
    # Explicit CLI arguments override scalar values from the config file.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config')
    preliminary, _ = pre.parse_known_args()
    config_argv = []
    if preliminary.config:
        settings = json.loads(Path(preliminary.config).read_text())
        if not isinstance(settings, dict):
            pre.error('Config file must contain a JSON object')
        allowed = {'host', 'api-url', 'api-user', 'client-id', 'server-id', 'ip',
                   'ssh-user', 'ssh-key', 'exclude-domain', 'exclude-database',
                   'skip-web-domain', 'mail-merge'}
        for key, value in settings.items():
            if key not in allowed:
                pre.error('Unknown config setting: '+key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, (str, int)):
                    pre.error('Invalid config value for '+key)
                config_argv.extend(['--'+key, str(item)])
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', help='JSON file with environment options; CLI scalar options override config')
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--inventory', action='store_true')
    group.add_argument('--plan', action='store_true')
    group.add_argument('--apply', action='store_true')
    group.add_argument('--sync-mail-only', action='store_true', help='Incremental Maildir sync only: no API, web, SQL, DNS, or staging')
    ap.add_argument('--domain')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--host', help='Destination SSH hostname (required for --apply / --sync-mail-only)')
    ap.add_argument('--include-sql', action='store_true')
    ap.add_argument('--import-sql', action='store_true', help='Import staged SQL (requires --include-sql)')
    ap.add_argument('--replace-sql', action='store_true', help='Explicitly replace nonempty destination SQL databases after creating a remote backup (requires --import-sql)')
    ap.add_argument('--include-dns', action='store_true', help='Preview DNS records; no writes without --dns-apply')
    ap.add_argument('--dns-apply', action='store_true', help='Create DNS records via API')
    ap.add_argument('--create-dns-zones', action='store_true', help='Create missing zones and reconcile Default baseline records')
    ap.add_argument('--activate-mail', action='store_true', help='Activate mail domains in ISPConfig (does not change public MX)')
    ap.add_argument('--dns-ns1', help='Primary nameserver FQDN for newly created zones')
    ap.add_argument('--dns-ns2', help='Secondary nameserver FQDN for newly created zones')
    ap.add_argument('--skip-web', action='store_true')
    ap.add_argument('--skip-mail', action='store_true')
    ap.add_argument('--api-url', help='ISPConfig Remote API URL (required for --apply)')
    ap.add_argument('--api-user', help='ISPConfig Remote API username (required for --apply)')
    ap.add_argument('--client-id', type=int, help='ISPConfig client ID (required for --apply)')
    ap.add_argument('--server-id', type=int, help='ISPConfig server ID (required for --apply)')
    ap.add_argument('--ip', help='Destination web IP (required for --apply)')
    ap.add_argument('--ssh-user', default='imscp', help='Destination SSH user (default: imscp)')
    ap.add_argument('--ssh-key', default='/root/.ssh/id_rsa', help='SSH private key path')
    ap.add_argument('--exclude-domain', action='append', default=[], metavar='DOMAIN', help='Exclude a domain and its resources; repeatable')
    ap.add_argument('--exclude-database', action='append', default=[], metavar='DATABASE', help='Exclude a SQL database; repeatable')
    ap.add_argument('--skip-web-domain', action='append', default=[], metavar='DOMAIN', help='Do not migrate main website for this domain; repeatable')
    ap.add_argument('--mail-merge', action='append', default=[], metavar='SOURCE=TARGET', help='Stage special source mailbox for manual merge into target; repeatable')
    ap.add_argument('--confirm-apply', action='store_true', help='Required for --apply')
    ap.add_argument('--api-only', action='store_true', help='Provision records without transferring content')
    ap.add_argument('--dry-run', action='store_true', help='With --sync-mail-only, preview rsync without writing')
    args = ap.parse_args(config_argv + sys.argv[1:])
    global SSH_KEY, SSH_USER, SSH_TRANSPORT, EXCLUDED_DOMAINS, EXCLUDED_DATABASES, SKIP_WEB_DOMAINS, MAIL_MERGES
    SSH_KEY = args.ssh_key
    SSH_USER = args.ssh_user
    if not SSH_USER or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', SSH_USER):
        ap.error('Invalid --ssh-user')
    if not Path(SSH_KEY).is_file():
        if args.apply or args.sync_mail_only:
            ap.error('SSH private key not found: '+SSH_KEY)
    SSH_TRANSPORT = 'ssh -i '+shlex.quote(SSH_KEY)+' -o IdentitiesOnly=yes -o BatchMode=yes'
    EXCLUDED_DOMAINS = set(args.exclude_domain)
    EXCLUDED_DATABASES = set(args.exclude_database)
    SKIP_WEB_DOMAINS = set(args.skip_web_domain)
    for domain in EXCLUDED_DOMAINS | SKIP_WEB_DOMAINS:
        if not HOSTNAME.fullmatch(domain) or '..' in domain:
            ap.error('Invalid excluded/skip domain: '+domain)
    for name in EXCLUDED_DATABASES:
        if not IDENT.fullmatch(name):
            ap.error('Invalid excluded database: '+name)
    MAIL_MERGES = {}
    for mapping in args.mail_merge:
        if mapping.count('=') != 1:
            ap.error('--mail-merge expects SOURCE=TARGET')
        source, target = mapping.split('=', 1)
        if not re.fullmatch(r'[^@\s]+@[^@\s]+', source) or not re.fullmatch(r'[^@\s]+@[^@\s]+', target):
            ap.error('Invalid --mail-merge address')
        MAIL_MERGES[source] = target
    if (args.apply or args.sync_mail_only) and not args.host:
        ap.error('--host is required for remote operations')
    if args.apply:
        missing = [flag for flag, value in (('--api-url', args.api_url), ('--api-user', args.api_user),
                   ('--client-id', args.client_id), ('--server-id', args.server_id), ('--ip', args.ip)) if value is None]
        if missing:
            ap.error('Required for --apply: '+', '.join(missing))
    if args.dry_run and not args.sync_mail_only:
        ap.error('--dry-run requires --sync-mail-only')
    if args.sync_mail_only and (args.include_sql or args.import_sql or args.include_dns or args.dns_apply
                               or args.create_dns_zones or args.activate_mail or args.api_only
                               or args.skip_mail or args.skip_web or args.dns_ns1 or args.dns_ns2 or args.replace_sql):
        ap.error('--sync-mail-only cannot be combined with provisioning, DNS, SQL, or skip flags')
    if args.replace_sql and not (args.apply and args.import_sql and args.include_sql):
        ap.error('--replace-sql requires --apply --include-sql --import-sql')
    if args.import_sql and not args.include_sql:
        ap.error('--import-sql requires --include-sql')
    if args.import_sql and args.api_only:
        ap.error('--import-sql cannot be used with --api-only')
    if args.activate_mail and args.skip_mail:
        ap.error('--activate-mail conflicts with --skip-mail')
    if args.dns_apply and not args.include_dns:
        ap.error('--dns-apply requires --include-dns')
    if args.create_dns_zones and not args.dns_apply:
        ap.error('--create-dns-zones requires --dns-apply')
    if args.create_dns_zones and not (args.dns_ns1 and args.dns_ns2):
        ap.error('--create-dns-zones requires --dns-ns1 and --dns-ns2')
    if os.geteuid() != 0:
        ap.error('Run as root on Debian 10')
    if not args.inventory and not (args.all or args.domain):
        ap.error('Specify --all or --domain')
    ROOT.mkdir(exist_ok=True)
    os.chmod(ROOT, 0o700)
    inv = discover()
    # Filter the operational inventory, not just the display plan, so --apply
    # cannot accidentally stage excluded resources.
    inv['domains'] = [x for x in inv['domains'] if x['name'] not in EXCLUDED_DOMAINS]
    for section in ('websites', 'mail', 'dns_records', 'aliases'):
        inv[section] = [x for x in inv[section] if x['domain'] not in EXCLUDED_DOMAINS]
    inv['databases'] = [x for x in inv['databases'] if x['domain'] not in EXCLUDED_DOMAINS
                        and x['name'] not in EXCLUDED_DATABASES]
    (ROOT/'inventory.json').write_text(json.dumps(inv, indent=2, ensure_ascii=False))
    os.chmod(ROOT/'inventory.json', 0o600)
    print('Excluded domains:', ', '.join(sorted(EXCLUDED_DOMAINS)) or '(none)')
    print('Excluded SQL databases:', ', '.join(sorted(EXCLUDED_DATABASES)) or '(none)')
    print('Active domains:', len(inv['domains']), 'Webs:', len(inv['websites']),
          'Mail entries:', len(inv['mail']), 'SQL databases present:',
          sum(d['exists'] for d in inv['databases']))
    if args.inventory:
        print('Inventory saved:', ROOT/'inventory.json')
        return
    if args.domain in EXCLUDED_DOMAINS:
        raise RuntimeError('Domain explicitly excluded from migration: '+args.domain)
    selected = {args.domain} if args.domain else {d['name'] for d in inv['domains']}
    if not selected.issubset({d['name'] for d in inv['domains']}):
        raise RuntimeError('Requested domain is not active in i-MSCP')
    if args.sync_mail_only:
        if not args.dry_run and not args.confirm_apply:
            raise RuntimeError('--sync-mail-only requires --confirm-apply (or --dry-run)')
        sudo(args.host, 'true')
        total = 0
        for mail in inv['mail']:
            if mail['domain'] not in selected or not mail['mailbox']:
                continue
            if mail['merge_into']:
                print('SKIP special merge mailbox:', mail['address'])
                continue
            source = maildir_path(mail['source'])
            if not source:
                print('SKIP missing source Maildir:', mail['address'])
                continue
            dest = mail_dest(args.host, mail['address'])
            sync_mail_incremental(args.host, source, dest, mail['address'], dry_run=args.dry_run)
            total += 1
        print(('Dry-run complete.' if args.dry_run else 'Incremental mail sync complete.'),
              'Mailboxes processed:', total, 'No web, SQL, DNS or API changes.')
        return
    plan = []
    if not args.skip_web:
        for web in sorted(inv['websites'], key=lambda w: (w['kind'] != 'main', w['hostname'])):
            if web['domain'] not in selected or web['skip']:
                continue
            if not Path(web['source']).is_dir():
                print('SKIP missing web directory:', web['source'])
                continue
            plan.append(('web', web, web['source']))
    if not args.skip_mail:
        for mail in inv['mail']:
            if mail['domain'] not in selected or not (mail['mailbox'] or mail['merge_into']):
                continue
            path = maildir_path(mail['source'])
            if not path:
                print('SKIP missing Maildir:', mail['address'])
                continue
            plan.append(('mail', mail, path))
    for kind, item, source in plan:
        print('PLAN', kind, item.get('hostname', item.get('address')), source)
    if args.include_sql:
        for db in inv['databases']:
            if db['domain'] in selected:
                print('PLAN SQL', db['name'], 'exists='+str(db['exists']))
    print('DNS records inventoried:', sum(x['domain'] in selected for x in inv['dns_records']))
    if args.include_dns:
        dns_records, dns_errors = dns_records_for(inv, selected)
        for domain, record in dns_records:
            print('PLAN DNS', domain, record['type'], record['name'], record['data'], 'priority='+str(record['aux']), 'ttl='+str(record['ttl']))
        for error in dns_errors:
            print('REVIEW DNS', error)
        if dns_errors and args.dns_apply:
            raise RuntimeError('DNS source records require review; refusing DNS changes')
    else:
        print('DNS provisioning disabled (use --include-dns to preview)')
    if args.plan:
        return
    if not args.confirm_apply:
        raise RuntimeError('--apply requires --confirm-apply; review the plan first')
    # Capture pre-existing subdomains so manually configured sites are never recopied.
    preexisting_subdomains = set()
    if not args.skip_web:
        for kind, item, _source in plan:
            if kind == 'web' and item['kind'] == 'subdomain':
                rows = db_rows_by(args.host, 'web_domain', 'domain', item['hostname'], ['domain_id'])
                if rows:
                    preexisting_subdomains.add(item['hostname'])
    secret = os.environ.get('ISPC_API_PASSWORD') or getpass.getpass('ISPConfig API password: ')
    api = ISPApi(args.api_url, args.api_user, secret)
    try:
        provision_resources(api, args, inv, selected)
        if args.include_dns and args.dns_apply:
            provision_dns(api, args.host, inv, selected, args)
    finally:
        api.close()
    if args.api_only:
        print('API provisioning finished. No content transferred. DNS writes:', 'requested (see CREATED/REVIEW lines)' if args.dns_apply else 'none')
        return
    # ISPConfig services may need time to create filesystem directories.
    time.sleep(5)
    for kind, item, source in plan:
        if kind == 'web':
            resolved = web_dest(args.host, item)
            if resolved is None:
                continue
            if item['kind'] == 'subdomain' and item['hostname'] in preexisting_subdomains:
                print('REVIEW WEB SUBDOMAIN: skipping automatic file transfer to preserve manually configured content:', item['hostname'])
                continue
            dest, user, group = resolved
            print('RSYNC STAGE WEB', item['hostname'], flush=True)
            staged = stage_dir(args.host, source, 'web/'+item['hostname'])
            print('RSYNC INSTALL WEB', item['hostname'], flush=True)
            if item['kind'] == 'subdomain':
                sudo(args.host, 'test -d '+shlex.quote(str(Path(dest).parent))+
                     ' && install -d -o '+shlex.quote(user)+' -g '+shlex.quote(group)+
                     ' -m 0755 '+shlex.quote(dest))
            install_web(args.host, staged, dest, user, group)
            print('INSTALLED WEB', item['hostname'])
        else:
            address = item['merge_into'] or item['address']
            dest = mail_dest(args.host, address)
            print('RSYNC STAGE MAIL', item['address'], flush=True)
            staged = stage_dir(args.host, source, 'mail/'+item['address'])
            if not item['merge_into']:
                print('RSYNC INSTALL MAIL', item['address'], flush=True)
            if item['merge_into']:
                print('STAGED SPECIAL MAIL for manual collision-safe merge:', item['address'], '->', address)
                continue
            install_mail(args.host, staged, dest)
            print('INSTALLED MAIL', item['address'], '->', address)
    if args.include_sql:
        for db in inv['databases']:
            if db['domain'] in selected and db['exists']:
                archive = dump_db(db['name'])
                print('RSYNC STAGE SQL', db['name'], flush=True)
                staged = stage_file(args.host, str(archive), 'sql/'+archive.name)
                if args.import_sql:
                    import_sql(args.host, db['name'], staged, archive, replace=args.replace_sql)
                else:
                    print('STAGED SQL (not imported):', db['name'])
    print('Finished. ISPConfig resources provisioned. DNS:', 'requested (see CREATED/REVIEW lines)' if args.dns_apply else 'unchanged', '; SQL:', 'import requested' if args.import_sql else 'dumps staged only')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)

