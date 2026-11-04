from copy import deepcopy
import json
from uuid import uuid4

from pydantic import ValidationError
import pytest

from app.schemas.query_plan import QueryPlan


def parse(value):
    return QueryPlan.model_validate_json(json.dumps(value))


def user_mfa():
    return dict(operation="list", root="user", where=[dict(field="mfa_enabled", operator="eq", value=False)])


def assigned(where=None, exists=None):
    return dict(relation="assigned_to", direction="out", target_kind="user", where=where or [], exists=exists or [])


def test_valid_plan_is_immutable_and_json_round_trips():
    plan = parse(user_mfa())
    assert plan.where[0].value is False
    assert plan.version == 1
    assert isinstance(plan.where, tuple)
    with pytest.raises(ValidationError):
        plan.operation = "delete"
    assert QueryPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("value", [0, 1, "false", "true", None])
def test_booleans_are_strict_and_unknown_does_not_mean_false(value):
    raw = user_mfa()
    raw["where"][0]["value"] = value
    with pytest.raises(ValidationError):
        parse(raw)
    unknown = parse(dict(operation="list", root="user", where=[dict(field="mfa_enabled", operator="is_null")]))
    assert "value" not in unknown.where[0].model_dump()


@pytest.mark.parametrize("changes", [
    dict(root="devices"), dict(operation="delete"), dict(version=True), dict(version=1.0), dict(version=2),
    dict(sql="DROP TABLE devices"), dict(organization_id="other-org"), dict(select=["*"]), dict(order_by="annual_cost"),
    dict(limit=0), dict(limit=101), dict(limit=True), dict(offset=-1), dict(offset=10001),
    dict(where=[dict(field="status", operator="eq", value="ACTIVE")]),
    dict(where=[dict(field="hostname", operator="eq", value="host")]),
    dict(where=[dict(field="email", operator="contains", value="example")]),
    dict(where=[dict(field="name; DROP TABLE users", operator="eq", value="x")]),
    dict(where=[dict(field="mfa_enabled", operator="is_null", value=None)]),
    dict(where=[dict(field="department", operator="eq", value="Engineering")]),
])
def test_invalid_or_unsafe_vocabulary_is_rejected(changes):
    with pytest.raises(ValidationError):
        parse(user_mfa() | changes)


@pytest.mark.parametrize("field,values", [("status", []), ("status", ["active"] * 21),
                                          ("mfa_enabled", [False, "true"]), ("status", ["active", False])])
def test_membership_limits_and_types(field, values):
    with pytest.raises(ValidationError):
        parse(dict(operation="list", root="user", where=[dict(field=field, operator="in", value=values)]))


def test_membership_unknown_and_uuid_values():
    plan = parse(dict(operation="count", root="user", where=[dict(field="status", operator="in", value=["active", "suspended"])]))
    assert plan.where[0].value == ("active", "suspended")
    uid = str(uuid4())
    plan = parse(dict(operation="list", root="user", where=[dict(field="ci_id", operator="eq", value=uid.upper())]))
    assert plan.where[0].value == uid
    with pytest.raises(ValidationError):
        parse(dict(operation="list", root="user", where=[dict(field="ci_id", operator="eq", value="u_999")]))


def test_two_hop_same_user_constraint_is_preserved():
    uses = dict(relation="uses", direction="out", target_kind="application",
                where=[dict(field="name", operator="eq", value="GitHub")])
    raw = dict(operation="list", root="device", exists=[assigned(user_mfa()["where"], [uses])])
    plan = parse(raw)
    assert len(plan.exists) == 1
    assert plan.exists[0].where[0].value is False
    assert plan.exists[0].exists[0].where[0].value == "GitHub"
    three_hops = deepcopy(raw)
    three_hops["exists"][0]["exists"][0]["exists"] = [dict(
        relation="depends_on", direction="out", target_kind="application")]
    with pytest.raises(ValidationError, match="two relationship hops"):
        parse(three_hops)


@pytest.mark.parametrize("root,edge,valid", [
    ("device", assigned(), True),
    ("user", dict(relation="assigned_to", direction="in", target_kind="device"), True),
    ("application", dict(relation="uses", direction="in", target_kind="user"), True),
    ("application", dict(relation="owned_by", direction="out", target_kind="user"), True),
    ("application", dict(relation="integrates_with", direction="in", target_kind="application"), True),
    ("user", assigned(), False),
    ("device", dict(relation="uses", direction="out", target_kind="application"), False),
    ("user", dict(relation="depends_on", direction="out", target_kind="application"), False),
    ("device", dict(relation="assigned_to", direction="out", target_kind="application"), False),
    ("device", dict(relation="assigned_to", direction="both", target_kind="user"), False),
])
def test_relationship_direction_and_types(root, edge, valid):
    raw = dict(operation="list", root=root, exists=[edge])
    if valid:
        parse(raw)
    else:
        with pytest.raises(ValidationError):
            parse(raw)


def test_fanout_predicate_limits_and_related_field_context():
    for raw in [
        user_mfa() | dict(where=user_mfa()["where"] * 9),
        dict(operation="list", root="device", exists=[assigned()] * 5),
        dict(operation="list", root="device", exists=[assigned([dict(field="hostname", operator="eq", value="host")])]),
    ]:
        with pytest.raises(ValidationError):
            parse(raw)
