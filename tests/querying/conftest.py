import pytest

from app.config import Settings
from app.db.session import create_sqlite_engine, initialize_database, session_factory
from app.querying.execution import QueryExecutor
from app.reconciliation.policy import Policy
from app.schemas.ingest import SourceConfig
from .helpers import ci, link


@pytest.fixture
def engine():
    result = create_sqlite_engine(Settings(database_url="sqlite:///:memory:"))
    initialize_database(result)
    yield result
    result.dispose()


@pytest.fixture
def db(engine):
    with session_factory(engine)() as session:
        yield session
        session.rollback()


@pytest.fixture
def policy():
    return Policy([SourceConfig(organization_id="org-a", source_id="apps", adapter="applications_json",
        identifier_authorities={"app_name": "catalog"},
        application_aliases={"GitHub": "GitHub Enterprise", "Salesforce": "Salesforce CRM"})])


@pytest.fixture
def execute(db, policy):
    return QueryExecutor(db, "org-a", policy).execute


@pytest.fixture
def graph(db):
    a = ci(db, "user", "Alex", name="Alex", mfa_enabled=False, status="active", email="alex@example.test")
    b = ci(db, "user", "Blair", name="Blair", mfa_enabled=False, status="suspended")
    c = ci(db, "user", "Casey", name="Casey", mfa_enabled=True)
    u = ci(db, "user", "Dana", name="Dana", mfa_enabled=None)
    gh = ci(db, "application", "GitHub Enterprise", name="GitHub Enterprise",
            authority="catalog", identifier_type="app_name", value="github enterprise")
    sf = ci(db, "application", "Salesforce CRM", name="Salesforce CRM",
            authority="catalog", identifier_type="app_name", value="salesforce crm")
    chat = ci(db, "application", "Chat", name="Chat")
    dep = ci(db, "application", "Dependent", name="Dependent")
    integration = ci(db, "application", "Integration only", name="Integration only")
    ds = [ci(db, "device", f"D{i}", hostname=f"host-{i}", os_family=os,
             status=status, encrypted=encrypted) for i, os, status, encrypted in [
                 (1, "macos", "active", False), (2, "windows", "active", True),
                 (3, "macos", "inactive", None), (4, "linux", None, False)]]
    for device, user in zip(ds, [a, b, c, u]):
        link(db, device, user, "assigned_to")
    for user, app in [(a, gh), (a, chat), (b, chat), (c, gh), (u, gh)]:
        link(db, user, app, "uses")
    link(db, dep, sf, "depends_on")
    link(db, integration, sf, "integrates_with")
    link(db, chat, dep, "depends_on")
    link(db, chat, a, "owned_by")
    db.commit()
    return dict(a=a, b=b, c=c, u=u, gh=gh, sf=sf, chat=chat, dep=dep, integration=integration, ds=ds)
