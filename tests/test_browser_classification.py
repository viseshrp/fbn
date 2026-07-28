from __future__ import annotations

import pytest

import fbn.browser as browser_module
from fbn.browser import (
    PageSignals,
    PageState,
    classify_page,
    wait_for_terminal_page,
)
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
    LayoutChangedError,
    TransientNavigationError,
)
from fbn.extractor import parse_group_ref


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


def test_terminal_page_rejects_a_feed_redirected_away_from_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_page = object()
    monkeypatch.setattr(
        browser_module,
        "read_page_signals",
        lambda page, *, status=None: PageSignals(
            url="https://www.facebook.com/",
            status=status,
            post_count=1,
            has_feed=True,
        ),
    )

    with pytest.raises(AccessDeniedError, match="redirected away"):
        wait_for_terminal_page(
            wrong_page,  # type: ignore[arg-type]
            timeout_seconds=1,
            expected_group=parse_group_ref("expected-group"),
        )


def test_terminal_page_preserves_auth_error_after_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_page = object()
    monkeypatch.setattr(
        browser_module,
        "read_page_signals",
        lambda page, *, status=None: PageSignals(
            url="https://www.facebook.com/login/",
            status=status,
            has_login=True,
        ),
    )

    with pytest.raises(AuthenticationRequiredError):
        wait_for_terminal_page(
            login_page,  # type: ignore[arg-type]
            timeout_seconds=1,
            expected_group=parse_group_ref("expected-group"),
        )
