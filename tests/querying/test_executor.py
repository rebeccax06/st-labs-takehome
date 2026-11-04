import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select

from app.db.models import Base
from .helpers import edge, eq, link, plan


def ids(result):
    assert result.status == "answered"
    return {row.id for row in result.data}


def test_required_questions_and_same_person_semantics(graph, execute):
    g = graph
    assert ids(execute(plan(where=[eq("mfa_enabled", False)]))) == {g['a'].id, g['b'].id}
    assert ids(execute(plan("device", [eq("os_family", "macos")]))) == {g['ds'][0].id, g['ds'][2].id}
    uses = edge("uses", "application", [eq("name", "GitHub")])
    assert ids(execute(plan(exists=[uses]))) == {g['a'].id, g['c'].id, g['u'].id}
    assigned = edge("assigned_to", "user", [eq("mfa_enabled", False)])
    assert ids(execute(plan("device", exists=[assigned]))) == {g['ds'][0].id, g['ds'][1].id}
    assigned['exists'] = [uses]
    assert ids(execute(plan("device", exists=[assigned]))) == {g['ds'][0].id}
    result = execute(plan("application", exists=[edge("depends_on", "application", [eq("name", "Salesforce")])]))
    assert ids(result) == {g['dep'].id}  # no integration or transitive inference
    result = execute(plan("device", [eq("status", "active")], operation="count", limit=1, offset=100))
    assert result.total == 2 and result.data is None
    assert result.resolved_references == []


def test_count_no_duplicates_and_pagination(graph, execute):
    uses = edge("uses", "application")
    p = plan(exists=[uses], operation="count")
    assert execute(p).total == 4  # Alex has two matching edges
    first = execute(plan(exists=[uses], limit=1))
    second = execute(plan(exists=[uses], limit=1, offset=1))
    expected = sorted(graph[k].id for k in ['a', 'b', 'c', 'u'])
    assert [first.data[0].id, second.data[0].id] == expected[:2]
    assert first.total == second.total == 4
    beyond = execute(plan(exists=[uses], offset=100))
    assert beyond.total == 4 and beyond.data == []
    assert not any(w.code == 'no_recorded_matches' for w in beyond.warnings)


def test_null_false_true_and_in(graph, execute):
    assert ids(execute(plan(where=[dict(field='mfa_enabled', operator='is_null')]))) == {graph['u'].id}
    assert ids(execute(plan(where=[dict(field='mfa_enabled', operator='in', value=[False, True])]))) == {
        graph['a'].id, graph['b'].id, graph['c'].id}
    assert ids(execute(plan("device", [eq('encrypted', False)]))) == {graph['ds'][0].id, graph['ds'][3].id}
    result = execute(plan(where=[eq('mfa_enabled', False)]))
    warning = next(w for w in result.warnings if w.code == 'unknown_field')
    assert warning.count == 1 and warning.field == 'mfa_enabled'


def test_all_directions_and_self_kind_edges(graph, execute, db):
    g = graph
    assert ids(execute(plan("application", exists=[edge('uses', 'user', [eq('name', 'Alex')], direction='in')]))) == {
        g['gh'].id, g['chat'].id}
    assert ids(execute(plan(exists=[edge('assigned_to', 'device', [eq('hostname', 'host-1')], direction='in')]))) == {g['a'].id}
    assert ids(execute(plan(exists=[edge('owned_by', 'application', [eq('name', 'Chat')], direction='in')]))) == {g['a'].id}
    assert ids(execute(plan('application', exists=[edge('owned_by', 'user', [eq('name', 'Alex')])]))) == {g['chat'].id}
    assert ids(execute(plan('application', exists=[edge('depends_on', 'application', [eq('name', 'Dependent')], direction='in')]))) == {g['sf'].id}
    assert ids(execute(plan('application', exists=[edge('integrates_with', 'application', [eq('name', 'Salesforce')])]))) == {g['integration'].id}
    link(db, g['ds'][0], g['ds'][1], 'depends_on')
    link(db, g['ds'][0], g['dep'], 'depends_on')
    link(db, g['dep'], g['ds'][2], 'depends_on')
    db.commit()
    assert ids(execute(plan('device', exists=[edge('depends_on', 'device')]))) == {g['ds'][0].id}
    assert ids(execute(plan('application', exists=[edge('depends_on', 'device')]))) == {g['dep'].id}
    assert ids(execute(plan('device', exists=[edge('depends_on', 'application')]))) == {g['ds'][0].id}


def test_sibling_constraints_can_bind_different_entities(graph, execute):
    # No single app is both Chat and GitHub, but one user uses both.
    siblings = [edge('uses', 'application', [eq('name', name)]) for name in ['Chat', 'GitHub']]
    assert ids(execute(plan(exists=siblings))) == {graph['a'].id}
    same = [edge('uses', 'application', [eq('name', 'Chat'), eq('name', 'GitHub')])]
    assert ids(execute(plan(exists=same))) == set()


def test_sql_injection_is_a_bound_value_and_execution_is_read_only(graph, execute, db):
    before = {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))
    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        attack = "x' OR 1=1; DROP TABLE users; --"
        assert execute(plan(where=[eq('team', attack)])).total == 0
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)
    assert all(s.lstrip().upper().startswith('SELECT') for s, _ in statements)
    assert any(attack in params for _, params in statements)
    assert not any(attack in sql for sql, _ in statements)
    after = {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
    assert before == after
    assert not db.new and not db.dirty and not db.deleted


def test_revalidates_forged_plan_before_execution(graph, execute):
    forged = plan().model_copy(update={'where': ({'field': 'password', 'operator': 'eq', 'value': 'x'},)})
    with pytest.raises(ValidationError):
        execute(forged)
