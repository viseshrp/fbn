"""Synthetic mobile layouts. Never copy production HTML or account content."""

from __future__ import annotations

import json
from html import escape


def mobile_fixture(
    stories: list[tuple[str, str, str]],
    *,
    group: str = "contract-group",
    sorted_feed: bool = True,
    uppercase_sort: bool = False,
) -> str:
    cards = []
    for post_id, author, body in stories:
        cards.append(
            '<div data-mcomponent="MContainer" data-type="container" '
            f'data-story="{escape(post_id)}">'
            '<div data-mcomponent="ServerImageArea" style="height:1px"></div>'
            '<div data-mcomponent="MContainer">'
            '<div data-mcomponent="ServerTextArea">'
            f'<span role="link">{escape(author)}</span></div>'
            '<div data-mcomponent="ServerTextArea" '
            'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>'
            f'<button role="button" aria-label="More options for {escape(author)}" '
            f'data-action-id="menu-{escape(post_id)}" '
            f'onclick="openPost({escape(json.dumps(post_id))})">...</button></div>'
            '<div data-mcomponent="MContainer">'
            f'<div data-mcomponent="ServerTextArea">{escape(body)}</div>'
            '<div role="article" aria-label="Shared post">'
            '<div data-mcomponent="ServerTextArea">Quoted body</div>'
            '<button role="button" aria-label="More options for Quoted">'
            "...</button></div>"
            '<div aria-label="Comment by Reply Example">'
            '<div data-mcomponent="ServerTextArea">Nested reply body</div></div>'
            '</div><div data-mcomponent="MContainer">'
            '<button role="button" '
            'aria-label="Like, press and hold for more reactions">'
            "Like</button>"
            '<button role="button" aria-label="Make a comment">Comment</button></div>'
            '<div data-mcomponent="MContainer">'
            '<div data-mcomponent="ServerTextArea">Following reply preview</div>'
            "</div></div>"
        )
    sort_label = "New posts" if sorted_feed else "Most relevant"
    sort_action = "onclick=\"document.getElementById('sort-menu').hidden=false\""
    sort_control = (
        f'<div id="sort-label" data-mcomponent="ServerTextArea" '
        f'style="pointer-events:none">{sort_label}</div>'
        '<div data-mcomponent="ServerTextArea" data-action-id="sort" '
        f"{sort_action}>SORT</div>"
        if uppercase_sort
        else (
            f'<div id="sort-label" data-mcomponent="ServerTextArea">{sort_label}</div>'
            f"<button {sort_action}>"
            '<div data-mcomponent="ServerTextArea">\u200bSort\u200b</div>'
            "</button>"
        )
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body><div data-mcomponent="MScreen">'
        '<div data-mcomponent="MContainer" data-type="vscroller">'
        '<div data-mcomponent="MContainer">'
        + sort_control
        + "</div>"
        + "".join(cards)
        + "</div></div>"
        '<div id="sort-menu" hidden><button onclick="setSort()">'
        '<div data-mcomponent="ServerTextArea">'
        + ("NEW POSTS" if uppercase_sort else "New posts")
        + "</div></button></div>"
        '<div id="post-menu" hidden>'
        '<button aria-label="Close" onclick="closePost()">Close</button>'
        '<button aria-label="Share">Share</button>'
        '<button aria-label="Copy link" onclick="copyPost()">Copy link</button>'
        "</div><script>"
        f"const groupKey = {json.dumps(group)};"
        "let activePost = null;"
        "function openPost(id) {activePost=id; "
        'document.getElementById("post-menu").hidden=false;}'
        'function closePost() {document.getElementById("post-menu").hidden=true; '
        "activePost=null;}"
        "function copyPost() {navigator.clipboard.writeText("
        '"https://m.facebook.com/share/p/"+activePost+"/");}'
        'function setSort() {document.getElementById("sort-menu").hidden=true;'
        'document.getElementById("sort-label").textContent="New posts";}'
        "</script></body></html>"
    )
