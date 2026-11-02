import pytest

from app.config import Settings
from app.db.session import create_sqlite_engine, initialize_database


@pytest.fixture
def engine():
    engine = create_sqlite_engine(Settings(database_url="sqlite:///:memory:"))
    initialize_database(engine)
    yield engine
    engine.dispose()
