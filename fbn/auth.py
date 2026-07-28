"""Secret-file parsing for unattended Facebook browser authentication."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .exceptions import ConfigurationError

MAX_AUTH_FILE_BYTES = 10 * 1024 * 1024
_FACEBOOK_DOMAIN = "facebook.com"


def _configuration_error(message: str) -> ConfigurationError:
    # Keep all parser failures independent of cookie names and values.
    return ConfigurationError(f"authentication file {message}")


def _read_auth_file(path: Path) -> str:
    try:
        if not path.is_file():
            raise _configuration_error("must be a readable regular file")
        if path.stat().st_size > MAX_AUTH_FILE_BYTES:
            raise _configuration_error("is larger than 10 MiB")
        return path.read_text(encoding="utf-8-sig")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _configuration_error("could not be read as UTF-8") from exc


def _facebook_domain(raw_domain: object, raw_url: object = None) -> str | None:
    domain: str | None = None
    if isinstance(raw_domain, str) and raw_domain.strip():
        domain = raw_domain.strip().lower()
    elif isinstance(raw_url, str) and raw_url.strip():
        try:
            domain = (urlparse(raw_url.strip()).hostname or "").lower()
        except ValueError as exc:
            raise _configuration_error("contains a cookie with an invalid URL") from exc
    if domain is None:
        raise _configuration_error("contains a cookie without a domain")

    host = domain.lstrip(".").rstrip(".")
    if host != _FACEBOOK_DOMAIN and not host.endswith(f".{_FACEBOOK_DOMAIN}"):
        return None
    return f".{host}" if domain.startswith(".") else host


def _boolean(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise _configuration_error("contains an invalid cookie boolean")


def _expires(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _configuration_error("contains an invalid cookie expiry")
    try:
        expires = float(value)
    except (TypeError, ValueError) as exc:
        raise _configuration_error("contains an invalid cookie expiry") from exc
    if not math.isfinite(expires):
        raise _configuration_error("contains an invalid cookie expiry")
    return expires if expires > 0 else None


def _same_site(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _configuration_error("contains an invalid SameSite value")
    normalized = value.strip().casefold().replace("-", "_")
    choices = {
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "no_restriction": "None",
        "unspecified": None,
    }
    if normalized not in choices:
        raise _configuration_error("contains an invalid SameSite value")
    return choices[normalized]


def _normalize_cookie(raw: Mapping[str, Any]) -> dict[str, object] | None:
    domain = _facebook_domain(raw.get("domain"), raw.get("url"))
    if domain is None:
        return None

    name = raw.get("name")
    value = raw.get("value")
    path = raw.get("path", "/")
    if (
        not isinstance(name, str)
        or not name
        or any(character.isspace() or ord(character) < 32 for character in name)
        or not isinstance(value, str)
        or not isinstance(path, str)
        or not path.startswith("/")
        or any(ord(character) < 32 for character in path)
    ):
        raise _configuration_error("contains an invalid Facebook cookie")

    cookie: dict[str, object] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "httpOnly": _boolean(raw.get("httpOnly")),
        "secure": _boolean(raw.get("secure")),
    }
    raw_expiry = raw.get("expires")
    if raw_expiry is None:
        raw_expiry = raw.get("expirationDate")
    expiry = _expires(raw_expiry)
    if expiry is not None:
        cookie["expires"] = expiry
    same_site = _same_site(raw.get("sameSite"))
    if same_site is not None:
        cookie["sameSite"] = same_site
    return cookie


def _json_entries(text: str) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _configuration_error("is not valid JSON") from exc

    if isinstance(payload, dict):
        payload = payload.get("cookies")
    if not isinstance(payload, list):
        raise _configuration_error(
            "must contain a JSON cookie array or storage-state cookies"
        )
    if any(not isinstance(item, dict) for item in payload):
        raise _configuration_error("contains a non-object cookie entry")
    return payload


def _netscape_entries(text: str) -> list[Mapping[str, Any]]:
    entries: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or (
            stripped.startswith("#") and not stripped.startswith("#HttpOnly_")
        ):
            continue
        fields = line.split("\t", 6)
        if len(fields) != 7:
            raise _configuration_error("contains an invalid Netscape cookie row")
        domain, include_subdomains, path, secure, expires, name, value = fields
        http_only = domain.startswith("#HttpOnly_")
        if http_only:
            domain = domain.removeprefix("#HttpOnly_")
        if _boolean(include_subdomains) and not domain.startswith("."):
            domain = f".{domain}"
        entries.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure,
                "expires": expires,
                "name": name,
                "value": value,
                "httpOnly": http_only,
            }
        )
    return entries


def load_facebook_cookies(
    path: str | Path,
) -> tuple[list[dict[str, object]], int]:
    """Load supported cookie exports, retaining only Facebook-domain cookies.

    Returns the normalized cookies and the number of non-Facebook cookies that
    were deliberately ignored. Cookie names and values are never included in
    parser errors.
    """

    auth_path = Path(path)
    text = _read_auth_file(auth_path)
    stripped = text.lstrip()
    if not stripped:
        raise _configuration_error("is empty")

    if stripped.startswith(("{", "[")):
        entries = _json_entries(text)
    else:
        entries = _netscape_entries(text)

    normalized: dict[tuple[str, str, str], dict[str, object]] = {}
    ignored = 0
    for entry in entries:
        cookie = _normalize_cookie(entry)
        if cookie is None:
            ignored += 1
            continue
        key = (
            str(cookie["name"]),
            str(cookie["domain"]),
            str(cookie["path"]),
        )
        normalized[key] = cookie

    if not normalized:
        raise _configuration_error("contains no Facebook cookies")
    return list(normalized.values()), ignored
