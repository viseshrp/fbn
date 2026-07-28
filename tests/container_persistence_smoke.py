"""Facebook-independent container replacement smoke check.

This file is mounted into the built image by CI; it is not packaged.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from fbn.browser import PlaywrightPostSource
from fbn.config import BrowserSettings
from fbn.models import GroupRef, Post
from fbn.state import SQLiteStateRepository

DATA_DIR = Path("/home/fbn/.local/share/fbn")
PROFILE_DIR = DATA_DIR / "profile"
STATE_FILE = DATA_DIR / "state.sqlite3"
COOKIE_NAME = "fbn_container_persistence_probe"
COOKIE_VALUE = "sanitized-local-value"
GROUP = GroupRef(
    key="container-smoke",
    url="https://www.facebook.com/groups/container-smoke/",
)
OBSERVED_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)
POST = Post(
    group_key=GROUP.key,
    post_id="persistence-probe",
    url=("https://www.facebook.com/groups/container-smoke/posts/persistence-probe/"),
    text="sanitized local fixture",
    author="Test",
    observed_at=OBSERVED_AT,
    position=0,
)


def write_state() -> None:
    source = PlaywrightPostSource(
        BrowserSettings(
            browser="chromium",
            profile_dir=PROFILE_DIR,
            headless=True,
        )
    )
    with source._context(headless=True) as context:
        context.add_cookies(
            [
                {
                    "name": COOKIE_NAME,
                    "value": COOKIE_VALUE,
                    "domain": ".facebook.com",
                    "path": "/",
                    "expires": 2_000_000_000,
                    "secure": True,
                }
            ]
        )
    with SQLiteStateRepository(STATE_FILE) as repository:
        result = repository.observe(
            GROUP,
            (POST,),
            notify_initial=True,
            observed_at=OBSERVED_AT,
        )
    assert result.inserted == 1


def read_state() -> None:
    source = PlaywrightPostSource(
        BrowserSettings(
            browser="chromium",
            profile_dir=PROFILE_DIR,
            headless=True,
        )
    )
    with source._context(headless=True) as context:
        cookies = context.cookies("https://www.facebook.com/")
    assert any(
        cookie["name"] == COOKIE_NAME and cookie["value"] == COOKIE_VALUE
        for cookie in cookies
    )
    with SQLiteStateRepository(STATE_FILE) as repository:
        result = repository.observe(
            GROUP,
            (POST,),
            notify_initial=True,
            observed_at=OBSERVED_AT,
        )
    assert result.inserted == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("write", "read"))
    arguments = parser.parse_args()
    if arguments.mode == "write":
        write_state()
    else:
        read_state()


if __name__ == "__main__":
    main()
