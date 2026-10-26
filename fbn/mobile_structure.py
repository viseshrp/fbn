"""Named header recognition, independent of body extraction and acceptance."""

HEADER_COMPONENT_SCRIPT = r"""
  const recognizeHeader = (header, menu, inferredOwner = null) => {
    const owner = inferredOwner === null
      ? clean(menu.getAttribute('aria-label').slice(17)) : inferredOwner;
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
  const headerMetadataOnly = (root, menu, result, groups = []) => {
    if (result.status !== 'recognized') return false;
    const author = Array.from(root.querySelectorAll('[role="link"],a'))
      .find(node => visible(node) && clean(node.innerText) === result.owner);
    const groupLinks = Array.from(root.querySelectorAll('[role="link"],a'))
      .filter(node => visible(node) && groups.includes(clean(node.innerText)));
    const identities = [result.owner, ...groups];
    const pictures = Array.from(root.querySelectorAll(
      'button,[role="button"],[role="link"],a')).filter(node => visible(node)
      && node !== menu && !clean(node.innerText).replace(/\p{Co}/gu, '')
      && identities.some(name => [name, name + "'s picture", name + '\u2019s picture',
        name + ' profile picture'].includes(clean(node.getAttribute('aria-label'))))
      && node.querySelector('img,[data-mcomponent="ImageArea"],'
        + '[data-mcomponent="ServerImageArea"]'));
    const buttons = Array.from(root.querySelectorAll('button,[role="button"]'))
      .filter(visible);
    // Group-post headers also use unlabelled image-only action branches for
    // the group thumbnail and author's avatar. They are metadata only with a
    // verified group attribution, avatar-sized bounds and no nested actions.
    const untypedPictures = groups.length ? Array.from(root.querySelectorAll(
      '[data-mcomponent="MContainer"][data-action-id]:not([role])'
    )).filter(node => {
      if (!visible(node) || clean(node.innerText).replace(/\p{Co}/gu, '')
        || !node.querySelector('img,[data-mcomponent="ImageArea"],'
          + '[data-mcomponent="ServerImageArea"]')) return false;
      const media = Array.from(node.querySelectorAll(
        'img,[data-mcomponent="ImageArea"],[data-mcomponent="ServerImageArea"]'
      )).map(image => image.getBoundingClientRect());
      // Hit areas may be wider than the thumbnail. Unloaded image children
      // may have zero bounds while their fixed ImageArea is already rendered.
      if (!media.some(rect => rect.width > 0 && rect.height > 0)
        || media.some(rect => rect.width > 80 || rect.height > 80)) return false;
      return Array.from(node.querySelectorAll('*')).every(child =>
        !child.matches('button,a,input,select,textarea,[role],'
          + '[contenteditable="true"],[data-action-id]')
        && !Array.from(child.attributes).some(attr => /^on/i.test(attr.name))
        && (!child.hasAttribute('data-mcomponent')
          || ['MContainer','ImageArea','ServerImageArea']
            .includes(child.getAttribute('data-mcomponent'))));
    }) : [];
    const avatars = [...pictures, ...untypedPictures];
    if (buttons.some(node => node !== menu && !avatars.includes(node))
      || root.querySelector('input,select,textarea,[contenteditable="true"],'
        + '[role="textbox"],[role="radio"],[role="checkbox"],[role="switch"]')) {
      return false;
    }
    const owned = [author, result.timestamp.node, menu, ...avatars, ...groupLinks];
    const covered = node => owned.some(owner => owner && owner.contains(node));
    if (Array.from(root.querySelectorAll('*')).filter(node =>
      node.hasAttribute('data-action-id') || node.hasAttribute('tabindex')
      || Array.from(node.attributes).some(attr => /^on/i.test(attr.name))).some(node =>
      visible(node) && !owned.includes(node) && !(menu && menu.contains(node))
      && !avatars.some(avatar => avatar.contains(node))
      && !groupLinks.some(group => group.contains(node))
      && !(author.contains(node) && clean(node.innerText) === result.owner)
      && !(result.timestamp.node.contains(node)
        && timestampPattern.test(clean(node.innerText)))
      && !owned.some(owner => owner && node.contains(owner)))) return false;
    if (Array.from(root.querySelectorAll('a,[role="link"]')).some(node =>
      visible(node) && !covered(node))) return false;
    if (Array.from(root.querySelectorAll('img,video,[data-mcomponent="ImageArea"],'
      + '[data-mcomponent="ServerImageArea"]')).some(node => visible(node)
        && ![...avatars, ...groupLinks].some(avatar => avatar.contains(node)))) {
      return false;
    }
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

  // One outer card owns its header and action row. A bounded chain of complete
  // attributed post headers/bodies before that row is embedded content, not a
  // continuation of the author's message or additional primary feed entries.
  const recognizeEmbeddedCard = (card, menus) => {
    if (!menus.length || menus.length > 8) return null;
    const sections = sectionsFor(card), menu = menus[0];
    const header = branch(card, menu), start = sections.indexOf(header) + 1;
    if (!header || start <= 0) return null;
    const main = recognizeHeader(header, menu);
    if (main.status !== 'recognized') return null;
    const names = Array.from(header.querySelectorAll('[role="link"],a'))
      .filter(visible).map(node => clean(node.innerText))
      .filter(name => name && name !== main.owner);
    const groups = Array.from(new Set(names));
    if (groups.length > 1 || !headerMetadataOnly(header, menu, main, groups)) {
      return null;
    }
    const prefix = parseBody(sections.slice(0, start - 1));
    if (prefix.unsupported || prefix.text || prefix.bodyControlCount) return null;
    const footer = recognizeFooter(sections, start);
    if (footer.status !== 'recognized') return null;
    const embedded = [];
    for (let index = start; index < footer.index; index++) {
      const section = sections[index];
      if (section.matches(excludedSelector)) continue;
      const options = Array.from(section.querySelectorAll(menuSelector));
      if (options.length > 1) return null;
      let result, option = options[0] || null;
      if (option) result = recognizeHeader(section, option);
      else {
        // Permalinks omit embedded menus. A group attribution must agree with
        // the primary header, and exactly one linked author must own the date.
        const links = Array.from(section.querySelectorAll('[role="link"],a'))
          .filter(visible).map(node => clean(node.innerText)).filter(Boolean);
        if (groups.length !== 1 || !links.includes(groups[0])) continue;
        const candidates = Array.from(new Set(links))
          .filter(name => !groups.includes(name))
          .map(name => recognizeHeader(section, null, name))
          .filter(value => value.status === 'recognized');
        if (candidates.length !== 1) continue;
        result = candidates[0];
      }
      if (!headerMetadataOnly(section, option, result, groups)) continue;
      embedded.push({index, menu:option});
    }
    if (!embedded.length || embedded.length > 8) return null;
    if (menus.some(option => option !== menu
      && !embedded.some(unit => unit.menu === option))) return null;
    const ownBody = parseBody(sections.slice(start, embedded[0].index));
    if (ownBody.unsupported || (!ownBody.text && !ownBody.mediaCount)) return null;
    for (let index = 0; index < embedded.length; index++) {
      const from = embedded[index].index + 1;
      const end = embedded[index + 1]?.index ?? footer.index;
      const body = parseBody(sections.slice(from, end));
      if (body.unsupported || (!body.text && !body.mediaCount)) return null;
    }
    for (const section of sections.slice(embedded[0].index, footer.index)) {
      roles.set(section, 'discussion');
      ownBody.regions.push({role:'discussion', node:section});
      ownBody.blocks.push({role:'discussion', textChars:clean(section.innerText).length,
        component:section.getAttribute('data-mcomponent')});
    }
    ownBody.recognizers.find(item => item.name === 'discussion').status = 'recognized';
    return {menu, body:ownBody};
  };
"""
