"""Real-API integration test. Skipped by default.

Enable with:  RUN_REAL_AI_TESTS=1 docker exec -it acac-backend pytest -v -k test_p2_fix_13

Costs a few cents (DeepSeek) + Gemma free tier. Runs 2 API calls.
"""
import os
import pytest
from app.services.ai.client import call_deepseek, call_gemma, AIClientError
from app.services.ai.schemas import EVAL_PROMPT_TEMPLATE


ESSAY = """
Nigeria's fiscal policy landscape requires immediate structural reform. The federal
government's 2026 budget allocates ₦28.7 trillion to infrastructure, representing
a 34% increase over the prior year. However, the CBN's monetary tightening cycle
has pushed the policy rate to 27.5%, constraining private sector credit growth.
Coordinated fiscal-monetary policy is essential: without it, inflation targeting
will fail and the naira will remain volatile. The Debt Management Office reports
a debt service ratio of 42% of revenue, which crowds out capital expenditure.
A phased removal of fuel subsidies, paired with targeted cash transfers through
the National Social Investment Programme, would reduce distortionary effects while
protecting vulnerable households.
"""


def _verify_quotes(essay: str, criteria: dict) -> tuple[int, int]:
    """Return (found_count, total_count) for quote_text in criteria."""
    essay_norm = " ".join(essay.split())
    found = total = 0
    for ckey in ("c1", "c2", "c3", "c4", "c5"):
        q = criteria.get(ckey, {}).get("quote_text", "").strip()
        if not q:
            continue
        total += 1
        q_norm = " ".join(q.split())
        if q_norm in essay_norm:
            found += 1
    return found, total


@pytest.mark.skipif(
    os.getenv("RUN_REAL_AI_TESTS") != "1",
    reason="Set RUN_REAL_AI_TESTS=1 (costs money, needs network)",
)
@pytest.mark.asyncio
async def test_p2_fix_13_ai_real_smoke():
    """Calls DeepSeek + Gemma with a small essay. Verifies:
    - response parses to expected shape
    - 5 criteria present, marks in 0..1
    - at least 4/5 quotes verifiable in the source essay (hallucination check)
    - both models return quickly enough to be practical
    """
    import time

    for name, caller in (("deepseek", call_deepseek), ("gemma", call_gemma)):
        t0 = time.monotonic()
        prompt = EVAL_PROMPT_TEMPLATE.replace("{{ESSAY}}", ESSAY)
        result = await caller(prompt)
        elapsed = time.monotonic() - t0

        assert "parsed" in result
        assert "model_version" in result
        parsed = result["parsed"]
        assert "criteria" in parsed, f"{name}: no 'criteria' in parsed = {list(parsed.keys())}"

        crit = parsed["criteria"]
        for ckey in ("c1", "c2", "c3", "c4", "c5"):
            assert ckey in crit, f"{name}: missing {ckey}"
            mark = float(crit[ckey].get("mark", -1))
            assert 0.0 <= mark <= 1.0, f"{name}: {ckey} mark out of range: {mark}"
            assert crit[ckey].get("quote_text", "").strip(), f"{name}: {ckey} empty quote"

        found, total = _verify_quotes(ESSAY, crit)
        assert found >= 4, (
            f"{name}: only {found}/{total} quotes verbatim-verifiable. "
            f"Model may be paraphrasing despite instructions."
        )

        assert elapsed < 90, f"{name}: {elapsed:.1f}s too slow for production use"
        print(f"{name}: OK  {elapsed:.1f}s  found {found}/{total} quotes  model={result['model_version']}")
