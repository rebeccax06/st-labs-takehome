"""The reviewer demo must exercise real services while staying isolated/offline."""

import json

import httpx

from app import demo


def test_demo_is_offline_uses_no_credentials_and_cleans_database(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('The default demo must not use real HTTP or credentials')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    original_key = demo.OpenRouterClient._key
    def offline_key(client):
        assert client.settings.openrouter_api_key.get_secret_value() == 'offline-demo-placeholder'
        return original_key(client)
    monkeypatch.setattr(demo.OpenRouterClient, '_key', offline_key)
    temporary_directories = []
    original_directory = demo.TemporaryDirectory
    def tracked_directory(**kwargs):
        directory = original_directory(**kwargs)
        temporary_directories.append(directory.name)
        return directory
    monkeypatch.setattr(demo, 'TemporaryDirectory', tracked_directory)
    monkeypatch.setenv('CMDB_DATABASE_URL', f'sqlite:///{tmp_path / "user.db"}')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'DO-NOT-USE-THIS-KEY')
    output = demo.demo()
    assert output['mode'] == 'mocked_query_plans'
    assert output['inventory_totals'] == dict(devices=3, users=12, applications=28)
    assert output['mocked_provider_calls'] == 7
    assert output['replay']['replayed'] is True
    assert [q['response']['total'] for q in output['questions']] == [3, 2, 5, 0, 0, 0, 1]
    assert not (tmp_path / 'user.db').exists()
    assert 'DO-NOT-USE-THIS-KEY' not in json.dumps(output)
    from pathlib import Path
    assert temporary_directories and all(not Path(path).exists() for path in temporary_directories)


def test_demo_failure_has_nonzero_exit_and_no_traceback(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError('PRIVATE credential/provider body')
    monkeypatch.setattr(demo, 'demo', fail)
    assert demo.main([]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)['status'] == 'failed'
    assert 'PRIVATE' not in output.err


def test_demo_cli_summary_and_full_json(capsys):
    assert demo.main([]) == 0
    summary = capsys.readouterr().out
    assert 'Loaded 5 sample files.' in summary
    assert "Which users don't have MFA?" in summary
    assert '--json' in summary
    assert len(summary.splitlines()) < 45
    assert 'Alex Johnson (' in summary
    assert 'unknown_field' not in summary
    assert 'Showing 3' not in summary
    assert 'No dependency relationships are recorded' in summary
    assert demo.main(['--json']) == 0
    full = json.loads(capsys.readouterr().out)
    assert full['inventory_totals']['devices'] == 3
    assert len(full['questions']) == 7
    assert len(full['questions'][2]['response']['data']) == 5
