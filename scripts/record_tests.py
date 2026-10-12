"""Run the test suite and record the result in docs/results/smoke_checks.json (generated, not hand-written)."""
import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def main():
    with tempfile.TemporaryDirectory() as tmp:
        junit=Path(tmp)/'junit.xml'
        code=subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider','tests',f'--junitxml={junit}'],cwd=ROOT).returncode
        suite=ET.parse(junit).getroot()
        suite=suite if suite.tag=='testsuite' else suite.find('testsuite')
    total,failures,errors,skipped=(int(suite.get(k,0)) for k in ('tests','failures','errors','skipped'))
    with connect('record_tests') as conn:
        server=conn.execute('SHOW server_version').fetchone()[0]
    reports=sorted((ROOT/'docs/reports').glob('Pruefbericht_*.pdf'))
    result={
        'run_at':datetime.now(ZoneInfo('Europe/Berlin')).isoformat(timespec='seconds'),
        'command':'python scripts/record_tests.py (pytest -q tests)',
        'pytest':{'tests':total,'passed':total-failures-errors-skipped,'failed':failures,'errors':errors,'skipped':skipped,
                  'seconds':round(float(suite.get('time',0)),2)},
        'python':platform.python_version(),'postgresql':server,
        'reports':{p.name:{'pages':len(PdfReader(p).pages)} for p in reports},
        'scope':'Automated tests against the locally built database. Docker, Flyway and hosted CI runs are not recorded in this file.',
    }
    (ROOT/'docs/results/smoke_checks.json').write_bytes(json.dumps(result,indent=2,ensure_ascii=False).encode('utf-8'))
    print(json.dumps(result['pytest']))
    sys.exit(code)

if __name__=='__main__':main()
