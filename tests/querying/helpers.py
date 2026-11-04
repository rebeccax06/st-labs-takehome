import json

from app.db.models import Application, Device, EntityIdentifier, Relationship, User, new_id
from app.identity.namespaces import IdentifierScope
from app.schemas.query_plan import QueryPlan

MODELS = {"device": Device, "user": User, "application": Application}


def ci(session, kind, label, org="org-a", identifier_type="external_id", authority="inventory", value=None, **attrs):
    item = MODELS[kind](id=new_id(), organization_id=org, display_name=label, **attrs)
    item.identifiers.append(EntityIdentifier(namespace=IdentifierScope(organization_id=org, ci_type=kind,
        authority_id=authority, identifier_type=identifier_type).to_namespace(), value=value or item.id))
    session.add(item)
    return item


def link(session, source, target, relation):
    session.add(Relationship(from_ci=source, to_ci=target, relation_type=relation))


def eq(field, value):
    return dict(field=field, operator="eq", value=value)


def edge(relation, kind, where=None, exists=None, direction="out"):
    return dict(relation=relation, target_kind=kind, direction=direction, where=where or [], exists=exists or [])


def plan(root="user", where=None, exists=None, operation="list", **kwargs):
    return QueryPlan.model_validate_json(json.dumps(dict(root=root, operation=operation,
        where=where or [], exists=exists or []) | kwargs))
