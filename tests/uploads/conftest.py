import pytest

from app.config import Settings
from app.db.session import create_sqlite_engine, initialize_database


@pytest.fixture
def engine():
    engine = create_sqlite_engine(Settings(database_url="sqlite:///:memory:"))
    initialize_database(engine)
    yield engine
    engine.dispose()


def provider_output(value):
    """Migrate compact legacy fixtures to the provider's typed fact objects."""
    from copy import deepcopy
    value = deepcopy(value)
    for row in value.get("records", []):
        evidence = row.pop("attribute_evidence", {})
        row["attributes"] = {name: {"value": fact, "evidence": evidence.get(name, {})}
                             for name, fact in row.get("attributes", {}).items()}
        row.setdefault("identifiers", [])
        row.setdefault("relationships", [])
    return value
