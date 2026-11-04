"""Supplied fixtures are test cases, not identity or conflict policy in code."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.inventory_service import InventoryService
from app.db.models import ConfigurationItem, Relationship
from app.reconciliation.policy import load_sources
from .conftest import edge, eq, query_plan, snapshot

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'examples/sample_sources.json').read_text())


def upload_all(client, order):
    for index, file_index in enumerate(order):
        entry = MANIFEST[file_index]
        source = entry['source']
        content = (ROOT / 'input_data' / entry['file']).read_text()
        fmt = 'yaml' if source['adapter'] == 'hardware_yaml' else 'csv' if source['adapter'] == 'hardware_csv' else 'json'
        if index % 3 == 0:
            result = client.post('/ingest', files={'file': (entry['file'], content)}, data={'source_id': source['source_id']})
        elif index % 3 == 1:
            result = client.post('/ingest/raw', params={'source_id': source['source_id']}, content=content,
                                 headers={'Content-Type': 'text/plain'})
        else:
            result = client.post('/ingest', json=dict(organization_id=source['organization_id'],
                source_id=source['source_id'], format=fmt, content=content))
        assert result.status_code == 200, result.text
        yield result.json()


def semantic_snapshot(backend):
    """Compare projections/edges by stable identifiers, ignoring generated UUIDs."""
    with backend.sessions() as db:
        items = list(db.scalars(select(ConfigurationItem)))
        keys = {ci.id: tuple(sorted((key.namespace, key.value) for key in ci.identifiers)) for ci in items}
        inventory = InventoryService(db, 'example-corp')
        records = [dict(key=keys[ci.id], kind=ci.kind, display_name=ci.display_name, reference=ci.is_reference,
                        attributes=inventory.view(ci)['attributes'],
                        states={field: state['status'] for field, state in ci.field_state.items()}) for ci in items]
        edges = [(keys[edge.from_ci_id], edge.relation_type, keys[edge.to_ci_id])
                 for edge in db.scalars(select(Relationship))]
    return sorted(json.dumps(r, sort_keys=True) for r in records), sorted(edges)


@pytest.mark.parametrize('order', [(4, 3, 2, 1, 0), (3, 4, 0, 2, 1), (2, 0, 4, 1, 3)])
def test_fixture_orders_transports_replay_and_grounded_answers(backend_factory, order):
    sources = load_sources(ROOT / 'examples/sample_sources.json')
    baseline = backend_factory(sources=sources, name='baseline')
    with baseline.http() as client:
        list(upload_all(client, range(5)))
    backend = backend_factory(sources=sources, name='permuted')
    with backend.http() as client:
        summaries = list(upload_all(client, order))
        assert not any(s['replayed'] for s in summaries)
        assert semantic_snapshot(backend) == semantic_snapshot(baseline)
        assert [client.get(path).json()['total'] for path in ['/devices', '/users', '/apps']] == [3, 12, 28]
        before = snapshot(backend)
        replayed = list(upload_all(client, tuple(reversed(order))))
        assert all(s['replayed'] for s in replayed)
        assert {s['run_id'] for s in replayed} == {s['run_id'] for s in summaries}
        assert snapshot(backend) == before
        uses = edge('uses', 'application', [eq('name', 'GitHub')])
        cases = [
            ("Which users don't have MFA?", query_plan('user', [eq('mfa_enabled', False)]), 3),
            ('Which devices run macOS?', query_plan(where=[eq('os_family', 'macos')]), 2),
            ('Who uses GitHub?', query_plan('user', exists=[uses]), 5),
            ('Which devices belong to users without MFA?', query_plan(exists=[
                edge('assigned_to', 'user', [eq('mfa_enabled', False)])]), 0),
            ('Which devices belong to users without MFA who use GitHub?', query_plan(exists=[
                edge('assigned_to', 'user', [eq('mfa_enabled', False)], [uses])]), 0),
            ('Which applications depend on Salesforce?', query_plan('application', exists=[
                edge('depends_on', 'application', [eq('name', 'Salesforce')])]), 0),
            ('How many active devices are there?', query_plan(where=[eq('status', 'active')], operation='count'), 1),
        ]
        for question, p, total in cases:
            backend.provider.planning(p)
            response = client.post('/ask', json={'question': question})
            assert response.status_code == 200, response.text
            answer = response.json()
            assert answer['status'] == 'answered' and answer['total'] == total
            if 'belong' in question:
                assert any(w['code'] == 'unknown_field' and w['field'] == 'mfa_enabled' and w['count'] == 3
                           for w in answer['warnings'])
            if 'depend on' in question:
                assert any(w['code'] == 'no_recorded_relationships' for w in answer['warnings'])
            if p['operation'] == 'count':
                assert answer['data'] is None
                assert any(w['code'] == 'conflicting_field' and w['field'] == 'status' and w['count'] == 2
                           for w in answer['warnings'])
        assert snapshot(backend) == before
    assert not baseline.provider.calls  # known structured sources never need extraction
    assert all(call['response_format']['json_schema']['name'] == 'cmdb_query_plan' for call in backend.provider.calls)


@pytest.mark.parametrize('aliases,expected', [(False, 0), (True, 5)])
def test_application_aliases_are_configuration_not_fixture_branches(backend_factory, aliases, expected):
    sources = load_sources(ROOT / 'examples/sample_sources.json')
    if not aliases:
        sources = [source.model_copy(update={'application_aliases': {}}) for source in sources]
    backend = backend_factory(sources=sources)
    with backend.http() as client:
        list(upload_all(client, range(5)))
        backend.provider.planning(query_plan('user', exists=[edge('uses', 'application', [eq('name', 'GitHub Enterprise')])]))
        result = client.post('/ask', json={'question': 'Who uses GitHub Enterprise?'}).json()
        assert result['status'] == 'answered' and result['total'] == expected
