"""Ask a question against the configured database without starting a server."""

import argparse
import json
import sys

from pydantic import ValidationError

from app.ai.client import AIError, OpenRouterClient
from app.config import Settings
from app.db.session import create_sqlite_engine, initialize_database, session_factory
from app.querying.execution import AskService
from app.reconciliation.policy import Policy, load_sources
from sqlalchemy.exc import SQLAlchemyError
from app.querying.planner import PLANNER_VERSION, QueryPlanner
from app.schemas.query_plan import AskPlanRequest, PlanPreview


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ask a question about the inventory in the configured CMDB database.")
    parser.add_argument("--question", required=True, help="Natural-language question (makes a model request).")
    parser.add_argument("--limit", type=int, default=50, help="Result limit, 1–100.")
    parser.add_argument("--offset", type=int, default=0, help="Result offset, 0–10000.")
    parser.add_argument("--plan-only", action="store_true", help="Preview the query plan without accessing the database.")
    args = parser.parse_args(argv)
    try:
        request = AskPlanRequest(question=args.question, limit=args.limit, offset=args.offset)
        settings = Settings.from_env()
        policy = Policy(load_sources(settings.source_config_path))
        planner = QueryPlanner(OpenRouterClient(settings), aliases=policy.aliases)
        if args.plan_only:
            response = planner.plan(request)
            result = PlanPreview(result=response.result, planner_version=PLANNER_VERSION)
        else:
            engine = create_sqlite_engine(settings)
            try:
                initialize_database(engine)
                result = AskService(planner, session_factory(engine), settings.organization_id, policy).ask(request)
            finally:
                engine.dispose()
        print(result.model_dump_json(indent=2))
    except ValidationError:
        print(json.dumps(dict(status="failed", code="invalid_request", message="Check question length and pagination limits.")), file=sys.stderr)
        return 1
    except AIError as exc:
        print(json.dumps(dict(status="failed", code=exc.code, message=str(exc))), file=sys.stderr)
        return 1
    except (OSError, ValueError, SQLAlchemyError):
        print(json.dumps(dict(status="failed", code="configuration_or_database_error",
                             message="Check the database and source configuration.")), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
