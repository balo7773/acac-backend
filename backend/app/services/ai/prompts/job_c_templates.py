"""Job C prompt — conclusion audit. Determines whether the candidate's defense
answer contradicts the essay's core policy position."""

JOB_C_AUDIT_PROMPT = """You are auditing a candidate's defense answer against their own essay.

ESSAY (the candidate's submitted policy essay):
{essay}

DEFENSE QUESTION ASKED:
{question}

CANDIDATE'S DEFENSE ANSWER:
{answer}

YOUR TASK:
Determine whether the answer CONTRADICTS the core policy position in the essay.
- "contradictory" = the answer advocates a position OPPOSITE to the essay's core claim
- "consistent" = the answer defends, elaborates, refines, or agrees with the essay's core claim

IMPORTANT RULES:
- Using the word "contradict" does NOT mean contradictory (e.g., "this will not contradict" is CONSISTENT)
- Contradictory means the substantive policy positions conflict, not the vocabulary
- If the answer goes off-topic (doesn't engage with the question at all), still classify by the position it takes

Return ONLY this JSON, nothing else:
{{
  "label": "contradictory" OR "consistent",
  "essay_quote": "a VERBATIM sentence from the essay that represents its core position (must be exact copy-paste)",
  "conclusion_quote": "a VERBATIM sentence from the answer that best shows contradiction or consistency (must be exact copy-paste)",
  "addresses_question": true OR false,
  "reasoning": "one sentence explaining your label"
}}"""


def validate_audit_response(parsed: dict, essay: str, answer: str) -> dict:
    """Validate model output, verify quotes are verbatim, and compute reference_mark.
    Raises ValueError if the response is unusable, letting the caller fall back."""
    label = (parsed.get("label") or "").strip().lower()
    if label not in ("contradictory", "consistent"):
        raise ValueError(f"invalid label: {label!r}")

    essay_quote = (parsed.get("essay_quote") or "").strip()
    conclusion_quote = (parsed.get("conclusion_quote") or "").strip()
    addresses_question = bool(parsed.get("addresses_question", True))

    essay_norm = " ".join(essay.split())
    answer_norm = " ".join(answer.split())

    eq_norm = " ".join(essay_quote.split())
    cq_norm = " ".join(conclusion_quote.split())

    quotes_verified = (
        bool(eq_norm) and eq_norm in essay_norm
        and bool(cq_norm) and cq_norm in answer_norm
    )

    return {
        "label": label,
        "essay_quote": essay_quote[:500] if essay_quote else essay_norm[:100],
        "conclusion_quote": conclusion_quote[:500] if conclusion_quote else answer_norm[:100],
        "quotes_verified": quotes_verified,
        "addresses_question": addresses_question,
        "reference_mark": 6.0 if label == "contradictory" else 24.0,
    }
