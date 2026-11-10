#!/usr/bin/env python3
"""One-time script: uses DeepSeek to generate ~200 unique financial facts,
validates them, deduplicates, writes to ../acac-frontend/content/financial-facts.json"""

import asyncio, json, os, re, sys
from pathlib import Path

script_dir = Path(__file__).parent.resolve()
env_file = script_dir / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

sys.path.insert(0, str(script_dir / "backend"))
from app.services.ai.client import call_deepseek, AIClientError

CATEGORIES = [
    "savings", "investing", "banking", "budgeting",
    "financial_planning", "nigerian_finance", "history_of_money", "behavioral_finance",
]

ALLOWED_SOURCES = [
    ("SEC Nigeria", "https://sec.gov.ng"),
    ("CBN Nigeria", "https://www.cbn.gov.ng"),
    ("NDIC", "https://ndic.gov.ng"),
    ("NAICOM", "https://naicom.gov.ng"),
    ("PenCom", "https://www.pencom.gov.ng"),
    ("FIRS", "https://firs.gov.ng"),
    ("CAC", "https://cac.gov.ng"),
    ("NGX Group", "https://ngxgroup.com"),
    ("FMDQ Group", "https://fmdqgroup.com"),
    ("NIBSS", "https://nibssplc.com.ng"),
    ("CSCS Nigeria", "https://cscs.ng"),
    ("DMO Nigeria", "https://www.dmo.gov.ng"),
    ("World Bank", "https://www.worldbank.org"),
    ("IMF", "https://www.imf.org"),
    ("Investopedia", "https://www.investopedia.com"),
    ("Cowrywise", "https://cowrywise.com"),
]
SOURCE_MAP = dict(ALLOWED_SOURCES)

PROMPT = """Generate exactly {count} unique financial facts for a Nigerian university student competition.

Return ONLY a JSON object with this exact shape:
{{"facts": [
  {{"category": "savings", "snippet": "max 8 words teaser", "explanation": "Two sentences. Educational.", "source_name": "Investopedia", "source_url": "https://www.investopedia.com"}}
]}}

Constraints:
- category MUST be exactly one of: {categories}
- snippet: MAX 8 words, teaser style, no ending period
- explanation: exactly 2 sentences, factually accurate, educational, mention a specific mechanism or number
- source_name and source_url MUST be a matched pair from this list:
{source_pairs}
- No duplicate snippets within this batch
- Prefer diverse sub-topics, not rephrasings of the same idea
- Real, verifiable facts ONLY. If uncertain, choose a simpler, safer fact.
"""

async def generate_batch(count, batch_num, seen):
    prompt = PROMPT.format(
        count=count,
        categories=", ".join(CATEGORIES),
        source_pairs="\n".join(f"  - {n} | {u}" for n, u in ALLOWED_SOURCES),
    )
    print(f"  Batch {batch_num}: requesting {count}...", end=" ", flush=True)
    try:
        result = await call_deepseek(prompt, max_tokens=8000, temperature=0.95)
    except AIClientError as e:
        print(f"FAILED: {e}")
        return []
    parsed = result["parsed"]
    raw = parsed.get("facts") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
    if not raw:
        print(f"unexpected shape {type(parsed)}")
        return []
    valid = []
    for f in raw:
        if not isinstance(f, dict): continue
        cat = str(f.get("category", "")).strip()
        snip = str(f.get("snippet", "")).strip().rstrip(".")
        expl = str(f.get("explanation", "")).strip()
        sname = str(f.get("source_name", "")).strip()
        surl = str(f.get("source_url", "")).strip()
        if cat not in CATEGORIES: continue
        if not snip or not expl: continue
        if len(snip.split()) > 8: continue
        if snip.lower() in seen: continue
        if sname not in SOURCE_MAP: continue
        if SOURCE_MAP[sname] != surl: continue
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", expl) if s.strip()]
        if not (1 <= len(sentences) <= 3): continue
        valid.append({"category": cat, "snippet": snip, "explanation": expl,
                      "source_name": sname, "source_url": surl})
        seen.add(snip.lower())
    print(f"{len(valid)}/{len(raw)} valid")
    return valid

async def main():
    TARGET = 200
    BATCH_SIZE = 25
    NUM_BATCHES = (TARGET + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Generating {TARGET} facts in {NUM_BATCHES} batches of {BATCH_SIZE}...")
    print("(DeepSeek takes 20-40s per batch — expect ~5 min total)\n")
    all_facts, seen = [], set()
    for i in range(NUM_BATCHES):
        batch = await generate_batch(BATCH_SIZE, i + 1, seen)
        all_facts.extend(batch)
        if len(all_facts) >= TARGET:
            break
    final = all_facts[:TARGET]
    for i, f in enumerate(final, start=1):
        f["id"] = i
    cat_counts = {}
    for f in final:
        cat_counts[f["category"]] = cat_counts.get(f["category"], 0) + 1
    print(f"\n=== FINAL: {len(final)} facts ===")
    for c in CATEGORIES:
        print(f"  {c}: {cat_counts.get(c, 0)}")
    out_dir = script_dir.parent / "acac-frontend" / "content"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "financial-facts.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")

if __name__ == "__main__":
    asyncio.run(main())
