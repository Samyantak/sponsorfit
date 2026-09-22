"""Hardcoded per-model USD pricing. Update this table when providers change
prices — nothing else in the codebase should need to change."""

# USD per 1,000,000 tokens.
PRICING = {
    "anthropic": {
        "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
        "claude-sonnet-5": {"input": 2.00, "output": 10.00},
        "claude-opus-5": {"input": 5.00, "output": 25.00},
    },
    "openai": {
        # TODO verify current prices at https://platform.openai.com/docs/pricing
        # before the first real OpenAI call — not independently verified here.
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 2.50, "output": 10.00},
    },
}


def compute_cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    if provider == "ollama":
        return 0.0  # runs locally — no per-token API cost, regardless of model
    rates = PRICING[provider][model]  # KeyError on unknown model — fail loud, never log $0
    return (tokens_in / 1_000_000) * rates["input"] + (tokens_out / 1_000_000) * rates["output"]
