"""Cited supplier context only: it must never participate in a release verdict."""
import sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

MATCH='''SELECT coating_type_id FROM spec.coating_type
  WHERE coating_system='ZnNi' AND sealed=%s AND strpos(lower(description_de),%s)>0'''

def main():
    policy=yaml.safe_load((ROOT/'config/release_policy.yaml').read_text(encoding='utf-8'))
    # These six values were checked against Table 1 using a web PDF reader.
    # A local PDF copy is optional; never substitute fabricated values if fetching fails.
    source=policy['source']+'#page=2'
    attached=set(); rows=0
    with connect('supplier_reference_loader') as conn:
        wanted=set()
        for i,ref in enumerate(policy['corrosion_references'],1):
            ids=[r[0] for r in conn.execute(MATCH,(ref['sealed'],ref['match_word'].lower()))]
            for ct in ids:
                attached.add(ct)
                for corrosion in ('white','red'):
                    characteristic=f'supplier_{i}_{corrosion}'
                    wanted.add((ct,characteristic))
                    context=ref['finish']+'; '+corrosion+' corrosion; strictly > reported bound. '+policy['reference_context']
                    # A corrected transcription replaces the earlier one (audited); unchanged rows are left alone.
                    rows+=conn.execute('''INSERT INTO spec.requirement(coating_type_id,characteristic,lsl,lower_operator,unit,kind,source,context)
                      VALUES(%s,%s,%s,'>','h','reference',%s,%s)
                      ON CONFLICT(coating_type_id,characteristic,kind) DO UPDATE
                        SET lsl=excluded.lsl,lower_operator=excluded.lower_operator,unit=excluded.unit,source=excluded.source,context=excluded.context
                        WHERE (spec.requirement.lsl,spec.requirement.lower_operator,spec.requirement.unit,spec.requirement.source,spec.requirement.context)
                          IS DISTINCT FROM (excluded.lsl,excluded.lower_operator,excluded.unit,excluded.source,excluded.context)''',
                      (ct,characteristic,ref[f'{corrosion}_gt_h'],source,context)).rowcount
        # Supplier context attached by an earlier loader to finishes it does not describe is removed (audited).
        stale=[r for r in conn.execute("SELECT requirement_id,coating_type_id,characteristic FROM spec.requirement WHERE kind='reference' AND source=%s",(source,))
               if (r[1],r[2]) not in wanted]
        for r in stale:
            conn.execute('DELETE FROM spec.requirement WHERE requirement_id=%s',(r[0],))
    print(f'Supplier context: {len(wanted)} reference rows on {len(attached)} ZnNi catalog rows with a matching finish '
          f'({rows} written, {len(stale)} non-matching removed); no release criteria added')

if __name__=='__main__':main()
