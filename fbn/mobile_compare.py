"""Optional, same-DOM parser comparison with content-free results.

The reference is trusted source supplied explicitly by an offline test or a
bounded rollout harness. It is never downloaded, inferred from a page, used as
a fallback or permitted to publish anything. Normal acquisition has no reference.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_REFERENCE: ContextVar[str | None] = ContextVar(
    "fbn_mobile_parser_reference", default=None
)


@contextmanager
def parser_reference(script: str) -> Iterator[None]:
    """Enable fail-closed comparison for this execution context only."""

    if not script.strip() or len(script) > 1_000_000:
        raise ValueError("Invalid trusted parser reference.")
    token = _REFERENCE.set(script)
    try:
        yield
    finally:
        _REFERENCE.reset(token)


def comparison_script(candidate: str, *, permalink: bool) -> str:
    """Compare both parses synchronously; no navigation or asynchronous UI gap."""

    reference = _REFERENCE.get()
    if reference is None:
        return candidate
    if permalink:
        reference = reference.replace(
            "const permalink = false;", "const permalink = true;"
        )
    return r"""includeContent => {
          const start = performance.now();
          const before = (__REFERENCE__)(true);
          const middle = performance.now();
          const after = (__CANDIDATE__)(true);
          const finish = performance.now();
          const fields = ['author','text','timestampLabel','partial','mediaKeys',
            'mediaCount','position','bodyState','previewControlCount'];
          const differences = new Set();
          if (before.records.length !== after.records.length) {
            differences.add('post_count');
          }
          if (JSON.stringify([...before.missing].sort())
            !== JSON.stringify([...after.missing].sort())) {
            differences.add('missing_signals');
          }
          const count = Math.min(before.records.length, after.records.length);
          for (let index = 0; index < count; index++) {
            const old = before.records[index], current = after.records[index];
            if (old.menu !== current.menu) differences.add('menu_ownership');
            for (const field of fields) {
              if (JSON.stringify(old.payload[field])
                !== JSON.stringify(current.payload[field])) {
                differences.add(field);
              }
            }
          }
          const comparison = {matched: differences.size === 0,
            referencePosts: before.records.length, candidatePosts: after.records.length,
            differingFields: Array.from(differences).sort(),
            referenceMs: Math.round((middle-start)*100)/100,
            candidateMs: Math.round((finish-middle)*100)/100};
          return {...after, comparison};
        }""".replace("__REFERENCE__", reference).replace("__CANDIDATE__", candidate)
