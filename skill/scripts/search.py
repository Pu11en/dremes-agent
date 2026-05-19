#!/usr/bin/env python3
"""Permanent web search tool — DuckDuckGo HTML, no API keys, no rate limits."""
import sys, json, re, urllib.request, urllib.parse
from urllib.parse import unquote

def search(query: str, num: int = 10):
    params = urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        f"https://html.duckduckgo.com/html/?{params}",
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    )
    html = urllib.request.urlopen(req, timeout=15).read().decode()
    
    results = []
    for m in re.finditer(r'uddg=([^&"]+)', html):
        url = unquote(m.group(1))
        if url in [r['url'] for r in results]: continue
        results.append({"url": url})
    
    # Add titles from nearby result__a tags
    for r in results:
        idx = html.find(r['url'][:40])
        if idx < 0: continue
        nearby = html[max(0,idx-800):idx+300]
        tm = re.search(r'result__a[^>]*>([^<]+)</a>', nearby)
        r['title'] = tm.group(1).strip() if tm else ""
    
    return {"results": results[:num]}

if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "test"
    print(json.dumps(search(q), indent=2))
