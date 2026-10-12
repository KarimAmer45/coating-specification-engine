import os
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

@pytest.fixture
def db():
    if not (os.environ.get('DATABASE_URL') or os.environ.get('PGDATABASE')):
        pytest.skip('Database tests require an explicitly configured project database')
    conn=connect('tests_rolled_back')
    try:yield conn
    finally:conn.rollback();conn.close()

@pytest.fixture
def lot(db):
    return db.execute('SELECT lot_id FROM trace.complaint ORDER BY complaint_id LIMIT 1').fetchone()[0]
