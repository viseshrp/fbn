"""Shared mobile card grammar and content-only snapshots.

DOM handles remain in the browser; PostView is the internal JSON boundary.
No URL, author, action ID, or body value belongs in structural diagnostics.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict

from .extractor import _FACEBOOK_TIMESTAMP_RE


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


# Bloks cards are direct children of a vscroller. The contextual menu belongs
# to the primary header; its author must agree with that header's visible link.
# Neither the footer nor any following comment preview is a content section.
MOBILE_SCAN_SCRIPT = r"""
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
  const branch = (root, node) => {
    while (node && node.parentElement !== root) node = node.parentElement;
    return node;
  };
  const sectionsFor = card => Array.from(card.children).filter(node =>
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
  const footerIndex = (sections, start) => {
    for (let index = start; index < sections.length; index++) {
      const like = action(sections[index], /^(?:like|unlike)(?:,|$)/);
      const comment = action(sections[index], /^(?:make a comment|comments?)$/);
      // The first action row belongs to the post. If it is incomplete, wait
      // for it; never scan through replies to find a later substitute footer.
      // Its aria labels remain usable when content-visibility:auto defers paint.
      if (like || comment) return like && comment ? index : -1;
    }
    return -1;
  };

  // Partition first, extract second. UI/attachments never participate in the
  // message's prefix, completeness, or text/media identity.
  const parseBody = sections => {
    const excluded = [], attachments = [], regions = [], blocks = [];
    const texts = [], expanders = [];
    const mediaKeys = new Set();
    let mediaCount = 0, bodyControlCount = 0, unsupported = false;
    const describe = (role, node) => {
      roles.set(node, role);
      regions.push({role, node});
      blocks.push({role, textChars: clean(node.innerText).length,
        component: node.getAttribute('data-mcomponent') || null});
    };
    for (const section of sections) {
      for (const node of [section, ...section.querySelectorAll(excludedSelector)]) {
        if (node.matches(excludedSelector)) {
          excluded.push(node); describe('discussion', node);
        }
      }
      for (const menu of section.querySelectorAll(menuSelector)) {
        // Known discussion owns its own exclusion. A wrapper containing both
        // message and quoted menu must not make the whole message disappear.
        if (menu.closest(excludedSelector)) continue;
        unsupported = true;
        describe('unknown', menu);
      }
      const own = node => !node.closest(excludedSelector)
        && !excluded.some(root => root.contains(node));
      const messageOwn = node => own(node)
        && !attachments.some(root => root.contains(node));
      const imageSelector = 'img,[data-mcomponent="ImageArea"],'
        + '[data-mcomponent="ServerImageArea"]';
      const attachmentSelector = '[role="radiogroup"],'
        + '[role="group"][aria-label="Poll"],[role="img"],a[href],[role="link"]';
      for (const node of [section, ...section.querySelectorAll(attachmentSelector)]
        .filter(node => node.matches(attachmentSelector))) {
        if (!visible(node) || !own(node) || node.closest(textSelector)) continue;
        let attachment = node.matches(
          '[role="radiogroup"],[role="group"][aria-label="Poll"]');
        // Bloks image cards can own preview text and action controls without
        // any href. Require actual image content in this isolated image region;
        // a role label alone must never hide an unknown interactive message.
        if (node.matches('[role="img"]') && !node.querySelector(menuSelector)
          && Array.from(node.querySelectorAll(
            'img,[data-mcomponent="ImageArea"],[data-mcomponent="ServerImageArea"]'
          )).some(visible)) attachment = true;
        if (!attachment && Array.from(node.querySelectorAll(imageSelector))
          .some(visible)) {
          // Bloks links can expose only an action, not an href. Their linked
          // image is still an attachment; never promote a text-only action.
          if (node.matches('[role="link"][data-action-id]:not([href])')) {
            attachment = true;
          } else if (node.hasAttribute('href')) try {
            const url = new URL(node.getAttribute('href'), location.href);
            attachment = ['http:','https:'].includes(url.protocol);
            if (/(^|\.)facebook\.com$/.test(url.hostname)) {
              const listing = url.pathname.match(/^\/marketplace\/item\/(\d+)\/?$/);
              if (listing) mediaKeys.add('listing:' + listing[1]);
            }
          } catch {}
        }
        if (attachment) { attachments.push(node); describe('attachment', node); }
      }
      // A split Bloks link preview has a linked image, optional empty media
      // decorations, then a separately clickable domain/title caption. There is
      // no shared attachment wrapper: excluding the whole section would also
      // discard the authored message. Require this exact local grammar instead.
      for (const link of section.querySelectorAll('[role="link"][data-action-id]')) {
        if (!visible(link) || !own(link) || clean(link.innerText)
          || !attachments.includes(link)) continue;
        // Bloks can add arbitrary inert wrappers around the message/media.
        // Follow DOM order within this owned section, never section-level
        // siblings or a fixed nesting depth. The first text must be the domain;
        // crossing discussion, another link, or an unknown control is forbidden.
        const follows = node => !link.contains(node) && Boolean(
          link.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
        const firstText = Array.from(section.querySelectorAll(textSelector))
          .find(node => visible(node) && messageOwn(node) && clean(node.innerText)
            && !node.querySelector(textSelector) && follows(node));
        const captionBranch = firstText?.closest(
          '[data-mcomponent="MContainer"][data-action-id]');
        if (!captionBranch || !section.contains(captionBranch)) continue;
        if (Array.from(section.querySelectorAll(excludedSelector
          + ',button,[role="button"],[role="link"],a[href]')).some(node =>
          visible(node) && follows(node) && (node === firstText || Boolean(
            node.compareDocumentPosition(firstText)
              & Node.DOCUMENT_POSITION_FOLLOWING)))) {
          continue;
        }
        let owner = captionBranch.parentElement, ambiguous = false;
        while (owner && section.contains(owner) && !owner.contains(link)) {
          if (owner.hasAttribute('data-action-id')) { ambiguous = true; break; }
          owner = owner.parentElement;
        }
        if (ambiguous || !owner || !section.contains(owner)) continue;
        const parts = Array.from(captionBranch.querySelectorAll(textSelector))
          .filter(node => visible(node) && own(node) && clean(node.innerText)
            && !node.querySelector(textSelector));
        if (parts.length !== 2) continue;
        const domain = clean(parts[0].innerText);
        if (!/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$/i
          .test(domain)) continue;
        const caption = parts[0].closest(
          '[data-mcomponent="MContainer"][data-action-id]');
        if (!caption || !captionBranch.contains(caption)
          || !caption.contains(parts[1]) || caption.closest(textSelector)
          || captionBranch.matches(excludedSelector + ',button,[role="button"],'
            + '[role="link"],a[href]')
          || captionBranch.querySelector(excludedSelector + ',button,[role="button"],'
            + '[role="link"],a[href],' + imageSelector)
          || (captionBranch.hasAttribute('data-action-id') && captionBranch !== caption)
          || Array.from(captionBranch.querySelectorAll('[data-action-id]'))
            .some(node => node !== caption)
          || clean(caption.innerText) !== clean(captionBranch.innerText)
          || clean(caption.innerText) !== clean(
            parts.map(node => node.innerText).join(' '))) {
          continue;
        }
        attachments.push(caption); describe('attachment', caption);
      }
      const buttons = Array.from(section.querySelectorAll('button,[role="button"]'))
        .filter(node => visible(node) && messageOwn(node));
      const buttonName = node => clean(
        node.getAttribute('aria-label') || node.innerText);
      const positive = buttons.filter(node => buttonName(node) === 'Interested');
      const negative = buttons.filter(node => buttonName(node) === 'Not interested');
      // A literal sentence inside the message is not an interface panel. The
      // heading and both feedback actions must share their own isolated subtree.
      if (positive.length === 1 && negative.length === 1) {
        let panel = positive[0].parentElement;
        while (panel && section.contains(panel)) {
          if (panel.contains(negative[0]) && !panel.querySelector(menuSelector)
            && buttons.filter(node => panel.contains(node)).length === 2) {
            const nodes = [panel, ...panel.querySelectorAll(textSelector)]
              .filter(visible);
            const heading = nodes.some(node => node.matches(textSelector)
              && clean(node.innerText) === 'Are you interested in this post?');
            const leaves = nodes.filter(node => node.matches(textSelector)
              && !node.querySelector(textSelector))
              .map(node => clean(node.innerText)).filter(Boolean);
            if (heading && leaves.every(value => [
              'Are you interested in this post?', 'Interested', 'Not interested',
            ].includes(value))) {
              excluded.push(panel); describe('interface_panel', panel);
              break;
            }
          }
          panel = panel.parentElement;
        }
      }
      const candidates = Array.from(section.querySelectorAll(
        'span,button,[role="button"]'
      )).filter(node => visible(node) && messageOwn(node)
        && (/^(?:\.{3}|…)\s*see more$/i.test(clean(node.innerText))
          || (/^see more$/i.test(clean(node.innerText))
            && node.closest('button,[role="button"]')))
        && !node.closest('a,[role="link"]')
        && section.contains(node.closest('button,[role="button"],'
          + '[data-mcomponent="TextArea"][data-action-id],'
          + '[data-mcomponent="ServerTextArea"][data-action-id]')));
      const controls = candidates.filter(node => !candidates.some(
        other => other !== node && node.contains(other)
      ));
      expanders.push(...controls);
      const mediaNodes = Array.from(section.querySelectorAll(
        '[data-mcomponent="ServerImageArea"],[data-mcomponent="ImageArea"],img,video'
      )).filter(node => visible(node) && own(node)
        && !node.closest(textSelector + ',button,[role="button"]'));
      const topMedia = mediaNodes.filter(node => !mediaNodes.some(
        other => other !== node && other.contains(node)
      ));
      for (const node of topMedia) {
        mediaCount++;
        describe('attachment', node);
        const anchor = node.closest('a,[role="link"]');
        const source = node.getAttribute('src') || node.querySelector('[src]')?.src
          || node.style.backgroundImage?.match(/url\(["']?(.*?)["']?\)/)?.[1];
        for (const value of [anchor?.getAttribute('href'), source]) {
          if (!value) continue;
          try {
            const url = new URL(value, location.href);
            // Only stable Facebook attachment identifiers, never dimensions,
            // signatures, arbitrary URL strings, or a UI image count.
            if (/(^|\.)facebook\.com$/.test(url.hostname)) {
              const id = url.searchParams.get('fbid') || url.searchParams.get('v');
              if (/^\d{1,80}$/.test(id || '')) mediaKeys.add('attachment:' + id);
            } else if (/(^|\.)(?:fbcdn\.net|fbsbx\.com)$/.test(url.hostname)) {
              const pattern = /\/(\d{6,}_\d{6,}_\d+)_\w+\.(?:jpg|png|webp)$/i;
              const id = url.pathname.match(pattern);
              if (id) mediaKeys.add('image:' + id[1]);
            }
          } catch {}
        }
      }
      for (const node of buttons.filter(node => own(node))) {
        if (attachments.some(root => root.contains(node))) continue;
        if (controls.some(control => node.contains(control)
          || control.contains(node))) {
          bodyControlCount++; continue;
        }
        unsupported = true;
        describe('unknown', node);
        excluded.push(node);
      }
      excluded.push(...controls);
      const textNodes = [section, ...section.querySelectorAll(textSelector)]
          .filter(node => node.matches(textSelector)
            && visible(node) && messageOwn(node)
          && !node.parentElement.closest(textSelector));
      for (const node of textNodes) {
        const parts = [];
        const read = current => {
          if (current.nodeType === Node.TEXT_NODE) {
            parts.push(current.textContent); return;
          }
          if (!(current instanceof Element) || !visible(current)
            || !messageOwn(current)) return;
          const block = !['inline','inline-block','inline-flex','contents'].includes(
            getComputedStyle(current).display);
          if (block || current.tagName === 'BR') parts.push(' ');
          for (const child of current.childNodes) read(child);
          if (block) parts.push(' ');
        };
        read(node);
        const text = clean(parts.join(''));
        if (text) { texts.push(text); describe('message', node); }
      }
    }
    return {text: clean(texts.join(' ')), texts, expanders, mediaCount,
      mediaKeys: Array.from(mediaKeys).sort(), partial: expanders.length > 0,
      bodyControlCount, blocks, regions, unsupported};
  };

  for (const root of roots) {
    let cards = Array.from(root.children);
    if (permalink) {
      // Single-post pages can put header/body/actions directly in the scroller.
      // Only use that shape when one primary header precedes its own footer.
      const menus = primaryMenus(root), sections = sectionsFor(root);
      if (menus.length === 1) {
        const start = sections.indexOf(branch(root, menus[0])) + 1;
        if (footerIndex(sections, start) >= start) cards = [root];
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
      const owner = clean(menu.getAttribute('aria-label').slice(17));
      const authors = Array.from(header.querySelectorAll('[role="link"],a'))
        .filter(node => visible(node) && clean(node.innerText) === owner);
      if (!owner || authors.length !== 1) {
        missing.add('matching_primary_author'); continue;
      }
      const timestampLabel = node => {
        const stripOwner = value => value.startsWith(owner + ',')
          ? value.slice(owner.length + 1).replace(/^[,\s]+/, '') : value;
        let label = stripOwner(clean(node.getAttribute('aria-label'))
          .replace(/^[,\s]+/, ''));
        // Metadata prefixes must also exist as rendered, non-link text within
        // this exact wrapper. Do not maintain a growing list of badge names or
        // strip an unverified author's name merely because it precedes a date.
        for (let pass = 0; pass < 3; pass++) {
          const date = label.replace(/, (?:Private|Public) group$/, '');
          if (timestampPattern.test(date)) break;
          const prefix = Array.from(node.querySelectorAll(textSelector))
            .filter(child => visible(child) && !child.closest('[role="link"],a')
              && !child.querySelector('[role="link"],a')
              && !child.querySelector(menuSelector))
            .map(child => clean(child.innerText))
            .find(value => value && value.length <= 80
              && label.startsWith(value + ','));
          if (!prefix) break;
          // Permalink metadata can place a badge before the verified author.
          label = stripOwner(label.slice(prefix.length + 1)
            .replace(/^[,\s]+/, ''));
        }
        return label;
      };
      const timestamps = Array.from(header.querySelectorAll(textSelector)).filter(
        node => visible(node) && clean(node.getAttribute('aria-label'))
          && !authors[0].contains(node)
      ).map(node => ({node, label: timestampLabel(node)})).filter(item =>
        timestampPattern.test(item.label.replace(/, (?:Private|Public) group$/, ''))
      );
      // Metadata wrappers may contain the verified author and contributor badge
      // as well as the date. Only a recognized date label can qualify, and no
      // wrapper containing a post menu can become a timestamp.
      for (const node of header.querySelectorAll(
        '[data-mcomponent="MContainer"][aria-label]'
      )) {
        if (!visible(node) || node.contains(menu) || node.querySelector(menuSelector)
          || authors[0].contains(node)) continue;
        const label = timestampLabel(node);
        const date = label.replace(/, (?:Private|Public) group$/, '');
        if (timestampPattern.test(date)) timestamps.push({node, label});
      }
      const distinctTimestamps = timestamps.filter(item => !timestamps.some(
        other => other !== item && item.node.contains(other.node)
          && item.label === other.label
      ));
      if (distinctTimestamps.length !== 1) {
        missing.add('unique_primary_timestamp'); continue;
      }
      const timestamp = distinctTimestamps[0];
      const sections = sectionsFor(card);
      const start = sections.indexOf(header) + 1;
      const end = footerIndex(sections, start);
      if (end < start) { missing.add('primary_action_footer'); continue; }
      roles.set(sections[end], 'footer');
      const body = parseBody(sections.slice(start, end));
      if (body.unsupported) {
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
"""
