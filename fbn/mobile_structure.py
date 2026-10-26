"""Named header recognition, independent of body extraction and acceptance."""

HEADER_COMPONENT_SCRIPT = r"""
  const recognizeHeader = (header, menu) => {
    const owner = clean(menu.getAttribute('aria-label').slice(17));
    const authors = Array.from(header.querySelectorAll('[role="link"],a'))
      .filter(node => visible(node) && clean(node.innerText) === owner);
    if (!owner || authors.length !== 1) {
      return {status: 'ambiguous', missing: 'matching_primary_author'};
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
      return {status: 'ambiguous', missing: 'unique_primary_timestamp'};
    }
    return {status: 'recognized', owner, timestamp: distinctTimestamps[0]};
  };

  // This proves a known incomplete assembly, not which menu is the main one.
  // Nothing from such a card is accepted; only a fresh acquisition may recover.
  const headerMetadataOnly = (root, menu, result) => {
    if (result.status !== 'recognized') return false;
    const author = Array.from(root.querySelectorAll('[role="link"],a'))
      .find(node => visible(node) && clean(node.innerText) === result.owner);
    const buttons = Array.from(root.querySelectorAll('button,[role="button"]'))
      .filter(visible);
    const avatars = buttons.filter(node => node !== menu
      && attachmentControlKind(node) === 'credit_picture'
      && [result.owner + "'s picture", result.owner + '\u2019s picture',
        result.owner + ' profile picture']
          .includes(clean(node.getAttribute('aria-label')))
      && !clean(node.innerText).replace(/\p{Co}/gu, '')
      && node.querySelector('img,[data-mcomponent="ImageArea"],'
        + '[data-mcomponent="ServerImageArea"]'));
    if (buttons.some(node => node !== menu && !avatars.includes(node))
      || root.querySelector('input,select,textarea,[contenteditable="true"],'
        + '[role="textbox"],[role="radio"],[role="checkbox"],[role="switch"]')) {
      return false;
    }
    const owned = [author, result.timestamp.node, menu, ...avatars];
    const covered = node => owned.some(owner => owner && owner.contains(node));
    if (Array.from(root.querySelectorAll('*')).filter(node =>
      node.hasAttribute('data-action-id') || node.hasAttribute('tabindex')
      || Array.from(node.attributes).some(attr => /^on/i.test(attr.name))).some(node =>
      visible(node) && !owned.includes(node) && !menu.contains(node)
      && !avatars.some(avatar => avatar.contains(node))
      && !(author.contains(node) && clean(node.innerText) === result.owner)
      && !(result.timestamp.node.contains(node)
        && timestampPattern.test(clean(node.innerText)))
      && !owned.some(owner => owner && node.contains(owner)))) return false;
    if (Array.from(root.querySelectorAll('a,[role="link"]')).some(node =>
      visible(node) && !covered(node))) return false;
    if (Array.from(root.querySelectorAll('img,video,[data-mcomponent="ImageArea"],'
      + '[data-mcomponent="ServerImageArea"]')).some(node => visible(node)
        && !avatars.some(avatar => avatar.contains(node)))) return false;
    let visited = 0;
    const metadata = (node, depth = 0) => {
      if (++visited > 12000 || depth > 192) return false;
      if (node.nodeType === Node.TEXT_NODE) {
        return !clean(node.textContent.replace(/\p{Co}/gu, ''));
      }
      if (!(node instanceof Element)) return true;
      if (!visible(node) && getComputedStyle(node).display !== 'contents') return true;
      if (covered(node)) return true;
      const component = node.getAttribute('data-mcomponent');
      if (component && !['MContainer','TextArea','ServerTextArea'].includes(component)
        || node.hasAttribute('role') || node.matches(excludedSelector)
        || Array.from(node.attributes).some(attr => /^on/i.test(attr.name))) {
        return false;
      }
      if ((node.hasAttribute('data-action-id') || node.hasAttribute('tabindex'))
        && !owned.some(owner => owner && node.contains(owner))) return false;
      return Array.from(node.childNodes).every(child => metadata(child, depth + 1));
    };
    return metadata(root);
  };

  const recognizeHeaderAssembly = (card, menus) => {
    if (menus.length < 2 || menus.length > 8) return false;
    const sections = sectionsFor(card);
    const headers = menus.map(menu => ({menu, root:branch(card, menu)}));
    if (headers.some(header => !header.root)
      || new Set(headers.map(header => header.root)).size !== menus.length) {
      return false;
    }
    for (const header of headers) {
      const result = recognizeHeader(header.root, header.menu);
      if (!headerMetadataOnly(header.root, header.menu, result)) return false;
    }
    const first = Math.min(...headers.map(header => sections.indexOf(header.root)));
    const footer = recognizeFooter(sections, first + 1);
    if (first < 0 || footer.status !== 'recognized'
      || headers.some(header => sections.indexOf(header.root) >= footer.index)) {
      return false;
    }
    const headerRoots = new Set(headers.map(header => header.root));
    const prefix = parseBody(sections.slice(0, first));
    const body = parseBody(sections.slice(first + 1, footer.index)
      .filter(section => !headerRoots.has(section)));
    if (prefix.unsupported || prefix.text || prefix.bodyControlCount
      || body.unsupported || (!body.text && !body.mediaCount)) return false;
    for (const header of headers) roles.set(header.root, 'header');
    return true;
  };

  // A feed wrapper is not a post boundary. Some chronological-feed wrappers
  // hold several sibling header/body units and paint only the last footer.
  // These are acquisition candidates: every unit still needs an independently
  // copied, same-group permalink and exact complete destination comparison.
  const recognizeCompoundPosts = (card, menus) => {
    if (menus.length < 2 || menus.length > 8) return null;
    const sections = sectionsFor(card);
    const headers = menus.map(menu => ({menu, header:branch(card, menu)}));
    if (headers.some(unit => !unit.header)
      || new Set(headers.map(unit => unit.header)).size !== menus.length) {
      return null;
    }
    let previous = -1;
    for (const unit of headers) {
      unit.index = sections.indexOf(unit.header);
      unit.headerResult = recognizeHeader(unit.header, unit.menu);
      if (unit.index <= previous
        || !headerMetadataOnly(unit.header, unit.menu, unit.headerResult)) {
        return null;
      }
      previous = unit.index;
    }
    const prefix = parseBody(sections.slice(0, headers[0].index));
    if (prefix.unsupported || prefix.text || prefix.mediaKeys.length
      || prefix.bodyControlCount || prefix.partial
      || sections.slice(0, headers[0].index).some(section =>
        section.matches('img,video,[role="img"]')
        || section.querySelector('img,video,[role="img"]'))) return null;
    for (let index = 0; index < headers.length; index++) {
      const unit = headers[index], next = headers[index + 1];
      unit.start = unit.index + 1;
      const footer = recognizeFooter(sections, unit.start);
      if (next) {
        if (footer.status === 'incomplete') return null;
        const ownFooter = footer.status === 'recognized' && footer.index < next.index;
        unit.end = ownFooter ? footer.index : next.index;
        unit.footer = ownFooter ? sections[footer.index] : null;
        if (ownFooter) {
          const gap = parseBody(sections.slice(footer.index + 1, next.index));
          if (gap.unsupported || gap.text || gap.mediaCount
            || gap.bodyControlCount || gap.partial) return null;
        }
      } else {
        if (footer.status !== 'recognized' || footer.index < unit.start) {
          return null;
        }
        unit.end = footer.index;
        unit.footer = sections[footer.index];
      }
      unit.sections = sections;
      unit.body = parseBody(sections.slice(unit.start, unit.end));
      if (unit.body.unsupported || (!unit.body.text && !unit.body.mediaCount)
        || unit.body.partial && unit.body.expanders.length !== 1) return null;
    }
    return headers;
  };

  const postUnitMenus = card => {
    const initial = primaryMenus(card);
    return Array.from(card.querySelectorAll(menuSelector)).filter(menu => {
      if (!visible(menu) || menu.closest(excludedSelector)) return false;
      if (initial.includes(menu)) return true;
      const header = branch(card, menu);
      return header && headerMetadataOnly(header, menu, recognizeHeader(header, menu));
    });
  };
"""
