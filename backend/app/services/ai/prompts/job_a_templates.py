"""Twelve defense-question templates. Each has {quote} placeholder that must
be filled with a verbatim quote from the candidate's essay."""

JOB_A_TEMPLATES = [
    "You propose \"{quote}\". Identify the single highest-risk assumption in this position and explain what concrete contingency you would trigger if that assumption fails in year one.",
    "Referencing your statement \"{quote}\", specify which institutional stakeholder stands to lose the most influence under your proposal, and explain how your framework prevents them from blocking execution.",
    "Your essay argues for \"{quote}\". If your budget or financing capacity were cut by 40% midway through implementation, which specific component would you sacrifice first, and why?",
    "You claim \"{quote}\". What is the single earliest milestone you would need to hit within 90 days to validate this claim, and what concrete evidence would convince you to abandon the approach?",
    "Your essay states \"{quote}\". Identify one specific piece of data that would contradict this position if it turned out to be true, and explain how you would respond if that data emerged.",
    "Referencing \"{quote}\", describe the political coalition you would need to assemble for this to succeed, and name one faction you would deliberately exclude from the process.",
    "You argue \"{quote}\". What existing Nigerian regulatory framework would this proposal clash with, and how would you sequence the reform to avoid the clash?",
    "Your essay claims \"{quote}\". Which Nigerian geopolitical zone stands to benefit least from this proposal, and how would you address the resulting political risk?",
    "Referencing \"{quote}\", is your proposal designed for short-term relief or long-term structural change? Explain what specifically would break if the time horizon were halved.",
    "You state \"{quote}\". Explicitly name the worst-case trade-off your proposal accepts, and explain in concrete terms why that trade-off is acceptable to Nigerian citizens.",
    "Your essay claims \"{quote}\". If this policy failed in year two, who should be held accountable, and what specific measurable metric would prove the failure?",
    "Referencing \"{quote}\", describe one alternative policy you considered and rejected, and explain the single decisive reason for its rejection.",
]


JOB_A_PERSONALIZE_PROMPT = """You are personalizing a defense question for a candidate.

TEMPLATE (contains one {{quote}} placeholder):
{template}

YOUR TASK:
1. Read the candidate's essay below.
2. Find a VERBATIM sentence (exact copy-paste, at least 15 words, no paraphrasing) 
   from the essay that best represents their core policy position on this matter.
3. Replace the {{quote}} placeholder in the template with that verbatim sentence.

Return ONLY this JSON, nothing else:
{{
  "essay_quote": "<verbatim sentence from the essay, at least 15 words>",
  "question": "<the template with {{quote}} replaced by the verbatim sentence>"
}}

ESSAY:
{essay}"""


def choose_template(submission_id: int) -> tuple[int, str]:
    """Deterministic template assignment. Returns (template_id 1..12, template_str)."""
    template_id = ((submission_id - 1) % len(JOB_A_TEMPLATES)) + 1
    return template_id, JOB_A_TEMPLATES[template_id - 1]


def plain_template(template_str: str) -> str:
    """Return the template with a neutral phrase instead of a real quote.
    Used in stub mode or when personalization fails verification."""
    return template_str.replace("{quote}", "your stated policy position")
