"""sesame agent — providers.py — endpoint presets (restored from the old sesame).

Dropping the OpenAI wire cost every provider except DeepSeek/Anthropic. It is
back: `wire` says which protocol to speak, not which vendor to trust.
"""

# name: (base_url, wire, default_model)
PRESETS = {
    "deepseek": ("https://api.deepseek.com/anthropic", "anthropic", "deepseek-v4-flash"),
    "anthropic": ("https://api.anthropic.com", "anthropic", ""),
    "openai": ("https://api.openai.com/v1", "openai", ""),
    "openrouter": ("https://openrouter.ai/api/v1", "openai", ""),
    "groq": ("https://api.groq.com/openai/v1", "openai", ""),
    "together": ("https://api.together.xyz/v1", "openai", ""),
    "xai": ("https://api.x.ai/v1", "openai", ""),
    "mistral": ("https://api.mistral.ai/v1", "openai", ""),
    # local: no key needed, nothing leaves your machine
    "ollama": ("http://localhost:11434/v1", "openai", ""),
    "lmstudio": ("http://localhost:1234/v1", "openai", ""),
}
# An empty default model means "ask the provider": sesame fetches the live list
# and picks from it. A hardcoded model id is a promise that goes stale.
#
# There is one row per VENDOR, not per protocol. DeepSeek speaks both the
# Anthropic and the OpenAI wire; that is a detail of how we talk to it, not a
# second provider you should have to choose between. The wire is derived from
# the URL (".../anthropic" means the Anthropic wire) and can still be forced
# with `wire` in the config if you ever need to.

# providers that run on your own machine: no API key required
LOCAL = {"ollama", "lmstudio"}


def is_local(name_or_url):
    return (name_or_url in LOCAL
            or "localhost" in str(name_or_url) or "127.0.0.1" in str(name_or_url))

WIRES = ("anthropic", "openai")


def preset(name):
    return PRESETS.get(name.lower())


def names():
    return sorted(PRESETS)
