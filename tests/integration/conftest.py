"""File-backed application instances and fail-closed provider transport."""

from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from app.ai.client import OpenRouterClient
from app.ai.extraction import Extractor
from app.config import Settings
from app.db.models import Base
from app.db.session import create_sqlite_engine, session_factory
from app.main import create_app
from app.querying.planner import QueryPlanner
from app.reconciliation.policy import Policy
from app.schemas.ingest import SourceConfig


class Provider:
    """Mock only HTTP, leaving provider envelopes, validation and services real."""
    def __init__(self):
        self.outputs = {}
        self.calls = []

    def handle(self, request):
        body = json.loads(request.content)
        name = body['response_format']['json_schema']['name']
        self.calls.append(deepcopy(body))
        assert request.headers['authorization'] == 'Bearer integration-test-key'
        assert 'integration-test-key' not in request.content.decode()
        if name not in self.outputs:
            raise AssertionError(f'Unexpected provider request: {name}')
        value = self.outputs[name]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        content = value if isinstance(value, str) else json.dumps(value)
        return httpx.Response(200, json={'choices': [dict(finish_reason='stop', message={'content': content})]})

    def planning(self, plan):
        self.outputs['cmdb_query_plan'] = dict(result=dict(status='planned', plan=plan))


@pytest.fixture(autouse=True)
def forbid_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Integration tests must never use real HTTP transport')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)


@pytest.fixture
def backend_factory(tmp_path):
    engines = []
    def build(*, sources=None, name='integration', provider=None, busy_timeout_ms=5):
        organization = sources[0].organization_id if sources else 'org-a'
        settings = Settings(organization_id=organization, database_url=f'sqlite:///{tmp_path / (name + ".db")}',
                            openrouter_api_key='integration-test-key', sqlite_busy_timeout_ms=busy_timeout_ms)
        if sources is None:
            sources = [SourceConfig(organization_id='org-a', source_id='hardware', adapter='hardware_json',
                                    identifier_authorities={'serial': 'serials'}),
                       SourceConfig(organization_id='org-a', source_id='notes', adapter='auto')]
        provider = provider or Provider()
        client = OpenRouterClient(settings, transport=httpx.MockTransport(provider.handle))
        engine = create_sqlite_engine(settings)
        engines.append(engine)
        app = create_app(settings=settings, sources=sources, engine=engine, extractor=Extractor(client),
                         planner=QueryPlanner(client, aliases=Policy(sources).aliases))
        return SimpleNamespace(app=app, engine=engine, provider=provider,
                               http=lambda: TestClient(app, raise_server_exceptions=False),
                               sessions=session_factory(engine))
    yield build
    for engine in engines:
        engine.dispose()


def snapshot(backend):
    """All persisted values, not just row counts; includes runs and evidence."""
    with backend.sessions() as db:
        return {table.name: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                for row in db.execute(select(table)).mappings()) for table in Base.metadata.sorted_tables}


def ingest(client, rows, source='hardware'):
    return client.post('/ingest', files={'file': ('inventory.json', json.dumps(rows), 'application/json')},
                       data={'source_id': source})


def query_plan(root='device', where=None, exists=None, operation='list'):
    return dict(root=root, operation=operation, where=where or [], exists=exists or [])


def eq(field, value):
    return dict(field=field, operator='eq', value=value)


def edge(relation, target_kind, where=None, exists=None):
    return dict(relation=relation, direction='out', target_kind=target_kind, where=where or [], exists=exists or [])
