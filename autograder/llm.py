"""Minimal, dependency-free client for local (Ollama) and hosted (Groq /
OpenAI-compatible) chat models, with a response cache.

Reproducibility: requests use temperature 0 and a fixed seed, and every
response is stored on disk under a key derived from (provider, model,
temperature, seed, prompt). A repeated grading run reads the stored response
instead of asking the model again, so the LLM-based metric returns the same
score even if the model's sampling is not bit-exact across runs. The cache key
does not include the host, so a cache filled on one machine can be reused on
another.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .env import DEFAULT_MODEL, ollama_model
from .util import canonical_json, sha256_text

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
PROVIDER_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
}
PROVIDER_KEYS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}

_cache_lock = threading.Lock()
log = logging.getLogger("autograder")


class LLMError(RuntimeError):
    pass


class LLMTimeout(LLMError):
    """The model did not answer in time (it may be loading, or the machine is short of memory)."""


@dataclass
class LLMReply:
    text: str
    cached: bool
    provider: str
    model: str
    cache_key: str


class LLMClient:
    def __init__(self, provider: str = "ollama", model: str = DEFAULT_MODEL, *,
                 temperature: float = 0.0, seed: int = 42, host: Optional[str] = None,
                 base_url: Optional[str] = None, api_key_env: Optional[str] = None,
                 timeout_s: float = 300.0, cache_dir: Optional[Path] = None, json_mode: bool = True,
                 num_ctx: int = 4096, num_predict: int = 2048, keep_alive: str = "30m"):
        self.provider = provider
        self.model = model
        self.temperature = float(temperature)
        self.seed = int(seed)
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.base_url = (base_url or PROVIDER_URLS.get(provider, "")).rstrip("/")
        self.api_key_env = api_key_env or PROVIDER_KEYS.get(provider, "LLM_API_KEY")
        self.timeout_s = timeout_s
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.json_mode = json_mode
        self.num_ctx = num_ctx          # prompt + answer window; smaller means less memory and faster calls
        self.num_predict = num_predict  # most tokens one answer may have: stops a model that rambles
        self.keep_alive = keep_alive    # how long Ollama keeps the model in memory after a call

    @classmethod
    def from_params(cls, params: Dict[str, Any], *, cache_dir: Optional[Path], seed: Optional[int] = None,
                    json_mode: bool = True) -> "LLMClient":
        """A client configured from an analyzer's ``params`` (provider, model, limits ...).

        For Ollama the model comes from ``OLLAMA_MODEL`` (the ``.env`` file) when it is set, whatever the rules
        say, so one setting switches the whole project; otherwise from the params, else the project default.
        """
        provider = params.get("provider", "ollama")
        model = ollama_model(params.get("model")) if provider == "ollama" else params.get("model", DEFAULT_MODEL)
        return cls(provider=provider, model=model,
                   temperature=params.get("temperature", 0), seed=params.get("seed", 42) if seed is None else seed,
                   host=params.get("host"), base_url=params.get("base_url"), api_key_env=params.get("api_key_env"),
                   timeout_s=float(params.get("timeout_s", 300)), cache_dir=cache_dir, json_mode=json_mode,
                   num_ctx=int(params.get("num_ctx", 4096)), num_predict=int(params.get("num_predict", 2048)),
                   keep_alive=str(params.get("keep_alive", "30m")))

    def warm(self) -> None:
        """Ollama only: load the model into memory now (a cold load can take a minute on a small machine)."""
        if self.provider != "ollama":
            return
        try:
            self._resolve_model()
            self._post(self.host + "/api/generate", {"model": self.model, "keep_alive": self.keep_alive}, {})
        except LLMError:
            pass  # the real request reports any problem

    # ------------------------------------------------------------ public
    def chat(self, system: str, user: str) -> LLMReply:
        for attempt in range(2):
            request = {"provider": self.provider, "model": self.model, "temperature": self.temperature,
                       "seed": self.seed, "json": self.json_mode, "system": system, "user": user}
            key = sha256_text(canonical_json(request))
            cached = self._cache_get(key)
            if cached is not None:
                return LLMReply(cached, True, self.provider, self.model, key)
            # Not cached: make sure the model exists here, else switch to an installed one and look again.
            if attempt == 0 and not self._resolve_model():
                break
        if self.provider == "ollama":
            text = self._ollama(system, user)
        elif self.provider in ("groq", "openai", "openai_compatible"):
            text = self._openai_compatible(system, user)
        else:
            raise LLMError(f"unknown LLM provider {self.provider!r}")
        self._cache_put(key, request, text)
        return LLMReply(text, False, self.provider, self.model, key)

    def chat_json(self, system: str, user: str) -> Tuple[Dict[str, Any], LLMReply]:
        reply = self.chat(system, user)
        return extract_json(reply.text), reply

    def _resolve_model(self) -> bool:
        """Ollama only: if the requested model is not installed, use an installed one. True if it changed.

        Preference: same family (``qwen2.5-coder:7b`` -> ``qwen2.5-coder:3b``), then any coder model,
        then the first installed model. The model actually used is what reports and manifests record.
        """
        if self.provider != "ollama":
            return False
        try:
            with urllib.request.urlopen(self.host + "/api/tags", timeout=5) as resp:
                names = [m.get("name", "") for m in json.loads(resp.read().decode("utf-8")).get("models", [])]
        except (OSError, ValueError):
            return False  # unreachable: the request itself reports the error
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if not names or wanted in names or self.model in names:
            return False
        family = self.model.split(":")[0]
        pick = (sorted(n for n in names if n.split(":")[0] == family) or sorted(n for n in names if "coder" in n)
                or sorted(names))[0]
        log.warning("LLM model %s is not installed in Ollama; using %s instead (ollama pull %s to use it)",
                    self.model, pick, self.model)
        self.model = pick
        return True

    # ------------------------------------------------------------ backends
    def _post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMTimeout(f"no answer from the model within {self.timeout_s:g} s") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise LLMTimeout(f"no answer from the model within {self.timeout_s:g} s") from exc
            raise LLMError(f"cannot reach {url}: {exc}") from exc
        except (OSError, ValueError) as exc:
            raise LLMError(f"cannot reach {url}: {exc}") from exc

    def _ollama(self, system: str, user: str) -> str:
        payload: Dict[str, Any] = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": self.temperature, "seed": self.seed, "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict},
            "keep_alive": self.keep_alive,
        }
        if self.json_mode:
            payload["format"] = "json"
        body = self._post(self.host + "/api/chat", payload, {})
        try:
            return body["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected Ollama response: {str(body)[:200]}") from exc

    def _openai_compatible(self, system: str, user: str) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise LLMError(f"environment variable {self.api_key_env} is not set")
        payload: Dict[str, Any] = {
            "model": self.model, "temperature": self.temperature, "seed": self.seed,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = self._post(self.base_url + "/chat/completions", payload, {"Authorization": f"Bearer {key}"})
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response: {str(body)[:200]}") from exc

    # ------------------------------------------------------------ cache
    def _cache_file(self, key: str) -> Optional[Path]:
        return self.cache_dir / key[:2] / f"{key}.json" if self.cache_dir else None

    def _cache_get(self, key: str) -> Optional[str]:
        path = self._cache_file(key)
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        except (ValueError, KeyError, OSError):
            return None

    def _cache_put(self, key: str, request: Dict[str, Any], text: str) -> None:
        path = self._cache_file(key)
        if path is None:
            return
        with _cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"request": request, "response": text}, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)


def extract_json(text: str) -> Dict[str, Any]:
    """Parse the first JSON object in a model reply (tolerates fences and <think> blocks)."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(cleaned[start:end + 1])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise LLMError(f"model reply is not a JSON object: {text[:200]!r}")


def extract_code(text: str) -> str:
    """Extract a C program from a free-form model reply."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
    m = re.search(r"```(?:c|C|cpp)?\s*\n([\s\S]*?)```", cleaned)
    return (m.group(1) if m else cleaned).strip() + "\n"
