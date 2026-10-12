"""Create an isolated portable PostgreSQL cluster, given an existing binary directory."""
import argparse
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--postgres-dir', type=Path, required=True, help='Distribution directory containing bin/initdb.exe')
    args = ap.parse_args()
    target = ROOT / '.local/postgres'
    if not target.exists():
        shutil.copytree(args.postgres_dir, target)
    envfile = ROOT / '.env'
    if not envfile.exists():
        envfile.write_text('PGHOST=127.0.0.1\nPGPORT=55436\nPGUSER=spec_admin\nPGPASSWORD=' + secrets.token_urlsafe(32) + '\nPGDATABASE=coating_spec\n', encoding='utf-8')
    env = os.environ.copy()
    env.update(dict(line.split('=', 1) for line in envfile.read_text().splitlines() if '=' in line))
    pgdata = ROOT / '.local/pgdata'
    if not (pgdata / 'PG_VERSION').exists():
        password = ROOT / '.local/init-password.txt'
        password.write_text(env['PGPASSWORD'], encoding='utf-8')
        try:
            subprocess.run([str(target/'bin/initdb.exe'), '-D', str(pgdata), '-U', 'spec_admin', '--pwfile='+str(password), '--auth=scram-sha-256', '--encoding=UTF8', '--locale=C'], check=True)
        finally:
            password.unlink(missing_ok=True)
    ctl = str(target / 'bin/pg_ctl.exe')
    if subprocess.run([ctl, '-D', str(pgdata), 'status'], capture_output=True).returncode:
        subprocess.run([ctl, '-D', str(pgdata), '-l', str(ROOT/'.local/postgres.log'), '-o', '-h 127.0.0.1 -p 55436', '-w', 'start'], check=True)
    with psycopg.connect(host=env['PGHOST'], port=env['PGPORT'], user=env['PGUSER'], password=env['PGPASSWORD'], dbname='postgres', autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='coating_spec'").fetchone():
            conn.execute('CREATE DATABASE coating_spec')
    print('Project 4 database ready at 127.0.0.1:55436/coating_spec')

if __name__ == '__main__':
    main()
