"""sesame agent — team.py — named specialists that review the lead's work.

A team member is not the `task` sub-agent (subagent.py), which is a one-shot
context firewall. A member is durable: it has a NAME, a ROLE, its own private
MEMORY that survives across runs, and a standing OBJECTIVE it checks the lead
agent against after every turn.

Two ways to engage a member, exactly as the interface exposes them:

  task       — a single-turn job, run now, with full tools. The member acts
               (reads, edits, runs commands) and reports back.

  objective  — a standing watch. After each of the lead's turns, every watcher
               inspects the actual result against its objective with read-only
               tools, records notes in its private memory, and raises `flag`s.
               The lead then addresses the flags. Watchers run in parallel and
               never write, so several can review the same turn without racing;
               the fixing happens single-threaded through the lead.

This is the "smarter than one pass" part: instead of one agent judging its own
work, a panel of specialists — each with a memory of what it has seen before —
reviews it from a fixed vantage point and feeds corrections back automatically.

Like goals.py, the state here is plain and persisted with the session, so a
resumed session keeps its team. The runner is a thin wrapper over shell.run,
the same trick subagent.py uses: a member is just run() over a fresh transcript.
"""

import random
import uuid

from shell import run as shell_run

# A member run is bounded like a sub-agent: it investigates and reports, it does
# not go build the whole feature. The lead owns the work; members keep it honest.
MEMBER_TOOL_CALLS = 18
MEMBER_MEMORY_MAX = 40          # a member's private notes are capped so they cannot grow without bound
MAX_REVIEW_ROUNDS = 3           # after a turn, the board drives at most this many follow-ups, then yields

# Plain human first names, deliberately varied. A member gets one at hire time so
# you can talk about "what did John find" instead of "sub-agent 3".
NAMES = [
    "Ada", "Alex", "Amir", "Aria", "Ben", "Bianca", "Cai", "Chloe", "Cleo", "Dana",
    "Diego", "Dmitri", "Elena", "Emre", "Esme", "Farah", "Finn", "Gina", "Hugo",
    "Ines", "Ingrid", "Ivan", "Jade", "John", "Kai", "Kira", "Lena", "Leo", "Luca",
    "Maya", "Milo", "Mira", "Nadia", "Noah", "Nora", "Omar", "Otto", "Priya", "Quinn",
    "Rana", "Ravi", "Remy", "Rosa", "Ruben", "Sana", "Sean", "Sofia", "Suki", "Tariq",
    "Tessa", "Theo", "Uma", "Vera", "Victor", "Wade", "Wren", "Yara", "Yusuf", "Zane", "Zoe",
]


def pick_name(taken, rng=random):
    """A human name not already on the team; if every name is used, number one."""
    used = {t.lower() for t in taken}
    free = [n for n in NAMES if n.lower() not in used]
    if free:
        return rng.choice(free)
    base = rng.choice(NAMES)
    n = 2
    while f"{base} {n}".lower() in used:
        n += 1
    return f"{base} {n}"


class Member:
    def __init__(self, name, role, objective="", watching=False):
        self.id = uuid.uuid4().hex
        self.name = name.strip()
        self.role = role.strip()
        self.objective = (objective or "").strip()
        self.watching = bool(watching and self.objective)
        self.memory = []            # private notes, only this member sees them
        self.runs = 0               # how many times it has been engaged
        self.interventions = 0      # how many of those raised a flag
        self.last = ""              # a one-line summary of its most recent run, for the roster

    def add_note(self, text):
        text = (text or "").strip()
        if not text:
            return
        self.memory.append(text)
        del self.memory[:-MEMBER_MEMORY_MAX]     # keep the most recent notes

    def memory_block(self):
        if not self.memory:
            return "(your memory is empty — this is your first run, or you have noted nothing yet)"
        return "\n".join(f"- {n}" for n in self.memory[-MEMBER_MEMORY_MAX:])

    def watch(self, objective):
        self.objective = (objective or "").strip()
        self.watching = bool(self.objective)

    def card(self):
        return {"id": self.id, "name": self.name, "role": self.role,
                "objective": self.objective, "watching": self.watching,
                "runs": self.runs, "interventions": self.interventions,
                "memory": list(self.memory), "last": self.last}

    def to_dict(self):
        return self.card()

    @classmethod
    def from_dict(cls, d):
        m = cls(d.get("name", "?"), d.get("role", ""), d.get("objective", ""),
                d.get("watching", False))
        m.id = d.get("id", m.id)
        m.memory = list(d.get("memory") or [])
        m.runs = d.get("runs", 0)
        m.interventions = d.get("interventions", 0)
        m.last = d.get("last", "")
        return m


class Team:
    def __init__(self):
        self.members = []

    def get(self, name):
        name = (name or "").strip().lower()
        return next((m for m in self.members if m.name.lower() == name), None)

    def add(self, role, objective="", name=None, rng=random):
        name = (name or "").strip() or pick_name([m.name for m in self.members], rng)
        m = Member(name, role, objective=objective, watching=bool(objective))
        self.members.append(m)
        return m

    def fire(self, name):
        m = self.get(name)
        if m:
            self.members.remove(m)
        return m

    def watchers(self):
        return [m for m in self.members if m.watching and m.objective]

    def to_dict(self):
        return [m.to_dict() for m in self.members]

    @classmethod
    def from_dict(cls, data):
        t = cls()
        t.members = [Member.from_dict(d) for d in (data or [])]
        return t


# ── the runner: a member is shell.run over a fresh transcript ────────────────

_REVIEW_SYSTEM = """You are {name}, a member of the lead agent's team. You are not the lead — \
you are a specialist with one job.

Your role: {role}
{objective}
Your private memory (notes only you keep, carried across your reviews):
{memory}

The lead agent just finished a piece of work. Review it against YOUR role and objective only — \
stay in your lane, do not redo the lead's whole job or comment on things outside your remit.

Investigate the ACTUAL result, do not guess: read the files that changed, run the command, look \
at the real output. You have read-only tools; you cannot edit. When you find something wrong, you \
raise it and the lead fixes it.

Then finish by calling exactly one of:
  - all_clear  — if everything within your responsibility is fine.
  - flag       — for each concrete problem you found (call it once per problem). Give the issue, \
a specific fix, where it is (file:line), and a severity. Be precise; a vague flag wastes a round.

Along the way, use note() to record anything worth remembering for next time (what you checked, a \
recurring problem, a decision) so your future reviews are sharper. Keep notes short.

Do not write prose back to the user; your flags and notes are your whole output."""

_TASK_SYSTEM = """You are {name}, a member of the lead agent's team. You are not the lead — \
you are a specialist with one job.

Your role: {role}
{objective}
Your private memory (notes only you keep, carried across your runs):
{memory}

The lead has handed you a single task. Do it now with the tools you have — read, search, run \
commands, and edit files as needed. Act; do not just describe what you would do.

Use note() to record anything worth remembering for next time. If you notice a problem outside \
this task, raise it with flag() so the lead knows. When done, reply with a short, concrete report \
of what you did and what you found."""


def _member_tools(member, state, *, review):
    """The control tools every member has, on top of its work tools. They close
    over `state` so the runner reads the verdict back after run() returns."""

    def _note(inp):
        member.add_note(inp.get("text", ""))
        state["notes"] += 1
        return {"ok": True, "content": "noted (saved to your private memory)"}

    def _flag(inp):
        issue = (inp.get("issue") or "").strip()
        if not issue:
            return {"ok": False, "content": "an issue is required"}
        state["flags"].append({
            "issue": issue,
            "fix": (inp.get("fix") or "").strip(),
            "where": (inp.get("where") or "").strip(),
            "severity": inp.get("severity") or "normal",
        })
        return {"ok": True, "content": "flagged for the lead"}

    def _clear(inp):
        state["cleared"] = True
        note = (inp.get("note") or "").strip()
        return {"ok": True, "content": f"marked all-clear{': ' + note if note else ''}"}

    tools = [
        {"name": "note", "read_only": True,   # writes only to the member's own memory, never files
         "description": "Save a short line to your private memory, carried into your future runs.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"]},
         "execute": _note},
        {"name": "flag", "read_only": True,
         "description": ("Raise one concrete problem for the lead to fix. Call once per problem. "
                         "Give a specific fix and where it is; severity is minor|normal|blocker."),
         "input_schema": {"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "fix": {"type": "string"},
                                         "where": {"type": "string", "description": "file:line or component"},
                                         "severity": {"type": "string",
                                                      "enum": ["minor", "normal", "blocker"]}},
                          "required": ["issue"]},
         "execute": _flag},
    ]
    if review:
        tools.append(
            {"name": "all_clear", "read_only": True,
             "description": "Declare that everything within your responsibility is fine this round.",
             "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}},
             "execute": _clear})
    return tools


def run_member(member, *, kind, brief, tools, api, budget, safety, on_event=None,
               transform_context=None, should_stop=None):
    """Engage one member. kind is "review" (read-only, judges the lead's work) or
    "task" (full tools, does a job). Returns the verdict:
        {name, role, kind, flags, cleared, summary, notes_added, spent}

    should_stop() is polled on every shell event; when it turns true the member's
    run is abandoned at once (KeyboardInterrupt, which shell.run unwinds cleanly).
    Without this, a member on a slow model kept working after you pressed Stop.
    """
    review = kind == "review"
    state = {"flags": [], "cleared": False, "notes": 0}
    _emit = on_event or (lambda ev: None)

    def emit(ev):
        if should_stop and should_stop():
            raise KeyboardInterrupt
        _emit(ev)

    objective = f"Your standing objective: {member.objective}\n" if member.objective else ""
    template = _REVIEW_SYSTEM if review else _TASK_SYSTEM
    system = template.format(name=member.name, role=member.role,
                             objective=objective, memory=member.memory_block())

    member_tools = list(tools) + _member_tools(member, state, review=review)
    member_budget = {
        "tool_calls": min(MEMBER_TOOL_CALLS, budget.get("tool_calls", MEMBER_TOOL_CALLS)),
        "thinking_tokens": budget.get("thinking_tokens", 4000),
        "effort": budget.get("effort", "medium"),
        "grace": 1,
    }
    transcript = [{"role": "user", "content": brief}]
    res = shell_run(transcript=transcript, system=system, tools=member_tools,
                    budget=member_budget, safety=safety,
                    on_event=lambda ev: emit({**ev, "member": member.name}),
                    journal=lambda m: None, api=api, transform_context=transform_context)

    msg = res.get("message") or {}
    summary = "".join(b.get("text", "") for b in msg.get("content", [])
                      if b.get("type") == "text").strip()

    member.runs += 1
    if state["flags"]:
        member.interventions += 1
    member.last = (summary[:120] if summary
                   else (f"{len(state['flags'])} flag(s)" if state["flags"]
                         else "all clear" if state["cleared"] else "no report"))
    return {"name": member.name, "role": member.role, "kind": kind,
            "flags": state["flags"], "cleared": state["cleared"], "summary": summary,
            "notes_added": state["notes"], "spent": res.get("spent", {})}


def compose_review(interventions):
    """Turn the board's flags into one continuation prompt for the lead."""
    order = {"blocker": 0, "normal": 1, "minor": 2}
    lines = ["Your team reviewed your work. Address the points below, then stop. "
             "If a flag is wrong, say why instead of changing anything.\n"]
    for v in interventions:
        lines.append(f"[{v['name']} · {v['role']}]")
        for f in sorted(v["flags"], key=lambda f: order.get(f.get("severity"), 1)):
            sev = f.get("severity", "normal")
            tag = f"({sev}) " if sev != "normal" else ""
            where = f"  [{f['where']}]" if f.get("where") else ""
            fix = f" → {f['fix']}" if f.get("fix") else ""
            lines.append(f"  - {tag}{f['issue']}{fix}{where}")
        lines.append("")
    return "\n".join(lines).rstrip()
