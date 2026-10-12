"""Small reviewable build outputs (counts and the complaint lot), written with LF line endings.

norm_matrix.csv is a row-level extract of the published tables: a local output (git-ignored).
"""
import csv
import io
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def write_csv(path,cur):
    buf=io.StringIO()
    writer=csv.writer(buf,lineterminator='\n');writer.writerow(c.name for c in cur.description);writer.writerows(cur)
    path.write_bytes(buf.getvalue().encode('utf-8'))

def main():
    out=ROOT/'docs/results'
    out.mkdir(parents=True,exist_ok=True)
    with connect('export') as conn:
        summary={}
        for name,sql in {
            'catalog': 'SELECT count(*) norms,(SELECT count(*) FROM spec.norm_version) versions,(SELECT count(*) FROM spec.coating_type) coating_rows FROM spec.norm',
            'link_status': 'SELECT reason,count(*) lots FROM spec.v_lot_link_status GROUP BY reason ORDER BY reason',
            'production': 'SELECT count(*) lots,(SELECT count(*) FROM trace.measurement) measurements FROM trace.lot',
            'references': 'SELECT kind,count(*) rows,count(DISTINCT coating_type_id) catalog_rows FROM spec.requirement GROUP BY kind ORDER BY kind',
            'release_policy': "SELECT min_distinct_thickness_samples,max_analysis_age::text max_analysis_age,max_thickness_factor FROM spec.release_policy",
        }.items():
            cur=conn.execute(sql)
            summary[name]=[dict(zip([c.name for c in cur.description],r)) for r in cur]
        lot=conn.execute('SELECT l.lot_id,l.lot_no FROM trace.complaint c JOIN trace.lot l USING(lot_id) ORDER BY complaint_id LIMIT 1').fetchone()
        verdict,technical=conn.execute('SELECT verdict,technical_verdict FROM spec.release_verdicts(%s)',(lot[0],)).fetchone()
        summary['complaint_lot']={'lot_id':lot[0],'lot_no':lot[1],'verdict':verdict,'technical_verdict':technical}
        # Every simulated lot: overall verdict and the technical verdict without the source-date check.
        conn.execute('''CREATE TEMP TABLE lot_verdict ON COMMIT DROP AS
          SELECT l.lot_id,l.origin,l.status,v.verdict,v.technical_verdict FROM trace.lot l CROSS JOIN LATERAL spec.release_verdicts(l.lot_id) v''')
        cur=conn.execute('SELECT verdict,technical_verdict,count(*) lots FROM lot_verdict GROUP BY 1,2 ORDER BY 1,2')
        summary['verdicts']=[dict(zip([c.name for c in cur.description],r)) for r in cur]
        for name,sql,params in [
            ('complaint_checks','SELECT * FROM spec.release_check(%s)',(lot[0],)),
            ('verdict_distribution','SELECT origin,status,verdict,technical_verdict,count(*) lots FROM lot_verdict GROUP BY 1,2,3,4 ORDER BY 1,2,3,4',()),
            ('norm_matrix','SELECT norm_code,row_no,type_code,thickness_min_um,thickness_max_um,barrel_ok,rack_ok,coating_system,passivation,sealed,quality_flags FROM spec.v_norm_matrix ORDER BY norm_code,row_no',()),
            ('unlinked_lots',"SELECT lot_no,spec_code,reason FROM spec.v_lot_link_status WHERE reason<>'linked' ORDER BY lot_id",()),
        ]:
            write_csv(out/(name+'.csv'),conn.execute(sql,params))
    (out/'build_summary.json').write_bytes(json.dumps(summary,indent=2,default=str).encode('utf-8'))
    print(json.dumps(summary,default=str))

if __name__=='__main__':main()
