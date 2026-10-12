"""Read-only, single-page German release assessment with an exact JSON snapshot.

The footer prints the first 16 hex digits of the SHA-256 of the JSON file exactly as written
(UTF-8, LF line endings), so `sha256sum` of the JSON starts with the printed value on any OS.
"""
import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import yaml
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle
from reportlab.platypus.doctemplate import LayoutError
from pypdf import PdfReader
from common import ROOT,connect

# Bitstream Vera ships inside ReportLab (permissive licence); embedding it makes µ, ≥ and ≤
# independent of the viewer's fonts.
pdfmetrics.registerFont(TTFont('Vera','Vera.ttf'))
pdfmetrics.registerFont(TTFont('Vera-Bold','VeraBd.ttf'))
rl_config.canvas_basefontname='Vera'
INK='#193047'; MUTED='#596A79'; LINE='#DCE4EA'
VERDICTS={'PASS':('#23735A','BESTANDEN'),'WARN':('#9C6200','PRÜFUNG ERFORDERLICH'),
          'INCOMPLETE':('#596A79','UNVOLLSTÄNDIG'),'FAIL':('#B12F3B','NICHT BESTANDEN')}
STYLE=ParagraphStyle('body',fontName='Vera',fontSize=7.6,leading=9.6,textColor=colors.HexColor(INK))
SMALL=ParagraphStyle('small',parent=STYLE,fontSize=6.7,leading=8.4)
AUTHOR='Portfolio-Demonstration K. Amer'

def p(value,small=False):
    return Paragraph(escape(str(value) if value is not None else '-'),SMALL if small else STYLE)

def table(rows,widths,header=True):
    result=Table([[p(c,True) for c in row] for row in rows],colWidths=widths,hAlign='LEFT')
    commands=[('FONT',(0,0),(-1,-1),'Vera'),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),
              ('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),3.5),('BOTTOMPADDING',(0,0),(-1,-1),3.5),
              ('LINEBELOW',(0,0),(-1,-1),.35,colors.HexColor(LINE))]
    if header:commands.append(('BACKGROUND',(0,0),(-1,0),colors.HexColor('#EEF3F7')))
    result.setStyle(TableStyle(commands))
    return result

def de_date(value):
    return value.strftime('%d.%m.%Y') if value else '-'

def de_int(value):
    return f'{value:,}'.replace(',','.')

def de_num(value):
    return format(value.normalize() if hasattr(value,'normalize') else value,'f').replace('.',',')

def rows(conn,sql,params=()):
    cur=conn.execute(sql,params)
    return [dict(zip([c.name for c in cur.description],r)) for r in cur]

def matching_references(policy,lot,refs):
    """Keep supplier results only for the lot's own finish (colour word and sealing)."""
    keep=set()
    description=(lot['description_de'] or '').lower()
    for i,ref in enumerate(policy['corrosion_references'],1):
        if lot['coating_system']=='ZnNi' and lot['sealed'] is ref['sealed'] and ref['match_word'].lower() in description:
            keep.add(i)
    result=[]
    for r in refs:
        m=re.fullmatch(r'supplier_(\d+)_(white|red)',r['characteristic'])
        if m and int(m.group(1)) in keep:
            ref=policy['corrosion_references'][int(m.group(1))-1]
            result.append({**r,'finish':ref['finish'],'finish_de':ref['finish_de'],'corrosion':m.group(2)})
    return result

def snapshot(lot_no=None,conn=None):
    """Collect everything the report shows from one read-only repeatable-read snapshot.

    Tests may pass their own (rolled-back) connection; then its current transaction is used."""
    if conn is not None:
        return collect(conn,lot_no)
    with connect('inspection_report') as conn:
        # common.connect has already opened a transaction; restart it with a stable read snapshot.
        conn.commit()
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return collect(conn,lot_no)

def collect(conn,lot_no):
    policy=yaml.safe_load((ROOT/'config/release_policy.yaml').read_text(encoding='utf-8'))
    if lot_no is None:
        lot_no=conn.execute('SELECT lot_no FROM trace.complaint JOIN trace.lot USING(lot_id) ORDER BY complaint_id LIMIT 1').fetchone()[0]
    found=rows(conn,'''SELECT l.lot_id,l.lot_no,l.qty_pcs,l.status,spec.status_label(l.status) AS status_de,l.origin,
      parent.lot_no AS parent_lot_no,l.spec_code,c.code AS customer,p.part_no,o.order_no,o.received_on,
      ct.coating_type_id,ct.row_no,ct.type_code_raw,ct.type_code,ct.description_de,ct.thickness_min_um,ct.thickness_max_um,
      ct.coating_system,ct.passivation,ct.sealed,ct.barrel_ok,ct.rack_ok,ct.quality_flags,ct.raw_cells,
      n.norm_code,n.page_url,v.version_id,v.valid_from,v.valid_to,v.observed_at,v.page_modified,v.historical_assumption,
      v.raw_sha256,v.cells_sha256,v.parser_version
      FROM trace.lot l JOIN trace.customer_order o USING(order_id) JOIN ref.customer c ON c.customer_id=o.customer_id
      JOIN ref.part p ON p.part_id=l.part_id LEFT JOIN trace.lot parent ON parent.lot_id=l.parent_lot_id
      LEFT JOIN spec.coating_type ct ON ct.coating_type_id=l.coating_type_id
      LEFT JOIN spec.norm_version v USING(version_id) LEFT JOIN spec.norm n USING(norm_id) WHERE l.lot_no=%s''',(lot_no,))
    if not found:raise ValueError('Unknown lot: '+lot_no)
    lot=found[0]
    pol=rows(conn,'SELECT min_distinct_thickness_samples,max_analysis_age::text AS max_analysis_age,max_thickness_factor,source FROM spec.release_policy')[0]
    checks=rows(conn,'SELECT * FROM spec.release_check(%s)',(lot['lot_id'],))
    verdicts=rows(conn,'SELECT * FROM spec.release_verdicts(%s)',(lot['lot_id'],))[0]
    measurements=rows(conn,'SELECT sample_no AS sample,characteristic,value,method,measured_at FROM trace.measurement WHERE lot_id=%s ORDER BY characteristic,sample_no,measurement_id',(lot['lot_id'],))
    coverage=rows(conn,'''SELECT step_id,relation,lot_no,carrier_no,tank_code,process_code,in_station,analysis_id,sampled_at,
      entered_at,valid_during,counted_during,parameters_complete FROM spec.plating_coverage(%s,(SELECT max_analysis_age FROM spec.release_policy))
      ORDER BY lower(in_station),step_id,sampled_at''',(lot['lot_id'],))
    ids=sorted({c['analysis_id'] for c in coverage if c['analysis_id'] is not None})
    results=rows(conn,'''SELECT r.analysis_id,r.sampled_at,r.valid_during,r.parameter_code,r.value,p.unit,r.lsl,r.target,r.usl,r.status
      FROM lab.v_result_status r JOIN lab.parameter p USING(parameter_code) WHERE r.analysis_id=ANY(%s)
      ORDER BY r.analysis_id,r.parameter_code''',(ids,))
    refs=rows(conn,"SELECT characteristic,lower_operator,lsl AS bound,unit,context,source FROM spec.requirement WHERE coating_type_id=%s AND kind='reference' ORDER BY characteristic",(lot['coating_type_id'],))
    observed_on=lot['observed_at'].astimezone(timezone.utc).date() if lot['observed_at'] else None
    backdated=bool(lot['historical_assumption'] and observed_on and lot['received_on']<observed_on)
    catalog={k:lot[k] for k in ('norm_code','page_url','version_id','valid_from','valid_to','observed_at','page_modified',
             'historical_assumption','raw_sha256','cells_sha256','parser_version','row_no','type_code_raw','type_code',
             'quality_flags','raw_cells')}
    catalog['assumed_for_this_order']=backdated
    analyses=[{'analysis_id':i,'results':[r for r in results if r['analysis_id']==i]} for i in ids]
    return dict(generated_at=datetime.now(timezone.utc).isoformat(),lot=lot,checks=checks,verdict=verdicts['verdict'],
                technical_verdict=verdicts['technical_verdict'],release_policy=pol,catalog=catalog,
                bath_evidence=dict(coverage=coverage,analyses=analyses),measurements=measurements,
                references=matching_references(policy,lot,refs),references_attached=len(refs))

def render(data,output):
    lot=data['lot']; verdict=data['verdict']; color,label=VERDICTS[verdict]
    payload=json.dumps(data,indent=2,ensure_ascii=False,default=str).encode('utf-8')
    digest=hashlib.sha256(payload).hexdigest()
    title=ParagraphStyle('title',parent=STYLE,fontName='Vera-Bold',fontSize=22,leading=26)
    section=ParagraphStyle('section',parent=STYLE,fontName='Vera-Bold',fontSize=9,leading=12,spaceBefore=9,spaceAfter=4)
    technical=data['technical_verdict']
    story=[Paragraph('PRÜFBERICHT',title),Spacer(1,4),p('Beschichtung / Losbezogene Freigabeprüfung'),Spacer(1,9),
        Paragraph(f'<font color="{color}"><b>{verdict} · {label}</b></font>',ParagraphStyle('verdict',parent=STYLE,fontName='Vera-Bold',fontSize=13,leading=17)),
        p(f'Technische Bewertung ohne Quellenstand: {technical} · {VERDICTS[technical][1]}',True),
        Spacer(1,4),p('Portfolio-Demonstration: Produktions- und Messdaten sind simuliert. Dies ist keine Kundenfreigabe.'),Spacer(1,9)]
    status=lot['status_de']+(f" (Nacharbeit von {lot['parent_lot_no']})" if lot['parent_lot_no'] else '')
    story.append(table([['Los',lot['lot_no'],'Kunde',lot['customer']],['Auftrag',lot['order_no'],'Artikel',lot['part_no']],
        ['Menge',f"{de_int(lot['qty_pcs'])} Stück",'Losstatus',status],['Auftragsdatum',de_date(lot['received_on']),'Katalogversion',lot['version_id']]], [72,190,70,179],False))
    story += [Spacer(1,6),p(lot['spec_code']),p(lot['description_de'],True)]
    if lot['version_id'] is not None:
        cells=lot['raw_cells'] or {}
        modified=de_date(lot['page_modified'].astimezone(timezone.utc).date()) if lot['page_modified'] else 'unbekannt'
        story.append(p(f"Quelle: {lot['norm_code']}, Tabellenzeile {lot['row_no']} (Typ „{cells.get('typ','')}“, Schichtdicke „{cells.get('schichtdicke','')}“), "
                       f"Webstand beobachtet {de_date(lot['observed_at'].astimezone(timezone.utc).date())}, Seite geändert {modified}.",True))
    story.append(Paragraph('01 / Freigabeprüfungen',section))
    story.append(table([['Prüfung','Soll','Ist','Bewertung']]+[[r['check_name'],r['required'],r['observed'],r['verdict']] for r in data['checks']],[118,150,183,60]))
    story.append(Paragraph('02 / Messwerte des Loses',section))
    # Aggregate every measurement by characteristic; do not truncate long datasets to force one page.
    groups={}
    for m in data['measurements']:groups.setdefault(m['characteristic'],[]).append(m)
    table_rows=[['Merkmal / Einheit','n Werte','Minimum – Maximum','Verfahren']]
    units={'thickness_um':'Schichtdicke / µm','ni_pct':'Nickel / %','nss_h_white':'Weißkorrosion / h','nss_h_red':'Rotkorrosion / h'}
    for key,items in groups.items():
        values=[m['value'] for m in items if m['value'] is not None]
        table_rows.append([units.get(key,key),len(values),f'{min(values):.2f} – {max(values):.2f}'.replace('.',',') if values else 'Nicht bestimmt',', '.join(sorted({m['method'] for m in items}))])
    if not groups:table_rows.append(['Keine Messdaten','0','-','-'])
    story.append(table(table_rows,[150,55,145,161]))
    analyses=sorted({(c['analysis_id'],c['sampled_at']) for c in data['bath_evidence']['coverage'] if c['analysis_id'] is not None})
    if analyses:
        story.append(Spacer(1,3))
        story.append(p('Badanalysen zu den Beschichtungsschritten: '+', '.join(f"Analyse {i} ({t.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC)" for i,t in analyses)
                       +'. Werte, Grenzen und Deckungsintervalle stehen im JSON-Snapshot.',True))
    story.append(Paragraph('03 / Lieferantenreferenz – kein Freigabekriterium',section))
    if data['references']:
        # These bounds come from the same database snapshot as the release results.
        pairs={}
        for r in data['references']:
            pairs.setdefault(r['finish_de'],{})[r['corrosion']]=f"{'>' if r['lower_operator']=='>' else '≥'} {de_num(r['bound'])} h"
        story.append(table([['ZnNi-Oberfläche (Studie)','Weißkorrosion','Rotkorrosion']]+[[k,v.get('white','-'),v.get('red','-')] for k,v in pairs.items()],[301,105,105]))
        story.append(p('ASTM B117, Smie / NASF SUR/FIN 2008, Tabelle 1 (PDF S. 2). Ergebnisse der Studie, keine Prognose für dieses Los. https://nmfrc.org/pdf/sf2008/sf0828.pdf',True))
    else:
        story.append(p('Keine passende Referenz: Die Studie nennt nur blau passivierte sowie schwarz passivierte und versiegelte ZnNi-Oberflächen.',True))
    notes=[Spacer(1,8),p('Bewertung: FAIL = Abweichung; INCOMPLETE = Nachweis fehlt; WARN = manuelle Prüfung; PASS = geprüfte Kriterien erfüllt.',True)]
    if data['catalog']['assumed_for_this_order']:
        observed=de_date(lot['observed_at'].astimezone(timezone.utc).date())
        notes.append(p(f"Der Webstand vom {observed} wurde für den Auftrag vom {de_date(lot['received_on'])} rückwirkend angenommen (Demoannahme).",True))
    notes+=[p('Vollständige OEM-Norm und freigegebener Prüfplan sind nicht Bestandteil dieser Prüfung.',True),
            Spacer(1,14),p('Geprüft durch: _______________________     Datum / Unterschrift: _______________________')]
    story+=notes
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=output.with_name(output.stem+'.tmp.pdf')
    json_path=output.with_suffix('.json')
    def footer(canvas,doc):
        canvas.setStrokeColor(colors.HexColor(LINE));canvas.line(42,55,553,55)
        canvas.setFont('Vera',6.3);canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(42,43,f"Erstellt (UTC): {data['generated_at'][:19]}  |  Snapshot: {digest[:16]} = SHA-256 von {json_path.name}")
        canvas.drawRightString(553,43,f'Seite {doc.page}')
        canvas.drawString(42,33,'Normquelle: '+str(lot['page_url'] or 'Keine Zuordnung'))
    try:
        SimpleDocTemplate(str(temp),pagesize=A4,leftMargin=42,rightMargin=42,topMargin=32,bottomMargin=64,
            title='Prüfbericht '+lot['lot_no'],author=AUTHOR,subject='Simulierte Daten; keine Kundenfreigabe',creator=AUTHOR
            ).build(story,onFirstPage=footer,onLaterPages=footer)
        pages=len(PdfReader(temp).pages)
    except LayoutError:
        pages=None  # a single cell taller than the page
    if pages!=1:
        temp.unlink(missing_ok=True)
        # Do not leave an older pair next to a failed run looking current.
        superseded=output.parent/'superseded'
        for old in (output,json_path):
            if old.exists():
                superseded.mkdir(exist_ok=True)
                shutil.move(str(old),str(superseded/old.name))
        raise RuntimeError('Report exceeds one page; revise layout before publishing (no report written for '+lot['lot_no']+')')
    temp_json=json_path.with_name(json_path.stem+'.tmp.json')
    temp_json.write_bytes(payload)
    temp_json.replace(json_path)
    temp.replace(output)
    print(output)
    return digest

def main():
    ap=argparse.ArgumentParser();ap.add_argument('lot_no',nargs='?')
    ap.add_argument('--output-dir',type=Path,default=ROOT/'docs/reports');args=ap.parse_args()
    data=snapshot(args.lot_no)
    safe=re.sub(r'[^a-zA-Z0-9_-]','_',data['lot']['lot_no'])
    render(data,args.output_dir/f'Pruefbericht_{safe}.pdf')

if __name__=='__main__':main()
