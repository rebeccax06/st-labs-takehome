"""Exercise the manual input corpus through HTTP, including persistence and replay."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import ConfigurationItem, IngestionRun, Observation, Relationship
from app.db.session import session_factory
from app.main import create_app
from app.reconciliation.policy import load_sources

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'input_data/test_files'
CASES = [case for case in json.loads((FIXTURES / 'manifest.json').read_text())
         if case['expected'] in {'accepted', 'rejected'}]


class NoAI:
    version = 'manifest-no-ai'

    def prepare(self, *args):
        raise AssertionError('Configured fixtures must not invoke AI')


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['file'])
def test_manual_fixture_http_persistence_and_replay(engine, case):
    app = create_app(settings=Settings(), sources=load_sources(ROOT / 'examples/sample_sources.json'),
                     engine=engine, extractor=NoAI())
    content = (FIXTURES / case['file']).read_bytes()
    with TestClient(app) as client:
        result = client.post('/ingest', files={'file': (case['file'], content)},
                             data={'source_id': case['source_id']})
        if case['expected'] == 'rejected':
            expected_status = 413 if case['error_code'] == 'limit_exceeded' else 422
            assert result.status_code == expected_status, result.text
            assert result.json()['error']['request_id'] == result.headers['X-Request-ID']
            with session_factory(engine)() as db:
                for model in (ConfigurationItem, IngestionRun, Observation, Relationship):
                    assert db.scalar(select(func.count()).select_from(model)) == 0
        else:
            assert result.status_code == 200, result.text
            body = result.json()
            assert body['records']['received'] == case['records']
            replay = client.post('/ingest/raw', params={'source_id': case['source_id']},
                                 content=content, headers={'Content-Type': 'text/plain'})
            assert replay.status_code == 200, replay.text
            assert replay.json()['replayed'] is True
            assert replay.json()['run_id'] == body['run_id']
            with session_factory(engine)() as db:
                assert db.scalar(select(func.count()).select_from(IngestionRun)) == 1
