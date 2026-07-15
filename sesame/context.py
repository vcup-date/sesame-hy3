"""sesame agent — context.py — keep the transcript inside the window.

The failure this prevents: with no context management, a long session ends in a
hard 400 ("prompt is too long") and *stays* wedged — every later message is
appended to an already-oversized transcript, so the session is unrecoverable and
the only signal is an error. That is not an acceptable daily-driver failure.

Two tiers, cheapest first:

  1. ELIDE (free, no API call, structure-preserving). Old tool_result bodies are
     where the tokens actually are — a single 40k-char result is ~10k tokens.
     Replace old ones with a short digest, keeping the tool_use blocks and the
     whole signed thinking trajectory intact. This is what makes it safe for a
     shell whose entire thesis is that the reasoning chain survives.
  2. SUMMARIZE (one API call, lossy). Only when elision is not enough.

The shell calls this through its `transform_context` seam. The loop still makes
no decisions: it asks "does this fit?" — a resource fact, not a policy.
"""

DIGEST_CHARS = 240
KEEP_RECENT = 6  # recent messages are never touched: the model is still using them
MARK = "…[elided"  # the digest suffix doubles as the "already shrunk" sentinel:
#                    a bookkeeping key would have to be stripped before every
#                    send (the wire rejects unknown keys), which would erase it.


def estimate_tokens(transcript, system=""):
    """~4 chars/token. Cheap, dependency-free, and good enough to steer on."""
    n = len(system)
    for m in transcript:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
            continue
        for b in c or []:
            t = b.get("type")
            if t == "text":
                n += len(b.get("text", ""))
            elif t == "thinking":
                n += len(b.get("thinking", ""))
            elif t == "tool_use":
                n += len(str(b.get("input", "")))
            elif t == "tool_result":
                n += len(str(b.get("content", "")))
    return n // 4


def _elide(transcript, keep_recent=KEEP_RECENT):
    """Digest old tool results in place. Returns how many were shrunk."""
    cut = len(transcript) - keep_recent
    shrunk = 0
    for m in transcript[:max(0, cut)]:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if b.get("type") != "tool_result":
                continue
            body = str(b.get("content", ""))
            if len(body) <= DIGEST_CHARS or MARK in body:
                continue
            head = body[:DIGEST_CHARS].rstrip()
            b["content"] = f"{head}\n{MARK} {len(body) - len(head)} chars to free context]"
            shrunk += 1
    return shrunk


def make_manager(*, window, system_fn, summarize_fn, on_event=None,
                 elide_at=0.70, summarize_at=0.88, keep_recent=KEEP_RECENT):
    """Returns a transform_context(transcript, spent) callable for shell.run()."""
    emit = on_event or (lambda ev: None)

    def transform(transcript, spent):
        used = estimate_tokens(transcript, system_fn())
        if used < window * elide_at:
            return
        shrunk = _elide(transcript, keep_recent)
        if shrunk:
            after = estimate_tokens(transcript, system_fn())
            emit({"type": "context", "action": "elide", "results": shrunk,
                  "before": used, "after": after, "window": window})
            used = after
        if used < window * summarize_at:
            return
        new, did = summarize_fn(transcript)
        if did:
            transcript[:] = new
            emit({"type": "context", "action": "summarize",
                  "before": used, "after": estimate_tokens(transcript, system_fn()),
                  "window": window})
        else:
            emit({"type": "context", "action": "failed", "before": used, "window": window})

    return transform
