"""Bounds on bodies crossing the agent-facing surfaces (ADR-0034).

Rule 2 of ADR-0034: *a truncated body is never valid input to a write.* A
bounded read hands a caller the first `body_chunk_size` characters of a body
plus `body_truncated: true`; an agent that edits what it received and writes it
back would replace a 170,000-character body with its first 8,000. Silent,
permanent, and produced by the safety feature itself.

`refuse_truncated_body` is the one implementation of that rule. Every write
surface calls it on the **raw** request -- the dict as it arrived, before any
model has dropped the unknown key and before the service layer, which takes an
`Entry` and cannot carry the marker.
"""

from __future__ import annotations

from typing import Any

#: The keys a bounded read attaches to a truncated body (ADR-0034 rule 2).
#: Only `body_truncated` triggers the refusal; the others travel with it and
#: are listed here so callers can strip the whole set if they ever need to.
TRUNCATION_MARKER = "body_truncated"
TRUNCATION_KEYS = (TRUNCATION_MARKER, "body_length", "body_offset", "body_chunk_size")

REFUSAL_CODE = "VALIDATION_FAILED"

REFUSAL_MESSAGE = (
    "Refusing to write a body marked body_truncated: the body you are writing "
    "is a partial read, and writing it would replace the stored entry with that "
    "fragment (ADR-0034). Re-read the whole body first -- kb_read_body with "
    "body_offset paging until has_more is false, or a body_limit above "
    "body_length -- then write that, without the body_truncated key."
)

REFUSAL_SUGGESTION = (
    "Call kb_read_body (or pass a large enough body_limit) to assemble the full "
    "body, then retry the write with the complete text and no body_truncated key. "
    "To change other fields without touching the body, omit body from the request."
)


def _is_truthy_marker(value: Any) -> bool:
    """Is this value a `body_truncated` that means "yes, truncated"?

    `True` and the strings a JSON-ish client might send in its place count.
    `False`, `None`, `0` and the string `"false"` do not: a read that did not
    truncate carries `body_truncated: false`, and refusing that would break
    every caller that faithfully echoes back what it was given.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def carries_truncation_marker(payload: Any) -> bool:
    """Does `payload` carry a truthy `body_truncated`, at any depth?

    Top level is the documented shape. `metadata` (MCP) and the extension
    bag it becomes are searched too: a caller that relays a read result into
    a metadata dict is doing the same dangerous thing one level down, and
    ADR-0034's rule is about the body, not about where the key sits.
    """
    if isinstance(payload, dict):
        if TRUNCATION_MARKER in payload and _is_truthy_marker(payload[TRUNCATION_MARKER]):
            return True
        return any(carries_truncation_marker(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(carries_truncation_marker(v) for v in payload)
    return False


def has_body(payload: Any, *, body_key: str = "body") -> bool:
    """Is this write actually writing a body, at the top level or nested?

    A metadata-only update that happens to carry the marker writes nothing
    truncated, so it is allowed: the refusal protects bodies, and there is no
    body here to lose. Nested shapes count because some write tools carry
    their bodies one level down (a list of child-task specs, an import's
    parsed entries).
    """
    if isinstance(payload, dict):
        if payload.get(body_key) is not None:
            return True
        return any(has_body(v, body_key=body_key) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(has_body(v, body_key=body_key) for v in payload)
    return False


def refuse_truncated_body(payload: Any, *, body_key: str = "body") -> str | None:
    """Return the refusal message if this write must be refused, else `None`.

    A write is refused when it carries **both** a body and a truthy
    `body_truncated` marker. Callers turn the message into their surface's own
    error shape: MCP's flat `{"error", "error_code", "retryable"}` envelope, or
    REST's structured `detail` -- with `retryable` false either way, because the
    same truncated body fails identically on every retry.
    """
    if not has_body(payload, body_key=body_key):
        return None
    if not carries_truncation_marker(payload):
        return None
    return REFUSAL_MESSAGE
