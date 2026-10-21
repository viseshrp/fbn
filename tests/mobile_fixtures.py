"""Synthetic mobile layouts. Never copy production HTML or account content."""

from __future__ import annotations

import json
from html import escape


def mobile_loading_shell_fixture(*, accessible_spinner: bool = False) -> str:
    """Synthetic empty boot screen, including invisible fallback UI.

    The retained incident had a CSS spinner without a progressbar role. Hidden
    fallback text/forms must not make that shell readable or authenticated.
    """

    return (
        "<!doctype html><html><body><div hidden><h1>Hidden fallback</h1>"
        '<input name="email"><a href="/login/">Hidden login</a></div>'
        '<div style="position:fixed;top:50%;left:50%;width:24px;height:24px;'
        'border:4px solid #ccc;border-top-color:#888;border-radius:50%"'
        + (' role="progressbar"' if accessible_spinner else "")
        + "></div></body></html>"
    )


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


def mobile_preview_fixture(
    preview: str,
    complete: str,
    *,
    component: str = "TextArea",
    button: bool = False,
    behavior: str = "expand",
    delay_ms: int = 0,
    action_region: bool = True,
) -> str:
    """Synthetic inline Bloks truncation; no production content or attributes."""
    html = mobile_fixture([("301", "Synthetic Author", preview)])
    label = (
        '<button role="button">See more</button>'
        if button
        else "<span>...see more</span>"
    )
    body = (
        f'<div data-mcomponent="{component}" data-action-id="expand-301" '
        + (
            'style="pointer-events:none"'
            if action_region
            else 'onclick="expandPreview(this)"'
        )
        + f"><div>{escape(preview)} {label}</div></div>"
    )
    if action_region:
        body = (
            '<div data-mcomponent="MContainer" data-action-id="body-action-301" '
            'onclick="expandPreview(this.firstElementChild)">' + body + "</div>"
        )
    html = html.replace(
        f'<div data-mcomponent="ServerTextArea">{escape(preview)}</div>', body, 1
    )
    return html + (
        "<script>function expandPreview(node) {"
        "window.previewClicks=(window.previewClicks||0)+1;"
        "setTimeout(() => {"
        f"const behavior={json.dumps(behavior)};"
        "if(behavior==='never')return;"
        f"node.textContent={json.dumps(complete)};"
        "node.removeAttribute('onclick');"
        "if(behavior==='author'){"
        "const header=document.querySelector('[data-story]').children[1];"
        "header.querySelector('[role=link]').textContent='Other Author';"
        "header.querySelector('button').setAttribute('aria-label',"
        "'More options for Other Author');}"
        "if(behavior==='publication'){"
        "document.querySelector('[data-story] [aria-label]')"
        ".setAttribute('aria-label','Monday, October 12, 2026 at 10:00 AM');}"
        "if(behavior==='replace_menu'){"
        "const menu=document.querySelector('[data-story] button');"
        "menu.replaceWith(menu.cloneNode(true));}"
        "if(behavior==='replace_card'){"
        "const old=document.querySelector('[data-story]');"
        "const next=old.cloneNode(false); next.append(...old.childNodes);"
        "old.replaceWith(next);}"
        "if(behavior==='action_refresh'||behavior==='reuse_menu'){"
        "const menu=document.querySelector('[data-story] button');"
        "menu.setAttribute('data-action-id','refreshed-action');"
        "if(behavior==='reuse_menu')menu.setAttribute('onclick','openPost(\"999\")');}"
        "if(behavior==='footer_loading'){"
        "const footer=document.querySelector('[aria-label=\"Make a comment\"]');"
        "footer.hidden=true; setTimeout(()=>footer.hidden=false,150);}"
        f"}}, {delay_ms});"
        "}</script>"
    )


def mobile_split_link_preview_fixture(
    preview: str,
    complete: str,
    *,
    component: str = "TextArea",
    partial: bool = True,
    caption: str = "Automatic preview title",
) -> str:
    """Bloks puts linked media and its clickable caption in sibling branches."""
    html = (
        mobile_preview_fixture(preview, complete, component=component, delay_ms=80)
        if partial
        else mobile_fixture([("301", "Synthetic Author", complete)])
    )
    attachment = (
        '<div data-mcomponent="MContainer" role="link" data-action-id="image-link">'
        '<div data-mcomponent="ImageArea" style="width:32px;height:32px">'
        '<img width="32" height="32" '
        'src="https://scontent.example.fbcdn.net/123456_234567_345_n.jpg">'
        "</div></div>"
        '<div data-mcomponent="ImageArea" style="height:1px"></div>'
        '<div data-mcomponent="MContainer"><div data-mcomponent="MContainer" '
        'data-action-id="caption-link">'
        f'<div data-mcomponent="{component}">EXAMPLE.INVALID</div>'
        f'<div data-mcomponent="{component}">{escape(caption)}</div>'
        "</div></div>"
    )
    marker = (
        '</div><div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like,'
    )
    return html.replace(marker, attachment + marker, 1)


def mobile_layout_fixture(
    *,
    partial: bool = False,
    panel: bool = False,
    component: str = "TextArea",
    button: bool = False,
    flat: bool = False,
) -> str:
    """Independent preview/panel switches reproduce the combined failure."""
    preview = "Synthetic preview."
    full = "Synthetic preview. Full continuation."
    html = (
        mobile_preview_fixture(preview, full, component=component, button=button)
        if partial
        else mobile_fixture([("301", "Synthetic Author", full)])
    )
    if panel:
        feedback = (
            '<div data-mcomponent="MContainer" class="feedback-panel">'
            '<div data-mcomponent="ServerTextArea">'
            "Are you interested in this post?</div>"
            '<div data-mcomponent="MContainer">'
            '<button role="button" aria-label="Interested">'
            '<span data-mcomponent="TextArea">Interested</span></button>'
            '<button role="button" aria-label="Not interested">'
            '<span data-mcomponent="TextArea">Not interested</span></button>'
            "</div></div>"
        )
        marker = (
            '</div><div data-mcomponent="MContainer">'
            '<button role="button" aria-label="Like,'
        )
        html = html.replace(marker, "</div>" + feedback + marker[6:], 1)
    if flat:
        html += (
            '<script>const layoutRoot=document.querySelector("[data-type=vscroller]");'
            "layoutRoot.firstElementChild.remove();"
            'const layoutCard=layoutRoot.querySelector("[data-story]");'
            "layoutCard.firstElementChild.remove();"
            "layoutCard.replaceWith(...layoutCard.childNodes);</script>"
        )
    return html


def app_prompt_fixture(*, known: bool = True, delay_ms: int = 0) -> str:
    """A blocking app prompt; no navigation or account action is allowed."""
    heading = "Facebook is better on the app" if known else "Unknown blocking dialog"
    content = (
        "<h2>" + heading + "</h2><button role=button>Get app</button>"
        '<button role=button onclick="this.parentElement.remove();'
        'window.promptDismissals=(window.promptDismissals||0)+1">Not now</button>'
    )
    return (
        '<script>setTimeout(() => {const modal=document.createElement("div");'
        'modal.setAttribute("role","dialog");'
        'modal.style="position:fixed;inset:0;background:white;z-index:99999";'
        f"modal.innerHTML={json.dumps(content)};"
        "document.body.append(modal);"
        f"}}, {delay_ms});</script>"
    )
