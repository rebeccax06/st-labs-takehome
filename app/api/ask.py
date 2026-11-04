"""Natural-language questions answered from deterministic database queries."""

from fastapi import APIRouter, Request

from app.querying.execution import AskService
from app.schemas.ask import AskResponse
from app.schemas.query_plan import AskPlanRequest

router = APIRouter(tags=["Questions"])


@router.post("/ask", response_model=AskResponse,
             summary="Ask a question about recorded inventory",
             description="Plan with AI, resolve exact references, then execute a bounded read-only query. "
                         "Answers include the plan, database results, and evidence limitations.")
def ask(payload: AskPlanRequest, request: Request):
    state = request.app.state
    return AskService(state.planner, state.sessions, state.settings.organization_id, state.policy).ask(payload)
