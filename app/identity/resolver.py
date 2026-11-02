"""Batch identity resolution using exact scoped keys, with no fuzzy matching."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select, tuple_
from sqlalchemy.orm import Session, selectinload

from app.db.models import Application, ConfigurationItem, Device, EntityIdentifier, User, new_id
from app.identity.aliases import Aliases, key
from app.identity.namespaces import IdentifierScope
from app.schemas.normalized import Identifier

MODELS = {"device": Device, "user": User, "application": Application}
# These authorities issue one stable identity value per CI. Emails/names can be aliases.
SINGLE_VALUE_TYPES = {"device_id", "user_id", "app_id", "external_id", "employee_id", "serial"}


@dataclass
class IdentityInput:
    token: str
    kind: str
    organization_id: str
    identifiers: list[Identifier]
    reference_only: bool = False


@dataclass
class Resolution:
    status: str
    ci: ConfigurationItem | None = None
    candidate_ids: tuple[str, ...] = ()


def fingerprint(ci: ConfigurationItem) -> dict[str, Any]:
    """Semantic CI state; fresh provenance alone does not count as an attribute update."""
    fields = {column.key: getattr(ci, column.key) for column in inspect(ci).mapper.column_attrs
              if column.key not in {"created_at", "updated_at", "field_state", "ci_id"}}
    return {"fields": fields, "identifiers": sorted((i.namespace, i.value) for i in ci.identifiers)}


class IdentityResolver:
    def __init__(self, session: Session, aliases: Aliases | None = None):
        self.session = session
        self.aliases = aliases or Aliases()
        self.created: set[str] = set()
        self.before: dict[str, dict] = {}
        self.touched: dict[str, ConfigurationItem] = {}

    def resolve_many(self, inputs: list[IdentityInput]) -> dict[str, Resolution]:
        """Coalesce connected keys before writes; ambiguous components are quarantined.

        The caller controls the transaction and flush. Call again for relationship
        targets after flushing root CIs; both paths use exactly these identity rules.
        """
        if len({item.token for item in inputs}) != len(inputs):
            raise ValueError("Identity input tokens must be unique")
        normalized = {}
        lookup_keys: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for item in inputs:
            values = {}
            for identifier in item.identifiers:
                canonical = self.aliases.canonical(identifier)
                if (canonical.scope.organization_id, canonical.scope.ci_type) != (item.organization_id, item.kind):
                    raise ValueError("Identity scope does not match entity")
                canonical_key = key(canonical)
                values[canonical_key] = canonical
                lookup_keys.setdefault(canonical_key, set()).update({canonical_key, key(identifier)})
            normalized[item.token] = values

        parent = {item.token: item.token for item in inputs}
        def root(token):
            while parent[token] != token:
                parent[token] = parent[parent[token]]
                token = parent[token]
            return token
        owners = {}
        for token, identifiers in normalized.items():
            for identity_key in identifiers:
                if identity_key in owners:
                    parent[root(token)] = root(owners[identity_key])
                owners[identity_key] = token
        components: dict[str, list[IdentityInput]] = {}
        for item in inputs:
            components.setdefault(root(item.token), []).append(item)

        # Chunk tuple lookups to stay below SQLite parameter limits.
        stored = {}
        keys = sorted({lookup for identity_key in owners for lookup in lookup_keys[identity_key]})
        with self.session.no_autoflush:
            for offset in range(0, len(keys), 400):
                rows = self.session.scalars(select(EntityIdentifier).where(
                    tuple_(EntityIdentifier.namespace, EntityIdentifier.value).in_(keys[offset:offset + 400])
                )).all()
                stored.update({(row.namespace, row.value): row.ci_id for row in rows})
            # Also recognize keys staged by a preceding resolution call in this transaction.
            for obj in self.session.new:
                if isinstance(obj, EntityIdentifier) and obj.ci is not None:
                    stored[(obj.namespace, obj.value)] = obj.ci.id

            outcomes = {}
            for component in components.values():
                identifiers = {k: v for item in component for k, v in normalized[item.token].items()}
                matches = {stored[lookup] for identity_key in identifiers
                           for lookup in lookup_keys[identity_key] if lookup in stored}
                ci = None
                if len(matches) == 1:
                    ci_id = next(iter(matches))
                    ci = self.touched.get(ci_id) or self.session.scalar(
                        select(ConfigurationItem).options(selectinload(ConfigurationItem.identifiers))
                        .where(ConfigurationItem.id == ci_id)
                    )
                all_keys = set(identifiers)
                if ci is not None:
                    all_keys.update((row.namespace, row.value) for row in ci.identifiers)
                slots = {}
                conflict = len(matches) > 1
                for namespace, value in all_keys:
                    scope = IdentifierScope.from_namespace(namespace)
                    if scope.identifier_type in SINGLE_VALUE_TYPES:
                        if namespace in slots and slots[namespace] != value:
                            conflict = True
                        slots[namespace] = value
                if conflict:
                    result = Resolution("identity_conflict", candidate_ids=tuple(sorted(matches)))
                elif not identifiers:
                    result = Resolution("unresolved")
                else:
                    if ci is None:
                        item = component[0]
                        ci = MODELS[item.kind](id=new_id(), organization_id=item.organization_id,
                                               display_name=f"{item.kind}:unobserved",
                                               is_reference=all(i.reference_only for i in component))
                        self.session.add(ci)
                        self.created.add(ci.id)
                    elif ci.id not in self.created:
                        self.before.setdefault(ci.id, fingerprint(ci))
                    existing = {(row.namespace, row.value) for row in ci.identifiers}
                    for namespace, value in sorted(identifiers):
                        if (namespace, value) not in existing:
                            ci.identifiers.append(EntityIdentifier(namespace=namespace, value=value))
                    self.touched[ci.id] = ci
                    result = Resolution("resolved", ci=ci)
                outcomes.update({item.token: result for item in component})
            return outcomes
