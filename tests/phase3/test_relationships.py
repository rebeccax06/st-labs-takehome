from app.db.models import ConfigurationItem
from app.identity.namespaces import IdentifierScope
from app.identity.resolver import IdentityInput, IdentityResolver
from app.ingestion.relationships import resolve_targets
from app.schemas.normalized import DeviceRecord, Identifier, UserRecord


def identifier(kind, value):
    return Identifier(scope=IdentifierScope(organization_id="org-a", ci_type=kind,
                      authority_id="inventory", identifier_type="external_id"), value=value)


def test_forward_targets_use_existing_root_identity(db):
    user_key = identifier("user", "U1")
    records = [DeviceRecord(organization_id="org-a", record_key="/0", identifiers=[identifier("device", "D1")],
                           attributes={}, relationships=[{"relation_type": "assigned_to", "target": {
                               "ci_type": "user", "identifiers": [user_key]}}]),
               UserRecord(organization_id="org-a", record_key="/1", identifiers=[user_key], attributes={})]
    resolver = IdentityResolver(db)
    roots = resolver.resolve_many([IdentityInput(f"r:{i}", row.ci_type, row.organization_id, row.identifiers)
                                   for i, row in enumerate(records)])
    db.flush()
    edges = resolve_targets(resolver, records, roots)
    assert edges[0].resolution.ci.id == roots["r:1"].ci.id
    assert len(resolver.created) == 2


def test_missing_stable_target_is_reference_but_name_only_is_not_a_user(db):
    record = DeviceRecord(organization_id="org-a", record_key="/0", identifiers=[identifier("device", "D1")],
                          attributes={}, relationships=[
                              {"relation_type": "assigned_to", "target": {"ci_type": "user", "display_name": "A. Person"}},
                              {"relation_type": "depends_on", "target": {"ci_type": "application", "identifiers": [identifier("application", "APP1")]}}
                          ])
    resolver = IdentityResolver(db)
    roots = resolver.resolve_many([IdentityInput("r:0", record.ci_type, record.organization_id, record.identifiers)])
    db.flush()
    edges = resolve_targets(resolver, [record], roots)
    assert edges[0].resolution.status == "unresolved"
    assert edges[1].resolution.ci.is_reference is True
    assert len(resolver.created) == 2
