"""Application catalog inventory; integrations are not dependencies."""

from app.ingestion.adapters.common import mapping, records, string_list
from app.ingestion.errors import InputError
from app.ingestion.normalization import boolean, nonmissing, text
from app.schemas.normalized import ApplicationAttributes, ApplicationRecord


def applications_json(document, ctx):
    document = mapping(document, "/")
    rows = records(document.get("applications"), "/applications")
    metadata = mapping(document.get("inventory_metadata", {}), "/inventory_metadata")
    summary = mapping(document.get("summary", {}), "/summary")
    ctx.check_count(metadata.get("total_applications"), len(rows), "/inventory_metadata/total_applications")
    ctx.check_count(summary.get("total_apps"), len(rows), "/summary/total_apps")
    result = []
    for i, row in enumerate(rows):
        path = f"/applications/{i}"
        ctx.record_path = path
        if not {"app_id", "name"}.intersection(row):
            raise InputError("Application object requires an app_id or name field", path=path)
        count = row.get("users_count")
        if count is not None and (type(count) is not int or count < 0):
            raise InputError("Expected a nonnegative integer user count", path=path + "/users_count")
        values = nonmissing({key: text(row.get(key), path + "/" + key)
                             for key in ("name", "vendor", "category", "deployment")})
        type_value = text(row.get("app_type"), path + "/app_type")
        if type_value is not None:
            type_map = {"saas": "saas", "on-premise": "on_prem", "on-prem": "on_prem", "on_prem": "on_prem", "unknown": "unknown"}
            values["app_type"] = type_map.get(type_value.casefold(), "unknown")
            if type_value.casefold() not in type_map:
                ctx.warn("unknown_app_type", path + "/app_type", "Unrecognized app type; normalized to unknown")
        values.update(nonmissing(dict(
            owner_team=text(row.get("owner"), path + "/owner"),
            reported_user_count=row.get("users_count"),
            sso_enabled=boolean(row.get("sso_enabled"), path + "/sso_enabled", ctx.warn),
        )))
        attributes = ApplicationAttributes(**values)
        targets = string_list(row.get("integrations"), path + "/integrations")
        edges = [ctx.app_edge("integrates_with", name, f"{path}/integrations/{j}")
                 for j, name in enumerate(targets)]
        result.append(ApplicationRecord(**ctx.base(row, path), display_name=attributes.name,
                                        identifiers=ctx.identifiers(
                                            ctx.identifier("application", "app_id", row.get("app_id"), path + "/app_id"),
                                            ctx.identifier("application", "app_name", attributes.name, path + "/name"),
                                        ), attributes=attributes, relationships=edges))
    return result, {"inventory_metadata": metadata, "summary": summary}
