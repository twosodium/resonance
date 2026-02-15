"""
Papermint MCP Server — exposes Papermint tools to Poke.

When `npx poke` runs, it starts this server on port 8765 and opens a tunnel
so Poke's cloud AI can call these @mcp.tool() functions from group chats,
iMessage, Slack, etc.

Each function you decorate with @mcp.tool() becomes a "tool" that Poke's
AI agent can decide to call based on what the user asks in the chat.
"""

from __future__ import annotations

import json
import os
import sys
import logging

# ── Make sure the parent project is importable ──────────────────────────
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_DIR, ".env"))

from fastmcp import FastMCP

logger = logging.getLogger("poke-mcp")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# ── FastMCP app ─────────────────────────────────────────────────────────
mcp = FastMCP(
    "Papermint Research",
    instructions=(
        "You are Papermint, an AI research-scouting assistant. "
        "You help users discover promising new research papers and debate "
        "their merits. You can search for papers, run multi-agent debates, "
        "look up past results, and brainstorm follow-up ideas.\n\n"
        "IMPORTANT: If the user hasn't linked their Papermint account yet, "
        "ask them to run `link_account` with the token from their Settings "
        "page first. This lets you access their personal papers and results."
    ),
)


# ── Session state: linked user ──────────────────────────────────────────
# In-memory — resets when the server restarts, which is fine.
_linked_user: dict | None = None  # {user_id, first_name, role, bio}

API_BASE = os.environ.get("PAPERMINT_API_BASE", "http://localhost:5000")


def _get_user_id() -> str | None:
    """Return the linked user_id, or None."""
    return _linked_user["user_id"] if _linked_user else None


def _get_user_context() -> str:
    """Build context string from linked user's profile."""
    if not _linked_user:
        return ""
    parts = []
    if _linked_user.get("role"):
        parts.append(f"Role: {_linked_user['role']}")
    if _linked_user.get("bio"):
        parts.append(f"Bio: {_linked_user['bio']}")
    return "\n".join(parts)


# =====================================================================
#  Tool 0 — Link Papermint account (must be done first)
# =====================================================================
@mcp.tool()
def link_account(token: str) -> str:
    """
    Link your Papermint account so I can access your personal papers
    and search results.

    Go to your Papermint Settings page → "Poke Integration" section →
    click "Generate link token", then paste the token here.

    The token is single-use and expires after 15 minutes.

    Args:
        token: The link token from your Papermint Settings page (starts with "pmint_").

    Returns:
        Confirmation that the account is linked, or an error message.
    """
    global _linked_user
    import httpx

    try:
        resp = httpx.post(
            f"{API_BASE}/api/link-token/verify",
            json={"token": token},
            timeout=10,
        )
        if resp.status_code != 200:
            body = resp.json()
            return f"❌ {body.get('error', 'Invalid token.')}"

        data = resp.json()
        _linked_user = {
            "user_id": data["user_id"],
            "first_name": data.get("first_name", ""),
            "role": data.get("role", ""),
            "bio": data.get("bio", ""),
        }

        name = _linked_user["first_name"] or "there"
        return (
            f"✅ Account linked! Hey {name}! 👋\n"
            f"I can now access your papers and search results.\n\n"
            f"Try asking me to search a topic or show your previous results."
        )
    except Exception as e:
        logger.exception("link_account failed")
        return f"❌ Could not verify token: {e}"


@mcp.tool()
def whoami() -> str:
    """
    Check if a Papermint account is linked, and show the linked user info.

    Returns:
        The linked user's name and role, or a message saying no account is linked.
    """
    if not _linked_user:
        return (
            "No account linked yet.\n"
            "Go to your Papermint Settings page → 'Poke Integration' → "
            "'Generate link token', then use `link_account(token)` here."
        )
    name = _linked_user.get("first_name") or "User"
    role = _linked_user.get("role") or "not set"
    return f"🔗 Linked as **{name}** (role: {role})"


# =====================================================================
#  Tool 1 — Search & Debate (the main thing people will ask for)
# =====================================================================
@mcp.tool()
def research_topic(topic: str) -> str:
    """
    Search for research papers on a topic, then run a multi-agent debate
    (Scout, Advocate, Skeptic, Moderator) on each paper to evaluate its
    promise.

    This is the core Papermint pipeline.  It:
    1. Scrapes papers from arXiv (and optionally bioRxiv / web)
    2. Stores them in the database
    3. Runs a multi-agent debate on each paper
    4. Returns a summary of the most promising papers

    If an account is linked, results are saved to that user's profile.

    Args:
        topic: The research topic to investigate (e.g. "CRISPR gene editing",
               "mechanistic interpretability", "room-temperature superconductors")

    Returns:
        A summary of the pipeline results — how many papers found, debated,
        and the verdicts for the top papers.
    """
    from pipeline import full_pipeline, load_config

    cfg = load_config()
    user_id = _get_user_id()
    user_ctx = _get_user_context()

    try:
        result = full_pipeline(
            topic=topic,
            user_id=user_id,
            user_context=user_ctx,
            candidate_count=cfg.get("candidate_count", 5),
            top_k=cfg.get("top_k", 5),
            debate_rounds=cfg.get("debate_rounds", 2),
        )

        linked_note = ""
        if user_id:
            linked_note = " Results are saved to your account."
        else:
            linked_note = " (Tip: link your account to save results to your profile.)"

        return (
            f"✅ Pipeline complete for \"{topic}\".{linked_note}\n"
            f"• Papers scraped: {result.get('papers_count', 0)}\n"
            f"• Papers debated: {result.get('debates_count', 0)}\n\n"
            f"Use `get_results(\"{topic}\")` to see the detailed verdicts."
        )
    except Exception as e:
        logger.exception("research_topic failed")
        return f"❌ Pipeline failed: {e}"


# =====================================================================
#  Tool 2 — Get results from the database
# =====================================================================
@mcp.tool()
def get_results(topic: str, limit: int = 10) -> str:
    """
    Retrieve debate results for a previously researched topic.

    Returns the verdicts, confidence scores, one-liners, and key strengths/risks
    for each debated paper.  Use this after `research_topic()` has run, or to
    look up past queries.

    Args:
        topic: The research topic to look up.
        limit: Max number of results to return (default 10).

    Returns:
        Formatted list of debate results with paper details.
    """
    from pipeline import _get_supabase

    try:
        sb = _get_supabase()
        query = (
            sb.table("debates")
            .select("*, papers(*)")
            .eq("topic", topic)
            .order("confidence", desc=True)
            .limit(limit)
        )
        user_id = _get_user_id()
        if user_id:
            query = query.eq("user_id", user_id)

        resp = query.execute()
        rows = resp.data or []

        if not rows:
            return f"No results found for topic \"{topic}\".  Try running `research_topic(\"{topic}\")` first."

        # Build a readable summary
        lines = [f"📊 Found {len(rows)} debate result(s) for \"{topic}\":\n"]
        for i, row in enumerate(rows, 1):
            paper = row.get("papers") or {}
            verdict = row.get("verdict", "?")
            conf = row.get("confidence", 0)
            emoji = {"PROMISING": "🟢", "INTERESTING": "🟡", "UNCERTAIN": "🟠", "WEAK": "🔴"}.get(verdict, "⚪")

            lines.append(f"{i}. {emoji} **{paper.get('paper_name', 'Unknown')}**")
            lines.append(f"   Verdict: {verdict} · Confidence: {conf:.0%}")
            if row.get("one_liner"):
                lines.append(f"   → {row['one_liner']}")

            strengths = row.get("key_strengths")
            if strengths:
                if isinstance(strengths, str):
                    try:
                        strengths = json.loads(strengths)
                    except json.JSONDecodeError:
                        strengths = []
                if strengths:
                    lines.append(f"   Strengths: {'; '.join(strengths[:3])}")

            risks = row.get("key_risks")
            if risks:
                if isinstance(risks, str):
                    try:
                        risks = json.loads(risks)
                    except json.JSONDecodeError:
                        risks = []
                if risks:
                    lines.append(f"   Risks: {'; '.join(risks[:3])}")

            if paper.get("url"):
                lines.append(f"   URL: {paper['url']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("get_results failed")
        return f"❌ Error fetching results: {e}"


# =====================================================================
#  Tool 3 — List previously searched topics
# =====================================================================
@mcp.tool()
def list_topics() -> str:
    """
    List all topics that have been researched so far.

    If an account is linked, only shows that user's topics.

    Returns:
        A list of topics with paper counts.
    """
    from pipeline import _get_supabase

    try:
        sb = _get_supabase()
        query = sb.table("papers").select("topic")
        user_id = _get_user_id()
        if user_id:
            query = query.eq("user_id", user_id)

        resp = query.execute()
        rows = resp.data or []

        if not rows:
            return "No topics found yet.  Use `research_topic(\"your topic\")` to get started."

        # Count papers per topic
        topic_counts: dict[str, int] = {}
        for row in rows:
            t = row.get("topic", "unknown")
            topic_counts[t] = topic_counts.get(t, 0) + 1

        lines = ["📚 Previously researched topics:\n"]
        for t, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
            lines.append(f"• {t} ({count} papers)")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("list_topics failed")
        return f"❌ Error: {e}"


# =====================================================================
#  Tool 4 — Debate a single paper on demand
# =====================================================================
@mcp.tool()
def debate_paper(paper_id: int) -> str:
    """
    Run the multi-agent debate on a specific paper that's already in the database.

    Useful for re-evaluating a paper or debating one that was scraped but not
    yet debated.

    Args:
        paper_id: The database ID of the paper to debate.

    Returns:
        The debate verdict and analysis.
    """
    from pipeline import _get_supabase, store_debate, load_config
    from agents import run_debate

    try:
        sb = _get_supabase()
        resp = sb.table("papers").select("*").eq("id", paper_id).single().execute()
        paper = resp.data

        if not paper:
            return f"❌ No paper found with ID {paper_id}."

        cfg = load_config()
        user_ctx = _get_user_context()
        result = run_debate(
            paper,
            num_rounds=cfg.get("debate_rounds", 2),
            user_context=user_ctx,
        )

        # Store in DB
        store_debate(sb, result, user_id=_get_user_id())

        v = result.verdict
        verdict = v.get("verdict", "?")
        emoji = {"PROMISING": "🟢", "INTERESTING": "🟡", "UNCERTAIN": "🟠", "WEAK": "🔴"}.get(verdict, "⚪")

        lines = [
            f"{emoji} **{paper.get('paper_name', 'Unknown')}**",
            f"Verdict: {verdict} · Confidence: {v.get('confidence', 0):.0%}",
        ]
        if v.get("one_liner"):
            lines.append(f"→ {v['one_liner']}")
        if v.get("key_strengths"):
            lines.append(f"Strengths: {'; '.join(v['key_strengths'][:3])}")
        if v.get("key_risks"):
            lines.append(f"Risks: {'; '.join(v['key_risks'][:3])}")
        if v.get("big_ideas"):
            lines.append(f"Big ideas: {'; '.join(v['big_ideas'][:3])}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("debate_paper failed")
        return f"❌ Error: {e}"


# =====================================================================
#  Tool 5 — Quick ask (brainstorm with Claude about a topic/paper)
# =====================================================================
@mcp.tool()
def brainstorm(question: str) -> str:
    """
    Ask a general brainstorming or research question.

    This uses Claude to answer follow-up questions, brainstorm ideas, explain
    concepts, or discuss research directions — without running the full pipeline.
    Good for quick back-and-forth in a group chat.

    Args:
        question: Any research-related question or brainstorming prompt.

    Returns:
        Claude's response.
    """
    try:
        from anthropic import Anthropic

        user_ctx = _get_user_context()
        system = (
            "You are Papermint, a research-scouting AI assistant. "
            "You help users brainstorm research directions, explain "
            "scientific concepts, and evaluate ideas. Be concise and "
            "insightful. Use plain language when possible."
        )
        if user_ctx:
            system = f"{user_ctx}\n\n{system}"

        client = Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text

    except Exception as e:
        logger.exception("brainstorm failed")
        return f"❌ Error: {e}"


# =====================================================================
#  Tool 6 — Get current config
# =====================================================================
@mcp.tool()
def get_config() -> str:
    """
    Show the current Papermint pipeline configuration (candidate count,
    top-k, debate rounds, etc.).

    Returns:
        The current config as JSON.
    """
    from pipeline import load_config

    cfg = load_config()
    return json.dumps(cfg, indent=2)


# =====================================================================
#  Run the server
# =====================================================================
if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8765)
