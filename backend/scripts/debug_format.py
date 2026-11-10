import asyncio, json, sys, httpx
sys.path.insert(0, "/app")
from app.core.config import settings

PROMPT = """Generate 2 unique financial facts for Nigerian university students.

Return each fact on its own line as pipe-separated fields:
category|snippet|explanation|source_name|source_url

Example: savings|Emergency fund rule|Save three to six months of expenses. Keep it liquid for instant access.|Investopedia|https://www.investopedia.com

Output ONLY the 2 lines. No commentary. No markdown. No JSON."""

async def main():
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(
            settings.DEEPSEEK_API_URL,
            json={
                "model": settings.DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": 4000,
                "temperature": 0.9,
            },
            headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                     "Content-Type": "application/json"},
        )
    body = r.json()
    usage = body.get("usage", {})
    choice = body["choices"][0]
    msg = choice["message"]
    content = msg.get("content", "") or ""
    reasoning = msg.get("reasoning_content", "") or ""
    print("FINISH REASON:", choice.get("finish_reason"))
    print("CONTENT LEN:", len(content))
    print("REASONING LEN:", len(reasoning))
    print("USAGE:", json.dumps(usage, indent=2))
    print("\n=== CONTENT (repr) ===")
    print(repr(content))
    print("\n=== CONTENT (plain) ===")
    print(content)

asyncio.run(main())
