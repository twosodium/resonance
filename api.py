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
import threading
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from pipeline import full_pipeline, load_config, save_config  # noqa: E402 (must be after dotenv)

logger = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# Anthropic client for paper chat — lazy init
_chat_client = None

def _get_chat_client():
    global _chat_client
    if _chat_client is None:
        from anthropic import Anthropic
        _chat_client = Anthropic()
    return _chat_client

# In-memory paper chat histories: { paper_id: [ {role, content} ] }
_paper_chats: dict[int, list[dict]] = {}
_paper_chats_lock = threading.Lock()

app = Flask(__name__)
CORS(app)  # allow frontend on any origin during dev

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

    def _run():
        try:
            _set_job(job_key, "scraping")
            result = full_pipeline(
                topic=topic,
                user_id=user_id,
                on_phase=lambda phase: _set_job(job_key, phase),
            )
            _set_job(
                job_key, "complete",
                papers_count=result.get("papers_count", 0),
                debates_count=result.get("debates_count", 0),
            )
        except Exception as exc:
            logger.error("Pipeline failed for %s: %s", topic, traceback.format_exc())
            _set_job(job_key, "error", error=str(exc))

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
    return jsonify({
        "topic": cfg.get("topic", ""),
        "multiplier": cfg.get("multiplier", 3),
        "candidate_count": cfg.get("candidate_count", 5),
        "top_k": cfg.get("top_k", 5),
        "debate_rounds": cfg.get("debate_rounds", 2),
        "skip_browserbase": os.environ.get("SKIP_BROWSERBASE", "") in ("1", "true", "yes"),
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_browserbase_key": bool(os.environ.get("BROWSERBASE_API_KEY")),
    })


@app.route("/api/settings", methods=["PUT"])
def put_settings():
    """Update pipeline config and env toggles."""
    data = request.get_json(silent=True) or {}

    cfg = load_config()

    # Config file fields
    for key in ("topic", "multiplier", "candidate_count", "top_k", "debate_rounds"):
        if key in data:
            cfg[key] = data[key]
    save_config(cfg)

    # Env-level toggles (persist for the running process)
    if "skip_browserbase" in data:
        os.environ["SKIP_BROWSERBASE"] = "1" if data["skip_browserbase"] else ""

    # API keys (only set if provided & non-empty — never echo them back)
    if data.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = data["anthropic_api_key"]
    if data.get("browserbase_api_key"):
        os.environ["BROWSERBASE_API_KEY"] = data["browserbase_api_key"]
    if data.get("browserbase_project_id"):
        os.environ["BROWSERBASE_PROJECT_ID"] = data["browserbase_project_id"]

    return jsonify({"ok": True, **cfg})


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
# Health check
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 5000))
    logger.info("Starting Papermint API on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=True)

