"""
pipeline.py
Run standalone:  python pipeline.py              # runs debate workflow
                 python pipeline.py --scrape     # runs scrape workflow
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from supabase import create_client, Client as SupabaseClient

from agents import DebateResult, run_debate
from scrapers import run_scraper  # abstract todo

# ---------------------------------------------------------------------------
# Config — loaded from config.json (dashboard writes this file)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    """Read config.json, falling back to sensible defaults."""
    defaults = {"topic": "mechanistic interpretability", "multiplier": 3}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**defaults, **cfg}
    return defaults

def save_config(cfg: dict) -> None:
    """Persist config back to config.json (called by the dashboard later)."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # anon or service-role key


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

def _get_supabase() -> SupabaseClient:
    """Return a Supabase client.  Raises if creds are missing."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "Set SUPABASE_URL and SUPABASE_KEY env vars "
            "(grab them from your Supabase project → Settings → API)."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# A.  SCRAPE workflow  
# ---------------------------------------------------------------------------

async def scrape_and_store(
    topics: list[str] | None = None,
    *,
    per_topic: int = 10,
) -> list[dict]:
    """
    Call the scraper, then upsert results into the ``papers`` table.

    Returns the list of stored rows.
    """
    topics = topics or [load_config()["topic"]]
    sb = _get_supabase()

    print("🔍  Scraping papers …")
    papers = await run_scraper(topics=topics, per_topic=per_topic)
    print(f"   Scrapers returned {len(papers)} papers")

    if not papers:
        return []

    # Normalise into the DB schema before upserting
    rows = []
    for p in papers:
        rows.append({
            "topic": p.get("topic", topics[0]),
            "paper_name": p.get("paper_name", p.get("title", "")),
            "paper_authors": p.get("paper_authors", p.get("authors", [])),
            "published": p.get("published", p.get("date", None)),
            "summary": p.get("summary", p.get("abstract", "")),
            "journal": p.get("journal", ""),
            "fulltext": p.get("fulltext", ""),
            "url": p.get("url", ""),
        })

    resp = sb.table("papers").upsert(rows, on_conflict="paper_name").execute()
    stored = resp.data if resp.data else rows
    print(f"   Stored {len(stored)} papers in DB")
    return stored


# ---------------------------------------------------------------------------
# B.  DEBATE workflow
# ---------------------------------------------------------------------------

def fetch_papers_by_topic(
    topic: str,
    *,
    sb: SupabaseClient | None = None,
    limit: int = 50,
) -> list[dict]:
    """
    SELECT papers from Supabase whose ``topic`` matches *topic*.

    Uses a case-insensitive ``ilike`` so "Mechanistic Interpretability"
    matches "mechanistic interpretability".
    """
    if sb is None:
        sb = _get_supabase()

    resp = (
        sb.table("papers")
        .select("*")
        .ilike("topic", f"%{topic}%")
        .order("published", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data if resp.data else []


def store_debate(sb: SupabaseClient, result: DebateResult) -> dict:
    """Insert a single debate result into the ``debates`` table.

    Links back to ``papers`` via the ``paper_id`` (FK → papers.id)
    """
    v = result.verdict
    row = {
        "paper_id": result.paper.get("id"),  # FK → papers.id
        "topic": result.paper.get("topic", ""),
        "verdict": v.get("verdict", "UNCERTAIN"),
        "confidence": v.get("confidence", 0.0),
        "one_liner": v.get("one_liner", ""),
        "key_strengths": json.dumps(v.get("key_strengths", [])),
        "key_risks": json.dumps(v.get("key_risks", [])),
        "suggested_verticals": json.dumps(v.get("suggested_verticals", [])),
        "follow_up_questions": json.dumps(v.get("follow_up_questions", [])),
        "debate_log": json.dumps(result.rounds),
        "raw_verdict": result.raw_verdict,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = sb.table("debates").insert(row).execute()
    return resp.data[0] if resp.data else row


def run_debate_pipeline(
    topic: str | None = None,
    *,
    multiplier: int | None = None,
    debate_rounds: int = 3,
    limit: int = 50,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """
    Pull papers for *topic* from the DB, debate each one, store verdicts,
    then return the top 1/multiplier papers ranked by confidence.

    Parameters
    ----------
    topic : str | None
        Filter papers by topic.  ``None`` → reads from config.json.
    multiplier : int | None
        Return the top 1/*multiplier* fraction of debated papers.
    debate_rounds : int
        Number of Scout ↔ Advocate ↔ Skeptic loops per paper.
    limit : int
        Max papers to pull from the DB.
    verbose : bool
        Print agent responses to stdout.

    Returns
    -------
    list[dict]
        Top papers after the multiplier cut, sorted by confidence desc.
    """
    cfg = load_config()
    topic = topic or cfg["topic"]
    multiplier = multiplier or cfg.get("multiplier", 3)
    sb = _get_supabase()

    # 1. Fetch papers for this topic -----------------------------------------
    print(f"📚  Fetching papers for topic: \"{topic}\" …")
    papers = fetch_papers_by_topic(topic, sb=sb, limit=limit)
    print(f"   Found {len(papers)} papers in DB")

    if not papers:
        print("   Nothing to debate — run the scraper first, or check your topic.")
        return []

    # 2. Debate each paper ---------------------------------------------------
    all_results: list[dict[str, Any]] = []
    for idx, paper in enumerate(papers, 1):
        name = paper.get("paper_name", "?")
        print(f"\n📄  [{idx}/{len(papers)}] Debating: {name}")

        debate_result = run_debate(
            paper,
            num_rounds=debate_rounds,
            verbose=verbose,
        )

        # 3. Store verdict ----------------------------------------------------
        stored = store_debate(sb, debate_result)
        all_results.append({
            "paper": paper,
            "verdict": debate_result.verdict,
            "debate_log": debate_result.rounds,
            "stored": stored,
        })

        v = debate_result.verdict
        emoji = {
            "PROMISING": "🟢",
            "INTERESTING": "🟡",
            "UNCERTAIN": "🟠",
            "WEAK": "🔴",
        }.get(v.get("verdict", ""), "⚪")
        print(f"   {emoji} {v.get('verdict', '?')}  (confidence {v.get('confidence', '?')})")
        print(f"   → {v.get('one_liner', '')}")

    # 4. Apply multiplier — keep top 1/multiplier by confidence ---------------
    all_results.sort(
        key=lambda r: r["verdict"].get("confidence", 0.0),
        reverse=True,
    )
    top_n = max(1, math.ceil(len(all_results) / multiplier))
    top_results = all_results[:top_n]

    print(f"\n{'='*60}")
    print(f"🎯  Returning top {top_n} / {len(all_results)} papers (multiplier=1/{multiplier})")
    for r in top_results:
        c = r["verdict"].get("confidence", 0)
        print(f"   • [{c:.2f}] {r['paper'].get('paper_name')}")
    print("=" * 60)

    return top_results


def get_top_papers(
    topic: str | None = None,
    *,
    multiplier: int | None = None,
) -> list[dict]:
    """Fetch the top 1/*multiplier* debates, joined with paper data.

    Reads ``topic`` and ``multiplier`` from config.json when not provided.
    """
    cfg = load_config()
    topic = topic or cfg["topic"]
    multiplier = multiplier or cfg.get("multiplier", 3)
    sb = _get_supabase()

    # Pull all debates for this topic, ordered by confidence desc
    query = (
        sb.table("debates")
        .select("*, papers(*)")
        .order("confidence", desc=True)
    )
    if topic:
        query = query.ilike("topic", f"%{topic}%")
    resp = query.execute()
    rows = resp.data if resp.data else []

    # Apply multiplier cut
    top_n = max(1, math.ceil(len(rows) / multiplier)) if rows else 0
    return rows[:top_n]


if __name__ == "__main__":
    cfg = load_config()

    parser = argparse.ArgumentParser(description="Research paper debate pipeline")
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Run the SCRAPE workflow (calls scrapers.run_scraper → DB).",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help=f"Topic to scrape / debate (config.json default: {cfg['topic']}).",
    )
    parser.add_argument(
        "--multiplier",
        type=int,
        default=None,
        help=f"Return top 1/N papers (config.json default: {cfg.get('multiplier', 3)}).",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=2,
        help="Debate rounds per paper (default: %(default)s).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max papers to pull from DB (default: %(default)s).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print full agent responses.",
    )
    args = parser.parse_args()

    if args.scrape:
        # --- SCRAPE workflow ---
        asyncio.run(scrape_and_store([args.topic] if args.topic else None))
    else:
        # --- DEBATE workflow ---
        results = run_debate_pipeline(
            topic=args.topic,
            multiplier=args.multiplier,
            debate_rounds=args.rounds,
            limit=args.limit,
            verbose=args.verbose,
        )

        # Dump results to JSON for inspection
        out_path = "pipeline_results.json"
        with open(out_path, "w") as f:
            json.dump(
                [
                    {
                        "paper_name": r["paper"].get("paper_name"),
                        "verdict": r["verdict"],
                        "debate_rounds": len(r["debate_log"]),
                    }
                    for r in results
                ],
                f,
                indent=2,
            )
        print(f"\n💾  Full results saved to {out_path}")
