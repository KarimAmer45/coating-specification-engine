from datetime import date
import pytest
import psycopg
from p4_partec_norms import load_page,link_lots

def verdict(db,lot):return db.execute('SELECT spec.release_verdict(%s)',(lot,)).fetchone()[0]

def test_complaint_bath_warning(db,lot):
    rows={r[0]:r[3] for r in db.execute('SELECT * FROM spec.release_check(%s)',(lot,))}
    assert rows['Bad im Prozessfenster']=='WARN'
    assert rows['Schichtdicke: Minimum']=='PASS'
    assert rows['Quellenstand']=='WARN'
    assert verdict(db,lot)=='WARN'

def test_reference_never_fails_lot(db,lot):
    before=verdict(db,lot)
    db.execute('''INSERT INTO spec.requirement(coating_type_id,characteristic,lsl,lower_operator,unit,kind,source,context)
      SELECT coating_type_id,'supplier_test_white',1,'>','h','reference','test','test' FROM trace.lot WHERE lot_id=%s''',(lot,))
    db.execute("UPDATE spec.requirement SET lsl=999999 WHERE kind='reference'")
    assert verdict(db,lot)==before

def test_missing_measurements_incomplete(db,lot):
    db.execute("DELETE FROM trace.measurement WHERE lot_id=%s AND characteristic='thickness_um'",(lot,))
    assert verdict(db,lot)=='INCOMPLETE'

def test_out_of_spec_overrides_warning(db,lot):
    db.execute("UPDATE trace.measurement SET value=0 WHERE lot_id=%s AND characteristic='thickness_um' AND sample_no=1",(lot,))
    assert verdict(db,lot)=='FAIL'

def test_missing_process_evidence_incomplete(db,lot):
    db.execute('DELETE FROM trace.load_lot WHERE lot_id=%s',(lot,))
    assert verdict(db,lot)=='INCOMPLETE'

def test_invalid_sample_policy(db,lot):
    with pytest.raises(psycopg.Error):db.execute('SELECT * FROM spec.release_check(%s,0)',(lot,))

def test_history_and_order_date_selection(db,lot):
    page=dict(norm_code='TEST VERSIONED',url='https://example.org/test',modified=None,rows=[dict(typ='ABC',beschichtung='Zink transparent ohne Versiegelung',schichtdicke='5 - 10',trommel='Ja',gestell='Ja')])
    evidence=dict(fetched_at='2026-01-01T00:00:00+00:00',sha256='a'*64)
    load_page(db,page,evidence,date(2026,1,1))
    db.execute("UPDATE trace.lot SET spec_code='TEST VERSIONED ABC' WHERE lot_id=%s",(lot,))
    link_lots(db)
    first=db.execute('SELECT coating_type_id FROM trace.lot WHERE lot_id=%s',(lot,)).fetchone()[0]
    page['rows'][0]['schichtdicke']='20 - 30'
    evidence['fetched_at']='2026-09-01T00:00:00+00:00'
    load_page(db,page,evidence,date(2026,9,1))
    link_lots(db)
    assert db.execute('SELECT coating_type_id FROM trace.lot WHERE lot_id=%s',(lot,)).fetchone()[0]==first
    assert db.execute('SELECT thickness_min_um FROM spec.coating_type WHERE coating_type_id=%s',(first,)).fetchone()[0]==5
    db.execute("UPDATE trace.customer_order SET received_on='2026-09-01' WHERE order_id=(SELECT order_id FROM trace.lot WHERE lot_id=%s)",(lot,))
    link_lots(db)
    assert db.execute('SELECT coating_type_id FROM trace.lot WHERE lot_id=%s',(lot,)).fetchone()[0]!=first

def test_ambiguous_type_not_linked(db,lot):
    db.execute("UPDATE trace.lot SET spec_code='VW 13750 TL244 Ofl-r645' WHERE lot_id=%s",(lot,))
    link_lots(db)
    assert db.execute('SELECT coating_type_id FROM trace.lot WHERE lot_id=%s',(lot,)).fetchone()[0] is None
    # Missing link evidence is INCOMPLETE with a reason (it was FAIL before the review).
    rows={r[0]:(r[2],r[3]) for r in db.execute('SELECT * FROM spec.release_check(%s)',(lot,))}
    assert rows['Beschichtungszuordnung']==('Mehrdeutig: 2 Katalogzeilen','INCOMPLETE')
    assert verdict(db,lot)=='INCOMPLETE'

def test_catalog_rows_immutable(db):
    with pytest.raises(psycopg.Error):db.execute('UPDATE spec.coating_type SET thickness_min_um=0 WHERE coating_type_id=(SELECT min(coating_type_id) FROM spec.coating_type)')

def test_lab_cannot_release(db,lot):
    db.execute('SET LOCAL ROLE role_lab')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute("UPDATE trace.lot SET status='released' WHERE lot_id=%s",(lot,))
