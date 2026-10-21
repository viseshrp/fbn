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
"""
