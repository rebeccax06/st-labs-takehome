from uuid import uuid4

import pytest

from app.config import Settings
from app.db.models import Application, Device, EntityIdentifier, User
from app.db.session import create_sqlite_engine, initialize_database, session_factory
from app.identity.namespaces import IdentifierScope


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


def ci(kind="device", *, organization="org-a", value=None, **attributes):
    models = {"device": Device, "user": User, "application": Application}
    item = models[kind](organization_id=organization, display_name="Example", **attributes)
    item.identifiers.append(EntityIdentifier(
        namespace=IdentifierScope(
            organization_id=organization, ci_type=kind,
            authority_id="test-inventory", identifier_type="external_id",
        ).to_namespace(),
        value=value or str(uuid4()),
    ))
    return item
