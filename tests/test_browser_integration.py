"""Headless browser checks against sanitized local DOM fixtures only."""

from __future__ import annotations

import json
import os
import stat
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from fbn.browser import PageState
from fbn.config import BrowserSettings
from fbn.exceptions import (
    AccessDeniedError,
    AccountActionRequiredError,
    AuthenticationRequiredError,
    LayoutChangedError,
    TransientNavigationError,
)
from fbn.extractor import extract_posts, parse_group_ref
from fbn.mobile import (
    MobilePostSource,
    copy_post_link,
    expand_post_preview,
    mobile_records,
    resolve_post_link,
    select_new_posts,
    wait_for_mobile_page,
)
from fbn.mobile_compare import parser_reference
from fbn.mobile_dom import MOBILE_SCAN_SCRIPT
from fbn.models import ScanPolicy
from fbn.state import SQLiteStateRepository
from tests.dom_variations import DOM_VARIATION_SCRIPT
from tests.mobile_fixtures import (
    app_prompt_fixture,
    mobile_controlled_attachment_fixture,
    mobile_fixture,
    mobile_layout_fixture,
    mobile_loading_shell_fixture,
    mobile_preview_fixture,
    mobile_split_link_preview_fixture,
)

# These cases assert DOM/identity behavior, not shared-runner navigation speed.
# Deadline arithmetic and budget enforcement have separate deterministic tests.
OFFLINE_NAVIGATION_SECONDS = 5


@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize(
    "kind",
    [
        "body_wrappers",
        "branch_wrappers",
        "display_contents",
        "split_text",
        "empty_decorations",
        "discussion_wrappers",
        "typed_body_wrappers",
        "typed_branch_wrappers",
        "typed_empty_decorations",
    ],
)
def test_normalization_generated_presentations_preserve_post_content(
    tmp_path, component, kind
):
    full = "Original authored body. EXAMPLE.INVALID remains part of this sentence."
    with _local_context(tmp_path) as context:
        page = context.new_page()
        for depth in (0, 1, 2, 7, 31, 63):
            page.set_content(
                mobile_split_link_preview_fixture(
                    "Original authored body.",
                    full,
                    component=component,
                    partial=False,
                    body_wrappers=0,
                )
            )
            baseline_nodes = None
            if kind == "typed_body_wrappers":
                with mobile_records(page) as baseline:
                    baseline_nodes = baseline[0][1]["normalization"]["nodes"]
            page.evaluate(DOM_VARIATION_SCRIPT, {"kind": kind, "depth": depth})
            with mobile_records(page) as records:
                assert len(records) == 1, (kind, depth)
                payload = records[0][1]
                assert payload["text"] == full, (kind, depth)
                assert payload["partial"] is False
                assert payload["mediaKeys"] == ["image:123456_234567_345"]
                assert payload["normalization"]["version"] == 1
                if baseline_nodes is not None:
                    assert payload["normalization"]["nodes"] == baseline_nodes
                assert {item["name"] for item in payload["recognizers"]} >= {
                    "discussion",
                    "attachments",
                    "split_preview",
                    "unknown_controls",
                }


@pytest.mark.parametrize(
    "kind", ["body_wrappers", "display_contents", "empty_decorations"]
)
def test_normalization_generated_preview_expansion_keeps_same_owner_and_body(
    tmp_path, kind
):
    full = "Original prefix. Full authored continuation."
    with _local_context(tmp_path) as context:
        page = context.new_page()
        for depth in (0, 2, 8, 32):
            page.set_content(
                mobile_split_link_preview_fixture("Original prefix.", full)
            )
            page.evaluate(DOM_VARIATION_SCRIPT, {"kind": kind, "depth": depth})
            with mobile_records(page) as records:
                menu, payload = records[0]
                assert payload["text"] == "Original prefix."
                expanded = expand_post_preview(page, menu, payload, timeout_seconds=3)
                assert expanded["text"] == full
                assert expanded["author"] == payload["author"]


@pytest.mark.parametrize(
    "injection",
    [
        "button",
        "input",
        "unknown_link",
        "unclassified_text",
        "mixed_caption_control",
        "empty_action",
    ],
)
def test_normalization_unknown_interactive_or_unclaimed_content_rejects_body(
    tmp_path, injection
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_split_link_preview_fixture("Prefix.", "Prefix. Full body.")
        )
        page.evaluate(
            r"""kind => {
          const body = document.querySelector('[data-story]').children[2];
          const node = document.createElement(kind === 'input' ? 'input' : 'div');
          if (kind === 'button') node.setAttribute('role','button');
          if (kind === 'unknown_link') {node.setAttribute('role','link');
            node.setAttribute('data-action-id','synthetic-unknown');}
          if (kind !== 'input') node.textContent = 'Synthetic unknown region';
          if (kind === 'empty_action') {node.textContent='';
            node.setAttribute('data-action-id','synthetic-unowned-action');}
          if (kind === 'mixed_caption_control') {
            node.setAttribute('role','button');
            document.querySelector('[data-action-id="caption-link"]').append(node);
          } else body.prepend(node);
        }""",
            injection,
        )
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            assert records == []


def test_normalization_preserves_multiple_posts_with_identical_authors_and_prefixes(
    tmp_path,
):
    bodies = ["Same prefix. First distinct body.", "Same prefix. Second distinct body."]
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture(
                [
                    ("301", "Synthetic Author", bodies[0]),
                    ("302", "Synthetic Author", bodies[1]),
                ]
            )
        )
        page.evaluate(DOM_VARIATION_SCRIPT, {"kind": "branch_wrappers", "depth": 16})
        with mobile_records(page) as records:
            assert [payload["text"] for _, payload in records] == bodies
            assert [payload["actionToken"] for _, payload in records] == [
                "menu-301",
                "menu-302",
            ]


def test_normalization_limits_reject_excessive_depth_without_acceptance(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Synthetic Author", "Original body")]))
        page.evaluate(DOM_VARIATION_SCRIPT, {"kind": "body_wrappers", "depth": 210})
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize(
    "location", ["outside_card", "all_sections", "header", "footer"]
)
def test_normalization_card_and_section_wrappers_keep_distinct_ownership(
    tmp_path, location
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture(
                [
                    ("301", "Synthetic Author", "First authored body"),
                    ("302", "Synthetic Author", "Second authored body"),
                ]
            )
        )
        page.evaluate(
            r"""location => {
          for (const card of document.querySelectorAll('[data-story]')) {
            let target = location === 'header' ? card.children[1]
              : location === 'footer' ? card.children[3] : card;
            if (location === 'all_sections') {
              const inner = document.createElement('div');
              inner.setAttribute('data-mcomponent','MContainer');
              inner.append(...card.childNodes); card.append(inner); target = inner;
            }
            for (let depth = 0; depth < 16; depth++) {
              const wrap = document.createElement('div');
              wrap.setAttribute('data-mcomponent','MContainer');
              target.replaceWith(wrap); wrap.append(target); target = wrap;
            }
          }
        }""",
            location,
        )
        with mobile_records(page) as records:
            assert [payload["text"] for _, payload in records] == [
                "First authored body",
                "Second authored body",
            ]
            assert [payload["actionToken"] for _, payload in records] == [
                "menu-301",
                "menu-302",
            ]


def test_normalization_reference_comparison_same_dom_is_content_free_and_fail_closed(
    tmp_path,
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture([("301", "Synthetic Author", "Private synthetic body")])
        )
        with parser_reference(MOBILE_SCAN_SCRIPT), mobile_records(page) as records:
            assert len(records) == 1
        mismatch = MOBILE_SCAN_SCRIPT.replace(
            "author: owner,", "author: 'Different synthetic author',"
        )
        with (
            parser_reference(mismatch),
            pytest.raises(LayoutChangedError, match="reference_parser_agreement"),
            mobile_records(page),
        ):
            pytest.fail("Reference disagreement accepted")


def test_normalization_discussion_is_opaque_to_body_node_budget(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Synthetic Author", "Authored body")]))
        with mobile_records(page) as records:
            initial = records[0][1]["normalization"]["nodes"]
        page.evaluate(r"""() => {
          const comment = document.querySelector('[aria-label^="Comment by "]');
          for (let index=0; index<2000; index++) {
            const text = document.createElement('div');
            text.setAttribute('data-mcomponent','TextArea');
            text.textContent = 'Synthetic discussion text'; comment.append(text);
          }
        }""")
        with mobile_records(page) as records:
            assert records[0][1]["text"] == "Authored body"
            assert records[0][1]["normalization"]["nodes"] == initial


@pytest.mark.parametrize("style", ["display:none", "visibility:hidden", "opacity:0"])
def test_normalization_card_wrapper_cannot_expose_hidden_post(tmp_path, style):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Synthetic Author", "Hidden body")]))
        page.evaluate(
            r"""style => {
          const card=document.querySelector('[data-story]');
          const wrap=document.createElement('div'); wrap.style.cssText=style;
          card.replaceWith(wrap); wrap.append(card);
        }""",
            style,
        )
        with mobile_records(page) as records:
            assert records == []


def test_normalization_publication_label_can_be_accessible_without_timestamp_text(
    tmp_path,
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Synthetic Author", "Authored body")]))
        page.locator('[aria-label="Sunday, October 11, 2026 at 10:00 AM"]').evaluate(
            "node => {node.textContent='';node.style='width:32px;height:24px'}"
        )
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["timestamp"] == ""
            assert records[0][1]["timestampLabel"].startswith("Sunday, October 11")


def test_normalization_node_budget_is_explicit_and_does_not_accept_truncated_body(
    tmp_path,
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Synthetic Author", "Original body")]))
        page.evaluate(r"""() => {
          const body=document.querySelector('[data-story]').children[2];
          const fragment=document.createDocumentFragment();
          for(let index=0;index<13000;index++) {
            const text=document.createElement('div');
            text.setAttribute('data-mcomponent','TextArea');
            text.textContent='Synthetic paragraph'; fragment.append(text);
          }
          body.append(fragment);
        }""")
        with pytest.raises(LayoutChangedError, match="body_normalization_budget"):
            wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            assert records == []


@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize("button", [False, True])
def test_mobile_preview_marker_is_partial_and_not_post_content(
    tmp_path, component, button
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_preview_fixture(
                "Synthetic preview.",
                "Synthetic preview. Full continuation.",
                component=component,
                button=button,
            )
        )
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["partial"] is True
            assert records[0][1]["text"] == "Synthetic preview."


@pytest.mark.parametrize("source_partial", [False, True])
@pytest.mark.parametrize("target_partial", [False, True])
@pytest.mark.parametrize("panel_location", ["neither", "source", "target", "both"])
@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("button", [False, True])
def test_layout_grid_preserves_content_link_receipts_and_restart(
    tmp_path, source_partial, target_partial, panel_location, component, flat, button
):
    from fbn.monitor import MonitorService

    full = "Synthetic preview. Full continuation."
    group = parse_group_ref("contract-group")
    requests = []
    notifications = []
    with _local_context(tmp_path) as context:

        def serve(route):
            target = "/share/" in route.request.url
            requests.append(target)
            html = mobile_layout_fixture(
                partial=target_partial if target else source_partial,
                panel=panel_location
                in ({"target", "both"} if target else {"source", "both"}),
                component=component,
                button=button,
                flat=flat and target,
            )
            if target:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        sink = type(
            "RecordingSink",
            (),
            {"send": lambda self, value: notifications.append(value)},
        )()
        policy = ScanPolicy(
            sample_count=1,
            max_scrolls=0,
            navigation_timeout_seconds=5,
            settle_seconds=0,
        )
        state_path = tmp_path / "layout.sqlite3"
        with SQLiteStateRepository(state_path) as state:
            result = MonitorService(source, state, sink).run_once(
                group, policy, notify_initial=True
            )
            assert result.delivered == 1 and result.pending == 0
            assert not state.pending(group)
        # A new repository instance represents a process restart; delivery
        # receipts must suppress a second notification of the same GUID.
        with SQLiteStateRepository(state_path) as state:
            result = MonitorService(source, state, sink).run_once(group, policy)
            assert result.delivered == 0 and result.pending == 0
        assert len(notifications) == 1
        assert notifications[0].body == (
            "Synthetic Author\n"
            + full
            + "\nhttps://www.facebook.com/groups/contract-group/posts/301/"
        )
        assert requests == [False, True, False, True]
        assert not (tmp_path / "failure-screenshots").exists()


@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
def test_layout_roles_exclude_panel_media_and_preserve_repeated_paragraphs(
    tmp_path, component
):
    html = mobile_layout_fixture(panel=True, component=component)
    html = html.replace(
        "Synthetic preview. Full continuation.</div>",
        'Same paragraph.</div><div data-mcomponent="TextArea">Same paragraph.</div>',
        1,
    )
    html = html.replace(
        'class="feedback-panel">',
        'class="feedback-panel"><img src="data:image/png;base64,iVBORw0KGgo=">',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            assert len(records) == 1
            payload = records[0][1]
            assert payload["text"] == "Same paragraph. Same paragraph."
            assert payload["mediaCount"] == 0
            assert [b["role"] for b in payload["blocks"]].count("interface_panel") == 1


@pytest.mark.parametrize(
    "unknown", ["button", "feedback_variant", "ambiguous_heading", "hidden_heading"]
)
def test_unknown_interactive_body_never_qualifies_as_authored_content(
    tmp_path, unknown
):
    html = mobile_layout_fixture(panel=True)
    if unknown == "button":
        html = html.replace("Not interested", "Different action")
    elif unknown == "feedback_variant":
        html = html.replace("Are you interested in this post?", "Different heading")
    elif unknown == "hidden_heading":
        html = html.replace(
            '<div data-mcomponent="ServerTextArea">Are you interested',
            '<div data-mcomponent="ServerTextArea" style="display:none">'
            "Are you interested",
        )
    else:
        html = html.replace(
            'class="feedback-panel">',
            'class="feedback-panel"><div data-mcomponent="TextArea">'
            "Unclassified extra content</div>",
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_known_app_prompt_is_dismissed_once_before_sorting(tmp_path):
    html = (
        mobile_fixture([("301", "Author", "Body")], sorted_feed=False)
        + app_prompt_fixture()
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.get_by_role("dialog").wait_for(state="visible")
        wait_for_mobile_page(page, timeout_seconds=5)
        select_new_posts(page, timeout_seconds=5)
        assert page.evaluate("window.promptDismissals") == 1
        page.evaluate(
            "document.body.insertAdjacentHTML('beforeend', "
            "'<h2>Facebook is better on the app</h2>')"
        )
        assert page.evaluate("window.promptDismissals") == 1


def test_unknown_app_like_dialog_is_not_dismissed(tmp_path):
    import time

    from fbn.mobile import _dismiss_app_prompt

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture([("301", "Author", "Body")])
            + app_prompt_fixture(known=False)
        )
        page.get_by_role("dialog").wait_for(state="visible")
        assert not _dismiss_app_prompt(page, time.monotonic() + 5)
        assert page.evaluate("window.promptDismissals || 0") == 0
        with pytest.raises(LayoutChangedError, match="supported_blocking_dialog"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def _photo_action_shell(
    page, *, component="ImageArea", depth=0, count=1, extra="", typed=False
):
    """Synthetic family matching a retained content-free photo-shell topology."""
    page.set_content(mobile_fixture([("301", "Synthetic Author", "Placeholder")]))
    page.evaluate(
        r"""options => {
          const body = document.querySelector('[data-story]').children[2];
          const shell = document.createElement('div');
          shell.setAttribute('data-mcomponent','MContainer');
          shell.setAttribute('data-type','container');
          shell.setAttribute('data-action-id','synthetic-photo-shell');
          const layout = () => {
            const el = document.createElement('div');
            el.setAttribute('data-mcomponent','MContainer');
            el.setAttribute('data-type','container'); return el;
          };
          const album = options.typed ? layout() : shell;
          for (let i=0; i<options.count; i++) {
            const image = document.createElement('div');
            image.setAttribute('data-mcomponent',options.component);
            image.setAttribute('aria-label','Synthetic photo');
            image.style.cssText='width:32px;height:32px';
            if (options.typed) {
              const holder = layout(); holder.append(image); album.append(holder);
            } else album.append(image);
          }
          if (options.extra) {
            const tag = options.extra === 'input' ? 'input' : 'div';
            const el = document.createElement(tag);
            if (options.extra === 'plain_text') {
              el.textContent='Unowned synthetic words';
            }
            if (options.extra === 'button') el.setAttribute('role','button');
            if (options.extra === 'link') el.setAttribute('role','link');
            if (options.extra === 'empty_action') {
              el.setAttribute('data-action-id','unknown-action');
              if (options.typed) el.append(layout());
            }
            if (options.extra === 'menu') {
              el.setAttribute('aria-label','More options for Synthetic');
            }
            el.style.cssText='width:32px;height:32px';
            album.append(el);
          }
          if (options.typed) {
            const inner = layout(); inner.append(album); shell.append(inner);
          }
          let wrapped = shell;
          for (let i=0; i<options.depth; i++) {
            const outer = shell.cloneNode(false);
            outer.setAttribute('data-action-id','synthetic-outer-'+i);
            const presentation = document.createElement('div');
            if (options.typed) {
              presentation.setAttribute('data-mcomponent','MContainer');
              presentation.setAttribute('data-type','container');
            }
            presentation.append(wrapped); outer.append(presentation); wrapped=outer;
          }
          body.replaceChildren(wrapped);
        }""",
        {
            "component": component,
            "depth": depth,
            "count": count,
            "extra": extra,
            "typed": typed,
        },
    )


@pytest.mark.parametrize("component", ["ImageArea", "ServerImageArea"])
@pytest.mark.parametrize("depth", [0, 1, 7])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("typed", [False, True])
def test_photo_only_action_shells_compose_attachment_ownership(
    tmp_path, component, depth, count, typed
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        _photo_action_shell(
            page, component=component, depth=depth, count=count, typed=typed
        )
        wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            assert len(records) == 1
            payload = records[0][1]
            assert payload["text"] == ""
            assert payload["mediaCount"] == count
            assert payload["partial"] is False
            assert all(block["role"] == "attachment" for block in payload["blocks"])


@pytest.mark.parametrize(
    "extra", ["plain_text", "button", "link", "input", "empty_action", "menu"]
)
@pytest.mark.parametrize("typed", [False, True])
def test_photo_action_shell_cannot_claim_unknown_text_or_controls(
    tmp_path, extra, typed
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        _photo_action_shell(page, depth=2, extra=extra, typed=typed)
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            assert records == []


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("data-type", "unknown"),
        ("data-mcomponent", "UnknownArea"),
        ("role", "button"),
        ("aria-label", "Unknown synthetic region"),
    ],
)
def test_typed_layout_does_not_erase_semantic_boundaries(tmp_path, attribute, value):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        _photo_action_shell(page, typed=True)
        page.evaluate(
            "([name,value]) => document.querySelector("
            "'[data-action-id=synthetic-photo-shell]').firstElementChild"
            ".setAttribute(name,value)",
            [attribute, value],
        )
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            assert records == []


@pytest.mark.parametrize("kind", ["photo", "video", "link_preview", "listing", "poll"])
def test_owned_attachments_do_not_become_message_text_or_actions(tmp_path, kind):
    caption = "Original authored caption."
    html = mobile_fixture([("301", "Synthetic Author", caption)])
    image = (
        '<img style="width:32px;height:32px" '
        'src="https://scontent.example.fbcdn.net/123456_234567_345_n.jpg">'
    )
    if kind == "photo":
        attachment = (
            '<a href="https://m.facebook.com/photo/?fbid=44001">' + image + "</a>"
        )
    elif kind == "video":
        attachment = (
            '<a href="https://m.facebook.com/watch/?v=44001">'
            '<video style="width:32px;height:32px"></video></a>'
        )
    elif kind == "link_preview":
        attachment = (
            '<a href="https://example.invalid/article">'
            + image
            + '<div data-mcomponent="TextArea">Automatic link preview, '
            "not the author message</div></a>"
        )
    elif kind == "listing":
        attachment = (
            '<div role="link" href="https://m.facebook.com/marketplace/item/44001/">'
            + image
            + '<div data-mcomponent="TextArea">Item title and price</div>'
            '<button role="button">Message</button></div>'
        )
    else:
        attachment = (
            '<div role="radiogroup"><div data-mcomponent="TextArea">Poll options</div>'
            '<button role="button">First choice</button>'
            '<button role="button">Second choice</button></div>'
        )
    marker = (
        '</div><div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like,'
    )
    html = html.replace(
        marker,
        '</div><div data-mcomponent="MContainer">' + attachment + "</div>" + marker[6:],
        1,
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(html)
        wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["text"] == caption
            assert records[0][1]["previewControlCount"] == 0
            assert any(
                block["role"] == "attachment" for block in records[0][1]["blocks"]
            )


@pytest.mark.parametrize("section_root", [False, True])
@pytest.mark.parametrize("has_media", [False, True])
def test_image_card_owns_its_preview_controls_not_the_authored_message(
    tmp_path, section_root, has_media
):
    from fbn.mobile_dom import STRUCTURE_SNAPSHOT_SCRIPT

    caption = "Synthetic authored caption outside the image card."
    media = (
        '<div data-mcomponent="ImageArea"><img style="width:32px;height:32px" '
        'src="https://scontent.example.fbcdn.net/123456_234567_345_n.jpg"></div>'
        if has_media
        else ""
    )
    attachment = (
        '<div data-mcomponent="MContainer" role="img" aria-label="Image card">'
        + media
        + '<div data-mcomponent="MContainer" data-action-id="synthetic-preview">'
        '<div data-mcomponent="TextArea">Preview price</div>'
        '<div data-mcomponent="TextArea" role="button">Preview title action</div>'
        '<div data-mcomponent="TextArea">Preview location</div></div></div>'
    )
    if not section_root:
        attachment = '<div data-mcomponent="MContainer">' + attachment + "</div>"
    html = mobile_fixture([("301", "Synthetic Author", caption)])
    marker = (
        '</div><div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like,'
    )
    html = html.replace(marker, "</div>" + attachment + marker[6:], 1)
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(html)
        if not has_media:
            with pytest.raises(
                LayoutChangedError, match="supported_primary_body_controls"
            ):
                wait_for_mobile_page(page, timeout_seconds=0.1)
            snapshot = page.evaluate(STRUCTURE_SNAPSHOT_SCRIPT)
            assert any(node["block"] == "unknown" for node in snapshot["nodes"])
            return
        wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
        with mobile_records(page) as records:
            source = records[0][1]
            assert source["text"] == caption
            assert source["bodyControlCount"] == 0
            assert source["mediaKeys"] == ["image:123456_234567_345"]
        target = context.new_page()
        context.unroute("**/*")
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=html.replace("Preview title action", "Different preview title"),
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            source,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


@pytest.mark.parametrize("wrong", [False, True])
def test_media_only_destination_requires_stable_owned_attachment_id(tmp_path, wrong):
    def html(identifier):
        value = mobile_fixture([("301", "Synthetic Author", "")])
        return value.replace(
            '<div data-mcomponent="ServerTextArea"></div>',
            '<a href="https://m.facebook.com/photo/?fbid='
            + str(identifier)
            + '"><img style="width:32px;height:32px" '
            'src="data:image/png;base64,iVBORw0KGgo="></a>',
            1,
        )

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html(44001))
        with mobile_records(page) as records:
            source = records[0][1]
            assert source["text"] == "" and source["mediaKeys"] == ["attachment:44001"]
        target = context.new_page()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=html(44002 if wrong else 44001),
            ),
        )
        if wrong:
            with pytest.raises(
                LayoutChangedError, match="resolved_primary_media_matches_source"
            ):
                resolve_post_link(
                    target,
                    "https://m.facebook.com/groups/contract-group/posts/301/",
                    source,
                    frozenset({"contract-group"}),
                    timeout_seconds=0.5,
                )
        else:
            assert resolve_post_link(
                target,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=5,
            ).endswith("/posts/301/")


@pytest.mark.parametrize("kind", ["photo", "video", "photo_credit", "event"])
@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize("depth", [0, 4])
def test_attachment_control_ownership_preserves_main_post_and_link(
    tmp_path, kind, component, depth
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        html = mobile_controlled_attachment_fixture(
            kind, component=component, depth=depth
        )
        page.set_content(html)
        wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            assert len(records) == 1
            source = records[0][1]
            assert source["text"] == "Synthetic authored message. Full continuation."
            assert source["mediaCount"] == 1
            assert source["bodyControlCount"] == source["previewControlCount"] == 0
            assert source["partial"] is False
        target = context.new_page()
        context.unroute("**/*")
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=html.replace(
                    "Synthetic attachment title", "Another preview title"
                ),
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            source,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


@pytest.mark.parametrize("kind", ["photo", "video", "photo_credit", "event"])
@pytest.mark.parametrize(
    "extra",
    [
        '<button role="button" aria-label="Unknown control">Unexpected</button>',
        '<input aria-label="Unknown input">',
        '<div data-action-id="unowned-action" style="width:8px;height:8px"></div>',
        "Unowned meaningful text",
    ],
)
def test_attachment_control_ownership_rejects_unrelated_controls_or_text(
    tmp_path, kind, extra
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(mobile_controlled_attachment_fixture(kind, extra=extra))
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize("kind", ["photo", "video", "photo_credit", "event"])
def test_attachment_control_ownership_keeps_the_authored_expander(tmp_path, kind):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(mobile_controlled_attachment_fixture(kind, partial=True))
        wait_for_mobile_page(page, timeout_seconds=0.1)
        with mobile_records(page) as records:
            menu, source = records[0]
            assert source["previewControlCount"] == 1
            complete = expand_post_preview(
                page, menu, source, timeout_seconds=OFFLINE_NAVIGATION_SECONDS
            )
        assert complete["text"] == "Synthetic authored message. Full continuation."
        assert complete["partial"] is False
        assert page.evaluate("window.previewClicks") == 1


@pytest.mark.parametrize("fault", ["credit_label", "credit_text", "extra_control"])
def test_attachment_media_credit_requires_date_and_separate_owned_metadata(
    tmp_path, fault
):
    html = mobile_controlled_attachment_fixture("photo_credit")
    if fault == "credit_label":
        html = html.replace(
            'aria-label="Sunday, October 11, 2026 at 10:00 AM">Yesterday',
            'aria-label="Unclassified caption">Yesterday',
        )
    elif fault == "credit_text":
        html = html.replace(
            'aria-label="Sunday, October 11, 2026 at 10:00 AM">Yesterday',
            'aria-label="Sunday, October 11, 2026 at 10:00 AM">Unclassified caption',
        )
    else:
        html = html.replace(
            'data-action-id="synthetic-credit">',
            'data-action-id="synthetic-credit"><button role="button">Unknown</button>',
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize(
    "fault",
    ["no_image", "no_date", "unknown_root", "wrong_control_text", "empty_inner_action"],
)
def test_attachment_event_requires_a_complete_bounded_preview(tmp_path, fault):
    html = mobile_controlled_attachment_fixture("event")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(html)
        page.evaluate(
            """fault => {
            const card = document.querySelector('[data-action-id=synthetic-event]');
            if (fault === 'no_image') {
              card.querySelector('[data-mcomponent=ServerImageArea]').remove();
            }
            if (fault === 'no_date') {
              card.querySelectorAll('[data-mcomponent=ServerTextArea]')[0]
                .textContent = 'Unknown metadata';
            }
            if (fault === 'unknown_root') {
              card.setAttribute('role','button');
              card.setAttribute('aria-label','Unknown root action');
            }
            if (fault === 'wrong_control_text') {
              card.querySelector('[aria-label=Interested]')
                .textContent = 'Unrelated meaningful words';
            }
            if (fault === 'empty_inner_action') {
              const empty=document.createElement('div');
              empty.setAttribute('data-action-id','unknown-empty');
              empty.style.cssText='width:8px;height:8px';
              card.querySelector('[aria-label=Interested]').append(empty);
            }
            }""",
            fault,
        )
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize(
    "label", ["Photo", "View photo", "Image: synthetic description"]
)
def test_attachment_media_labels_are_only_evidence_with_actual_media(tmp_path, label):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(mobile_controlled_attachment_fixture("photo"))
        node = page.locator("[data-action-id=synthetic-media-open]")
        node.evaluate("(el,label) => el.setAttribute('aria-label',label)", label)
        wait_for_mobile_page(page, timeout_seconds=0.1)
        node.evaluate(
            "el => el.replaceChildren(Object.assign(document.createElement('div'),"
            "{textContent:'Unrelated authored words'}))"
        )
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_attachment_event_date_and_control_variants_preserve_ownership(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        for date in (
            "Today",
            "Tomorrow",
            "Sun, Jan 17",
            "Today at 4PM",
            "Tomorrow at 10:30 AM",
            "Jan 17, 2027 at 16:00",
            "\U000f0123 Today at 4PM",
        ):
            for label in ("Interested", "Going", "View event"):
                html = mobile_controlled_attachment_fixture("event")
                html = html.replace("Sun, Jan 17 at 4:00 PM", date)
                html = html.replace("Interested", label)
                page.set_content(html)
                wait_for_mobile_page(page, timeout_seconds=0.1)
                with mobile_records(page) as records:
                    assert records[0][1]["text"] == (
                        "Synthetic authored message. Full continuation."
                    )
                    assert records[0][1]["mediaCount"] == 1


@pytest.mark.parametrize(
    "value",
    ["Today is fun 4", "Jan 99 at 4PM", "Today at 29:99", "Jan 17, 2027 at 4PM fun"],
)
def test_attachment_event_requires_calendar_and_clock_metadata(tmp_path, value):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        html = mobile_controlled_attachment_fixture("event")
        page.set_content(html.replace("Sun, Jan 17 at 4:00 PM", value))
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize("kind", ["photo", "photo_credit", "event"])
def test_attachment_ownership_cannot_erase_unknown_media_roles(tmp_path, kind):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(mobile_controlled_attachment_fixture(kind))
        owner = "synthetic-event" if kind == "event" else "synthetic-media-open"
        page.locator(
            f'[data-action-id="{owner}"] [data-mcomponent="ServerImageArea"]'
        ).evaluate(
            """el => {
              const unknown=document.createElement('div');
              unknown.setAttribute('role','alert');
              unknown.style.cssText='width:8px;height:8px';
              el.append(unknown);
            }"""
        )
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize(
    ("kind", "category"),
    [("photo", "photo"), ("video", "video"), ("event", "event_interest")],
)
def test_attachment_control_diagnostics_never_expose_accessible_labels(
    tmp_path, kind, category
):
    from fbn.failure_screenshots import _safe_structure
    from fbn.mobile_dom import STRUCTURE_SNAPSHOT_SCRIPT

    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route("**/*", lambda route: route.abort())
        page.set_content(mobile_controlled_attachment_fixture(kind))
        wait_for_mobile_page(page, timeout_seconds=0.1)
        saved = json.loads(_safe_structure(page.evaluate(STRUCTURE_SNAPSHOT_SCRIPT)))
        assert category in {node["control"] for node in saved["nodes"]}
        assert "Synthetic" not in json.dumps(saved)
        assert "description" not in json.dumps(saved)


@pytest.mark.parametrize("kind", ["photo", "event"])
@pytest.mark.parametrize("mismatch", [False, True])
def test_attachment_control_delivery_restart_and_rejection_are_atomic(
    tmp_path, kind, mismatch
):
    from fbn.monitor import MonitorService

    group = parse_group_ref("contract-group")
    notifications = []
    with _local_context(tmp_path) as context:
        requests = []

        def serve(route):
            if route.request.resource_type != "document":
                route.abort()
                return
            target = "/share/" in route.request.url
            requests.append(target)
            html = mobile_controlled_attachment_fixture(kind, partial=not target)
            if target:
                if mismatch:
                    html = html.replace("Full continuation.", "Wrong continuation.")
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        sink = type(
            "RecordingSink", (), {"send": lambda self, n: notifications.append(n)}
        )()
        policy = ScanPolicy(
            sample_count=1,
            max_scrolls=0,
            navigation_timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            settle_seconds=0,
        )
        path = tmp_path / "attachment.sqlite3"
        with SQLiteStateRepository(path) as state:
            monitor = MonitorService(source, state, sink)
            if mismatch:
                with pytest.raises(
                    LayoutChangedError, match="resolved_primary_body_matches_source"
                ):
                    monitor.run_once(group, policy, notify_initial=True)
                assert not state.pending(group) and not notifications
                assert (
                    state._connection.execute("SELECT COUNT(*) FROM posts").fetchone()[
                        0
                    ]
                    == 0
                )
                return
            result = monitor.run_once(group, policy, notify_initial=True)
            assert result.delivered == 1 and result.pending == 0
        with SQLiteStateRepository(path) as state:
            result = MonitorService(source, state, sink).run_once(group, policy)
            assert result.delivered == result.pending == 0
        assert len(notifications) == 1
        assert notifications[0].body == (
            "Synthetic Author\nSynthetic authored message. Full continuation.\n"
            "https://www.facebook.com/groups/contract-group/posts/301/"
        )
        assert requests == [False, True, False, True]


def test_header_is_owned_by_boundaries_not_a_fixed_child_number(tmp_path):
    html = mobile_fixture([("301", "Synthetic Author", "Body")])
    marker = '<div data-mcomponent="ServerImageArea" style="height:1px"></div>'
    html = html.replace(marker, marker * 4, 1)
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            assert len(records) == 1 and records[0][1]["text"] == "Body"


def test_message_normalization_preserves_inline_text_node_boundaries(tmp_path):
    html = mobile_preview_fixture(
        "Synthetic preview.", "Synthetic preview. Full continuation."
    )
    html = html.replace(
        "Synthetic preview. <span>", "Synthetic <b>pre</b>view. <span>", 1
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            menu, before = records[0]
            assert before["text"] == "Synthetic preview."
            after = expand_post_preview(page, menu, before, timeout_seconds=5)
            assert after["text"] == "Synthetic preview. Full continuation."


@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize("source_partial", [False, True])
@pytest.mark.parametrize("target_partial", [False, True])
@pytest.mark.parametrize(
    "body_wrappers,image_wrapped", [(0, False), (1, False), (3, True)]
)
def test_split_link_preview_expands_and_verifies_only_authored_message(
    tmp_path, component, source_partial, target_partial, body_wrappers, image_wrapped
):
    preview = "Synthetic message."
    full = preview + " Visit https://example.invalid/signup for the full details."
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_split_link_preview_fixture(
                preview,
                full,
                component=component,
                partial=source_partial,
                body_wrappers=body_wrappers,
                image_wrapped=image_wrapped,
            )
        )
        with mobile_records(page) as records:
            menu, before = records[0]
            assert before["text"] == (preview if source_partial else full)
            assert before["bodyTextPartCount"] == 1
            assert any(
                b["role"] == "attachment" and b["textChars"] for b in before["blocks"]
            )
            copied = copy_post_link(page, menu, timeout_seconds=5)
            expanded = expand_post_preview(page, menu, before, timeout_seconds=5)
        assert expanded["text"] == full
        assert not expanded["partial"]
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_split_link_preview_fixture(
                    preview,
                    full,
                    component=component,
                    partial=target_partial,
                    caption="Different automatic destination preview title",
                    body_wrappers=body_wrappers,
                    image_wrapped=image_wrapped,
                )
                + "<script>history.replaceState(null,'',"
                "'/groups/contract-group/posts/301/')</script>",
            ),
        )
        target = context.new_page()
        assert resolve_post_link(
            target, copied, expanded, frozenset({"contract-group"}), timeout_seconds=5
        ).endswith("/posts/301/")


@pytest.mark.parametrize(
    "mutation",
    [
        "no_linked_media",
        "no_caption_action",
        "not_a_domain",
        "intervening_message",
        "nested_action",
        "discussion",
        "text_only_link",
    ],
)
def test_split_link_preview_requires_owned_image_domain_and_caption_action(
    tmp_path, mutation
):
    html = mobile_split_link_preview_fixture("Prefix.", "Prefix. Full body.")
    if mutation == "no_linked_media":
        html = html.replace('role="link" data-action-id="image-link"', 'role="group"')
    elif mutation == "no_caption_action":
        html = html.replace('data-action-id="caption-link"', "")
    elif mutation == "not_a_domain":
        html = html.replace("EXAMPLE.INVALID", "Authored sentence")
    elif mutation == "intervening_message":
        html = html.replace(
            '<div data-mcomponent="ImageArea" style="height:1px"></div>',
            '<div data-mcomponent="TextArea">Another authored paragraph.</div>',
        )
    elif mutation == "nested_action":
        html = html.replace(
            "Automatic preview title</div>",
            'Automatic preview title</div><div data-mcomponent="MContainer" '
            'data-action-id="unknown"></div>',
        )
    else:
        barrier = (
            '<div role="article"><div data-mcomponent="TextArea">Reply.</div></div>'
            if mutation == "discussion"
            else '<div role="link" data-action-id="other">Other link</div>'
        )
        html = html.replace(
            '<div data-mcomponent="ImageArea" style="height:1px"></div>', barrier
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        if mutation in {"text_only_link", "nested_action"}:
            # An unowned interactive link is an explicit rejection now, rather
            # than treating its caption as authored content or hiding the link.
            with pytest.raises(
                LayoutChangedError, match="supported_primary_body_controls"
            ):
                wait_for_mobile_page(page, timeout_seconds=0.1)
            return
        with mobile_records(page) as records:
            assert "Automatic preview title" in records[0][1]["text"]


@pytest.mark.parametrize("root_control", [False, True])
def test_split_link_preview_never_hides_unknown_caption_controls(
    tmp_path, root_control
):
    html = mobile_split_link_preview_fixture("Prefix.", "Prefix. Full body.")
    if root_control:
        html = html.replace(
            '<div data-mcomponent="MContainer"><div data-mcomponent="MContainer" '
            'data-action-id="caption-link">',
            '<div data-mcomponent="MContainer" data-action-id="caption-link" '
            'role="button">',
        ).replace(
            "Automatic preview title</div></div></div>",
            "Automatic preview title</div></div>",
            1,
        )
    else:
        html = html.replace(
            "Automatic preview title</div>",
            "Automatic preview title</div><button>Unknown action</button>",
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="supported_primary_body_controls"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize("unlabelled_menu", [False, True])
def test_split_link_preview_nested_discussion_never_erases_the_primary_body(
    tmp_path, unlabelled_menu
):
    full = "Prefix. Full authored body."
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_split_link_preview_fixture("Prefix.", full, body_wrappers=0)
        )
        page.evaluate(
            "() => {const section=document.querySelector('[data-story]').children[2];"
            "const wrap=document.createElement('div');"
            "wrap.setAttribute('data-mcomponent','MContainer');"
            "wrap.append(...section.childNodes);section.append(wrap);}"
        )
        if unlabelled_menu:
            page.locator('[role="article"]').evaluate(
                "node => {node.removeAttribute('role');"
                "node.removeAttribute('aria-label')}"
            )
            with pytest.raises(LayoutChangedError, match="unique_primary_menu"):
                wait_for_mobile_page(page, timeout_seconds=0.1)
        else:
            with mobile_records(page) as records:
                assert len(records) == 1 and records[0][1]["text"] == "Prefix."
                expanded = expand_post_preview(page, *records[0], timeout_seconds=3)
            assert expanded["text"] == full


def test_split_link_preview_keeps_strict_message_and_destination_checks(tmp_path):
    full = "Prefix. Authored full body mentioning EXAMPLE.INVALID and preview title."
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_split_link_preview_fixture("Prefix.", "Changed body."))
        with (
            mobile_records(page) as records,
            pytest.raises(
                LayoutChangedError, match="expanded_primary_body_preserves_preview"
            ),
        ):
            expand_post_preview(page, *records[0], timeout_seconds=2)
        page.set_content(mobile_split_link_preview_fixture("Prefix.", full))
        with mobile_records(page) as records:
            menu, before = records[0]
            expanded = expand_post_preview(page, menu, before, timeout_seconds=5)
        assert expanded["text"] == full
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_split_link_preview_fixture(
                    "Prefix.", "Prefix. Different full body.", partial=False
                ),
            ),
        )
        with pytest.raises(
            LayoutChangedError, match="resolved_primary_body_matches_source"
        ):
            resolve_post_link(
                context.new_page(),
                "https://m.facebook.com/groups/contract-group/posts/301/",
                expanded,
                frozenset({"contract-group"}),
                timeout_seconds=2,
            )


@pytest.mark.parametrize("mismatch", [False, True])
def test_split_link_preview_delivery_restart_and_rejection_are_atomic(
    tmp_path, mismatch
):
    from fbn.monitor import MonitorService

    full = "Prefix. Full authored body with https://example.invalid/signup."
    group = parse_group_ref("contract-group")
    notifications = []
    with _local_context(tmp_path) as context:

        def serve(route):
            target = "/share/" in route.request.url
            body = "Prefix. Wrong complete body." if target and mismatch else full
            html = mobile_split_link_preview_fixture("Prefix.", body)
            if target:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        sink = type(
            "RecordingSink", (), {"send": lambda self, n: notifications.append(n)}
        )()
        policy = ScanPolicy(
            sample_count=1,
            max_scrolls=0,
            navigation_timeout_seconds=3,
            settle_seconds=0,
        )
        path = tmp_path / "split.sqlite3"
        with SQLiteStateRepository(path) as state:
            monitor = MonitorService(source, state, sink)
            if mismatch:
                with pytest.raises(
                    LayoutChangedError, match="resolved_primary_body_matches_source"
                ):
                    monitor.run_once(group, policy, notify_initial=True)
                assert not state.pending(group) and not notifications
                assert (
                    state._connection.execute("SELECT COUNT(*) FROM posts").fetchone()[
                        0
                    ]
                    == 0
                )
                return
            result = monitor.run_once(group, policy, notify_initial=True)
            assert result.delivered == 1 and result.pending == 0
        with SQLiteStateRepository(path) as state:
            result = MonitorService(source, state, sink).run_once(group, policy)
            assert result.delivered == result.pending == 0
        assert len(notifications) == 1
        assert notifications[0].body == (
            "Synthetic Author\n"
            + full
            + "\nhttps://www.facebook.com/groups/contract-group/posts/301/"
        )


@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize(
    "behavior",
    ["expand", "replace_card", "footer_loading", "action_refresh", "reuse_menu"],
)
@pytest.mark.parametrize("target_preview", [False, True])
def test_mobile_fetch_expands_inline_preview_and_verifies_the_full_body(
    tmp_path, component, behavior, target_preview
):
    full = "Synthetic preview. Full continuation that was hidden in the group feed."
    with _local_context(tmp_path) as context:
        requests = []

        def routes(route):
            is_post = "/share/" in route.request.url
            if is_post:
                assert route.request.url.endswith("/share/p/301/")
            requests.append(is_post)
            html = (
                (
                    mobile_preview_fixture(
                        "Synthetic preview.",
                        full,
                        component=component,
                        delay_ms=120,
                        behavior="action_refresh"
                        if behavior == "action_refresh"
                        else "expand",
                    )
                    if target_preview
                    else mobile_fixture([("301", "Synthetic Author", full)])
                )
                if is_post
                else mobile_preview_fixture(
                    "Synthetic preview.",
                    full,
                    component=component,
                    delay_ms=120,
                    behavior=behavior,
                )
            )
            if is_post:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        result = source.fetch_recent(
            parse_group_ref("contract-group"),
            ScanPolicy(
                sample_count=1,
                max_scrolls=1,
                navigation_timeout_seconds=5,
                settle_seconds=0,
            ),
            boundary_post_id="301",
        )
        assert len(result.posts) == 1
        assert result.posts[0].text == full
        assert result.posts[0].post_id == "301"
        assert not result.posts[0].partial
        assert requests == [False, True]
        assert not (tmp_path / "failure-screenshots").exists()


@pytest.mark.parametrize("component", ["TextArea", "ServerTextArea"])
@pytest.mark.parametrize("matching", [False, True])
def test_mobile_permalink_preview_expands_within_scroller_and_requires_full_equality(
    tmp_path, component, matching
):
    full = "Synthetic prefix. Full source body."
    target_body = full if matching else "Synthetic prefix. Wrong full target body."
    html = mobile_preview_fixture("Synthetic prefix.", target_body, component=component)
    # Real single-post views can put sections directly in the scroller.
    html += (
        "<script>const root=document.querySelector('[data-type=vscroller]');"
        "root.firstElementChild.remove();"
        "const card=root.querySelector('[data-story]');"
        "card.firstElementChild.remove();"
        "card.replaceWith(...card.childNodes);</script>"
    )
    with _local_context(tmp_path) as context:
        source = context.new_page()
        source.set_content(mobile_fixture([("301", "Synthetic Author", full)]))
        with mobile_records(source) as records:
            payload = records[0][1]
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )
        target = context.new_page()
        if matching:
            assert resolve_post_link(
                target,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                payload,
                frozenset({"contract-group"}),
                timeout_seconds=5,
            ).endswith("/posts/301/")
        else:
            with pytest.raises(
                LayoutChangedError, match="resolved_primary_body_matches_source"
            ):
                resolve_post_link(
                    target,
                    "https://m.facebook.com/groups/contract-group/posts/301/",
                    payload,
                    frozenset({"contract-group"}),
                    timeout_seconds=5,
                )
        assert target.evaluate("window.previewClicks") == 1


@pytest.mark.parametrize("action_region", [False, True])
def test_mobile_expansion_waits_for_complete_body_without_fixed_sleep(
    tmp_path, action_region
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_preview_fixture(
                "Synthetic prefix.",
                "Synthetic prefix. Complete body.",
                action_region=action_region,
                delay_ms=150,
            )
        )
        with mobile_records(page) as records:
            menu, preview = records[0]
            expanded = expand_post_preview(page, menu, preview, timeout_seconds=5)
        assert expanded["text"] == "Synthetic prefix. Complete body."
        assert not expanded["partial"]
        assert page.evaluate("window.previewClicks") == 1


@pytest.mark.parametrize(
    "behavior,complete",
    [
        ("author", "Synthetic prefix. Full body."),
        ("publication", "Synthetic prefix. Full body."),
        ("replace_menu", "Synthetic prefix. Full body."),
        ("expand", "Unrelated complete body."),
        ("expand", ""),
        ("never", "Synthetic prefix. Full body."),
    ],
)
def test_mobile_expansion_rejects_changed_owner_date_menu_body_or_incomplete_render(
    tmp_path, behavior, complete
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_preview_fixture("Synthetic prefix.", complete, behavior=behavior)
        )
        error = TransientNavigationError if behavior == "never" else LayoutChangedError
        with mobile_records(page) as records, pytest.raises(error):
            expand_post_preview(page, *records[0], timeout_seconds=2)
        assert page.evaluate("window.previewClicks") == 1


@pytest.mark.parametrize(
    "body",
    [
        '<div data-mcomponent="ServerTextArea">Normal text saying see more.</div>',
        '<div data-mcomponent="TextArea" data-action-id="literal">'
        "<span>See more</span></div>",
        '<div data-mcomponent="ServerTextArea">Normal text. '
        '<a href="https://example.invalid/">...see more</a></div>',
        '<div role="article"><div data-mcomponent="TextArea" data-action-id="quoted">'
        "Quoted preview <button>See more</button></div></div>",
        '<div aria-label="Comment by Someone"><div data-mcomponent="TextArea" '
        'data-action-id="comment">Reply preview <span>...see more</span></div></div>',
    ],
)
def test_mobile_preview_detection_ignores_post_words_links_quotes_and_replies(
    tmp_path, body
):
    html = mobile_fixture([("301", "Author", "Primary body")]).replace(
        '<div data-mcomponent="ServerTextArea">Primary body</div>',
        '<div data-mcomponent="ServerTextArea">Primary body</div>' + body,
        1,
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            assert len(records) == 1
            assert not records[0][1]["partial"]
            assert records[0][1]["previewControlCount"] == 0


def test_mobile_expansion_requires_one_control_before_clicking(tmp_path):
    html = mobile_preview_fixture(
        "Synthetic prefix.", "Synthetic prefix. Complete body."
    )
    html = html.replace(
        "<span>...see more</span>", "<span>...see more</span><span>...see more</span>"
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            assert records[0][1]["previewControlCount"] == 2
            with pytest.raises(
                LayoutChangedError, match="unique_primary_preview_control"
            ):
                expand_post_preview(page, *records[0], timeout_seconds=2)
        assert page.evaluate("window.previewClicks || 0") == 0


def test_mobile_full_target_must_match_expanded_source_not_only_preview_prefix(
    tmp_path,
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_preview_fixture(
                "Synthetic prefix.", "Synthetic prefix. Full source body."
            )
        )
        with mobile_records(page) as records:
            expanded = expand_post_preview(page, *records[0], timeout_seconds=5)
        target = context.new_page()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture(
                    [
                        (
                            "301",
                            "Synthetic Author",
                            "Synthetic prefix. Wrong full target body.",
                        )
                    ]
                ),
            ),
        )
        with pytest.raises(
            LayoutChangedError, match="resolved_primary_body_matches_source"
        ):
            resolve_post_link(
                target,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                expanded,
                frozenset({"contract-group"}),
                timeout_seconds=2,
            )


@pytest.mark.parametrize(
    ("html", "error"),
    [
        ('<input name="email">', AuthenticationRequiredError),
        ('<input name="approvals_code">', AccountActionRequiredError),
        ('<iframe src="about:blank#captcha"></iframe>', AccountActionRequiredError),
        ("<main>Content isn't available</main>", AccessDeniedError),
        ("<main>Temporarily blocked</main>", TransientNavigationError),
        ("<main>Unknown layout</main>", LayoutChangedError),
    ],
)
def test_mobile_dom_failure_states_stop_without_reading_posts(tmp_path, html, error):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(error):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_mobile_valid_post_quoting_error_text_is_still_a_post(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture(
                [
                    (
                        "101",
                        "Synthetic Author",
                        "Content isn't available. Temporarily blocked.",
                    )
                ]
            )
        )
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED


@pytest.fixture
def reader_contract(tmp_path: Path) -> Iterator[object]:
    group = parse_group_ref("contract-group")
    observed_at = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def read(stories: list[tuple[str, str, str]]) -> tuple:
            by_id = {story[0]: story for story in stories}

            def route_mobile(route):
                path = route.request.url.split("?")[0].rstrip("/").split("/")
                is_post = "share" in path or "posts" in path
                chosen = [by_id[path[-1]]] if is_post else stories
                html = mobile_fixture(chosen)
                if "share" in path:
                    # Playwright routes only the first request of an HTTP
                    # redirect chain. Simulate its final URL locally so the
                    # fixture never attempts the redirected network request.
                    html += (
                        "<script>history.replaceState(null,'','/groups/"
                        f"{group.key}/posts/{path[-1]}/')</script>"
                    )
                route.fulfill(status=200, content_type="text/html", body=html)

            context.route("**/*", route_mobile)
            page.goto(f"https://m.facebook.com/groups/{group.key}/")
            target = context.new_page()
            payloads = []
            try:
                with mobile_records(page) as records:
                    for menu, payload in records:
                        copied = copy_post_link(
                            page, menu, timeout_seconds=OFFLINE_NAVIGATION_SECONDS
                        )
                        href = resolve_post_link(
                            target,
                            copied,
                            payload,
                            frozenset({group.key}),
                            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
                        )
                        payloads.append({**payload, "href": href})
                return extract_posts(payloads, group, observed_at, limit=50)
            finally:
                target.close()
                context.unroute("**/*", route_mobile)

        try:
            yield read
        finally:
            page.close()


def test_reader_contract_main_post_content_and_link_stay_together(reader_contract):
    posts = reader_contract(
        [("301", "First Author", "First body"), ("302", "Second Author", "Second body")]
    )
    assert [(post.post_id, post.author, post.text) for post in posts] == [
        ("301", "First Author", "First body"),
        ("302", "Second Author", "Second body"),
    ]
    for post in posts:
        assert post.url.endswith(f"/posts/{post.post_id}/")
        assert "Reply" not in post.text
        assert "Quoted" not in post.text


def test_reader_contract_duplicate_cards_do_not_duplicate_posts(reader_contract):
    posts = reader_contract(
        [("301", "First Author", "First body"), ("301", "First Author", "First body")]
    )
    assert [post.post_id for post in posts] == ["301"]


def test_reader_contract_catches_every_post_since_marker(reader_contract, tmp_path):
    group = parse_group_ref("contract-group")
    with SQLiteStateRepository(tmp_path / "contract.sqlite3") as state:
        baseline = state.observe(
            group, reader_contract([("300", "Author", "Previous scan")])
        )
        assert baseline.baseline and not baseline.pending
        batch = state.observe(
            group,
            reader_contract(
                [
                    ("302", "Author", "Newest post"),
                    ("301", "Author", "Post missed between checks"),
                    ("300", "Author", "Previous scan"),
                ]
            ),
        )
        assert {item.post_id for item in batch.pending} == {"301", "302"}


def test_reader_contract_missing_marker_keeps_gap_open(reader_contract, tmp_path):
    group = parse_group_ref("contract-group")
    with SQLiteStateRepository(tmp_path / "contract.sqlite3") as state:
        state.observe(group, reader_contract([("300", "Author", "Previous scan")]))
        first = state.observe(group, reader_contract([("302", "Author", "Newest")]))
        state.mark_delivered([item.event_id for item in first.pending])
        assert state.notification_boundary(group) == "300"
        later = state.observe(
            group,
            reader_contract(
                [
                    ("303", "Author", "Newer"),
                    ("302", "Author", "Newest"),
                    ("301", "Author", "Only exposed on a later check"),
                ]
            ),
        )
        assert {item.post_id for item in later.pending} == {"301", "303"}
        assert state.notification_boundary(group) == "300"


def test_reader_contract_restart_does_not_repeat_delivered_posts(
    reader_contract, tmp_path
):
    group = parse_group_ref("contract-group")
    path = tmp_path / "contract.sqlite3"
    stories = [("301", "Author", "Already delivered")]
    with SQLiteStateRepository(path) as state:
        batch = state.observe(group, reader_contract(stories), notify_initial=True)
        state.mark_delivered([item.event_id for item in batch.pending])
    with SQLiteStateRepository(path) as state:
        repeated = state.observe(group, reader_contract(stories))
        assert repeated.inserted == repeated.queued == 0
        assert not repeated.pending


def test_reader_contract_identical_content_can_belong_to_distinct_posts(
    reader_contract,
):
    posts = reader_contract(
        [
            ("301", "Author", "Repeated announcement"),
            ("302", "Author", "Repeated announcement"),
        ]
    )
    assert [post.post_id for post in posts] == ["301", "302"]


def test_mobile_selects_new_posts_instead_of_trusting_the_url(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_fixture([("301", "Author", "Body")], sorted_feed=False))
        select_new_posts(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
        assert page.locator("#sort-label").inner_text() == "New posts"
        assert not page.locator("#sort-menu").is_visible()
        assert (
            wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
            is PageState.FEED
        )


def test_mobile_excludes_discussion_nested_inside_the_body_text_component(tmp_path):
    html = mobile_fixture([("301", "Author", "Primary body")]).replace(
        ">Primary body</div>",
        '>Primary body<span aria-label="Comment by Another">Hidden reply</span></div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["text"] == "Primary body"


@pytest.mark.parametrize("hydrated", [False, True])
@pytest.mark.parametrize("comment_label", ["\U000f0379 2comments", "\U000f0379comment"])
def test_mobile_reads_hydrated_text_and_icon_prefixed_actions(
    tmp_path, hydrated, comment_label
):
    html = mobile_fixture([("301", "Author", "Primary body")], sorted_feed=False)
    if hydrated:
        html = html.replace("ServerTextArea", "TextArea").replace(
            "ServerImageArea", "ImageArea"
        )
    html = html.replace(
        "Like, press and hold for more reactions",
        "2like, double-tap and hold for more reactions",
    ).replace("Make a comment", comment_label)
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        select_new_posts(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
        assert (
            wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
            is PageState.FEED
        )
        with mobile_records(page) as records:
            assert len(records) == 1
            assert records[0][1]["author"] == "Author"
            assert records[0][1]["text"] == "Primary body"


def test_mobile_reply_actions_cannot_replace_the_primary_footer(tmp_path):
    html = mobile_fixture([("301", "Author", "Primary body")]).replace(
        'aria-label="Make a comment"', 'aria-label="Reply"'
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="primary_action_footer"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_mobile_waits_for_all_primary_cards_to_finish_loading(tmp_path):
    html = mobile_fixture([("301", "Author", "Body"), ("302", "Other", "Body")])
    html = html.replace(
        'aria-label="More options for Other"',
        'aria-label="More options for Other" id="loading-menu"',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.evaluate("""() => {
            const timestamp = document.getElementById('loading-menu').parentElement
                .querySelector('[data-mcomponent=ServerTextArea][aria-label]');
            const label = timestamp.getAttribute('aria-label');
            timestamp.removeAttribute('aria-label');
            setTimeout(() => timestamp.setAttribute('aria-label', label), 100);
        }""")
        assert (
            wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
            is PageState.FEED
        )


@pytest.mark.parametrize("permalink", [False, True])
@pytest.mark.parametrize(
    ("late_view", "error"),
    [
        ("complete", TransientNavigationError),
        ("blank", TransientNavigationError),
        ("login", AuthenticationRequiredError),
        ("checkpoint", AccountActionRequiredError),
        ("unknown", LayoutChangedError),
    ],
)
def test_mobile_deadline_rechecks_a_late_render_or_security_transition(
    tmp_path, monkeypatch, permalink, late_view, error
):
    from fbn import mobile

    views = {
        "complete": mobile_fixture([("301", "Author", "Body")]),
        "blank": mobile_loading_shell_fixture(),
        "login": '<input name="email">',
        "checkpoint": '<input name="approvals_code">',
        "unknown": "<p>Unknown visible UI</p>",
    }
    original = mobile.read_page_signals
    transitioned = False

    def transition_after_signals(page, **kwargs):
        nonlocal transitioned
        signals = original(page, **kwargs)
        if not transitioned:
            page.set_content(views[late_view])
            transitioned = True
        return signals

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_loading_shell_fixture())
        monkeypatch.setattr(mobile, "read_page_signals", transition_after_signals)
        with pytest.raises(error) as failure:
            wait_for_mobile_page(page, timeout_seconds=0, permalink=permalink)
        assert failure.value._fbn_failure_phase == "page_render"
        assert transitioned


def test_mobile_waits_for_scroller_hydration_without_reloading(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        'data-type="vscroller"', 'data-type="loading" id="loading-scroller"'
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.evaluate("""() => {
            setTimeout(() => document.getElementById('loading-scroller')
                .setAttribute('data-type', 'vscroller'), 350);
        }""")
        assert (
            wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
            is PageState.FEED
        )
        with mobile_records(page) as records:
            assert len(records) == 1


@pytest.mark.parametrize("permalink", [False, True])
@pytest.mark.parametrize("accessible_spinner", [False, True])
def test_mobile_blank_loading_shell_times_out_as_transient_without_reloading(
    tmp_path, monkeypatch, permalink, accessible_spinner
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type("Log", (), {"warning": lambda self, message, **kw: logged.append(kw)})(),
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        requests = []
        context.route("**/*", lambda route: requests.append(route.request.url))
        page.set_content(
            mobile_loading_shell_fixture(accessible_spinner=accessible_spinner)
        )
        with pytest.raises(TransientNavigationError, match="blank loading shell"):
            wait_for_mobile_page(page, timeout_seconds=0.05, permalink=permalink)
        assert requests == []
    snapshot = logged[0]
    assert snapshot["document_ready"] == "complete"
    assert snapshot["visible_text_chars"] == snapshot["visible_controls"] == 0
    assert snapshot["loading_indicators"] == int(accessible_spinner)
    assert "Hidden fallback" not in str(logged)


@pytest.mark.parametrize("permalink", [False, True])
@pytest.mark.parametrize("accessible_spinner", [False, True])
@pytest.mark.parametrize(
    "phase", ["find_primary_preview", "expanded_primary_preview_render"]
)
def test_preview_loading_transition_uses_shared_transient_classification(
    tmp_path, monkeypatch, permalink, accessible_spinner, phase
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type("Log", (), {"warning": lambda self, message, **kw: logged.append(kw)})(),
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_preview_fixture("Synthetic prefix.", "Full body."))
        shell = mobile_loading_shell_fixture(accessible_spinner=accessible_spinner)
        requests = []
        context.route("**/*", lambda route: requests.append(route.request.url))
        with mobile_records(page, permalink=permalink) as records:
            if phase == "find_primary_preview":
                page.set_content(shell)
            else:
                page.evaluate(
                    "html => window.expandPreview = () => {"
                    "document.body.innerHTML=html}",
                    shell,
                )
            with pytest.raises(
                TransientNavigationError, match="blank loading shell"
            ) as failure:
                expand_post_preview(
                    page,
                    *records[0],
                    timeout_seconds=0.2
                    if phase == "find_primary_preview"
                    else OFFLINE_NAVIGATION_SECONDS,
                    permalink=permalink,
                )
        assert failure.value._fbn_failure_phase == phase
        assert requests == []
    snapshots = [fields for fields in logged if "document_ready" in fields]
    assert len(snapshots) == 1
    assert snapshots[0]["phase"] == phase
    assert snapshots[0]["page_kind"] == ("permalink" if permalink else "feed")
    assert snapshots[0]["visible_text_chars"] == 0
    assert "Synthetic prefix" not in str(logged)


@pytest.mark.parametrize("permalink", [False, True])
@pytest.mark.parametrize(
    ("late_ui", "error"),
    [
        ("<p>Unknown visible content</p>", LayoutChangedError),
        ('<button aria-label="Unknown action"></button>', LayoutChangedError),
        ('<svg width="50" height="50"></svg>', LayoutChangedError),
        ('<input name="email">', AuthenticationRequiredError),
        ('<input name="approvals_code">', AccountActionRequiredError),
        ("<main>Content isn't available</main>", AccessDeniedError),
    ],
)
def test_preview_loading_cannot_hide_unknown_layout_or_security(
    tmp_path, permalink, late_ui, error
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_preview_fixture("Synthetic prefix.", "Full body."))
        with mobile_records(page, permalink=permalink) as records:
            page.set_content(mobile_loading_shell_fixture() + late_ui)
            with pytest.raises(error) as failure:
                expand_post_preview(
                    page, *records[0], timeout_seconds=0.2, permalink=permalink
                )
        assert not isinstance(failure.value, TransientNavigationError)
        assert failure.value._fbn_failure_phase == "find_primary_preview"


@pytest.mark.parametrize("permalink", [False, True])
@pytest.mark.parametrize("timeout_kind", ["action_budget", "browser_waiter"])
@pytest.mark.parametrize(
    ("late_ui", "error"),
    [
        (None, TransientNavigationError),
        ("", TransientNavigationError),
        ("<p>Unknown visible content</p>", LayoutChangedError),
        ('<button aria-label="Unknown action"></button>', LayoutChangedError),
        ('<input name="email">', AuthenticationRequiredError),
        ('<input name="approvals_code">', AccountActionRequiredError),
        ("<main>Content isn't available</main>", AccessDeniedError),
    ],
)
def test_preview_timeout_still_checks_current_security_and_layout(
    tmp_path, monkeypatch, permalink, timeout_kind, late_ui, error
):
    from fbn import mobile

    original_remaining = mobile._remaining_seconds

    def expired_find(deadline, phase):
        if phase == "find_primary_preview":
            failure = TransientNavigationError("Synthetic action budget exhausted")
            mobile._annotate_failure(failure, phase)
            raise failure
        return original_remaining(deadline, phase)

    def expired_waiter(*args, **kwargs):
        raise PlaywrightTimeoutError("Synthetic browser waiter deadline")

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_preview_fixture("Synthetic prefix.", "Full body."))
        with mobile_records(page, permalink=permalink) as records:
            if late_ui is not None:
                page.set_content(mobile_loading_shell_fixture() + late_ui)
            if timeout_kind == "action_budget":
                monkeypatch.setattr(mobile, "_remaining_seconds", expired_find)
            else:
                monkeypatch.setattr(page, "wait_for_function", expired_waiter)
            with pytest.raises(error) as failure:
                expand_post_preview(
                    page, *records[0], timeout_seconds=5, permalink=permalink
                )
        assert failure.value._fbn_failure_phase == "find_primary_preview"


@pytest.mark.parametrize("permalink", [False, True])
def test_preview_loading_after_navigation_preserves_identity_failure(
    tmp_path, permalink
):
    with _local_context(tmp_path) as context:
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_preview_fixture("Synthetic prefix.", "Full body."),
            ),
        )
        page = context.new_page()
        page.goto("https://m.facebook.com/groups/contract-group/posts/301/")
        with mobile_records(page, permalink=permalink) as records:
            page.evaluate(
                "html => window.expandPreview = () => {"
                "history.pushState(null,'','/groups/contract-group/posts/999/');"
                "document.body.innerHTML=html}",
                mobile_loading_shell_fixture(),
            )
            with pytest.raises(
                LayoutChangedError, match="expanded_primary_url_unchanged"
            ):
                expand_post_preview(
                    page, *records[0], timeout_seconds=0.3, permalink=permalink
                )


@pytest.mark.parametrize("phase", ["open_sort", "select_new_posts"])
def test_sort_loading_transition_uses_shared_transient_classification(tmp_path, phase):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        shell = mobile_loading_shell_fixture()
        if phase == "open_sort":
            page.set_content(shell)
        else:
            page.set_content(
                mobile_fixture([("301", "Author", "Body")], sorted_feed=False)
            )
            page.evaluate(
                "html => document.querySelector('button').onclick = () => {"
                "document.body.innerHTML=html}",
                shell,
            )
        with pytest.raises(
            TransientNavigationError, match="blank loading shell"
        ) as failure:
            select_new_posts(page, timeout_seconds=0.2)
        assert failure.value._fbn_failure_phase == phase


@pytest.mark.parametrize(
    "visible_ui",
    [
        "<p>Unknown layout</p>",
        '<button aria-label="Unknown action"></button>',
        '<img alt="Unknown content" style="width:50px;height:50px">',
        '<svg width="50" height="50"></svg>',
    ],
)
def test_loading_spinner_cannot_disguise_an_unsupported_visible_layout(
    tmp_path, visible_ui
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_loading_shell_fixture() + visible_ui)
        with pytest.raises(LayoutChangedError, match="unique_mobile_scroller"):
            wait_for_mobile_page(page, timeout_seconds=0.05)


@pytest.mark.parametrize("permalink", [False, True])
def test_mobile_blank_shell_waits_for_actual_content_without_reload(
    tmp_path, permalink
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_loading_shell_fixture())
        page.evaluate(
            "html => setTimeout(() => {document.open();document.write(html);"
            "document.close();}, 100)",
            mobile_fixture([("301", "Author", "Full body")]),
        )
        assert (
            wait_for_mobile_page(
                page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS, permalink=permalink
            )
            is PageState.FEED
        )
        with mobile_records(page, permalink=permalink) as records:
            assert records[0][1]["text"] == "Full body"


@pytest.mark.parametrize(
    ("terminal", "error"),
    [
        ('<input name="email">', AuthenticationRequiredError),
        ('<input name="approvals_code">', AccountActionRequiredError),
        ("<main>Content isn't available</main>", AccessDeniedError),
        ("<main>Temporarily blocked</main>", TransientNavigationError),
    ],
)
def test_loading_shell_does_not_hide_terminal_security_or_access_state(
    tmp_path, terminal, error
):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(mobile_loading_shell_fixture() + terminal)
        with pytest.raises(error) as failure:
            wait_for_mobile_page(page, timeout_seconds=0.05, permalink=True)
        assert "blank loading shell" not in str(failure.value)


def test_permalink_shell_after_readable_render_is_transient_and_captures_evidence(
    tmp_path, monkeypatch
):
    from fbn import mobile
    from fbn.failure_screenshots import capture_failure_screenshots

    original_records = mobile.mobile_records

    @contextmanager
    def replace_with_shell(page, **kwargs):
        page.set_content(mobile_loading_shell_fixture())
        with original_records(page, **kwargs) as records:
            yield records

    monkeypatch.setattr(mobile, "mobile_records", replace_with_shell)
    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)
        feed = context.new_page()
        feed.set_content(mobile_fixture([("301", "Author", "Full body")]))
        with original_records(feed) as records:
            payload = records[0][1]
        requests = []

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture([("301", "Author", "Full body")]),
            )

        context.route("**/*", serve)
        with (
            pytest.raises(TransientNavigationError, match="blank loading shell"),
            mobile._post_target(
                context,
                on_error=lambda target, error: capture_failure_screenshots(
                    tmp_path / "profile", error, {"post": target, "feed": feed}
                ),
            ) as target,
        ):
            resolve_post_link(
                target,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                payload,
                frozenset({"contract-group"}),
                timeout_seconds=1,
            )
        assert len(requests) == 1
        assert target.is_closed()
        assert tuple(context.pages) == (*original_pages, feed)
    evidence = list((tmp_path / "failure-screenshots").iterdir())
    assert len(evidence) == 1
    assert {"feed.png", "post.png", "metadata.json"} <= {
        path.name for path in evidence[0].iterdir()
    }


def test_missing_mobile_scroller_logs_structure_without_page_content(
    tmp_path, monkeypatch
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type("Log", (), {"warning": lambda self, message, **kw: logged.append(kw)})(),
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            '<div data-mcomponent="MScreen"><div role="progressbar"></div>'
            "Synthetic private body must not appear in diagnostics</div>"
        )
        with pytest.raises(LayoutChangedError, match="unique_mobile_scroller"):
            wait_for_mobile_page(page, timeout_seconds=0.05, permalink=True)
    snapshot = logged[0]
    assert snapshot["page_kind"] == "permalink"
    assert snapshot["mobile_screens"] == snapshot["loading_indicators"] == 1
    assert snapshot["total_scrollers"] == snapshot["visible_scrollers"] == 0
    assert "Synthetic private body" not in str(logged)
    assert "facebook.com" not in str(logged)


@pytest.mark.parametrize(
    "failure_kind", ["initial", "feed_preview", "permalink_preview"]
)
@pytest.mark.parametrize("failed_attempts", [1, 2])
def test_loading_shell_in_check_retry_verifies_and_delivers_once_end_to_end(
    tmp_path, monkeypatch, failure_kind, failed_attempts
):
    from datetime import timedelta
    from types import SimpleNamespace

    from fbn import mobile
    from fbn.config import ScheduleSettings
    from fbn.exceptions import RenderTimeoutError
    from fbn.models import Post
    from fbn.monitor import MonitorService
    from fbn.scheduling import MonitorLoop

    group = parse_group_ref("contract-group")
    now = [datetime(2026, 10, 20, tzinfo=timezone.utc)]
    attempts, waits, notifications = [], [], []
    original_readiness = mobile.wait_for_mobile_page

    def bounded_loading_readiness(page, **kwargs):
        # Shorten only the blank-shell readiness wait, never navigation to it.
        if (
            failure_kind == "initial"
            and len(attempts) <= failed_attempts
            and kwargs.get("permalink")
        ):
            kwargs["timeout_seconds"] = 0.1
        return original_readiness(page, **kwargs)

    monkeypatch.setattr(mobile, "wait_for_mobile_page", bounded_loading_readiness)
    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)
        stories = [("301", "Author", "New body"), ("300", "Author", "Old body")]

        def preview(html):
            shell = json.dumps(mobile_loading_shell_fixture())
            return html.replace(
                '<div data-mcomponent="ServerTextArea">New body</div>',
                '<div data-mcomponent="TextArea" data-action-id="preview-301" '
                'onclick="expandSynthetic(this)">New <span>...see more</span></div>',
                1,
            ) + (
                "<script>function expandSynthetic(node){"
                + (
                    f"document.body.innerHTML={shell};"
                    if len(attempts) <= failed_attempts
                    else 'node.textContent="New body";'
                )
                + "}</script>"
            )

        def serve(route):
            if "/share/" in route.request.url:
                number = route.request.url.rstrip("/").split("/")[-1]
                html = mobile_fixture([s for s in stories if s[0] == number])
                if number == "301":
                    html = html.replace("10:00 AM", "11:00 AM")
                html += (
                    "<script>history.replaceState(null,'',"
                    f"'/groups/contract-group/posts/{number}/')</script>"
                )
                if failure_kind == "initial" and len(attempts) <= failed_attempts:
                    html = mobile_loading_shell_fixture()
                elif failure_kind == "permalink_preview" and number == "301":
                    html = preview(html)
            else:
                # Distinct publication times avoid the intentional coarse-age
                # tie rule that conservatively includes unsent tied posts.
                html = mobile_fixture(stories).replace("10:00 AM", "11:00 AM", 1)
                if failure_kind == "feed_preview":
                    html = preview(html)
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "profile"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context

        def acquire(*args, **kwargs):
            attempts.append(now[0])
            monkeypatch.setattr(
                mobile,
                "MAX_LINK_SECONDS",
                OFFLINE_NAVIGATION_SECONDS,
            )
            try:
                return source.fetch_recent(*args, **kwargs)
            except TransientNavigationError as error:
                assert isinstance(error, RenderTimeoutError)
                raise

        with SQLiteStateRepository(tmp_path / "state.sqlite3") as state:
            state.observe(
                group,
                [
                    Post(
                        group.key,
                        "300",
                        group.url + "posts/300/",
                        "Old body",
                        "Author",
                        now[0],
                        0,
                    )
                ],
                observed_at=now[0],
            )
            service = MonitorService(
                SimpleNamespace(fetch_recent=acquire),
                state,
                SimpleNamespace(send=notifications.append),
            )

            class Stopper:
                stopped = False

                def is_set(self):
                    return self.stopped

                def wait(self, timeout):
                    waits.append(timeout)
                    if len(waits) <= failed_attempts:
                        assert len(attempts) == len(waits)
                        assert not notifications and not state.pending(group)
                        assert state.notification_boundary(group) == "300"
                        assert state.consecutive_failures(group) == 1
                        assert state.next_eligible(group) == datetime(
                            2026, 10, 20, 0, 44, tzinfo=timezone.utc
                        )
                        assert tuple(context.pages) == original_pages
                        now[0] += timedelta(seconds=timeout)
                        return False
                    self.stopped = True
                    return True

            MonitorLoop(
                service,
                state,
                ScheduleSettings(timedelta(minutes=44), timedelta(minutes=99)),
                clock=lambda: now[0],
                uniform=lambda lower, upper: lower,
            ).run(
                group,
                ScanPolicy(sample_count=3, max_scrolls=1, settle_seconds=0),
                stop_event=Stopper(),
            )
            assert len(attempts) == failed_attempts + 1
            assert [later - earlier for earlier, later in pairwise(attempts)] == [
                timedelta(seconds=5 * 2**index) for index in range(failed_attempts)
            ]
            short_delays = [5 * 2**index for index in range(failed_attempts)]
            assert waits == [*short_delays, 44 * 60 - sum(short_delays)]
            assert len(notifications) == 1
            assert state.notification_boundary(group) == "301"
            assert not state.pending(group)
            assert state.consecutive_failures(group) == 0
            # A later successful scan sees the same GUIDs and sends nothing again.
            later = service.run_once(
                group, ScanPolicy(sample_count=3, max_scrolls=1, settle_seconds=0)
            )
            assert later.delivered == 0, notifications
            assert len(notifications) == 1
            assert tuple(context.pages) == original_pages


@pytest.mark.parametrize(
    ("missing", "component"),
    [
        ("scroller", "unique_mobile_scroller"),
        ("timestamp", "unique_primary_timestamp"),
        ("footer", "primary_action_footer"),
        ("hidden", "primary_mobile_card"),
        ("body", "resolved_primary_body_matches_source"),
        ("multiple", "unique_resolved_primary_post"),
    ],
)
def test_permalink_verification_deadline_logs_current_missing_layout(
    tmp_path, monkeypatch, missing, component
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type(
            "Log",
            (),
            {"warning": lambda self, message, **kw: logged.append((message, kw))},
        )(),
    )
    original_records = mobile.mobile_records
    changed = False

    @contextmanager
    def records(page, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            if missing == "scroller":
                page.evaluate(
                    "document.querySelector('[data-type=vscroller]').remove()"
                )
            elif missing == "timestamp":
                page.evaluate(
                    "document.querySelector('[data-story] [aria-label]')"
                    ".removeAttribute('aria-label')"
                )
            elif missing == "footer":
                page.evaluate(
                    "document.querySelector('[aria-label=\"Make a comment\"]').remove()"
                )
            elif missing == "multiple":
                page.evaluate(
                    "document.querySelector('[data-type=vscroller]').firstElementChild.remove();"
                    "const n=document.querySelector('[data-story]');"
                    "n.after(n.cloneNode(true))"
                )
            elif missing == "hidden":
                page.evaluate(
                    "document.querySelector('[data-story] button')"
                    ".style.visibility='hidden'"
                )
        with original_records(page, **kwargs) as items:
            yield items

    monkeypatch.setattr(mobile, "mobile_records", records)
    source = {
        "author": "Synthetic private author",
        "text": "Expected private body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
    }
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture(
                    [("301", source["author"], "Different private body")]
                ),
            ),
        )
        # Removing the entire scroller leaves only a blank shell. The other
        # mutations leave visible unsupported content and remain layout errors.
        error = (
            TransientNavigationError if missing == "scroller" else LayoutChangedError
        )
        pattern = "blank loading shell" if missing == "scroller" else component
        with pytest.raises(error, match=pattern):
            resolve_post_link(
                page,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )
    snapshots = [
        fields for message, fields in logged if message == "Mobile layout snapshot"
    ]
    assert len(snapshots) == 1
    assert snapshots[0]["phase"] == "post_verification"
    assert snapshots[0]["verification_post_count"] == (
        2 if missing == "multiple" else 1 if missing == "body" else 0
    )
    if missing in {"scroller", "timestamp", "footer", "hidden"}:
        assert component in snapshots[0]["missing_components"]
    if missing == "hidden":
        assert snapshots[0]["visible_primary_menus"] < snapshots[0]["primary_menus"]
    output = str(logged)
    assert "private" not in output and "facebook.com" not in output
    assert "October" not in output


@pytest.mark.parametrize(
    ("html", "error", "signal"),
    [
        ('<input name="email">', AuthenticationRequiredError, "has_login"),
        (
            '<input name="approvals_code">',
            AccountActionRequiredError,
            "has_account_action",
        ),
        (
            "<main>Content isn't available</main>",
            AccessDeniedError,
            "has_access_denied",
        ),
        (
            "<main>Temporarily blocked</main>",
            TransientNavigationError,
            "has_rate_limit",
        ),
    ],
)
def test_permalink_terminal_state_after_initial_render_is_not_a_layout_error(
    tmp_path, monkeypatch, html, error, signal
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type(
            "Log",
            (),
            {"warning": lambda self, message, **kw: logged.append((message, kw))},
        )(),
    )
    original_records = mobile.mobile_records
    changed = False

    @contextmanager
    def records(page, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            page.set_content(html + "Synthetic private content")
        with original_records(page, **kwargs) as items:
            yield items

    monkeypatch.setattr(mobile, "mobile_records", records)
    source = {
        "author": "Author",
        "text": "Expected body",
        "mediaCount": 0,
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
    }
    with _local_context(tmp_path) as context:
        page = context.new_page()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture([("301", "Author", "Body")]),
            ),
        )
        with pytest.raises(error):
            resolve_post_link(
                page,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )
    snapshots = [
        fields for message, fields in logged if message == "Mobile layout snapshot"
    ]
    assert snapshots[0][signal] is True
    assert "Synthetic private content" not in str(logged)


def test_permalink_temporary_missing_footer_recovers_without_reload_or_warning(
    tmp_path, monkeypatch
):
    from fbn import mobile

    logged = []
    monkeypatch.setattr(
        mobile,
        "LOGGER",
        type(
            "Log",
            (),
            {"warning": lambda self, message, **kw: logged.append((message, kw))},
        )(),
    )
    original_records = mobile.mobile_records
    changed = False

    @contextmanager
    def records(page, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            page.evaluate("""() => {
                const footer = document.querySelector('[aria-label="Make a comment"]');
                footer.setAttribute('aria-label', 'Loading');
                setTimeout(() => {
                    footer.setAttribute('aria-label', 'Make a comment');
                }, 200);
            }""")
        with original_records(page, **kwargs) as items:
            yield items

    monkeypatch.setattr(mobile, "mobile_records", records)
    source = {
        "author": "Author",
        "text": "Body",
        "mediaCount": 0,
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
    }
    requests = []
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="text/html",
                body=mobile_fixture([("301", "Author", "Body")]),
            )

        context.route("**/*", serve)
        assert resolve_post_link(
            page,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            source,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")
    assert len(requests) == 1
    assert logged == []


def test_mobile_defers_unrendered_cards_until_scrolling_makes_them_readable(tmp_path):
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            mobile_fixture([("301", "First", "Body"), ("302", "Second", "Later body")])
        )
        card = page.locator('[data-story="302"]')
        card.evaluate("""n => {
            n.style.marginTop = '10000px';
            n.style.contentVisibility = 'auto';
            n.style.containIntrinsicSize = '500px';
        }""")
        page.wait_for_timeout(100)
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED
        with mobile_records(page) as records:
            assert [payload["author"] for _, payload in records] == ["First"]
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(100)
        assert (
            wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
            is PageState.FEED
        )
        with mobile_records(page) as records:
            assert [payload["author"] for _, payload in records] == ["First", "Second"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_mobile_verifies_permalink_with_author_inside_timestamp_component(
    tmp_path, wrapped
):
    html = mobile_fixture([("301", "Author", "Body")])
    target_html = html.replace('<span role="link">Author</span>', "Group title")
    target_html = target_html.replace(
        'aria-label="Sunday, October 11, 2026 at 10:00 AM"',
        'aria-label="Author, Sunday, October 11, 2026 at 10:00 AM"',
    ).replace(">2h</div>", '><span role="link">Author</span> 2h</div>')
    if not wrapped:
        target_html += """<script>
          const root = document.querySelector('[data-type=vscroller]');
          root.firstElementChild.remove();
          const card = root.querySelector('[data-story]');
          card.firstElementChild.remove();
          card.replaceWith(...card.children);
          root.prepend(document.createElement('div'), document.createElement('div'));
        </script>"""
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=target_html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


@pytest.mark.parametrize("wrong", [None, "author", "body", "age", "identity"])
def test_mobile_relative_age_rollover_keeps_post_link_checks_strict(tmp_path, wrong):
    source_html = mobile_fixture([("301", "Author", "Body")]).replace(
        "Sunday, October 11, 2026 at 10:00 AM", "19 hours ago"
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(source_html)
        with mobile_records(page) as records:
            payload = records[0][1]
        assert isinstance(payload["timestampObservedAt"], int)
        target = context.new_page()
        author = "Wrong" if wrong == "author" else "Author"
        body = "Wrong" if wrong == "body" else "Body"
        age = "21 hours ago" if wrong == "age" else "20 hours ago"
        html = mobile_fixture([("301", author, body)]).replace(
            "Sunday, October 11, 2026 at 10:00 AM", age
        )
        if wrong == "identity":
            html += (
                "<script>history.replaceState(null,'',"
                "'/groups/contract-group/posts/999/')</script>"
            )
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )

        def resolve():
            return resolve_post_link(
                target,
                "https://m.facebook.com/groups/contract-group/posts/301/",
                payload,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )

        if wrong is None:
            assert resolve().endswith("/posts/301/")
        else:
            with pytest.raises(LayoutChangedError):
                resolve()


@pytest.mark.parametrize(
    "badge",
    ["All-star contributor", "Top contributor", "Rising contributor", "Group expert"],
)
@pytest.mark.parametrize("permalink", [False, True])
def test_mobile_reads_timestamp_from_contributor_metadata_wrapper(
    tmp_path, badge, permalink
):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        '<div data-mcomponent="ServerTextArea" '
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>',
        '<div data-mcomponent="MContainer" '
        f'aria-label="{badge}, , Sunday, October 11, 2026 at 10:00 AM, Private group">'
        f'<div data-mcomponent="TextArea">{badge}</div>'
        '<div data-mcomponent="TextArea">2h</div></div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert (
            wait_for_mobile_page(page, timeout_seconds=0.1, permalink=permalink)
            is PageState.FEED
        )
        with mobile_records(page, permalink=permalink) as records:
            assert len(records) == 1
            assert records[0][1]["timestampLabel"] == (
                "Sunday, October 11, 2026 at 10:00 AM, Private group"
            )


@pytest.mark.parametrize("badge", [None, "All-star contributor"])
@pytest.mark.parametrize("include_author", [False, True])
def test_mobile_reads_primary_date_from_metadata_container(
    tmp_path, badge, include_author
):
    prefix = "Author, " if include_author else ""
    prefix += f"{badge}, , " if badge else ""
    contents = '<span role="link">Author</span>' if include_author else ""
    contents += f'<div data-mcomponent="TextArea">{badge}</div>' if badge else ""
    html = mobile_fixture([("301", "Author", "Body")])
    if include_author:
        html = html.replace('<span role="link">Author</span>', "Group title")
    html = html.replace(
        '<div data-mcomponent="ServerTextArea" '
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>',
        '<div data-mcomponent="MContainer" '
        f'aria-label="{prefix}Sunday, October 11, 2026 at 10:00 AM, Private group">'
        + contents
        + '<div data-mcomponent="TextArea">2h</div></div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert (
            wait_for_mobile_page(page, timeout_seconds=0.1, permalink=True)
            is PageState.FEED
        )
        with mobile_records(page, permalink=True) as records:
            assert len(records) == 1
            assert records[0][1]["timestampLabel"] == (
                "Sunday, October 11, 2026 at 10:00 AM, Private group"
            )


@pytest.mark.parametrize(
    "prefix_order",
    ["badge-author", "badge-author-badge", "author-badge", "badge-badge-author"],
)
@pytest.mark.parametrize(
    "date", ["4 days ago", "26 Sep", "Sunday, October 11, 2026 at 10:00 AM"]
)
def test_mobile_verifies_metadata_with_badges_before_or_after_author(
    tmp_path, prefix_order, date
):
    source_html = mobile_fixture([("301", "Primary Author", "Primary body")]).replace(
        "Sunday, October 11, 2026 at 10:00 AM", date
    )
    parts = prefix_order.split("-")
    badges = iter(["All-star contributor", "Group expert"])
    labels = ["Primary Author" if part == "author" else next(badges) for part in parts]
    contents = "".join(
        '<div data-mcomponent="ServerTextArea">'
        + ('<span role="link">Primary Author</span>' if part == "author" else label)
        + "</div>"
        for part, label in zip(parts, labels, strict=True)
    )
    target_html = source_html.replace(
        '<span role="link">Primary Author</span>', "Group title"
    ).replace(
        '<div data-mcomponent="ServerTextArea" ' + f'aria-label="{date}">2h</div>',
        '<div data-mcomponent="MContainer" '
        + f'aria-label="{", , ".join(labels)}, {date}, Private group">'
        + contents
        + '<div data-mcomponent="TextArea">2h</div></div>',
    )
    with _local_context(tmp_path) as context:
        source = context.new_page()
        source.set_content(source_html)
        with mobile_records(source) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=target_html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


@pytest.mark.parametrize("wrapper", [False, True])
def test_mobile_verifies_older_post_with_date_only_publication(tmp_path, wrapper):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h',
        'aria-label="26 Sep, Private group">26 Sep',
    )
    if wrapper:
        html = html.replace(
            '<div data-mcomponent="ServerTextArea" '
            'aria-label="26 Sep, Private group">26 Sep</div>',
            '<div data-mcomponent="MContainer" '
            'aria-label="Group expert, , 26 Sep, Private group">'
            '<div data-mcomponent="TextArea">Group expert</div>'
            '<div data-mcomponent="TextArea">26 Sep</div></div>',
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED
        with mobile_records(page) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/groups/contract-group/posts/301/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


def test_mobile_ignores_non_date_accessibility_labels_in_primary_header(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        '<span role="link">Author</span>',
        '<span role="link">Author</span>'
        '<div data-mcomponent="TextArea" aria-label="Group expert">Group expert</div>'
        '<div data-mcomponent="TextArea" aria-label="Follow">Follow</div>',
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        assert wait_for_mobile_page(page, timeout_seconds=0.1) is PageState.FEED
        with mobile_records(page) as records:
            assert len(records) == 1


def test_mobile_waits_for_short_link_client_side_canonical_navigation(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")])
    target_html = (
        html
        + """<script>
      setTimeout(() => {
        history.replaceState(null, '', '/groups/contract-group/posts/301/');
      }, 100);
    </script>"""
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with mobile_records(page) as records:
            payload = records[0][1]
        target = context.new_page()
        target.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=target_html
            ),
        )
        assert resolve_post_link(
            target,
            "https://m.facebook.com/share/p/example/",
            payload,
            frozenset({"contract-group"}),
            timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
        ).endswith("/posts/301/")


@pytest.mark.parametrize(
    "invalid",
    ["badge_only", "unrelated_badge", "two_dates", "menu_container", "foreign_author"],
)
def test_mobile_contributor_wrapper_cannot_hide_missing_or_ambiguous_timestamp(
    tmp_path, invalid
):
    replacement = (
        '<div data-mcomponent="MContainer" '
        'aria-label="All-star contributor, , 2 hours ago, Private group">'
        '<div data-mcomponent="TextArea">All-star contributor</div>'
        '<div data-mcomponent="TextArea">2h</div></div>'
    )
    if invalid == "badge_only":
        replacement = replacement.replace(", , 2 hours ago, Private group", "")
    elif invalid == "unrelated_badge":
        replacement = replacement.replace(">All-star contributor<", ">Another badge<")
    elif invalid == "two_dates":
        replacement += (
            '<div data-mcomponent="TextArea" aria-label="3 hours ago">3h</div>'
        )
    elif invalid == "foreign_author":
        replacement = replacement.replace(
            "All-star contributor, ,", "Another,"
        ).replace(">All-star contributor<", '><a role="link">Another</a><')
    else:
        replacement = replacement.replace(
            ">2h</div>",
            '>2h<button role="button" aria-label="More options for Nested">'
            "...</button></div>",
        )
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        '<div data-mcomponent="ServerTextArea" '
        'aria-label="Sunday, October 11, 2026 at 10:00 AM">2h</div>',
        replacement,
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="unique_primary_"):
            wait_for_mobile_page(page, timeout_seconds=0.1)


def test_mobile_does_not_accept_sort_text_inside_a_post(tmp_path):
    html = mobile_fixture([("301", "Author", "New posts")], sorted_feed=False)
    html = html.replace('id="sort-label"', 'id="sort-label" style="display:none"')
    html = html.replace(">\u200bSort\u200b<", ">Unavailable<")
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="group_sort_control"):
            select_new_posts(page, timeout_seconds=0.1)


def test_mobile_failed_copy_cleans_up_and_cannot_reuse_a_previous_link(tmp_path):
    html = mobile_fixture([("301", "Author", "Body")]).replace(
        "function copyPost() {navigator.clipboard.writeText(",
        "function copyPost() {return; navigator.clipboard.writeText(",
    )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        page.evaluate(
            "window.__fbnPreviousCopiedLink='https://m.facebook.com/share/p/999/'"
        )
        with mobile_records(page) as records:
            assert len(records) == 1
            # Either Playwright's wait or our shared action deadline can expire
            # first. Assert the typed failure and cleanup, not library wording
            # or how much of a 100ms budget a particular runner spent clicking.
            with pytest.raises((PlaywrightTimeoutError, TransientNavigationError)):
                copy_post_link(
                    page, records[0][0], timeout_seconds=OFFLINE_NAVIGATION_SECONDS
                )
        assert not page.locator("#post-menu").is_visible()
        assert page.evaluate("!window.__fbnCopyCapture")
        assert page.evaluate("!Object.hasOwn(navigator,'clipboard')")


@pytest.mark.parametrize("missing", ["author", "footer", "timestamp"])
def test_mobile_incomplete_primary_card_fails_closed(tmp_path, missing):
    html = mobile_fixture([("301", "Author", "Body")])
    replacements = {
        "author": ('<span role="link">Author</span>', "<span>Author</span>"),
        "footer": ('aria-label="Make a comment"', 'aria-label="Other action"'),
        "timestamp": ('aria-label="Sunday, October 11, 2026 at 10:00 AM"', ""),
    }
    html = html.replace(*replacements[missing])
    if missing == "footer":
        html = html.replace(
            'aria-label="Like, press and hold for more reactions"',
            'aria-label="Other action"',
        )
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        with pytest.raises(LayoutChangedError, match="missing="):
            wait_for_mobile_page(page, timeout_seconds=0.1)


@pytest.mark.parametrize(
    "wrong", ["body", "author", "group", "comment_only", "offsite", "timestamp"]
)
def test_mobile_rejects_wrong_copied_link_destinations(tmp_path, wrong):
    source_payload = {
        "author": "Author",
        "text": "Expected body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
    }
    stories = [("999", "Author", "Expected body")]
    if wrong in {"body", "comment_only"}:
        stories = [("999", "Author", "Different primary body")]
    if wrong == "author":
        stories = [("999", "Different Author", "Expected body")]
    with _local_context(tmp_path) as context:
        page = context.new_page()
        html = mobile_fixture(stories)
        if wrong == "comment_only":
            html = html.replace("Nested reply body", "Expected body")
        if wrong == "timestamp":
            html = html.replace("at 10:00 AM", "at 9:00 AM")
        group_key = "wrong-group" if wrong == "group" else "contract-group"

        def route_target(route):
            body = (
                html
                + "<script>history.replaceState(null,'','/groups/"
                + f"{group_key}/posts/999/')</script>"
            )
            route.fulfill(status=200, content_type="text/html", body=body)

        context.route("**/*", route_target)
        copied = (
            "https://example.invalid/share/p/999/"
            if wrong == "offsite"
            else "https://m.facebook.com/share/p/999/"
        )
        with pytest.raises(LayoutChangedError):
            resolve_post_link(
                page,
                copied,
                source_payload,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )


@pytest.mark.parametrize("changed_identity", [False, True, "before_render"])
def test_mobile_waits_for_permalink_body_to_finish_rendering(
    tmp_path, changed_identity
):
    source = {
        "author": "Author",
        "text": "Expected complete body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
    }
    with _local_context(tmp_path) as context:
        page = context.new_page()
        html = mobile_fixture([("999", "Author", "Still rendering")])
        html += """<script>
          history.replaceState(null,'','/groups/contract-group/posts/999/');
          setTimeout(() => {
            const body = document.querySelector('[data-story="999"]')
              .children[2].querySelector('[data-mcomponent="ServerTextArea"]');
            body.textContent = 'Expected complete body';
          }, 250);
        </script>"""
        if changed_identity:
            html += """<script>setTimeout(() => history.replaceState(
              null, '', '/groups/contract-group/posts/888/'), 100);</script>"""
        if changed_identity == "before_render":
            html += """<script>
              const footer = document.querySelector('[aria-label="Make a comment"]');
              footer.hidden = true;
              setTimeout(() => footer.hidden = false, 250);
            </script>"""
        requests = []

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)

        def resolve():
            return resolve_post_link(
                page,
                "https://m.facebook.com/share/p/999/",
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )

        if changed_identity:
            with pytest.raises(LayoutChangedError, match="unchanged_resolved_post"):
                resolve()
        else:
            assert resolve().endswith("/posts/999/")
        assert len(requests) == 1


@pytest.mark.parametrize(
    "destination",
    ["matching", "wrong_body", "wrong_identity", "account_action", "persistent_panel"],
)
def test_mobile_known_panel_is_partitioned_without_canonical_reload(
    tmp_path, destination
):
    source = {
        "author": "Author",
        "text": "Expected body",
        "timestampLabel": "Sunday, October 11, 2026 at 10:00 AM",
        "mediaCount": 0,
        "bodyControlCount": 0,
        "partial": False,
    }
    panel = (
        '<div data-mcomponent="MContainer">'
        '<div data-mcomponent="ServerTextArea">Are you interested in this post?</div>'
        '<button role="button"><span data-mcomponent="TextArea">Interested</span>'
        '</button><button role="button"><span data-mcomponent="TextArea">'
        "Not interested</span></button></div>"
    )
    canonical = "https://m.facebook.com/groups/contract-group/posts/999/"
    requests = []
    with _local_context(tmp_path) as context:
        page = context.new_page()

        def serve(route):
            requests.append(route.request.url)
            body = "Expected body"
            if destination == "wrong_body":
                body += " Different continuation"
            html = mobile_fixture([("999", "Author", body)])
            footer = (
                '</div><div data-mcomponent="MContainer">'
                '<button role="button" aria-label="Like,'
            )
            html = html.replace(footer, "</div>" + panel + footer[6:])
            path = "/groups/contract-group/posts/999/"
            if destination == "wrong_identity":
                path = "/groups/contract-group/posts/888/"
            if destination == "account_action":
                path = "/checkpoint/"
                html = '<input name="approvals_code">'
            html += f"<script>history.replaceState(null,'','{path}')</script>"
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        error = (
            AccountActionRequiredError
            if destination == "account_action"
            else LayoutChangedError
        )

        def resolve():
            return resolve_post_link(
                page,
                canonical,
                source,
                frozenset({"contract-group"}),
                timeout_seconds=OFFLINE_NAVIGATION_SECONDS,
            )

        if destination in {"matching", "persistent_panel"}:
            assert resolve() == canonical.replace("m.facebook.com", "www.facebook.com")
        else:
            with pytest.raises(error):
                resolve()
        assert requests == [canonical]


@pytest.mark.parametrize("footer_state", ["incomplete", "virtualized", "loading"])
def test_mobile_never_uses_a_reply_action_row_as_the_post_footer(
    tmp_path, footer_state
):
    html = mobile_fixture([("999", "Author", "Only the primary body")])
    primary_footer = (
        '</div><div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like, press and hold for more reactions">'
    )
    replacement = primary_footer.replace(
        '<div data-mcomponent="MContainer">',
        '<div data-mcomponent="MContainer" '
        'style="content-visibility:auto;contain-intrinsic-size:100px;'
        'margin-top:3000px">',
    )
    if footer_state == "virtualized":
        html = html.replace(primary_footer, replacement)
    else:
        html = html.replace('aria-label="Make a comment"', 'aria-label="Loading"')
    html = html.replace(
        "Following reply preview</div></div></div>",
        'Following reply preview</div><img alt="Reply avatar"></div>'
        '<div data-mcomponent="MContainer">'
        '<button role="button" aria-label="Like">Like</button>'
        '<button role="button" aria-label="Comment">Comment</button></div></div>',
    )
    if footer_state == "loading":
        html += """<script>setTimeout(() => document.querySelector(
          '[aria-label="Loading"]').setAttribute('aria-label','Make a comment'),
          250);</script>"""
    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(html)
        if footer_state == "incomplete":
            with pytest.raises(LayoutChangedError, match="primary_action_footer"):
                wait_for_mobile_page(page, timeout_seconds=0.1)
        else:
            wait_for_mobile_page(page, timeout_seconds=OFFLINE_NAVIGATION_SECONDS)
            with mobile_records(page) as records:
                assert len(records) == 1
                assert records[0][1]["text"] == "Only the primary body"
                assert records[0][1]["mediaCount"] == 0


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("uppercase_sort", [False, True])
def test_mobile_complete_fetch_is_bounded_and_reads_beyond_the_marker(
    tmp_path, rename, uppercase_sort
):
    group = parse_group_ref("123" if rename else "contract-group")
    stories = [("302", "Author", "Newer"), ("301", "Author", "Marker")]
    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)
        opened = []
        context.on("page", lambda page: opened.append(page))

        def routes(route):
            url = route.request.url
            key = url.split("?")[0].rstrip("/").split("/")[-1]
            is_post = "/share/" in url or "/posts/" in url
            chosen = (
                [next(story for story in stories if story[0] == key)]
                if is_post
                else stories
            )
            html = mobile_fixture(
                chosen, sorted_feed=is_post, uppercase_sort=uppercase_sort
            )
            if rename and not is_post:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/')</script>"
                )
            if "/share/" in url:
                html += (
                    "<script>history.replaceState(null,'','/groups/"
                    f"contract-group/posts/{key}/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        result = source.fetch_recent(
            group,
            ScanPolicy(sample_count=1, max_scrolls=2, settle_seconds=0),
            boundary_post_id="301",
        )
        assert [post.post_id for post in result.posts] == ["302", "301"]
        assert result.scrolls >= 1
        assert not result.bounded
        assert tuple(context.pages) == original_pages
        # The feed and each verified post own a separate, promptly closed tab.
        assert len(opened) == 3
        assert all(page.is_closed() for page in opened)


def test_mobile_fetch_allows_post_menu_to_load_beyond_old_three_second_cap(tmp_path):
    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)

        def routes(route):
            html = mobile_fixture([("301", "Author", "Body")])
            if "/share/" in route.request.url:
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            else:
                html = html.replace(
                    'document.getElementById("post-menu").hidden=false;',
                    'setTimeout(() => document.getElementById("post-menu")'
                    ".hidden=false, 3200);",
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        result = source.fetch_recent(
            parse_group_ref("contract-group"),
            ScanPolicy(sample_count=1, max_scrolls=0, navigation_timeout_seconds=5),
        )
        assert [post.post_id for post in result.posts] == ["301"]
        assert tuple(context.pages) == original_pages


@pytest.mark.parametrize("changed_body", [False, True])
def test_remounted_overlap_reuses_only_fresh_copied_link_with_matching_body(
    tmp_path, monkeypatch, changed_body
):
    import fbn.mobile as mobile

    monkeypatch.setattr(mobile, "MAX_LINK_SECONDS", 1)
    stories = [("302", "Author", "Identical body"), ("301", "Author", "Identical body")]
    visited = []
    with _local_context(tmp_path) as context:

        def serve(route):
            url = route.request.url
            target = "/share/" in url
            key = url.rstrip("/").split("/")[-1]
            if target:
                visited.append(key)
            html = mobile_fixture(
                [next(p for p in stories if p[0] == key)] if target else stories
            )
            if target:
                html += (
                    "<script>history.replaceState(null,'','/groups/"
                    f"contract-group/posts/{key}/')</script>"
                )
            else:
                html += (
                    """<script>window.scrollBy=()=>{
                    for(const card of document.querySelectorAll('[data-story]')){
                        const fresh=card.cloneNode(true);
                        fresh.querySelector('[data-action-id]').setAttribute('data-action-id','remounted-'+fresh.dataset.story);
                        card.replaceWith(fresh);
                    }
                """
                    + (
                        "document.querySelector('[data-story=\"302\"]')"
                        ".children[2].firstElementChild.textContent='Changed body';"
                        if changed_body
                        else ""
                    )
                    + "};</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        if changed_body:
            with pytest.raises(LayoutChangedError):
                source.fetch_recent(
                    parse_group_ref("contract-group"),
                    ScanPolicy(max_scrolls=2, settle_seconds=0),
                    boundary_post_id="301",
                )
            assert visited == ["302", "301", "302"]
        else:
            result = source.fetch_recent(
                parse_group_ref("contract-group"),
                ScanPolicy(max_scrolls=2, settle_seconds=0),
                boundary_post_id="301",
            )
            assert not result.bounded
            assert [p.post_id for p in result.posts] == ["302", "301"]
            assert visited == ["302", "301"]


def test_archived_marker_mobile_catchup_delivers_and_does_not_repeat(tmp_path):
    from fbn.monitor import MonitorService
    from tests.test_identity_migration import legacy_database
    from tests.test_state import GROUP, T0, post

    path = tmp_path / "state.sqlite3"
    legacy_database(path, url=GROUP.url)
    with SQLiteStateRepository(path) as state:
        state.observe(GROUP, [post("301")])
        state.mark_delivered([p.event_id for p in state.pending(GROUP)])
        state._require_connection().execute("PRAGMA user_version=4")
    notifications = []
    with _local_context(tmp_path) as context:

        def serve(route):
            url = route.request.url
            key = url.rstrip("/").split("/")[-1]
            target = "/share/" in url
            stories = [
                ("302", "Author", "New body"),
                ("301", "Author", "Delivered body"),
            ]
            html = mobile_fixture(
                [p for p in stories if p[0] == key] if target else stories,
                group=GROUP.key,
            )
            if target:
                html += (
                    "<script>history.replaceState(null,'','/groups/"
                    f"{GROUP.key}/posts/{key}/')</script>"
                )
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        with SQLiteStateRepository(path, clock=lambda: T0) as state:
            assert state.notification_boundary(GROUP) == "301"
            from types import SimpleNamespace

            service = MonitorService(
                source, state, SimpleNamespace(send=notifications.append)
            )
            result = service.run_once(
                GROUP, ScanPolicy(max_scrolls=2, settle_seconds=0)
            )
            assert result.delivered == 1
            assert len(notifications) == 1
            assert GROUP.url + "posts/302/" in notifications[0].body
            assert GROUP.url + "posts/301/" not in notifications[0].body
            assert not state.pending(GROUP)
            assert state.notification_boundary(GROUP) == "302"
        with SQLiteStateRepository(path) as state:
            result = MonitorService(
                source, state, SimpleNamespace(send=notifications.append)
            ).run_once(GROUP, ScanPolicy(max_scrolls=2, settle_seconds=0))
            assert result.delivered == 0 and len(notifications) == 1


@contextmanager
def _local_context(
    tmp_path: Path,
    *,
    timezone_id: str | None = None,
) -> Iterator[object]:
    requested = os.environ.get("FBN_TEST_BROWSER", "chromium")
    settings = BrowserSettings(
        browser=requested,
        profile_dir=tmp_path / requested,
        headless=True,
    )
    source = MobilePostSource(settings)
    with source._context(headless=True, timezone_id=timezone_id) as context:
        yield context


@pytest.mark.parametrize(
    "failure", ["feed", "post", "navigation", "unexpected", "auth", None]
)
def test_mobile_fetch_saves_only_failed_viewports_before_tabs_close(
    tmp_path, monkeypatch, failure
):
    from fbn import mobile

    with _local_context(tmp_path) as context:
        original_pages = tuple(context.pages)
        opened = []
        context.on("page", lambda page: opened.append(page))

        def routes(route):
            html = mobile_fixture([("301", "Synthetic Author", "Synthetic body")])
            if "/share/" in route.request.url:
                html = (
                    mobile_fixture([("301", "Synthetic Author", "Different body")])
                    if failure == "post"
                    else html
                )
                html += (
                    "<script>history.replaceState(null,'',"
                    "'/groups/contract-group/posts/301/')</script>"
                )
            elif failure == "feed":
                html = "<main>Synthetic unsupported layout</main>"
            elif failure == "auth":
                html = '<input name="email" value="SYNTHETIC_SECRET">'
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", routes)
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        if failure in {"navigation", "unexpected"}:

            def fail(*args, **kwargs):
                if failure == "navigation":
                    raise mobile.PlaywrightTimeoutError("Synthetic timeout")
                raise ValueError("Synthetic unexpected failure")

            monkeypatch.setattr(mobile, "select_new_posts", fail)
        error = {
            "feed": LayoutChangedError,
            "post": LayoutChangedError,
            "navigation": TransientNavigationError,
            "unexpected": ValueError,
            "auth": AuthenticationRequiredError,
        }.get(failure)
        if error:
            with pytest.raises(error):
                source.fetch_recent(
                    parse_group_ref("contract-group"),
                    ScanPolicy(
                        sample_count=1, max_scrolls=0, navigation_timeout_seconds=5
                    ),
                )
        else:
            result = source.fetch_recent(
                parse_group_ref("contract-group"),
                ScanPolicy(sample_count=1, max_scrolls=0, navigation_timeout_seconds=5),
            )
            assert [post.post_id for post in result.posts] == ["301"]
        assert tuple(context.pages) == original_pages
        assert all(page.is_closed() for page in opened)
        root = tmp_path / "failure-screenshots"
        if failure in {None, "auth"}:
            assert not root.exists()
            return
        events = tuple(root.iterdir())
        assert len(events) == 1
        event = events[0]
        expected = (
            {"post": "saved", "feed": "saved"}
            if failure == "post"
            else {"feed": "saved"}
        )
        assert json.loads((event / "metadata.json").read_text())["pages"] == expected
        for role in expected:
            image = event / f"{role}.png"
            data = image.read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", data[16:24])
            assert 0 < width <= 2_000
            assert 0 < height <= 3_000
            assert stat.S_IMODE(image.stat().st_mode) == 0o600


def test_real_failure_screenshots_mask_input_and_editable_content(tmp_path):
    from fbn.failure_screenshots import capture_failure_screenshots

    with _local_context(tmp_path) as context:
        page = context.new_page()
        page.set_content(
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<style>body{margin:0} input,textarea,div {box-sizing:border-box;"
            "width:200px;height:32px;display:block;border:0;padding:0;margin:0;"
            "overflow:hidden;}</style>"
            '<input value="Synthetic secret one">'
            "<textarea>Synthetic secret two</textarea>"
            '<div contenteditable="true">Synthetic secret three</div>'
        )
        capture_failure_screenshots(
            tmp_path / "unused", LayoutChangedError("Synthetic"), {"feed": page}
        )
        first = next((tmp_path / "failure-screenshots").iterdir()) / "feed.png"
        # Change values without mobile focus/zoom/scroll changing the viewport.
        page.evaluate("""() => {
            document.querySelector('input').value = 'Other secret one';
            document.querySelector('textarea').value = 'Other secret two';
            document.querySelector('[contenteditable]').textContent =
                'Other secret three';
        }""")
        capture_failure_screenshots(
            tmp_path / "unused", LayoutChangedError("Synthetic"), {"feed": page}
        )
        events = sorted((tmp_path / "failure-screenshots").iterdir())
        assert len(events) == 2
        assert first.read_bytes() == (events[-1] / "feed.png").read_bytes()


def test_bootstrap_layout_failure_saves_private_feed_before_closing(tmp_path):
    with _local_context(tmp_path) as context:
        opened = []
        context.on("page", lambda page: opened.append(page))
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<main>Synthetic unknown layout</main>",
            ),
        )
        source = MobilePostSource(BrowserSettings(profile_dir=tmp_path / "unused"))

        @contextmanager
        def existing_context(**kwargs):
            yield context

        source._context = existing_context
        with pytest.raises(LayoutChangedError):
            source._validate_authenticated_profile(
                parse_group_ref("contract-group"), navigation_timeout_seconds=5
            )
        assert all(page.is_closed() for page in opened)
        event = next((tmp_path / "failure-screenshots").iterdir())
        assert (event / "feed.png").read_bytes().startswith(b"\x89PNG")


def test_headless_browser_uses_configured_timezone(tmp_path: Path) -> None:
    with _local_context(tmp_path, timezone_id="America/New_York") as context:
        page = context.new_page()
        try:
            timezone_id = page.evaluate(
                "() => Intl.DateTimeFormat().resolvedOptions().timeZone"
            )
        finally:
            page.close()

    assert timezone_id == "America/New_York"


def test_persistent_profile_retains_facebook_cookie_across_headless_relaunch(
    tmp_path: Path,
) -> None:
    settings = BrowserSettings(
        browser=os.environ.get("FBN_TEST_BROWSER", "chromium"),
        profile_dir=tmp_path / "persistent-cookie-profile",
        headless=True,
    )
    source = MobilePostSource(settings)
    cookie = {
        "name": "fbn_persistence_probe",
        "value": "local-sanitized-value",
        "domain": ".facebook.com",
        "path": "/",
        "expires": 2_000_000_000,
        "secure": True,
    }
    with source._context(headless=True) as context:
        context.add_cookies([cookie])
    with source._context(headless=True) as context:
        persisted = context.cookies("https://www.facebook.com/")

    assert any(
        item["name"] == cookie["name"] and item["value"] == cookie["value"]
        for item in persisted
    )
