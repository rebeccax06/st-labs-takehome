"""Full raw input -> real extraction -> persistence -> real planner -> answer."""

from copy import deepcopy
import json

import httpx
import pytest

from .conftest import edge, eq, ingest, query_plan, snapshot


def synthetic_export():
    rows = [
        dict(id='D-42', kind='device', attrs=dict(hostname='build-laptop', os_family='macos', status='active'),
             links=[('assigned_to', 1)]),
        dict(id='U-42', kind='user', attrs=dict(name='Morgan', mfa_enabled=False), links=[('uses', 2)]),
        dict(id='A-42', kind='application', attrs=dict(name='Toolbox'),
             links=[('depends_on', 3), ('integrates_with', 3), ('owned_by', 1)]),
        dict(id='A-43', kind='application', attrs=dict(name='DataHub'), links=[]),
    ]
    def pointer(path):
        return dict(pointer=path)
    def identifier(index):
        kind = rows[index]['kind']
        return dict(identifier_type={'device': 'device_id', 'user': 'user_id', 'application': 'app_id'}[kind],
                    value=rows[index]['id'], evidence=pointer(f'/export_rows/{index}/id'))
    records = []
    for index, row in enumerate(rows):
        path = f'/export_rows/{index}'
        records.append(dict(ci_type=row['kind'], attributes={k: dict(value=v, evidence=pointer(path + '/attrs/' + k)) for k, v in row['attrs'].items()},
            identifiers=[identifier(index)], evidence=pointer(path), relationships=[
                dict(relation_type=relation, evidence=pointer(f'{path}/links/{j}'),
                     target=dict(ci_type=rows[target]['kind'], identifiers=[identifier(target)],
                                 evidence=pointer(f'/export_rows/{target}')))
                for j, (relation, target) in enumerate(row['links'])]))
    return json.dumps(dict(export_rows=rows)), dict(records=records)


def test_upload_extraction_preview_ask_provenance_and_restart_replay(backend_factory):
    backend = backend_factory()
    content, extracted = synthetic_export()
    backend.provider.outputs['cmdb_extraction'] = extracted
    p = query_plan(exists=[edge('assigned_to', 'user', [eq('mfa_enabled', False)],
                               [edge('uses', 'application', [eq('name', 'Toolbox')])])])
    backend.provider.planning(p)
    question = 'Which devices belong to users without MFA who use Toolbox?'
    with backend.http() as client:
        uploaded = client.post('/ingest', files={'file': ('custom.json', content, 'application/json')}, data={'source_id': 'notes'})
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()['entities']['created'] == 4
        assert uploaded.json()['relationships']['created'] == 5
        device = client.get('/devices').json()['data'][0]
        device_id = device['id']
        detail = client.get('/ci/' + device_id).json()
        assert detail['provenance']['data'][0]['origin'] == 'ai'
        assert detail['field_state']['hostname']['evidence'][0]['extraction_evidence']['pointer'].endswith('/attrs/hostname')
        before = snapshot(backend)
        preview = client.post('/ask/plan', json={'question': question})
        assert preview.status_code == 200 and preview.json()['executed'] is False
        answer = client.post('/ask', json={'question': question, 'limit': 1})
        assert answer.status_code == 200, answer.text
        assert answer.json()['total'] == 1 and answer.json()['data'][0]['id'] == device_id
        assert answer.json()['resolved_references'][0]['ci_id']
        assert snapshot(backend) == before  # preview and answer are read-only
        for relation in ['depends_on', 'integrates_with']:
            backend.provider.planning(query_plan('application', exists=[edge(relation, 'application', [eq('name', 'DataHub')])]))
            result = client.post('/ask', json={'question': f'Which applications {relation} DataHub?'}).json()
            assert result['total'] == 1 and result['data'][0]['attributes']['name'] == 'Toolbox'
        backend.provider.planning(query_plan('application', exists=[edge('owned_by', 'user', [eq('name', 'Morgan')])]))
        assert client.post('/ask', json={'question': 'Which applications are owned by Morgan?'}).json()['total'] == 1
    backend.engine.dispose()
    restarted = backend_factory(provider=backend.provider)
    calls_before = len(backend.provider.calls)
    with restarted.http() as client:
        replay = client.post('/ingest/raw?source_id=notes', content=content, headers={'Content-Type': 'text/plain'})
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert replay.json()['run_id'] == uploaded.json()['run_id']
        assert len(backend.provider.calls) == calls_before
        assert snapshot(restarted) == before
    # Only interpretation metadata/question reach the planner, never DB records.
    for call in backend.provider.calls:
        if call['response_format']['json_schema']['name'] == 'cmdb_query_plan':
            text = json.dumps(call)
            assert device_id not in text and 'build-laptop' not in text


@pytest.mark.parametrize('route', ['extract', 'ask'])
@pytest.mark.parametrize('fault,status,expected_calls', [
    ('timeout', 504, 1), ('rate_limit', 503, 1), ('provider_error', 503, 1),
    ('invalid_envelope', 502, 1), ('invalid_json', 502, 2), ('unsafe_output', 502, 2),
])
def test_provider_failures_preserve_existing_database_and_recover(backend_factory, route, fault, status, expected_calls):
    backend = backend_factory()
    content, good = synthetic_export()
    with backend.http() as client:
        assert ingest(client, [{'device_id': 'existing', 'status': 'active'}]).status_code == 200
        before = snapshot(backend)
        if fault == 'timeout':
            bad = httpx.ReadTimeout('PRIVATE upstream detail')
        elif fault in {'rate_limit', 'provider_error'}:
            bad = httpx.Response(429 if fault == 'rate_limit' else 500, text='PRIVATE provider body')
        elif fault == 'invalid_envelope':
            bad = httpx.Response(200, json={'choices': None, 'private': 'PRIVATE provider body'})
        elif fault == 'invalid_json':
            bad = 'PRIVATE not JSON'
        elif route == 'extract':
            bad = deepcopy(good)
            bad['records'][0]['identifiers'][0]['value'] = 'INVENTED-ID'
        else:
            bad = dict(result=dict(status='planned', plan=dict(root='device', operation='delete', sql='DROP TABLE devices')))
        schema = 'cmdb_extraction' if route == 'extract' else 'cmdb_query_plan'
        backend.provider.outputs[schema] = bad
        def submit():
            if route == 'extract':
                return client.post('/ingest/raw?source_id=notes', content=content, headers={'Content-Type': 'text/plain'})
            return client.post('/ask', json={'question': 'How many active devices are there?'})
        response = submit()
        assert response.status_code == status, response.text
        assert response.json()['error']['request_id'] == response.headers['x-request-id']
        assert 'PRIVATE' not in response.text and 'integration-test-key' not in response.text
        assert len(backend.provider.calls) == expected_calls
        assert snapshot(backend) == before
        if route == 'extract':
            backend.provider.outputs[schema] = good
            recovered = submit()
            assert recovered.status_code == 200 and recovered.json()['replayed'] is False
        else:
            backend.provider.planning(query_plan(where=[eq('status', 'active')], operation='count'))
            recovered = submit()
            assert recovered.status_code == 200 and recovered.json()['total'] == 1
            assert snapshot(backend) == before
