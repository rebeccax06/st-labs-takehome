"""Strict JSON decoding for model-generated objects."""

import json


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Model output contains duplicate JSON keys")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("Model output contains a non-finite number")


def decode_object(content):
    value = json.loads(content, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value
