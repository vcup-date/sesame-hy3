"""sesame agent — compact.py — summarize old history to free context.

Ported from the old sesame, with one deliberate change: the old loop
auto-compacted inside the agent loop; here compaction only runs when the user
types /compact (the REPL prints a hint when context passes 60%). The shell
stays loopless — this is a REPL-layer, user-invoked transform.
"""


def _text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "thinking":
            parts.append("(thinking)")
        elif t == "tool_use":
            parts.append(f"[tool {b.get('name')} {b.get('input')}]")
        elif t == "tool_result":
            parts.append(f"[result {str(b.get('content'))[:400]}]")
    return " ".join(parts)


def compact(complete_fn, messages, keep=8):
    """complete_fn(system, messages) -> text. Returns (new_messages, did)."""
    if len(messages) <= keep + 2:
        return messages, False

    head = messages[:-keep]
    tail = messages[-keep:]
    transcript = "\n".join(f"{m['role']}: {_text_of(m['content'])}" for m in head)

    prompt = ("Summarize this earlier conversation so work can continue. Capture the user's goals, "
              "decisions made, files changed, key facts discovered, and anything still pending. "
              "Be dense and specific.\n\n" + transcript[:120000])
    try:
        summary = complete_fn("You compress conversations without losing actionable detail.",
                              [{"role": "user", "content": prompt}])
    except Exception:
        return messages, False
    if not summary:
        return messages, False

    while tail and (isinstance(tail[0]["content"], list)
                    and tail[0]["content"] and tail[0]["content"][0].get("type") == "tool_result"):
        tail = tail[1:]

    new = [{"role": "user", "content": f"[summary of earlier conversation]\n{summary}"},
           {"role": "assistant", "content": [{"type": "text", "text": "Understood, continuing."}]}]
    return new + tail, True
