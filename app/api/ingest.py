"""File picker, direct raw bodies, and the backwards-compatible JSON envelope."""

from enum import Enum
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from app.ingestion.errors import InputError
from app.ingestion.formats import FORMATS, detect_format
from app.ingestion.parsing import MAX_BYTES, _unique_object
from app.ingestion.service import ADAPTERS
from app.schemas.api import IngestResponse
from app.schemas.ingest import IngestRequest, SourceConfig


def add_upload_source(sources, organization):
    sources = list(sources)
    automatic = next((source for source in sources if source.adapter == "auto"), None)
    if automatic is not None:
        return sources, automatic.source_id
    source_id = "uploaded-data"
    while any(s.source_id == source_id for s in sources):
        source_id += "-auto"
    sources.append(SourceConfig(organization_id=organization, source_id=source_id, adapter="auto",
                                identifier_authorities={"email": source_id},
                                case_insensitive_identifiers=["email", "app_name"]))
    return sources, source_id


def decode_content(data):
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        raise InputError("Use a UTF-8 JSON, CSV, YAML, or text file; binary uploads are not supported.",
                         code="unsupported_encoding", path="/file") from None
    if "\x00" in content or content.startswith(("%PDF-", "PK\x03\x04")):
        raise InputError("Binary files such as PDF/Excel/images are unsupported. Export JSON, CSV, YAML, or text.",
                         code="unsupported_file", path="/file")
    if not content.strip():
        raise InputError("The uploaded file or text is empty.", path="/content")
    return content


def create_ingest_router(sources, default_source):
    router = APIRouter(tags=["Ingestion"])
    SourceChoice = Enum("IngestSource", {f"SOURCE_{i}": source.source_id for i, source in enumerate(sources)}, type=str)
    by_id = {s.source_id: s for s in sources}
    source_description = (
        "Choose a configured source, or " + default_source + " for automatic layout detection and AI extraction. "
        "Organization and known-source format are derived from server configuration."
    )
    multipart_schema = dict(type="object", required=["file"], properties={
        "file": {"type": "string", "format": "binary", "description": "UTF-8 inventory file, at most 1 MiB."},
        "source_id": {"type": "string", "enum": list(by_id), "default": default_source, "description": source_description},
        "format": {"type": "string", "enum": ["auto", *FORMATS], "default": "auto",
                   "description": "Usually leave auto. Known sources determine their format."},
    })

    def raw_request(content, source_id, format="auto", *, filename=None, media_type=None):
        source = by_id.get(source_id)
        if source is None:
            raise InputError("Unknown source. Use GET /ingest/sources to list configured choices.", path="/source_id", code="unknown_source")
        if format not in ("auto", *FORMATS):
            raise InputError("Format must be auto, json, csv, yaml, or text.", path="/format")
        known = ADAPTERS.get(source.adapter)
        if known is not None:
            if format not in ("auto", known[0]):
                raise InputError(f"Selected source expects {known[0]}; leave format auto or choose a different source.", path="/format")
            format = known[0]
        elif format == "auto":
            format = detect_format(content, filename=filename, media_type=media_type)
        try:
            return IngestRequest(organization_id=source.organization_id, source_id=source.source_id,
                                 format=format, content=content)
        except ValidationError as exc:
            raise InputError("Invalid raw input; check the selected source and format.") from exc

    async def submit(request, payload):
        if payload.organization_id != request.app.state.settings.organization_id:
            raise InputError("Unknown source or organization", code="unknown_source", path="/organization_id")
        return await run_in_threadpool(request.app.state.ingestion.ingest, payload)

    @router.get("/ingest/sources", summary="List source choices and their derived formats")
    def source_choices():
        return {"default_source_id": default_source, "sources": [dict(
            source_id=s.source_id, organization_id=s.organization_id, adapter=s.adapter,
            formats=[ADAPTERS[s.adapter][0]] if s.adapter in ADAPTERS else list(FORMATS) if s.adapter == "auto" else [],
            ai_for_unfamiliar_layouts=s.adapter == "auto",
        ) for s in sources]}

    @router.post("/ingest", response_model=IngestResponse, summary="Upload raw inventory data",
                 description="Choose a file and source. Existing JSON-envelope clients remain supported. "
                             "Direct text/JSON bodies also work, with optional source_id and format query parameters. "
                             "For a pasteable text editor in this UI, use POST /ingest/raw.",
                 openapi_extra={"requestBody": {"required": True, "content": {
                     "multipart/form-data": {"schema": multipart_schema},
                     "application/json": {"schema": IngestRequest.model_json_schema()},
                 }}})
    async def ingest(request: Request):
        media = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if media == "application/x-www-form-urlencoded":
            raise InputError("Choose a file using multipart/form-data, or paste text at /ingest/raw.", path="/file")
        if media == "multipart/form-data":
            try:
                async with request.form(max_files=1, max_fields=2, max_part_size=MAX_BYTES) as form:
                    if set(form) - {"file", "source_id", "format"} or any(len(form.getlist(k)) != 1 for k in form):
                        raise InputError("Send one file and optional source_id/format fields.")
                    file = form.get("file")
                    if not isinstance(file, UploadFile):
                        raise InputError("Choose a file in the multipart 'file' field.", path="/file")
                    data = await file.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise InputError("File exceeds 1 MiB.", code="limit_exceeded", path="/file")
                    source_id = form.get("source_id", default_source)
                    format = form.get("format", "auto")
                    if not isinstance(source_id, str) or not isinstance(format, str):
                        raise InputError("Source and format must be text fields.")
                    payload = raw_request(decode_content(data), source_id, format,
                                          filename=file.filename, media_type=file.content_type)
            except HTTPException:
                raise InputError("Malformed multipart upload; send one file and at most source_id/format fields.") from None
        else:
            data = await request.body()
            if media == "application/json":
                try:
                    document = json.loads(data, object_pairs_hook=_unique_object)
                except (ValueError, UnicodeError, RecursionError):
                    raise InputError("Malformed or excessively nested JSON request. Use multipart for files or /ingest/raw to paste raw text.") from None
                if isinstance(document, dict) and {"content", "organization_id", "source_id"}.intersection(document):
                    try:
                        payload = IngestRequest.model_validate(document)
                    except ValidationError as exc:
                        raise RequestValidationError([
                            dict(error, loc=("body", *error["loc"])) for error in exc.errors(include_input=False)
                        ]) from None
                else:
                    payload = raw_request(decode_content(data), request.query_params.get("source_id", default_source),
                                          request.query_params.get("format", "auto"), media_type=media)
            else:
                payload = raw_request(decode_content(data), request.query_params.get("source_id", default_source),
                                      request.query_params.get("format", "auto"), media_type=media)
        return await submit(request, payload)

    @router.post("/ingest/raw", response_model=IngestResponse, summary="Paste raw JSON, CSV, YAML, or text",
                 description="Paste the document directly into the text/plain request body: no JSON wrapper or escaped quotes. "
                             "Choose a source; organization and known-source format are derived automatically.",
                 openapi_extra={"requestBody": {"required": True, "content": {"text/plain": {"schema": {
                     "type": "string", "example": 'device_id,hostname,os\nD-17,workstation-17,macOS\n',
                 }}}}})
    async def raw(request: Request,
                  source_id: Annotated[SourceChoice, Query(description=source_description)] = SourceChoice(default_source),
                  format: Annotated[Literal["auto", "json", "csv", "yaml", "text"], Query(
                      description="Leave auto unless an unfamiliar input's format is ambiguous.")] = "auto"):
        payload = raw_request(decode_content(await request.body()), source_id.value, format,
                              media_type=request.headers.get("content-type"))
        return await submit(request, payload)

    return router
