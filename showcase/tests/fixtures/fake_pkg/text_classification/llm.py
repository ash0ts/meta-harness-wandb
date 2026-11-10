# SPDX-License-Identifier: MIT
"""Stdlib-only stand-in for upstream ``llm.py`` with the same seams (``llm.py`` L134-L422 at f805ad3).

``ProviderLLM._generate_one`` / ``_cache_path`` / ``_load_cache`` and ``LLM.__call__`` / ``batch`` /
``_truncate`` keep upstream's shapes and call order. The completion call posts an OpenAI-style chat body with
``urllib`` to ``--api-base`` (Route 1) or ``$OPENROUTER_API_BASE`` (Route 2), bearer ``$OPENROUTER_API_KEY``,
and accepts a ``chat.completion`` JSON body. Retries follow upstream's predicate with short sleeps.
"""

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CACHE_DIR = Path(
    os.path.expanduser(
        os.environ.get("TEXT_CLASSIFICATION_LLM_CACHE_DIR", "~/.cache/text-classification/litellm")
    )
)
CACHE_VERSION = 1
KNOWN_PROVIDER_PREFIXES = ("anthropic/", "gemini/", "openai/", "openrouter/")
MAX_PROMPT_CHARS = 224_000
MAX_ATTEMPTS = 6

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ProviderError(Exception):
    """A non-2xx reply or a transport failure. ``status_code`` is the HTTP status when there was one."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return any(t in text for t in ("429", "rate limit", "too many requests", "retry in", "timed out", "timeout"))


def _wire_model(model: str) -> str:
    return model[len("openrouter/"):] if model.startswith("openrouter/") else model


def litellm_completion(model: str, messages: list, **kwargs: Any) -> dict:
    """POST one chat completion. Named after upstream's seam (L15, L220), which the capture never patches."""
    base = kwargs.pop("base_url", None) or os.environ.get("OPENROUTER_API_BASE")
    key = kwargs.pop("api_key", None) or os.environ.get("OPENROUTER_API_KEY", "")
    timeout = kwargs.pop("timeout", 600.0)
    if not base:
        raise ProviderError("no API base configured")
    body = json.dumps({"model": _wire_model(model), "messages": messages, **kwargs}).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %d: %s" % (exc.code, text), exc.code) from None
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError("transport error: %s" % type(exc).__name__) from None


class LLMCallable(Protocol):
    def __call__(self, prompt: str) -> str: ...


class ProviderLLM:
    """Same shape as upstream's ``ProviderLLM``: file cache, usage counters, pool for list prompts."""

    def __init__(self, model: str, max_concurrent: int = 4, api_key: str | None = None,
                 api_base: str | None = None):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self._usage_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def __exit__(self, *exc):
        self._executor.shutdown(wait=True)
        return False

    def _normalized_model(self) -> str:
        if self.api_base and not self.model.startswith(KNOWN_PROVIDER_PREFIXES):
            return f"openai/{self.model}"
        return self.model

    def _cache_path(self, prompt: str, system_prompt: str | None, kwargs: dict[str, Any]) -> Path:
        payload = {"version": CACHE_VERSION, "model": self._normalized_model(), "api_base": self.api_base,
                   "system_prompt": system_prompt, "prompt": prompt, "kwargs": kwargs}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return CACHE_DIR / f"{digest}.json"

    def _load_cache(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cache(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp." + str(threading.get_ident()))
        try:
            with self._cache_lock:
                tmp.write_text(json.dumps(payload))
                tmp.replace(path)
        except OSError:
            pass

    def _call_completion(self, prompt: str, system_prompt: str | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_kwargs = dict(kwargs)
        call_kwargs["timeout"] = 600.0
        if self.api_base:
            call_kwargs["base_url"] = self.api_base
            call_kwargs.setdefault("api_key", self.api_key or "local")
        elif self.api_key is not None:
            call_kwargs["api_key"] = self.api_key
        attempt = 0
        while True:
            attempt += 1
            try:
                response = litellm_completion(model=self._normalized_model(), messages=messages, **dict(call_kwargs))
                break
            except Exception as exc:
                if attempt >= MAX_ATTEMPTS or not _is_retryable(exc):
                    raise
                time.sleep(min(0.05 * 2 ** attempt, 0.5))
        content = response["choices"][0]["message"].get("content") or ""
        usage = response.get("usage") or {}
        return {"content": content, "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0), "cost": 0.0}

    def _generate_one(self, prompt: str, system_prompt: str | None, kwargs: dict[str, Any]) -> str:
        cache_path = self._cache_path(prompt, system_prompt, kwargs)
        cached = self._load_cache(cache_path)
        if cached is not None:
            return cached["content"]
        result = self._call_completion(prompt, system_prompt, kwargs)
        self._save_cache(cache_path, result)
        with self._usage_lock:
            self.total_input_tokens += result["input_tokens"]
            self.total_output_tokens += result["output_tokens"]
            self.total_cost += result["cost"]
        return result["content"]

    def generate(self, prompts, system_prompt: str | None = None, **kwargs):
        if isinstance(prompts, str):
            return [[self._generate_one(prompts, system_prompt, kwargs)]]
        prompts = list(prompts)
        if not prompts:
            return []
        results = [None] * len(prompts)
        futures = {self._executor.submit(self._generate_one, p, system_prompt, kwargs): i
                   for i, p in enumerate(prompts)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
        return [[content] for content in results]


class LLM:
    """Same shape as upstream's ``LLM``: temperature rule, truncation, single and batch calls, usage."""

    def __init__(self, model: str = "openrouter/openai/gpt-oss-120b", api_key: str | None = None,
                 api_base: str | None = None, temperature: float | None = None, max_tokens: int = 16384,
                 max_workers: int = 32):
        self.model = model
        self.is_gpt_oss = "gpt-oss" in model.lower()
        if temperature is not None:
            self.temperature = temperature
        elif "gpt-5" in model or self.is_gpt_oss:
            self.temperature = 1.0
        else:
            self.temperature = 0.0
        if api_base is not None and not self.is_gpt_oss and max_tokens > 4096:
            max_tokens = 4096
        self.max_tokens = max_tokens
        self._model_kwargs: dict[str, Any] = {"temperature": self.temperature, "max_tokens": self.max_tokens}
        self._system_prompt = "Reasoning: medium" if self.is_gpt_oss else None
        if api_base is None and max_workers > 4:
            max_workers = 4
        self._provider = ProviderLLM(model=model, max_concurrent=max_workers, api_key=api_key, api_base=api_base)
        self._usage_lock = threading.Lock()
        self.total_calls = 0

    @property
    def total_input_tokens(self):
        return self._provider.total_input_tokens

    @property
    def total_output_tokens(self):
        return self._provider.total_output_tokens

    @property
    def total_cost(self):
        return self._provider.total_cost

    def _truncate(self, prompt: str) -> str:
        if len(prompt) > MAX_PROMPT_CHARS:
            original_len = len(prompt)
            half = MAX_PROMPT_CHARS // 2
            prompt = prompt[:half] + "\n\n... [TRUNCATED] ...\n\n" + prompt[-half:]
            logger.warning("Prompt truncated: %d -> %d chars (limit %d)", original_len, len(prompt),
                           MAX_PROMPT_CHARS)
        return prompt

    def __call__(self, prompt: str) -> str:
        prompt = self._truncate(prompt)
        results = self._provider.generate(prompt, system_prompt=self._system_prompt, **self._model_kwargs)
        content = results[0][0]
        with self._usage_lock:
            self.total_calls += 1
        return content

    def get_usage(self) -> dict[str, Any]:
        with self._usage_lock:
            calls = self.total_calls
        return {"model": self.model, "calls": calls, "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "total_tokens": self.total_input_tokens + self.total_output_tokens,
                "estimated_cost_usd": round(self.total_cost, 4)}

    def batch(self, prompts: list[str]) -> list[str]:
        if not prompts:
            return []
        prompts = [self._truncate(p) for p in prompts]
        results = self._provider.generate(prompts, system_prompt=self._system_prompt, **self._model_kwargs)
        contents = [r[0] for r in results]
        with self._usage_lock:
            self.total_calls += len(prompts)
        return contents
