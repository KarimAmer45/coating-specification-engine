"""Copy the release policy from config/release_policy.yaml into the database (audited)."""
import sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def main():
    policy=yaml.safe_load((ROOT/'config/release_policy.yaml').read_text(encoding='utf-8'))
    wanted={(system,process) for system,processes in policy['coating_system_processes'].items() for process in processes}
    with connect('policy_loader') as conn:
        conn.execute('''INSERT INTO spec.release_policy(policy_id,min_distinct_thickness_samples,max_analysis_age,max_thickness_factor,source)
          VALUES(true,%s,make_interval(days=>%s),%s,'config/release_policy.yaml')
          ON CONFLICT(policy_id) DO UPDATE SET min_distinct_thickness_samples=excluded.min_distinct_thickness_samples,
            max_analysis_age=excluded.max_analysis_age,max_thickness_factor=excluded.max_thickness_factor,source=excluded.source
          WHERE (spec.release_policy.min_distinct_thickness_samples,spec.release_policy.max_analysis_age,
                 spec.release_policy.max_thickness_factor,spec.release_policy.source)
            IS DISTINCT FROM (excluded.min_distinct_thickness_samples,excluded.max_analysis_age,
                 excluded.max_thickness_factor,excluded.source)''',
          (policy['minimum_distinct_thickness_samples'],policy['max_analysis_age_days'],policy['max_thickness_factor']))
        stored={tuple(r) for r in conn.execute('SELECT coating_system,process_code FROM spec.coating_system_process')}
        for pair in stored-wanted:
            conn.execute('DELETE FROM spec.coating_system_process WHERE coating_system=%s AND process_code=%s',pair)
        for pair in sorted(wanted-stored):
            conn.execute('INSERT INTO spec.coating_system_process VALUES(%s,%s)',pair)
    print(f"Release policy: {policy['minimum_distinct_thickness_samples']} samples, analyses up to {policy['max_analysis_age_days']} days old, "
          f"plausibility {policy['max_thickness_factor']} x minimum, {len(wanted)} coating-system/process pairs")

if __name__=='__main__':main()
