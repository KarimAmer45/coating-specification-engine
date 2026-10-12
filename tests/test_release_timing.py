"""Inspection timing, inherited exposure, late analysis entries, report layout and download cache tests."""
import hashlib
import json
import sys
from pathlib import Path
import pytest
from p4_release_report import snapshot, render
from test_release_rules import checks, verdict, order_after_observation, clean  # noqa: F401  (fixture re-export)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def plate_tank(db):
    return db.execute("SELECT tank_id FROM ref.tank WHERE function='plate' ORDER BY tank_id LIMIT 1").fetchone()[0]


def test_measurements_before_plating_not_counted(db, clean):
    db.execute("UPDATE trace.measurement SET measured_at='2026-03-01 00:00+00' WHERE lot_id=%s", (clean,))
    rows = checks(db, clean)
    observed, result = rows['Schichtdicke: Stichprobe']
    assert result == 'INCOMPLETE' and observed.startswith('0; 5 Messungen vor Beschichtungsende')
    assert rows['Schichtdicke: Minimum'] == ('Nicht gemessen', 'INCOMPLETE')
    assert verdict(db, clean) == 'INCOMPLETE'


def test_simulated_readings_are_after_plating(db):
    early = db.execute('''SELECT count(*) FROM trace.lot l, spec.lot_measurements(l.lot_id) m
        WHERE l.lot_id <= 300 AND m.before_plating_end''').fetchone()[0]
    assert early == 0


def test_rework_child_without_own_plating(db):
    child = db.execute("SELECT lot_id FROM trace.lot WHERE origin='rework' ORDER BY lot_id LIMIT 1").fetchone()[0]
    # A carrier that only went through a rinse tank, never through plating.
    tank, line = db.execute("SELECT tank_id,line_id FROM ref.tank WHERE function='rinse' ORDER BY tank_id LIMIT 1").fetchone()
    station = db.execute("INSERT INTO ref.station(tank_id,slot,code) VALUES(%s,99,'TEST-RINSE') RETURNING station_id", (tank,)).fetchone()[0]
    load = db.execute('''INSERT INTO trace.load(line_id,carrier_no,started_at,finished_at,program)
        VALUES(%s,'TEST-R1','2026-12-15 08:00+00','2026-12-15 12:00+00','test') RETURNING load_id''', (line,)).fetchone()[0]
    db.execute('INSERT INTO trace.load_lot VALUES(%s,%s,10)', (load, child))
    db.execute("INSERT INTO trace.load_step(load_id,station_id,in_station) VALUES(%s,%s,tstzrange('2026-12-15 09:00+00','2026-12-15 09:05+00'))", (load, station))
    rows = checks(db, child)
    assert rows['Badanalysen: Abdeckung'][1] == 'WARN' and 'nur vom Elternlos' in rows['Badanalysen: Abdeckung'][0]
    assert rows['Anlagenart'] == ('Keine eigene Beschichtung erfasst', 'INCOMPLETE')


def test_late_analysis_entry_warns(db, lot):
    order_after_observation(db, lot)
    dwell_start = db.execute('SELECT min(lower(in_station)) FROM spec.plating_exposure(%s)', (lot,)).fetchone()[0]
    tank = plate_tank(db)
    db.execute('SET LOCAL ROLE role_lab')
    # Sampled five minutes before the dwell, entered now; the client's own entry time is overridden.
    new = db.execute('''INSERT INTO lab.analysis(tank_id,sampled_at,valid_during,source,entered_at)
        VALUES(%s,%s - interval '5 minutes',tstzrange(%s - interval '5 minutes',NULL),'manual','2026-04-30 06:00+00')
        RETURNING analysis_id''', (tank, dwell_start, dwell_start)).fetchone()[0]
    db.execute('''INSERT INTO lab.analysis_result SELECT %s,parameter_code,value,method FROM lab.analysis_result
        WHERE analysis_id=(SELECT analysis_id FROM lab.analysis WHERE tank_id=%s AND sampled_at<'2026-04-30' ORDER BY sampled_at DESC LIMIT 1)''', (new, tank))
    db.execute('RESET ROLE')
    rows = checks(db, lot)
    assert rows['Bad im Prozessfenster'] == ('Keine', 'PASS')  # the deviating analysis no longer overlaps the dwell
    observed, result = rows['Badanalysen: Abdeckung']
    assert result == 'WARN' and f'Analyse {new} nachgetragen' in observed
    assert db.execute('SELECT * FROM spec.release_verdicts(%s)', (lot,)).fetchone() == ('WARN', 'WARN')


def test_simulated_analyses_not_flagged_as_late(db):
    assert db.execute('SELECT count(*) FROM lab.analysis WHERE entered_at > sampled_at').fetchone()[0] == 0


def test_analysis_age_cut_is_dst_safe(db, clean):
    tank = plate_tank(db)
    # Friday 27 March 2026 06:00 CET is the last analysis before the switch to CEST; remove Monday's and Tuesday's.
    db.execute("DELETE FROM lab.analysis WHERE tank_id=%s AND sampled_at IN ('2026-03-30 06:00+02','2026-03-31 06:00+02')", (tank,))
    station, line = db.execute('''SELECT s.station_id,t.line_id FROM ref.station s JOIN ref.tank t USING(tank_id)
        WHERE t.tank_id=%s ORDER BY s.station_id LIMIT 1''', (tank,)).fetchone()
    load = db.execute('''INSERT INTO trace.load(line_id,carrier_no,started_at,finished_at,program)
        VALUES(%s,'TEST-DST','2026-03-31 06:20+02','2026-03-31 06:55+02','test') RETURNING load_id''', (line,)).fetchone()[0]
    db.execute('INSERT INTO trace.load_lot VALUES(%s,%s,10)', (load, clean))
    # 06:30-06:50 local time: inside four days in UTC arithmetic (07:00 CEST), after the local cut (06:00 CEST).
    db.execute("INSERT INTO trace.load_step(load_id,station_id,in_station) VALUES(%s,%s,tstzrange('2026-03-31 06:30+02','2026-03-31 06:50+02'))", (load, station))
    observed, result = checks(db, clean)['Badanalysen: Abdeckung']
    assert result == 'INCOMPLETE' and '1 Lücke' in observed


def test_many_deviating_analyses_are_capped(db, lot):
    tank = plate_tank(db)
    for minute in ('09:10', '09:20', '09:30', '09:40'):
        new = db.execute(f"""INSERT INTO lab.analysis(tank_id,sampled_at,valid_during,source)
            VALUES(%s,'2026-04-30 {minute}+00',tstzrange('2026-04-30 {minute}+00',NULL),'test') RETURNING analysis_id""", (tank,)).fetchone()[0]
        db.execute('INSERT INTO lab.analysis_result SELECT %s,parameter_code,value,method FROM lab.analysis_result WHERE analysis_id=44', (new,))
    observed = checks(db, lot)['Bad im Prozessfenster'][0]
    assert observed.count('Analyse ') == 3 and observed.endswith('und 2 weitere')


def test_cell_taller_than_page_is_refused(db, tmp_path):
    data = snapshot('L260430-01721', conn=db)
    pdf = tmp_path / 'Pruefbericht_Y.pdf'
    render(data, pdf)
    data['checks'][0]['observed'] = 'Sehr lange Zelle ' * 3000
    with pytest.raises(RuntimeError):
        render(data, pdf)
    assert not pdf.exists() and (tmp_path / 'superseded' / pdf.name).exists()
    assert not list(tmp_path.glob('*.tmp.*'))


def test_failed_refresh_keeps_good_cache(tmp_path, monkeypatch):
    import requests
    import download_data as dd
    raw = tmp_path / 'raw'
    raw.mkdir()
    index = ''.join(f'<a href="{dd.INDEX}{k}/">{k}</a>' for k in ('a', 'b'))
    manifest = {}
    for name, body in (('_index.html', index), ('a.html', '<h1>A</h1>'), ('b.html', '<h1>B</h1>')):
        (raw / name).write_text(body, encoding='utf-8')
        url = dd.INDEX if name == '_index.html' else f'{dd.INDEX}{name[:-5]}/'
        manifest[url] = dict(status='ok', file=name, fetched_at='2026-10-05T00:00:00+00:00',
                             sha256=hashlib.sha256((raw / name).read_bytes()).hexdigest())
    (raw / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (raw / 'robots.txt').write_text('User-agent: *\nAllow: /\n', encoding='utf-8')

    class Response:
        def __init__(self, url):
            self.url, self.headers, self.encoding = url, {}, None
            self.text = index if url == dd.INDEX else '<h1>new</h1>'
        def raise_for_status(self):
            if self.url.endswith('/b/'):
                raise RuntimeError('HTTP 503')

    class Session:
        def __init__(self):
            self.headers = {}
        def get(self, url, timeout):
            return Response(url)

    monkeypatch.setattr(requests, 'Session', Session)
    monkeypatch.setattr(dd, 'RAW', raw)
    monkeypatch.setattr(dd, 'ROOT', tmp_path)
    monkeypatch.setattr(dd, 'fetch_supplier_pdf', lambda session: None)
    monkeypatch.setattr(dd.time, 'sleep', lambda seconds: None)
    dd.download(refresh=True)
    entry = json.loads((raw / 'manifest.json').read_text(encoding='utf-8'))[f'{dd.INDEX}b/']
    assert entry['status'] == 'ok' and entry['last_error'] == 'HTTP 503'
    assert dd.cache_problems(raw)[1] == []
    # Without a cached copy the page is marked failed and the cache check (used by build.py) reports it.
    (raw / 'b.html').unlink()
    dd.download(refresh=True)
    assert json.loads((raw / 'manifest.json').read_text(encoding='utf-8'))[f'{dd.INDEX}b/']['status'] == 'failed'
    assert dd.cache_problems(raw)[1] == [f'{dd.INDEX}b/']
