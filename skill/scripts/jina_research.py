#!/usr/bin/env python3
"""Jina on-demand web research — Reader + Search, API-key auth via env var.

Usage as module:
    from jina_research import search_web, read_page, research_topic

Usage as CLI:
    python3 jina_research.py search "query" [--num 5]
    python3 jina_research.py read "https://..." 
    python3 jina_research.py research "topic" [--num 3]

Env var: JINA_API_KEY (required for Search API, optional but recommended for Reader)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional


def _api_key() -> str:
    k = os.environ.get("JINA_API_KEY", "")
    if not k:
        raise RuntimeError("JINA_API_KEY not set in environment")
    return k


def _api_get(url: str, timeout: int = 30) -> str:
    headers = {"Accept": "application/json"}
    key = os.environ.get("JINA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Jina HTTP {e.code}: {body}")


# ── Reader API ──────────────────────────────────────────────────────────────

def read_page(url: str) -> dict:
    """Fetch clean markdown content from a URL via Jina Reader.

    Returns {"url": str, "title": str, "content": str, "status": int}
    """
    reader_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "application/json",
        "X-Return-Format": "markdown",
    }
    key = os.environ.get("JINA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # Use X-Engine to allow fallback if needed
    headers["X-Engine"] = "direct"
    
    req = urllib.request.Request(reader_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
            return {
                "url": url,
                "title": data.get("data", {}).get("title", ""),
                "content": data.get("data", {}).get("content", ""),
                "status": data.get("code", 200),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Jina Reader HTTP {e.code} for {url}: {body}")
    except json.JSONDecodeError:
        # Fallback: try plain text
        req2 = urllib.request.Request(reader_url, headers={"Accept": "text/plain"})
        if key:
            req2.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req2, timeout=45) as resp:
            return {
                "url": url,
                "title": "",
                "content": resp.read().decode(),
                "status": 200,
            }


# ── Search API ──────────────────────────────────────────────────────────────

def search_web(query: str, num: int = 5) -> list[dict]:
    """Search the web via Jina Search API.

    Returns list of {"title": str, "url": str, "description": str}
    Requires JINA_API_KEY.
    """
    _api_key()  # validate
    params = urllib.parse.urlencode({"q": query, "count": min(num, 20)})
    url = f"https://s.jina.ai/?{params}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_api_key()}",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Jina Search HTTP {e.code}: {body}")
    
    results = []
    for item in data.get("data", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
        })
    return results[:num]


# ── Combined Research ──────────────────────────────────────────────────────

def research_topic(topic: str, num_results: int = 3, deep_read: bool = False) -> dict:
    """Search + optionally deep-read top results. Returns structured context.

    Returns {"query": str, "search_results": [...], "deep_reads": [...] | None}
    """
    results = search_web(topic, num=num_results)
    
    deep = None
    if deep_read and results:
        deep = []
        for result in results[:2]:  # deep-read top 2
            try:
                page = read_page(result["url"])
                deep.append(page)
            except Exception as e:
                deep.append({"url": result["url"], "error": str(e)})
    
    return {
        "query": topic,
        "search_results": results,
        "deep_reads": deep,
    }


def research_to_context(research: dict, max_chars: int = 3000) -> str:
    """Convert research dict to a compact context block for prompt injection."""
    lines = [f"RESEARCH CONTEXT (from: \"{research['query']}\"):"]
    
    for i, r in enumerate(research.get("search_results", []), 1):
        desc = r.get("description", "")[:300]
        lines.append(f"  [{i}] {r['title'][:150]}")
        if desc:
            lines.append(f"      {desc}")
    
    if research.get("deep_reads"):
        for d in research["deep_reads"]:
            if "error" in d:
                lines.append(f"  [deep-read error: {d['url']} -> {d['error']}]")
                continue
            content = d.get("content", "")[:max_chars]
            lines.append(f"\n  DEEP READ: {d.get('title', d['url'])[:120]}")
            lines.append(f"  {'-'*40}")
            for line in content.split("\n")[:30]:
                if line.strip():
                    lines.append(f"  {line[:200]}")
    
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import urllib.parse
    
    parser = argparse.ArgumentParser(description="Jina web research tool")
    sub = parser.add_subparsers(dest="command", required=True)
    
    search_p = sub.add_parser("search", help="Web search")
    search_p.add_argument("query")
    search_p.add_argument("--num", type=int, default=5)
    
    read_p = sub.add_parser("read", help="Read a URL via Jina Reader")
    read_p.add_argument("url")
    
    research_p = sub.add_parser("research", help="Search + optionally deep-read")
    research_p.add_argument("topic")
    research_p.add_argument("--num", type=int, default=3)
    research_p.add_argument("--deep", action="store_true")
    
    args = parser.parse_args()
    
    if args.command == "search":
        results = search_web(args.query, num=args.num)
        print(json.dumps(results, indent=2))
    elif args.command == "read":
        result = read_page(args.url)
        print(f"# {result['title']}\n\n{result['content'][:5000]}")
    elif args.command == "research":
        result = research_topic(args.topic, num_results=args.num, deep_read=args.deep)
        print(json.dumps(result, indent=2))
