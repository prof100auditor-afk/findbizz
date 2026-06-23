async def fetch_rows() -> list[dict]:
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            url = f"{APPS_SCRIPT_URL}?key={APPS_SCRIPT_KEY}"
            r = await client.get(url, timeout=30)
            logger.info(f"Apps Script: {r.status_code}")
            if r.status_code == 200 and r.text.strip() != "Unauthorized":
                import json as _json
                rows = _json.loads(r.text)
                rows = [row for row in rows if row.get("Название компании", "").strip()]
                logger.info(f"Loaded {len(rows)} rows")
                return rows
    except Exception as e:
        logger.error(f"Fetch error: {e}")
    return []
