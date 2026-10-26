"""Named mobile-body recognizers over the normalized ownership tree.

Each recognizer declares owned regions and a fixed-name outcome. Extraction
and acceptance are separate; unknown controls or conflicting claims reject
the body rather than being silently omitted.
"""

BODY_COMPONENTS_SCRIPT = r"""
  const parseBody = sections => {
    const tree = normalizeBody(sections);
    const {nodes, contains, ancestors, descendants, text, read} = tree;
    const claims = new Map(), outcomes = [], issues = new Set();
    const expanders = [], mediaKeys = new Set();
    let mediaCount = 0, bodyControlCount = 0;
    const within = (node, role) => Boolean(ancestors(node,
      owner => claims.get(owner)?.role === role));
    const own = node => !within(node, 'discussion') && !within(node, 'interface_panel');
    const messageOwn = node => own(node) && !within(node, 'attachment')
      && !within(node, 'expander') && !within(node, 'unknown');
    const claim = (node, role, recognizer) => {
      const previous = claims.get(node);
      if (previous && previous.role !== role) {
        issues.add('conflicting_region_ownership'); return false;
      }
      claims.set(node, {role, recognizer}); return true;
    };
    const run = (name, recognize) => {
      const before = claims.size, issueCount = issues.size;
      recognize(name);
      outcomes.push({name, status: issues.size > issueCount ? 'ambiguous'
        : claims.size > before ? 'recognized' : 'absent'});
    };
    const leaves = node => [node, ...descendants(node)].filter(child => child.flags.text
      && !descendants(child).some(other => other.flags.text) && text(child));
    const recognizeDiscussion = name => {
      for (const node of nodes.filter(node => node.flags.discussion)) {
        claim(node, 'discussion', name);
      }
    };
    const recognizeAttachments = name => {
      const creditPictures = new Set();
      for (const node of nodes) {
        if (!own(node) || ancestors(node.parent,
          owner => owner.flags.text)) continue;
        const images = descendants(node).filter(child => child.flags.image);
        const linkedMedia = descendants(node).filter(child => child.flags.image
          || child.el.nodeType === Node.ELEMENT_NODE && child.el.tagName === 'VIDEO');
        let attachment = node.flags.poll;
        if (node.el.nodeType !== Node.ELEMENT_NODE) continue;
        if (node.el.getAttribute('role') === 'img'
          && !descendants(node).some(child => child.flags.menu) && images.length) {
          attachment = true;
        }
        if (!attachment && linkedMedia.length && node.flags.link) {
          if (node.el.hasAttribute('data-action-id') && !node.el.hasAttribute('href')) {
            attachment = true;
          } else if (node.el.hasAttribute('href')) try {
            const url = new URL(node.el.getAttribute('href'), location.href);
            attachment = ['http:','https:'].includes(url.protocol);
            if (/(^|\.)facebook\.com$/.test(url.hostname)) {
              const listing = url.pathname.match(/^\/marketplace\/item\/(\d+)\/?$/);
              if (listing) mediaKeys.add('listing:' + listing[1]);
            }
          } catch {}
        }
        if (attachment) claim(node, 'attachment', name);
      }
      // A media-opening button is an attachment surface only when its label
      // identifies that operation and every child is owned media/presentation.
      // An arbitrary button near an image must remain an unknown control.
      const mediaOnly = node => claims.get(node)?.role === 'attachment'
        || (node.flags.media && !text(node))
        || (node.kind === 'literal' ? !text(node)
          : node.transparent && node.children.length > 0
            && node.children.every(mediaOnly));
      const supportedStructure = node => node.el.nodeType !== Node.ELEMENT_NODE
        || ((!node.component || ['MContainer','TextArea','ServerTextArea',
          'ImageArea','ServerImageArea','VideoArea','ServerVideoArea']
          .includes(node.component))
          && (!node.el.hasAttribute('role')
            || ['button','img','link'].includes(node.el.getAttribute('role')))
          && (node.component !== 'MContainer' || !node.el.hasAttribute('data-type')
            || node.el.getAttribute('data-type') === 'container'));
      const untypedAction = node => node.flags.action
        && node.component === 'MContainer' && !node.el.hasAttribute('role')
        && supportedStructure(node) && own(node);
      for (const node of nodes.filter(node => own(node) && node.flags.button)) {
        const kind = attachmentControlKind(node.el);
        if (!['photo','video','credit_picture'].includes(kind)
          || !node.children.length) continue;
        const media = descendants(node).filter(child => child.flags.media);
        if (!media.length || !node.children.every(mediaOnly)) continue;
        if (descendants(node).some(child => !supportedStructure(child)
          || child.flags.button || child.flags.control
          || child.flags.menu || child.flags.link || child.flags.action)) continue;
        if (kind === 'video' && !media.some(child => child.el.matches(
          'video,[data-mcomponent="VideoArea"],'
          + '[data-mcomponent="ServerVideoArea"]'))) continue;
        if (kind !== 'video' && !media.some(child => child.flags.image)) continue;
        if (kind === 'credit_picture') { creditPictures.add(node); continue; }
        claim(node, 'attachment', name);
      }
      // A media card can place its credit/date beside the opening button.
      // That metadata is not authored body text. Require the complete credit
      // shape (one linked name and one accessible publication date), not merely
      // two nearby text areas, before composing the attachment's ownership.
      for (const node of nodes.filter(untypedAction)) {
        if (claims.get(node)?.role === 'attachment') continue;
        const content = descendants(node);
        const surfaces = content.filter(child => child.flags.button
          && (creditPictures.has(child)
            || (claims.get(child)?.role === 'attachment'
              && ['photo','video'].includes(attachmentControlKind(child.el)))));
        if (surfaces.length !== 1) continue;
        const surface = surfaces[0];
        const parts = leaves(node).filter(child => !contains(surface, child));
        if (parts.length !== 2) continue;
        const [credit, date] = parts;
        if (creditPictures.has(surface)
          && ![text(credit) + "'s picture", text(credit) + '\u2019s picture',
            text(credit) + ' profile picture']
            .includes(clean(surface.el.getAttribute('aria-label')))) continue;
        const links = descendants(credit).filter(child => child.flags.link);
        if (links.length !== 1 || text(links[0]) !== text(credit)
          || !date.el.hasAttribute('aria-label')
          || !timestampPattern.test(clean(date.el.getAttribute('aria-label')))
          || !(timestampPattern.test(text(date))
            || /^(?:today|yesterday)$/i.test(text(date)))) continue;
        const caption = ancestors(credit, child => child !== node
          && child.flags.action && contains(child, date));
        if (!caption || !contains(node, caption)) continue;
        if (content.some(child => !contains(surface, child)
          && (!supportedStructure(child)
            || child.flags.button || child.flags.control || child.flags.menu
            || child.flags.discussion || child.flags.media
            || (child.flags.link && !contains(credit, child))
            || (child.flags.action && child !== caption
              && !contains(credit, child))
            || (child.kind === 'literal' && text(child)
              && !ancestors(child.parent, owner => owner.flags.text))))) continue;
        claim(surface, 'attachment', name);
        claim(node, 'attachment', name);
      }
      // Event previews have their own image, date/title and response control.
      // Claim that bounded card, never an enclosing body or its authored text.
      const eventDatePattern = new RegExp(
        '^(?:(?:mon|tue|wed|thu|fri|sat|sun)'
        + '(?:day|sday|nesday|rsday|urday)?[, ]+)?'
        + '(?:yesterday|today|tomorrow|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?'
        + '|apr(?:il)?'
        + '|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?'
        + '|nov(?:ember)?|dec(?:ember)?)\\s+(?:0?[1-9]|[12][0-9]|3[01])'
        + '(?:[, ]+[12][0-9]{3})?)'
        + '(?:[, ]+(?:at\\s+)?(?:(?:0?[1-9]|1[0-2])'
        + '(?::[0-5][0-9])?\\s*(?:am|pm)'
        + '|(?:[01]?[0-9]|2[0-3]):[0-5][0-9])'
        + '(?:\\s+(?:UTC|GMT|[ECMP][SD]T))?)?$', 'i');
      const eventDate = value => eventDatePattern.test(
        clean(value.replace(/\p{Co}/gu, '')));
      for (const node of nodes.filter(untypedAction)) {
        if (claims.get(node)?.role === 'attachment') continue;
        const content = descendants(node);
        const buttons = content.filter(child => child.flags.button);
        if (buttons.length !== 1 || !['event_interest','event_response','event_open']
          .includes(attachmentControlKind(buttons[0].el))) continue;
        const button = buttons[0];
        const buttonText = clean(text(button).replace(/\p{Co}/gu, ''));
        if (buttonText && !/^(?:interested|going|not going|(?:view|see|open) event)$/i
          .test(buttonText)) continue;
        if (!content.some(child => child.flags.image)
          || content.some(child => !supportedStructure(child)
            || child.flags.control || child.flags.menu
            || child.flags.discussion || child.flags.link
            || (child.flags.action && !contains(button, child))
            || (child.flags.action && contains(button, child)
              && !leaves(child).length)
            || (child.component && !['MContainer','TextArea','ServerTextArea',
              'ImageArea','ServerImageArea'].includes(child.component))
            || (child.el.nodeType === Node.ELEMENT_NODE
              && child.el.hasAttribute('role')
              && !['button','img'].includes(child.el.getAttribute('role'))))) continue;
        const parts = leaves(node).filter(child => !contains(button, child));
        if (parts.length !== 2 || parts.filter(child => eventDate(text(child)))
          .length !== 1) continue;
        if (content.some(child => child.kind === 'literal' && text(child)
          && !ancestors(child.parent, owner => owner.flags.text))) continue;
        claim(node, 'attachment', name);
      }
      // A numbered overlay belongs to a photo mosaic only when its accessible
      // count agrees with its visible count and the entire bounded mosaic has
      // no authored text, other controls or unowned actions. Never click it.
      for (const button of nodes.filter(node => own(node) && node.flags.button
        && attachmentControlKind(node.el) === 'gallery_more')) {
        const label = clean(button.el.getAttribute('aria-label'));
        if (clean(text(button)).toLowerCase() + ' more' !== label.toLowerCase()) {
          continue;
        }
        for (let gallery = button.parent; gallery; gallery = gallery.parent) {
          if (!supportedStructure(gallery) || gallery.flags.button
            || gallery.flags.control || gallery.flags.link || gallery.flags.discussion
            || gallery.el.getAttribute('role')) break;
          const content = descendants(gallery);
          const images = content.filter(child => child.flags.image
            && !ancestors(child.parent, owner => owner !== gallery
              && owner.flags.image));
          if (images.length < 2) continue;
          if (content.some(child => !supportedStructure(child)
            || child.flags.control || child.flags.menu || child.flags.discussion
            || child.flags.link || (child.flags.button && child !== button)
            || (child.flags.action && child !== button && !child.flags.image)
            || (text(child) && !contains(button, child)
              && !contains(child, button)))) break;
          if (leaves(gallery).some(child => !contains(button, child))) break;
          claim(button, 'attachment', name);
          claim(gallery, 'attachment', name);
          break;
        }
      }
    };
    const recognizeSplitPreviews = name => {
      for (const image of nodes.filter(node => node.flags.link
        && claims.get(node)?.role === 'attachment' && !text(node)
        && node.el.hasAttribute('data-action-id'))) {
        const firstText = nodes.find(node => node.order >= image.end
          && node.section === image.section && node.flags.text && messageOwn(node)
          && !descendants(node).some(child => child.flags.text) && text(node));
        if (!firstText) continue;
        const caption = ancestors(firstText, node => node.flags.action
          && node.component === 'MContainer');
        if (!caption || caption.section !== image.section) continue;
        const barrier = nodes.some(node => node.order >= image.end
          && node.order <= firstText.order && node.section === image.section
          && (node.flags.discussion || node.flags.button || node.flags.control
            || node.flags.link));
        if (barrier) continue;
        let owner = caption.parent, ambiguous = false;
        while (owner && !contains(owner, image)) {
          if (owner.flags.action) { ambiguous = true; break; }
          owner = owner.parent;
        }
        if (ambiguous || !owner || owner.section !== image.section) continue;
        const parts = leaves(caption);
        if (parts.length !== 2 || parts[0] !== firstText
          || !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$/i
            .test(text(parts[0]))) continue;
        if (caption.flags.text || caption.flags.discussion || caption.flags.button
          || caption.flags.control || caption.flags.link
          || descendants(caption).some(node => node.flags.discussion
            || node.flags.button || node.flags.control || node.flags.link
            || node.flags.image || node.flags.action)
          || text(caption) !== clean(parts.map(text).join(' '))) continue;
        claim(caption, 'attachment', name);
      }
    };
    const recognizeInterfacePanels = name => {
      for (const root of tree.roots) {
        const buttons = descendants(root).filter(node =>
          node.flags.button && messageOwn(node));
        const buttonName = node => clean(
          node.el.getAttribute('aria-label') || text(node));
        const positive = buttons.filter(node => buttonName(node) === 'Interested');
        const negative = buttons.filter(node => buttonName(node) === 'Not interested');
        if (positive.length !== 1 || negative.length !== 1) continue;
        for (let panel = positive[0].parent; panel; panel = panel.parent) {
          if (!contains(panel, negative[0])
            || descendants(panel).some(node => node.flags.menu)
            || buttons.filter(node => contains(panel, node)).length !== 2) continue;
          const parts = leaves(panel);
          const labels = ['Are you interested in this post?',
            'Interested', 'Not interested'];
          if (parts.some(node => text(node) === labels[0])
            && parts.every(node => labels.includes(text(node)))) {
            claim(panel, 'interface_panel', name); break;
          }
        }
      }
    };
    const recognizeExpanders = name => {
      const candidates = nodes.filter(node => node.el.nodeType === Node.ELEMENT_NODE
        && node.el.matches('span,button,[role="button"]') && messageOwn(node)
        && (/^(?:\.{3}|…)\s*see more$/i.test(text(node))
          || (/^see more$/i.test(text(node))
            && ancestors(node, owner => owner.flags.button)))
        && !ancestors(node, owner => owner.flags.link)
        && ancestors(node, owner => owner.flags.button || (owner.flags.text
          && owner.el.hasAttribute('data-action-id'))));
      for (const node of candidates.filter(node => !candidates.some(other =>
        other !== node && contains(node, other)))) {
        expanders.push(node.el); claim(node, 'expander', name);
      }
    };
    const recognizeMedia = name => {
      const media = nodes.filter(node => node.flags.media && own(node)
        && !ancestors(node, owner => owner.flags.text || (owner.flags.button
          && !(claims.get(owner)?.role === 'attachment'
            && ['photo','video','credit_picture']
              .includes(attachmentControlKind(owner.el))))));
      for (const node of media.filter(node => !media.some(other =>
        other !== node && contains(other, node)))) {
        mediaCount++; claim(node, 'attachment', name);
        const anchor = ancestors(node, owner => owner.flags.link);
        const sourceNode = [node, ...descendants(node)].find(child =>
          child.el.nodeType === Node.ELEMENT_NODE && child.el.hasAttribute('src'));
        const source = sourceNode?.el.getAttribute('src')
          || node.el.style.backgroundImage?.match(/url\(["']?(.*?)["']?\)/)?.[1];
        for (const value of [anchor?.el.getAttribute('href'), source]) {
          if (!value) continue;
          try {
            const url = new URL(value, location.href);
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
    };
    const recognizeUnknownControls = name => {
      for (const node of nodes.filter(node => (node.flags.button || node.flags.control
        || node.flags.menu || (node.flags.link && !ancestors(node.parent,
          owner => owner.flags.text))) && own(node))) {
        if (within(node, 'attachment')) continue;
        if (expanders.some(el => node.el.contains(el) || el.contains(node.el))) {
          bodyControlCount++; continue;
        }
        claim(node, 'unknown', name); issues.add('unknown_body_control');
      }
    };
    const recognizeActionOwnership = name => {
      // Compose ownership from the leaves up. A clickable media shell is not
      // an unknown control when every child is an already-owned attachment or
      // transparent presentation. Empty actions, text and controls cannot earn
      // attachment ownership merely by being near an image.
      const attachmentOnly = node => claims.get(node)?.role === 'attachment'
        || (node.kind === 'literal' ? !text(node)
          : node.transparent && node.children.length > 0
            && node.children.every(attachmentOnly));
      for (const node of nodes.filter(node => node.flags.action && !node.flags.text
        && messageOwn(node) && !ancestors(node.parent, owner => owner.flags.text))
        .reverse()) {
        if (node.flags.button || node.flags.control || node.flags.link
          || node.flags.menu) continue;
        if (node.children.length && node.children.every(attachmentOnly)) {
          claim(node, 'attachment', name); continue;
        }
        const content = descendants(node).filter(messageOwn);
        const messageRoots = content.filter(child => child.flags.text
          && !ancestors(child.parent, owner => owner !== node && owner.flags.text));
        const ownedMessage = messageRoots.length > 0 && content.every(child =>
          messageRoots.some(root => contains(root, child))
          || (!child.flags.action && !child.flags.button && !child.flags.control
            && !child.flags.link && !child.flags.media));
        if (ownedMessage) claim(node, 'message', name);
        else { claim(node, 'unknown', name); issues.add('unknown_action_ownership'); }
      }
    };
    run('discussion', recognizeDiscussion);
    run('attachments', recognizeAttachments);
    run('split_preview', recognizeSplitPreviews);
    run('interface_panel', recognizeInterfacePanels);
    run('expander', recognizeExpanders);
    run('media', recognizeMedia);
    run('action_ownership', recognizeActionOwnership);
    run('unknown_controls', recognizeUnknownControls);
    if (tree.overflow) issues.add('normalization_budget');
    // Every meaningful literal must belong to an explicit message or an owned
    // attachment/UI/discussion region. Unknown text is not silently discarded.
    for (const node of nodes.filter(node => node.kind === 'literal' && text(node)
      && messageOwn(node) && !ancestors(node.parent, owner => owner.flags.text))) {
      claim(node.parent || node, 'unknown', 'unclaimed_text');
      issues.add('unclaimed_body_text');
    }
    const texts = [];
    for (const node of nodes.filter(node => node.flags.text && messageOwn(node)
      && !ancestors(node.parent, owner => owner.flags.text))) {
      const value = clean(read(node, messageOwn));
      if (value) { texts.push(value); claim(node, 'message', 'message'); }
    }
    const regions = [], blocks = [];
    for (const [node, evidence] of claims) {
      if (evidence.role === 'expander') continue;
      roles.set(node.el, evidence.role);
      regions.push({role: evidence.role, node: node.el});
      blocks.push({role: evidence.role, textChars: text(node).length,
        component: node.component || null});
    }
    return {text: clean(texts.join(' ')), texts, expanders, mediaCount,
      mediaKeys: Array.from(mediaKeys).sort(), partial: expanders.length > 0,
      bodyControlCount, blocks, regions, unsupported: issues.size > 0,
      recognizers: outcomes, issues: Array.from(issues).sort(),
      normalization: {version: 1, nodes: nodes.length, collapsed: tree.collapsed}};
  };
"""
