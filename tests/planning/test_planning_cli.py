import json

from app.ai.client import AIError
from app.querying import __main__ as cli


class Model:
    def complete(self, **kwargs):
        return json.dumps({"result": {"status": "planned", "plan": {
            "root": "device", "operation": "count", "where": []}}})


def test_cli_preview(monkeypatch, capsys):
    monkeypatch.setattr(cli, "OpenRouterClient", lambda settings: Model())
    assert cli.main(["--plan-only", "--question", "How many devices?"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["executed"] is False
    assert result["result"]["plan"]["operation"] == "count"


def test_cli_validation_precedes_provider(monkeypatch, capsys):
    def forbidden(settings):
        raise AssertionError("Provider must not be created")
    monkeypatch.setattr(cli, "OpenRouterClient", forbidden)
    assert cli.main(["--question", " "]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "invalid_request"


def test_cli_provider_failure(monkeypatch, capsys):
    class Failed:
        def complete(self, **kwargs):
            raise AIError("Provider unavailable", status_code=503)
    monkeypatch.setattr(cli, "OpenRouterClient", lambda settings: Failed())
    assert cli.main(["--plan-only", "--question", "Count devices"]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "failed"


def test_cli_executes_against_configured_database(monkeypatch, tmp_path, capsys):
    from app.config import Settings
    from app.db.session import create_sqlite_engine, initialize_database, session_factory
    from tests.querying.helpers import ci

    url = f"sqlite:///{tmp_path / 'inventory.db'}"
    engine = create_sqlite_engine(Settings(database_url=url))
    initialize_database(engine)
    with session_factory(engine)() as session:
        ci(session, 'user', 'Alex', org='example-corp', name='Alex', mfa_enabled=False)
        ci(session, 'user', 'Blair', org='example-corp', name='Blair', mfa_enabled=True)
        session.commit()
    engine.dispose()
    class Users:
        def complete(self, **kwargs):
            return json.dumps({'result': {'status': 'planned', 'plan': {
                'root': 'user', 'operation': 'list', 'where': [
                    {'field': 'mfa_enabled', 'operator': 'eq', 'value': False}]}}})
    monkeypatch.setenv('CMDB_DATABASE_URL', url)
    monkeypatch.setattr(cli, 'OpenRouterClient', lambda settings: Users())
    assert cli.main(['--question', "Which users don't have MFA?"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'answered'
    assert result['total'] == 1
    assert result['data'][0]['display_name'] == 'Alex'
