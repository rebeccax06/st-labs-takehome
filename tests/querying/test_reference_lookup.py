from uuid import uuid4

from app.querying.resolution import MAX_REFERENCES
from .helpers import ci, edge, eq, plan


def test_alias_lookup_and_reference_audit(graph, execute):
    result = execute(plan('application', [eq('name', 'github')]))
    assert result.total == 1 and result.data[0].id == graph['gh'].id
    assert result.plan.where[0].value == 'github'
    assert result.resolved_references[0].ci_id == graph['gh'].id
    assert execute(plan(where=[eq('email', 'ALEX@EXAMPLE.TEST')])).data[0].id == graph['a'].id
    assert execute(plan('device', [eq('hostname', 'HOST-1')])).data[0].id == graph['ds'][0].id


def test_duplicate_names_require_clarification_and_uuid_disambiguates(graph, execute, db):
    other = ci(db, 'user', 'Second Alex', name='Alex', mfa_enabled=True)
    db.commit()
    result = execute(plan(where=[eq('name', 'Alex')]))
    assert result.status == 'clarification_required'
    assert {c.id for c in result.candidates} == {graph['a'].id, other.id}
    assert result.candidate_total == 2
    assert not hasattr(result, 'data')
    assert execute(plan(where=[eq('ci_id', graph['a'].id)])).data[0].id == graph['a'].id
    # Target ambiguity is not hidden by filters, even if the final result would be empty.
    result = execute(plan('application', exists=[edge('owned_by', 'user', [eq('name', 'Alex')])]))
    assert result.status == 'clarification_required'


def test_hostname_is_not_unique(graph, execute, db):
    ci(db, 'device', 'Duplicate hostname', hostname='host-1')
    db.commit()
    assert execute(plan('device', [eq('hostname', 'host-1')])).candidate_total == 2


def test_missing_names_and_ids_are_grounded_empty_results(graph, execute):
    for p in [plan(where=[eq('name', 'Ale')]), plan(where=[eq('email', 'missing@example.test')]),
              plan(where=[eq('ci_id', str(uuid4()))]),
              plan('application', exists=[edge('depends_on', 'application', [eq('name', 'Missing App')])])]:
        result = execute(p)
        assert result.status == 'answered' and result.total == 0
        assert any(w.code == 'reference_not_found' for w in result.warnings)
        assert any(w.code == 'no_recorded_matches' for w in result.warnings)


def test_in_membership_resolves_each_literal_and_preserves_missing_warning(graph, execute):
    result = execute(plan('application', [dict(field='name', operator='in', value=['GitHub', 'Missing'])]))
    assert result.total == 1 and result.data[0].id == graph['gh'].id
    assert len(result.resolved_references) == 2
    assert sum(r.ci_id is None for r in result.resolved_references) == 1


def test_organization_isolation_for_refs_counts_coverage_and_candidates(graph, execute, db):
    foreign = ci(db, 'user', 'FOREIGN SECRET', org='org-b', name='Alex', mfa_enabled=None)
    ci(db, 'application', 'GitHub', org='org-b', name='GitHub')
    db.commit()
    result = execute(plan(where=[eq('name', 'Alex')]))
    assert result.total == 1 and result.data[0].id == graph['a'].id
    assert foreign.id not in result.model_dump_json()
    assert execute(plan(where=[eq('ci_id', foreign.id)])).total == 0
    assert execute(plan(operation='count')).total == 4
    warning = next(w for w in execute(plan(where=[eq('mfa_enabled', False)])).warnings if w.code == 'unknown_field')
    assert warning.count == 1


def test_alias_cannot_jump_authorities_or_hide_a_direct_name_conflict(db, execute, graph):
    # Same canonical label in an unrelated authority does not gain our alias.
    other = ci(db, 'application', 'Unrelated product', name='Different product',
               authority='unrelated', identifier_type='app_name', value='github enterprise')
    db.commit()
    assert execute(plan('application', [eq('name', 'GitHub')])).total == 1
    direct = ci(db, 'application', 'Another GitHub', name='GitHub')
    db.commit()
    result = execute(plan('application', [eq('name', 'GitHub')]))
    assert result.status == 'clarification_required'
    assert {c.id for c in result.candidates} == {graph['gh'].id, direct.id}
    assert other.id not in {c.id for c in result.candidates}


def test_candidates_are_bounded_and_ordered(db, execute):
    expected = [ci(db, 'user', f'Duplicate {i}', name='Duplicate').id for i in range(25)]
    db.commit()
    result = execute(plan(where=[eq('name', 'Duplicate')]))
    assert result.candidate_total == 25
    assert [c.id for c in result.candidates] == sorted(expected)[:20]


def test_execution_reference_budget_does_not_weaken_question(execute):
    clauses = [dict(field='name', operator='in', value=[f'User {i}' for i in range(start, start + 20)])
               for start in range(0, 120, 20)]
    result = execute(plan(where=clauses))
    assert result.status == 'unsupported' and str(MAX_REFERENCES) in result.reason
