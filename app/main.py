"""FastAPI application factory with explicit source policy and database lifetime."""

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI

from app.api.errors import install_errors
from app.api.ingest import add_upload_source, create_ingest_router
from app.api.upload_limits import IngestBodyLimit
from app.ai.client import OpenRouterClient
from app.ai.extraction import Extractor
from app.api.inventory import router as inventory_router
from app.api.planning import router as planning_router
from app.api.ask import router as ask_router
from app.querying.planner import QueryPlanner
from app.config import Settings
from app.db.session import create_sqlite_engine, initialize_database, session_factory
from app.ingestion.persistence import IngestService
from app.reconciliation.policy import Policy, load_sources


def create_app(*, settings=None, sources=None, engine=None, extractor=None, planner=None):
    settings = settings or Settings.from_env()
    sources = load_sources(settings.source_config_path) if sources is None else sources
    if any(source.organization_id != settings.organization_id for source in sources):
        raise ValueError("All sources must belong to the configured organization")
    sources, default_upload_source = add_upload_source(sources, settings.organization_id)
    policy = Policy(sources)
    owns_engine = engine is None
    engine = engine or create_sqlite_engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        initialize_database(engine)
        try:
            yield
        finally:
            if owns_engine:
                engine.dispose()

    app = FastAPI(title="AI-first CMDB", version="0.6.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.policy = policy
    app.state.sessions = session_factory(engine)
    client = OpenRouterClient(settings)
    extractor = extractor if extractor is not None else Extractor(client)
    app.state.planner = planner if planner is not None else QueryPlanner(client, aliases=policy.aliases)
    app.state.ingestion = IngestService(app.state.sessions, policy, extractor=extractor)
    app.add_middleware(IngestBodyLimit)

    @app.middleware("http")
    async def correlation_id(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    install_errors(app)
    app.include_router(create_ingest_router(sources, default_upload_source))
    app.include_router(inventory_router)
    app.include_router(planning_router)
    app.include_router(ask_router)
    return app


app = create_app()
