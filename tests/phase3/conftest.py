import pytest

from app.config import Settings
from app.db.session import create_sqlite_engine, initialize_database, session_factory


@pytest.fixture
def engine():
    engine = create_sqlite_engine(Settings(database_url="sqlite:///:memory:"))
    initialize_database(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with session_factory(engine)() as session:
        yield session
        session.rollback()
