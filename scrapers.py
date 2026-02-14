"""
scrapers.py — Web scraping layer (Browserbase + Bright Data).

EXPECTED OUTPUT SCHEMA  (each paper is a dict):
    {
        "topic":         str,   # e.g. "mechanistic interpretability"
        "paper_name":    str,   # title of the paper
        "paper_authors": list,  # e.g. ["Alice", "Bob"]
        "published":     str,   # ISO date, e.g. "2024-11-03"
        "summary":       str,   # abstract / short description
        "url":           str,   # link to the paper
    }

The pipeline will call:
    papers = await run_scraper(topics=["topic A", "topic B"], per_topic=10)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")

BRIGHTDATA_API_KEY = os.getenv("BRIGHTDATA_API_KEY", "")

# ---------------------------------------------------------------------------
# Browserbase scraper
# ---------------------------------------------------------------------------

async def browserbase_search(
    query: str,
    *,
    topic: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Use Browserbase to find research papers for *query*.

    TODO:
      1. Create a browser session via the Browserbase SDK or REST API.
      2. Navigate to your target source (Google Scholar, arXiv, Semantic
         Scholar, etc.).
      3. Extract paper metadata from the page.
      4. Return a list of dicts matching the schema at the top of this file.

    Starter snippet (Playwright SDK):
        from browserbase import Browserbase
        bb = Browserbase(api_key=BROWSERBASE_API_KEY)
        session = bb.sessions.create(project_id=BROWSERBASE_PROJECT_ID)
        # ... use Playwright to navigate + scrape ...

    Starter snippet (REST API):
        import httpx
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                "https://www.browserbase.com/v1/sessions",
                headers={"x-bb-api-key": BROWSERBASE_API_KEY},
                json={"projectId": BROWSERBASE_PROJECT_ID},
            )
            session_id = resp.json()["id"]
            # ... navigate, extract, close session ...
    """
    # PLACEHOLDER 
    print(f"   [browserbase] TODO: implement search for '{query}' (limit={limit})")
    return []


# ---------------------------------------------------------------------------
# Bright Data 
# ---------------------------------------------------------------------------

async def brightdata_search(
    query: str,
    *,
    topic: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Use Bright Data to find research papers for *query*.

    TODO:
      1. Hit the Bright Data SERP API or Web Scraper API.
      2. Parse the response into paper dicts.
      3. Return a list of dicts matching the schema at the top of this file.

    Starter snippet (SERP API):
        import httpx
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                "https://api.brightdata.com/serp/req",
                headers={"Authorization": f"Bearer {BRIGHTDATA_API_KEY}"},
                json={
                    "query": f"{query} site:arxiv.org",
                    "num": limit,
                    "country": "us",
                },
            )
            data = resp.json()
            for item in data.get("organic", []):
                ...  # map to output schema
    """
    print(f"   [brightdata] TODO: implement search for '{query}' (limit={limit})")
    return []


# ---------------------------------------------------------------------------
# Unified entry point 
# ---------------------------------------------------------------------------

async def run_scraper(
    topics: list[str],
    *,
    per_topic: int = 10,
) -> list[dict[str, Any]]:
    """
    Search for papers across all *topics* using both Browserbase and
    Bright Data, then return a de-duplicated list.

    This is the ONLY function the rest of the codebase calls.
    """
    all_papers: list[dict[str, Any]] = []

    for topic in topics:
        # Run both scrapers in parallel for each topic
        bb, bd = await asyncio.gather(
            browserbase_search(topic, topic=topic, limit=per_topic),
            brightdata_search(topic, topic=topic, limit=per_topic),
        )
        all_papers.extend(bb)
        all_papers.extend(bd)

    # De-duplicate by paper_name
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for p in all_papers:
        key = p.get("paper_name", "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


# ---------------------------------------------------------------------------
# Quick test idk 
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    async def _demo():
        papers = await run_scraper(
            topics=["mechanistic interpretability", "protein diffusion models"],
            per_topic=5,
        )
        print(f"\n📚 Scrapers returned {len(papers)} papers")
        for p in papers:
            print(f"  • {p.get('paper_name', '???')}")

    asyncio.run(_demo())
