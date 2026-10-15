"""Exercise installed-image failure screenshots offline, without test packages."""

from __future__ import annotations

import json
import stat
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from mobile_fixtures import mobile_fixture

from fbn.config import BrowserSettings
from fbn.exceptions import LayoutChangedError
from fbn.extractor import parse_group_ref
from fbn.mobile import MobilePostSource
from fbn.models import ScanPolicy


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fbn-error-screenshots-") as temporary:
        root = Path(temporary)
        source = MobilePostSource(BrowserSettings(profile_dir=root / "profile"))
        with source._context(headless=True) as context:
            original_pages = tuple(context.pages)
            _failure = "post"

            def routes(route):
                html = mobile_fixture([("101", "Synthetic Author", "Synthetic body")])
                if "/share/" in route.request.url:
                    html = mobile_fixture([("101", "Synthetic Author", "Wrong body")])
                    html += (
                        "<script>history.replaceState(null,'',"
                        "'/groups/contract-group/posts/101/')</script>"
                    )
                elif _failure == "feed":
                    html = "<main>Synthetic unsupported feed</main>"
                route.fulfill(status=200, content_type="text/html", body=html)

            context.route("**/*", routes)

            @contextmanager
            def existing_context(**kwargs):
                yield context

            source._context = existing_context
            for _failure in ("post", "feed"):
                try:
                    source.fetch_recent(
                        parse_group_ref("contract-group"),
                        ScanPolicy(
                            sample_count=1, max_scrolls=0, navigation_timeout_seconds=2
                        ),
                    )
                except LayoutChangedError:
                    pass
                else:
                    raise AssertionError("The synthetic invalid post was accepted.")
                assert tuple(context.pages) == original_pages
        storage = root / "failure-screenshots"
        events = sorted(storage.iterdir())
        assert len(events) == 2
        for event, roles in zip(events, ({"feed", "post"}, {"feed"}), strict=True):
            assert stat.S_IMODE(event.stat().st_mode) == 0o700
            metadata = json.loads((event / "metadata.json").read_text())
            assert metadata["pages"] == dict.fromkeys(roles, "saved")
            assert metadata["failure_category"] == "LayoutChangedError"
            for image in event.glob("*.png"):
                data = image.read_bytes()
                assert data.startswith(b"\x89PNG\r\n\x1a\n")
                assert all(value > 0 for value in struct.unpack(">II", data[16:24]))
                assert len(data) <= 5 * 1024 * 1024
            assert all(
                stat.S_IMODE(path.stat().st_mode) == 0o600 for path in event.iterdir()
            )
        assert stat.S_IMODE(storage.stat().st_mode) == 0o700
    print(json.dumps({"offlineFailureEvents": 2, "screenshots": 3, "private": True}))


if __name__ == "__main__":
    main()
