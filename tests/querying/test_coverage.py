from app.db.models import IngestionRun, Observation, utc_now
from .helpers import ci, edge, eq, plan


def warning_codes(result):
    return {w.code for w in result.warnings}


def test_unknowns_are_reported_for_reachable_assignees(graph, execute):
    nested = edge('assigned_to', 'user', [eq('mfa_enabled', False)],
                  [edge('uses', 'application', [eq('name', 'GitHub')])])
    result = execute(plan('device', exists=[nested]))
    missing = next(w for w in result.warnings if w.code == 'unknown_field')
    assert missing.path == 'root.exists[0]' and missing.count == 1
    # The unknown user only owns a Linux device; ancestor filters narrow coverage.
    result = execute(plan('device', [eq('os_family', 'macos')], exists=[nested]))
    assert 'unknown_field' not in warning_codes(result)


def test_no_edges_warning_is_typed_and_organization_scoped(execute, db):
    ci(db, 'application', 'Empty catalog', name='Empty catalog')
    db.commit()
    result = execute(plan('application', exists=[edge('depends_on', 'application')]))
    assert {'no_recorded_relationships', 'no_recorded_matches', 'recorded_evidence_only'} <= warning_codes(result)
    # A count answer still includes coverage warnings and no rows.
    result = execute(plan('application', exists=[edge('depends_on', 'application')], operation='count'))
    assert result.data is None and 'no_recorded_relationships' in warning_codes(result)


def test_reference_items_and_conflicting_assignment_are_visible(db, execute):
    user = ci(db, 'user', 'Reference', is_reference=True)
    device = ci(db, 'device', 'Conflicting device', field_state={
        'assigned_to': {'status': 'conflict', 'observation_ids': []}})
    db.commit()
    result = execute(plan())
    assert 'reference_items' in warning_codes(result)
    assert next(w for w in result.warnings if w.code == 'reference_items').count == 1
    result = execute(plan('device', exists=[edge('assigned_to', 'user')]))
    assert result.total == 0 and 'conflicting_assignments' in warning_codes(result)


def test_retained_unresolved_relationship_evidence_is_not_silently_ignored(db, execute, graph):
    run = IngestionRun(organization_id='org-a', source_id='inventory', format='json', payload_sha256='a'*64,
        policy_version='test', received_at=utc_now(), completed_at=utc_now(), result={})
    db.add(run)
    db.add(Observation(run=run, ci=graph['ds'][0], record_key='/unresolved', origin='adapter',
        resolution_status='unresolved', raw_record={}, normalized_record={
            'record_kind': 'relationship', 'relation_type': 'assigned_to', 'target_ci_id': None}))
    db.commit()
    result = execute(plan('device', exists=[edge('assigned_to', 'user', [eq('mfa_enabled', False)])]))
    warning = next(w for w in result.warnings if w.code == 'unresolved_relationships')
    assert warning.count == 1
    # Incoming queries cannot safely identify which target an unresolved record meant.
    result = execute(plan(exists=[edge('assigned_to', 'device', direction='in')]))
    assert 'organization-wide' in next(w.message for w in result.warnings if w.code == 'unresolved_relationships')
