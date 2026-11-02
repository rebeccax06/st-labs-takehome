"""Validate extracted facts, literal identifiers, and source evidence before persistence."""

import json
import re
from pathlib import Path

from pydantic import ValidationError

from app.ai.client import AIError
from app.ai.evidence import check_boolean, check_identifier_role, evidence_value, scoped_evidence
from app.ai.json_output import decode_object
from app.ai.schemas import Extraction
from app.ingestion.adapters.common import Context
from app.ingestion.errors import InputError
from app.ingestion.normalization import boolean, ip, mac, operating_system, status
from app.schemas.ingest import PreparedIngestion
from app.schemas.normalized import (
    ApplicationRecord, DeviceRecord, EntityReference, NormalizedBatch,
    RelationshipAssertion, UserRecord,
)

MAX_AI_BYTES = 64 * 1024
PROMPT_VERSION = "extraction-v4"
MODELS = {"device": DeviceRecord, "user": UserRecord, "application": ApplicationRecord}
IDENTIFIER_TYPES = {
    "device": {"device_id", "external_id", "serial"},
    "user": {"user_id", "employee_id", "email", "external_id"},
    "application": {"app_id", "app_name", "external_id"},
}
SYSTEM = (Path(__file__).parent / "prompts" / "extraction.md").read_text(encoding="utf-8")


class Extractor:
    def __init__(self, client):
        self.client = client
        self.version = PROMPT_VERSION + ":" + client.settings.openrouter_model

    def prepare(self, request, source, document):
        if len(request.content.encode("utf-8")) > MAX_AI_BYTES:
            raise InputError("Unfamiliar input is limited to 64 KiB for AI extraction; split it or select a known adapter.",
                             code="limit_exceeded")
        ctx = Context(source)
        if document is not None:
            ctx.snapshot(document)
        prompt = json.dumps(dict(format=request.format, parsed_input=document, raw_input=request.content if document is None else None), ensure_ascii=True)
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        for attempt in range(2):
            raw = self.client.complete(messages=messages, schema=Extraction.model_json_schema())
            try:
                # Reject duplicate keys instead of accepting the last contradictory value.
                decoded = decode_object(raw)
                extraction = Extraction.model_validate_json(json.dumps(decoded))
                if not extraction.records:
                    if extraction.unsupported_reason:
                        raise InputError("No supported devices, users, or applications were identified. Check the layout or select a known source.",
                                         code="unsupported_layout")
                    raise ValueError("Missing extracted records")
                if extraction.unsupported_reason:
                    raise ValueError("Extraction cannot be both supported and unsupported")
                trial = Context(source, observed_at=ctx.observed_at, warnings=list(ctx.warnings), warning_count=ctx.warning_count)
                return self._convert(extraction, request, trial, document)
            except (ValidationError, ValueError, RecursionError) as exc:
                if isinstance(exc, InputError):
                    raise
                if attempt:
                    raise AIError("AI extraction failed schema or evidence validation; no data was saved.") from None
                if isinstance(exc, ValidationError):
                    first = exc.errors(include_input=False, include_context=False)[0]
                    hint = "/".join(str(part) for part in first["loc"])[:160] + ": " + first["type"]
                elif isinstance(exc, RecursionError):
                    hint = "Output nesting is too deep"
                else:
                    hint = str(exc)[:240]
                messages.extend([
                    {"role": "assistant", "content": raw[:16384]},
                    {"role": "user", "content": "Correct the complete extraction, not just one field. "
                     "Validation failure: " + hint + ". Use only record-local, verifiable evidence. "
                     "Omit unsupported or missing attributes; never invent identifiers."},
                ])
        raise AssertionError("Unreachable extraction retry state")

    def _convert(self, extraction, request, ctx, document):
        def evidence(value, parent=None):
            if parent is not None:
                return scoped_evidence(value, parent, document, request.content)
            return evidence_value(value, document, request.content)
        def identifiers(kind, items, path, parent, attributes=None):
            result = []
            for item in items:
                if item.identifier_type not in IDENTIFIER_TYPES[kind]:
                    raise ValueError("Identifier type does not match CI type")
                observed = evidence(item.evidence, parent)
                check_identifier_role(item, attributes or {})
                value = item.value
                if item.evidence.pointer is not None:
                    if isinstance(observed, (dict, list, bool)) or observed is None:
                        raise ValueError("Identifier evidence must be scalar")
                    observed = str(observed).strip()
                    if item.identifier_type in ctx.source.case_insensitive_identifiers:
                        observed, value = observed.casefold(), value.casefold()
                    if observed != value.strip():
                        raise ValueError("Identifier is not the literal source value")
                else:
                    flags = re.IGNORECASE if item.identifier_type in ctx.source.case_insensitive_identifiers else 0
                    if not re.search(r"(?<![\w@.+-])" + re.escape(value) + r"(?![\w@.+-])", observed, flags):
                        raise ValueError("Identifier does not occur literally in evidence")
                identifier = ctx.identifier(kind, item.identifier_type, item.value, path,
                                            trusted_only=item.identifier_type in {"email", "serial"})
                if identifier is not None:
                    result.append(identifier.model_copy(update={"evidence": item.evidence}))
                else:
                    ctx.warn("untrusted_identifier", path, "Identifier authority is not configured; value retained only as evidence")
            return result
        records = []
        for i, row in enumerate(extraction.records):
            path = f"/ai/records/{i}"
            observed = evidence(row.evidence)
            attributes = {key: (value.strip() or None) if isinstance(value, str) else value
                          for key, value in row.attributes.items()}
            try:
                if row.ci_type in {"device", "user"} and attributes.get("status") is not None:
                    attributes["status"] = status(attributes["status"], path + "/status", ctx.warn,
                                                  user=row.ci_type == "user")
                if row.ci_type == "device":
                    if attributes.get("os_family") is not None:
                        attributes["os_family"] = operating_system(attributes["os_family"], path + "/os_family")[0]
                    for field, normalize in (("ip_address", ip), ("mac_address", mac)):
                        if attributes.get(field) is not None:
                            attributes[field] = normalize(attributes[field], path + "/" + field)
                bool_field = {"device": "encrypted", "user": "mfa_enabled", "application": "sso_enabled"}[row.ci_type]
                if attributes.get(bool_field) is not None:
                    attributes[bool_field] = boolean(attributes[bool_field], path + "/" + bool_field, ctx.warn)
                if row.ci_type == "application" and isinstance(attributes.get("app_type"), str):
                    app_type = attributes["app_type"].casefold()
                    attributes["app_type"] = {"on-premise": "on_prem", "on-prem": "on_prem"}.get(app_type, app_type)
            except InputError:
                raise ValueError("Extracted attributes cannot be normalized") from None
            for field, value in attributes.items():
                if value is None:
                    continue
                if field not in row.attribute_evidence:
                    raise ValueError("Attribute evidence is missing")
                original = evidence(row.attribute_evidence[field], row.evidence)
                if original is None or original == "":
                    raise ValueError("Cannot invent a missing attribute")
                if isinstance(value, bool):
                    check_boolean(value, original)
            relations = []
            for j, assertion in enumerate(row.relationships):
                evidence(assertion.evidence, row.evidence)
                evidence(assertion.target.evidence)
                target = assertion.target
                relations.append(RelationshipAssertion(
                    relation_type=assertion.relation_type, evidence=assertion.evidence,
                    target=EntityReference(ci_type=target.ci_type, display_name=target.display_name,
                                           identifiers=identifiers(target.ci_type, target.identifiers, f"{path}/relationships/{j}", target.evidence),
                                           evidence=target.evidence),
                ))
            values = dict(ci_type=row.ci_type, record_key=path, organization_id=request.organization_id,
                          attributes=attributes, evidence=row.evidence.model_dump(mode="json"),
                          identifiers=[key.model_dump(mode="json") for key in identifiers(row.ci_type, row.identifiers, path, row.evidence, attributes)],
                          relationships=[r.model_dump(mode="json") for r in relations],
                          observed_at=ctx.observed_at.isoformat() if ctx.observed_at else None,
                          raw_record=observed,
                          attribute_evidence={k: v.model_dump(mode="json") for k, v in row.attribute_evidence.items()})
            # JSON-mode validation restores aware datetimes and validates typed attributes.
            records.append(MODELS[row.ci_type].model_validate_json(json.dumps(values)))
        return PreparedIngestion(organization_id=request.organization_id, source_id=request.source_id,
                                 batch=NormalizedBatch(records=records), origin="ai", warnings=ctx.warnings,
                                 warning_count=ctx.warning_count,
                                 source_metadata={"extraction_model": self.client.settings.openrouter_model,
                                                  "extraction_version": PROMPT_VERSION})
