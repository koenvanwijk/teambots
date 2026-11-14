# LLM request router (design)

Status: proposal only. Cost tracking is in place; this router is not implemented yet.
Logged usage (`llm_usage`: model, tokens, cost, origin, success, latency) is the input for later
tuning.

The job is to choose, per request, between local Spark (`DeepSeek-V4.1-Flash-1017`, free), cheaper
Copilot models (Haiku, Luna, nano, …) and expensive Copilot models (Sonnet, Opus, Astra, …),
then fall back on errors or timeouts — without sending private data off the laptop unless the
person allows it.

## Constraints

- **Privacy.** Private project data, secrets and Overview chats stay on Spark unless a rule
  (or a later explicit “allow cloud for this bot”) says otherwise.
- **Cost.** Prefer a cheaper model that can still do the job. Use the price table and month-to-date
  budget from cost tracking; never invent prices.
- **Quality.** Tool use, long context and hard reasoning may need a stronger model.
- **Resilience.** On 4xx/5xx, timeout or empty reply, fall back along a list rather than fail the
  turn immediately.

## Option A — Rules only

A short, ordered policy, evaluated before each `llm.chat`:

1. If the bot, conversation or project is marked **local-only**, or the prompt/context is tagged
   private: Spark. No cloud fallback unless the error is “Spark is down” *and* the bot allows it.
2. If the project budget is at or above 80%: stay on Spark, or on the cheapest allowed Copilot
   model if Spark is down (and the bot is not local-only). At 100% with `budget_block`, do not
   call paid models.
3. Otherwise pick by **task signals** already visible without a classifier:
   - connection test, cheap classification (`_classify`), activity-feed summary: cheapest capable
     model (Spark, or Haiku / Luna / nano on Copilot).
   - tool-heavy or computer-use turns, or context above a token threshold (e.g. 32k): mid-tier
     (Sonnet 5 / 5.5, gpt-6-sol).
   - explicit “think hard”, long design, or repeated Spark failures on the same turn: expensive
     (Opus, Astra) only if budget allows.
4. Fallback list, e.g. Spark → cheap Copilot → mid Copilot. Stop on success. Record the chosen
   model and why in `llm_usage.notes`.

Effort: small. Deterministic and easy to explain in the UI. Risk: a mid/hard task sent to Spark
or Haiku may come back weak, and a simple task may still hit Sonnet if the signals are crude.

## Option B — Rules plus a small classifier

Keep the privacy and budget rules from A as hard gates. For everything else, add a cheap
classifier step (Spark, or a tiny Copilot model) that labels the request
`local | cheap | mid | expensive` from the user text, tool need, and context length. The router
then binds that label to a concrete model list.

The classifier itself is logged as `origin=route` (or the parent origin) so its cost is visible.
Default it **off** until Spark quality on this label task is verified. Thresholds and model
lists stay editable.

Effort: medium. Better quality/cost trade-off once usage data shows where A mis-routes. Risk: an
extra LLM call on every turn (mitigate: skip for short follow-ups and tool rounds; cache). A bad
classifier can send private or expensive work to the wrong place — privacy rules must stay
non-overridable.

## Option C — Always Spark, escalate on failure

Every turn starts on Spark. Escalate to a Copilot mid-tier only after a documented failure
(timeout, HTTP 5xx, empty/truncated visible answer, or a user “try again with a stronger model”).
No automatic jump to Opus/Astra.

Effort: smallest. Cheapest and most private. Risk: slower turns when Spark is a poor fit, and
more user intervention. Fine as a default for Overview if privacy is the priority; weak as the
only policy for ICT Copilot-heavy work.

## Recommendation

**Ship A first**, with C as the default policy for Overview and any bot marked local-only, and A
for project bots that may use ICT Copilot. Wire fallbacks and budget/privacy gates before any
classifier. After a few weeks of `llm_usage` (cost per origin, error rates, retries, model
distribution), add B as an opt-in per bot: the classifier only chooses *inside* the allowed
set, never around the privacy gate.

Do not auto-switch existing bots (Overview stays on Spark) when the router lands; each bot gets
an explicit routing policy (`local`, `rules`, later `rules+classifier`).
