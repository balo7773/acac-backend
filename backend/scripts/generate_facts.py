#!/usr/bin/env python3
"""DeepSeek facts generator — v3. Big token budget, small batches, lenient parser."""

import asyncio, json, re, sys
from pathlib import Path
import httpx

sys.path.insert(0, "/app")
from app.core.config import settings

CATEGORIES = ["savings", "investing", "banking", "budgeting",
              "financial_planning", "nigerian_finance", "history_of_money", "behavioral_finance"]

# Trusted domains — any URL whose host ends with one of these is accepted
TRUSTED_DOMAINS = [
    "sec.gov.ng", "cbn.gov.ng", "ndic.gov.ng", "naicom.gov.ng",
    "pencom.gov.ng", "firs.gov.ng", "cac.gov.ng", "nelf.gov.ng",
    "ngxgroup.com", "fmdqgroup.com", "nibssplc.com.ng", "cscs.ng",
    "dmo.gov.ng", "worldbank.org", "imf.org", "investopedia.com",
    "cowrywise.com", "nairametrics.com", "businessday.ng",
]

PROMPT = """Generate {count} unique financial facts for Nigerian university students.

FORMAT — exactly one fact per line, 5 pipe-separated fields:
category|snippet|explanation|source_name|source_url

Rules:
- category must be one of: {categories}
- snippet: max 8 words, no period at end
- explanation: exactly 2 sentences, factually accurate, mention a mechanism or number
- source_name: name of a real institution or publication
- source_url: a real URL whose domain ends in one of: {domains}
- No duplicates within this response

Output ONLY the {count} lines, nothing else. No markdown, no JSON, no numbering.
"""

CHECKPOINT = Path("/app/scripts/facts-checkpoint.json")
FINAL_OUT = Path("/app/scripts/financial-facts.json")
TARGET = 150
BATCH_SIZE = 5
MAX_BATCHES = 50

def valid_domain(url: str) -> bool:
    m = re.match(r"^https?://([^/]+)", url, re.I)
    if not m: return False
    host = m.group(1).lower().replace("www.", "")
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)

def parse_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"): return None
    # Strip markdown fences if present
    if line.startswith("```") or line == "---": return None
    # Strip leading numbering like "1. " or "- " or "• "
    line = re.sub(r"^[\d]+[.)]\s*", "", line)
    line = re.sub(r"^[-•*]\s*", "", line)
    if "|" not in line: return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) != 5: return None
    cat, snip, expl, sname, surl = parts
    if cat not in CATEGORIES: return None
    if not snip or not expl or not sname or not surl: return None
    if len(snip.split()) > 8: return None
    snip = snip.rstrip(".")
    if not valid_domain(surl): return None
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", expl) if s.strip()]
    if not (1 <= len(sentences) <= 3): return None
    return {"category": cat, "snippet": snip, "explanation": expl,
            "source_name": sname, "source_url": surl}

async def call_deepseek(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(
            settings.DEEPSEEK_API_URL,
            json={
                "model": settings.DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8000,
                "temperature": 0.9,
            },
            headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                     "Content-Type": "application/json"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]

async def generate_batch(seen):
    prompt = PROMPT.format(
        count=BATCH_SIZE,
        categories=", ".join(CATEGORIES),
        domains=", ".join(TRUSTED_DOMAINS),
    )
    try:
        text = await call_deepseek(prompt)
    except Exception as e:
        print(f"API error: {e}")
        return []
    facts = []
    for line in text.splitlines():
        parsed = parse_line(line)
        if not parsed: continue
        if parsed["snippet"].lower() in seen: continue
        seen.add(parsed["snippet"].lower())
        facts.append(parsed)
    return facts, text

async def main():
    all_facts = []
    if CHECKPOINT.exists():
        try:
            all_facts = json.loads(CHECKPOINT.read_text())
            print(f"Resuming from checkpoint: {len(all_facts)} facts")
        except: pass
    seen = {f["snippet"].lower() for f in all_facts}
    print(f"Target: {TARGET}. Current: {len(all_facts)}.\n")

    for batch_num in range(1, MAX_BATCHES + 1):
        if len(all_facts) >= TARGET: break
        print(f"Batch {batch_num}: ", end="", flush=True)
        result = await generate_batch(seen)
        if not result: 
            print("API failed — retrying")
            await asyncio.sleep(3)
            continue
        batch, raw = result
        if batch:
            all_facts.extend(batch)
            CHECKPOINT.write_text(json.dumps(all_facts, indent=2, ensure_ascii=False))
            print(f"+{len(batch)} (total {len(all_facts)}/{TARGET}) ✓")
        else:
            print(f"0 valid. Raw preview: {raw[:200]!r}")
            await asyncio.sleep(2)
        await asyncio.sleep(1)

    final = all_facts[:TARGET]
    for i, f in enumerate(final, start=1):
        f["id"] = i
    FINAL_OUT.write_text(json.dumps(final, indent=2, ensure_ascii=False))
    cat_counts = {}
    for f in final:
        cat_counts[f["category"]] = cat_counts.get(f["category"], 0) + 1
    print(f"\n=== DONE: {len(final)} facts ===")
    for c in CATEGORIES: print(f"  {c}: {cat_counts.get(c, 0)}")

if __name__ == "__main__":
    asyncio.run(main())
