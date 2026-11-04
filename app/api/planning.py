"""Planning preview only; POST /ask executes plans for grounded answers."""

from fastapi import APIRouter, Request

from app.querying.planner import PLANNER_VERSION
from app.schemas.query_plan import AskPlanRequest, PlanPreview

router = APIRouter(tags=["Query planning"])


@router.post("/ask/plan", response_model=PlanPreview, summary="Preview an AI query plan (no execution)",
             description="Translate a question into a validated read-only plan. This endpoint does not query "
                         "the CMDB, return rows/counts, or prove that named entities exist. "
                         "Use POST /ask for database name resolution and grounded answers.")
def preview(payload: AskPlanRequest, request: Request):
    response = request.app.state.planner.plan(payload)
    return PlanPreview(result=response.result, planner_version=PLANNER_VERSION)
