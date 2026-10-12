"""Build from the cached public norm pages and a deterministic, local simulated foundation.

The norm pages are read from the local cache. The network is used only when the cache is
incomplete (and --offline is not given) or when --refresh-norms is given.
"""
import subprocess
import argparse
import sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def run(*args):
    subprocess.run([sys.executable,*args],cwd=ROOT,check=True)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--skip-migrate',action='store_true',help='Use only after Flyway has migrated this database')
    mode=ap.add_mutually_exclusive_group()
    mode.add_argument('--offline',action='store_true',help='Never use the network; fail if the norm cache is incomplete')
    mode.add_argument('--refresh-norms',action='store_true',help='Re-download all norm pages before loading')
    ap.add_argument('--norm-source',type=Path,help='Load norm pages from this directory (manifest.json + HTML) instead of data/raw/partec_norms; implies --offline')
    args=ap.parse_args()
    if args.norm_source and args.refresh_norms:
        ap.error('--norm-source cannot be combined with --refresh-norms')
    policy=yaml.safe_load((ROOT/'config/release_policy.yaml').read_text(encoding='utf-8'))
    if not args.skip_migrate:run('scripts/migrate.py')
    with connect('build') as conn:
        seeded=conn.execute('SELECT EXISTS(SELECT 1 FROM trace.lot)').fetchone()[0]
    if not seeded:
        run('ingest/p1_simulate_plant.py')
        # The simulator writes six months of bath analyses in one bulk insert, so the database stamps
        # them all with today's entry time. Record them as entered when sampled (demo assumption, the
        # same one V015 applies to analyses that existed before entered_at); only analyses entered
        # later through the application can then be reported as back-filled ("nachgetragen").
        with connect('build') as conn:
            conn.execute("UPDATE lab.analysis SET entered_at=sampled_at WHERE source='sim:p1' AND entered_at>sampled_at")
    loader=['ingest/p4_partec_norms.py','--initial-effective-date',str(policy['initial_catalog_effective_date'])]
    if args.norm_source:
        loader+=['--source',str(args.norm_source.resolve())]
    elif args.refresh_norms:
        run('scripts/download_data.py','--refresh-norms')
    elif subprocess.run([sys.executable,'scripts/download_data.py','--offline'],cwd=ROOT).returncode!=0:
        if args.offline:
            raise SystemExit('The norm page cache is incomplete. Run once with network access (without --offline).')
        run('scripts/download_data.py')
    if not args.norm_source and subprocess.run([sys.executable,'scripts/download_data.py','--offline'],cwd=ROOT).returncode!=0:
        # Every page listed on the index must be cached and valid; a partial catalog is not built.
        raise SystemExit('Some listed norm pages are missing after the download; see the messages above.')
    run(*loader)
    run('scripts/load_policy.py')
    run('scripts/load_references.py')
    # Fresh tables have no planner statistics yet; the per-lot release checks in the export need them.
    with connect('build') as conn:
        conn.execute('ANALYZE')
    run('scripts/export_results.py')
    run('ingest/p4_release_report.py')

if __name__=='__main__':main()
