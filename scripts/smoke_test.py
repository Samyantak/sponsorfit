"""
Manual verification script — Phase 0 exit criteria:
Makes ONE real LLM call, then queries SQLite for its exact cost.

Run with: uv run python scripts/smoke_test.py
"""

from dotenv import load_dotenv

load_dotenv()

from sponsorfit import costs
from sponsorfit.llm import MeteredLLMClient


def main():
    client = MeteredLLMClient(run_id="smoke-test")
    result = client.call(
        provider="ollama",
        model="qwen3:8b",
        messages=[{"role": "user", "content": "Reply with exactly one word: pong"}],
        max_tokens=16,
        agent="smoke_test",
    )
    print(f"Response text : {result.text}")
    print(f"Call ID       : {result.call_id}")

    conn = costs.get_connection()
    row = costs.get_call(conn, result.call_id)
    print("SQLite row for this call:")
    for key in row.keys():
        print(f"  {key}: {row[key]}")


if __name__ == "__main__":
    main()
