"""Resolve, compile and execute a validated plan in one read transaction."""

from sqlalchemy import func, select

from app.api.inventory_service import InventoryService
from app.db.models import ConfigurationItem
from app.querying.compiler import Compiler
from app.querying.coverage import coverage_warnings
from app.querying.resolution import MAX_REFERENCES, ReferenceResolver, reference_views, references
from app.schemas.ask import Answered, AskClarification, QueryWarning
from app.schemas.query_plan import Planned, QueryPlan, Unsupported


class QueryExecutor:
    def __init__(self, session, organization_id, policy):
        self.session = session
        self.organization_id = organization_id
        self.policy = policy

    def execute(self, plan: QueryPlan):
        # Revalidate at the execution boundary even for internal callers. Values
        # copied/constructed without Pydantic validation cannot reach the compiler.
        plan = QueryPlan.model_validate_json(plan.model_dump_json(warnings=False))
        keys = references(plan)
        if len(keys) > MAX_REFERENCES:
            return Unsupported(status="unsupported", reason="A query may reference at most 100 distinct entity values. Narrow the question.")
        with self.session.no_autoflush:
            resolved, warnings = ReferenceResolver(self.session, self.organization_id, self.policy).resolve(keys)
            if isinstance(resolved, AskClarification):
                return resolved
            compiler = Compiler(self.organization_id, resolved)
            roots = compiler.roots(plan)
            total = self.session.scalar(select(func.count()).select_from(roots.subquery()))
            data = None
            if plan.operation == "list":
                ids = list(self.session.scalars(roots.order_by(roots.selected_columns.id).limit(plan.limit).offset(plan.offset)))
                items = {ci.id: ci for ci in self.session.scalars(select(ConfigurationItem).where(
                    ConfigurationItem.organization_id == self.organization_id, ConfigurationItem.id.in_(ids)))} if ids else {}
                inventory = InventoryService(self.session, self.organization_id)
                data = [inventory.view(items[ci_id]) for ci_id in ids]
            warnings.extend(coverage_warnings(self.session, compiler, plan))
            if total == 0:
                warnings.append(QueryWarning(code="no_recorded_matches", count=0,
                    message="No matching recorded evidence. This does not prove real-world absence."))
            noun = {"device": "devices", "user": "users", "application": "applications"}[plan.root]
            if total == 1:
                noun = {"device": "device", "user": "user", "application": "application"}[plan.root]
            verb = "matches" if total == 1 else "match"
            answer = f"{total} recorded {noun} {verb} the validated filters."
            if data is not None:
                answer += f" Showing {len(data)} at offset {plan.offset}."
            return Answered(answer=answer, plan=plan, resolved_references=reference_views(resolved),
                data=data, total=total, limit=plan.limit, offset=plan.offset, warnings=warnings)


class AskService:
    def __init__(self, planner, sessions, organization_id, policy):
        self.planner, self.sessions = planner, sessions
        self.organization_id, self.policy = organization_id, policy

    def ask(self, request):
        # Do not hold a DB transaction during a network/model request.
        decision = self.planner.plan(request).result
        if not isinstance(decision, Planned):
            if isinstance(decision, Unsupported):
                return decision
            return AskClarification(question=decision.question)
        with self.sessions() as session, session.begin():
            return QueryExecutor(session, self.organization_id, self.policy).execute(decision.plan)
