import json
from unittest.mock import MagicMock, patch

from sponsorfit import costs, pricing
from sponsorfit.llm import MeteredLLMClient


def _fake_anthropic_response():
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text="pong")]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 3
    return resp


def test_metered_call_records_cost(tmp_path):
    db_path = str(tmp_path / "costs.db")
    traces_dir = str(tmp_path / "traces")

    with patch("anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.return_value = _fake_anthropic_response()

        client = MeteredLLMClient(run_id="test-run", db_path=db_path, traces_dir=traces_dir)
        result = client.call(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "ping"}],
            prompt_version="placement_v1",
        )

    assert result.text == "pong"
    assert result.tokens_in == 10
    assert result.tokens_out == 3
    expected_cost = pricing.compute_cost_usd("anthropic", "claude-haiku-4-5-20251001", 10, 3)
    assert abs(result.cost_usd - expected_cost) < 1e-9

    conn = costs.get_connection(db_path)
    row = costs.get_call(conn, result.call_id)
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["tokens_in"] == 10 and row["tokens_out"] == 3
    assert abs(row["cost_usd"] - expected_cost) < 1e-9
    assert row["prompt_version"] == "placement_v1"

    trace_file = tmp_path / "traces" / "test-run.jsonl"
    lines = trace_file.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    matching = [e for e in events if e["payload"].get("call_id") == result.call_id]
    assert len(matching) == 1
    assert matching[0]["event_type"] == "llm_call"


def test_metered_call_ollama_is_free(tmp_path):
    db_path = str(tmp_path / "costs.db")
    traces_dir = str(tmp_path / "traces")

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(
        {"message": {"content": "pong"}, "prompt_eval_count": 8, "eval_count": 2}
    ).encode("utf-8")
    fake_response.__enter__.return_value = fake_response
    fake_response.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=fake_response):
        client = MeteredLLMClient(run_id="test-run-ollama", db_path=db_path, traces_dir=traces_dir)
        result = client.call(
            provider="ollama",
            model="qwen3:8b",
            messages=[{"role": "user", "content": "ping"}],
        )

    assert result.text == "pong"
    assert result.tokens_in == 8
    assert result.tokens_out == 2
    assert result.cost_usd == 0.0

    conn = costs.get_connection(db_path)
    row = costs.get_call(conn, result.call_id)
    assert row["provider"] == "ollama"
    assert row["cost_usd"] == 0.0


def test_ollama_structured_output_and_options_reach_the_request(tmp_path):
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(
        {"message": {"content": '{"sponsors": []}'}, "prompt_eval_count": 5, "eval_count": 4}
    ).encode("utf-8")
    fake_response.__enter__.return_value = fake_response
    fake_response.__exit__.return_value = False
    schema = {"type": "object", "properties": {"sponsors": {"type": "array"}}}

    with patch("urllib.request.urlopen", return_value=fake_response) as urlopen:
        client = MeteredLLMClient(db_path=str(tmp_path / "c.db"), traces_dir=str(tmp_path / "t"))
        client.call(
            provider="ollama",
            model="qwen3:8b",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=100,
            json_schema=schema,
            ollama_options={"num_ctx": 16384, "temperature": 0},
        )

    sent = json.loads(urlopen.call_args[0][0].data)
    assert sent["format"] == schema
    assert sent["options"] == {"num_predict": 100, "num_ctx": 16384, "temperature": 0}


def test_ollama_only_params_fail_loudly_for_cloud_providers(tmp_path):
    import pytest

    client = MeteredLLMClient(db_path=str(tmp_path / "c.db"), traces_dir=str(tmp_path / "t"))
    with pytest.raises(NotImplementedError):
        client.call(provider="anthropic", model="claude-haiku-4-5-20251001", messages=[], json_schema={"type": "object"})
