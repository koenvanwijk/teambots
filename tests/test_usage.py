"""Cost tracking: pricing math, usage parse, and API summaries. No real tokens."""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-usage-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")
os.environ.setdefault("TB_HTTP_ALLOW_PRIVATE", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import usage  # noqa: E402
from app.db import Endpoint  # noqa: E402


def test_parse_usage_openai_and_cached():
    raw = {"usage": {
        "prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 15},
    }}
    u = usage.parse_usage(raw)
    assert u == {"prompt_tokens": 100, "completion_tokens": 40, "cached_tokens": 20,
                 "cache_write_tokens": 0, "reasoning_tokens": 15}


def test_parse_usage_anthropic_names():
    raw = {"usage": {"input_tokens": 50, "output_tokens": 10,
                     "cache_read_input_tokens": 8, "cache_creation_input_tokens": 4}}
    u = usage.parse_usage(raw)
    assert u["prompt_tokens"] == 50 and u["completion_tokens"] == 10
    assert u["cached_tokens"] == 8 and u["cache_write_tokens"] == 4


def test_parse_usage_missing():
    assert usage.parse_usage({})["prompt_tokens"] == 0
    assert usage.parse_usage(None)["completion_tokens"] == 0


def test_local_endpoints_are_zero():
    spark = Endpoint(name="dgx-spark", base_url="http://192.168.86.29:8888/v1", default_model="x",
                     provider="openai", team_id=1, owner_id=1)
    mock = Endpoint(name="mock", base_url="http://127.0.0.1:18791/v1", default_model="x",
                    provider="openai", team_id=1, owner_id=1)
    cloud = Endpoint(name="ICT Copilot", base_url="https://copilot-api.ictgroup.ghe.com",
                     default_model="claude-sonnet-5", provider="github_copilot", team_id=1, owner_id=1)
    assert usage.is_local_endpoint(spark) and usage.is_local_endpoint(mock)
    assert not usage.is_local_endpoint(cloud)
    snap = usage.compute_cost(provider="openai", model="DeepSeek-V4.1-Flash-1017", endpoint=spark,
                              prompt_tokens=9000, completion_tokens=1000)
    assert snap["cost_usd"] == 0 and snap["cost_unknown"] is False and snap["billing_mode"] == "zero"


def test_token_cost_uses_cached_rate():
    row = SimpleNamespace(input_per_million=2.0, output_per_million=10.0, cached_per_million=0.2,
                          cache_write_per_million=2.5, premium_multiplier=None)
    ep = Endpoint(name="OpenAI", base_url="https://api.openai.com/v1", default_model="gpt-4.1",
                  provider="openai", team_id=1, owner_id=1)
    snap = usage.compute_cost(provider="openai", model="gpt-4.1", endpoint=ep,
                              prompt_tokens=1_000_000, cached_tokens=200_000,
                              completion_tokens=100_000, cache_write_tokens=0,
                              price_row=row, settings={"copilot_billing": "token"})
    # billed input = 800k * $2 + 200k * $0.2 + 100k * $10
    assert snap["cost_unknown"] is False
    assert abs(snap["cost_usd"] - (0.8 * 2 + 0.2 * 0.2 + 0.1 * 10)) < 1e-9


def test_unknown_model_is_not_guessed():
    ep = Endpoint(name="ICT Copilot", base_url="https://copilot-api.ictgroup.ghe.com",
                  default_model="mystery-model", provider="github_copilot", team_id=1, owner_id=1)
    snap = usage.compute_cost(provider="github_copilot", model="mystery-model", endpoint=ep,
                              prompt_tokens=10, completion_tokens=10,
                              price_row=None, settings={"copilot_billing": "token"})
    assert snap["cost_unknown"] is True and snap["cost_usd"] is None
    assert "price unknown" in snap["notes"].lower()


def test_premium_request_cost_and_followup():
    row = SimpleNamespace(input_per_million=None, output_per_million=None, cached_per_million=None,
                          cache_write_per_million=None, premium_multiplier=0.33)
    ep = Endpoint(name="ICT Copilot", base_url="https://copilot-api.ictgroup.ghe.com",
                  default_model="claude-haiku-4.5", provider="github_copilot", team_id=1, owner_id=1)
    settings = {"copilot_billing": "premium_request", "premium_request_price_usd": "0.04"}
    first = usage.compute_cost(provider="github_copilot", model="claude-haiku-4.5", endpoint=ep,
                               prompt_tokens=100, completion_tokens=20, billable_request=True,
                               price_row=row, settings=settings)
    assert first["cost_unknown"] is False
    assert abs(first["cost_usd"] - 0.33 * 0.04) < 1e-12
    assert first["premium_requests"] == 0.33
    follow = usage.compute_cost(provider="github_copilot", model="claude-haiku-4.5", endpoint=ep,
                                prompt_tokens=100, completion_tokens=20, billable_request=False,
                                price_row=row, settings=settings)
    assert follow["cost_usd"] == 0 and follow["premium_requests"] == 0


def test_premium_request_unknown_multiplier():
    ep = Endpoint(name="ICT Copilot", base_url="https://copilot-api.ictgroup.ghe.com",
                  default_model="claude-sonnet-5", provider="github_copilot", team_id=1, owner_id=1)
    snap = usage.compute_cost(provider="github_copilot", model="claude-sonnet-5", endpoint=ep,
                              billable_request=True, price_row=None,
                              settings={"copilot_billing": "premium_request",
                                        "premium_request_price_usd": "0.04"})
    assert snap["cost_unknown"] is True


def test_norm_model():
    assert usage.norm_model("Claude Sonnet 5") == "claude-sonnet-5"
    assert usage.norm_model("gpt-5.3-codex") == "gpt-5-3-codex" or usage.norm_model("gpt-5.3-codex") == "gpt-5.3-codex"
