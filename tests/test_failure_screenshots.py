"""Private screenshot storage tests using synthetic pages only."""

from __future__ import annotations

import json
import stat

import pytest

from fbn import failure_screenshots as screenshots
from fbn.exceptions import (
    AccountActionRequiredError,
    AuthenticationRequiredError,
    LayoutChangedError,
)


class FakePage:
    def __init__(self, *, closed=False, failure=False, image=b"synthetic-png"):
        self.closed = closed
        self.failure = failure
        self.image = image
        self.options = []
        self.selectors = []

    def is_closed(self):
        return self.closed

    def evaluate(self, script):
        if script == screenshots.AUTH_SCREEN_SCRIPT:
            return False
        return {"nodes": [{"parent": -1, "tag": "BODY"}], "truncated": False}

    def locator(self, selector):
        self.selectors.append(selector)
        return "masked-inputs"

    def screenshot(self, **kwargs):
        self.options.append(kwargs)
        if self.failure:
            raise RuntimeError("SYNTHETIC_SECRET in a browser error")
        return self.image


def _events(tmp_path):
    return sorted((tmp_path / "failure-screenshots").iterdir())


def test_capture_is_private_masked_bounded_and_has_no_secret_metadata(tmp_path):
    post, feed = FakePage(), FakePage()
    screenshots.capture_failure_screenshots(
        tmp_path / "profile",
        LayoutChangedError("SYNTHETIC_SECRET https://private.invalid/post"),
        {"post": post, "feed": feed},
    )
    event = _events(tmp_path)[0]
    assert screenshots._EVENT_NAME.fullmatch(event.name)
    for directory in (event.parent, event):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert {path.name for path in event.iterdir()} == {
        "post.png",
        "feed.png",
        "metadata.json",
        "post-layout.json",
        "feed-layout.json",
    }
    for path in event.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    metadata = json.loads((event / "metadata.json").read_text())
    assert metadata["failure_category"] == "LayoutChangedError"
    assert metadata["pages"] == {"post": "saved", "feed": "saved"}
    assert "SYNTHETIC_SECRET" not in (event / "metadata.json").read_text()
    assert "https://" not in (event / "metadata.json").read_text()
    for page in (post, feed):
        assert page.options == [
            {
                "type": "png",
                "full_page": False,
                "scale": "css",
                "timeout": 3_000,
                "mask": ["masked-inputs"],
                "mask_color": "#000000",
            }
        ]
        assert "input" in page.selectors[0]
        assert "contenteditable" in page.selectors[0]


def test_same_failure_is_captured_once_even_when_propagated(tmp_path):
    error, page = LayoutChangedError("Synthetic failure"), FakePage()
    for _ in range(3):
        screenshots.capture_failure_screenshots(
            tmp_path / "profile", error, {"feed": page}
        )
    assert len(_events(tmp_path)) == 1
    assert len(page.options) == 1


def test_structural_snapshot_cannot_serialize_content_identifiers_or_unknown_values():
    raw = {
        "nodes": [
            {
                "parent": -1,
                "tag": "SYNTHETIC_SECRET",
                "component": "https://private.invalid/",
                "role": "password",
                "text": "SYNTHETIC_SECRET",
                "href": "https://private.invalid/",
                "actionId": "SYNTHETIC_SECRET",
                "control": "SYNTHETIC_SECRET",
                "textChars": 9999999,
            }
        ],
        "truncated": False,
        "cookies": "SYNTHETIC_SECRET",
    }
    data = screenshots._safe_structure(raw)
    assert b"SYNTHETIC_SECRET" not in data and b"https://" not in data
    decoded = json.loads(data)
    assert decoded["nodes"][0]["tag"] == "other"
    assert decoded["nodes"][0]["control"] == "other"
    assert decoded["nodes"][0]["textChars"] == 100000
    assert len(data) <= screenshots.MAX_STRUCTURE_BYTES


@pytest.mark.parametrize(
    "kind",
    ["photo", "video", "event_interest", "event_response", "event_open", "unknown"],
)
def test_structural_snapshot_retains_only_fixed_control_categories(kind):
    data = screenshots._safe_structure(
        {"nodes": [{"parent": -1, "tag": "DIV", "control": kind}], "truncated": False}
    )
    assert json.loads(data)["nodes"][0]["control"] == kind


def test_structural_snapshot_is_node_bounded():
    data = screenshots._safe_structure(
        {"nodes": [{"parent": -1, "tag": "BODY"}] * 1001, "truncated": False}
    )
    decoded = json.loads(data)
    assert len(decoded["nodes"]) == 1000 and decoded["truncated"]


def test_late_authentication_screen_is_private_even_for_timeout_errors(tmp_path):
    class LateAuth(FakePage):
        def evaluate(self, script):
            assert script == screenshots.AUTH_SCREEN_SCRIPT
            return True

    page = LateAuth()
    screenshots.capture_failure_screenshots(
        tmp_path / "profile", TimeoutError(), {"feed": page}
    )
    assert not (tmp_path / "failure-screenshots").exists()
    assert not page.options


@pytest.mark.parametrize("status", ["closed", "failed", "too_large"])
def test_unavailable_post_does_not_prevent_feed_capture(tmp_path, status, monkeypatch):
    monkeypatch.setattr(screenshots, "MAX_SCREENSHOT_BYTES", 20)
    post = FakePage(
        closed=status == "closed",
        failure=status == "failed",
        image=b"x" * (21 if status == "too_large" else 1),
    )
    screenshots.capture_failure_screenshots(
        tmp_path / "profile",
        LayoutChangedError("Synthetic failure"),
        {"post": post, "feed": FakePage()},
    )
    event = _events(tmp_path)[0]
    assert not (event / "post.png").exists()
    assert (event / "feed.png").exists()
    assert json.loads((event / "metadata.json").read_text())["pages"] == {
        "post": status,
        "feed": "saved",
    }


@pytest.mark.parametrize(
    "error", [AuthenticationRequiredError, AccountActionRequiredError]
)
def test_known_authentication_screens_are_not_saved(tmp_path, error):
    page = FakePage()
    screenshots.capture_failure_screenshots(
        tmp_path / "profile", error("Synthetic"), {"feed": page}
    )
    assert not (tmp_path / "failure-screenshots").exists()
    assert not page.options


def test_no_pages_or_unknown_roles_do_not_create_storage(tmp_path):
    screenshots.capture_failure_screenshots(
        tmp_path / "profile",
        LayoutChangedError("Synthetic"),
        {"post": None, "../outside": FakePage()},
    )
    assert not (tmp_path / "failure-screenshots").exists()


def test_storage_rejects_symlink_without_chmod_or_writing_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (tmp_path / "failure-screenshots").symlink_to(outside, target_is_directory=True)
    before = outside.stat().st_mode
    screenshots.capture_failure_screenshots(
        tmp_path / "profile", LayoutChangedError("Synthetic"), {"feed": FakePage()}
    )
    assert outside.stat().st_mode == before
    assert not tuple(outside.iterdir())


def test_retention_only_removes_owned_event_directories(tmp_path):
    root = tmp_path / "failure-screenshots"
    root.mkdir(mode=0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("Synthetic owner data")
    (root / "unrelated").mkdir()
    (root / ("20000101T000000000000Z-" + "0" * 12)).symlink_to(
        outside, target_is_directory=True
    )
    owned_file = root / ("20000101T000000000001Z-" + "0" * 12)
    owned_file.write_text("Not an event directory")
    old_events = []
    for index in range(12):
        event = root / f"20000101T000001{index:06}Z-{'a' * 12}"
        event.mkdir()
        (event / "feed.png").write_bytes(b"synthetic")
        (event / "outside-link").symlink_to(outside, target_is_directory=True)
        old_events.append(event)
    screenshots.capture_failure_screenshots(
        tmp_path / "profile", LayoutChangedError("Synthetic"), {"feed": FakePage()}
    )
    assert not any(event.exists() for event in old_events[:3])
    assert all(event.exists() for event in old_events[3:])
    assert (
        len(
            [
                child
                for child in root.iterdir()
                if child.is_dir() and not child.is_symlink()
            ]
        )
        == 11
    )
    assert sentinel.read_text() == "Synthetic owner data"
    assert owned_file.read_text() == "Not an event directory"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_storage_failure_is_best_effort_and_does_not_log_exception_content(
    tmp_path, monkeypatch
):
    messages = []

    class Logger:
        def warning(self, message, **fields):
            messages.append((message, fields))

    def fail(*args):
        raise OSError("SYNTHETIC_SECRET full disk")

    monkeypatch.setattr(screenshots, "LOGGER", Logger())
    monkeypatch.setattr(screenshots, "_private_directory", fail)
    screenshots.capture_failure_screenshots(
        tmp_path / "profile",
        LayoutChangedError("SYNTHETIC_SECRET"),
        {"feed": FakePage()},
    )
    assert messages == [
        ("Failure screenshots unavailable; original error preserved", {})
    ]


def test_private_file_never_overwrites_existing_file_or_symlink(tmp_path):
    owner_file = tmp_path / "owner"
    owner_file.write_bytes(b"keep")
    link = tmp_path / "link"
    link.symlink_to(owner_file)
    for path in (owner_file, link):
        with pytest.raises(FileExistsError):
            screenshots._private_file(path, b"replace")
    assert owner_file.read_bytes() == b"keep"


def test_even_unavailable_logging_cannot_replace_original_error(tmp_path, monkeypatch):
    class BrokenLogger:
        def warning(self, *args, **kwargs):
            raise OSError("Synthetic closed stdout")

    monkeypatch.setattr(screenshots, "LOGGER", BrokenLogger())
    screenshots.capture_failure_screenshots(
        tmp_path / "profile", LayoutChangedError("Synthetic"), {"feed": FakePage()}
    )
    assert (_events(tmp_path)[0] / "feed.png").exists()
