import pytest
from sqlalchemy import func, select

from app.db.models import ConfigurationItem, EntityIdentifier
from app.identity.aliases import Aliases
from app.identity.namespaces import IdentifierScope
from app.identity.resolver import IdentityInput, IdentityResolver
from app.schemas.ingest import SourceConfig
from app.schemas.normalized import Identifier


def identifier(value, *, kind="device", authority="inventory", type="device_id", org="org-a"):
    return Identifier(scope=IdentifierScope(organization_id=org, ci_type=kind,
                      authority_id=authority, identifier_type=type), value=value)


def item(token, *keys, kind="device", org="org-a", reference=False):
    return IdentityInput(token, kind, org, list(keys), reference)


def test_multiple_keys_duplicates_and_transitive_batch_coalesce(db):
    a = identifier("A")
    b = identifier("B", authority="serials", type="serial")
    c = identifier("user@org.test", authority="email", type="email")
    resolver = IdentityResolver(db)
    result = resolver.resolve_many([item("a", a), item("b", b), item("bridge", a, b, c)])
    assert len({outcome.ci.id for outcome in result.values()}) == 1
    db.commit()
    assert db.scalar(select(func.count()).select_from(ConfigurationItem)) == 1
    assert db.scalar(select(func.count()).select_from(EntityIdentifier)) == 3


def test_existing_identifier_conflict_is_not_a_merge_or_update(db):
    resolver = IdentityResolver(db)
    first = resolver.resolve_many([item("a", identifier("A")), item("b", identifier("B", type="serial"))])
    db.flush()
    result = resolver.resolve_many([item("bridge", identifier("A"), identifier("B", type="serial"))])["bridge"]
    assert result.status == "identity_conflict"
    assert set(result.candidate_ids) == {r.ci.id for r in first.values()}
    assert db.scalar(select(func.count()).select_from(ConfigurationItem)) == 2
    assert len(list(db.new)) == 0


def test_conflicting_local_ids_cannot_attach_to_one_name_alias(db):
    resolver = IdentityResolver(db)
    name = identifier("widget", kind="application", type="app_name")
    resolver.resolve_many([item("a", name, identifier("APP1", kind="application", type="app_id"), kind="application")])
    db.flush()
    result = resolver.resolve_many([item("b", name, identifier("APP2", kind="application", type="app_id"), kind="application")])
    assert result["b"].status == "identity_conflict"


def test_source_scope_and_keyless_names(db):
    resolver = IdentityResolver(db)
    result = resolver.resolve_many([item("a", identifier("42")),
                                   item("b", identifier("42", authority="other")), item("name-only")])
    assert result["a"].ci.id != result["b"].ci.id
    assert result["name-only"].status == "unresolved"


def test_aliases_configurable_and_target_reference_enriches_existing_ci(db):
    config = SourceConfig(organization_id="org-a", source_id="apps", adapter="applications_json",
                          identifier_authorities={"app_name": "catalog"}, application_aliases={"Widget": "Widget Suite"})
    resolver = IdentityResolver(db, Aliases([config]))
    alias = identifier("widget", kind="application", authority="catalog", type="app_name")
    original = resolver.resolve_many([item("reference", alias, kind="application", reference=True)])["reference"].ci
    db.flush()
    canonical = identifier("widget suite", kind="application", authority="catalog", type="app_name")
    enriched = resolver.resolve_many([item("inventory", canonical, identifier("APP1", kind="application", type="app_id"), kind="application")])
    assert enriched["inventory"].ci.id == original.id
    assert len(original.identifiers) == 2


def test_alias_cycles_rejected():
    with pytest.raises(ValueError, match="cycle"):
        Aliases([SourceConfig(organization_id="org-a", source_id="apps", adapter="applications_json",
                              application_aliases={"a": "b", "b": "a"})])


def test_new_alias_cannot_hide_two_preexisting_distinct_cis(db):
    old = IdentityResolver(db)
    alias = identifier("widget", kind="application", authority="catalog", type="app_name")
    canonical = identifier("widget suite", kind="application", authority="catalog", type="app_name")
    previous = old.resolve_many([item("a", alias, kind="application"), item("b", canonical, kind="application")])
    db.flush()
    cfg = SourceConfig(organization_id="org-a", source_id="apps", adapter="applications_json",
                       identifier_authorities={"app_name": "catalog"},
                       application_aliases={"Widget": "Widget Suite"})
    resolved = IdentityResolver(db, Aliases([cfg])).resolve_many([item("incoming", alias, kind="application")])["incoming"]
    assert resolved.status == "identity_conflict"
    assert set(resolved.candidate_ids) == {r.ci.id for r in previous.values()}
