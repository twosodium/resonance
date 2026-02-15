"""
pipeline.py — End-to-end research pipeline.

Phases
------
1. **Scrape** — Use ``research_harness.run_harness()`` to fetch papers
   from arXiv / bioRxiv / web, then store in Supabase ``papers`` table.
2. **Debate** — Pull papers from DB, run multi-agent debate on each,
   store verdicts in Supabase ``debates`` table.
3. **full_pipeline** — Scrape ➜ Debate in one call (used by the API).

Run standalone::

    python pipeline.py --scrape --topic "protein folding"
    python pipeline.py --topic "protein folding"     # debate only
    python pipeline.py --full --topic "protein folding"   # both
"""

from __future__ import annotations

import argparse
import json
import logging
import concurrent.futures
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(_SCRIPT_DIR, ".env"))

from supabase import Client as SupabaseClient, create_client  # noqa: E402

from agents import DebateResult, run_debate  # noqa: E402

logger = logging.getLogger("pipeline")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# ---------------------------------------------------------------------------
# Config — loaded from config.json (dashboard writes this file)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """Read config.json, falling back to sensible defaults."""
    defaults = {"topic": "mechanistic interpretability"}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**defaults, **cfg}
    return defaults


def save_config(cfg: dict) -> None:
    """Persist config back to config.json (called by the dashboard later)."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Supabase client  (prefers service-role key for backend writes)
# ---------------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_KEY")
    or ""
)


def _get_supabase() -> SupabaseClient:
    """Return a Supabase client.  Raises if creds are missing."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "Set SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) "
            "env vars (grab them from your Supabase project → Settings → API)."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(s: str | None) -> str | None:
    """Remove null bytes that PostgreSQL text columns reject."""
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    return "".join(c for c in s if c != "\x00" and (ord(c) >= 32 or c in "\n\r\t"))


# ---------------------------------------------------------------------------
# A.  SCRAPE workflow — uses research_harness
# ---------------------------------------------------------------------------

def scrape_and_store(
    topic: str,
    *,
    user_id: str | None = None,
    candidate_count: int = 50,
    top_k: int = 20,
    max_age_months: int = 0,
) -> list[dict]:
    """
    Fetch papers via ``research_harness.run_harness()``, convert to DB
    schema dicts, and upsert into the ``papers`` table.

    Parameters
    ----------
    topic : str
        Research topic / query string.
    user_id : str | None
        UUID of the logged-in user (stored with every paper row).
    candidate_count : int
        How many candidates to fetch per source.
    top_k : int
        Max papers to keep after LLM filtering.

    Returns
    -------
    list[dict]
        The stored rows (including their DB ``id`` values).
    """
    from research_harness import paper_to_dict, run_harness

    logger.info("Scraping papers for topic=%r  (candidates=%d, top_k=%d)", topic, candidate_count, top_k)
    papers = run_harness(
        prompt=topic,
        candidate_count=candidate_count,
        top_k=top_k,
        max_age_months=max_age_months,
    )
    logger.info("Harness returned %d papers", len(papers))

    if not papers:
        return []

    # Convert Paper dataclass → dict (matching the DB schema)
    rows: list[dict] = []
    for p in papers:
        row = paper_to_dict(p, topic=topic)
        if user_id:
            row["user_id"] = user_id
        rows.append(row)

    # Upsert into Supabase
    sb = _get_supabase()
    try:
        resp = sb.table("papers").upsert(rows, on_conflict="url").execute()
        stored = resp.data if resp.data else rows
    except Exception as exc:
        err = str(exc)
        # If upsert fails because there's no unique constraint on url,
        # fall back to plain insert.
        if "42P10" in err or "unique or exclusion constraint" in err.lower():
            logger.warning("No UNIQUE on url — falling back to INSERT.")
            try:
                resp = sb.table("papers").insert(rows).execute()
                stored = resp.data if resp.data else rows
            except Exception as exc2:
                logger.error("Insert also failed: %s", exc2)
                stored = rows
        else:
            # Retry without columns that might not exist in the table
            col_match = re.search(r"Could not find the ['\"](\w+)['\"] column", err)
            if col_match:
                col = col_match.group(1)
                logger.warning("Column %r missing — retrying without it.", col)
                for r in rows:
                    r.pop(col, None)
                try:
                    resp = sb.table("papers").upsert(rows, on_conflict="url").execute()
                    stored = resp.data if resp.data else rows
                except Exception:
                    resp = sb.table("papers").insert(rows).execute()
                    stored = resp.data if resp.data else rows
            else:
                logger.error("Supabase upsert failed: %s", exc)
                stored = rows

    logger.info("Stored %d papers in DB", len(stored))
    return stored


# ---------------------------------------------------------------------------
# B.  DEBATE workflow
# ---------------------------------------------------------------------------

def fetch_papers_by_topic(
    topic: str,
    *,
    user_id: str | None = None,
    sb: SupabaseClient | None = None,
    limit: int = 50,
) -> list[dict]:
    """
    SELECT papers whose ``topic`` matches (case-insensitive).
    Optionally filter by ``user_id``.
    """
    if sb is None:
        sb = _get_supabase()

    query = (
        sb.table("papers")
        .select("*")
        .ilike("topic", f"%{topic}%")
        .order("published", desc=True)
        .limit(limit)
    )
    if user_id:
        query = query.eq("user_id", user_id)

    resp = query.execute()
    return resp.data if resp.data else []


def store_debate(
    sb: SupabaseClient,
    result: DebateResult,
    *,
    user_id: str | None = None,
) -> dict:
    """Insert a single debate result into the ``debates`` table.

    Links back to ``papers`` via ``paper_id`` (FK → papers.id).
    """
    v = result.verdict
    row: dict[str, Any] = {
        "paper_id": result.paper.get("id"),
        "topic": result.paper.get("topic", ""),
        "verdict": v.get("verdict", "UNCERTAIN"),
        "confidence": v.get("confidence", 0.0),
        "topicality": v.get("topicality", 0.5),
        "one_liner": v.get("one_liner", ""),
        "key_strengths": json.dumps(v.get("key_strengths", [])),
        "key_risks": json.dumps(v.get("key_risks", [])),
        "big_ideas": json.dumps(v.get("big_ideas", [])),
        "follow_up_questions": json.dumps(v.get("follow_up_questions", [])),
        "debate_log": json.dumps(result.rounds),
        "raw_verdict": result.raw_verdict,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if user_id:
        row["user_id"] = user_id

    resp = sb.table("debates").insert(row).execute()
    return resp.data[0] if resp.data else row


def run_debate_pipeline(
    topic: str | None = None,
    *,
    user_id: str | None = None,
    user_context: str = "",
    debate_rounds: int = 2,
    limit: int = 50,
    verbose: bool = False,
    on_phase: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Pull papers for *topic* from the DB, debate each one **in parallel**,
    store verdicts, and return all results sorted by confidence.
    """
    cfg = load_config()
    topic = topic or cfg["topic"]
    sb = _get_supabase()

    # 1. Fetch papers --------------------------------------------------------
    logger.info("Fetching papers for topic=%r, user_id=%s", topic, user_id)
    papers = fetch_papers_by_topic(topic, user_id=user_id, sb=sb, limit=limit)
    logger.info("Found %d papers in DB", len(papers))

    if not papers:
        logger.info("Nothing to debate — run the scraper first, or check your topic.")
        return []

    if on_phase:
        on_phase("debating")

    # 2. Debate papers in parallel -------------------------------------------
    from anthropic import Anthropic as _Anthropic

    shared_client = _Anthropic()

    def _debate_one(idx_paper: tuple[int, dict]) -> dict[str, Any]:
        idx, paper = idx_paper
        name = paper.get("paper_name", "?")
        logger.info("[%d/%d] Debating: %s", idx, len(papers), name)

        debate_result = run_debate(
            paper,
            num_rounds=debate_rounds,
            client=shared_client,
            verbose=verbose,
            user_context=user_context,
        )

        # Store verdict
        stored = store_debate(sb, debate_result, user_id=user_id)

        v = debate_result.verdict
        emoji = {
            "PROMISING": "🟢", "INTERESTING": "🟡",
            "UNCERTAIN": "🟠", "WEAK": "🔴",
        }.get(v.get("verdict", ""), "⚪")
        logger.info("  %s %s  (confidence %.2f)", emoji, v.get("verdict", "?"), v.get("confidence", 0))

        return {
            "paper": paper,
            "verdict": debate_result.verdict,
            "debate_log": debate_result.rounds,
            "stored": stored,
        }

    max_workers = min(5, len(papers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        all_results = list(pool.map(_debate_one, enumerate(papers, 1)))

    all_results.sort(key=lambda r: r["verdict"].get("confidence", 0.0), reverse=True)
    logger.info("Returning all %d debated papers", len(all_results))
    return all_results


# ---------------------------------------------------------------------------
# C.  FULL PIPELINE — scrape ➜ debate (called by api.py)
# ---------------------------------------------------------------------------

def full_pipeline(
    topic: str,
    *,
    user_id: str | None = None,
    user_context: str = "",
    candidate_count: int | None = None,
    top_k: int | None = None,
    debate_rounds: int | None = None,
    verbose: bool = False,
    on_phase: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run the complete pipeline end-to-end:

    1. Scrape papers for *topic* and store in DB with *user_id*.
    2. Debate each newly-stored paper.
    3. Return summary counts.

    Pipeline params fall back to ``config.json`` if not provided.
    """
    cfg = load_config()
    candidate_count = candidate_count or cfg.get("candidate_count", 5)
    top_k = top_k or cfg.get("top_k", 5)
    debate_rounds = debate_rounds or cfg.get("debate_rounds", 2)

    if on_phase:
        on_phase("scraping")

    stored_papers = scrape_and_store(
        topic,
        user_id=user_id,
        candidate_count=candidate_count,
        top_k=top_k,
    )

    if on_phase:
        on_phase("debating")

    debate_results = run_debate_pipeline(
        topic=topic,
        user_id=user_id,
        user_context=user_context,
        debate_rounds=debate_rounds,
        verbose=verbose,
        on_phase=on_phase,
    )

    if on_phase:
        on_phase("complete")

    return {
        "topic": topic,
        "papers_count": len(stored_papers),
        "debates_count": len(debate_results),
    }


# ---------------------------------------------------------------------------
# D.  GET top papers (read-only, used by dashboard)
# ---------------------------------------------------------------------------

def get_top_papers(
    topic: str | None = None,
    *,
    user_id: str | None = None,
) -> list[dict]:
    """Fetch all debates for *topic*, joined with paper data, sorted by confidence."""
    cfg = load_config()
    topic = topic or cfg["topic"]
    sb = _get_supabase()

    query = (
        sb.table("debates")
        .select("*, papers(*)")
        .order("confidence", desc=True)
    )
    if topic:
        query = query.ilike("topic", f"%{topic}%")
    if user_id:
        query = query.eq("user_id", user_id)

    resp = query.execute()
    return resp.data if resp.data else []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = load_config()

    parser = argparse.ArgumentParser(description="Research paper pipeline")
    parser.add_argument("--scrape", action="store_true", help="Run SCRAPE workflow only.")
    parser.add_argument("--full", action="store_true", help="Run full pipeline (scrape + debate).")
    parser.add_argument("--topic", type=str, default=None, help=f"Topic (default: {cfg['topic']}).")
    parser.add_argument("--user-id", type=str, default=None, help="User UUID to tag rows with.")
    parser.add_argument("--rounds", type=int, default=2, help="Debate rounds per paper.")
    parser.add_argument("--limit", type=int, default=20, help="Max papers to pull from DB.")
    parser.add_argument("--candidates", type=int, default=50, help="Candidates per scraper source.")
    parser.add_argument("--top-k", type=int, default=20, help="Papers to keep after LLM filter.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    topic = args.topic or cfg["topic"]

    if args.full:
        result = full_pipeline(
            topic=topic,
            user_id=args.user_id,
            candidate_count=args.candidates,
            top_k=args.top_k,
            debate_rounds=args.rounds,
            verbose=args.verbose,
        )
        print(json.dumps(result, indent=2))

    elif args.scrape:
        stored = scrape_and_store(
            topic,
            user_id=args.user_id,
            candidate_count=args.candidates,
            top_k=args.top_k,
        )
        print(f"Stored {len(stored)} papers.")

    else:
        # Debate only
        results = run_debate_pipeline(
            topic=topic,
            user_id=args.user_id,
            debate_rounds=args.rounds,
            limit=args.limit,
            verbose=args.verbose,
        )

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
        print(f"💾  Full results saved to {out_path}")
