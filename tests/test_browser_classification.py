from __future__ import annotations

import pytest

from fbn.browser import (
    PageSignals,
    PageState,
    classify_page,
)
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
    LayoutChangedError,
    TransientNavigationError,
)


def signals(**overrides: object) -> PageSignals:
    values: dict[str, object] = {"url": "https://www.facebook.com/groups/example/"}
    values.update(overrides)
    return PageSignals(**values)  # type: ignore[arg-type]


def test_post_links_classify_as_feed() -> None:
    assert classify_page(signals(post_count=2, has_feed=True)) is PageState.FEED


def test_only_explicit_empty_feed_classifies_as_empty() -> None:
    assert (
        classify_page(signals(has_feed=True, has_explicit_empty=True))
        is PageState.EMPTY
    )


def test_empty_phrase_inside_a_feed_item_is_not_an_empty_feed() -> None:
    with pytest.raises(LayoutChangedError):
        classify_page(
            signals(
                has_feed=True,
                feed_item_count=1,
                has_explicit_empty=True,
            )
        )


def test_layout_error_names_missing_structural_signals() -> None:
    page_signals = signals(
        has_feed=True,
        feed_root_count=1,
        feed_item_count=2,
        top_level_item_count=1,
    )

    with pytest.raises(
        LayoutChangedError,
        match=r"missing=direct_post_permalink,story_message;.*post_candidates=0",
    ):
        classify_page(page_signals)


@pytest.mark.parametrize(
    ("page_signals", "error"),
    [
        (signals(has_login=True), AuthenticationRequiredError),
        (signals(status=401), AuthenticationRequiredError),
        (signals(has_account_action=True), AccountActionRequiredError),
        (signals(has_access_denied=True), AccessDeniedError),
        (signals(status=403), AccessDeniedError),
        (signals(status=404), AccessDeniedError),
        (signals(status=410), AccessDeniedError),
        (signals(status=408), TransientNavigationError),
        (signals(status=429), TransientNavigationError),
        (signals(status=503), TransientNavigationError),
        (signals(has_rate_limit=True), TransientNavigationError),
        (signals(has_feed=True), LayoutChangedError),
        (signals(post_count=1), LayoutChangedError),
    ],
)
def test_fail_closed_classification(
    page_signals: PageSignals,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        classify_page(page_signals)


def test_account_action_wins_over_login_form() -> None:
    with pytest.raises(AccountActionRequiredError):
        classify_page(signals(has_account_action=True, has_login=True))


def test_post_content_error_phrases_do_not_override_a_recognized_feed() -> None:
    assert (
        classify_page(
            signals(
                post_count=1,
                has_feed=True,
                has_access_denied=True,
                has_rate_limit=True,
            )
        )
        is PageState.FEED
    )
