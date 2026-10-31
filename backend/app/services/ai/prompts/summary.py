"""DeepSeek prompt for generating a per-submission essay summary."""

SUMMARY_PROMPT = """You are writing a short summary of a Nigerian economic policy essay for a judge who is about to read it.

ESSAY:
{essay}

YOUR TASK:
Write 3 sentences (60-120 words) that capture:
1. The core policy proposal or position the essay argues for
2. The main evidence or reasoning it uses
3. The primary risk or trade-off it acknowledges

CRITICAL RULES:
- Do NOT use the candidate's name, matric number, or any identifying details
- Do NOT recommend a score, mark, or evaluation
- Do NOT compare to other essays
- Be neutral and factual, using the essay's own language where possible

Return ONLY this JSON, nothing else:
{
  "summary": "<3 sentences, 60-120 words>"
}"""
