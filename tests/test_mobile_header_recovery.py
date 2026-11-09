"""Counter contract for positively classified header assembly, not acceptance."""

import pytest

from fbn import mobile
from fbn.browser import PageSignals
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    LayoutChangedError,
    TransientNavigationError,
)
from fbn.mobile import _is_header_assembly, _is_retryable_feed_timestamp


def evidence():
    return {
        "feedRootCount": 1,
        "itemCount": 2,
        "postCount": 1,
        "headerAssemblyCount": 1,
        "unclassifiedMenuCards": 0,
        "missing": ["unique_primary_menu"],
    }


def test_header_assembly_requires_all_candidates_accounted_for():
    assert _is_header_assembly(evidence())
    value = evidence()
    value.update(itemCount=3, postCount=0, headerAssemblyCount=3)
    assert _is_header_assembly(value)


@pytest.mark.parametrize(
    "field",
    [
        "feedRootCount",
        "itemCount",
        "postCount",
        "headerAssemblyCount",
        "unclassifiedMenuCards",
    ],
)
@pytest.mark.parametrize("value", [None, True, -1, "SYNTHETIC_SECRET", {}, []])
def test_header_assembly_rejects_missing_or_non_integer_counts(field, value):
    data = evidence()
    data[field] = value
    assert not _is_header_assembly(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"feedRootCount": 2},
        {"headerAssemblyCount": 0},
        {"unclassifiedMenuCards": 1},
        {"itemCount": 3},
        {"missing": []},
        {"missing": ["unique_primary_menu", "unknown_layout"]},
        {"missing": ["unique_primary_timestamp"]},
    ],
)
def test_header_assembly_rejects_other_missing_evidence(mutation):
    data = evidence()
    data.update(mutation)
    assert not _is_header_assembly(data)


def timestamp_evidence():
    return {
        "feedRootCount": 1,
        "itemCount": 6,
        "postCount": 5,
        "headerAssemblyCount": 0,
        "unclassifiedMenuCards": 0,
        "timestampAbsentCards": 1,
        "timestampMultipleCards": 0,
        "missing": ["unique_primary_timestamp"],
    }


def test_one_ambiguous_timestamp_in_an_otherwise_readable_feed_can_retry():
    assert _is_retryable_feed_timestamp(
        timestamp_evidence(), {"dialogs": 0}, PageSignals(url="https://example.invalid")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"feedRootCount": 0},
        {"feedRootCount": 2},
        {"itemCount": 7},
        {"itemCount": True},
        {"postCount": 0},
        {"postCount": 6},
        {"headerAssemblyCount": 1},
        {"unclassifiedMenuCards": 1},
        {"timestampAbsentCards": 0},
        {"timestampAbsentCards": True},
        {"timestampMultipleCards": 1},
        {"missing": []},
        {"missing": ["unique_primary_timestamp", "unknown_layout"]},
    ],
)
def test_timestamp_retry_rejects_unaccounted_or_unsupported_feed_shapes(mutation):
    data = timestamp_evidence()
    data.update(mutation)
    assert not _is_retryable_feed_timestamp(
        data, {"dialogs": 0}, PageSignals(url="https://example.invalid")
    )


@pytest.mark.parametrize(
    "field", ["has_login", "has_account_action", "has_access_denied", "has_rate_limit"]
)
def test_timestamp_retry_rejects_security_or_access_signals(field):
    signals = PageSignals(url="https://example.invalid", **{field: True})
    assert not _is_retryable_feed_timestamp(
        timestamp_evidence(), {"dialogs": 0}, signals
    )


@pytest.mark.parametrize("dialogs", [None, True, 1, "0"])
def test_timestamp_retry_requires_a_known_dialog_free_snapshot(dialogs):
    assert not _is_retryable_feed_timestamp(
        timestamp_evidence(),
        {"dialogs": dialogs},
        PageSignals(url="https://example.invalid"),
    )


@pytest.mark.parametrize(
    "change", ["checkpoint", "navigation", "dialog", "access", "rate"]
)
def test_timestamp_retry_rechecks_security_and_navigation(monkeypatch, change):
    class Page:
        url = "https://m.facebook.com/groups/example/"
        snapshots = 0

        def evaluate(self, script, *args):
            if script == mobile.MOBILE_SCAN_SCRIPT:
                return timestamp_evidence()
            assert script == mobile.RENDER_SNAPSHOT_SCRIPT
            self.snapshots += 1
            return {"dialogs": int(change == "dialog" and self.snapshots == 2)}

    page = Page()
    calls = 0

    def signals(current, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and change == "navigation":
            current.url = "https://m.facebook.com/groups/other/"
        return PageSignals(
            url=current.url,
            has_feed=True,
            post_count=5,
            has_account_action=calls == 2 and change == "checkpoint",
            has_access_denied=calls == 2 and change == "access",
            has_rate_limit=calls == 2 and change == "rate",
        )

    monkeypatch.setattr(mobile, "read_page_signals", signals)
    expected = {
        "checkpoint": AccountActionRequiredError,
        "navigation": LayoutChangedError,
        "dialog": LayoutChangedError,
        "access": AccessDeniedError,
        "rate": TransientNavigationError,
    }[change]
    with pytest.raises(expected):
        mobile._inspect_failed_render(page, permalink=False, phase="page_render")
    assert calls == 2
