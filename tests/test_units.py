"""Pure unit tests (no Redis/Postgres needed): pricing, config, auth helpers, mock."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.auth import hash_key, parse_bearer
from gateway.config import ModelSpec, load_models
from gateway.credits import cost_of, estimate_prompt_tokens, worst_case_cost
from mock_backend.server import ChatMessage, pick_output, tokenize_output

SPEC_CHEAP = ModelSpec("a", {"model": "openai/a"}, price_in_per_1k=100, price_out_per_1k=200)
SPEC_DEAR = ModelSpec("b", {"model": "openai/b"}, price_in_per_1k=500, price_out_per_1k=1500)


def test_cost_rounds_up_to_whole_millicredits():
    assert cost_of(SPEC_CHEAP, 1000, 1000) == 300
    assert cost_of(SPEC_CHEAP, 1, 0) == 1  # 0.1 rounds up, never down
    assert cost_of(SPEC_CHEAP, 0, 0) == 0


def test_worst_case_uses_most_expensive_model_in_chain():
    assert worst_case_cost([SPEC_CHEAP, SPEC_DEAR], 100, 100) == cost_of(SPEC_DEAR, 100, 100)


def test_prompt_estimate_is_upper_bound_on_chars():
    msgs = [{"role": "user", "content": "hello world"}]
    assert estimate_prompt_tokens(msgs) >= len("hello world")


def test_parse_bearer():
    assert parse_bearer("Bearer abc") == "abc"
    assert parse_bearer("bearer  abc ") == "abc"
    assert parse_bearer("Basic abc") is None
    assert parse_bearer(None) is None


def test_hash_key_is_stable_sha256():
    assert hash_key("x") == hash_key("x")
    assert len(hash_key("x")) == 64


def test_model_config_expands_env_and_validates(tmp_path: Path):
    cfg = tmp_path / "m.yaml"
    cfg.write_text(
        "models:\n"
        "  - alias: a\n"
        "    litellm_params: {model: openai/a, api_base: '${BASE}'}\n"
        "    price_in_per_1k: 1\n"
        "    price_out_per_1k: 1\n"
        "    fallbacks: [missing]\n"
    )
    with pytest.raises(ValueError):
        load_models(cfg, env={"BASE": "http://x"})
    with pytest.raises(KeyError):
        load_models(cfg, env={})


def test_mock_tokenizer_roundtrips_and_honors_max_tokens():
    text = pick_output([ChatMessage(role="system", content="[task:order_extraction]")])
    assert "".join(tokenize_output(text, None)) == text
    assert len(tokenize_output(text, 3)) == 3
