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
import traceback
import threading

# ── Make sure the parent project is importable ──────────────────────────
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_DIR, ".env"))

from fastmcp import FastMCP

# ── Verbose logging ─────────────────────────────────────────────────────
# Only show DEBUG for our own logger; silence noisy libraries
logging.basicConfig(
    level=logging.WARNING,                       # default: quiet
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("poke-mcp")
logger.setLevel(logging.DEBUG)                   # our logs: verbose
logging.getLogger("docket").setLevel(logging.WARNING)
logging.getLogger("fakeredis").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Silence noisy MCP transport errors (ClientDisconnect from tunnel timeouts)
logging.getLogger("mcp.server.streamable_http").setLevel(logging.CRITICAL)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.CRITICAL)

# ── Startup diagnostics ────────────────────────────────────────────────
logger.info("=" * 60)
logger.info("Papermint MCP Server starting")
logger.info("PROJECT_DIR = %s", PROJECT_DIR)
logger.info("Python       = %s", sys.executable)
logger.info("=" * 60)

# Check critical env vars
_REQUIRED_ENV = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "ANTHROPIC_API_KEY",
]
_OPTIONAL_ENV = [
    "SUPABASE_KEY",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
    "SKIP_BROWSERBASE",
    "PAPERMINT_API_BASE",
]
for var in _REQUIRED_ENV:
    val = os.environ.get(var, "")
    status = "✅ SET" if val.strip() else "❌ MISSING"
    # Show first 8 chars only for security
    preview = val[:8] + "…" if len(val) > 8 else val
    logger.info("  env %-30s %s  (%s)", var, status, preview if val else "")
for var in _OPTIONAL_ENV:
    val = os.environ.get(var, "")
    status = "SET" if val.strip() else "not set"
    logger.info("  env %-30s %s", var, status)

# Check that key imports work
try:
    from supabase import create_client
    logger.info("  import supabase        ✅")
except ImportError as e:
    logger.error("  import supabase        ❌  %s", e)

try:
    from anthropic import Anthropic
    logger.info("  import anthropic       ✅")
except ImportError as e:
    logger.error("  import anthropic       ❌  %s", e)

try:
    import pipeline  # noqa: F401
    logger.info("  import pipeline        ✅")
except Exception as e:
    logger.error("  import pipeline        ❌  %s", e)

try:
    import agents  # noqa: F401
    logger.info("  import agents          ✅")
except Exception as e:
    logger.error("  import agents          ❌  %s", e)

logger.info("=" * 60)


# ── FastMCP app ─────────────────────────────────────────────────────────
mcp = FastMCP(
    "Papermint Research",
    instructions=(
        "You are Papermint, an AI research-scouting assistant. "
        "You help users discover promising new research papers and debate "
        "their merits. You can search for papers, run multi-agent debates, "
        "look up past results, and brainstorm follow-up ideas.\n\n"
        "ACCOUNT LINKING (CRITICAL):\n"
        "- On your VERY FIRST message, call the `whoami` tool to check if an account is linked.\n"
        "- If NOT linked, tell the user exactly this: 'To get started, please link your Papermint account:\n"
        "  1. Open your Papermint dashboard (the website where you signed up)\n"
        "  2. Go to Settings (gear icon in the sidebar)\n"
        "  3. Scroll to the Poke Integration section\n"
        "  4. Click Generate link token\n"
        "  5. Copy the token and paste it here'\n"
        "- Do NOT invent URLs, links, or authentication pages. There is NO external auth URL.\n"
        "- The ONLY way to link is with a token from the Papermint Settings page.\n"
        "- Wait for the user to provide the token, then call `link_account(token)`.\n\n"
        "RESEARCH WORKFLOW:\n"
        "- When the user asks you to research a topic, call `research_topic(topic)`. "
        "It runs in the background.\n"
        "- After starting research, PROACTIVELY call `check_research_status(topic)` "
        "after about 60-90 seconds to see if it's done.\n"
        "- When research is complete, `check_research_status` returns the top results "
        "with paper links — share these with the user immediately.\n"
        "- Results from Poke queries are automatically saved to the user's Papermint "
        "dashboard — mention this so they know.\n\n"
        "OTHER RULES:\n"
        "- NEVER make up or assume any data. Only report what tools actually return.\n"
        "- NEVER invent URLs or links. If you don't know a URL, say so.\n"
        "- If a tool returns an error, show the error to the user.\n"
        "- You can use `brainstorm` without a linked account for general questions."
    ),
)


# ── Session state: linked user ──────────────────────────────────────────
# In-memory — resets when the server restarts, which is fine.
_linked_user: dict | None = None  # {user_id, first_name, role, bio}

API_BASE = os.environ.get("PAPERMINT_API_BASE", "http://localhost:5000")
logger.info("API_BASE = %s", API_BASE)

# ── Background pipeline tracker ─────────────────────────────────────────
# Tracks async research_topic jobs so the tool can return immediately
_bg_jobs: dict[str, dict] = {}  # key = "user_id:topic"


def _get_user_id() -> str | None:
    """Return the linked user_id, or None."""
    uid = _linked_user["user_id"] if _linked_user else None
    logger.debug("_get_user_id() -> %s", uid)
    return uid


def _get_user_context() -> str:
    """Build context string from linked user's profile."""
    if not _linked_user:
        return ""
    parts = []
    if _linked_user.get("role"):
        parts.append(f"Role: {_linked_user['role']}")
    if _linked_user.get("bio"):
        parts.append(f"Bio: {_linked_user['bio']}")
    ctx = "\n".join(parts)
    logger.debug("_get_user_context() -> %r", ctx[:100])
    return ctx


def _require_linked(tool_name: str) -> str | None:
    """Return an error message if user is not linked, else None."""
    if _linked_user:
        return None
    logger.warning("Tool %s called but no account is linked!", tool_name)
    return (
        "❌ No Papermint account linked yet.\n\n"
        "To use this feature, go to your **Papermint Settings** page → "
        "**Poke Integration** → click **Generate link token**, then paste "
        "the token here using `link_account(token)`."
    )


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
    logger.info(">>> link_account called  token=%s…", token[:12] if token else "(empty)")

    import httpx

    try:
        url = f"{API_BASE}/api/link-token/verify"
        logger.info("    POST %s", url)
        resp = httpx.post(url, json={"token": token}, timeout=10)
        logger.info("    Response status=%d  body=%s", resp.status_code, resp.text[:200])

        if resp.status_code != 200:
            body = resp.json()
            err = body.get("error", "Invalid token.")
            logger.warning("    link_account FAILED: %s", err)
            return f"❌ {err}"

        data = resp.json()
        _linked_user = {
            "user_id": data["user_id"],
            "first_name": data.get("first_name", ""),
            "role": data.get("role", ""),
            "bio": data.get("bio", ""),
        }
        logger.info("    ✅ Linked user_id=%s  name=%s  role=%s",
                     _linked_user["user_id"], _linked_user["first_name"], _linked_user.get("role"))

        name = _linked_user["first_name"] or "there"
        return (
            f"✅ Account linked! Hey {name}! 👋\n"
            f"I can now access your papers and search results.\n\n"
            f"Try asking me to search a topic or show your previous results."
        )
    except Exception as e:
        logger.exception("link_account EXCEPTION")
        return f"❌ Could not verify token: {e}"


@mcp.tool()
def whoami() -> str:
    """
    Check if a Papermint account is linked, and show the linked user info.

    Returns:
        The linked user's name and role, or a message saying no account is linked.
    """
    logger.info(">>> whoami called  _linked_user=%s", _linked_user)
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
#  Tool 1 — Search & Debate (non-blocking — starts in background)
# =====================================================================

ALL_SOURCES = ["arxiv", "openalex", "semantic_scholar", "biorxiv", "internet"]


def _run_pipeline_bg(job_key: str, topic: str, user_id: str | None, user_ctx: str, cfg: dict, sources: list[str] | None = None):
    """Background thread that runs the full pipeline and updates _bg_jobs."""
    try:
        _bg_jobs[job_key]["phase"] = "scraping"
        logger.info("    [bg] Starting pipeline for %r  sources=%s", topic, sources)

        from pipeline import full_pipeline
        result = full_pipeline(
            topic=topic,
            user_id=user_id,
            user_context=user_ctx,
            candidate_count=cfg.get("candidate_count", 5),
            top_k=cfg.get("top_k", 5),
            debate_rounds=cfg.get("debate_rounds", 2),
            sources=sources,
        )

        # Fetch the top results so we can include them when the user checks status
        top_summary = _fetch_top_results_summary(topic, user_id, limit=5)

        _bg_jobs[job_key] = {
            "status": "done",
            "phase": "complete",
            "papers_count": result.get("papers_count", 0),
            "debates_count": result.get("debates_count", 0),
            "top_results": top_summary,
        }
        logger.info("    [bg] ✅ Pipeline complete: papers=%s debates=%s",
                     result.get("papers_count"), result.get("debates_count"))
    except Exception as e:
        logger.exception("[bg] Pipeline EXCEPTION")
        _bg_jobs[job_key] = {"status": "error", "phase": "failed", "error": str(e)}


def _fetch_top_results_summary(topic: str, user_id: str | None, limit: int = 5) -> str:
    """Build a readable summary of the top debate results for a topic."""
    try:
        from pipeline import _get_supabase
        sb = _get_supabase()

        query = (
            sb.table("debates")
            .select("*, papers(*)")
            .eq("topic", topic)
            .order("confidence", desc=True)
            .limit(limit)
        )
        if user_id:
            query = query.eq("user_id", user_id)

        resp = query.execute()
        rows = resp.data or []
        if not rows:
            return "No debate results found."

        lines = []
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
                    lines.append(f"   Strengths: {'; '.join(str(s) for s in strengths[:3])}")

            if paper.get("url"):
                lines.append(f"   🔗 {paper['url']}")
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        logger.warning("_fetch_top_results_summary failed: %s", e)
        return f"(Could not fetch results: {e})"


@mcp.tool()
def research_topic(topic: str, sources: str = "") -> str:
    """
    Search for research papers on a topic, then run a multi-agent debate
    (Scout, Advocate, Skeptic, Moderator) on each paper to evaluate its
    promise.

    By default all available sources are used (arXiv, OpenAlex, Semantic
    Scholar, bioRxiv, internet).  You can restrict to specific sources by
    passing a comma-separated list.

    This kicks off the pipeline in the background and returns immediately.
    Use `check_research_status(topic)` to check progress — once done it
    will include the top ranked results automatically.

    Results are also saved to the user's Papermint dashboard.

    Requires a linked account.

    Args:
        topic: The research topic to investigate (e.g. "CRISPR gene editing",
               "mechanistic interpretability", "room-temperature superconductors")
        sources: Optional comma-separated list of sources to use.  Defaults
                 to all: "arxiv,openalex,semantic_scholar,biorxiv,internet"

    Returns:
        Confirmation that the search has started (it runs in the background).
    """
    logger.info(">>> research_topic called  topic=%r  sources=%r", topic, sources)

    err = _require_linked("research_topic")
    if err:
        return err

    from pipeline import load_config

    cfg = load_config()
    user_id = _get_user_id()
    user_ctx = _get_user_context()
    job_key = f"{user_id}:{topic}"

    # Check if already running
    existing = _bg_jobs.get(job_key, {})
    if existing.get("status") == "running":
        phase = existing.get("phase", "working")
        return f"⏳ Already researching \"{topic}\" (currently {phase}). Use `check_research_status(\"{topic}\")` to check progress."

    # Parse sources — default to ALL
    if sources and sources.strip():
        src_list = [s.strip() for s in sources.split(",") if s.strip()]
    else:
        src_list = list(ALL_SOURCES)

    logger.info("    Starting background pipeline  user_id=%s  sources=%s", user_id, src_list)

    _bg_jobs[job_key] = {"status": "running", "phase": "starting"}

    t = threading.Thread(
        target=_run_pipeline_bg,
        args=(job_key, topic, user_id, user_ctx, cfg, src_list),
        daemon=True,
    )
    t.start()

    src_names = ", ".join(src_list)
    return (
        f"🔍 Started researching \"{topic}\"! This takes 1-3 minutes.\n\n"
        f"Scraping from: {src_names}\n"
        f"Then running multi-agent debates on each paper found.\n\n"
        f"Use `check_research_status(\"{topic}\")` to check progress — "
        f"once done I'll show you the top ranked results.\n\n"
        f"💡 These results will also appear on your Papermint dashboard."
    )


@mcp.tool()
def check_research_status(topic: str) -> str:
    """
    Check the progress of a background research pipeline.

    Use this after calling `research_topic(topic)` to see if it's done.
    When the pipeline is complete, the response will include the top-ranked
    results automatically (with links).

    Args:
        topic: The topic you started researching.

    Returns:
        Current status: running, done (with results), or error.
    """
    logger.info(">>> check_research_status called  topic=%r", topic)

    user_id = _get_user_id()
    job_key = f"{user_id}:{topic}"

    job = _bg_jobs.get(job_key)
    if not job:
        return f"No research job found for \"{topic}\". Use `research_topic(\"{topic}\")` to start one."

    status = job.get("status", "unknown")
    if status == "running":
        phase = job.get("phase", "working")
        return f"⏳ Still researching \"{topic}\" — currently **{phase}**. Check back in a minute!"
    elif status == "done":
        header = (
            f"✅ Research complete for \"{topic}\"!\n"
            f"• Papers scraped: {job.get('papers_count', 0)}\n"
            f"• Papers debated: {job.get('debates_count', 0)}\n\n"
            f"💡 These results are now visible on your Papermint dashboard too.\n\n"
            f"**Top ranked results:**\n\n"
        )
        top = job.get("top_results", "")
        if not top:
            # Fallback: fetch fresh if not cached
            top = _fetch_top_results_summary(topic, user_id, limit=5)
        return header + top
    elif status == "error":
        return f"❌ Research failed for \"{topic}\": {job.get('error', 'Unknown error')}"
    else:
        return f"Status: {status}"


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

    Requires a linked account.

    Args:
        topic: The research topic to look up.
        limit: Max number of results to return (default 10).

    Returns:
        Formatted list of debate results with paper details.
    """
    logger.info(">>> get_results called  topic=%r  limit=%d", topic, limit)

    err = _require_linked("get_results")
    if err:
        return err

    from pipeline import _get_supabase

    try:
        sb = _get_supabase()
        user_id = _get_user_id()
        logger.info("    Querying debates for topic=%r user_id=%s", topic, user_id)

        query = (
            sb.table("debates")
            .select("*, papers(*)")
            .eq("topic", topic)
            .order("confidence", desc=True)
            .limit(limit)
        )
        if user_id:
            query = query.eq("user_id", user_id)

        resp = query.execute()
        rows = resp.data or []
        logger.info("    Got %d debate rows", len(rows))

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
        logger.exception("get_results EXCEPTION")
        return f"❌ Error fetching results: {e}\n\nFull traceback:\n{traceback.format_exc()}"


# =====================================================================
#  Tool 3 — List previously searched topics
# =====================================================================
@mcp.tool()
def list_topics() -> str:
    """
    List all topics that have been researched by the linked user.

    Requires a linked account.

    Returns:
        A list of topics with paper counts.
    """
    logger.info(">>> list_topics called")

    err = _require_linked("list_topics")
    if err:
        return err

    from pipeline import _get_supabase

    try:
        sb = _get_supabase()
        user_id = _get_user_id()
        logger.info("    Querying papers for user_id=%s", user_id)

        query = sb.table("papers").select("topic")
        if user_id:
            query = query.eq("user_id", user_id)

        resp = query.execute()
        rows = resp.data or []
        logger.info("    Got %d paper rows", len(rows))

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
        logger.exception("list_topics EXCEPTION")
        return f"❌ Error: {e}\n\nFull traceback:\n{traceback.format_exc()}"


# =====================================================================
#  Tool 4 — Debate a single paper on demand
# =====================================================================
@mcp.tool()
def debate_paper(paper_id: int) -> str:
    """
    Run the multi-agent debate on a specific paper that's already in the database.

    Useful for re-evaluating a paper or debating one that was scraped but not
    yet debated.

    Requires a linked account.

    Args:
        paper_id: The database ID of the paper to debate.

    Returns:
        The debate verdict and analysis.
    """
    logger.info(">>> debate_paper called  paper_id=%d", paper_id)

    err = _require_linked("debate_paper")
    if err:
        return err

    from pipeline import _get_supabase, store_debate, load_config
    from agents import run_debate

    try:
        sb = _get_supabase()
        logger.info("    Fetching paper id=%d", paper_id)
        resp = sb.table("papers").select("*").eq("id", paper_id).single().execute()
        paper = resp.data

        if not paper:
            logger.warning("    Paper id=%d not found", paper_id)
            return f"❌ No paper found with ID {paper_id}."

        logger.info("    Paper found: %s", (paper.get("paper_name") or "?")[:60])
        cfg = load_config()
        user_ctx = _get_user_context()

        logger.info("    Running debate (rounds=%d)…", cfg.get("debate_rounds", 2))
        result = run_debate(
            paper,
            num_rounds=cfg.get("debate_rounds", 2),
            user_context=user_ctx,
        )

        logger.info("    Storing debate result…")
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

        logger.info("    ✅ Debate complete: verdict=%s conf=%.2f", verdict, v.get("confidence", 0))
        return "\n".join(lines)

    except Exception as e:
        logger.exception("debate_paper EXCEPTION")
        return f"❌ Error: {e}\n\nFull traceback:\n{traceback.format_exc()}"


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

    Does not require a linked account.

    Args:
        question: Any research-related question or brainstorming prompt.

    Returns:
        Claude's response.
    """
    logger.info(">>> brainstorm called  question=%r", question[:100])

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

        logger.info("    Calling Claude (claude-haiku-4-5-20251001)…")
        client = Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        answer = resp.content[0].text
        logger.info("    ✅ Got response (%d chars)", len(answer))
        return answer

    except Exception as e:
        logger.exception("brainstorm EXCEPTION")
        return f"❌ Error: {e}\n\nFull traceback:\n{traceback.format_exc()}"


# =====================================================================
#  Tool 6 — Get current config
# =====================================================================
@mcp.tool()
def get_config() -> str:
    """
    Show the current Papermint pipeline configuration (candidate count,
    top-k, debate rounds, etc.).

    Does not require a linked account.

    Returns:
        The current config as JSON.
    """
    logger.info(">>> get_config called")
    from pipeline import load_config

    cfg = load_config()
    logger.info("    Config: %s", json.dumps(cfg))
    return json.dumps(cfg, indent=2)


# =====================================================================
#  Run the server
# =====================================================================
if __name__ == "__main__":
    logger.info("Starting MCP server on 0.0.0.0:8765")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8765)
