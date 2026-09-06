from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import fbn.browser as browser_module
from fbn.browser import PageState, PlaywrightPostSource
from fbn.config import BrowserSettings
from fbn.extractor import parse_group_ref
from fbn.models import ScanPolicy


class FakePage:
    def __init__(self) -> None:
        self.loaded = 2
        self.scrolls = 0

    def evaluate(self, script: str) -> None:
        assert "scrollBy" in script
        self.loaded = 4
        self.scrolls += 1

    def wait_for_timeout(self, milliseconds: float) -> None:
        assert milliseconds >= 0


def payloads(page: FakePage) -> list[dict[str, object]]:
    return [
        {
            "href": f"https://www.facebook.com/groups/example/posts/{index}/",
            "text": f"post {index}",
            "author": "Author",
            "position": index - 1,
            "timestamp": "1m",
        }
        for index in range(1, page.loaded + 1)
    ]


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlaywrightPostSource:
    monkeypatch.setattr(
        browser_module,
        "wait_for_terminal_page",
        lambda *args, **kwargs: PageState.FEED,
    )
    monkeypatch.setattr(
        browser_module,
        "collect_group_aliases",
        lambda *args, **kwargs: frozenset(),
    )
    monkeypatch.setattr(browser_module, "collect_dom_payloads", payloads)
    return PlaywrightPostSource(
        BrowserSettings(profile_dir=tmp_path / "profile"),
    )


def test_normal_scan_stops_at_sample_count(source: PlaywrightPostSource) -> None:
    page = FakePage()

    result = source._scan_feed(
        page,
        parse_group_ref("example"),
        ScanPolicy(sample_count=2, max_scrolls=1),
        datetime(2026, 9, 5, tzinfo=timezone.utc),
    )

    assert [post.post_id for post in result.posts] == ["1", "2"]
    assert result.bounded is True
    assert page.scrolls == 0


def test_catch_up_scan_continues_past_sample_count_to_marker(
    source: PlaywrightPostSource,
) -> None:
    page = FakePage()

    result = source._scan_feed(
        page,
        parse_group_ref("example"),
        ScanPolicy(sample_count=2, max_scrolls=1),
        datetime(2026, 9, 5, tzinfo=timezone.utc),
        boundary_post_id="4",
    )

    assert [post.post_id for post in result.posts] == ["1", "2", "3", "4"]
    assert result.bounded is False
    assert page.scrolls == 1


def test_catch_up_scan_reports_limit_when_marker_is_missing(
    source: PlaywrightPostSource,
) -> None:
    page = FakePage()
    page.loaded = 4

    result = source._scan_feed(
        page,
        parse_group_ref("example"),
        ScanPolicy(sample_count=2, max_scrolls=1),
        datetime(2026, 9, 5, tzinfo=timezone.utc),
        boundary_post_id="missing",
    )

    assert [post.post_id for post in result.posts] == ["1", "2", "3", "4"]
    assert result.bounded is True
    assert page.scrolls == 0
