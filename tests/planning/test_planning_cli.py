import json

from app.ai.client import AIError
from app.querying import __main__ as cli


class Model:
    def complete(self, **kwargs):
        return json.dumps({"result": {"status": "planned", "plan": {
            "root": "device", "operation": "count", "where": []}}})


def test_cli_preview(monkeypatch, capsys):
    monkeypatch.setattr(cli, "OpenRouterClient", lambda settings: Model())
    assert cli.main(["--question", "How many devices?"]) == 0
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
    assert cli.main(["--question", "Count devices"]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "failed"
