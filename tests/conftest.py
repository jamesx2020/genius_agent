import os
import tempfile
from pathlib import Path
import pytest

TEST_DIR=tempfile.TemporaryDirectory(prefix='zhuxu-tests-')
os.environ['DATA_DIR']=TEST_DIR.name
os.environ['DEEPSEEK_API_KEY']=''

from backend.config import settings
from backend.db import Base,engine,init_db
from backend.app import app
from fastapi.testclient import TestClient

@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql('DROP TABLE IF EXISTS alembic_version')
    for suffix in ('','-wal','-shm'):
        Path(TEST_DIR.name,'checkpoints.db'+suffix).unlink(missing_ok=True)
    init_db()
    yield

@pytest.fixture
def client():
    # No lifespan: tests explicitly drive the worker's graph execution.
    return TestClient(app)

@pytest.fixture
def login(client):
    def perform(username='pm'):
        r=client.post('/api/auth/login',json={'username':username,'password':'demo123'})
        assert r.status_code==200
        client.headers['X-CSRF-Token']=r.json()['csrf']
        return r.json()
    return perform
