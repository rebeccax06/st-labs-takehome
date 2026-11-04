"""Compile the bounded AST using fixed SQLAlchemy tables and bound values.

Core aliases avoid inheritance-join ambiguity, particularly for self-kind edges.
No model-provided string is ever interpreted as SQL or an attribute path.
"""

from sqlalchemy import and_, literal, select

from app.db.models import ConfigurationItem, Device, User, Application, Relationship
from app.querying.registry import FIELDS
from app.schemas.query_plan import Equal, InValues, IsNull

TABLES = {"device": Device.__table__, "user": User.__table__, "application": Application.__table__}


class Compiler:
    def __init__(self, organization_id, resolved):
        self.organization_id = organization_id
        self.resolved = resolved

    def node(self, kind):
        ci, attrs = ConfigurationItem.__table__.alias(), TABLES[kind].alias()
        return ci, attrs, ci.join(attrs, attrs.c.ci_id == ci.c.id)

    def scope(self, ci, kind):
        return [ci.c.organization_id == self.organization_id, ci.c.kind == kind]

    def column(self, ci, attrs, field):
        return ci.c.id if field == "ci_id" else attrs.c[field]

    def predicates(self, ci, attrs, kind, where):
        terms = []
        for predicate in where:
            column = self.column(ci, attrs, predicate.field)
            if isinstance(predicate, IsNull):
                terms.append(column.is_(None))
            elif FIELDS[kind][predicate.field].reference:
                values = (predicate.value,) if isinstance(predicate, Equal) else predicate.value
                ids = [self.resolved[(kind, predicate.field, v)] for v in values]
                terms.append(ci.c.id.in_([v for v in ids if v is not None]))
            elif isinstance(predicate, Equal):
                terms.append(column == predicate.value)
            elif isinstance(predicate, InValues):
                terms.append(column.in_(predicate.value))
            else:
                raise ValueError("Unsupported predicate")
        return terms

    def edge(self, parent, target, edge, relation):
        source, destination = (parent, target) if edge.direction == "out" else (target, parent)
        return and_(relation.c.from_ci_id == source.c.id, relation.c.to_ci_id == destination.c.id,
                    relation.c.relation_type == edge.relation)

    def constraints(self, ci, attrs, kind, where, exists):
        terms = self.scope(ci, kind) + self.predicates(ci, attrs, kind, where)
        for edge in exists:
            target, attributes, joined = self.node(edge.target_kind)
            relation = Relationship.__table__.alias()
            subquery = select(literal(1)).select_from(joined.join(relation, relation.c.to_ci_id == target.c.id
                if edge.direction == "out" else relation.c.from_ci_id == target.c.id)).where(
                    self.edge(ci, target, edge, relation),
                    *self.constraints(target, attributes, edge.target_kind, edge.where, edge.exists)).correlate(ci)
            terms.append(subquery.exists())
        return terms

    def roots(self, plan):
        ci, attrs, joined = self.node(plan.root)
        return select(ci.c.id).select_from(joined).where(
            *self.constraints(ci, attrs, plan.root, plan.where, plan.exists))

    def reachable(self, plan, path):
        """Coverage candidates: follow path, applying ancestors' scalar filters.

        Deliberately omit each node's sibling/descendant EXISTS so missing evidence
        isn't hidden by the very constraint whose coverage we are checking.
        """
        ci, attrs, joined = self.node(plan.root)
        kind, where, exists = plan.root, plan.where, plan.exists
        terms = self.scope(ci, kind)
        for index in path:
            terms += self.predicates(ci, attrs, kind, where)
            edge = exists[index]
            target, attributes, target_join = self.node(edge.target_kind)
            relation = Relationship.__table__.alias()
            joined = joined.join(relation, relation.c.from_ci_id == ci.c.id if edge.direction == "out"
                                 else relation.c.to_ci_id == ci.c.id).join(target_join, self.edge(ci, target, edge, relation))
            ci, attrs = target, attributes
            kind, where, exists = edge.target_kind, edge.where, edge.exists
            terms += self.scope(ci, kind)
        return select(ci.c.id).select_from(joined).where(*terms).distinct(), ci, attrs
