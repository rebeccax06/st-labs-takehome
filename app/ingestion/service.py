"""Common validation/preparation boundary for every input transport."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from app.ingestion.adapters.applications import applications_json
from app.ingestion.adapters.common import Context
from app.ingestion.adapters.hardware import hardware_csv, hardware_json, hardware_yaml
from app.ingestion.adapters.okta import okta_json
from app.ingestion.errors import InputError
from app.ingestion.formats import recognized_adapter
from app.ingestion.parsing import parse_content, validate_content_size
from app.schemas.ingest import IngestRequest, PreparedIngestion, SourceConfig
from app.schemas.normalized import NormalizedBatch

Adapter = Callable[[Any, Context], tuple[list, dict]]
ADAPTERS: dict[str, tuple[str, Adapter]] = {
    "hardware_json": ("json", hardware_json),
    "hardware_csv": ("csv", hardware_csv),
    "hardware_yaml": ("yaml", hardware_yaml),
    "okta_json": ("json", okta_json),
    "applications_json": ("json", applications_json),
}


def validate_request(request, sources, adapters=None):
    index = {(source.organization_id, source.source_id): source for source in sources}
    if len(index) != len(sources):
        raise ValueError("Duplicate source configuration")
    source = index.get((request.organization_id, request.source_id))
    if source is None:
        raise InputError("Unknown source or organization", path="/source_id", code="unknown_source")
    registry = ADAPTERS if adapters is None else adapters
    selected = registry.get(source.adapter)
    if source.adapter != "auto":
        if selected is None:
            raise InputError("No deterministic adapter configured", code="unsupported_source")
        if request.format != selected[0]:
            raise InputError("Format does not match configured source adapter", path="/format")
    validate_content_size(request.content)
    return source, selected


def prepare_ingestion(
    request: IngestRequest,
    sources: Sequence[SourceConfig],
    *,
    adapters: Mapping[str, tuple[str, Adapter]] | None = None,
    extractor=None,
) -> PreparedIngestion:
    source, selected = validate_request(request, sources, adapters)
    document = parse_content(request.content, request.format) if request.format != "text" else None
    if source.adapter == "auto":
        if request.format != "text" and (document is None or document == [] or document == {}
                                        or (request.format == "csv" and not document["rows"])):
            raise InputError("Input contains no inventory records.", code="unsupported_layout")
        detected = recognized_adapter(document, request.format)
        if detected:
            selected = ADAPTERS[detected]
        elif extractor is not None:
            return extractor.prepare(request, source, document)
        else:
            raise InputError("Unfamiliar layout: configure OpenRouter extraction or select a supported source adapter.",
                             code="unsupported_layout")
    _, adapter = selected
    ctx = Context(source)
    if document is not None:
        ctx.snapshot(document)
    try:
        normalized, metadata = adapter(document, ctx)
        if len(normalized) > 1000:
            raise InputError("More than 1000 extracted entity records", code="limit_exceeded")
        batch = NormalizedBatch(records=normalized)
    except ValidationError as exc:
        location = "/".join(str(part) for part in exc.errors()[0]["loc"])
        raise InputError("Record does not satisfy the normalized schema", path=ctx.record_path.rstrip("/") + "/" + location) from exc
    return PreparedIngestion(organization_id=request.organization_id, source_id=request.source_id,
                             batch=batch, source_metadata=metadata,
                             warning_count=ctx.warning_count, warnings=ctx.warnings)
