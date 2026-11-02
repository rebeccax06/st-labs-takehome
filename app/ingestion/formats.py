"""Transport hints never establish source authority or identity scopes."""

import re

FORMATS = ("json", "csv", "yaml", "text")
EXTENSIONS = {"json": "json", "csv": "csv", "yaml": "yaml", "yml": "yaml"}
MEDIA_TYPES = {"application/json": "json", "text/csv": "csv", "application/yaml": "yaml",
               "application/x-yaml": "yaml", "text/yaml": "yaml"}


def detect_format(content: str, *, filename: str | None = None, media_type: str | None = None) -> str:
    if filename and filename.rsplit(".", 1)[-1].lower() in EXTENSIONS:
        return EXTENSIONS[filename.rsplit(".", 1)[-1].lower()]
    if media_type and media_type.split(";", 1)[0].lower() in MEDIA_TYPES:
        return MEDIA_TYPES[media_type.split(";", 1)[0].lower()]
    stripped = content.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    lines = stripped.splitlines()
    if len(lines) > 1 and "," in lines[0] and "," in lines[1]:
        return "csv"
    if stripped.startswith(("---", "inventory:", "applications:")) or (
        len(re.findall(r"(?m)^\s*[\w-]+:\s", stripped)) >= 2
    ):
        return "yaml"
    return "text"


def recognized_adapter(document, format: str) -> str | None:
    if isinstance(document, dict):
        inventory = document.get("inventory")
        if isinstance(inventory, dict) and isinstance(inventory.get("devices"), list):
            return "hardware_yaml"
        if isinstance(document.get("applications"), list):
            return "applications_json"
        if format == "csv" and {"device_id", "hostname"}.issubset(document.get("headers", [])):
            return "hardware_csv"
    if isinstance(document, list) and document and all(isinstance(row, dict) for row in document):
        if all("device_id" in row and not isinstance(row.get("assigned_to"), dict) for row in document):
            return "hardware_json"
        if all("user_id" in row for row in document):
            return "okta_json"
    return None
