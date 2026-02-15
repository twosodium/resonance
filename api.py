"""
api.py — Flask backend API for Papermint.

Endpoints
---------
POST /api/search   — Kick off scrape + debate pipeline for a topic.
GET  /api/status    — Check the status of a running pipeline job.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import traceback
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, request
from flask_cors import CORS

# Load .env from project root
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)

from pipeline import full_pipeline, load_config, save_config  # noqa: E402 (must be after dotenv)
from supabase import create_client as _create_client  # noqa: E402

logger = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# Anthropic client for paper chat — rebuilt whenever the API key changes
_chat_client = None
_chat_client_key: str | None = None  # tracks which key the client was built with

def _get_chat_client():
    global _chat_client, _chat_client_key
    current_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if _chat_client is None or current_key != _chat_client_key:
        from anthropic import Anthropic
        _chat_client = Anthropic(api_key=current_key)
        _chat_client_key = current_key
    return _chat_client

# In-memory paper chat histories: { paper_id: [ {role, content} ] }
_paper_chats: dict[int, list[dict]] = {}
_paper_chats_lock = threading.Lock()

app = Flask(__name__)
CORS(app)  # allow frontend on any origin during dev


def _get_sb():
    """Return a Supabase client for profile lookups."""
    return _create_client(
        os.environ.get("SUPABASE_URL", ""),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", ""),
    )


def _build_user_context(user_id: str | None) -> str:
    """Fetch user profile and return a short context string for the agents."""
    if not user_id:
        return ""
    try:
        sb = _get_sb()
        resp = sb.table("profiles").select("role, bio").eq("id", user_id).single().execute()
        profile = resp.data if resp.data else {}
    except Exception:
        return ""

    parts = []
    role = (profile.get("role") or "").strip()
    bio = (profile.get("bio") or "").strip()
    if role:
        parts.append(f"Role: {role}")
    if bio:
        parts.append(f"Bio: {bio}")
    return "\n".join(parts)

# ---------------------------------------------------------------------------
# In-memory job tracker  (topic -> status dict)
# For a hackathon this is fine; production would use Redis / DB.
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(key: str, status: str, **extra):
    with _jobs_lock:
        _jobs[key] = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **extra}


def _get_job(key: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(key, {"status": "unknown"}))


_cancel_requested: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# POST /api/search/cancel — request cancel of running pipeline
# ---------------------------------------------------------------------------

@app.route("/api/search/cancel", methods=["POST"])
def search_cancel():
    """Set cancel flag for the job so the pipeline stops after current phase."""
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    user_id = (data.get("user_id") or "").strip() or None
    if not topic:
        return jsonify({"error": "topic is required"}), 400
    job_key = f"{user_id or 'anon'}:{topic}"
    with _jobs_lock:
        _cancel_requested[job_key] = True
    _set_job(job_key, "cancelled")
    return jsonify({"ok": True, "status": "cancelled"})


# ---------------------------------------------------------------------------
# POST /api/search
# Body: { "topic": "...", "user_id": "..." }
# ---------------------------------------------------------------------------

@app.route("/api/search", methods=["POST"])
def search():
    """Start pipeline: user synthesis query (or topic) → Claude summarizes → fast scrape → Supabase → debate."""
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or data.get("query") or "").strip()
    user_id = (data.get("user_id") or "").strip() or None

    if not topic:
        return jsonify({"error": "topic or query is required"}), 400

    job_key = f"{user_id or 'anon'}:{topic}"

    # Don't start duplicate jobs
    current = _get_job(job_key)
    if current.get("status") in ("scraping", "debating"):
        return jsonify({"status": current["status"], "topic": topic, "message": "Pipeline already running."})

    _set_job(job_key, "scraping")

    # Build user context once before spawning the thread
    user_ctx = _build_user_context(user_id)

    def _run():
        from pipeline import full_pipeline
        try:
            _set_job(job_key, "scraping")
            cancel_check = lambda: _cancel_requested.get(job_key)
            result = full_pipeline(
                topic=topic,
                user_id=user_id,
                user_context=user_ctx,
                on_phase=lambda phase, **kw: _set_job(job_key, phase, **kw),
                cancel_check=cancel_check,
            )
            with _jobs_lock:
                _cancel_requested.pop(job_key, None)
            if _get_job(job_key).get("status") == "cancelled":
                return
            if result is None:
                _set_job(job_key, "cancelled")
                return
            _set_job(
                job_key, "complete",
                papers_count=result.get("papers_count", 0),
                debates_count=result.get("debates_count", 0),
            )
        except Exception as exc:
            logger.error("Pipeline failed for %s: %s", topic, traceback.format_exc())
            if _get_job(job_key).get("status") != "cancelled":
                _set_job(job_key, "error", error=str(exc))
        finally:
            with _jobs_lock:
                _cancel_requested.pop(job_key, None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"status": "started", "topic": topic})


# ---------------------------------------------------------------------------
# GET /api/status?topic=...&user_id=...
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def status():
    topic = request.args.get("topic", "").strip()
    user_id = request.args.get("user_id", "").strip() or None

    if not topic:
        return jsonify({"error": "topic query param is required"}), 400

    job_key = f"{user_id or 'anon'}:{topic}"
    return jsonify(_get_job(job_key))


# ---------------------------------------------------------------------------
# GET/PUT /api/settings — read / update user settings
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return current pipeline config + env-level toggles."""
    cfg = load_config()
    default_sources = ["arxiv", "biorxiv", "openalex", "semantic_scholar", "internet"]
    return jsonify({
        "topic": cfg.get("topic", ""),
        "candidate_count": cfg.get("candidate_count", 5),
        "top_k": cfg.get("top_k", 5),
        "debate_rounds": cfg.get("debate_rounds", 2),
        "sources": cfg.get("sources", default_sources),
        "skip_browserbase": os.environ.get("SKIP_BROWSERBASE", "") in ("1", "true", "yes"),
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_browserbase_key": bool(os.environ.get("BROWSERBASE_API_KEY")),
    })


@app.route("/api/settings", methods=["PUT"])
def put_settings():
    """Update pipeline config and env toggles.

    Pipeline params are saved to ``config.json``.
    API keys and toggles are written to both ``os.environ`` (immediate
    effect) **and** the ``.env`` file (survives server restarts).
    """
    data = request.get_json(silent=True) or {}

    cfg = load_config()

    # Config file fields
    for key in ("topic", "candidate_count", "top_k", "debate_rounds"):
        if key in data:
            cfg[key] = data[key]
    allowed_sources = {"arxiv", "biorxiv", "openalex", "semantic_scholar", "internet"}
    if "sources" in data and isinstance(data["sources"], list):
        cfg["sources"] = [s for s in data["sources"] if s in allowed_sources]
        if not cfg["sources"]:
            cfg["sources"] = list(allowed_sources)
    save_config(cfg)

    # Env-level toggles — persist to both os.environ AND .env file
    if "skip_browserbase" in data:
        val = "1" if data["skip_browserbase"] else ""
        os.environ["SKIP_BROWSERBASE"] = val
        set_key(_ENV_PATH, "SKIP_BROWSERBASE", val)

    # API keys — write to env + .env file (only if non-empty)
    if data.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = data["anthropic_api_key"]
        set_key(_ENV_PATH, "ANTHROPIC_API_KEY", data["anthropic_api_key"])
    if data.get("browserbase_api_key"):
        os.environ["BROWSERBASE_API_KEY"] = data["browserbase_api_key"]
        set_key(_ENV_PATH, "BROWSERBASE_API_KEY", data["browserbase_api_key"])
    if data.get("browserbase_project_id"):
        os.environ["BROWSERBASE_PROJECT_ID"] = data["browserbase_project_id"]
        set_key(_ENV_PATH, "BROWSERBASE_PROJECT_ID", data["browserbase_project_id"])

    return jsonify({"ok": True, **cfg})


# ---------------------------------------------------------------------------
# GET/PUT /api/profile — read / update user role & bio
# ---------------------------------------------------------------------------

@app.route("/api/profile", methods=["GET"])
def get_profile_api():
    """Return the current user's role and bio from the profiles table."""
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id query param is required"}), 400

    try:
        sb = _get_sb()
        resp = sb.table("profiles").select("role, bio, first_name, last_name").eq("id", user_id).single().execute()
        profile = resp.data if resp.data else {}
        return jsonify({
            "role": profile.get("role", ""),
            "bio": profile.get("bio", ""),
            "first_name": profile.get("first_name", ""),
            "last_name": profile.get("last_name", ""),
        })
    except Exception as exc:
        logger.error("get_profile failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/profile", methods=["PUT"])
def put_profile_api():
    """Update the current user's role and bio."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    update: dict = {}
    if "role" in data:
        update["role"] = (data["role"] or "").strip()
    if "bio" in data:
        update["bio"] = (data["bio"] or "").strip()
    if "first_name" in data:
        update["first_name"] = (data["first_name"] or "").strip()
    if "last_name" in data:
        update["last_name"] = (data["last_name"] or "").strip()

    if not update:
        return jsonify({"ok": True})

    try:
        sb = _get_sb()
        sb.table("profiles").update(update).eq("id", user_id).execute()
        return jsonify({"ok": True, **update})
    except Exception as exc:
        logger.error("put_profile failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/link-token — generate a one-time token for Poke account linking
# GET  /api/link-token/verify — verify a token and return the user_id
# ---------------------------------------------------------------------------

@app.route("/api/link-token", methods=["POST"])
def create_link_token():
    """Generate a short-lived, single-use token that maps to a user_id.

    The user copies this token from the Settings page and pastes it into
    a Poke group chat.  The MCP server calls ``/api/link-token/verify``
    to exchange the token for the ``user_id``, then discards the token.
    """
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    token = "pmint_" + secrets.token_urlsafe(16)          # e.g. pmint_a8f3c2...
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)

    try:
        sb = _get_sb()
        sb.table("profiles").update({
            "link_token": token,
            "link_token_expires": expires.isoformat(),
        }).eq("id", user_id).execute()
    except Exception as exc:
        logger.error("create_link_token failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"token": token, "expires": expires.isoformat()})


@app.route("/api/link-token/verify", methods=["POST"])
def verify_link_token():
    """Exchange a link token for the user_id.  Consumed on first use."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token is required"}), 400

    try:
        sb = _get_sb()
        resp = (
            sb.table("profiles")
            .select("id, first_name, role, bio, link_token_expires")
            .eq("link_token", token)
            .single()
            .execute()
        )
        profile = resp.data
    except Exception:
        return jsonify({"error": "Invalid or expired token."}), 401

    if not profile:
        return jsonify({"error": "Invalid or expired token."}), 401

    # Check expiry
    exp_str = profile.get("link_token_expires", "")
    if exp_str:
        exp = datetime.fromisoformat(exp_str)
        if datetime.now(timezone.utc) > exp:
            return jsonify({"error": "Token has expired. Generate a new one from Settings."}), 401

    # Consume the token — set it to NULL so it can't be reused
    try:
        sb.table("profiles").update({
            "link_token": None,
            "link_token_expires": None,
        }).eq("id", profile["id"]).execute()
    except Exception:
        pass  # best-effort cleanup

    return jsonify({
        "user_id": profile["id"],
        "first_name": profile.get("first_name", ""),
        "role": profile.get("role", ""),
        "bio": profile.get("bio", ""),
    })


# ---------------------------------------------------------------------------
# POST /api/papers/<paper_id>/chat — per-paper conversation with Claude
# Body: { "message": "...", "paper": { ...paper data... } }
# ---------------------------------------------------------------------------

@app.route("/api/papers/<int:paper_id>/chat", methods=["POST"])
def paper_chat(paper_id: int):
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    paper = data.get("paper") or {}

    if not user_message:
        return jsonify({"error": "message is required"}), 400

    with _paper_chats_lock:
        history = _paper_chats.get(paper_id)
        if history is None:
            # Build system context from the paper
            paper_context = []
            if paper.get("paper_name"):
                paper_context.append(f"Title: {paper['paper_name']}")
            if paper.get("paper_authors"):
                authors = paper["paper_authors"]
                if isinstance(authors, list):
                    authors = ", ".join(authors)
                paper_context.append(f"Authors: {authors}")
            if paper.get("abstract"):
                paper_context.append(f"Abstract: {paper['abstract']}")
            if paper.get("journal"):
                paper_context.append(f"Journal: {paper['journal']}")
            if paper.get("published"):
                paper_context.append(f"Published: {paper['published']}")
            if paper.get("url"):
                paper_context.append(f"URL: {paper['url']}")
            if paper.get("fulltext"):
                paper_context.append(f"Full text:\n{paper['fulltext'][:4000]}")

            history = [{
                "role": "system",
                "content": (
                    "You are a research assistant helping a user explore a specific "
                    "academic paper. Answer follow-up questions about the paper, "
                    "suggest experiments, explain concepts, and brainstorm applications.\n\n"
                    "PAPER DETAILS:\n" + "\n".join(paper_context)
                ),
            }]
            _paper_chats[paper_id] = history

    # Build messages for Claude (strip 'system' into system param)
    system_prompt = history[0]["content"] if history and history[0]["role"] == "system" else ""
    messages = [m for m in history if m["role"] != "system"]
    messages.append({"role": "user", "content": user_message})

    try:
        client = _get_chat_client()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        )
        reply = resp.content[0].text
    except Exception as exc:
        logger.error("Paper chat failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    # Save to history
    with _paper_chats_lock:
        _paper_chats[paper_id].append({"role": "user", "content": user_message})
        _paper_chats[paper_id].append({"role": "assistant", "content": reply})

    return jsonify({"reply": reply})


@app.route("/api/papers/<int:paper_id>/chat", methods=["DELETE"])
def clear_paper_chat(paper_id: int):
    """Clear conversation history for a paper."""
    with _paper_chats_lock:
        _paper_chats.pop(paper_id, None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# DELETE /api/papers/<paper_id> — delete a paper (and its debates)
# ---------------------------------------------------------------------------

@app.route("/api/papers/<int:paper_id>", methods=["DELETE"])
def delete_paper(paper_id: int):
    try:
        from supabase import create_client
        sb = create_client(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", ""),
        )
        # Delete debates first (FK constraint)
        sb.table("debates").delete().eq("paper_id", paper_id).execute()
        sb.table("papers").delete().eq("id", paper_id).execute()
        return jsonify({"ok": True, "deleted_paper_id": paper_id})
    except Exception as exc:
        logger.error("Delete paper %d failed: %s", paper_id, exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/ideas/mindmap — get related-ideas graph (edges + similarity labels)
# ---------------------------------------------------------------------------

@app.route("/api/ideas/mindmap", methods=["POST"])
def ideas_mindmap():
    """Given a list of ideas (id, paper_name, topic, one_liner), return edges with similarity labels via Claude."""
    data = request.get_json(silent=True) or {}
    ideas = data.get("ideas") or []
    if not ideas or len(ideas) < 2:
        return jsonify({"edges": []})

    # Build a short list for the prompt
    items = []
    for i, idea in enumerate(ideas[:50]):  # cap at 50
        idea_id = idea.get("id") or idea.get("paper_id") or str(i)
        name = (idea.get("paper_name") or idea.get("topic") or "Unknown")[:120]
        oneliner = (idea.get("one_liner") or "")[:200]
        items.append({"id": str(idea_id), "title": name, "one_liner": oneliner})

    prompt = (
        "You are given a list of research ideas/papers. "
        "For each pair that shares a deeper intellectual connection, output one edge. "
        "The label should describe the SPECIFIC conceptual bridge or overlap between them in at most 7 words — "
        "for example 'shared attention mechanism', 'both target protein misfolding', 'complementary imaging modalities'. "
        "Do NOT use the search topic name as the label. Be creative, specific, and concise (≤7 words per label). "
        "Only include genuinely meaningful connections (not every pair). "
        "Return valid JSON only, no markdown fences, in this exact format:\n"
        '{"edges": [{"from_id": "<id>", "to_id": "<id>", "label": "<specific conceptual bridge>"}, ...]}'
        "\n\nIdeas:\n"
        + "\n".join(
            f"- id={it['id']} | {it['title']}"
            + (f" | {it['one_liner']}" if it.get("one_liner") else "")
            + (f" | topic={idea.get('topic','')}" if (idea := ideas[i] if i < len(ideas) else {}).get("topic") else "")
            for i, it in enumerate(items)
        )
    )

    try:
        client = _get_chat_client()
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown code block if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        import json as _json
        out = _json.loads(text)
        edges = out.get("edges") or []
        # Normalize to from_id, to_id, label
        for e in edges:
            e.setdefault("from_id", str(e.get("from_id", "")))
            e.setdefault("to_id", str(e.get("to_id", "")))
            e.setdefault("label", e.get("label", "related"))
        return jsonify({"edges": edges})
    except Exception as exc:
        logger.error("Mindmap edges failed: %s", exc)
        return jsonify({"edges": []})


# ---------------------------------------------------------------------------
# DELETE /api/user/topics — clear all papers and debates for a user
# ---------------------------------------------------------------------------

@app.route("/api/user/topics", methods=["DELETE"])
def clear_user_topics():
    """Delete all papers and debates for the given user (clears previous topics)."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip() or None
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    try:
        from supabase import create_client
        sb = create_client(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", ""),
        )
        sb.table("debates").delete().eq("user_id", user_id).execute()
        sb.table("papers").delete().eq("user_id", user_id).execute()
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Clear user topics failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 5000))
    logger.info("Starting Papermint API on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=True)

