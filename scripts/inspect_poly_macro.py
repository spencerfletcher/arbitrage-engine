import requests, json, datetime

DAYS = 7
cutoff = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=DAYS)

r = requests.get('https://gamma-api.polymarket.com/markets', params={
    'q': 'federal reserve rate CPI inflation 2026',
    'limit': 10000,
    'active': 'true',
    'closed': 'false',
})
for m in r.json():
    end_raw = m.get('resolutionTime') or m.get('endDate') or m.get('closeTime') or ''
    if not end_raw:
        continue
    from bot.core.matcher import parse_iso
    end = parse_iso(end_raw)
    if end is None or end > cutoff:
        continue
    if end < datetime.datetime.now(datetime.timezone.utc):
        continue
    tokens = json.loads(m.get('clobTokenIds') or '[]')
    print(f"  condition_id: {m['conditionId']}")
    print(f"  question:     {m.get('question')}")
    print(f"  resolves:     {end_raw}")
    print(f"  YES token:    {tokens[0] if tokens else '?'}")
    print(f"  NO  token:    {tokens[1] if len(tokens) > 1 else '?'}")
    print(f"end: {end}")
    print(f"cutoff: {cutoff}")
    print(f"{datetime.datetime.now(datetime.timezone.utc)}")
    print()