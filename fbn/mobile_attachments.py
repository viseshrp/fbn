"""Fixed, content-free attachment control categories shared with diagnostics."""

ATTACHMENT_CONTROL_SCRIPT = r"""
  const attachmentControlKind = el => {
    if (!el.matches('button,[role="button"]')) return null;
    const cleanLabel = value => (value || '').normalize('NFKC')
      .replace(/[\u034f\u200b\u200e\u200f\ufeff\p{Co}]/gu, '')
      .replace(/\s+/g, ' ').trim();
    const label = cleanLabel(el.getAttribute('aria-label'));
    const name = label || cleanLabel(el.innerText);
    if (/^(?:(?:open|view|see) (?:the )?(?:photo|image)|photo|image)(?:$|:)/i
      .test(label)) return 'photo';
    if (/^.+(?:['\u2019]s picture| profile picture)$/i.test(label)) {
      return 'credit_picture';
    }
    if (/^\+[1-9][0-9]{0,5}\s+more$/i.test(label)) return 'gallery_more';
    if (/^(?:play|open|view) (?:the )?video$/i.test(name)) return 'video';
    const response = name.replace(/(?:,\s*|\s+)(?:not )?selected$/i, '');
    if (/^interested$/i.test(response)) return 'event_interest';
    if (/^(?:going|not going)$/i.test(response)) return 'event_response';
    if (/^(?:view|see|open) event$/i.test(name)) return 'event_open';
    return 'unknown';
  };
"""
