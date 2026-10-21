"""Content boundary and independent validation for the mobile parser."""

from __future__ import annotations

from typing import Literal, TypedDict


class BlockEvidence(TypedDict):
    role: Literal["message", "attachment", "interface_panel", "discussion", "unknown"]
    textChars: int
    component: str | None


class PostView(TypedDict, total=False):
    author: str
    text: str
    timestamp: str
    timestampLabel: str
    timestampObservedAt: int
    cardToken: int
    actionToken: str | None
    mediaCount: int
    mediaKeys: list[str]
    partial: bool
    position: int
    bodyControlCount: int
    bodyTextPartCount: int
    bodyTextLengths: list[int]
    previewControlCount: int
    blocks: list[BlockEvidence]
    bodyState: Literal["partial", "complete"]
    parserVersion: int
    recognizers: list[dict[str, str]]
    normalization: dict[str, int]


def post_view_issues(value: object) -> tuple[str, ...]:
    """Validate required evidence, without returning content in error details."""

    if not isinstance(value, dict):
        return ("post_view_object",)
    issues = []
    for name in ("author", "text", "timestamp", "timestampLabel"):
        field = value.get(name)
        if not isinstance(field, str) or (
            name not in {"text", "timestamp"} and not field.strip()
        ):
            issues.append(f"post_view_{name}")
    for name in (
        "timestampObservedAt",
        "cardToken",
        "mediaCount",
        "position",
        "bodyControlCount",
        "bodyTextPartCount",
        "previewControlCount",
    ):
        field = value.get(name)
        if type(field) is not int or field < 0:
            issues.append(f"post_view_{name}")
    if type(value.get("partial")) is not bool:
        issues.append("post_view_partial")
    elif value.get("bodyState") != ("partial" if value["partial"] else "complete") or (
        type(value.get("previewControlCount")) is int
        and value["partial"] != (value["previewControlCount"] > 0)
    ):
        issues.append("post_view_completeness")
    if type(value.get("parserVersion")) is not int or value["parserVersion"] != 1:
        issues.append("post_view_parser_version")
    if "actionToken" not in value or (
        value["actionToken"] is not None
        and (
            not isinstance(value["actionToken"], str)
            or not value["actionToken"].strip()
        )
    ):
        issues.append("post_view_action_token")
    keys = value.get("mediaKeys")
    if not isinstance(keys, list) or not all(
        isinstance(key, str) and key for key in keys
    ):
        issues.append("post_view_media_keys")
    lengths = value.get("bodyTextLengths")
    if not isinstance(lengths, list) or not all(
        type(length) is int and length >= 0 for length in lengths
    ):
        issues.append("post_view_text_lengths")
    elif type(value.get("bodyTextPartCount")) is int and len(lengths) != min(
        32, value["bodyTextPartCount"]
    ):
        issues.append("post_view_text_part_count")
    normalization = value.get("normalization")
    if (
        not isinstance(normalization, dict)
        or any(
            type(normalization.get(name)) is not int or normalization[name] < 0
            for name in ("version", "nodes", "collapsed")
        )
        or normalization.get("version") != 1
    ):
        issues.append("post_view_normalization")
    recognizers = value.get("recognizers")
    expected = {
        "discussion",
        "attachments",
        "split_preview",
        "interface_panel",
        "expander",
        "media",
        "action_ownership",
        "unknown_controls",
    }
    if (
        not isinstance(recognizers, list)
        or len(recognizers) != len(expected)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("status"), str)
            or item.get("name") not in expected
            or item.get("status") not in {"recognized", "absent"}
            or (
                item.get("name") == "unknown_controls"
                and item.get("status") != "absent"
            )
            for item in recognizers
        )
        or {item["name"] for item in recognizers} != expected
    ):
        issues.append("post_view_recognizers")
    blocks = value.get("blocks")
    allowed_roles = {"message", "attachment", "interface_panel", "discussion"}
    if (
        not isinstance(blocks, list)
        or not blocks
        or any(
            not isinstance(block, dict)
            or not isinstance(block.get("role"), str)
            or block.get("role") not in allowed_roles
            or type(block.get("textChars")) is not int
            or block["textChars"] < 0
            or not (
                block.get("component") is None or isinstance(block["component"], str)
            )
            for block in blocks
        )
    ):
        issues.append("post_view_blocks")
    return tuple(issues)
