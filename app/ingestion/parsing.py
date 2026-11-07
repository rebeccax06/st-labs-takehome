"""Bounded strict readers for JSON, CSV and safe YAML."""

import csv
import io
import json
import math
from typing import Any

import yaml

from app.ingestion.errors import InputError

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 100000
MAX_RECORDS = 1000


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("Duplicate mapping key")
        result[key] = value
    return result


class InventoryLoader(yaml.SafeLoader):
    """Reject aliases rather than expanding untrusted YAML graphs."""

    def __init__(self, stream):
        super().__init__(stream)
        self.inventory_depth = 0
        self.inventory_nodes = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise InputError("YAML aliases are not supported")
        self.inventory_depth += 1
        self.inventory_nodes += 1
        if self.inventory_depth > MAX_DEPTH or self.inventory_nodes > MAX_NODES:
            raise InputError("YAML nesting or node limit exceeded", code="limit_exceeded")
        try:
            return super().compose_node(parent, index)
        finally:
            self.inventory_depth -= 1

    def construct_mapping(self, node, deep=False):
        pairs = []
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise InputError("Mapping keys must be strings")
            pairs.append((key, self.construct_object(value_node, deep=deep)))
        return _unique_object(pairs)


# Preserve date spelling as text; semantic timestamp validation happens explicitly.
InventoryLoader.yaml_implicit_resolvers = {
    key: [(tag, regex) for tag, regex in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _check_tree(data: Any) -> None:
    stack = [(data, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise InputError("Document nesting or node limit exceeded", code="limit_exceeded")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise InputError("Mapping keys must be strings")
            for key in value:
                try:
                    key.encode("utf-8")
                except UnicodeError as exc:
                    raise InputError("Mapping keys must be valid UTF-8 text") from exc
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise InputError("Source strings must be valid UTF-8 text") from exc
        elif isinstance(value, float) and not math.isfinite(value):
            raise InputError("Non-finite numbers are not supported")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise InputError("Unsupported source value type")


def validate_content_size(content: str) -> None:
    try:
        size = len(content.encode("utf-8"))
    except UnicodeError as exc:
        raise InputError("Content must be valid UTF-8 text") from exc
    if size > MAX_BYTES:
        raise InputError("Upload exceeds 1 MiB", code="limit_exceeded")


def parse_content(content: str, format: str) -> Any:
    validate_content_size(content)
    try:
        if format == "json":
            data = json.loads(content, object_pairs_hook=_unique_object)
        elif format == "yaml":
            data = yaml.load(content, Loader=InventoryLoader)
        elif format == "csv":
            reader = csv.reader(io.StringIO(content, newline=""), strict=True)
            headers = next(reader, None)
            if not headers:
                raise InputError("CSV requires a header")
            headers = [header.strip().removeprefix("\ufeff") for header in headers]
            if any(not h for h in headers) or len(headers) != len(set(headers)):
                raise InputError("CSV headers must be nonblank and unique")
            data = []
            for row in reader:
                if not row:  # Empty physical lines are not inventory records.
                    continue
                if len(row) != len(headers):
                    raise InputError("CSV row length does not match header", path=f"/rows/{len(data)}")
                if len(data) == MAX_RECORDS:
                    raise InputError("More than 1000 records", code="limit_exceeded")
                data.append(dict(zip(headers, row)))
            # Headers remain available even if the document contains zero rows.
            document = {"headers": headers, "rows": data}
            _check_tree(document)
            return document
        else:
            raise InputError("No deterministic adapter for this format", code="unsupported_format")
    except InputError:
        raise
    except (ValueError, yaml.YAMLError, csv.Error, RecursionError) as exc:
        raise InputError("Malformed input document") from exc
    _check_tree(data)
    return data
