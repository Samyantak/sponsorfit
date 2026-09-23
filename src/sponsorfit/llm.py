"""The one call-through surface for LLM calls. Every call — from any
agent, any script — goes through MeteredLLMClient.call() (or the call_llm()
convenience wrapper), so cost tracking and tracing can never be
accidentally bypassed. No retry/backoff here; that's Phase 3's job."""

import os
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from sponsorfit import costs, pricing, tracing

Provider = Literal["anthropic", "openai", "ollama"]


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    call_id: str
    raw: object


class MeteredLLMClient:
    def __init__(
        self,
        run_id: str | None = None,
        db_path: str | None = None,
        traces_dir: str | None = None,
    ):
        self.run_id = run_id
        self._db_conn = costs.get_connection(db_path)
        self._tracer = tracing.RunTracer(run_id=run_id, traces_dir=traces_dir)
        self._anthropic_client = None
        self._openai_client = None

    def call(
        self,
        *,
        provider: Provider,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 1024,
        prompt_version: str | None = None,
        agent: str = "manual",
        json_schema: dict | None = None,
        ollama_options: dict | None = None,
    ) -> LLMResponse:
        """json_schema / ollama_options are Ollama-only for now (structured
        output via Ollama's `format`, and options like num_ctx/temperature).
        Passing them for a cloud provider fails loudly rather than being
        silently ignored."""
        if provider != "ollama" and (json_schema or ollama_options):
            raise NotImplementedError(f"json_schema/ollama_options not supported for provider={provider}")
        call_id = uuid.uuid4().hex
        start = time.monotonic()
        try:
            if provider == "anthropic":
                text, tokens_in, tokens_out, raw = self._call_anthropic(
                    model, messages, system, max_tokens
                )
            elif provider == "openai":
                text, tokens_in, tokens_out, raw = self._call_openai(
                    model, messages, system, max_tokens
                )
            elif provider == "ollama":
                text, tokens_in, tokens_out, raw = self._call_ollama(
                    model, messages, system, max_tokens, json_schema, ollama_options
                )
            else:
                raise ValueError(f"unknown provider: {provider}")
        except Exception as e:
            latency_ms = int((time.monotonic() - start) * 1000)
            costs.record_call(
                self._db_conn,
                call_id=call_id,
                run_id=self.run_id,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_ms=latency_ms,
                status="error",
                error_message=str(e),
            )
            self._tracer.log(
                "llm_call_error",
                {"call_id": call_id, "provider": provider, "model": model, "error": str(e)},
                agent=agent,
            )
            raise

        latency_ms = int((time.monotonic() - start) * 1000)
        cost_usd = pricing.compute_cost_usd(provider, model, tokens_in, tokens_out)

        costs.record_call(
            self._db_conn,
            call_id=call_id,
            run_id=self.run_id,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        self._tracer.log(
            "llm_call",
            {
                "call_id": call_id,
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
            },
            agent=agent,
        )

        return LLMResponse(
            text=text,
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            call_id=call_id,
            raw=raw,
        )

    def _call_anthropic(self, model, messages, system, max_tokens):
        if self._anthropic_client is None:
            import anthropic

            self._anthropic_client = anthropic.Anthropic()
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            kwargs["system"] = system  # SDK rejects an explicit system=None; omit instead
        resp = self._anthropic_client.messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if block.type == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens, resp

    def _call_openai(self, model, messages, system, max_tokens):
        if self._openai_client is None:
            import openai

            self._openai_client = openai.OpenAI()
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        resp = self._openai_client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=full_messages
        )
        text = resp.choices[0].message.content
        return text, resp.usage.prompt_tokens, resp.usage.completion_tokens, resp

    def _call_ollama(self, model, messages, system, max_tokens, json_schema=None, ollama_options=None):
        import json
        import urllib.request

        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        payload = {
            "model": model,
            "messages": full_messages,
            "stream": False,
            "think": False,  # hybrid thinking models (e.g. qwen3) otherwise burn
            # max_tokens on hidden reasoning and can return empty `content`.
            # Reasoning belongs in explicit structured-output fields (Phase 3+),
            # not a hidden chain-of-thought.
            "options": {"num_predict": max_tokens, **(ollama_options or {})},
        }
        if json_schema:
            payload["format"] = json_schema  # Ollama constrains decoding to this schema
        req = urllib.request.Request(
            f"{base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read())
        text = data["message"]["content"]
        tokens_in = data.get("prompt_eval_count", 0)
        tokens_out = data.get("eval_count", 0)
        return text, tokens_in, tokens_out, data


def call_llm(provider: Provider | None = None, model: str | None = None, **kwargs) -> LLMResponse:
    """Module-level convenience wrapper for simple/ad-hoc scripts (no run_id tracking)."""
    provider = provider or os.environ["SPONSORFIT_DEFAULT_PROVIDER"]
    model = model or os.environ["SPONSORFIT_DEFAULT_MODEL"]
    return MeteredLLMClient().call(provider=provider, model=model, **kwargs)
