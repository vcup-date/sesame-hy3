"""sesame agent — shell.py — the loopless core.

The model owns the task: what to do next, which tool, when to stop. This file
owns nothing that the model could own — no planner, no steering queue, no
follow-up queue, no per-turn config, no hidden prompt.

What it does own (and this list is the honest one — it has grown since v0.1):

  I/O       — forward tool calls out, results back, over either wire. The
              request/response trampoline is mandated by the protocol; it
              carries no task decisions.
  retries   — transient failures (429/5xx/network) are re-sent with backoff.
  safety    — one gate at the tool boundary: the caller allows, or denies with
              a reason (the reason goes back to the model).
  budget    — a hard resource ceiling: thinking tokens (API-enforced) and tool
              calls (enforced here, including ending a runaway turn).
  refusals  — a call the wire delivered broken (arguments truncated by the
              output limit, or missing a field its own schema requires) is
              failed instead of executed.
  context   — one caller-supplied seam (transform_context) to shrink an
              oversized transcript. Refusing this seam was a mistake: without
              it a long session dies on an unrecoverable 400.

Steering (interrupt mid-run and inject a new message) lives in loop.py/tui.py,
outside the loop — so the loop itself still makes no decisions.

Deliberately coupled to the Anthropic Messages wire and its reasoning contract:
thinking blocks (signatures included) are preserved and sent back verbatim so
the reasoning trajectory survives tool boundaries. Flattening them through a
model-agnostic layer is the one thing this shell refuses.

KeyboardInterrupt is the abort channel: Ctrl+C during a run lands the completed
blocks in the transcript and returns {"aborted": True}.
"""

import json
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import log

MAX_TOOL_RESULT_CHARS = 40_000
RETRY_ON = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 5

# Interleaved thinking — the model reasoning BETWEEN tool calls instead of only
# before the first one — is exactly this shell's premise, and on Anthropic it is
# gated behind this beta header. Without it the reasoning trajectory does not
# actually survive the tool boundary; it only looked like it did because
# DeepSeek's compatibility shim ignores the header.
INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"

# Blocks the wire hands back that must be echoed verbatim. redacted_thinking is
# encrypted reasoning: drop it and the next request is missing part of the chain.
ECHO_BLOCKS = ("thinking", "redacted_thinking", "text", "tool_use")


def run(*, transcript, system, tools, budget, safety, on_event, journal, api,
        transform_context=None, steer=None):
    """The whole public surface. One call = one task trajectory.

    transcript — provider-native message list; run() appends to it
    system     — visible system string (caller displays it; nothing is hidden)
    tools      — [{"name", "description", "input_schema", "read_only", "execute"}]
    budget     — {"tool_calls": int, "thinking_tokens": int}
    safety     — (tool_use_block) -> {"allow": True} | {"allow": False, "reason": str}
    on_event   — render callback; the shell never touches the terminal
    journal    — (message) -> None, called with each wire message
    api        — {"base_url", "api_key", "model", "max_tokens"}
    """
    spent = {"tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    emit = on_event or (lambda ev: None)
    jot = journal or (lambda msg: None)
    stalled = 0

    while True:  # trampoline: each iteration is mandated by the wire, not by policy
        # The one seam the shell concedes: the caller may shrink an oversized
        # transcript in place before the send. Without it, a long session ends
        # in an unrecoverable 400 — the loop stays decision-free, but a context
        # that cannot fit is a resource fact, not a policy choice.
        if transform_context:
            transform_context(transcript, spent)
        try:
            msg = _retrying_stream(transcript=transcript, system=system, tools=tools,
                                   api=api, budget=budget, emit=emit)
        except KeyboardInterrupt as err:
            content = list(getattr(err, "partial", None) or [])
            if not any(b["type"] == "text" for b in content):
                content.append({"type": "text", "text": "[interrupted]"})
            assistant = {"role": "assistant", "content": content}
            transcript.append(assistant)
            jot(assistant)
            return {"aborted": True, "spent": spent, "message": None}

        spent["input_tokens"] += msg["usage"].get("input_tokens", 0)
        spent["output_tokens"] += msg["usage"].get("output_tokens", 0)
        spent["cache_read_tokens"] += msg["usage"].get("cache_read_input_tokens", 0)
        assistant = {"role": "assistant", "content": msg["content"]}
        transcript.append(assistant)
        jot(assistant)

        calls = [b for b in msg["content"] if b["type"] == "tool_use"]
        if not calls:
            emit({"type": "done", "usage": msg["usage"], "spent": spent})
            return {"aborted": False, "spent": spent, "message": msg}

        # A "length" stop means the model was cut off mid-call: its arguments
        # cannot be trusted, so every call in this message fails. The results
        # still go back (the wire requires one per tool_use) and the model gets
        # to retry with what's left of the budget.
        cut = set(msg.get("truncated") or ())
        if msg["stop_reason"] == "length":
            cut = {c["id"] for c in calls}
            emit({"type": "truncated", "count": len(calls)})

        # Gate every call first (sequentially — approvals are a human queue),
        # then execute. Models routinely emit 3-5 reads/searches in one message;
        # running those serially is dead wall-clock. Only read_only tools go
        # concurrent: mutating tools keep strict order so their effects compose.
        plan, interrupted, ran = [], False, 0
        for call in calls:
            verdict = None
            if call["id"] in cut:
                verdict = ("call was cut off by the output limit before its arguments finished; "
                           "it was NOT executed. Retry with a smaller call.")
            elif interrupted:
                verdict = "interrupted by user"
            elif spent["tool_calls"] >= budget["tool_calls"]:
                verdict = (f"tool-call budget exhausted ({budget['tool_calls']}). "
                           "Finish the task with what you already have.")
            tool = next((t for t in tools if t["name"] == call["name"]), None)
            if verdict is None:
                missing = _missing_args(tool, call.get("input") or {})
                if tool is None:
                    verdict = f"unknown tool: {call['name']}"
                elif missing:
                    # An incomplete call never reaches the tool: the declared
                    # input_schema is the contract, and the wire can deliver a
                    # call that violates it (truncation, model error).
                    verdict = f"missing required argument(s): {', '.join(missing)}. Not executed."
                else:
                    try:
                        allow = safety(call)
                    except KeyboardInterrupt:
                        allow, interrupted = {"allow": False, "reason": "interrupted by user"}, True
                    if not allow.get("allow"):
                        why = allow.get("reason")
                        verdict = f"denied by user: {why}" if why else "denied by user"
                    else:
                        spent["tool_calls"] += 1
                        ran += 1
            plan.append((call, tool, verdict))

        def execute(entry):
            call, tool, verdict = entry
            if verdict is not None:
                return False, verdict
            try:
                out = tool["execute"](call.get("input") or {})
                return out.get("ok", True), str(out.get("content", ""))
            except KeyboardInterrupt:
                return False, "interrupted by user"
            except Exception as exc:  # tool bugs become model-visible errors
                return False, f"tool failed: {exc}"

        runnable = [e for e in plan if e[2] is None]
        parallel = [e for e in runnable if e[1].get("read_only")]
        outcomes = {}
        if len(parallel) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(parallel))) as pool:
                for entry, res in zip(parallel, pool.map(execute, parallel)):
                    outcomes[entry[0]["id"]] = res
        for entry in plan:  # everything else, in the order the model asked for it
            if entry[0]["id"] not in outcomes:
                outcomes[entry[0]["id"]] = execute(entry)

        results = []
        for call, _tool, _v in plan:
            ok, content = outcomes[call["id"]]
            if len(content) > MAX_TOOL_RESULT_CHARS:
                dropped = len(content) - MAX_TOOL_RESULT_CHARS
                content = content[:MAX_TOOL_RESULT_CHARS] + f"\n…[truncated {dropped} chars]"
            if content == "":
                content = "(no output)"
            emit({"type": "tool_result", "id": call["id"], "name": call["name"],
                  "ok": ok, "content": content})
            result = {"type": "tool_result", "tool_use_id": call["id"], "content": content}
            if not ok:
                result["is_error"] = True
            results.append(result)

        # Steering: a message you typed while it was working. It rides along with
        # this round's tool results, so the model reads it on its very next step
        # and adjusts — the run is NOT stopped and nothing it has done is lost.
        # (tool_result blocks must come first in a user message; the note goes
        # after them.)
        content = list(results)
        if steer:
            note = steer()
            if note:
                content.append({"type": "text", "text": note})
                emit({"type": "steer", "text": note})

        user = {"role": "user", "content": content}
        transcript.append(user)
        jot(user)
        if interrupted:
            return {"aborted": True, "spent": spent, "message": None}

        # The budget is a hard ceiling, not a nag. Denying calls forever while
        # the model keeps asking would burn tokens without bound, so once the
        # budget is spent the model gets exactly `grace` more turns to write a
        # final answer; if it only calls tools, the run ends here.
        if ran == 0 and spent["tool_calls"] >= budget["tool_calls"]:
            stalled += 1
            if stalled > budget.get("grace", 2):
                emit({"type": "budget_stop", "spent": spent})
                return {"aborted": False, "spent": spent, "message": None,
                        "stopped": "tool-call budget exhausted"}
        else:
            stalled = 0


class APIError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"API {status}: {detail}" if status else detail)
        self.status = status
        self.detail = detail

    @property
    def context_overflow(self):
        d = self.detail.lower()
        return self.status == 400 and ("too long" in d or "context" in d or "max_tokens" in d)


def _why(exc):
    """What actually went wrong, in the words the user needs.

    Every retry used to be announced as "rate limited". A local server that was
    not running said "rate limited" too, five times, over a minute, while the one
    fact that would have helped (nothing is listening on that port) went unsaid.
    """
    if isinstance(exc, APIError):
        if exc.status == 429:
            return "rate limited"
        if exc.status and exc.status >= 500:
            return f"server error {exc.status}"
        return f"http {exc.status}"
    inner = getattr(exc, "reason", exc)
    text = str(inner).lower()
    if isinstance(inner, ConnectionRefusedError) or "refused" in text:
        return "connection refused"
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return "timed out"
    if isinstance(exc, json.JSONDecodeError):
        return "bad response from the server"
    if "name or service not known" in text or "nodename nor servname" in text:
        return "cannot resolve the host"
    return "network error"


def _retrying_stream(*, transcript, system, tools, api, budget, emit):
    """Transient failures are the norm on a long task, not the exception. A 429
    at tool call 14 used to throw the whole run away; now it costs a few seconds."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _stream(transcript=transcript, system=system, tools=tools,
                           api=api, budget=budget, emit=emit)
        except APIError as exc:
            retryable = exc.status in RETRY_ON
            if not retryable or attempt == MAX_ATTEMPTS:
                raise
            why = _why(exc)
            wait = exc.retry_after if getattr(exc, "retry_after", None) else min(30, 2 ** attempt)
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            why = _why(exc)
            # A refused connection is not a hiccup: waiting does not make a server
            # that is not listening start listening. Two quick tries in case a
            # gateway is restarting, then say the useful thing.
            refused = why == "connection refused"
            limit = 3 if refused else MAX_ATTEMPTS
            if attempt >= limit:
                if refused:
                    where = (api or {}).get("base_url") or "the endpoint"
                    raise APIError(None, f"cannot reach {where} — is the server running?") from None
                raise APIError(None, f"{why}: {exc}") from None
            wait = min(2 ** attempt, 4) if refused else min(30, 2 ** attempt)
        wait += random.uniform(0, 0.5)  # jitter: don't retry in lockstep with other clients
        emit({"type": "retry", "attempt": attempt, "of": MAX_ATTEMPTS,
              "wait": round(wait, 1), "why": why})
        time.sleep(wait)


def _missing_args(tool, given):
    if not tool:
        return []
    required = (tool.get("input_schema") or {}).get("required") or []
    return [k for k in required if given.get(k) in (None, "")]


# Endpoints that refuse an assistant message carrying reasoning_content. Found by
# asking, not by guessing: the field is sent, and if the server rejects it the
# request is retried without it and the endpoint is remembered.
NO_REASONING_ECHO = set()


def to_openai(system, messages, echo_reasoning=True):
    """Anthropic-shaped transcript → OpenAI chat messages.

    The reasoning goes back with the assistant turn as `reasoning_content`. Dropping
    it is the default in this wire, and it means the model re-derives its plan from
    its own tool calls at every step: it cannot see what it was thinking when it made
    them. Servers that reject the field are remembered in NO_REASONING_ECHO and get
    the plain shape.
    """
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text, calls, thinking = "", [], ""
            for b in content or []:
                if b.get("type") == "text":
                    text += b.get("text", "")
                elif b.get("type") == "thinking":
                    thinking += b.get("thinking", "")
                elif b.get("type") == "tool_use":
                    calls.append({"id": b.get("id"), "type": "function",
                                  "function": {"name": b.get("name"),
                                               "arguments": json.dumps(b.get("input") or {})}})
            msg = {"role": "assistant", "content": text}
            if thinking and echo_reasoning:
                msg["reasoning_content"] = thinking
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif role == "user":
            texts = []
            for b in content or []:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                                "content": str(b.get("content", ""))})
                elif b.get("type") == "text":
                    texts.append(b.get("text", ""))
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _stream(*, transcript, system, tools, api, budget, emit):
    """One wire round. Speaks either the Anthropic Messages wire (with
    interleaved thinking) or the OpenAI chat wire, and returns the same shape."""
    # Bookkeeping keys (context.py's elision markers) must never reach the wire.
    for m in transcript:
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                b.pop("_elided", None)

    if api.get("wire", "anthropic") == "openai":
        return _stream_openai(transcript=transcript, system=system, tools=tools,
                              api=api, budget=budget, emit=emit)

    body = {
        "model": api["model"],
        "max_tokens": api["max_tokens"],
        "stream": True,
        "messages": transcript,
    }
    # The reasoning contract has versions: budget_tokens (DeepSeek's shim, older
    # Anthropic), adaptive effort (current Anthropic), or no thinking at all.
    mode = api.get("thinking", "budget")
    if mode == "budget":
        body["thinking"] = {"type": "enabled", "budget_tokens": budget["thinking_tokens"]}
    elif mode == "adaptive":
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": budget.get("effort", "high")}
    if api.get("temperature") is not None and mode == "none":
        body["temperature"] = api["temperature"]
    if system:
        # One cache breakpoint after the (stable) system prompt: without it,
        # Anthropic re-bills the whole prefix every round — roughly 10x on a
        # long task. DeepSeek caches automatically and ignores this.
        body["system"] = [{"type": "text", "text": system,
                           "cache_control": {"type": "ephemeral"}}] if api.get("cache") else system
    if tools:
        body["tools"] = [{k: t[k] for k in ("name", "description", "input_schema")} for t in tools]

    headers = {
        "x-api-key": api["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if api.get("interleaved", True) and mode != "none":
        headers["anthropic-beta"] = INTERLEAVED_BETA
    log.write("request", f"anthropic {api['model']} msgs={len(transcript)} "
                         f"thinking={mode} interleaved={'anthropic-beta' in headers}")

    req = urllib.request.Request(f"{api['base_url'].rstrip('/')}/v1/messages",
                                 data=json.dumps(body).encode(), headers=headers, method="POST")

    blocks, partial_json, truncated = {}, {}, []
    message_id, stop_reason, usage = "", None, {}

    def completed():
        done = []
        for i in sorted(blocks):
            b = blocks[i]
            t = b["type"]
            keep = ((t == "text" and b.get("text"))
                    or (t == "thinking" and b.get("thinking"))
                    or (t == "redacted_thinking" and b.get("data"))
                    or (t == "tool_use" and "input" in b))
            if keep:
                if t == "thinking" and not b.get("signature"):
                    b["signature"] = message_id or "interrupted"
                done.append(b)
        return done

    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            for raw in res:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                etype = ev.get("type")
                if etype == "message_start":
                    message_id = ev["message"]["id"]
                    usage = dict(ev["message"].get("usage") or {})
                elif etype == "content_block_start":
                    i = ev["index"]
                    blocks[i] = dict(ev["content_block"])
                    if blocks[i]["type"] == "tool_use":
                        partial_json[i] = ""
                    emit({"type": "block_start", "block_type": blocks[i]["type"]})
                elif etype == "content_block_delta":
                    i, d = ev["index"], ev["delta"]
                    b = blocks[i]
                    if d["type"] == "thinking_delta":
                        b["thinking"] = b.get("thinking", "") + d["thinking"]
                        emit({"type": "thinking", "text": d["thinking"]})
                    elif d["type"] == "text_delta":
                        b["text"] = b.get("text", "") + d["text"]
                        emit({"type": "text", "text": d["text"]})
                    elif d["type"] == "input_json_delta":
                        partial_json[i] += d["partial_json"]
                    elif d["type"] == "signature_delta":
                        b["signature"] = b.get("signature", "") + d["signature"]
                    elif d["type"] == "redacted_thinking_delta":
                        b["data"] = b.get("data", "") + d.get("data", "")
                elif etype == "content_block_stop":
                    i = ev["index"]
                    b = blocks[i]
                    if b["type"] == "tool_use":
                        # Arguments can be cut off mid-JSON when max_tokens hits.
                        # Record the id as truncated rather than raising; run()
                        # fails the call so the transcript stays valid. (The id
                        # is kept out of the block — the wire rejects extra keys.)
                        try:
                            b["input"] = json.loads(partial_json[i]) if partial_json[i] else {}
                        except json.JSONDecodeError:
                            b["input"] = {}
                            truncated.append(b["id"])
                        emit({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]})
                elif etype == "message_delta":
                    stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
                    usage.update(ev.get("usage") or {})
                elif etype == "error":
                    raise RuntimeError(f"stream error: {json.dumps(ev.get('error'))}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        err = APIError(exc.code, detail)
        ra = exc.headers.get("retry-after") if exc.headers else None
        try:
            err.retry_after = float(ra) if ra else None
        except ValueError:
            err.retry_after = None
        raise err from None
    except KeyboardInterrupt as exc:
        # hand completed blocks to run() so an aborted trajectory still lands in the transcript
        exc.partial = completed()
        raise

    # DeepSeek signs thinking blocks with the message id; backfill if the stream omitted it
    for b in blocks.values():
        if b["type"] == "thinking" and not b.get("signature"):
            b["signature"] = message_id

    content = [blocks[i] for i in sorted(blocks) if blocks[i]["type"] in ECHO_BLOCKS]
    log.write("response", f"stop={stop_reason} blocks={[b['type'] for b in content]} usage={usage}")
    return {
        "id": message_id,
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage,
        "truncated": truncated,
    }


def _rejects_reasoning(detail):
    d = (detail or "").lower()
    return any(w in d for w in ("reasoning_content", "reasoning", "unrecognized",
                                "unknown field", "additional propert", "unexpected"))


def _stream_openai(*, transcript, system, tools, api, budget, emit):
    """The OpenAI chat wire. Reasoning arrives as `reasoning_content` deltas and
    tool calls arrive fragmented across chunks, so both must be accumulated."""
    endpoint = api["base_url"].rstrip("/")
    echo = endpoint not in NO_REASONING_ECHO
    try:
        return _openai_request(transcript=transcript, system=system, tools=tools, api=api,
                               budget=budget, emit=emit, echo_reasoning=echo)
    except APIError as exc:
        if not (echo and exc.status == 400 and _rejects_reasoning(exc.detail)):
            raise
        # this server will not take the reasoning back. Remember, and carry on
        # without it rather than failing the turn.
        NO_REASONING_ECHO.add(endpoint)
        log.write("request", f"openai {endpoint} rejects reasoning echo, dropping it")
        return _openai_request(transcript=transcript, system=system, tools=tools, api=api,
                               budget=budget, emit=emit, echo_reasoning=False)


def _openai_request(*, transcript, system, tools, api, budget, emit, echo_reasoning):
    body = {
        "model": api["model"],
        "messages": to_openai(system, transcript, echo_reasoning=echo_reasoning),
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": api["max_tokens"],
    }
    if api.get("thinking", "budget") != "none":
        body["reasoning_effort"] = budget.get("effort", "high")
    if api.get("temperature") is not None:
        body["temperature"] = api["temperature"]
    if tools:
        body["tools"] = [{"type": "function",
                          "function": {"name": t["name"], "description": t.get("description", ""),
                                       "parameters": t["input_schema"]}} for t in tools]
        body["tool_choice"] = "auto"

    log.write("request", f"openai {api['model']} msgs={len(transcript)} tools={bool(tools)}")
    req = urllib.request.Request(
        f"{api['base_url'].rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api['api_key'] or 'local'}",
                 "Content-Type": "application/json"},
        method="POST")

    text, thinking, calls = "", "", {}
    stop_reason, usage, msg_id = None, {}, ""
    started = {"thinking": False, "text": False}

    def completed():
        out = []
        if thinking:
            out.append({"type": "thinking", "thinking": thinking, "signature": msg_id or "openai"})
        if text:
            out.append({"type": "text", "text": text})
        return out

    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            for raw in res:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                msg_id = chunk.get("id") or msg_id
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = {"input_tokens": u.get("prompt_tokens", 0),
                             "output_tokens": u.get("completion_tokens", 0),
                             "cache_read_input_tokens":
                                 (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)}
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if rc:
                    if not started["thinking"]:
                        started["thinking"] = True
                        emit({"type": "block_start", "block_type": "thinking"})
                    thinking += rc
                    emit({"type": "thinking", "text": rc})
                if delta.get("content"):
                    if not started["text"]:
                        started["text"] = True
                        emit({"type": "block_start", "block_type": "text"})
                    text += delta["content"]
                    emit({"type": "text", "text": delta["content"]})
                for tcd in delta.get("tool_calls") or []:
                    i = tcd.get("index", 0)
                    slot = calls.setdefault(i, {"id": None, "name": "", "arguments": ""})
                    if tcd.get("id"):
                        slot["id"] = tcd["id"]
                    fn = tcd.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    fr = choice["finish_reason"]
                    stop_reason = ("tool_use" if fr == "tool_calls"
                                   else "length" if fr == "length" else "end_turn")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        err = APIError(exc.code, detail)
        ra = exc.headers.get("retry-after") if exc.headers else None
        try:
            err.retry_after = float(ra) if ra else None
        except ValueError:
            err.retry_after = None
        raise err from None
    except KeyboardInterrupt as exc:
        exc.partial = completed()
        raise

    content, truncated = completed(), []
    for i in sorted(calls):
        slot = calls[i]
        tid = slot["id"] or f"call_{i}"
        try:
            inp = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            inp, _ = {}, truncated.append(tid)
        block = {"type": "tool_use", "id": tid, "name": slot["name"], "input": inp}
        content.append(block)
        emit({"type": "tool_use", "id": tid, "name": slot["name"], "input": inp})

    log.write("response", f"stop={stop_reason} usage={usage}")
    return {"id": msg_id, "content": content, "stop_reason": stop_reason,
            "usage": usage, "truncated": truncated}
