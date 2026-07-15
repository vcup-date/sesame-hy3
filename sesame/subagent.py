"""sesame agent — subagent.py — a context firewall.

"Find every caller of X across 400 files" would otherwise dump every grep hit
and every file read into the one transcript, burning the context window on
material the model needs only once. A sub-agent runs the search in its own
fresh transcript with read-only tools and returns just the answer.

This costs almost nothing to build precisely because shell.run() is a pure
function over (transcript, tools, budget, safety): a sub-agent is a tool whose
execute() calls run() again.
"""

from shell import run

SYSTEM = ("You are a sesame sub-agent: a focused researcher with read-only tools. "
          "Investigate the task and return ONLY the findings — file paths with line "
          "numbers, exact snippets, and a direct answer. No preamble, no offers to "
          "help further. You cannot modify anything; do not try.")


def make_subagent_tool(*, tools, api, budget, on_event=None):
    read_only = [t for t in tools if t.get("read_only") and t["name"] not in ("remember", "forget")]

    def execute(inp):
        sub_budget = {
            "tool_calls": min(int(inp.get("max_tool_calls") or 15), budget["tool_calls"]),
            "thinking_tokens": budget["thinking_tokens"],
            "grace": 1,
        }
        transcript = [{"role": "user", "content": inp["task"]}]
        emit = on_event or (lambda ev: None)
        emit({"type": "subagent_start", "task": inp["task"]})
        res = run(transcript=transcript, system=SYSTEM, tools=read_only, budget=sub_budget,
                  safety=lambda call: {"allow": True},  # read-only tools only, by construction
                  on_event=lambda ev: emit({**ev, "sub": True}),
                  journal=lambda m: None, api=api)
        msg = res.get("message")
        text = "".join(b.get("text", "") for b in (msg or {}).get("content", [])
                       if b["type"] == "text").strip()
        emit({"type": "subagent_done", "calls": res["spent"]["tool_calls"]})
        if not text:
            return {"ok": False, "content": "sub-agent returned nothing (budget exhausted?)"}
        return {"ok": True, "content": f"{text}\n\n[sub-agent used {res['spent']['tool_calls']} tool calls]"}

    return {
        "name": "task",
        "read_only": True,  # it can only reach read-only tools itself
        "description": (
            "Delegate a research question to a sub-agent with its own context and read-only "
            "tools (search, read, list, browse, websearch). Use this for open-ended exploration "
            "— \"find every caller of X\", \"how does auth work here\" — so the raw file dumps "
            "stay out of this conversation. Returns only the findings."),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "self-contained question; the sub-agent sees no history"},
                "max_tool_calls": {"type": "integer", "description": "default 15"},
            },
            "required": ["task"],
        },
        "execute": execute,
    }
