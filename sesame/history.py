"""sesame agent — history.py — transcript validation and repair.

Ported from the old sesame. Guarantees the Anthropic-wire invariants before a
send: every tool_use is answered by a tool_result, roles alternate sensibly,
and the transcript starts with a real user message. This is REPL-layer
transcript hygiene, not loop policy — the shell never calls it.

It also closes the truncation gap: if a run ends with dangling tool_use blocks
(e.g. stop_reason "length"), repair fills in "[no result recorded]" results so
the next send is valid.
"""

_MISSING = "[no result recorded]"


def _results(pending, provided=None):
    provided = provided or {}
    return [provided.get(tid) or {"type": "tool_result", "tool_use_id": tid, "content": _MISSING}
            for tid in pending]


def validate_and_repair(messages):
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) or (isinstance(c, list) and not (c and isinstance(c[0], dict)
                                                                   and c[0].get("type") == "tool_result")):
                break
        i += 1
    msgs = messages[i:]

    out = []
    pending = []
    for m in msgs:
        role, content = m.get("role"), m.get("content")
        if role == "assistant":
            if pending:
                out.append({"role": "user", "content": _results(pending)})
                pending = []
            blocks = content if isinstance(content, list) else (
                [{"type": "text", "text": content}] if content else [])
            if not blocks:
                continue
            out.append({"role": "assistant", "content": blocks})
            pending = [b.get("id") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
        elif role == "user":
            if isinstance(content, list):
                provided = {b.get("tool_use_id"): b for b in content
                            if isinstance(b, dict) and b.get("type") == "tool_result"}
                other = [b for b in content if isinstance(b, dict) and b.get("type") != "tool_result"]
                if pending:
                    out.append({"role": "user", "content": _results(pending, provided) + other})
                    pending = []
                elif other:
                    out.append({"role": "user", "content": other})
            else:
                if pending:
                    out.append({"role": "user", "content": _results(pending)})
                    pending = []
                out.append({"role": "user", "content": content})

    if pending:
        out.append({"role": "user", "content": _results(pending)})

    # Merge consecutive user messages. Two user turns in a row can happen when a
    # round ends with every tool denied; some endpoints tolerate it, the wire
    # spec does not.
    merged = []
    for m in out:
        if merged and m["role"] == "user" and merged[-1]["role"] == "user":
            prev, cur = merged[-1]["content"], m["content"]
            if isinstance(prev, str) and isinstance(cur, str):
                merged[-1] = {"role": "user", "content": prev + "\n\n" + cur}
                continue
            as_list = (lambda c: c if isinstance(c, list) else [{"type": "text", "text": c}])
            merged[-1] = {"role": "user", "content": as_list(prev) + as_list(cur)}
            continue
        merged.append(m)
    return merged


def is_valid(messages):
    return validate_and_repair(messages) == messages
