from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import fbn.browser as browser_module
from fbn.browser import DOM_SCAN_SCRIPT, hydrate_post_permalinks


class FakeHandle:
    def __init__(self, *, hydrates: bool = True) -> None:
        self.hydrates = hydrates
        self.hovered = False
        self.disposed = False

    def as_element(self) -> FakeHandle:
        return self

    def hover(self, *, timeout: float) -> None:
        assert 0 < timeout <= 500
        self.hovered = True

    def get_attribute(self, name: str) -> str:
        assert name == "href"
        return "https://www.facebook.com/groups/test-group/posts/502/"

    def dispose(self) -> None:
        self.disposed = True


class FakeArray:
    def __init__(self, handles: list[FakeHandle]) -> None:
        self.handles = handles
        self.disposed = False

    def get_properties(self) -> dict[str, FakeHandle]:
        return {str(index): handle for index, handle in enumerate(self.handles)}

    def dispose(self) -> None:
        self.disposed = True


class FakePage:
    def __init__(self, handles: list[FakeHandle]) -> None:
        self.array = FakeArray(handles)
        self.results: list[FakeHandle] = []
        self.reads = 0

    def evaluate_handle(self, script: str, mode: str) -> FakeArray:
        assert script == DOM_SCAN_SCRIPT
        assert mode == "timestamps"
        self.reads += 1
        return self.array

    def wait_for_function(
        self, script: str, *, arg: FakeHandle, timeout: float
    ) -> FakeHandle:
        assert "node.href" in script
        assert 0 < timeout <= 500
        if not arg.hydrates:
            raise PlaywrightTimeoutError("synthetic unhydrated timestamp")
        result = FakeHandle()
        self.results.append(result)
        return result


def test_hydration_caps_attempts_and_disposes_all_handles() -> None:
    handles = [FakeHandle() for _ in range(4)]
    page = FakePage(handles)

    hydrate_post_permalinks(page, max_candidates=2)

    assert [handle.hovered for handle in handles] == [True, True, False, False]
    assert all(handle.disposed for handle in handles)
    assert all(result.disposed for result in page.results)
    assert page.array.disposed


def test_hydration_timeout_preserves_scan_and_cleans_up() -> None:
    handles = [FakeHandle(hydrates=False), FakeHandle()]
    page = FakePage(handles)

    hydrate_post_permalinks(page)

    assert all(handle.hovered and handle.disposed for handle in handles)
    assert page.array.disposed


def test_hydration_uses_one_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    handles = [FakeHandle(), FakeHandle()]
    page = FakePage(handles)
    ticks = iter([0.0, 0.0, 0.0, 2.1])
    monkeypatch.setattr(browser_module.time, "monotonic", lambda: next(ticks))

    hydrate_post_permalinks(page, timeout_seconds=2)

    assert [handle.hovered for handle in handles] == [True, False]
    assert not page.results
    assert all(handle.disposed for handle in handles)
    assert page.array.disposed


@pytest.mark.parametrize("max_candidates,timeout_seconds", [(0, 2), (10, 0)])
def test_disabled_hydration_does_not_read_page(
    max_candidates: int, timeout_seconds: float
) -> None:
    page = FakePage([FakeHandle()])

    hydrate_post_permalinks(
        page, max_candidates=max_candidates, timeout_seconds=timeout_seconds
    )

    assert page.reads == 0
