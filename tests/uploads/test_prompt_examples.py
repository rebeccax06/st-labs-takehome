"""Keep complete prompt examples compatible with the real evidence validator."""

import json
import re
from types import SimpleNamespace

import pytest

from app.ai.extraction import Extractor, SYSTEM
from app.config import Settings
from app.schemas.ingest import IngestRequest, SourceConfig


@pytest.mark.parametrize("index,content,count", [
    (0, "Employee ID: EXAMPLE-101\nName: Example Person\nMFA: disabled", 1),
    (1, "Application: Example Portal\nApplication: Example Directory\n"
        "Example Portal integrates with Example Directory.", 2),
])
def test_complete_prompt_examples_pass_evidence_validation(index, content, count):
    examples = re.findall(r"```json\n(.*?)\n```", SYSTEM, re.DOTALL)
    output = examples[index]
    client = SimpleNamespace(settings=Settings(), complete=lambda **kwargs: output)
    source = SourceConfig(organization_id="test", source_id="uploads", adapter="auto")
    request = IngestRequest(organization_id="test", source_id="uploads", format="text", content=content)
    result = Extractor(client).prepare(request, source, None)
    assert len(result.batch.records) == count
    if index == 0:
        assert result.batch.records[0].attributes.mfa_enabled is False
    else:
        edges = result.batch.records[0].relationships
        assert [edge.relation_type for edge in edges] == ["integrates_with"]
        assert edges[0].target.identifiers[0].value == "example directory"


def test_correction_identifies_unsupported_default_attribute():
    output = json.loads(re.findall(r"```json\n(.*?)\n```", SYSTEM, re.DOTALL)[1])
    output["records"][0]["attributes"]["app_type"] = {"value": "unknown"}
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["messages"][:])
        if len(calls) == 2:
            del output["records"][0]["attributes"]["app_type"]
        return json.dumps(output)

    client = SimpleNamespace(settings=Settings(), complete=complete)
    source = SourceConfig(organization_id="test", source_id="uploads", adapter="auto")
    request = IngestRequest(organization_id="test", source_id="uploads", format="text",
                           content="Application: Example Portal\nApplication: Example Directory\n"
                                   "Example Portal integrates with Example Directory.")
    result = Extractor(client).prepare(request, source, None)
    assert len(result.batch.records) == 2
    hint = calls[1][-1]["content"]
    assert "app_type" in hint
    assert "missing" in hint


def test_structured_example_preserves_identity_and_normalization():
    content = json.dumps({"assets": [{"inventory_id": 17, "label": "example-host",
        "platform": "Ubuntu 24.04", "site": "Example Lab"}]}, indent=2)
    example = re.findall(r"```json\n(.*?)\n```", SYSTEM, re.DOTALL)[-1]
    client = SimpleNamespace(settings=Settings(), complete=lambda **kwargs: example)
    source = SourceConfig(organization_id="test", source_id="uploads", adapter="auto")
    request = IngestRequest(organization_id="test", source_id="uploads", format="json", content=content)
    result = Extractor(client).prepare(request, source, json.loads(content))
    record = result.batch.records[0]
    assert record.identifiers[0].value == "17"
    assert record.attributes.hostname == "example-host"
    assert record.attributes.os_family == "linux"
    assert record.attributes.os_version == "24.04"
    assert record.attributes.location == "Example Lab"


@pytest.mark.parametrize("bad_evidence", [
    {"quote": '{"inventory_id":17,"label":"example-host"}'},
    {"pointer": "/parsed_input/assets/0"},
    {"pointer": "/assets/99"},
])
def test_structured_example_rejects_reconstructed_quotes_and_bad_pointers(bad_evidence):
    from app.ai.client import AIError

    content = json.dumps({"assets": [{"inventory_id": 17, "label": "example-host",
        "platform": "Ubuntu 24.04", "site": "Example Lab"}]}, indent=2)
    example = json.loads(re.findall(r"```json\n(.*?)\n```", SYSTEM, re.DOTALL)[-1])
    example['records'][0]['evidence'] = bad_evidence
    client = SimpleNamespace(settings=Settings(), complete=lambda **kwargs: json.dumps(example))
    request = IngestRequest(organization_id="test", source_id="uploads", format="json", content=content)
    source = SourceConfig(organization_id="test", source_id="uploads", adapter="auto")
    with pytest.raises(AIError, match="evidence validation"):
        Extractor(client).prepare(request, source, json.loads(content))


def test_separate_assignment_table_uses_enclosing_scope_and_preserves_edge():
    document = {"members": [{"id": "U-1", "name": "Morgan"}],
                "application": {"id": "A-1", "name": "Portal"},
                "assignments": [{"member_id": "U-1", "application_id": "A-1"}]}
    output = {"records": [{"ci_type": "user",
        "attributes": {"name": {"value": "Morgan", "evidence": {"pointer": "/members/0/name"}}},
        "identifiers": [{"identifier_type": "user_id", "value": "U-1", "evidence": {"pointer": "/members/0/id"}}],
        "evidence": {"pointer": "/members/0"},
        "relationships": [{"relation_type": "uses", "evidence": {"pointer": "/assignments/0"},
            "target": {"ci_type": "application", "display_name": "Portal",
                "identifiers": [{"identifier_type": "app_id", "value": "A-1", "evidence": {"pointer": "/application/id"}}],
                "evidence": {"pointer": "/application"}}}]}], "unsupported_reason": None}
    calls = []

    def complete(**kwargs):
        calls.append(kwargs['messages'][:])
        if len(calls) == 2:
            output['records'][0]['evidence'] = {'pointer': '/'}
        return json.dumps(output)

    client = SimpleNamespace(settings=Settings(), complete=complete)
    request = IngestRequest(organization_id='test', source_id='uploads', format='json', content=json.dumps(document))
    source = SourceConfig(organization_id='test', source_id='uploads', adapter='auto')
    result = Extractor(client).prepare(request, source, document)
    assert len(calls) == 2
    assert '/records/0/relationships/0/evidence' in calls[1][-1]['content']
    assert "Set this record's evidence.pointer to '/'" in calls[1][-1]['content']
    assert result.batch.records[0].relationships[0].relation_type == 'uses'
    assert result.batch.records[0].relationships[0].target.identifiers[0].value == 'A-1'


def test_complete_assignment_example_preserves_separate_entities_and_edge():
    document = {"members": [{"id": "U-1", "name": "Morgan"}],
                "application": {"id": "A-1", "name": "Portal"},
                "assignments": [{"member_id": "U-1", "application_id": "A-1"}]}
    example = re.findall(r"```json\n(.*?)\n```", SYSTEM, re.DOTALL)[2]
    client = SimpleNamespace(settings=Settings(), complete=lambda **kwargs: example)
    source = SourceConfig(organization_id="test", source_id="uploads", adapter="auto")
    request = IngestRequest(organization_id="test", source_id="uploads", format="json",
                            content=json.dumps(document))
    result = Extractor(client).prepare(request, source, document)
    assert [row.ci_type for row in result.batch.records] == ["user", "application"]
    user, app = result.batch.records
    assert user.identifiers[0].value == "U-1"
    assert user.relationships[0].relation_type == "uses"
    assert user.relationships[0].target.identifiers == app.identifiers


def test_text_scope_correction_explains_facts_across_sentences():
    content = 'Employee ID: EXAMPLE-101\nName: Example Person\nMFA: disabled'
    output = json.loads(re.findall(r"```json\n(.*?)\n```", SYSTEM, re.DOTALL)[0])
    output['records'][0]['evidence'] = {'quote': 'MFA: disabled'}
    calls = []

    def complete(**kwargs):
        calls.append(kwargs['messages'][:])
        if len(calls) == 2:
            output['records'][0]['evidence'] = {'quote': content}
        return json.dumps(output)

    client = SimpleNamespace(settings=Settings(), complete=complete)
    source = SourceConfig(organization_id='test', source_id='uploads', adapter='auto')
    request = IngestRequest(organization_id='test', source_id='uploads', format='text', content=content)
    result = Extractor(client).prepare(request, source, None)
    assert len(result.batch.records) == 1
    assert 'entire raw_input' in calls[1][-1]['content']
    assert 'Do not reconstruct or paraphrase' in calls[1][-1]['content']
