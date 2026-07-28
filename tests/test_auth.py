from __future__ import annotations

import json
from pathlib import Path

import pytest

from fbn.auth import MAX_AUTH_FILE_BYTES, load_facebook_cookies
from fbn.exceptions import ConfigurationError


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_loads_playwright_storage_state_and_ignores_origins(tmp_path: Path) -> None:
    auth_file = tmp_path / "state.json"
    write_json(
        auth_file,
        {
            "cookies": [
                {
                    "name": "session",
                    "value": "private-value",
                    "domain": ".facebook.com",
                    "path": "/",
                    "expires": 2_000_000_000,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ],
            "origins": [{"origin": "https://www.facebook.com", "localStorage": []}],
        },
    )

    cookies, ignored = load_facebook_cookies(auth_file)

    assert cookies == [
        {
            "name": "session",
            "value": "private-value",
            "domain": ".facebook.com",
            "path": "/",
            "expires": 2_000_000_000.0,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]
    assert ignored == 0


def test_loads_exported_json_and_filters_foreign_domains(tmp_path: Path) -> None:
    auth_file = tmp_path / "cookies.json"
    write_json(
        auth_file,
        [
            {
                "name": "session",
                "value": "facebook-secret",
                "domain": "www.facebook.com",
                "path": "/",
                "expirationDate": "2000000000",
                "secure": "true",
                "httpOnly": 1,
                "sameSite": "no_restriction",
            },
            {
                "name": "unrelated",
                "value": "foreign-secret",
                "domain": ".example.com",
                "path": "/",
            },
        ],
    )

    cookies, ignored = load_facebook_cookies(auth_file)

    assert len(cookies) == 1
    assert cookies[0]["domain"] == "www.facebook.com"
    assert cookies[0]["expires"] == 2_000_000_000.0
    assert cookies[0]["sameSite"] == "None"
    assert ignored == 1


def test_loads_netscape_cookies_and_deduplicates_last_value(tmp_path: Path) -> None:
    auth_file = tmp_path / "cookies.txt"
    auth_file.write_text(
        "\n".join(
            (
                "# Netscape HTTP Cookie File",
                "facebook.com\tTRUE\t/\tTRUE\t0\tsession\told",
                "#HttpOnly_facebook.com\tTRUE\t/\tTRUE\t0\tsession\tnew",
            )
        ),
        encoding="utf-8",
    )

    cookies, ignored = load_facebook_cookies(auth_file)

    assert cookies == [
        {
            "name": "session",
            "value": "new",
            "domain": ".facebook.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }
    ]
    assert ignored == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"cookies": "not-a-list"},
        [{"name": "session", "value": "secret-without-domain"}],
        [
            {
                "name": "session",
                "value": "secret",
                "domain": ".facebook.com",
                "path": "relative",
            }
        ],
        [
            {
                "name": "session",
                "value": "secret",
                "domain": ".facebook.com",
                "sameSite": "invalid",
            }
        ],
    ],
)
def test_rejects_invalid_json_without_echoing_cookie_data(
    tmp_path: Path,
    payload: object,
) -> None:
    auth_file = tmp_path / "cookies.json"
    write_json(auth_file, payload)

    with pytest.raises(ConfigurationError) as caught:
        load_facebook_cookies(auth_file)

    assert "secret" not in str(caught.value)
    assert "session" not in str(caught.value)


def test_rejects_a_file_with_only_foreign_cookies(tmp_path: Path) -> None:
    auth_file = tmp_path / "cookies.json"
    write_json(
        auth_file,
        [
            {
                "name": "foreign",
                "value": "private",
                "domain": ".example.com",
                "path": "/",
            }
        ],
    )

    with pytest.raises(ConfigurationError, match="no Facebook cookies"):
        load_facebook_cookies(auth_file)


def test_rejects_malformed_cookie_url_without_echoing_file_data(
    tmp_path: Path,
) -> None:
    secret = "abc:def:cookie-secret"
    auth_file = tmp_path / "cookies.json"
    write_json(
        auth_file,
        [
            {
                "name": "session",
                "value": "another-secret",
                "url": f"https://[{secret}]",
            }
        ],
    )

    with pytest.raises(ConfigurationError) as caught:
        load_facebook_cookies(auth_file)

    assert secret not in str(caught.value)
    assert "another-secret" not in str(caught.value)


def test_rejects_oversized_file_before_parsing(tmp_path: Path) -> None:
    auth_file = tmp_path / "cookies.json"
    auth_file.write_bytes(b"x" * (MAX_AUTH_FILE_BYTES + 1))

    with pytest.raises(ConfigurationError, match="larger than 10 MiB"):
        load_facebook_cookies(auth_file)
