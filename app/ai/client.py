"""Budget-guarded Claude Haiku 5.5 calls.

Every call goes through `AIClient.call_json`, which:
1. refuses if AI is disabled, the daily call cap is hit, or month-to-date spend
   has reached the monthly budget (settings page, hard max $5);
2. trims the prompt to <= 4k input tokens (estimated);
3. asks for strict JSON via structured outputs, max_tokens 400 (800 for the playbook);
4. logs tokens and $ to cost_log - success or failure.

Thinking is switched off and effort is low: these are short judgement calls,
and thinking tokens would be billed as output and eat the 400-token cap.
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

MAX_INPUT_TOKENS = 4000
CHARS_PER_TOKEN = 3.5          # conservative estimate for English + numbers


@dataclass
class AIResult:
    ok: bool
    data: dict | None = None
    error: str | None = None
    unavailable: bool = False   # True = blocked by budget/caps/disabled (not an API error)


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


class AIClient:
    def __init__(self, db: DB):
        self.db = db
        self.client = anthropic.AsyncAnthropic(api_key=ENV.anthropic_api_key, max_retries=2,
                                               timeout=60) if ENV.has_ai else None

    # -------------------------------------------------------------- budget
    def calls_today(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return int(self.db.scalar("SELECT COUNT(*) FROM cost_log WHERE at >= ?", (day,)) or 0)

    def month_spend(self) -> float:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return float(self.db.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM cost_log WHERE at >= ?",
                                    (month,)) or 0.0)

    def budget_block(self, cfg: dict) -> str | None:
        a = cfg["ai"]
        if not self.client:
            return "ANTHROPIC_API_KEY not set"
        if not a["enabled"]:
            return "AI disabled in settings"
        if self.calls_today() >= a["max_calls_per_day"]:
            return f"daily AI call cap reached ({a['max_calls_per_day']})"
        if self.month_spend() >= a["monthly_budget_usd"]:
            return f"monthly AI budget reached (${a['monthly_budget_usd']:.2f})"
        return None

    def _log(self, purpose: str, tin: int, tout: int, ok: bool, error: str | None = None) -> float:
        cost = tin / 1e6 * ENV.ai_price_in_per_m + tout / 1e6 * ENV.ai_price_out_per_m
        self.db.execute(
            "INSERT INTO cost_log(at, purpose, model, input_tokens, output_tokens, cost_usd, ok, error) "
            "VALUES (?,?,?,?,?,?,?,?)", (iso(), purpose, ENV.ai_model, tin, tout, cost, int(ok), error))
        return cost

    # ---------------------------------------------------------------- call
    async def call_json(self, cfg: dict, purpose: str, system: str, user: str, schema: dict,
                        max_tokens: int = 400) -> AIResult:
        block = self.budget_block(cfg)
        if block:
            return AIResult(False, error=block, unavailable=True)

        budget_chars = int((MAX_INPUT_TOKENS - estimate_tokens(system) - 50) * CHARS_PER_TOKEN)
        if len(user) > budget_chars:
            user = user[:budget_chars] + "\n[...truncated to fit the 4k-token cap]"

        assert self.client is not None
        try:
            resp = await self.client.messages.create(
                model=ENV.ai_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "disabled"},
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": schema}},
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            self._log(purpose, 0, 0, False, f"{type(e).__name__}")
            return AIResult(False, error=f"{type(e).__name__}: temporary, will retry next signal")
        except anthropic.APIStatusError as e:
            self._log(purpose, 0, 0, False, f"{e.status_code} {e.message}"[:300])
            return AIResult(False, error=f"API error {e.status_code}: {e.message}"[:300])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._log(purpose, 0, 0, False, str(e)[:300])
            return AIResult(False, error=str(e)[:300])

        u = resp.usage
        tin, tout = u.input_tokens, u.output_tokens
        if resp.stop_reason == "refusal":
            self._log(purpose, tin, tout, False, "refusal")
            return AIResult(False, error="model declined")
        if resp.stop_reason == "max_tokens":
            self._log(purpose, tin, tout, False, "max_tokens")
            return AIResult(False, error="reply cut off at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self._log(purpose, tin, tout, False, "invalid JSON")
            return AIResult(False, error="invalid JSON")
        self._log(purpose, tin, tout, True)
        return AIResult(True, data=data)
