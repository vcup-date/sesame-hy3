"""sesame agent — goals.py — two drivers that keep the agent working on their own.

Neither belongs in shell.py: the shell runs exactly one turn. These sit at the
REPL layer and decide whether to run another, the same place steering and
compaction live.

  Goal  (like Codex /goal): a durable objective. After each turn the agent is
        asked to keep going toward it, until it calls goal_done, a token budget
        runs out, or you pause or clear it. You control pause/resume; the model
        only creates and completes — it cannot keep itself alive.

  Loop  (like Claude Code /loop): one prompt re-run on an interval, e.g. every
        10 minutes, until you stop it.

Both are plain state, persisted with the session, so /resume picks a goal back up
mid-pursuit. The pure logic here has no I/O, which is what makes it testable.
"""

import time
import uuid

MAX_GOAL_TURNS = 60          # a hard ceiling: a goal can never run forever
DEFAULT_LOOP_SECONDS = 600   # /loop with no interval means every 10 minutes
LOOP_EXPIRY_SECONDS = 7 * 24 * 3600   # a loop self-deletes 7 days after it is created,
                                      # so a forgotten one cannot run forever


def parse_interval(token):
    """'5m' -> 300, '30s' -> 30, '2h' -> 7200, '10' -> 600 (bare number = minutes).
    Returns seconds, or None if the token is not an interval."""
    if not token:
        return None
    t = token.strip().lower()
    unit = 60
    if t[-1:] in ("s", "m", "h"):
        unit = {"s": 1, "m": 60, "h": 3600}[t[-1]]
        t = t[:-1]
    try:
        n = float(t)
    except ValueError:
        return None
    if n <= 0:
        return None
    return int(n * unit)


class Goal:
    def __init__(self, objective, budget=None, base_out=0):
        self.objective = objective.strip()
        self.status = "active"       # active | paused | budget_limited | complete | blocked
        self.budget = budget         # output-token ceiling, or None for no ceiling
        self.base_out = base_out     # output tokens spent when the goal began
        self.turns = 0               # continuation turns taken so far
        self.id = uuid.uuid4().hex
        self.summary = ""

    def used(self, output_tokens):
        return max(0, output_tokens - self.base_out)

    def next_prompt(self, output_tokens):
        """After a turn: the continuation to run, or None if the goal stops now.
        Called by the interface, which runs the returned prompt as the next turn."""
        if self.status != "active":
            return None
        if self.budget and self.used(output_tokens) >= self.budget:
            self.status = "budget_limited"
            return None
        if self.turns >= MAX_GOAL_TURNS:
            self.status = "budget_limited"
            return None
        self.turns += 1
        return (
            "Continue working toward this goal:\n\n"
            f"{self.objective}\n\n"
            "Keep going until it is fully done — do the next concrete step now, do "
            "not just describe it. When the goal is completely achieved, call "
            "goal_done with a one-line summary. If you are genuinely blocked and "
            "cannot make progress, call goal_done and explain what stopped you."
        )

    def to_dict(self):
        return {"objective": self.objective, "status": self.status, "budget": self.budget,
                "base_out": self.base_out, "turns": self.turns, "id": self.id,
                "summary": self.summary}

    @classmethod
    def from_dict(cls, d):
        g = cls(d.get("objective", ""), d.get("budget"), d.get("base_out", 0))
        g.status = d.get("status", "active")
        g.turns = d.get("turns", 0)
        g.id = d.get("id", g.id)
        g.summary = d.get("summary", "")
        return g


class LoopJob:
    def __init__(self, interval, prompt, clock=None, created_at=None):
        self.interval = interval
        self.prompt = prompt.strip()
        self.id = uuid.uuid4().hex
        self.count = 0
        # created_at is wall-clock, so the 7-day expiry is right even across a
        # resume in a new process; next_at is monotonic, for the interval itself.
        self.created_at = created_at if created_at is not None else time.time()
        self.next_at = (clock or time.monotonic)()   # due immediately, then every interval

    def due(self, now):
        return now >= self.next_at

    def fired(self, now):
        self.count += 1
        self.next_at = now + self.interval

    def expired(self, wall_now=None):
        now = wall_now if wall_now is not None else time.time()
        return now >= self.created_at + LOOP_EXPIRY_SECONDS

    def expires_in(self, wall_now=None):
        now = wall_now if wall_now is not None else time.time()
        return max(0, int(self.created_at + LOOP_EXPIRY_SECONDS - now))

    def to_dict(self):
        return {"interval": self.interval, "prompt": self.prompt, "count": self.count,
                "id": self.id, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d):
        j = cls(d.get("interval", DEFAULT_LOOP_SECONDS), d.get("prompt", ""),
                created_at=d.get("created_at"))
        j.count = d.get("count", 0)
        j.id = d.get("id", j.id)
        return j
