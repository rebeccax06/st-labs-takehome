"""Preview a natural-language plan without starting a server or database."""

import argparse
import json
import sys

from pydantic import ValidationError

from app.ai.client import AIError, OpenRouterClient
from app.config import Settings
from app.querying.planner import PLANNER_VERSION, QueryPlanner
from app.schemas.query_plan import AskPlanRequest, PlanPreview


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ask OpenRouter for a validated CMDB query plan. No database query is executed.")
    parser.add_argument("--question", required=True, help="Natural-language question (makes a model request).")
    parser.add_argument("--limit", type=int, default=50, help="Result limit for the eventual query, 1–100.")
    parser.add_argument("--offset", type=int, default=0, help="Eventual result offset, 0–10000.")
    args = parser.parse_args(argv)
    try:
        request = AskPlanRequest(question=args.question, limit=args.limit, offset=args.offset)
        response = QueryPlanner(OpenRouterClient(Settings.from_env())).plan(request)
        print(PlanPreview(result=response.result, planner_version=PLANNER_VERSION).model_dump_json(indent=2))
    except ValidationError:
        print(json.dumps(dict(status="failed", code="invalid_request", message="Check question length and pagination limits.")), file=sys.stderr)
        return 1
    except AIError as exc:
        print(json.dumps(dict(status="failed", code=exc.code, message=str(exc))), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
