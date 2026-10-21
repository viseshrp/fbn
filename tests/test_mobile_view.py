"""Independent browser-to-application boundary validation; no browser needed."""

from __future__ import annotations

import pytest

from fbn.mobile_compare import comparison_script, parser_reference
from fbn.mobile_view import post_view_issues
from tests.mobile_fixtures import post_view_fixture as valid_view


def test_mobile_view_boundary_accepts_complete_synthetic_evidence():
    assert post_view_issues(valid_view()) == ()


@pytest.mark.parametrize("field", list(valid_view()))
@pytest.mark.parametrize("invalid", [None, {}, [], -1, True, ""])
def test_mobile_view_boundary_rejects_invalid_required_evidence(field, invalid):
    value = valid_view()
    value[field] = invalid
    issues = post_view_issues(value)
    # Empty text and empty attachment lists are legitimate in their own right;
    # cross-field acceptance separately requires text or verifiable media.
    if (
        (field in {"text", "timestamp"} and invalid == "")
        or (field == "actionToken" and invalid is None)
        or (field == "mediaKeys" and invalid == [])
        or (field == "partial" and invalid is True)
    ):
        if field == "partial":
            assert "post_view_completeness" in issues
        else:
            assert issues == ()
    else:
        assert issues
        assert "Synthetic" not in repr(issues)


def test_mobile_view_boundary_rejects_non_object_and_contradictory_completeness():
    assert post_view_issues([]) == ("post_view_object",)
    value = valid_view()
    value.update(partial=True, bodyState="partial", previewControlCount=0)
    assert post_view_issues(value) == ("post_view_completeness",)


@pytest.mark.parametrize(
    "mutation",
    ["unknown_block", "ambiguous", "duplicate_recognizer", "unknown_control"],
)
def test_mobile_view_rejects_unknown_or_ambiguous_claim_evidence(mutation):
    value = valid_view()
    if mutation == "unknown_block":
        value["blocks"][0]["role"] = "unknown"
    elif mutation == "ambiguous":
        value["recognizers"][0]["status"] = "ambiguous"
    elif mutation == "duplicate_recognizer":
        value["recognizers"][0] = value["recognizers"][1].copy()
    else:
        value["recognizers"][-1]["status"] = "recognized"
    assert post_view_issues(value)


@pytest.mark.parametrize("malformed", [None, [], {}, 1, True])
@pytest.mark.parametrize("field", ["name", "status", "role"])
def test_mobile_view_nested_evidence_errors_never_raise_or_echo_content(
    malformed, field
):
    value = valid_view()
    item = value["blocks"][0] if field == "role" else value["recognizers"][0]
    item[field] = malformed
    issues = post_view_issues(value)
    assert issues and "Synthetic" not in repr(issues)


def test_reference_context_has_no_default_fallback_and_resets_after_error():
    assert comparison_script("candidate", permalink=False) == "candidate"
    with pytest.raises(RuntimeError), parser_reference("const permalink = false;"):
        assert "const permalink = true;" in comparison_script(
            "candidate", permalink=True
        )
        raise RuntimeError("synthetic")
    assert comparison_script("candidate", permalink=False) == "candidate"


@pytest.mark.parametrize("reference", ["", " " * 10, "x" * 1_000_001])
def test_reference_context_requires_bounded_trusted_source(reference):
    with pytest.raises(ValueError), parser_reference(reference):
        pytest.fail("Invalid reference accepted")
