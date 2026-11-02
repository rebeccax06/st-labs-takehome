"""Check source locations without giving the model control over CI identity."""

import re

from app.ingestion.errors import InputError
from app.ingestion.normalization import boolean

DISPLAY_FIELDS = {"name", "fullname", "displayname", "firstname", "lastname", "hostname",
                  "computername", "machinename", "ip", "ipaddress"}
UNKNOWN_BOOLEANS = {"unknown", "n/a", "not reported", "unspecified", "not available"}


def evidence_value(evidence, document, content):
    if evidence.quote is not None and evidence.quote not in content:
        raise ValueError("Evidence quote does not occur in the input")
    if evidence.pointer is None:
        return evidence.quote
    pointer = evidence.pointer
    if document is None or not pointer.startswith("/"):
        raise ValueError("Evidence pointer is not valid for this input")
    value = document
    if pointer == "/":
        return value  # Document-root convention used by the extraction contract.
    try:
        for part in pointer[1:].split("/"):
            if re.search(r"~(?![01])", part):
                raise ValueError("Invalid pointer escape")
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
                value = value[int(part)]
            else:
                raise ValueError("Invalid pointer target")
    except (KeyError, IndexError):
        raise ValueError("Evidence pointer does not exist") from None
    return value


def scoped_evidence(evidence, parent, document, content):
    """Require attributes/IDs to belong to their declared source record.

    References to another entity use that target's own evidence scope. This checks
    record boundaries, not whether every natural-language inference is correct.
    """
    value = evidence_value(evidence, document, content)
    parent_value = evidence_value(parent, document, content)
    if parent.pointer is not None and evidence.pointer is not None:
        if parent.pointer != "/" and evidence.pointer != parent.pointer and not evidence.pointer.startswith(parent.pointer + "/"):
            raise ValueError("Evidence points outside its record; use a record-local pointer")
    elif parent.quote is not None and evidence.quote is not None:
        if evidence.quote not in parent.quote:
            raise ValueError("Evidence quote is outside its record quote")
    elif isinstance(parent_value, str) and evidence.quote is not None:
        if evidence.quote not in parent_value:
            raise ValueError("Evidence quote is outside its record text")
    elif parent.quote != content:
        raise ValueError("Use consistent record-local pointers for structured evidence")
    return value


def check_boolean(value, observed):
    if isinstance(observed, str) and observed.strip().casefold() in UNKNOWN_BOOLEANS:
        raise ValueError("Unknown boolean cannot become true or false")
    if type(observed) is int and observed in (0, 1):
        expected = bool(observed)
    else:
        try:
            expected = boolean(observed, "/evidence", lambda *args: None)
        except InputError:
            expected = None
    if expected is not None and value != expected:
        raise ValueError("Boolean contradicts explicit source evidence")


def check_identifier_role(identifier, attributes):
    """Reject obvious attempts to promote names/hosts/IPs into source IDs."""
    if identifier.identifier_type == "app_name":
        return
    proof = identifier.evidence
    if proof.pointer is not None:
        field = proof.pointer.rsplit("/", 1)[-1]
        if re.sub(r"[^a-z]", "", field.casefold()) in DISPLAY_FIELDS:
            raise ValueError("Display names, hostnames, and IPs are not stable identifier fields")
    elif identifier.value in (attributes.get("hostname"), attributes.get("name"), attributes.get("ip_address")):
        if not re.search(r"\b(id|identifier|asset[ -]?tag|employee[ -]?(id|number))\b", proof.quote, re.IGNORECASE):
            raise ValueError("A display label needs explicit source-ID evidence before becoming an identifier")
