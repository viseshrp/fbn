"""Shared mobile card grammar and content-only snapshots.

DOM handles remain in the browser; PostView is the internal JSON boundary.
No URL, author, action ID, or body value belongs in structural diagnostics.
"""

from __future__ import annotations

import json

from .extractor import _FACEBOOK_TIMESTAMP_RE
from .mobile_attachments import ATTACHMENT_CONTROL_SCRIPT
from .mobile_components import BODY_COMPONENTS_SCRIPT
from .mobile_normalize import NORMALIZE_BODY_SCRIPT
from .mobile_structure import HEADER_COMPONENT_SCRIPT
from .mobile_view import BlockEvidence as BlockEvidence
from .mobile_view import PostView as PostView

# Bloks cards are direct children of a vscroller. The contextual menu belongs
# to the primary header; its author must agree with that header's visible link.
# Neither the footer nor any following comment preview is a content section.
MOBILE_SCAN_SCRIPT = (
    r"""
(includeContent) => {
  const permalink = false;
  const clean = value => (value || '').normalize('NFKC')
    .replace(/[\u034f\u200b\u200e\u200f\ufeff]/g, '')
    .replace(/\s+/g, ' ').trim();
  // Bloks leaves off-screen cards in the DOM with content-visibility:auto.
  // Their boxes exist, but innerText is empty until scrolling renders them.
  const visible = node => node.checkVisibility({
    contentVisibilityAuto: true, visibilityProperty: true, opacityProperty: true,
  });
  const menuSelector = '[role="button"][aria-label^="More options for "]';
  const textSelector = '[data-mcomponent="ServerTextArea"],'
    + '[data-mcomponent="TextArea"]';
  const timestampPattern = new RegExp(__FBN_TIMESTAMP_PATTERN__, 'i');
  const actionLabel = node => clean(node.getAttribute('aria-label'))
    .replace(/\p{Co}/gu, '').trim().replace(/^\d+\s*/, '').toLowerCase();
  const excludedSelector = '[role="article"],[aria-label^="Comment by "],'
    + '[aria-label^="Reply by "],[aria-label="Shared post"],'
    + '[data-ad-rendering-role="comment"]';
  const roots = Array.from(document.querySelectorAll(
    '[data-mcomponent="MContainer"][data-type="vscroller"]'
  )).filter(root => visible(root) && !root.parentElement.closest(
    '[data-type="vscroller"]'
  ));
  const records = [];
  const observedAt = Date.now();
  const tokens = window.__fbnMobileCardTokens ||= {next: 0, cards: new WeakMap()};
  const roles = window.__fbnMobileBlockRoles = new WeakMap();
  const missing = new Set();
  let candidateCount = 0;
  if (roots.length !== 1) missing.add('unique_mobile_scroller');
  const branch = (root, node) => presentationChildren(root)
    .find(child => child === node || child.contains(node)) || null;
  const sectionsFor = card => presentationChildren(card).filter(node =>
    node.hasAttribute('data-mcomponent') || node.querySelector('[data-mcomponent]')
  );
  const primaryMenus = card => {
    const sections = sectionsFor(card);
    const boundary = sections.findIndex(section =>
      action(section, /^(?:like|unlike)(?:,|$)/)
      || action(section, /^(?:make a comment|comments?)$/));
    return Array.from(card.querySelectorAll(menuSelector))
      .filter(menu => visible(menu) && !menu.closest(excludedSelector)
        && (boundary < 0 || sections.indexOf(branch(card, menu)) < boundary));
  };
  const action = (section, pattern) => Array.from(section.querySelectorAll(
    '[role="button"]'
  )).some(node => !node.closest(excludedSelector) && node.checkVisibility({
    visibilityProperty: true, opacityProperty: true,
  })
    && pattern.test(actionLabel(node)));
  const recognizeFooter = (sections, start) => {
    for (let index = start; index < sections.length; index++) {
      const like = action(sections[index], /^(?:like|unlike)(?:,|$)/);
      const comment = action(sections[index], /^(?:make a comment|comments?)$/);
      // The first action row belongs to the post. If it is incomplete, wait
      // for it; never scan through replies to find a later substitute footer.
      // Its aria labels remain usable when content-visibility:auto defers paint.
      if (like || comment) return {
        status: like && comment ? 'recognized' : 'incomplete',
        index: like && comment ? index : -1};
    }
    return {status: 'absent', index: -1};
  };

  __FBN_BODY_NORMALIZER__
  __FBN_ATTACHMENT_CONTROLS__
  __FBN_HEADER_COMPONENT__
  __FBN_BODY_COMPONENTS__
  for (const root of roots) {
    let cards = presentationChildren(root).map(unwrapPresentation);
    if (permalink) {
      // Single-post pages can put header/body/actions directly in the scroller.
      // Only use that shape when one primary header precedes its own footer.
      const menus = primaryMenus(root), sections = sectionsFor(root);
      if (menus.length === 1) {
        const start = sections.indexOf(branch(root, menus[0])) + 1;
        if (recognizeFooter(sections, start).index >= start) cards = [root];
      }
    }
    for (const card of cards) {
      const primary = primaryMenus(card);
      if (!primary.length) continue;
      candidateCount++;
      if (primary.length !== 1) { missing.add('unique_primary_menu'); continue; }
      const menu = primary[0];
      const header = branch(card, menu);
      roles.set(header, 'header');
      const headerResult = recognizeHeader(header, menu);
      if (headerResult.status !== 'recognized') {
        missing.add(headerResult.missing); continue;
      }
      const {owner, timestamp} = headerResult;
      const sections = sectionsFor(card);
      const start = sections.indexOf(header) + 1;
      const footer = recognizeFooter(sections, start);
      const end = footer.index;
      if (footer.status !== 'recognized' || end < start) {
        missing.add('primary_action_footer'); continue;
      }
      roles.set(sections[end], 'footer');
      const body = parseBody(sections.slice(start, end));
      if (body.unsupported) {
        for (const issue of body.issues) missing.add('body_' + issue);
        missing.add('supported_primary_body_controls'); continue;
      }
      const {text, texts, expanders, mediaCount, partial, bodyControlCount} = body;
      if (!text && !mediaCount) { missing.add('primary_body_or_media'); continue; }
      if (!tokens.cards.has(card)) tokens.cards.set(card, ++tokens.next);
      let expand = expanders.length === 1 ? expanders[0].closest(
        'button,[role="button"],[data-mcomponent="TextArea"][data-action-id],'
        + '[data-mcomponent="ServerTextArea"][data-action-id]'
      ) : null;
      // Bloks text can have pointer-events:none. Its enclosing body action
      // region owns the click and can have a different action ID.
      const actionRegion = expand?.closest(
        '[data-mcomponent="MContainer"][data-action-id]'
      );
      if (actionRegion && sections.slice(start, end).some(
        section => section.contains(actionRegion)
      ) && !body.regions.some(region => region.role !== 'message'
        && actionRegion.contains(region.node))) expand = actionRegion;
      records.push({card, menu, expand, regions: body.regions,
        header, footer: sections[end],
        payload: {
        author: owner, text, timestamp: clean(timestamp.node.innerText),
        timestampLabel: timestamp.label,
        timestampObservedAt: observedAt,
        cardToken: tokens.cards.get(card),
        actionToken: menu.getAttribute('data-action-id'),
        mediaCount, mediaKeys: body.mediaKeys, partial, position: records.length,
        blocks: body.blocks, bodyState: partial ? 'partial' : 'complete',
        bodyControlCount, bodyTextPartCount: texts.length,
        bodyTextLengths: texts.slice(0, 32).map(text => text.length),
        previewControlCount: expanders.length,
        parserVersion: 1, recognizers: body.recognizers,
        normalization: body.normalization,
      }});
    }
  }
  const counts = {
    feedRootCount: roots.length, itemCount: candidateCount,
    containerCount: candidateCount, authorCount: records.length,
    storyMessageCount: records.length, postCount: records.length,
    hasFeed: roots.length === 1, missing: Array.from(missing),
  };
  return includeContent ? {...counts, records} : counts;
}
""".replace("__FBN_TIMESTAMP_PATTERN__", json.dumps(_FACEBOOK_TIMESTAMP_RE.pattern))
    .replace("__FBN_BODY_NORMALIZER__", NORMALIZE_BODY_SCRIPT)
    .replace("__FBN_ATTACHMENT_CONTROLS__", ATTACHMENT_CONTROL_SCRIPT)
    .replace("__FBN_BODY_COMPONENTS__", BODY_COMPONENTS_SCRIPT)
)
MOBILE_SCAN_SCRIPT = MOBILE_SCAN_SCRIPT.replace(
    "__FBN_HEADER_COMPONENT__", HEADER_COMPONENT_SCRIPT
)

MOBILE_PERMALINK_SCAN_SCRIPT = MOBILE_SCAN_SCRIPT.replace(
    "const permalink = false;", "const permalink = true;"
)


APP_PROMPT_SCRIPT = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const excluded = '[aria-label^="More options for "],[role="article"],'
    + '[aria-label^="Comment by "],[aria-label^="Reply by "]';
  const texts = Array.from(document.querySelectorAll(
    '[data-mcomponent="TextArea"],[data-mcomponent="ServerTextArea"],'
    + 'h1,h2,h3,button,[role="button"]'
  )).filter(n => n.checkVisibility({contentVisibilityAuto:true,
    visibilityProperty:true,opacityProperty:true}) && !n.closest(excluded));
  const exact = label => texts.filter(n => clean(n.innerText) === label)
    .filter(n => !texts.some(other => other !== n && n.contains(other)
      && clean(other.innerText) === label));
  const headings = exact('Facebook is better on the app');
  const accept = exact('Get app'), dismiss = exact('Not now');
  if (headings.length !== 1 || accept.length !== 1 || dismiss.length !== 1) return null;
  let panel = dismiss[0].parentElement;
  while (panel && (!panel.contains(headings[0]) || !panel.contains(accept[0]))) {
    panel = panel.parentElement;
  }
  if (!panel || panel.querySelector(excluded + ',[data-type="vscroller"]')
    || panel.querySelector('input,textarea,[contenteditable="true"]')) return null;
  let action = dismiss[0].closest('button,[role="button"],'
    + '[data-mcomponent="MContainer"][data-action-id]');
  return action && panel.contains(action) ? action : null;
}
"""


AUTH_SCREEN_SCRIPT = r"""
() => /\/(?:login|recover|checkpoint|two_step_verification|consent|captcha)(?:\/|$)/i
  .test(location.pathname) || Array.from(document.querySelectorAll(
    'input[name="email"],input[name="pass"],input[name="approvals_code"],'
    + 'iframe[src*="captcha"]'
  )).some(node => node.checkVisibility({visibilityProperty:true,opacityProperty:true}))
"""


RENDER_SNAPSHOT_SCRIPT = r"""
() => {
  const visible = node => node.checkVisibility({contentVisibilityAuto:true,
    visibilityProperty:true,opacityProperty:true});
  const count = selector => Array.from(document.querySelectorAll(selector))
    .filter(visible).length;
  return {
    document_ready: document.readyState,
    document_visible: document.visibilityState === 'visible',
    mobile_screens: document.querySelectorAll('[data-mcomponent="MScreen"]').length,
    total_scrollers: document.querySelectorAll('[data-type="vscroller"]').length,
    primary_menus: document.querySelectorAll(
      '[role="button"][aria-label^="More options for "]').length,
    visible_primary_menus: count('[role="button"][aria-label^="More options for "]'),
    loading_indicators: count('[role="progressbar"],[role="status"][aria-busy="true"]'),
    dialogs: count('[role="dialog"],[aria-modal="true"]'),
    visible_text_chars: Math.min((document.body?.innerText || '').trim().length,100000),
    visible_controls: count('button,a[href],input,select,textarea,[role="button"],'
      + '[role="link"],[role="textbox"],[contenteditable="true"]'),
    visible_media: count('img,video,canvas,svg,[role="img"],'
      + '[data-mcomponent="ImageArea"],[data-mcomponent="ServerImageArea"],'
      + '[data-mcomponent="VideoArea"],[data-mcomponent="ServerVideoArea"]'),
  };
}
"""


STRUCTURE_SNAPSHOT_SCRIPT = r"""
() => {
  __FBN_ATTACHMENT_CONTROLS__
  const allowed = {
    tag: ['BODY','DIV','SPAN','BUTTON','A','IMG','VIDEO','INPUT','TEXTAREA',
      'MAIN','SECTION','H1','H2','H3','P','UL','LI','SVG'],
    component: ['MContainer','MScreen','TextArea','ServerTextArea','ImageArea',
      'ServerImageArea','VideoArea','ServerVideoArea'],
    role: ['button','link','dialog','progressbar','textbox','article','group',
      'radiogroup','radio','img','heading'],
    type: ['vscroller','container','loading'],
    block: ['header','message','attachment','interface_panel','footer',
      'discussion','unknown'],
  };
  const safe = (value, kind) => value === null ? null
    : allowed[kind].includes(value) ? value : 'other';
  const nodes = [], queue = document.body ? [[document.body,-1,0]] : [];
  const roles = window.__fbnMobileBlockRoles;
  let cursor=0;
  while (cursor < queue.length && nodes.length < 1000) {
    const [node,parent,depth] = queue[cursor++];
    const index=nodes.length, component=node.getAttribute('data-mcomponent');
    nodes.push({parent,tag:safe(node.tagName,'tag'),component:safe(component,'component'),
      role:safe(node.getAttribute('role'),'role'),
      type:safe(node.getAttribute('data-type'),'type'),
      block:safe(roles?.get(node) || null,'block'),
      control:attachmentControlKind(node),
      hasAction:node.hasAttribute('data-action-id'),hasLabel:node.hasAttribute('aria-label'),
      visible:node.checkVisibility({contentVisibilityAuto:true,
        visibilityProperty:true,opacityProperty:true}),
      textChars: /^(?:Server)?TextArea$/.test(component || '')
        ? Math.min(node.textContent.length,100000) : 0});
    if (depth < 20) for (const child of node.children) {
      if (queue.length >= 1000) break;
      queue.push([child,index,depth+1]);
    }
  }
  return {version:1,nodes,truncated:queue.length >= 1000
    || nodes.some((node,index) => queue[index]?.[2] === 20)};
}
""".replace("__FBN_ATTACHMENT_CONTROLS__", ATTACHMENT_CONTROL_SCRIPT)
