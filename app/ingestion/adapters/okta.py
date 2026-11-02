"""Okta-style user export: account facts, groups, and access assertions."""

from app.ingestion.adapters.common import records, string_list
from app.ingestion.errors import InputError
from app.ingestion.normalization import boolean, nonmissing, status, text, timestamp
from app.schemas.normalized import UserAttributes, UserRecord


def okta_json(document, ctx):
    result = []
    for i, row in enumerate(records(document, "")):
        path = f"/{i}"
        ctx.record_path = path
        if not {"user_id", "email", "name"}.intersection(row):
            raise InputError("User object requires a user_id, email, or name field", path=path)
        values = nonmissing(dict(
            name=text(row.get("name"), path + "/name"),
            email=text(row.get("email"), path + "/email"),
            status=status(row.get("status"), path + "/status", ctx.warn, user=True),
            mfa_enabled=boolean(row.get("mfa_enabled"), path + "/mfa_enabled", ctx.warn),
            last_login=timestamp(row.get("last_login"), path + "/last_login"),
        ))
        if row.get("groups") is not None:
            values["groups"] = string_list(row["groups"], path + "/groups")
        attributes = UserAttributes(**values)
        apps = string_list(row.get("apps"), path + "/apps")
        edges = [ctx.app_edge("uses", name, f"{path}/apps/{j}") for j, name in enumerate(apps)]
        result.append(UserRecord(**ctx.base(row, path),
                                 display_name=attributes.name or attributes.email,
                                 identifiers=ctx.identifiers(
                                     ctx.identifier("user", "user_id", row.get("user_id"), path + "/user_id"),
                                     ctx.identifier("user", "email", attributes.email, path + "/email", trusted_only=True),
                                 ), attributes=attributes, relationships=edges))
    return result, {}
