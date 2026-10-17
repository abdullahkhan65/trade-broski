"""Budget-guarded Claude calls on two tiers.

* fast   = Claude Haiku 5.5 ($0.10 / $0.50 per M tokens): every trade gate, daily reviews, news digest.
* strong = Claude Sonnet 5.5 ($2 / $10 per M tokens, 20x Haiku): only where judgement matters
  most - a second opinion on high-stakes trades Haiku approved, and the weekly playbook rewrite.

Every call goes through `AIClient.call_json`, which:
1. refuses if AI is disabled, the daily call cap is hit, or month-to-date spend has
   reached the TOTAL monthly budget (hard max $8); strong calls also have their own
   daily cap and monthly sub-budget (hard max $5);
2. trims the prompt to the tier's input cap (estimated);
3. asks for strict JSON via structured outputs;
4. logs tokens and $ to cost_log (with the model) - success or failure.

Haiku runs with thinking off and low effort: short judgement calls. Sonnet runs with
adaptive thinking at the effort the caller asks for; max_tokens must leave room for it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from ..config import ENV
from ..db import DB, iso

log = logging.getLogger("ai")

MAX_INPUT_TOKENS = {"fast": 4000, "strong": 6000}
CHARS_PER_TOKEN = 3.5          # conservative estimate for English + numbers


@dataclass
class AIResult:
    ok: bool
    data: dict | None = None
    error: str | None = None
    unavailable: bool = False   # True = blocked by budget/caps/disabled (not an API error)


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def model_for(tier: str) -> str:
    return ENV.ai_strong_model if tier == "strong" else ENV.ai_model


def _prices(tier: str) -> tuple[float, float]:
    if tier == "strong":
        return ENV.ai_strong_price_in_per_m, ENV.ai_strong_price_out_per_m
    return ENV.ai_price_in_per_m, ENV.ai_price_out_per_m


class AIClient:
    def __init__(self, db: DB):
        self.db = db
        self.client = anthropic.AsyncAnthropic(api_key=ENV.anthropic_api_key, max_retries=2,
                                               timeout=90) if ENV.has_ai else None

    # -------------------------------------------------------------- budget
    @staticmethod
    def _day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def calls_today(self, tier: str | None = None) -> int:
        if tier:
            return int(self.db.scalar("SELECT COUNT(*) FROM cost_log WHERE at >= ? AND model=?",
                                      (self._day(), model_for(tier))) or 0)
        return int(self.db.scalar("SELECT COUNT(*) FROM cost_log WHERE at >= ?", (self._day(),)) or 0)

    def month_spend(self, tier: str | None = None) -> float:
        if tier:
            return float(self.db.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM cost_log WHERE at >= ? AND model=?",
                                        (self._month(), model_for(tier))) or 0.0)
        return float(self.db.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM cost_log WHERE at >= ?",
                                    (self._month(),)) or 0.0)

    def budget_block(self, cfg: dict, tier: str = "fast") -> str | None:
        a = cfg["ai"]
        if not self.client:
            return "ANTHROPIC_API_KEY not set"
        if not a["enabled"]:
            return "AI disabled in settings"
        if self.calls_today() >= a["max_calls_per_day"]:
            return f"daily AI call cap reached ({a['max_calls_per_day']})"
        if self.month_spend() >= a["monthly_budget_usd"]:
            return f"monthly AI budget reached (${a['monthly_budget_usd']:.2f})"
        if tier == "strong":
            if not a.get("strong_enabled", True):
                return "Sonnet disabled in settings"
            if self.calls_today("strong") >= a["strong_calls_per_day"]:
                return f"daily Sonnet call cap reached ({a['strong_calls_per_day']})"
            if self.month_spend("strong") >= a["strong_budget_usd"]:
                return f"monthly Sonnet budget reached (${a['strong_budget_usd']:.2f})"
        return None

    def _log(self, tier: str, purpose: str, tin: int, tout: int, ok: bool, error: str | None = None) -> float:
        p_in, p_out = _prices(tier)
        cost = tin / 1e6 * p_in + tout / 1e6 * p_out
        self.db.execute(
            "INSERT INTO cost_log(at, purpose, model, input_tokens, output_tokens, cost_usd, ok, error) "
            "VALUES (?,?,?,?,?,?,?,?)", (iso(), purpose, model_for(tier), tin, tout, cost, int(ok), error))
        return cost

    # ---------------------------------------------------------------- call
    async def call_json(self, cfg: dict, purpose: str, system: str, user: str, schema: dict,
                        max_tokens: int = 400, tier: str = "fast", effort: str = "low") -> AIResult:
        block = self.budget_block(cfg, tier)
        if block:
            return AIResult(False, error=block, unavailable=True)

        budget_chars = int((MAX_INPUT_TOKENS[tier] - estimate_tokens(system) - 50) * CHARS_PER_TOKEN)
        if len(user) > budget_chars:
            user = user[:budget_chars] + "\n[...truncated to fit the input cap]"

        # Haiku: no thinking (short calls, cheap). Sonnet: adaptive thinking - the reason we pay for it.
        thinking = {"type": "adaptive"} if tier == "strong" else {"type": "disabled"}
        assert self.client is not None
        try:
            resp = await self.client.messages.create(
                model=model_for(tier),
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking=thinking,
                output_config={"effort": effort,
                               "format": {"type": "json_schema", "schema": schema}},
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            self._log(tier, purpose, 0, 0, False, f"{type(e).__name__}")
            return AIResult(False, error=f"{type(e).__name__}: temporary, will retry next signal")
        except anthropic.APIStatusError as e:
            self._log(tier, purpose, 0, 0, False, f"{e.status_code} {e.message}"[:300])
            return AIResult(False, error=f"API error {e.status_code}: {e.message}"[:300])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._log(tier, purpose, 0, 0, False, str(e)[:300])
            return AIResult(False, error=str(e)[:300])

        u = resp.usage
        tin, tout = u.input_tokens, u.output_tokens
        if resp.stop_reason == "refusal":
            self._log(tier, purpose, tin, tout, False, "refusal")
            return AIResult(False, error="model declined")
        if resp.stop_reason == "max_tokens":
            self._log(tier, purpose, tin, tout, False, "max_tokens")
            return AIResult(False, error="reply cut off at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self._log(tier, purpose, tin, tout, False, "invalid JSON")
            return AIResult(False, error="invalid JSON")
        self._log(tier, purpose, tin, tout, True)
        return AIResult(True, data=data)
