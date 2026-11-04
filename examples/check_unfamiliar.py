"""Live smoke test of synthetic unfamiliar layouts; isolated databases, no mocks."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.db.session import create_sqlite_engine
from app.ai.extraction import PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', action='append', help='Fixture filename; repeat to select cases. Defaults to all ten.')
    args = parser.parse_args()
    cases = json.loads((ROOT / 'input_data/unfamiliar/manifest.json').read_text())
    if args.file:
        unknown = set(args.file) - {case['file'] for case in cases}
        if unknown:
            parser.error('Unknown fixture: ' + ', '.join(sorted(unknown)))
        cases = [case for case in cases if case['file'] in args.file]
    results = []
    for case in cases:
        with TemporaryDirectory(prefix='cmdb-unfamiliar-') as directory:
            settings = Settings.from_env().model_copy(update={'database_url': f'sqlite:///{directory}/test.db'})
            engine = create_sqlite_engine(settings)
            try:
                with TestClient(create_app(settings=settings, sources=[], engine=engine), raise_server_exceptions=False) as client:
                    content = (ROOT / 'input_data/unfamiliar' / case['file']).read_bytes()
                    response = client.post('/ingest', data={'source_id':'uploaded-data'}, files={'file':(case['file'],content)})
                    result = dict(file=case['file'], extraction_version=PROMPT_VERSION, model=settings.openrouter_model, http_status=response.status_code, ingestion=response.json())
                    if response.status_code == 200:
                        inventories = {kind:client.get(path,params={'limit':100}).json() for kind,path in [('device','/devices'),('user','/users'),('application','/apps')]}
                        items = inventories[case['kind']]['data']
                        details = [client.get('/ci/'+item['id']).json() for page in inventories.values() for item in page['data']]
                        replay = client.post('/ingest',data={'source_id':'uploaded-data'},files={'file':(case['file'],content)})
                        checks = dict(expected_entity=any(case['expected_name'] in json.dumps(item,ensure_ascii=False) for item in items),ai_provenance=any(observation['origin'] == 'ai' for detail in details for observation in detail['provenance']['data']),replay=replay.status_code==200 and replay.json().get('replayed') is True and replay.json().get('run_id') == response.json()['run_id'])
                        matching = [item for item in items if item['display_name'] == case['expected_name']]
                        checks['field_preservation'] = any(all(item['attributes'].get(key) == value for key,value in case.get('expected_attributes', {}).items()) for item in matching)
                        if case['kind'] == 'user' and 'mfa_enabled' not in case.get('expected_attributes', {}):
                            checks['missing_mfa_unknown'] = all(item['attributes'].get('mfa_enabled') is None for item in matching)
                        result.update(inventory=inventories,details=details,checks=checks,passed=all(checks.values()))
                    else:
                        result['passed'] = False
                    results.append(result)
                    print(json.dumps(result),flush=True)
            finally:
                engine.dispose()
    print(json.dumps(dict(summary=True,passed=sum(r['passed'] for r in results),total=len(results))),flush=True)
    return 0 if all(r['passed'] for r in results) else 1

if __name__ == '__main__':
    raise SystemExit(main())
