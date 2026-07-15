"""sesame agent — models.py — what we know about each model.

Rules for this table:
  · never invent a price. If it isn't known, it is None and shows as "—".
  · `window` is the real context window, not a guess.
  · a model missing from here still works — you just get no cost estimate.
"""

DEEPSEEK = "https://api.deepseek.com/anthropic"
DEEPSEEK_OAI = "https://api.deepseek.com"
ANTHROPIC = "https://api.anthropic.com"
OPENAI = "https://api.openai.com/v1"
OPENROUTER = "https://openrouter.ai/api/v1"
GROQ = "https://api.groq.com/openai/v1"
TOGETHER = "https://api.together.xyz/v1"
XAI = "https://api.x.ai/v1"
MISTRAL = "https://api.mistral.ai/v1"

# This table is ONLY for metadata (context window, price) of models whose numbers
# are actually known. It is NOT the list of models you can use: that is fetched
# live from your provider by fetch(), because a hardcoded list goes stale and can
# never know what your own ollama has pulled.
MODELS = {
    # deepseek
    "deepseek-v4-flash": {
        "provider": "deepseek", "base_url": DEEPSEEK, "window": 1_000_000,
        "thinking": "budget", "in": 0.14, "cache_read": 0.0028, "cache_write": 0.14, "out": 0.28,
    },
    "deepseek-chat": {
        "provider": "deepseek", "base_url": DEEPSEEK, "window": 128_000,
        "thinking": "none", "in": 0.14, "cache_read": 0.0028, "cache_write": 0.14, "out": 0.28,
    },
    "deepseek-reasoner": {
        "provider": "deepseek", "base_url": DEEPSEEK, "window": 128_000,
        "thinking": "budget", "in": 0.55, "cache_read": 0.014, "cache_write": 0.55, "out": 2.19,
    },
}

UNKNOWN = {"provider": "?", "base_url": None, "window": 128_000, "thinking": "budget",
           "in": None, "cache_read": None, "cache_write": None, "out": None}


def spec(model):
    return MODELS.get(model, UNKNOWN)


def fetch(base_url, api_key="", wire="openai", timeout=8):
    """Ask the provider what it actually serves.

    Hardcoding model ids goes stale the week you write it, and it cannot know
    what YOUR ollama has pulled. Every OpenAI-compatible server (including
    ollama and LM Studio) exposes GET /models; Anthropic exposes /v1/models.
    Returns [] if the endpoint does not answer, and the caller falls back to the
    static table.
    """
    import json as _json
    import urllib.error
    import urllib.request

    base = base_url.rstrip("/")
    if wire == "anthropic":
        url = base + "/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"Authorization": f"Bearer {api_key or 'local'}"}
    def _get(u, h):
        req = urllib.request.Request(u, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return _json.loads(res.read())

    try:
        data = _get(url, headers)
    except Exception:
        # an Anthropic-compatible shim (e.g. .../anthropic) usually still serves
        # the plain OpenAI model list at the root
        try:
            root = base.rsplit("/anthropic", 1)[0]
            data = _get(root + "/v1/models", {"Authorization": f"Bearer {api_key or 'local'}"})
        except Exception:
            return []
    items = data.get("data") or data.get("models") or []
    out = []
    for it in items:
        mid = it.get("id") or it.get("name") if isinstance(it, dict) else str(it)
        if mid:
            out.append(mid)
    return sorted(set(out))


def window_of(base_url, api_key="", wire="openai", model="", timeout=6):
    """The context window the SERVER says it has, or 0 if it will not say.

    A model you serve yourself has no row in the table above, so the only honest
    source for its window is the server. llama.cpp reports meta.n_ctx, vLLM
    max_model_len, MTPLX and others context_length. Guessing 128k instead means
    sesame compacts a 256k model at half its capacity, or overflows a 32k one.
    """
    import json as _json
    import urllib.request

    base = base_url.rstrip("/")
    url = base + ("/models" if base.endswith("/v1") else "/v1/models")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key or 'local'}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = _json.loads(res.read())
    except Exception:
        return 0
    items = data.get("data") or data.get("models") or []
    keys = ("context_length", "max_model_len", "max_context_length", "n_ctx", "context_window")
    for it in items:
        if not isinstance(it, dict):
            continue
        if model and (it.get("id") or it.get("name")) != model:
            continue
        for src in (it, it.get("meta") or {}, it.get("details") or {}):
            if not isinstance(src, dict):
                continue
            for k in keys:
                try:
                    n = int(src.get(k) or 0)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    return n
    return 0


def known(provider=None):
    if provider:
        return sorted(m for m, s in MODELS.items() if s["provider"] == provider)
    return sorted(MODELS)


def priced(model):
    return spec(model)["in"] is not None


def cost(model, usage):
    """usage: {input_tokens, cache_read_tokens, cache_write_tokens, output_tokens}.

    On the Anthropic wire `input_tokens` already EXCLUDES cached tokens, so they
    are billed separately here rather than subtracted.
    """
    s = spec(model)
    if s["in"] is None:
        return 0.0
    return (usage.get("input_tokens", 0) / 1e6 * s["in"]
            + usage.get("cache_read_tokens", 0) / 1e6 * (s["cache_read"] or 0)
            + usage.get("cache_write_tokens", 0) / 1e6 * (s["cache_write"] or 0)
            + usage.get("output_tokens", 0) / 1e6 * s["out"])
