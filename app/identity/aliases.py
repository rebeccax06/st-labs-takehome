"""Configured application-name aliases; never infer product equivalence."""

from app.identity.namespaces import IdentifierScope
from app.schemas.normalized import Identifier


def key(identifier: Identifier) -> tuple[str, str]:
    return identifier.scope.to_namespace(), identifier.value


class Aliases:
    def __init__(self, sources=()):
        self.mapping: dict[tuple[str, str], tuple[str, str]] = {}
        for source in sources:
            scope = IdentifierScope(
                organization_id=source.organization_id, ci_type="application",
                authority_id=source.identifier_authorities.get("app_name", source.source_id),
                identifier_type="app_name",
            )
            namespace = scope.to_namespace()
            def normalize(value):
                value = " ".join(value.split())
                return value.casefold() if "app_name" in source.case_insensitive_identifiers else value
            for alias, canonical in source.application_aliases.items():
                origin, target = (namespace, normalize(alias)), (namespace, normalize(canonical))
                if not origin[1] or not target[1]:
                    raise ValueError("Application aliases cannot be blank")
                if origin == target:
                    continue
                if origin in self.mapping and self.mapping[origin] != target:
                    raise ValueError("Conflicting application alias configuration")
                self.mapping[origin] = target
        for origin in self.mapping:
            self.canonical_key(origin)  # Fail at configuration time on cycles.

    def canonical_key(self, origin: tuple[str, str]) -> tuple[str, str]:
        seen = set()
        while origin in self.mapping:
            if origin in seen:
                raise ValueError("Application alias cycle")
            seen.add(origin)
            origin = self.mapping[origin]
        return origin

    def canonical(self, identifier: Identifier) -> Identifier:
        namespace, value = self.canonical_key(key(identifier))
        return Identifier(scope=IdentifierScope.from_namespace(namespace), value=value, evidence=identifier.evidence)
