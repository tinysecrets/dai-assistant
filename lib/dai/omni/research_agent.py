#!/usr/bin/env python3
"""
Research agent for omni-assistant.

Performs real research by searching the web (via DuckDuckGo HTML) and then
asking the model-router (dai/auto) to synthesize findings. Stdlib only.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def router_base() -> str:
    host = os.environ.get("DAI_ROUTER_HOST", "127.0.0.1")
    port = os.environ.get("DAI_ROUTER_PORT", "11435")
    return f"http://{host}:{port}"


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo HTML and extract result snippets."""
    # Use DuckDuckGo's HTML interface (no API key needed)
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"error": f"Search failed: {e}", "title": "", "snippet": "", "url": ""}]

    # Parse results - look for result__snippet and result__title
    results = []
    # Pattern for result snippets
    snippet_pattern = r'class="result__snippet">(.*?)</a>'
    title_pattern = r'class="result__title">.*?<a[^>]*>(.*?)</a>'
    url_pattern = r'class="result__url">.*?>(.*?)</a>'

    # Better: find all result blocks
    result_blocks = re.findall(r'class="result__body">(.*?)</div>\s*</div>', html, re.DOTALL)
    for block in result_blocks[:max_results]:
        # Extract title
        title_match = re.search(r'class="result__title">.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ""

        # Extract snippet
        snippet_match = re.search(r'class="result__snippet">(.*?)</a>', block, re.DOTALL)
        snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip() if snippet_match else ""

        # Extract URL
        url_match = re.search(r'class="result__url">.*?>(.*?)</a>', block, re.DOTALL)
        url = re.sub(r'<[^>]+>', '', url_match.group(1)).strip() if url_match else ""

        if title or snippet:
            results.append({"title": title, "snippet": snippet, "url": url})

    return results[:max_results]


def ask_router(prompt: str, timeout: float = 180.0) -> str:
    body = {
        "model": "dai/auto",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a research agent. Given a goal and web search results, "
                    "produce a concise, well-structured briefing: key findings, notable facts, "
                    "and open questions. Cite sources by title/URL. Be factual; flag uncertainty explicitly."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        f"{router_base()}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"router HTTP {exc.code}: {detail[:300]}") from exc
    return (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: research_agent.py <goal>")
        return 1

    goal = sys.argv[1]
    print(f"[Research Agent] Starting research: {goal}", flush=True)

    # Step 1: Search the web
    print(f"[Research Agent] Searching web for: {goal}", flush=True)
    search_results = web_search(goal, max_results=5)

    if search_results and "error" not in search_results[0]:
        print(f"[Research Agent] Found {len(search_results)} results", flush=True)
        for i, r in enumerate(search_results, 1):
            print(f"  {i}. {r.get('title', 'N/A')} - {r.get('url', 'N/A')}", flush=True)
    else:
        print("[Research Agent] Web search failed, using model knowledge only", flush=True)
        search_results = []

    # Step 2: Synthesize with router
    try:
        if search_results:
            results_text = "\n".join([
                f"Source {i}: {r.get('title', 'N/A')} ({r.get('url', 'N/A')})\n{r.get('snippet', 'N/A')}"
                for i, r in enumerate(search_results, 1)
            ])
            prompt = f"Research goal: {goal}\n\nWeb search results:\n{results_text}\n\nSynthesize a briefing with citations."
        else:
            prompt = f"Research goal: {goal}\n\n(No web search results available; use your training knowledge.)"

        findings = ask_router(prompt)
        print("[Research Agent] Findings:\n" + findings, flush=True)
        print("[Research Agent] Agent completed successfully.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[Research Agent] FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
