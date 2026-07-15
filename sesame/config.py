"""sesame agent — config.py — the Config object the TUI and Loop read.

Field names match the original sesame's Config so the TUI works unchanged;
the storage underneath is the layered one (.env → ~/.sesame → ./.sesame → env)
and the provider table from providers.py.
"""

import os

import models
import project
import providers


def _int(v):
    """A window read from JSON or the environment may be a string."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class Config:
    def __init__(self, install_defaults=None):
        d = project.load_config(install_defaults or {})
        self._raw = d
        # keys are remembered PER PROVIDER, so switching back and forth never
        # asks again — and a switch can never leave you authenticated to nothing
        self.keys = dict(d.get("keys") or {})
        self.api_key = d.get("apiKey", "")
        if self.api_key and d.get("provider"):
            self.keys.setdefault(d["provider"], self.api_key)
        self.base_url = d.get("baseUrl") or "https://api.deepseek.com/anthropic"
        self.model = d.get("model") or "deepseek-v4-flash"
        self.api_type = d.get("wire", "anthropic")          # "anthropic" | "openai"
        self.active_provider = d.get("provider", "deepseek")
        self.max_output_tokens = d.get("maxTokens", 16000)
        self.thinking_budget = d.get("thinkingTokens", 8000)
        self.reasoning_effort = d.get("effort", "high")
        self.temperature = d.get("temperature")
        self.interleaved = d.get("interleaved", True)
        self.confirm_danger = d.get("confirmDanger", True)
        # ask on EVERY write, not just dangerous ones (off — that was the noise)
        self.confirm_all = d.get("confirmAll", False)
        self.browser_headed = d.get("browserHeaded", False)
        # mouse capture off → the terminal keeps its native selection, so ⌘C works
        self.mouse = d.get("mouse", False)
        self.bash_timeout = d.get("bashTimeoutMs", 180_000) / 1000
        self.tool_output_limit = d.get("toolOutputLimit", 16_000)
        self.tool_call_budget = d.get("toolCallBudget", 50)
        self.compact_keep_recent = d.get("compactKeepRecent", 8)
        self.log_file = d.get("log") or os.environ.get("SESAME_LOG", "")
        self.profile = d.get("profile")
        self.workdir = os.environ.get("SESAME_WORKDIR") or os.getcwd()
        self._apply_model(self.model)
        self.context_window = _int(d.get("contextWindow")) or models.spec(self.model)["window"]
        if d.get("thinking") in ("none", "budget", "effort"):
            self.thinking_mode = d["thinking"]
        # SESAME_PROFILE runs one session under a profile without persisting it,
        # so a local-model launcher cannot leave your normal setup switched over.
        env_profile = os.environ.get("SESAME_PROFILE")
        if env_profile:
            prof = project.load_profiles().get(env_profile)
            if prof:
                self._apply_profile(prof)
                self.profile = env_profile

    # ── derived views the Loop hands to the shell ────────────────────────────
    @property
    def effective_max_tokens(self):
        if self.thinking_budget:
            return max(self.max_output_tokens, self.thinking_budget + 4096)
        return self.max_output_tokens

    @property
    def api(self):
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "max_tokens": self.effective_max_tokens,
            "wire": self.api_type,
            "thinking": self.thinking_mode,
            "interleaved": self.interleaved,
            "temperature": self.temperature,
            "cache": self.base_url.rstrip("/") == models.ANTHROPIC,
        }

    @property
    def budget(self):
        return {"tool_calls": self.tool_call_budget,
                "thinking_tokens": self.thinking_budget,
                "effort": self.reasoning_effort,
                "grace": 2}

    def _apply_model(self, model):
        """Window and thinking mode come from the model, always. A profile that
        needs to override them (a model sesame does not know) does so after this
        call, never by leaving a value behind in the config for the next model to
        inherit."""
        spec = models.spec(model)
        self.model = model
        self.thinking_mode = spec["thinking"]
        self.context_window = spec["window"]

    # ── switching ────────────────────────────────────────────────────────────
    def provider_names(self):
        return providers.names()

    def key_for(self, provider):
        return self.keys.get(provider, "")

    def needs_key(self, provider, base_url=""):
        if providers.is_local(provider) or (base_url and providers.is_local(base_url)):
            return False
        return not self.key_for(provider)

    def switch_provider(self, name, api_key=""):
        """Never commits a half-configured provider: if it needs a key and none
        is known, the caller must supply one first (that 401 was this bug)."""
        p = providers.preset(name)
        if not p:
            return False
        url, wire, model = p
        if api_key:
            self.keys[name] = api_key
        if self.needs_key(name, url):
            return False                       # caller asks for a key, then retries
        self.base_url, self.api_type = url, wire
        self.active_provider = name
        self.api_key = self.key_for(name)
        # the preset's default model is a hint, not a fact: ask the provider what
        # it really serves and take the default only if it is actually there
        live = models.fetch(url, self.api_key, wire, timeout=6)
        if live and model not in live:
            model = live[0]
        self._apply_model(model)
        self.profile = None          # you picked a provider yourself
        project.save_config({"baseUrl": url, "wire": wire, "model": model, "provider": name,
                             "apiKey": self.api_key, "keys": self.keys,
                             "contextWindow": self.context_window,
                             "thinking": self.thinking_mode, "profile": None})
        return True

    def use_model(self, model, api_key=""):
        """Switch model, following its provider's endpoint. Same rule: no key,
        no commit."""
        spec = models.spec(model)
        prov, url = spec["provider"], spec["base_url"]
        if api_key:
            self.keys[prov] = api_key
        if url and self.needs_key(prov, url):
            return False
        if url:
            self.base_url = url
            self.api_type = "anthropic" if "anthropic" in url else "openai"
            self.active_provider = prov
            self.api_key = self.key_for(prov) or self.api_key
        self._apply_model(model)
        self.profile = None          # you picked a model yourself
        project.save_config({"model": model, "baseUrl": self.base_url, "wire": self.api_type,
                             "provider": self.active_provider, "apiKey": self.api_key,
                             "keys": self.keys, "contextWindow": self.context_window,
                             "thinking": self.thinking_mode, "profile": None})
        return True

    # ── profiles ─────────────────────────────────────────────────────────────
    # contextWindow and thinking are part of a profile because a model sesame has
    # never heard of (anything you serve yourself) has no entry in models.py: it
    # would be sized at the 128k default and sent a reasoning_effort its server
    # never asked for. The profile is where that knowledge lives.
    FIELDS = ("provider", "baseUrl", "wire", "model", "apiKey", "effort",
              "thinkingTokens", "contextWindow", "thinking")

    def profiles(self):
        return project.load_profiles()

    def snapshot(self):
        """The current setup, as a profile would store it."""
        return {"provider": self.active_provider, "baseUrl": self.base_url,
                "wire": self.api_type, "model": self.model, "apiKey": self.api_key,
                "effort": self.reasoning_effort, "thinkingTokens": self.thinking_budget,
                "contextWindow": self.context_window, "thinking": self.thinking_mode}

    def save_profile(self, name):
        profs = project.load_profiles()
        profs[name] = self.snapshot()
        project.save_profiles(profs)
        project.save_config({"profile": name})
        self.profile = name
        return name

    def delete_profile(self, name):
        profs = project.load_profiles()
        if name not in profs:
            return False
        del profs[name]
        project.save_profiles(profs)
        if self.profile == name:
            project.save_config({"profile": None})
            self.profile = None
        return True

    def _apply_profile(self, prof):
        """Load a profile into this process. Saves nothing."""
        self.active_provider = prof.get("provider", "custom")
        self.base_url = prof.get("baseUrl", self.base_url)
        self.api_type = prof.get("wire", "openai")
        self.api_key = prof.get("apiKey", "")
        if prof.get("effort"):
            self.set_effort(prof["effort"])
        if prof.get("thinkingTokens"):
            self.thinking_budget = prof["thinkingTokens"]
        # order matters: _apply_model would reset both of these from models.py
        self._apply_model(prof.get("model", self.model))
        if _int(prof.get("contextWindow")):
            self.context_window = _int(prof["contextWindow"])
        if prof.get("thinking") in ("none", "budget", "effort"):
            self.thinking_mode = prof["thinking"]
        if self.api_key:
            self.keys[self.active_provider] = self.api_key

    def use_profile(self, name):
        profs = project.load_profiles()
        prof = profs.get(name)
        if not prof:
            return False
        self._apply_profile(prof)
        project.save_config({"provider": self.active_provider, "baseUrl": self.base_url,
                             "wire": self.api_type, "model": self.model,
                             "apiKey": self.api_key, "keys": self.keys,
                             "contextWindow": self.context_window,
                             "thinking": self.thinking_mode, "profile": name})
        self.profile = name
        return True

    def connect(self, base_url, model, api_key="", wire="openai", window=0, thinking=None):
        """Point sesame at any endpoint: a local runtime, or your own gateway.

        The key is the one you give for THIS endpoint, or the one you gave it
        last time. Never the key of the provider you happen to be leaving.
        Carrying that one over would send your DeepSeek (or Anthropic) key as a
        bearer token to whatever URL you just typed in. Keys are kept per
        provider, so leaving one behind loses nothing: switch back and it is
        still there.
        """
        self.base_url = base_url
        self.api_type = wire if wire in providers.WIRES else "openai"
        self.active_provider = "custom"
        if api_key:
            self.keys["custom"] = api_key
        self.api_key = api_key or self.keys.get("custom", "")
        self._apply_model(model)
        if _int(window):
            self.context_window = _int(window)
        if thinking in ("none", "budget", "effort"):
            self.thinking_mode = thinking
        self.profile = None          # you pointed it somewhere yourself
        # apiKey is written even when it is empty. Leaving the old one on disk was
        # the leak: on the next load, provider=custom + a stale key meant the key
        # got adopted as the custom endpoint's own, and sent to it.
        save = {"baseUrl": base_url, "model": model, "wire": self.api_type, "provider": "custom",
                "keys": self.keys, "contextWindow": self.context_window,
                "thinking": self.thinking_mode, "profile": None, "apiKey": self.api_key}
        project.save_config(save)
        return True

    def switch_model(self, model):
        self._apply_model(model)
        self.profile = None
        project.save_config({"model": model, "contextWindow": self.context_window,
                             "thinking": self.thinking_mode, "profile": None})
        return True

    def set(self, field, value):
        key = {"key": "apiKey", "api_key": "apiKey", "url": "baseUrl", "base_url": "baseUrl",
               "model": "model", "type": "wire", "api_type": "wire"}.get(field)
        if not key:
            return False
        project.save_config({key: value})
        if key == "apiKey":
            self.api_key = value
        elif key == "baseUrl":
            self.base_url = value
        elif key == "wire":
            self.api_type = value
        else:
            self._apply_model(value)
        return True

    def set_effort(self, level):
        levels = {"low": 2000, "medium": 4000, "high": 8000, "max": 16000}
        if level not in levels:
            return False
        self.reasoning_effort = level
        self.thinking_budget = levels[level]
        return True

    def validate(self):
        if not self.api_key and not providers.is_local(self.base_url):
            return ("No API key. Run: ./run.sh setup   (or put SESAME_API_KEY in a .env file)")
        return None
