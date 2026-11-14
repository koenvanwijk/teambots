"""Official model prices seeded into the editable price table.

Every row has a source URL and the date it was read (2026-11-14). Amounts are
USD per 1 million tokens unless billing_mode is premium_request. Nothing here
is a guess: models that do not appear on an official page stay unpriced.
"""
from __future__ import annotations

# Dates are the day these pages were retrieved.
OPENAI_SRC = "https://developers.openai.com/api/docs/pricing"
OPENAI_DATE = "2026-11-14"
ANTHROPIC_SRC = "https://docs.anthropic.com/en/docs/about-claude/pricing"
ANTHROPIC_DATE = "2026-11-14"
COPILOT_TOKEN_SRC = "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing"
COPILOT_TOKEN_DATE = "2026-11-14"
COPILOT_PR_SRC = "https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/copilot-requests"
COPILOT_PR_DATE = "2026-11-14"
COPILOT_MULT_SRC = "https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/model-multipliers-for-annual-plans"
COPILOT_MULT_DATE = "2026-11-14"

# Additional premium requests on legacy annual Copilot Pro/Pro+ plans.
PREMIUM_REQUEST_PRICE_USD = 0.04

# Official short-context / default-tier token prices. Long-context tiers are
# noted in `notes` but not auto-applied (that would require guessing the tier).
SEED: list[dict] = []


def _row(provider, model, inp, out, cached=None, cache_write=None, *, source, dated,
         notes="", aliases=(), billing_mode="token", premium_multiplier=None):
    return dict(
        provider=provider, model=model, billing_mode=billing_mode,
        currency="USD",
        input_per_million=inp, output_per_million=out,
        cached_per_million=cached, cache_write_per_million=cache_write,
        premium_multiplier=premium_multiplier,
        source_url=source, source_date=dated, notes=notes,
        aliases=list(aliases),
    )


# --- OpenAI API (standard, short context ≤272K where the page splits tiers)
_OAI = dict(source=OPENAI_SRC, dated=OPENAI_DATE)
SEED += [
    _row("openai", "gpt-6-astra", 10.00, 50.00, 1.00, 12.50, **_OAI,
         notes="Standard tier, short context (≤272K input). Long context is priced higher on the same page.",
         aliases=("gpt-6-astra", "GPT-6 Astra")),
    _row("openai", "gpt-6.1-sol", 2.00, 10.00, 0.10, 2.50, **_OAI,
         notes="Standard tier, short context (≤272K input)."),
    _row("openai", "gpt-6.1-terra", 1.00, 5.00, 0.05, 1.25, **_OAI,
         notes="Standard tier, short context (≤272K input)."),
    _row("openai", "gpt-6-luna", 0.10, 0.50, 0.01, 0.125, **_OAI,
         notes="Standard tier, short context (≤272K input)."),
    _row("openai", "gpt-6-sol", 2.00, 10.00, 0.20, 2.50, **_OAI,
         notes="Standard tier, short context (≤272K input)."),
    _row("openai", "gpt-5.6-sol", 4.00, 20.00, 0.40, 5.00, **_OAI,
         notes="Standard tier, short context (≤272K input). Promotional pricing at least through 2026-11-21."),
    _row("openai", "gpt-5.6-terra", 2.00, 12.00, 0.20, 2.50, **_OAI,
         notes="Standard tier, short context (≤272K input)."),
    _row("openai", "gpt-5.6-luna", 0.20, 1.20, 0.02, 0.25, **_OAI,
         notes="Standard tier, short context (≤272K input)."),
    _row("openai", "gpt-5.5", 5.00, 30.00, 0.50, None, **_OAI,
         notes="Standard, short context (<272K)."),
    _row("openai", "gpt-5.4", 2.50, 15.00, 0.25, None, **_OAI,
         notes="Standard, short context (<272K)."),
    _row("openai", "gpt-5.4-mini", 0.75, 4.50, 0.075, None, **_OAI),
    _row("openai", "gpt-5.4-nano", 0.20, 1.25, 0.02, None, **_OAI),
    _row("openai", "gpt-5.2", 1.75, 14.00, 0.175, None, **_OAI),
    _row("openai", "gpt-5.1", 1.25, 10.00, 0.125, None, **_OAI),
    _row("openai", "gpt-5", 1.25, 10.00, 0.125, None, **_OAI),
    _row("openai", "gpt-5-mini", 0.25, 2.00, 0.025, None, **_OAI),
    _row("openai", "gpt-5-nano", 0.05, 0.40, 0.005, None, **_OAI),
    _row("openai", "gpt-4.1", 2.00, 8.00, 0.50, None, **_OAI),
    _row("openai", "gpt-4.1-mini", 0.40, 1.60, 0.10, None, **_OAI),
    _row("openai", "gpt-4o", 2.50, 10.00, 1.25, None, **_OAI),
    _row("openai", "gpt-4o-mini", 0.15, 0.60, 0.075, None, **_OAI),
    _row("openai", "o3", 2.00, 8.00, 0.50, None, **_OAI),
]

# --- Anthropic API (5-minute cache write column)
_AN = dict(source=ANTHROPIC_SRC, dated=ANTHROPIC_DATE)
SEED += [
    _row("anthropic", "claude-fable-5.2", 8.00, 40.00, 0.20, 10.00, **_AN,
         aliases=("Claude Fable 5.2",)),
    _row("anthropic", "claude-fable-5.1", 10.00, 50.00, 0.25, 12.50, **_AN,
         aliases=("Claude Fable 5.1",)),
    _row("anthropic", "claude-fable-5", 10.00, 50.00, 1.00, 12.50, **_AN,
         aliases=("Claude Fable 5",)),
    _row("anthropic", "claude-opus-5.5", 4.00, 20.00, 0.20, 5.00, **_AN,
         aliases=("Claude Opus 5.5", "claude-opus-5.5")),
    _row("anthropic", "claude-opus-5", 5.00, 25.00, 0.50, 6.25, **_AN,
         aliases=("Claude Opus 5",)),
    _row("anthropic", "claude-opus-4.8", 5.00, 25.00, 0.50, 6.25, **_AN,
         aliases=("Claude Opus 4.8",)),
    _row("anthropic", "claude-opus-4.7", 5.00, 25.00, 0.50, 6.25, **_AN),
    _row("anthropic", "claude-opus-4.6", 5.00, 25.00, 0.50, 6.25, **_AN),
    _row("anthropic", "claude-opus-4.5", 5.00, 25.00, 0.50, 6.25, **_AN),
    _row("anthropic", "claude-sonnet-5.5", 2.00, 10.00, 0.10, 2.50, **_AN,
         aliases=("Claude Sonnet 5.5",)),
    _row("anthropic", "claude-sonnet-5", 2.00, 10.00, 0.20, 2.50, **_AN,
         notes="The $2/$10 per million input/output price is the standard price (the planned rise on 2026-09-01 did not happen).",
         aliases=("Claude Sonnet 5",)),
    _row("anthropic", "claude-sonnet-4.6", 3.00, 15.00, 0.30, 3.75, **_AN,
         aliases=("Claude Sonnet 4.6",)),
    _row("anthropic", "claude-sonnet-4.5", 3.00, 15.00, 0.30, 3.75, **_AN,
         aliases=("Claude Sonnet 4.5",)),
    _row("anthropic", "claude-haiku-5.5", 0.10, 0.50, 0.01, 0.125, **_AN,
         notes="Price for prompts up to 100,000 tokens. Over 100K: $0.50 / $2.50 / $0.05 cached / $0.625 cache write.",
         aliases=("Claude Haiku 5.5",)),
    _row("anthropic", "claude-haiku-4.5", 1.00, 5.00, 0.10, 1.25, **_AN,
         aliases=("Claude Haiku 4.5",)),
]

# --- GitHub Copilot current (usage-based) token prices, default/short tier
_CP = dict(source=COPILOT_TOKEN_SRC, dated=COPILOT_TOKEN_DATE)
SEED += [
    _row("github_copilot", "gpt-5.3-codex", 1.75, 14.00, 0.175, None, **_CP,
         aliases=("GPT-5.3-Codex", "gpt-5.3-codex")),
    _row("github_copilot", "gpt-5.4-nano", 0.20, 1.25, 0.02, None, **_CP,
         aliases=("GPT-5.4 nano",)),
    _row("github_copilot", "gpt-5.6-luna", 0.20, 1.20, 0.02, 0.25, **_CP,
         notes="Default tier, ≤200K input. Long context (>200K) is priced higher on the same page."),
    _row("github_copilot", "gpt-5.6-sol", 4.00, 20.00, 0.40, 5.00, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "gpt-5.6-terra", 2.00, 12.00, 0.20, 2.50, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "gpt-6-astra", 10.00, 50.00, 1.00, 12.50, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "gpt-6-luna", 0.10, 0.50, 0.01, 0.125, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "gpt-6-sol", 2.00, 10.00, 0.20, 2.50, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "gpt-6.1-sol", 2.00, 10.00, 0.10, 2.50, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "gpt-6.1-terra", 1.00, 5.00, 0.05, 1.25, **_CP,
         notes="Default tier, ≤272K input."),
    _row("github_copilot", "claude-haiku-4.5", 1.00, 5.00, 0.10, 1.25, **_CP,
         aliases=("Claude Haiku 4.5",)),
    _row("github_copilot", "claude-haiku-5.5", 0.10, 0.50, 0.01, 0.125, **_CP,
         notes="Default tier, ≤100K input. Long context (>100K) is priced higher on the same page.",
         aliases=("Claude Haiku 5.5",)),
    _row("github_copilot", "claude-sonnet-4", 3.00, 15.00, 0.30, 3.75, **_CP,
         aliases=("Claude Sonnet 4",)),
    _row("github_copilot", "claude-sonnet-4.6", 3.00, 15.00, 0.30, 3.75, **_CP,
         aliases=("Claude Sonnet 4.6",)),
    _row("github_copilot", "claude-opus-4.8", 5.00, 25.00, 0.50, 6.25, **_CP,
         aliases=("Claude Opus 4.8",)),
    _row("github_copilot", "claude-opus-5", 5.00, 25.00, 0.50, 6.25, **_CP,
         aliases=("Claude Opus 5",)),
    _row("github_copilot", "claude-opus-5.5", 4.00, 20.00, 0.20, 5.00, **_CP,
         aliases=("Claude Opus 5.5",)),
    _row("github_copilot", "claude-sonnet-5", 2.00, 10.00, 0.20, 2.50, **_CP,
         aliases=("Claude Sonnet 5",)),
    _row("github_copilot", "claude-sonnet-5.5", 2.00, 10.00, 0.10, 2.50, **_CP,
         aliases=("Claude Sonnet 5.5",)),
    _row("github_copilot", "claude-fable-5", 10.00, 50.00, 1.00, 12.50, **_CP,
         aliases=("Claude Fable 5",)),
    _row("github_copilot", "claude-fable-5.1", 10.00, 50.00, 0.25, 12.50, **_CP,
         aliases=("Claude Fable 5.1",)),
    _row("github_copilot", "claude-fable-5.2", 8.00, 40.00, 0.20, 10.00, **_CP,
         aliases=("Claude Fable 5.2",)),
    _row("github_copilot", "gemini-3.8-flash", 0.75, 3.75, 0.075, None, **_CP,
         notes="Promotional pricing through 2026-12-31.",
         aliases=("Gemini 3.8 Flash",)),
    _row("github_copilot", "gemini-4-argon", 2.00, 10.00, 0.10, None, **_CP,
         notes="Promotional pricing until 2026-12-01.",
         aliases=("Gemini 4 Argon", "gemini-4-argon")),
    _row("github_copilot", "gemini-4-carbon", 2.00, 10.00, 0.10, None, **_CP,
         notes="Promotional pricing until 2026-12-01. From 2026-12-01: $4 / $20 / $0.20 cached.",
         aliases=("Gemini 4 Carbon",)),
    _row("github_copilot", "gemini-4-hydrogen", 1.50, 7.50, 0.075, None, **_CP,
         aliases=("Gemini 4 Hydrogen",)),
    _row("github_copilot", "mai-code-1.1-flash", 0.20, 1.20, 0.02, None, **_CP,
         aliases=("MAI-Code-1.1-Flash",)),
    _row("github_copilot", "grok-4.5", 2.00, 6.00, 0.50, None, **_CP,
         notes="Default tier, ≤200K input."),
    _row("github_copilot", "grok-4.6", 2.00, 6.00, 0.50, None, **_CP,
         notes="Default tier, ≤200K input."),
    _row("github_copilot", "grok-4.7", 2.00, 6.00, 0.50, None, **_CP,
         notes="Default tier, ≤200K input."),
    _row("github_copilot", "kimi-k3", 3.00, 15.00, 0.30, None, **_CP,
         aliases=("Kimi K3",)),
]

# --- Legacy Copilot premium-request multipliers (annual Pro/Pro+ only).
# Official table as of 2026-11-14 lists only these models. Others stay unknown
# in premium_request mode. Price per premium request is $0.04 USD.
_PR = dict(source=COPILOT_MULT_SRC, dated=COPILOT_MULT_DATE, billing_mode="premium_request")
SEED += [
    _row("github_copilot", "claude-haiku-4.5", None, None, billing_mode="premium_request",
         premium_multiplier=0.33, source=COPILOT_MULT_SRC, dated=COPILOT_MULT_DATE,
         notes="Legacy annual Copilot Pro/Pro+ only (after 2026-06-01). Additional premium requests: $0.04 USD.",
         aliases=("Claude Haiku 4.5",)),
    _row("github_copilot", "claude-opus-4.8", None, None, billing_mode="premium_request",
         premium_multiplier=27, source=COPILOT_MULT_SRC, dated=COPILOT_MULT_DATE,
         notes="Legacy annual Copilot Pro/Pro+ only (after 2026-06-01). Additional premium requests: $0.04 USD.",
         aliases=("Claude Opus 4.8",)),
    _row("github_copilot", "claude-sonnet-4.6", None, None, billing_mode="premium_request",
         premium_multiplier=9, source=COPILOT_MULT_SRC, dated=COPILOT_MULT_DATE,
         notes="Legacy annual Copilot Pro/Pro+ only (after 2026-06-01). Additional premium requests: $0.04 USD.",
         aliases=("Claude Sonnet 4.6",)),
    _row("github_copilot", "gpt-5.3-codex", None, None, billing_mode="premium_request",
         premium_multiplier=6, source=COPILOT_MULT_SRC, dated=COPILOT_MULT_DATE,
         notes="Legacy annual Copilot Pro/Pro+ only (after 2026-06-01). Additional premium requests: $0.04 USD.",
         aliases=("GPT-5.3-Codex",)),
    _row("github_copilot", "mai-code-1.1-flash", None, None, billing_mode="premium_request",
         premium_multiplier=0.25, source=COPILOT_MULT_SRC, dated=COPILOT_MULT_DATE,
         notes="Legacy annual Copilot Pro/Pro+ only (after 2026-06-01). Additional premium requests: $0.04 USD.",
         aliases=("MAI-Code-1.1-Flash",)),
]
