"""Release check, catalog history, role and report tests added with the hardened release logic."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path
import pytest
import psycopg
import yaml
from pypdf import PdfReader
from p4_partec_norms import load_page, link_lots
from p4_release_report import snapshot, render

ROOT = Path(__file__).resolve().parents[1]


def checks(db, lot):
    return {r[0]: (r[2], r[3]) for r in db.execute('SELECT * FROM spec.release_check(%s)', (lot,))}


def verdict(db, lot):
    return db.execute('SELECT spec.release_verdict(%s)', (lot,)).fetchone()[0]


def lot_id(db, lot_no):
    return db.execute('SELECT lot_id FROM trace.lot WHERE lot_no=%s', (lot_no,)).fetchone()[0]


def order_after_observation(db, lot):
    """Every simulated order predates the catalog observation (source-date WARN). Move the order
    to the latest observation date, as in the audit's fault script, so that the other checks decide
    the verdict. The date is read from the catalog: a page cache downloaded later is observed later."""
    db.execute("""UPDATE trace.customer_order
                     SET received_on=(SELECT (max(observed_at) AT TIME ZONE 'UTC')::date FROM spec.norm_version)
                   WHERE order_id=(SELECT order_id FROM trace.lot WHERE lot_id=%s)""", (lot,))


@pytest.fixture
def clean(db):
    lot = lot_id(db, 'L260302-00002')  # DIN EN ISO 19598 Fe//ZnNi8//Cn//T0, shipped, in-window bath
    order_after_observation(db, lot)
    return lot


# Release check: faulted lots -----------------------------------------------------------------

def test_b0_clean_lot_passes(db, clean):
    assert verdict(db, clean) == 'PASS'
    assert db.execute('SELECT * FROM spec.release_verdicts(%s)', (clean,)).fetchone() == ('PASS', 'PASS')


def test_f1_implausible_thickness_on_minimum_only_spec(db, clean):
    db.execute("UPDATE trace.measurement SET value=value*10 WHERE lot_id=%s AND characteristic='thickness_um'", (clean,))
    assert checks(db, clean)['Schichtdicke: Plausibilität'][1] == 'INCOMPLETE'
    assert verdict(db, clean) == 'INCOMPLETE'


def test_f3_coating_system_process_mismatch_fails(db, clean):
    db.execute("UPDATE trace.lot SET spec_code='DIN EN ISO 19598 Fe//Zn8//Cn//T0' WHERE lot_id=%s", (clean,))
    link_lots(db)
    row = checks(db, clean)['Beschichtungssystem / Bad']
    assert row == ('ZNNI_ALK', 'FAIL')
    assert verdict(db, clean) == 'FAIL'


def test_f7_stale_analysis_leaves_gap(db, clean):
    station, line = db.execute('''SELECT s.station_id,t.line_id FROM ref.station s JOIN ref.tank t USING(tank_id)
        WHERE t.function='plate' ORDER BY s.station_id LIMIT 1''').fetchone()
    load = db.execute("""INSERT INTO trace.load(line_id,carrier_no,started_at,finished_at,program)
        VALUES(%s,'TEST-B1','2026-12-15 08:00+00','2026-12-15 12:00+00','test') RETURNING load_id""", (line,)).fetchone()[0]
    db.execute('INSERT INTO trace.load_lot VALUES(%s,%s,100)', (load, clean))
    db.execute("INSERT INTO trace.load_step(load_id,station_id,in_station) VALUES(%s,%s,tstzrange('2026-12-15 09:00+00','2026-12-15 10:40+00'))", (load, station))
    observed, result = checks(db, clean)['Badanalysen: Abdeckung']
    assert result == 'INCOMPLETE' and '1 Lücke' in observed and 'ältere Analysen' in observed
    assert verdict(db, clean) == 'INCOMPLETE'


def test_f8_reworked_parent_not_releasable(db):
    lot = db.execute("SELECT lot_id FROM trace.lot WHERE status='reworked' ORDER BY lot_id LIMIT 1").fetchone()[0]
    order_after_observation(db, lot)
    assert checks(db, lot)['Losstatus'] == ('nachgearbeitet', 'FAIL')
    assert verdict(db, lot) == 'FAIL'


@pytest.mark.parametrize('status,expected', [('received', 'INCOMPLETE'), ('in_process', 'INCOMPLETE'),
                                             ('blocked', 'FAIL'), ('scrapped', 'FAIL'), ('released', 'PASS')])
def test_lot_status_domain(db, clean, status, expected):
    db.execute('UPDATE trace.lot SET status=%s WHERE lot_id=%s', (status, clean))
    assert checks(db, clean)['Losstatus'][1] == expected


def test_complaint_lot_still_warns_for_the_same_reasons(db, lot):
    rows = checks(db, lot)
    assert [k for k, v in rows.items() if v[1] != 'PASS'] == ['Quellenstand', 'Bad im Prozessfenster']
    assert rows['Bad im Prozessfenster'][0] == 'Analyse 44: Ni 0,926 < 1,1 g/L'
    assert db.execute('SELECT * FROM spec.release_verdicts(%s)', (lot,)).fetchone() == ('WARN', 'WARN')


def test_policy_in_database_matches_config(db):
    policy = yaml.safe_load((ROOT / 'config/release_policy.yaml').read_text(encoding='utf-8'))
    samples, days, factor = db.execute('''SELECT min_distinct_thickness_samples,extract(day FROM max_analysis_age)::int,
        max_thickness_factor FROM spec.release_policy''').fetchone()
    assert (samples, days, factor) == (policy['minimum_distinct_thickness_samples'], policy['max_analysis_age_days'], policy['max_thickness_factor'])
    pairs = {tuple(r) for r in db.execute('SELECT coating_system,process_code FROM spec.coating_system_process')}
    assert pairs == {(s, p) for s, ps in policy['coating_system_processes'].items() for p in ps}


# Catalog link --------------------------------------------------------------------------------

def test_stale_link_incomplete_with_reason(db, clean):
    other = db.execute("SELECT coating_type_id FROM spec.coating_type WHERE type_code='Fe//ZnNi12//Cn//T0' LIMIT 1").fetchone()[0]
    db.execute('UPDATE trace.lot SET coating_type_id=%s WHERE lot_id=%s', (other, clean))
    rows = checks(db, clean)
    assert rows['Beschichtungszuordnung'][1] == 'INCOMPLETE' and 'veraltet' in rows['Beschichtungszuordnung'][0]
    db.execute("UPDATE trace.lot SET status='scrapped' WHERE lot_id=%s", (clean,))
    assert verdict(db, clean) == 'FAIL'  # a real deviation is not hidden by the link problem


def test_norm_name_whitespace_ignored_for_matching(db, clean):
    db.execute("UPDATE trace.lot SET spec_code='DIN EN  ISO 19598  Fe//ZnNi8//Cn//T0 ' WHERE lot_id=%s", (clean,))
    assert db.execute('SELECT count(*) FROM spec.v_link_candidates WHERE lot_id=%s', (clean,)).fetchone()[0] == 1


# Bath evidence permissions -------------------------------------------------------------------

def test_lab_cannot_move_analysis(db, lot):
    db.execute('SET LOCAL ROLE role_lab')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("UPDATE lab.analysis SET sampled_at='2026-04-30 11:00+00' WHERE analysis_id=44")


def test_lab_cannot_delete_analysis(db, lot):
    db.execute('SET LOCAL ROLE role_lab')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('DELETE FROM lab.analysis WHERE analysis_id=44')


def test_lab_insert_is_audited_and_keeps_timeline(db, lot):
    tank = db.execute("SELECT tank_id FROM ref.tank WHERE function='plate' LIMIT 1").fetchone()[0]
    db.execute('SET LOCAL ROLE role_lab')
    new = db.execute("INSERT INTO lab.analysis(tank_id,sampled_at,valid_during,source) VALUES(%s,'2026-12-01 05:00+00',tstzrange('2026-12-01 05:00+00',NULL),'test') RETURNING analysis_id", (tank,)).fetchone()[0]
    db.execute('RESET ROLE')
    logged = db.execute("SELECT count(*) FROM audit.change_log WHERE table_name='lab.analysis' AND row_pk->>'analysis_id'=%s", (str(new),)).fetchone()[0]
    assert logged == 1
    assert verdict(db, lot) == 'WARN'


# Parser-only changes -------------------------------------------------------------------------

def test_parser_only_change_creates_no_version(db, monkeypatch):
    page = dict(norm_code='TEST PARSER', url='https://example.invalid/parser', modified=None,
                rows=[dict(typ='ABC', beschichtung='Zink transparent ohne Versiegelung', schichtdicke='5 - 10', trommel='Ja', gestell='Ja')])
    evidence = dict(fetched_at='2026-01-01T00:00:00+00:00', sha256='a' * 64)
    assert load_page(db, page, evidence, date(2026, 1, 1))[1] is True
    import p4_partec_norms
    original = p4_partec_norms.clean_rows
    monkeypatch.setattr(p4_partec_norms, 'clean_rows', lambda rows, code: [dict(r, quality_flags=r['quality_flags'] + ['new_rule']) for r in original(rows, code)])
    evidence = dict(fetched_at='2026-02-01T00:00:00+00:00', sha256='b' * 64)
    assert load_page(db, page, evidence, date(2026, 2, 1))[1] is False
    assert db.execute("SELECT count(*) FROM spec.norm_version JOIN spec.norm USING(norm_id) WHERE norm_code='TEST PARSER'").fetchone()[0] == 1


def test_changed_page_after_backdated_first_build_takes_effect_when_observed(db):
    # The build passes its initial effective date for every page on every run.
    page = dict(norm_code='TEST REBUILD', url='https://example.invalid/rebuild', modified=None,
                rows=[dict(typ='ABC', beschichtung='Zink transparent ohne Versiegelung', schichtdicke='5 - 10', trommel='Ja', gestell='Ja')])
    assert load_page(db, page, dict(fetched_at='2026-02-01T00:00:00+00:00', sha256='a' * 64), date(2026, 1, 1), initial_only=True)[1] is True
    page['rows'][0]['schichtdicke'] = '20 - 30'
    assert load_page(db, page, dict(fetched_at='2026-06-01T00:00:00+00:00', sha256='b' * 64), date(2026, 1, 1), initial_only=True)[1] is True
    versions = db.execute("""SELECT valid_from,valid_to,historical_assumption FROM spec.norm_version JOIN spec.norm USING(norm_id)
                              WHERE norm_code='TEST REBUILD' ORDER BY valid_from""").fetchall()
    assert versions == [(date(2026, 1, 1), date(2026, 6, 1), True), (date(2026, 6, 1), None, False)]


# Observation dates ---------------------------------------------------------------------------

def test_backdate_without_flag_rejected(db, lot):
    version = db.execute('SELECT version_id FROM spec.coating_type WHERE coating_type_id=(SELECT coating_type_id FROM trace.lot WHERE lot_id=%s)', (lot,)).fetchone()[0]
    db.execute('SET LOCAL ROLE role_ingest')
    db.execute("UPDATE spec.norm_version SET valid_to='2026-02-01' WHERE version_id=%s", (version,))
    with pytest.raises(psycopg.errors.RaiseException):  # earlier observation than the existing version
        db.execute('''INSERT INTO spec.norm_version(norm_id,valid_from,observed_at,raw_sha256,content_sha256,historical_assumption)
            SELECT norm_id,'2026-02-01','2026-02-01',raw_sha256,content_sha256,false FROM spec.norm_version WHERE version_id=%s''', (version,))


def test_version_before_observation_needs_flag(db):
    norm = db.execute("INSERT INTO spec.norm(norm_code,page_url) VALUES('TEST FLAG','https://example.invalid/flag') RETURNING norm_id").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO spec.norm_version(norm_id,valid_from,observed_at,raw_sha256,content_sha256) VALUES(%s,'2026-01-01','2026-10-05 12:00+00','x','x')", (norm,))


def test_order_between_observations_warns(db, lot):
    page = dict(norm_code='TEST BETWEEN', url='https://example.invalid/between', modified=None,
                rows=[dict(typ='ABC', beschichtung='Zink-Nickel-Legierung transparent ohne Versiegelung', schichtdicke='5 - 30', trommel='Ja', gestell='Ja')])
    load_page(db, page, dict(fetched_at='2026-02-01T00:00:00+00:00', sha256='a' * 64), None)
    page['rows'][0]['schichtdicke'] = '6 - 30'
    load_page(db, dict(page, modified=None), dict(fetched_at='2026-06-01T00:00:00+00:00', sha256='b' * 64), None)
    db.execute("UPDATE trace.lot SET spec_code='TEST BETWEEN ABC' WHERE lot_id=%s", (lot,))
    db.execute("UPDATE trace.customer_order SET received_on='2026-03-15' WHERE order_id=(SELECT order_id FROM trace.lot WHERE lot_id=%s)", (lot,))
    link_lots(db)
    observed, result = checks(db, lot)['Quellenstand']
    assert result == 'WARN' and 'zwischen dieser und der nächsten Beobachtung' in observed


# Requirements and supplier references --------------------------------------------------------

def test_reference_kind_cannot_change(db, lot):
    ref = db.execute('''INSERT INTO spec.requirement(coating_type_id,characteristic,lsl,lower_operator,unit,kind,source,context)
        SELECT coating_type_id,'supplier_test_white',1,'>','h','reference','test','test' FROM trace.lot WHERE lot_id=%s RETURNING requirement_id''', (lot,)).fetchone()[0]
    db.execute('SET LOCAL ROLE role_quality')
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE spec.requirement SET kind='requirement' WHERE requirement_id=%s", (ref,))


def test_requirement_unit_must_match(db, lot):
    ct = db.execute('SELECT coating_type_id FROM trace.lot WHERE lot_id=%s', (lot,)).fetchone()[0]
    db.execute("INSERT INTO spec.requirement(coating_type_id,characteristic,lsl,unit,kind,source,context) VALUES(%s,'ni_pct',12,'%%','requirement','test','test')", (ct,))
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE spec.requirement SET unit='mm' WHERE coating_type_id=%s AND characteristic='ni_pct'", (ct,))


def test_strict_lower_bound(db, clean):
    ct = db.execute('SELECT coating_type_id FROM trace.lot WHERE lot_id=%s', (clean,)).fetchone()[0]
    low = db.execute("SELECT min(value) FROM trace.measurement WHERE lot_id=%s AND characteristic='thickness_um'", (clean,)).fetchone()[0]
    db.execute("INSERT INTO spec.requirement(coating_type_id,characteristic,lsl,lower_operator,unit,kind,source,context) VALUES(%s,'thickness_um',%s,'>','µm','requirement','test','test')", (ct, low))
    assert verdict(db, clean) == 'FAIL'


def test_supplier_references_only_for_matching_finish(db):
    finishes = db.execute('''SELECT DISTINCT ct.passivation,ct.sealed FROM spec.requirement r JOIN spec.coating_type ct USING(coating_type_id)
        WHERE r.kind='reference' ''').fetchall()
    assert set(finishes) <= {('black', True), ('transparent', False), ('transparent', True), (None, True), (None, False)}
    assert ('black', False) not in finishes and ('thick_layer', False) not in finishes


# Report pair ---------------------------------------------------------------------------------

def test_report_hash_matches_file_on_disk(tmp_path):
    data = snapshot('L260430-01721')
    pdf = tmp_path / 'Pruefbericht_L260430-01721.pdf'
    render(data, pdf)
    on_disk = hashlib.sha256(pdf.with_suffix('.json').read_bytes()).hexdigest()
    footer = re.search(r'Snapshot: ([0-9a-f]{16})', PdfReader(pdf).pages[0].extract_text()).group(1)
    assert on_disk.startswith(footer)
    assert b'\r' not in pdf.with_suffix('.json').read_bytes()
    assert not list(tmp_path.glob('*.tmp.*'))
    meta = PdfReader(pdf).metadata
    assert 'Partec' not in (meta.author or '')


def test_report_names_bath_evidence_and_provenance():
    data = snapshot('L260430-01721')
    assert [a['analysis_id'] for a in data['bath_evidence']['analyses']] == [44]
    ni = [r for r in data['bath_evidence']['analyses'][0]['results'] if r['parameter_code'] == 'NI'][0]
    assert ni['status'] == 'LOW'
    assert all(c['counted_during'] for c in data['bath_evidence']['coverage'])
    assert data['catalog']['assumed_for_this_order'] is True and data['catalog']['raw_cells']
    assert data['references'] == []  # black unsealed: no matching supplier result
    json.dumps(data, default=str)


def test_pass_report_has_no_backdate_sentence(db, clean, tmp_path):
    data = snapshot('L260302-00002', conn=db)
    render(data, tmp_path / 'r.pdf')
    text = PdfReader(tmp_path / 'r.pdf').pages[0].extract_text()
    assert data['verdict'] == 'PASS' and data['catalog']['assumed_for_this_order'] is False
    assert 'rückwirkend' not in text


def test_overflow_moves_previous_pair_aside(tmp_path, monkeypatch):
    data = snapshot('L260430-01721')
    pdf = tmp_path / 'Pruefbericht_X.pdf'
    render(data, pdf)
    data['measurements'] = [dict(m, characteristic=f'extra_{i}') for i in range(80) for m in data['measurements'][:1]]
    with pytest.raises(RuntimeError):
        render(data, pdf)
    assert not pdf.exists() and not pdf.with_suffix('.json').exists()
    assert (tmp_path / 'superseded' / pdf.name).exists()
    assert not list(tmp_path.glob('*.tmp.*'))
