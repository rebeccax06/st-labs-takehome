from datetime import UTC, datetime
from itertools import permutations

import pytest

from app.reconciliation.evidence import Candidate, choose
from app.reconciliation.policy import Policy
from app.schemas.ingest import SourceConfig

OLD = datetime(2024, 1, 1, tzinfo=UTC)
NEW = datetime(2025, 1, 1, tzinfo=UTC)


def test_authority_wins_without_format_or_arrival_order():
    candidates = [Candidate("active", "1", "mdm", OLD), Candidate("inactive", "2", "other", NEW)]
    for ordering in permutations(candidates):
        result = choose(list(ordering), "mdm")
        assert result.value == "active"
        assert result.observation_ids == ("1",)


def test_freshness_requires_every_competing_value_to_be_comparable():
    result = choose([Candidate("a", "1", "one", OLD), Candidate("b", "2", "two", NEW)])
    assert result.value == "b"
    result = choose([Candidate("a", "1", "one", None), Candidate("b", "2", "two", NEW)])
    assert result.status == "conflict" and result.value is None
    assert result.observation_ids == ("1", "2")


def test_equal_time_conflicts_and_missing_values():
    assert choose([Candidate(None, "1", "one", NEW)]) is None
    assert choose([Candidate(False, "1", "one", None), Candidate(None, "2", "one", NEW)]).value is False
    assert choose([Candidate("a", "1", "one", NEW), Candidate("b", "2", "two", NEW)]).status == "conflict"


def test_agreement_without_timestamps_and_reference_label_fallback():
    assert choose([Candidate("same", "1", "one", None), Candidate("same", "2", "two", NEW)]).value == "same"
    assert choose([Candidate("label", "1", "one", NEW, True), Candidate("inventory", "2", "two", None)]).value == "inventory"


def test_policy_is_versioned_and_rejects_multiple_authorities():
    first = SourceConfig(organization_id="org-a", source_id="one", adapter="hardware_json",
                         authoritative_attributes=["device.status"])
    second = first.model_copy(update={"source_id": "two"})
    with pytest.raises(ValueError, match="one source"):
        Policy([first, second])
    assert Policy([first]).version != Policy([first.model_copy(update={"authoritative_attributes": []})]).version
    assert Policy([first]).authority("org-a", "device", "status") == "one"
