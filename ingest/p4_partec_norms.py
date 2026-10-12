"""Load a downloaded catalog transactionally, preserving effective-date versions."""
import argparse
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from psycopg.types.json import Jsonb
from common import ROOT, connect
from p4_designation import PARSER_VERSION, parse_page, clean_rows, iso_single_value_as_minimum

DEFAULT_SOURCE = ROOT / 'data/raw/partec_norms'

def load_page(conn, page, evidence, effective_date=None, initial_only=False):
    """Insert a new version only when the extracted source cells changed.

    Returns (rows, changed). A parser-only change (different flags or normalization for the same
    cells) keeps the existing version; its parser_version shows which parser produced the rows.
    With initial_only, effective_date applies to a norm's first version only (the build's backdating
    assumption); a later change takes effect on the day it was observed.
    """
    observed = datetime.fromisoformat(evidence['fetched_at'])
    rows = clean_rows(page['rows'], page['norm_code'])
    cells = conn.execute('SELECT spec.cells_digest(%s)', (Jsonb([r['raw_cells'] for r in rows]),)).fetchone()[0]
    parsed = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    norm = conn.execute('INSERT INTO spec.norm(norm_code,page_url) VALUES(%s,%s) ON CONFLICT(norm_code) DO UPDATE SET page_url=excluded.page_url RETURNING norm_id', (page['norm_code'],page['url'])).fetchone()[0]
    old = conn.execute('SELECT version_id,cells_sha256,valid_from FROM spec.norm_version WHERE norm_id=%s AND valid_to IS NULL FOR UPDATE', (norm,)).fetchone()
    if old and old[1] == cells:
        return rows, False
    if initial_only and conn.execute('SELECT EXISTS(SELECT 1 FROM spec.norm_version WHERE norm_id=%s)', (norm,)).fetchone()[0]:
        effective_date = None
    effective = effective_date or observed.date()
    if old:
        if effective <= old[2]:
            raise ValueError('Changed catalog requires a later effective date; same-day revisions need review')
        conn.execute('UPDATE spec.norm_version SET valid_to=%s WHERE version_id=%s', (effective,old[0]))
    version = conn.execute('''INSERT INTO spec.norm_version(norm_id,valid_from,observed_at,page_modified,raw_sha256,content_sha256,
      cells_sha256,parser_version,historical_assumption)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING version_id''',
      (norm,effective,observed,page['modified'],evidence['sha256'],parsed,cells,PARSER_VERSION,effective<observed.date())).fetchone()[0]
    for row in rows:
        conn.execute('''INSERT INTO spec.coating_type(version_id,row_no,type_code_raw,type_code,description_de,
          thickness_min_um,thickness_max_um,barrel_ok,rack_ok,coating_system,passivation,sealed,raw_cells,quality_flags)
          VALUES(%(version)s,%(row_no)s,%(type_code_raw)s,%(type_code)s,%(description_de)s,%(thickness_min_um)s,
          %(thickness_max_um)s,%(barrel_ok)s,%(rack_ok)s,%(coating_system)s,%(passivation)s,%(sealed)s,%(raw_cells)s,%(quality_flags)s)''',
          {**row,'version':version,'raw_cells':Jsonb(row['raw_cells'])})
    return rows, True

def link_lots(conn):
    return conn.execute('''WITH unique_match AS (SELECT lot_id,min(coating_type_id) AS id FROM spec.v_link_candidates GROUP BY lot_id HAVING count(*)=1),
      wanted AS (SELECT l.lot_id,u.id FROM trace.lot l LEFT JOIN unique_match u USING(lot_id))
      UPDATE trace.lot l SET coating_type_id=w.id FROM wanted w WHERE l.lot_id=w.lot_id AND l.coating_type_id IS DISTINCT FROM w.id''').rowcount

def norm_pages(manifest):
    """Norm page entries of a cache manifest; the index page and unlisted pages are not norms."""
    for url, evidence in manifest.items():
        if evidence.get('kind') == 'index' or evidence.get('file') == '_index.html':
            continue
        yield url, evidence

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--initial-effective-date',type=date.fromisoformat,help='Explicit demo backdating assumption, never an OEM issue date')
    ap.add_argument('--source',type=Path,default=DEFAULT_SOURCE,help='Directory with manifest.json and cached pages (default: data/raw/partec_norms)')
    args=ap.parse_args()
    raw=args.source.resolve()
    manifest_path=raw/'manifest.json'
    if not manifest_path.exists():
        raise SystemExit(f'No page cache manifest at {manifest_path}; run scripts/download_data.py first')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    parsed=[]; report=[]
    for url,evidence in sorted(norm_pages(manifest)):
        if evidence.get('listed') is False:
            report.append(dict(url=url,status='not_listed',reason='No longer linked from the index; kept in history, not loaded'))
            continue
        if evidence['status']!='ok':
            report.append(dict(url=url,status='download_failed',reason=evidence.get('reason')))
            continue
        path=raw/evidence['file']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=evidence['sha256']:
            raise ValueError('Source checksum mismatch: '+str(path))
        try:
            page=parse_page(path.read_text(encoding='utf-8'))
            if page['url'] and page['url'].rstrip('/')!=url.rstrip('/'):
                raise ValueError('Canonical URL does not match requested norm')
            page['url']=url
            parsed.append((page,evidence))
        except ValueError as exc:
            report.append(dict(url=url,status='parse_failed',reason=str(exc)))
    problems=[r for r in report if r['status'] in ('download_failed','parse_failed')]
    if problems:
        # Loading a partial catalog would silently drop norms; stop before writing anything.
        raise SystemExit('Not loading a partial catalog: '+'; '.join(f"{r['url']} {r['status']} ({r.get('reason')})" for r in problems))
    flags=Counter(); interpretations=Counter(); new_versions=0
    with connect('catalog_loader') as conn:
        conn.execute('SELECT pg_advisory_xact_lock(71004001)')
        for page,evidence in parsed:
            rows,changed=load_page(conn,page,evidence,args.initial_effective_date,initial_only=True)
            new_versions+=changed
            # Report what the catalog holds (rows of the current version), not what this parser would produce.
            stored=conn.execute('''SELECT ct.row_no,ct.type_code,ct.quality_flags,ct.thickness_min_um,ct.thickness_max_um,v.parser_version
              FROM spec.norm n JOIN spec.norm_version v USING(norm_id) JOIN spec.coating_type ct USING(version_id)
              WHERE n.norm_code=%s AND v.valid_to IS NULL ORDER BY ct.row_no''',(page['norm_code'],)).fetchall()
            flags.update(f for r in stored for f in r[2])
            interpretations.update('iso19598_single_value_as_minimum' for r in stored
                if iso_single_value_as_minimum(dict(thickness_min_um=r[3],thickness_max_um=r[4]),page['norm_code']))
            report.append(dict(url=page['url'],norm=page['norm_code'],status='loaded',rows=len(stored),new_version=changed,
                parser_version=stored[0][5] if stored else None,
                flagged_rows=[dict(row_no=r[0],type_code=r[1],flags=r[2]) for r in stored if r[2]]))
        changed_links=link_lots(conn)
        outdated=conn.execute('SELECT count(*) FROM spec.norm_version WHERE valid_to IS NULL AND parser_version IS DISTINCT FROM %s',(PARSER_VERSION,)).fetchone()[0]
        links=[dict(zip(('lot_no','spec_code','reason'),r)) for r in conn.execute('SELECT lot_no,spec_code,reason FROM spec.v_lot_link_status ORDER BY lot_id')]
    out=ROOT/'docs/results'
    out.mkdir(parents=True,exist_ok=True)
    (out/'catalog_quality.json').write_bytes(json.dumps(dict(pages=report,flag_counts=flags,interpretation_counts=interpretations),indent=2,ensure_ascii=False).encode('utf-8'))
    (out/'lot_links.json').write_bytes(json.dumps(links,indent=2).encode('utf-8'))
    if outdated:
        print(f'Note: {outdated} current norm versions were parsed by another parser version than {PARSER_VERSION}; '
              'their rows are unchanged until the source page changes.')
    print(json.dumps(dict(pages=len(parsed),rows=sum(p['rows'] for p in report if p['status']=='loaded'),new_versions=new_versions,
                          changed_links=changed_links,flags=flags,interpretations=interpretations)))

if __name__=='__main__':main()
