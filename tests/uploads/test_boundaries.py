"""Hostile scalar values must fail cleanly before any inventory is written."""

import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.db.models import IngestionRun
from app.db.session import session_factory
from app.ingestion.errors import InputError
from app.ingestion.parsing import parse_content
from tests.uploads.test_inputs import application


@pytest.mark.parametrize("format,content", [
    ("json", '{"number":' + '9' * 5000 + '}'),
    ("yaml", 'number: ' + '9' * 5000),
])
def test_oversized_integer_is_an_input_error(format, content):
    with pytest.raises(InputError):
        parse_content(content, format)


def test_csv_obeys_the_document_node_budget():
    content = ','.join(f'field{i}' for i in range(101)) + '\n' + (',' * 100 + '\n') * 1000
    with pytest.raises(InputError) as error:
        parse_content(content, 'csv')
    assert error.value.code == 'limit_exceeded'


CASES = [
    ("hardware", "json", '[{"device_id":' + '9' * 5000 + '}]'),
    ("yaml", "yaml", 'inventory: {devices: [{device_id: ' + '9' * 5000 + '}]}'),
] + [
    ("hardware", "json", json.dumps([{"device_id": "D1", "last_checkin": value}]))
    for value in ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
]


@pytest.mark.parametrize("transport", ["file", "raw", "envelope"])
@pytest.mark.parametrize("source,format,content", CASES)
def test_bad_scalars_are_sanitized_and_leave_no_runs(engine, transport, source, format, content):
    with TestClient(application(engine)) as client:
        if transport == "file":
            response = client.post("/ingest", files={"file": ("input." + format, content)},
                                   data={"source_id": source})
        elif transport == "raw":
            response = client.post("/ingest/raw", params={"source_id": source}, content=content,
                                   headers={"Content-Type": "text/plain"})
        else:
            response = client.post("/ingest", json=dict(organization_id="org-a", source_id=source,
                                                        format=format, content=content))
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "invalid_input"
        assert error["request_id"] == response.headers["X-Request-ID"]
        assert content not in response.text
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 0
