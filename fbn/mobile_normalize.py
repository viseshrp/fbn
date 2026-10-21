"""Browser-side, ownership-preserving normalization of a mobile body.

Only this adapter walks raw child nodes and computes presentation. Recognizers
consume its ordered tree, never infer ownership from sibling depth. DOM handles
stay in the browser and normalized content is never diagnostic output.
"""

NORMALIZE_BODY_SCRIPT = r"""
  const boundaryAttribute = new RegExp('^(?:role|href|tabindex|contenteditable|'
    + 'data-action-id|data-type|aria-.*|on.*)$', 'i');
  const presentationWrapper = el => ['DIV','SPAN','SECTION','P'].includes(el.tagName)
    && (!el.hasAttribute('data-mcomponent')
      || el.getAttribute('data-mcomponent') === 'MContainer')
    && !Array.from(el.attributes).some(attr => boundaryAttribute.test(attr.name))
    && !Array.from(el.childNodes).some(node => node.nodeType === Node.TEXT_NODE
      && clean(node.textContent));
  const unwrapPresentation = el => {
    for (let depth = 0; depth < 192 && presentationWrapper(el)
      && el.children.length === 1; depth++) el = el.firstElementChild;
    return el;
  };
  const presentationChildren = el => {
    let owner = el;
    for (let depth = 0; depth < 192 && owner.children.length === 1
      && presentationWrapper(owner.firstElementChild); depth++) {
      owner = owner.firstElementChild;
    }
    return Array.from(owner.children);
  };
  const normalizeBody = sections => {
    const nodes = [], roots = [], styles = new WeakMap();
    let visited = 0, collapsed = 0, overflow = false;
    const style = el => {
      if (!styles.has(el)) styles.set(el, getComputedStyle(el));
      return styles.get(el);
    };
    const build = (el, section, depth, force = false) => {
      if (++visited > 12000 || depth > 192) { overflow = true; return null; }
      if (el.nodeType === Node.TEXT_NODE) {
        return el.textContent ? {el, kind: 'literal', children: [], section,
          block: false, flags: {}, parent: null} : null;
      }
      if (!(el instanceof Element)
        || /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT)$/.test(el.tagName)) {
        return null;
      }
      const css = style(el);
      if (css.display === 'none' || css.visibility === 'hidden'
        || css.visibility === 'collapse' || css.opacity === '0') return null;
      // display:contents has no box but its descendants can be readable.
      if (css.display !== 'contents' && !visible(el)) return null;
      const component = el.getAttribute('data-mcomponent');
      const flags = {
        text: el.matches(textSelector), discussion: el.matches(excludedSelector),
        menu: el.matches(menuSelector),
        button: el.matches('button,[role="button"]'),
        control: el.matches('input,select,textarea,[contenteditable="true"],'
          + '[role="textbox"],[role="radio"],[role="checkbox"],[role="switch"]'),
        link: el.matches('a[href],[role="link"]'),
        image: el.matches('img,[data-mcomponent="ImageArea"],'
          + '[data-mcomponent="ServerImageArea"]'),
        media: el.matches('img,video,[data-mcomponent="ImageArea"],'
          + '[data-mcomponent="ServerImageArea"],[data-mcomponent="VideoArea"],'
          + '[data-mcomponent="ServerVideoArea"]'),
        action: el.hasAttribute('data-action-id') || Array.from(el.attributes)
          .some(attr => /^on/i.test(attr.name)) || el.hasAttribute('tabindex'),
        label: el.hasAttribute('aria-label'),
        preview: el.matches('span,button,[role="button"]')
          && /^(?:(?:\.{3}|…)\s*)?see more$/i.test(clean(el.innerText)),
        poll: el.matches('[role="radiogroup"],[role="group"][aria-label="Poll"]'),
      };
      const block = !['inline','inline-block','inline-flex','contents']
        .includes(css.display);
      const children = [];
      for (const child of flags.discussion ? [] : el.childNodes) {
        const normalized = build(child, section, depth + 1);
        if (normalized) children.push(normalized);
        if (overflow) break;
      }
      const meaningful = children.filter(node => node.kind !== 'literal'
        || clean(node.el.textContent));
      const boundary = force || Object.values(flags).some(Boolean)
        || el.hasAttribute('role') || el.hasAttribute('data-type')
        || Array.from(el.attributes).some(attr => /^aria-/i.test(attr.name))
        || (component && !['MContainer'].includes(component))
        || !['DIV','SPAN','SECTION','P'].includes(el.tagName);
      if (!boundary && !meaningful.length) { collapsed++; return null; }
      // Collapse only unary presentation wrappers. Branching containers retain
      // grouping/ownership. A block around inline text retains its line break.
      if (!boundary && meaningful.length === 1
        && (!block || meaningful[0].block)) {
        collapsed++;
        return meaningful[0];
      }
      const kind = flags.discussion ? 'discussion' : flags.text ? 'text'
        : flags.button || flags.control ? 'control' : flags.link ? 'link'
        : flags.media ? 'media' : flags.action ? 'action' : 'group';
      const node = {el, kind, flags, block, component, section, children,
        transparent: !boundary,
        parent: null};
      for (const child of children) child.parent = node;
      return node;
    };
    for (const section of sections) {
      const root = build(section, section, 0, true);
      if (root) {
        // A sole presentation group covering the entire owned section adds no
        // ownership boundary; keep the section root and adopt that group's children.
        while (root.children.length === 1 && root.children[0].transparent
          && root.children[0].kind === 'group' && root.children[0].block) {
          root.children = root.children[0].children;
          for (const child of root.children) child.parent = root;
          collapsed++;
        }
        roots.push(root);
      }
      if (overflow) break;
    }
    const walk = node => {
      node.order = nodes.length; nodes.push(node);
      for (const child of node.children) walk(child);
      node.end = nodes.length;
    };
    for (const root of roots) walk(root);
    const contains = (parent, child) => parent.order <= child.order
      && child.order < parent.end;
    const ancestors = (node, predicate) => {
      for (let owner = node; owner; owner = owner.parent) {
        if (predicate(owner)) return owner;
      }
      return null;
    };
    const descendants = node => nodes.slice(node.order + 1, node.end);
    const text = node => node.kind === 'literal' ? clean(node.el.textContent)
      : clean(node.el.innerText);
    const read = (node, accept) => {
      if (!accept(node)) return '';
      if (node.kind === 'literal') return node.el.textContent;
      if (node.el.tagName === 'BR') return ' ';
      const value = node.children.map(child => read(child, accept)).join('');
      return node.block ? ' ' + value + ' ' : value;
    };
    return {nodes, roots, contains, ancestors, descendants, text, read,
      overflow, visited, collapsed};
  };
"""
