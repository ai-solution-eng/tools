"""Off-peak billing at the PROXY boundary (audit r5 #8).

The streamed chat path settles through rp._StreamLedger.finish(); every other
settle site in the tree (and the X-PCAI-Billing header for the very same
response) resolves the fraction with offpeak.effective_for(model, base).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from pcai_llm.api import routes_proxy as rp
from pcai_llm.core import offpeak

MODEL = "glm-4"
BASE = 0.5  # the KEY's base fraction (profile ?? env default)
MODEL_RATIO = 0.25  # the console's per-model ratio override


def _ledger(model: str) -> Any:
    led = rp._StreamLedger(SimpleNamespace(id=uuid.uuid4(), key_prefix="hllm-XX"), model, 10, "rid-r5-8", 0.0, BASE)
    led.usage = {"prompt_tokens": 7, "completion_tokens": 3}
    led.completed = True
    return led


def test_r5_8_streamed_settle_bills_the_per_model_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streamed settle must book the SERVED MODEL's fraction.

    PRE-FIX (api/routes_proxy.py _StreamLedger.finish):
    `billing_fraction=offpeak.effective(self._off_base)` — the only global
    effective() call left in the tree. A busy model's stream booked a discount
    the operator never granted (or lost one they did), and the ledger number
    is what the budget gates consume; the SAME response's X-PCAI-Billing
    header was already computed with the per-model form, so one response could
    describe two different prices.
    """
    settles: list[dict[str, Any]] = []

    async def spy_settle(*a: Any, **kw: Any) -> None:
        settles.append(kw)

    monkeypatch.setattr(rp, "settle", spy_settle)
    monkeypatch.setattr(offpeak.state, "enabled", True)
    monkeypatch.setattr(offpeak.state, "active", True)
    monkeypatch.setattr(offpeak.state, "fraction", BASE)
    monkeypatch.setattr(offpeak, "_overrides", {MODEL: MODEL_RATIO})

    # The two forms must genuinely disagree here, or the assertion below could
    # pass for the wrong reason.
    assert offpeak.effective(BASE) == BASE
    assert offpeak.effective_for(MODEL, BASE) == MODEL_RATIO

    asyncio.run(_ledger(MODEL).finish())
    assert settles and settles[0].get("billing_fraction") == MODEL_RATIO, (
        "the streamed settle booked the GLOBAL fraction instead of the served model's"
    )
