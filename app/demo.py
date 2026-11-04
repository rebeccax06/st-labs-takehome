"""Repeatable sample API demo; isolated database, mocked planning unless --live."""

import argparse
import json
from pathlib import Path
import sys
import textwrap
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
import httpx

from app.ai.client import OpenRouterClient
from app.ai.extraction import Extractor
from app.config import Settings
from app.db.session import create_sqlite_engine
from app.main import create_app
from app.querying.planner import QueryPlanner
from app.reconciliation.policy import Policy, load_sources

ROOT = Path(__file__).resolve().parents[1]


def demo(*, live=False):
    manifest = json.loads((ROOT / 'examples/sample_sources.json').read_text(encoding='utf-8'))
    sources = load_sources(ROOT / 'examples/sample_sources.json')
    questions = json.loads((ROOT / 'examples/demo_questions.json').read_text(encoding='utf-8'))
    plans = {entry['question']: entry['plan'] for entry in questions}
    provider_calls = 0

    def provider(request):
        nonlocal provider_calls
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] != 'cmdb_query_plan':
            raise RuntimeError('Known sample layouts unexpectedly required AI extraction')
        question = json.loads(body['messages'][1]['content'])['question']
        if question not in plans:
            raise RuntimeError('Offline demo only supports its explicitly listed questions')
        provider_calls += 1
        content = json.dumps(dict(result=dict(status='planned', plan=plans[question])))
        return httpx.Response(200, json={'choices': [dict(finish_reason='stop', message=dict(content=content))]})

    def checked(response):
        if response.status_code != 200:
            raise RuntimeError(f'{response.request.url.path} returned HTTP {response.status_code}; '
                               'check source configuration and, in live mode, provider connectivity/credentials')
        return response.json()

    # Ignore user DB/source settings; live mode uses only the configured model/key
    # settings. Offline mode does not use credentials or access token.txt.
    settings = Settings.from_env() if live else Settings(openrouter_api_key='offline-demo-placeholder')
    with TemporaryDirectory(prefix='cmdb-demo-') as directory:
        settings = settings.model_copy(update=dict(organization_id=sources[0].organization_id,
            database_url=f'sqlite:///{Path(directory) / "demo.db"}'))
        engine = create_sqlite_engine(settings)
        try:
            ai = OpenRouterClient(settings, transport=None if live else httpx.MockTransport(provider))
            app = create_app(settings=settings, sources=sources, engine=engine, extractor=Extractor(ai),
                             planner=QueryPlanner(ai, aliases=Policy(sources).aliases))
            with TestClient(app, raise_server_exceptions=False) as client:
                runs = []
                for entry in manifest:
                    content = (ROOT / 'input_data' / entry['file']).read_bytes()
                    result = checked(client.post('/ingest', data={'source_id': entry['source']['source_id']},
                                                  files={'file': (entry['file'], content)}))
                    runs.append(dict(file=entry['file'], response=result))
                inventory = {kind: checked(client.get(path)) for kind, path in [
                    ('devices', '/devices'), ('users', '/users'), ('applications', '/apps')]}
                first = manifest[0]
                replay = checked(client.post('/ingest/raw', params={'source_id': first['source']['source_id']},
                    content=(ROOT / 'input_data' / first['file']).read_bytes(), headers={'Content-Type': 'text/plain'}))
                if replay['run_id'] != runs[0]['response']['run_id'] or not replay['replayed']:
                    raise RuntimeError('Cross-transport replay check failed')
                answers = []
                for entry in questions:
                    answer = checked(client.post('/ask', json=dict(question=entry['question'], limit=100)))
                    if answer['status'] != 'answered' or answer['total'] != entry['expected_total']:
                        raise RuntimeError('A demo answer did not match the expected sample evidence; '
                                           'in live mode, inspect the plan using /ask/plan')
                    answers.append(dict(question=entry['question'], response=answer))
                ci_id = inventory['devices']['data'][0]['id']
                detail = checked(client.get('/ci/' + ci_id))
                return dict(mode='live_openrouter' if live else 'mocked_query_plans',
                    database='temporary; removed when the demo exits',
                    note='Real API, parsers, identity, reconciliation and query execution. '
                         + ('Live model requests; semantic expectations are checked.' if live else
                            'Prewritten plans from examples/demo_questions.json; this does not evaluate model quality.'),
                    inventory_totals={kind: page['total'] for kind, page in inventory.items()},
                    ingestion=runs, replay=replay, ci_detail=detail, questions=answers,
                    mocked_provider_calls=provider_calls if not live else None)
        finally:
            engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a temporary sample CMDB through the HTTP API.')
    parser.add_argument('--live', action='store_true', help='Use configured OpenRouter credentials for seven questions; '
                        'may incur charges and validation retries. Default uses explicit mock plans with no network.')
    parser.add_argument('--json', action='store_true', help='Print full API responses as JSON instead of a short summary.')
    args = parser.parse_args(argv)
    try:
        output = demo(live=args.live)
    except (OSError, ValueError, RuntimeError):
        print(json.dumps(dict(status='failed', message='Demo failed. Verify the bundled samples/configuration; '
              'live mode also requires valid OpenRouter settings and connectivity. No application database was changed.')),
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print('CMDB demo — ' + ('live OpenRouter' if args.live else 'offline, using prewritten query plans'))
        print(f"Loaded {len(output['ingestion'])} sample files. " + ', '.join(
            f'{total} {kind}' for kind, total in output['inventory_totals'].items()) + '.')
        print('Duplicate upload check passed. Item details and provenance retrieved.')
        print()
        for entry in output['questions']:
            response = entry['response']
            print(entry['question'])
            rows = response['data']
            if rows is None:
                noun = 'device' if response['total'] == 1 else 'devices'
                print(f"  {response['total']} confirmed active {noun}.")
            elif rows:
                labels = []
                for row in sorted(rows, key=lambda item: item['display_name'].casefold()):
                    label = row['display_name']
                    email = row['attributes'].get('email')
                    labels.append(f'{label} ({email})' if email else label)
                print(textwrap.fill(f"{response['total']} matches: " + ', '.join(labels),
                                    width=100, initial_indent='  ', subsequent_indent='    '))
            else:
                print('  No matches in the recorded data.')
            notes = []
            for warning in response.get('warnings', []):
                code = warning['code']
                field = warning.get('field') or 'required information'
                count = warning.get('count')
                if code == 'unknown_field':
                    note = (f'{count} users have unknown MFA status and are excluded.'
                            if field == 'mfa_enabled' else
                            f'{count} items have unknown {field} and are excluded.')
                elif code == 'conflicting_field':
                    note = f'Sources disagree on {field} for {count} items; those values are treated as unknown.'
                elif code == 'unresolved_relationships':
                    note = 'Some device owners could not be reliably linked to user records with MFA data.'
                elif code == 'reference_items':
                    note = 'Some applications are recorded by reference only, with incomplete details.'
                elif code == 'no_recorded_relationships':
                    note = 'No dependency relationships are recorded; integrations do not count as dependencies.'
                elif code in {'recorded_evidence_only', 'no_recorded_matches'}:
                    continue
                else:
                    note = warning['message']
                if note not in notes:
                    notes.append(note)
            for note in notes:
                print('  ' + note)
        print()
        print('Results reflect recorded evidence; missing data does not prove real-world absence.')
        print('Temporary database removed. Use --json for full API responses and evidence notes.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
