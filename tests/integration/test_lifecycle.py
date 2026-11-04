"""Public API ingestion/read/ask flows, restarts and transaction recovery."""

import json

import pytest
from sqlalchemy import func, select

from app.db.models import ConfigurationItem, IngestionRun, Observation
from .conftest import edge, eq, ingest, query_plan, snapshot


def test_restart_cross_transport_replay_and_reformatted_duplicate(backend_factory):
    backend = backend_factory()
    rows = [{'device_id': 'D1', 'hostname': 'quoted "host"', 'status': 'active', 'os': 'macOS'}]
    content = json.dumps(rows)
    with backend.http() as client:
        first = ingest(client, rows)
        assert first.status_code == 200, first.text
        original = snapshot(backend)
        device_id = client.get('/devices').json()['data'][0]['id']
        detail = client.get('/ci/' + device_id).json()
        assert detail['provenance']['total'] == 1
        assert detail['provenance']['data'][0]['source_id'] == 'hardware'
        for result in [client.post('/ingest/raw?source_id=hardware', content=content, headers={'Content-Type': 'text/plain'}),
                       client.post('/ingest', json=dict(organization_id='org-a', source_id='hardware', format='json', content=content))]:
            assert result.status_code == 200, result.text
            assert result.json()['run_id'] == first.json()['run_id'] and result.json()['replayed'] is True
        assert snapshot(backend) == original
    backend.engine.dispose()
    restarted = backend_factory(name='integration')  # new engine, application and policy, same file
    with restarted.http() as client:
        replay = ingest(client, rows)
        assert replay.json()['replayed'] is True
        assert replay.json()['run_id'] == first.json()['run_id']
        assert client.get('/devices').json()['data'][0]['id'] == device_id
        assert snapshot(restarted) == original
        reformatted = client.post('/ingest/raw?source_id=hardware', content=json.dumps(rows, indent=2),
                                  headers={'Content-Type': 'text/plain'})
        assert reformatted.status_code == 200 and reformatted.json()['replayed'] is False
        assert client.get('/devices').json()['total'] == 1
        restarted.provider.planning(query_plan(where=[eq('status', 'active')], operation='count'))
        answer = client.post('/ask', json={'question': 'How many active devices are there?'}).json()
        assert answer['status'] == 'answered' and answer['total'] == 1 and answer['data'] is None
    assert not backend.provider.calls
    with restarted.sessions() as db:
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 2
        assert db.scalar(select(func.count()).select_from(Observation)) == 2


def test_partial_identity_failure_commits_quarantine_without_corrupting_valid_entities(backend_factory):
    backend = backend_factory()
    with backend.http() as client:
        initial = ingest(client, [{'device_id': 'D1', 'serial_number': 'S1', 'hostname': 'one'},
                                  {'device_id': 'D2', 'serial_number': 'S2', 'hostname': 'two'}])
        assert initial.status_code == 200
        rows = [{'device_id': 'D1', 'serial_number': 'S2', 'hostname': 'wrong'},
                {'hostname': 'keyless'}, {'device_id': 'D3', 'hostname': 'three'}]
        response = ingest(client, rows)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['records'] == dict(received=3, resolved=1, quarantined=2)
        assert {'identity_conflict', 'missing_identifiers'} <= {w['code'] for w in body['warnings']}
        assert {d['attributes']['hostname'] for d in client.get('/devices').json()['data']} == {'one', 'two', 'three'}
        backend.provider.planning(query_plan(operation='count'))
        assert client.post('/ask', json={'question': 'Count devices'}).json()['total'] == 3
        original = snapshot(backend)
        assert ingest(client, rows).json()['replayed'] is True
        assert snapshot(backend) == original
    with backend.sessions() as db:
        conflicts = list(db.scalars(select(Observation).where(Observation.resolution_status == 'identity_conflict')))
        assert len(conflicts) == 1 and conflicts[0].ci_id is None
        assert len(conflicts[0].normalized_record['candidate_ids']) == 2


@pytest.mark.parametrize('stage', ['attributes', 'relationships', 'commit'])
def test_http_failure_rolls_back_existing_changes_and_retries_cleanly(backend_factory, monkeypatch, stage):
    import app.ingestion.persistence as persistence
    from sqlalchemy import event
    backend = backend_factory()
    with backend.http() as client:
        assert ingest(client, [{'device_id': 'D1', 'hostname': 'original'}]).status_code == 200
        before = snapshot(backend)
        rows = [{'device_id': 'D1', 'hostname': 'changed'}, {'device_id': 'D2', 'hostname': 'new'}]
        def fail(*args):
            raise RuntimeError('PRIVATE failure with SQL parameters and secret')
        with monkeypatch.context() as patch:
            if stage == 'commit':
                # Runs after all projections and flushes, immediately before commit.
                event.listen(backend.engine, 'commit', fail)
            else:
                patch.setattr(persistence, 'project_attributes' if stage == 'attributes' else 'project_relationships', fail)
            try:
                result = ingest(client, rows)
            finally:
                if stage == 'commit':
                    event.remove(backend.engine, 'commit', fail)
        assert result.status_code == 500
        assert result.json()['error']['request_id'] == result.headers['x-request-id']
        assert 'PRIVATE' not in result.text and 'secret' not in result.text
        assert snapshot(backend) == before
        result = ingest(client, rows)
        assert result.status_code == 200 and result.json()['replayed'] is False
        devices = client.get('/devices').json()
        assert devices['total'] == 2
        old = next(d for d in devices['data'] if d['attributes']['hostname'] != 'new')
        assert old['attributes']['hostname'] is None and old['has_conflicts']  # both undated values retained
        assert ingest(client, rows).json()['replayed'] is True


def test_real_sqlite_write_lock_has_uniform_error_then_recovers(backend_factory):
    backend = backend_factory()
    with backend.http() as client:
        assert ingest(client, [{'device_id': 'D1'}]).status_code == 200
        before = snapshot(backend)
        with backend.engine.connect().execution_options(sqlite_write=True) as connection:
            with connection.begin():
                result = ingest(client, [{'device_id': 'D2'}])
                assert result.status_code == 503
                assert result.json()['error']['code'] == 'database_busy'
                assert result.json()['error']['request_id'] == result.headers['x-request-id']
        assert snapshot(backend) == before
        assert ingest(client, [{'device_id': 'D2'}]).status_code == 200
        assert client.get('/devices').json()['total'] == 2


@pytest.mark.parametrize('fault,status', [
    ('validation', 422), ('missing_ci', 404), ('bad_json', 422), ('binary', 415),
    ('too_large', 413), ('unknown_source', 422),
])
def test_error_contract_and_no_side_effects_on_nonempty_database(backend_factory, fault, status):
    backend = backend_factory()
    with backend.http() as client:
        assert ingest(client, [{'device_id': 'D1', 'hostname': 'preserved'}]).status_code == 200
        before = snapshot(backend)
        client.headers['X-Request-ID'] = 'client-forged-id'
        if fault == 'validation':
            result = client.post('/ask', json={'question': 'Count', 'organization_id': 'forbidden'})
        elif fault == 'missing_ci':
            result = client.get('/ci/00000000-0000-4000-8000-000000000000')
        elif fault == 'bad_json':
            result = client.post('/ingest/raw?source_id=hardware', content='{bad', headers={'Content-Type': 'text/plain'})
        elif fault == 'binary':
            result = client.post('/ingest', files={'file': ('private.pdf', b'%PDF-private')})
        elif fault == 'too_large':
            result = client.post('/ingest/raw?source_id=hardware', content='x' * (1024*1024 + 1),
                                 headers={'Content-Type': 'text/plain'})
        else:
            result = client.post('/ingest/raw?source_id=missing', content='anything', headers={'Content-Type': 'text/plain'})
        assert result.status_code == status, result.text
        error = result.json()['error']
        assert set(error) == {'code', 'message', 'request_id', 'details'}
        assert error['request_id'] == result.headers['x-request-id']
        assert error['request_id'] != 'client-forged-id'
        assert snapshot(backend) == before
        assert not backend.provider.calls


def test_concurrent_http_transports_commit_one_replay_run(backend_factory):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    backend = backend_factory(busy_timeout_ms=1500)
    rows = [{'device_id': 'concurrent', 'hostname': 'single-ci'}]
    ready = Barrier(2)
    with backend.http() as client:
        def submit(index):
            ready.wait(timeout=10)
            if index == 0:
                return ingest(client, rows)
            return client.post('/ingest/raw?source_id=hardware', content=json.dumps(rows),
                               headers={'Content-Type': 'text/plain'})
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(submit, [0, 1]))
        assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
        assert {r.json()['replayed'] for r in results} == {False, True}
        assert len({r.json()['run_id'] for r in results}) == 1
        assert client.get('/devices').json()['total'] == 1
    with backend.sessions() as db:
        for table in (ConfigurationItem, IngestionRun, Observation):
            assert db.scalar(select(func.count()).select_from(table)) == 1
