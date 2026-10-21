"""Generated presentation mutations, using synthetic pages only."""

DOM_VARIATION_SCRIPT = r"""
({kind, depth}) => {
  const typed = kind.startsWith('typed_');
  if (typed) kind = kind.slice(6);
  for (const card of document.querySelectorAll('[data-story]')) {
    const body = card.children[2];
    const wrap = (node, style) => {
      const outer = document.createElement('div');
      outer.setAttribute('data-mcomponent', 'MContainer');
      if (typed) outer.setAttribute('data-type','container');
      if (style) outer.style.cssText = style;
      node.replaceWith(outer); outer.append(node); return outer;
    };
    if (kind === 'body_wrappers' || kind === 'display_contents') {
      for (let index = 0; index < depth; index++) {
        const inner = document.createElement('div');
        inner.setAttribute('data-mcomponent', 'MContainer');
        if (typed) inner.setAttribute('data-type','container');
        if (kind === 'display_contents') inner.style.display = 'contents';
        inner.append(...body.childNodes); body.append(inner);
      }
    } else if (kind === 'branch_wrappers') {
      for (const child of Array.from(body.children)) {
        let current = child;
        for (let index = 0; index < depth; index++) current = wrap(current);
      }
    } else if (kind === 'split_text') {
      const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
      const leaves = []; while (walker.nextNode()) leaves.push(walker.currentNode);
      for (const leaf of leaves) {
        const value = leaf.textContent;
        const parts = [], step = Math.max(1, Math.ceil(value.length / (depth + 1)));
        for (let index = 0; index < value.length; index += step) {
          const span = document.createElement('span');
          span.textContent = value.slice(index, index + step); parts.push(span);
        }
        leaf.replaceWith(...parts);
      }
    } else if (kind === 'empty_decorations') {
      for (let index = 0; index < depth; index++) {
        const empty = document.createElement('div');
        empty.setAttribute('data-mcomponent','MContainer');
        if (typed) empty.setAttribute('data-type','container');
        body.insertBefore(empty, body.children[index % (body.children.length || 1)]);
      }
    } else if (kind === 'discussion_wrappers') {
      const discussions = body.querySelectorAll(
        '[role="article"],[aria-label^="Comment by "]');
      for (const discussion of discussions) {
        let current = discussion;
        for (let index = 0; index < depth; index++) current = wrap(current);
      }
    }
  }
}
"""
