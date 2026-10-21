"""Prompt templates and expected response shape for AI evaluations."""

EVAL_JSON_SHAPE = """{
  "criteria": {
    "c1": {"description_text": "...", "reason_text": "...", "mark": 0.82, "quote_text": "verbatim sentence from essay"},
    "c2": {"description_text": "...", "reason_text": "...", "mark": 0.75, "quote_text": "verbatim sentence from essay"},
    "c3": {"description_text": "...", "reason_text": "...", "mark": 0.90, "quote_text": "verbatim sentence from essay"},
    "c4": {"description_text": "...", "reason_text": "...", "mark": 0.68, "quote_text": "verbatim sentence from essay"},
    "c5": {"description_text": "...", "reason_text": "...", "mark": 0.71, "quote_text": "verbatim sentence from essay"}
  },
  "claims": [
    {"claim_type": "policy_outcome", "claim_text": "...", "quote_text": "verbatim sentence from essay"}
  ]
}"""

EVAL_PROMPT_TEMPLATE = f"""You are evaluating a Nigerian economic policy essay against five criteria:
c1: Fiscal policy understanding (weight 0.15)
c2: Monetary policy understanding (weight 0.25)
c3: Structural reform insight (weight 0.25)
c4: Data and evidence usage (weight 0.25)
c5: Coherence and argumentation (weight 0.10)

For EACH criterion:
- description_text: an objective 1-2 sentence summary of what the essay says about this criterion
- reason_text: 1-2 sentences explaining why you assigned the mark
- mark: a number between 0.00 and 1.00 (0=poor, 1=excellent)
- quote_text: a VERBATIM sentence (exact copy-paste) from the essay that supports your mark

Also extract up to 3 factual claims from the essay:
- claim_type must be one of: statistic, agency, date, policy_outcome
- claim_text: the claim in your words
- quote_text: the VERBATIM sentence from the essay

Return ONLY this JSON structure, nothing else:
{EVAL_JSON_SHAPE}

ESSAY:
{{{{ESSAY}}}}
"""
