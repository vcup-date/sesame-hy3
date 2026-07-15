"""End-to-end smoke test: exercises thinking round-trip, internal search,
browser tool, budget accounting — no terminal interaction.
Run from the repo root: python3 test/smoke.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import models                  # noqa: E402
import project                 # noqa: E402
from shell import run          # noqa: E402
from subagent import make_subagent_tool  # noqa: E402
from tools import TOOLS        # noqa: E402

_install = json.loads((ROOT / "sesame.config.json").read_text())
CFG = project.load_config(_install)  # env → ./.sesame → ~/.sesame → defaults
if not CFG.get("apiKey"):
    sys.exit("no API key: set SESAME_API_KEY or put it in .sesame/config.json")
SPEC = models.spec(CFG["model"])
API = {"base_url": CFG["baseUrl"], "api_key": CFG["apiKey"], "model": CFG["model"],
       "max_tokens": CFG["maxTokens"], "thinking": SPEC["thinking"], "cache": False}

BUDGET = {"tool_calls": 12, "thinking_tokens": 2048, "effort": "high", "grace": 2}
ALL = TOOLS + [make_subagent_tool(tools=TOOLS, api=API, budget=BUDGET)]

transcript = [{
    "role": "user",
    "content": ('Three jobs: (1) use the search tool to find which file mentions "Compiled AI"; '
                '(2) use the browse tool to fetch https://example.com and report its <title>; '
                '(3) use the task tool to ask a sub-agent: "which python file defines the '
                'retry logic, and what statuses does it retry?" Then answer all three briefly.'),
}]


def on_event(ev):
    pad = "    " if ev.get("sub") else ""
    if ev["type"] == "tool_use":
        print(f"{pad}  ⏺ {ev['name']}({json.dumps(ev['input'])[:90]})")
    elif ev["type"] == "tool_result":
        state = "ok" if ev["ok"] else "ERROR"
        print(f"{pad}    ⎿ {state}: {ev['content'].splitlines()[0][:90]}")
    elif ev["type"] == "retry":
        print(f"  ⟳ retry {ev['attempt']}/{ev['of']} in {ev['wait']}s")


result = run(
    transcript=transcript,
    system="You are sesame agent (smoke test). Be brief.",
    tools=ALL,
    budget=BUDGET,
    safety=lambda call: {"allow": True},
    on_event=on_event,
    journal=lambda msg: None,
    api=API,
)

final_text = "".join(b["text"] for b in result["message"]["content"] if b["type"] == "text")
thinking = [b for m in transcript if m["role"] == "assistant"
            for b in m["content"] if b["type"] == "thinking"]
used = {b["name"] for m in transcript if m["role"] == "assistant"
        for b in m["content"] if b["type"] == "tool_use"}

print("\n--- final answer ---\n" + final_text)
print("\n--- checks ---")
low = final_text.lower()
checks = [
    ("used search tool", "search" in used),
    ("used browse tool", "browse" in used),
    ("delegated to a sub-agent (task tool)", "task" in used),
    ("thinking blocks preserved in transcript", len(thinking) >= 2),
    ("thinking blocks signed", all(b.get("signature") for b in thinking)),
    ("found the report file", "report_full" in low),
    ("got example.com title", "example domain" in low),
    ("sub-agent found the retry logic", "shell" in low and ("429" in low or "retry" in low)),
    ("budget accounted", 3 <= result["spent"]["tool_calls"] <= 12),
]
failed = 0
for name, ok in checks:
    print(f" {'✓' if ok else '✗'} {name}")
    failed += 0 if ok else 1

spent = result["spent"]
print(f"\nspent: {json.dumps(spent)}")
print(f"cost:  ${models.cost(API['model'], spent):.6f}  "
      f"(fresh in {spent['input_tokens']}, cached {spent['cache_read_tokens']}, "
      f"out {spent['output_tokens']})")
sys.exit(1 if failed else 0)
