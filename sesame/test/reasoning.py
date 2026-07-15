"""Does the model still know what it was thinking, after a tool call?

sesame agent sends the model's reasoning back to it with the next request. Without
that, an agent's thinking is thrown away at every tool boundary: the model sees its
own tool calls, but not the reasoning that produced them.

This measures it. The model picks a random number inside its reasoning, never says it
out loud, then calls a tool. Afterwards it is asked what the number was. It cannot
re-derive it, so either it can see its earlier thinking, or it is guessing.

    python3 test/reasoning.py 5

Measured on deepseek-v4-flash over the OpenAI wire:

    reasoning carried back   5/5 recalled
    reasoning dropped        0/5 recalled, a different number invented every time
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shell                                     # noqa: E402
from config import Config                        # noqa: E402

PROBE = ("Inside your thinking, choose a random four digit number as a secret code and write it "
         "there. Do NOT put the number in your visible answer. Then call the bash tool with "
         "`echo ok`, and afterwards reply only with the word 'done'.")

ASK = "What was the secret code? Reply with the number only."

TOOL = [{"name": "bash", "description": "run a shell command", "read_only": False,
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                          "required": ["command"]},
         "execute": lambda inp: {"ok": True, "content": "ok"}}]


def four_digits(text):
    found = re.findall(r"\b([1-9]\d{3})\b", text or "")
    return found[0] if found else None


def trial(api, carry):
    shell.NO_REASONING_ECHO.clear()
    if not carry:
        shell.NO_REASONING_ECHO.add(api["base_url"].rstrip("/"))

    kw = dict(system="You are a careful agent.", tools=TOOL,
              budget={"tool_calls": 2, "thinking_tokens": 1500, "effort": "low", "grace": 1},
              safety=lambda call: {"allow": True}, on_event=lambda ev: None,
              journal=lambda msg: None, api=api)

    t = [{"role": "user", "content": PROBE}]
    shell.run(transcript=t, **kw)
    thought = " ".join(b.get("thinking", "") for m in t
                       if m.get("role") == "assistant" and isinstance(m.get("content"), list)
                       for b in m["content"])
    picked = four_digits(thought)

    t.append({"role": "user", "content": ASK})
    shell.run(transcript=t, **kw)
    last = t[-1].get("content")
    said = " ".join(b.get("text", "") for b in last if isinstance(b, dict)) \
        if isinstance(last, list) else str(last)
    return picked, four_digits(said)


def main(rounds=5):
    cfg = Config()
    api = dict(cfg.api)
    api["thinking"] = "budget"          # the question is about reasoning: ask for it

    print(f"model {api['model']} on the {api['wire']} wire\n")

    # A model with thinking switched off has no reasoning to carry, and this would
    # print 0/5 twice and look like a result. It is not one.
    picked, _ = trial(api, True)
    if not picked:
        print("this model returned no reasoning, so there is nothing to carry across the\n"
              "tool call. Point sesame at a reasoning model (or switch thinking on: a local\n"
              "llama.cpp started with enable_thinking=false will never reason) and run again.")
        return 1

    for label, carry in (("carried back", True), ("dropped", False)):
        hits = 0
        print(f"reasoning {label}:")
        for i in range(rounds):
            picked, said = trial(api, carry)
            ok = bool(picked and said and picked == said)
            hits += ok
            print(f"  {i + 1}. thought {picked}, said {said} after the tool call"
                  f"   {'ok' if ok else 'lost'}")
        print(f"  recalled its own reasoning {hits}/{rounds}\n")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
