"""
Research paper harness: arXiv (API), bioRxiv and web (Stagehand/Browserbase), then Anthropic filter.
Optional Supabase upsert. Output: JSON (topic, paper_name, paper_authors, published, journal, abstract, fulltext, url).
"""

import argparse
import asyncio
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

# Load .env from the directory containing this script (so it works regardless of cwd)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(_SCRIPT_DIR, ".env"))

logger = logging.getLogger("research_harness")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"


@dataclass
class Paper:
    """Research paper with metadata; abstract from arXiv API; full_text from PDF or scrape."""

    title: str
    authors: list[str]
    journal: str
    url: str
    source: str  # "arxiv" | "biorxiv" | "internet"
    published_date: str | None = None
    abstract: str | None = None
    full_text: str | None = None
    pdf_url: str | None = None  # set from arXiv API for reliable PDF fetch


def _sanitize_for_db(s: str | None) -> str | None:
    """Remove null bytes and other control chars that PostgreSQL text rejects (e.g. \\u0000)."""
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    return "".join(c for c in s if c != "\x00" and (ord(c) >= 32 or c in "\n\r\t"))


def fetch_arxiv(query: str, max_results: int = 20) -> list[Paper]:
    """Query the free arXiv API; results are requested newest-first by submission date. Retries on 429/503 with backoff."""
    papers: list[Paper] = []
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = "https://export.arxiv.org/api/query"
    headers = {"User-Agent": "arxiv-py/1.0 (https://arxiv.org/help/api)"}
    resp = None
    for attempt in range(3):
        resp = requests.get(url, params=params, timeout=30, headers=headers)
        if resp.status_code in (429, 503):
            wait = (5, 15, 30)[min(attempt, 2)]
            logger.warning(
                "arXiv API rate limit (429/503); waiting %ds before retry %d/3.",
                wait,
                attempt + 1,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    if resp is None or resp.status_code in (429, 503):
        if resp is not None:
            resp.raise_for_status()
        raise RuntimeError("arXiv API rate limit: try again in a few minutes.")

    root = ET.fromstring(resp.content)

    for entry in root.findall(f".//{{{ATOM}}}entry"):
        title_el = entry.find(f"{{{ATOM}}}title")
        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""

        authors_list = []
        for author in entry.findall(f"{{{ATOM}}}author"):
            name_el = author.find(f"{{{ATOM}}}name")
            if name_el is not None and name_el.text:
                authors_list.append(name_el.text.strip())

        journal_el = entry.find(f"{{{ARXIV}}}journal_ref")
        journal = (journal_el.text or "").strip() if journal_el is not None and journal_el.text else "arXiv"

        url = ""
        pdf_url = None
        for link in entry.findall(f"{{{ATOM}}}link"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if link.get("rel") == "alternate" and not url:
                url = href
            if (link.get("type") or "").strip().lower() == "application/pdf":
                pdf_url = href
        if not url:
            id_el = entry.find(f"{{{ATOM}}}id")
            if id_el is not None and id_el.text:
                url = id_el.text.strip()

        # Publication date: prefer atom:published, else atom:updated
        published_date = None
        for tag in ("published", "updated"):
            el = entry.find(f"{{{ATOM}}}{tag}")
            if el is not None and el.text:
                # Atom dates are ISO 8601; take date part only
                published_date = (el.text.strip() or "").split("T")[0] or None
                if published_date:
                    break

        abstract_el = entry.find(f"{{{ATOM}}}summary")
        abstract = (abstract_el.text or "").strip().replace("\n", " ")[:8000] if abstract_el is not None and abstract_el.text else None

        if title and url:
            papers.append(
                Paper(
                    title=title,
                    authors=authors_list,
                    journal=journal,
                    url=url,
                    source="arxiv",
                    published_date=published_date,
                    abstract=abstract,
                    pdf_url=pdf_url,
                )
            )
    return papers


def _arxiv_abs_url_to_pdf_url(abs_url: str) -> str | None:
    """Convert arXiv abstract URL to PDF URL. Handles old IDs with slash (e.g. hep-th/9901001)."""
    if not abs_url or "arxiv.org" not in abs_url:
        return None
    # Capture path after /abs/ or /pdf/ (ID may contain slash for old papers)
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+)", abs_url, re.IGNORECASE)
    if m:
        pid = m.group(1).strip().rstrip("/").replace(".pdf", "").strip()
        if pid:
            return f"https://arxiv.org/pdf/{pid}.pdf"
    return None


def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str | None:
    """Extract raw text from PDF bytes. Tries PyMuPDF first, then pypdf. Returns None on failure."""
    # 1) PyMuPDF (fitz) - try direct bytes then temp file
    try:
        import fitz
        doc = None
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(pdf_bytes)
                tmp = f.name
            try:
                doc = fitz.open(tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if doc is not None:
            parts = []
            for page in doc:
                parts.append(page.get_text("text") or "")
            doc.close()
            out = "\n".join(parts).strip()
            if out:
                return out
    except ImportError:
        pass
    except Exception:
        pass
    # 2) pypdf fallback
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            parts.append((page.extract_text() or "").strip())
        out = "\n".join(p for p in parts if p).strip()
        if out:
            return out
    except ImportError:
        pass
    except Exception:
        pass
    return None


def _fetch_arxiv_pdf_fulltext(paper: Paper) -> Paper:
    """
    Fetch the arXiv PDF for this paper (source=arxiv) and extract full text as a string.
    Sets full_text on the returned Paper so it appears in JSON as fulltext.
    """
    if paper.source != "arxiv" or not paper.url:
        return paper
    pdf_url = getattr(paper, "pdf_url", None) or _arxiv_abs_url_to_pdf_url(paper.url)
    if not pdf_url:
        logger.warning("Could not determine PDF URL for arXiv paper: %s", paper.url)
        return paper
    if pdf_url.startswith("http://"):
        pdf_url = "https://" + pdf_url[7:]
    try:
        r = requests.get(pdf_url, timeout=45, headers={"User-Agent": "arxiv-py/1.0 (https://arxiv.org/help/api)"})
        r.raise_for_status()
        pdf_bytes = r.content
        if len(pdf_bytes) < 200:
            logger.warning("arXiv response too small (%d bytes), likely not a PDF: %s", len(pdf_bytes), pdf_url[:50])
            return paper
        # Allow any response that looks like PDF (magic bytes) or has pdf in content-type
        ct = (r.headers.get("Content-Type") or "").lower()
        if "application/pdf" not in ct and not pdf_bytes.startswith(b"%PDF"):
            logger.warning("Response may not be PDF (Content-Type: %s): %s", ct[:30], pdf_url[:50])
    except Exception as e:
        logger.warning("Could not fetch arXiv PDF %s: %s", pdf_url[:60], e)
        return paper

    full_text = _extract_text_from_pdf_bytes(pdf_bytes)
    if full_text:
        full_text = full_text.strip()
    if full_text:
        logger.info("Extracted %d chars full text from arXiv PDF: %s", len(full_text), paper.url[:50])
        return Paper(
            title=paper.title,
            authors=paper.authors,
            journal=paper.journal,
            url=paper.url,
            source=paper.source,
            published_date=paper.published_date,
            abstract=paper.abstract,
            full_text=full_text,
            pdf_url=getattr(paper, "pdf_url", None),
        )
    logger.warning("No text could be extracted from arXiv PDF (may be scanned/image): %s", paper.url[:50])
    return paper


def _fetch_biorxiv_pdf_fulltext(paper: Paper) -> Paper:
    """
    Fetch the bioRxiv full PDF (url + '.full.pdf') and extract full text into paper.full_text.
    Ensures fulltext is not NULL in the JSON output.
    """
    if paper.source != "biorxiv" or not paper.url:
        return paper
    pdf_url = paper.url.rstrip("/") + ".full.pdf"
    try:
        r = requests.get(
            pdf_url,
            timeout=60,
            headers={"User-Agent": "research-harness/1.0 (https://www.biorxiv.org)"},
        )
        r.raise_for_status()
        pdf_bytes = r.content
        if len(pdf_bytes) < 200:
            logger.warning("bioRxiv .full.pdf too small (%d bytes): %s", len(pdf_bytes), pdf_url[:60])
            return paper
        if not pdf_bytes.startswith(b"%PDF") and "application/pdf" not in (r.headers.get("Content-Type") or "").lower():
            logger.warning("bioRxiv response may not be PDF: %s", pdf_url[:60])
    except Exception as e:
        logger.warning("Could not fetch bioRxiv PDF %s: %s", pdf_url[:60], e)
        return paper

    full_text = _extract_text_from_pdf_bytes(pdf_bytes)
    if full_text:
        full_text = full_text.strip()
    if full_text:
        logger.info("Extracted %d chars full text from bioRxiv PDF: %s", len(full_text), paper.url[:50])
        return Paper(
            title=paper.title,
            authors=paper.authors,
            journal=paper.journal,
            url=paper.url,
            source=paper.source,
            published_date=paper.published_date,
            abstract=paper.abstract,
            full_text=full_text,
            pdf_url=getattr(paper, "pdf_url", None),
        )
    logger.warning("No text extracted from bioRxiv PDF (may be scanned): %s", paper.url[:50])
    return paper


def _log_collection_sources(papers: list[Paper]) -> None:
    by_source: dict[str, list[Paper]] = {}
    for p in papers:
        label = "arXiv API" if p.source == "arxiv" else ("bioRxiv" if p.source == "biorxiv" else "general search")
        by_source.setdefault(label, []).append(p)
    counts = {label: len(group) for label, group in by_source.items()}
    logger.info("Collection: %s", ", ".join(f"{c} from {label}" for label, c in sorted(counts.items(), key=lambda x: -x[1])))


def _filter_papers_with_llm(topic: str, papers: list[Paper], top_k: int) -> list[Paper]:
    """
    Use Claude (Anthropic) to select the best top_k papers from the combined candidate list.
    Requires ANTHROPIC_API_KEY. Optional: FILTER_LLM_MODEL (default claude-haiku-4-5).
    Returns up to top_k papers; if config missing or LLM fails, returns first top_k by date.
    """
    if not papers or top_k <= 0:
        return papers[: top_k] if top_k > 0 else []
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not anthropic_key:
        logger.info(
            "Anthropic filter skipped: set ANTHROPIC_API_KEY to enable. Returning first %d by date.",
            top_k,
        )
        return papers[:top_k]

    model = (os.environ.get("FILTER_LLM_MODEL") or "").strip() or "claude-haiku-4-5"
    n = min(top_k, len(papers))

    lines: list[str] = []
    for i, p in enumerate(papers, 1):
        abst = (p.abstract or "(no abstract)")[:1200].strip()
        authors_str = ", ".join(p.authors[:10]) if p.authors else "(no authors)"
        date_str = p.published_date or "(no date)"
        lines.append(
            f"[{i}] URL: {p.url}\nTitle: {p.title}\nAuthors: {authors_str}\nDate: {date_str}\nSource: {p.source}\nAbstract: {abst}\n"
        )
    block = "\n".join(lines)
    user_content = (
        f'User research topic: "{topic}"\n\n'
        f"Below are candidate research papers from arXiv, bioRxiv, and the web. "
        f"Select the best {n} papers that are most relevant and highest quality for this topic. "
        f"Return a JSON array of exactly {n} URL strings (or fewer if fewer are relevant), in order of preference (best first). "
        f"Use only URLs that appear in the list below. Return nothing else — only a JSON array of URL strings.\n\n"
        f"{block}"
    )

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_content}],
        )
        text = (resp.content[0].text if resp.content else "").strip()

        if "```" in text:
            text = re.sub(r"^.*?```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```.*$", "", text, flags=re.DOTALL)
        text = text.strip()
        urls = json.loads(text)
        if not isinstance(urls, list):
            return papers[:top_k]
        url_order = [u for u in urls if isinstance(u, str) and u.strip()]
        by_url = {p.url: p for p in papers}
        filtered = [by_url[u] for u in url_order if u in by_url][:top_k]
        logger.info("Anthropic filter: selected %d best papers from %d candidates.", len(filtered), len(papers))
        return filtered if filtered else papers[:top_k]
    except Exception as e:
        logger.warning("Anthropic filter failed: %s. Returning first %d by date.", e, top_k)
        return papers[:top_k]


def _filter_recency(papers: list[Paper], max_age_months: int) -> list[Paper]:
    """Keep only papers with published_date within the last max_age_months; drop the rest. Log result."""
    if max_age_months <= 0:
        return papers
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_months * 30)).strftime("%Y-%m-%d")
    kept, dropped = [], []
    for p in papers:
        if p.published_date and p.published_date >= cutoff:
            kept.append(p)
        elif p.published_date:
            dropped.append(p)
        else:
            kept.append(p)  # no date: keep so we don't lose them
    logger.info(
        "Recency filter (max age %s months): kept %d, dropped %d papers.",
        max_age_months,
        len(kept),
        len(dropped),
    )
    return kept


def _sort_papers_by_date(papers: list[Paper]) -> list[Paper]:
    """Sort papers by publication date (newest first). Papers without date go last."""
    with_date: list[Paper] = []
    without_date: list[Paper] = []
    for p in papers:
        if p.published_date:
            with_date.append(p)
        else:
            without_date.append(p)
    with_date.sort(key=lambda p: p.published_date or "", reverse=True)
    result = with_date + without_date
    logger.info(
        "Prioritizing by publication date (newest first): %d papers with date, %d without.",
        len(with_date),
        len(without_date),
    )
    return result


async def _scrape_paper_metadata(session: Any, paper: Paper, source: str) -> Paper:
    """
    Navigate to paper.url and extract published_date, abstract, and full_text using Stagehand.
    Returns a new Paper with those fields set (or original if extraction fails).
    """
    try:
        await session.navigate(url=paper.url)
    except Exception as e:
        logger.debug("Could not load %s: %s", paper.url[:60], e)
        return paper
    if source == "biorxiv":
        instruction = (
            "From this bioRxiv article page, extract: (1) published_date as YYYY-MM-DD if visible, "
            "(2) abstract - full abstract text, (3) full_text - main article body (exclude nav/footer). Use null if not present."
        )
        schema = {
            "type": "object",
            "properties": {
                "published_date": {"type": "string"},
                "abstract": {"type": "string"},
                "full_text": {"type": "string"},
            },
            "required": ["published_date", "abstract", "full_text"],
        }
        extra_title, extra_authors = None, None
    else:
        instruction = (
            "From this research article page extract: (1) title - the article/paper title, "
            "(2) authors - author names comma-separated if visible, (3) published_date as YYYY-MM-DD if visible, "
            "(4) abstract - the abstract or summary, (5) full_text - main body text (exclude nav/ads). Use null for any missing."
        )
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "authors": {"type": "string"},
                "published_date": {"type": "string"},
                "abstract": {"type": "string"},
                "full_text": {"type": "string"},
            },
            "required": ["title", "authors", "published_date", "abstract", "full_text"],
        }
        extra_title, extra_authors = "title", "authors"

    try:
        resp = await session.extract(instruction=instruction, schema=schema)
        data = resp.data.result if resp and resp.data else None
        if isinstance(data, dict):
            date_val = (data.get("published_date") or "").strip() or None
            if date_val and len(date_val) > 10:
                date_val = date_val[:10]
            abst = (data.get("abstract") or "").strip() or None
            if abst:
                abst = abst[:12000]
            full = (data.get("full_text") or "").strip() or None
            title_out = paper.title
            authors_out = paper.authors
            if extra_title and (data.get("title") or "").strip():
                title_out = (data.get("title") or "").strip()
            if extra_authors and (data.get("authors") or "").strip():
                authors_out = [a.strip() for a in (data.get("authors") or "").split(",") if a.strip()]
            return Paper(
                title=title_out,
                authors=authors_out,
                journal=paper.journal,
                url=paper.url,
                source=paper.source,
                published_date=date_val,
                abstract=abst,
                full_text=full,
            )
    except Exception as e:
        logger.debug("Extract failed for %s: %s", paper.url[:50], e)
    return paper


async def _fetch_biorxiv_stagehand(topic: str, max_results: int = 25) -> list[Paper]:
    """
    Use Stagehand (Browserbase + AI) to open bioRxiv search, extract relevant papers,
    then visit each paper page to scrape published_date, abstract, and full_text.
    Returns up to max_results papers with metadata populated.
    """
    api_key = os.environ.get("BROWSERBASE_API_KEY")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
    # Stagehand can use Anthropic for extract; fall back to OpenAI if Stagehand is configured for it
    model_key = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key or not project_id or not model_key:
        logger.info(
            "Skipping bioRxiv: set BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, and ANTHROPIC_API_KEY to enable."
        )
        return []

    try:
        from stagehand import AsyncStagehand
    except ImportError:
        logger.warning("Stagehand not installed; run pip install stagehand. Skipping bioRxiv.")
        return []

    encoded = urllib.parse.quote(topic, safe="")
    search_url = (
        f"https://www.biorxiv.org/search/{encoded}"
        "?sort=publication-date&direction=descending&numresults=50"
    )

    papers: list[Paper] = []
    async with AsyncStagehand(
        browserbase_api_key=api_key,
        browserbase_project_id=project_id,
        model_api_key=model_key,
    ) as client:
        session = await client.sessions.start(model_name="anthropic/claude-haiku-4-5")
        try:
            await session.navigate(url=search_url)
            extract_response = await session.extract(
                instruction=(
                    f"From this search results page, extract ONLY the research papers that are "
                    f"directly and clearly relevant to the topic: \"{topic}\". "
                    f"For each relevant paper extract: title (full title), url (the full link to the paper, e.g. https://www.biorxiv.org/content/...), "
                    f"and authors (comma-separated if visible). Exclude papers that are only loosely or tangentially related."
                ),
                schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Full paper title"},
                            "url": {"type": "string", "description": "Full URL to the paper"},
                            "authors": {"type": "string", "description": "Author names if visible"},
                        },
                        "required": ["title", "url"],
                    },
                },
            )
            result = extract_response.data.result if extract_response and extract_response.data else None
            if isinstance(result, list):
                for item in result[:max_results]:
                    if not isinstance(item, dict):
                        continue
                    title = (item.get("title") or "").strip()
                    url = (item.get("url") or "").strip()
                    if not title or not url or "biorxiv" not in url.lower():
                        continue
                    if not url.startswith("http"):
                        url = "https://www.biorxiv.org" + (url if url.startswith("/") else "/" + url)
                    authors_str = item.get("authors") or ""
                    authors_list = [a.strip() for a in authors_str.split(",") if a.strip()] if authors_str else []
                    papers.append(
                        Paper(
                            title=title,
                            authors=authors_list,
                            journal="bioRxiv",
                            url=url,
                            source="biorxiv",
                        )
                    )
            # Scrape metadata (date, abstract, full_text) from each paper page
            for i, p in enumerate(papers):
                if i >= max_results:
                    break
                logger.info("Scraping metadata for bioRxiv paper %d/%d: %s", i + 1, len(papers), p.url[:60] + "...")
                papers[i] = await _scrape_paper_metadata(session, p, "biorxiv")
            logger.info("Stagehand extracted %d relevant bioRxiv papers (with metadata).", len(papers))
        finally:
            await session.end()

    return papers


def _parse_search_results(result: Any, max_results: int) -> list[Paper]:
    """Parse extract result into list of Paper; dedupe by URL; cap at max_results."""
    papers: list[Paper] = []
    seen: set[str] = set()
    if not isinstance(result, list):
        return papers
    for item in result:
        if len(papers) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title or not url or len(url) < 10 or url in seen:
            continue
        if not url.startswith("http"):
            url = "https://" + url.lstrip("/")
        seen.add(url)
        authors_str = item.get("authors") or ""
        authors_list = [a.strip() for a in authors_str.split(",") if a.strip()] if authors_str else []
        papers.append(Paper(title=title, authors=authors_list, journal="", url=url, source="internet"))
    return papers


async def _fetch_internet_stagehand(topic: str, max_results: int = 25) -> list[Paper]:
    """
    Fetch up to max_results internet candidates: Google Search first, Google Scholar fallback if 0.
    Then scrape each page for title, authors, date, abstract so Claude can consider them.
    """
    api_key = os.environ.get("BROWSERBASE_API_KEY")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
    model_key = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key or not project_id or not model_key:
        logger.info("Browserbase/Stagehand not configured; skipping internet. Set BROWSERBASE_* and ANTHROPIC_API_KEY.")
        return []

    try:
        from stagehand import AsyncStagehand
    except ImportError:
        logger.warning("Stagehand not installed; run pip install stagehand. Skipping internet search.")
        return []

    search_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "authors": {"type": "string"}},
            "required": ["title", "url"],
        },
    }
    query_google = f"{topic} research paper"
    encoded_google = urllib.parse.quote(query_google, safe="")
    encoded_topic = urllib.parse.quote(topic, safe="")

    papers: list[Paper] = []
    async with AsyncStagehand(
        browserbase_api_key=api_key,
        browserbase_project_id=project_id,
        model_api_key=model_key,
    ) as client:
        session = await client.sessions.start(model_name="anthropic/claude-haiku-4-5")
        try:
            # 1) Google Search first
            await session.navigate(url=f"https://www.google.com/search?q={encoded_google}")
            extract_response = await session.extract(
                instruction=(
                    f"This is a Google search results page for \"{query_google}\". "
                    f"Extract up to {max_results} results that look like articles or academic papers. "
                    f"For each result give: title (link text or headline), url (clickable href), authors if visible. "
                    f"Prefer diverse sources: journals, .edu, .org, publishers; include some arXiv/PDFs but not only those. "
                    f"Skip ads, login, and navigation links. Return as many valid (title, url) pairs as you see, up to {max_results}."
                ),
                schema=search_schema,
            )
            result = extract_response.data.result if extract_response and extract_response.data else None
            papers = _parse_search_results(result, max_results)

            # 2) Google Scholar fallback if too few
            if len(papers) < 2:
                logger.info("Google returned %d; trying Google Scholar fallback.", len(papers))
                await session.navigate(url=f"https://scholar.google.com/scholar?q={encoded_topic}")
                extract_response = await session.extract(
                    instruction=(
                        f"This is a Google Scholar results page for \"{topic}\". "
                        f"Extract up to {max_results} papers: for each give title, url (to the paper or abstract page), authors if visible. "
                        f"Return as many valid (title, url) pairs as you see, up to {max_results}."
                    ),
                    schema=search_schema,
                )
                result = extract_response.data.result if extract_response and extract_response.data else None
                scholar_papers = _parse_search_results(result, max_results)
                seen_urls = {p.url for p in papers}
                for p in scholar_papers:
                    if len(papers) >= max_results:
                        break
                    if p.url not in seen_urls:
                        seen_urls.add(p.url)
                        papers.append(p)

            if not papers:
                logger.warning("Internet search returned 0 results for \"%s\". Check BROWSERBASE_* and ANTHROPIC_API_KEY.", topic)

            # 3) Scrape each for title, authors, date, abstract so Claude can consider them
            for i, p in enumerate(papers):
                if i >= max_results:
                    break
                logger.info("Scraping metadata for internet paper %d/%d: %s", i + 1, len(papers), p.url[:60] + "...")
                papers[i] = await _scrape_paper_metadata(session, p, "internet")
            logger.info("Internet: %d candidates (with title, authors, date, abstract).", len(papers))
        finally:
            await session.end()

    return papers


async def _fetch_biorxiv_and_internet(prompt: str, candidate_count: int) -> tuple[list[Paper], list[Paper]]:
    """Run bioRxiv and internet search in sequence (same event loop)."""
    biorxiv_papers: list[Paper] = []
    internet_papers: list[Paper] = []
    try:
        biorxiv_papers = await _fetch_biorxiv_stagehand(prompt, max_results=candidate_count)
    except Exception as e:
        logger.warning("Stagehand/bioRxiv failed: %s", e)
    try:
        internet_papers = await _fetch_internet_stagehand(prompt, max_results=candidate_count)
    except Exception as e:
        logger.warning("Stagehand/internet search failed: %s", e)
    return biorxiv_papers, internet_papers


def run_harness(
    prompt: str,
    candidate_count: int = 50,
    top_k: int = 20,
    max_age_months: int = 0,
) -> list[Paper]:
    """Accumulate candidate_count from each of arXiv, bioRxiv, and internet; then Claude picks best top_k."""
    logger.info("Fetching up to %d candidates from each source (arXiv, bioRxiv, internet).", candidate_count)
    arxiv_papers: list[Paper] = []
    try:
        arxiv_papers = fetch_arxiv(prompt, max_results=candidate_count)
        logger.info("arXiv: %d candidates.", len(arxiv_papers))
    except Exception as e:
        logger.warning("arXiv API failed: %s. Continuing without arXiv.", e)

    biorxiv_papers: list[Paper] = []
    internet_papers: list[Paper] = []
    skip_browserbase = os.environ.get("SKIP_BROWSERBASE", "").strip().lower() in ("1", "true", "yes")
    if skip_browserbase:
        logger.info("SKIP_BROWSERBASE is set — skipping bioRxiv & internet (Stagehand).")
    else:
        try:
            biorxiv_papers, internet_papers = asyncio.run(_fetch_biorxiv_and_internet(prompt, candidate_count))
            logger.info("bioRxiv: %d, internet: %d candidates.", len(biorxiv_papers), len(internet_papers))
            if len(biorxiv_papers) == 0 and len(internet_papers) == 0:
                logger.warning(
                    "No bioRxiv or internet candidates. For web results set BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, ANTHROPIC_API_KEY."
                )
        except Exception as e:
            logger.warning("Browserbase fetch failed: %s. Continuing with arXiv only.", e)

    seen_urls: set[str] = set()
    all_papers: list[Paper] = []
    for p in arxiv_papers + biorxiv_papers + internet_papers:
        if p.url and p.url not in seen_urls:
            seen_urls.add(p.url)
            all_papers.append(p)
    logger.info("Combined %d unique candidates.", len(all_papers))

    if max_age_months > 0:
        all_papers = _filter_recency(all_papers, max_age_months)
    all_papers = _sort_papers_by_date(all_papers)
    all_papers = _filter_papers_with_llm(prompt, all_papers, top_k)

    for i, p in enumerate(all_papers):
        if p.source == "arxiv":
            logger.info("Fetching arXiv PDF full text for paper %d/%d: %s", i + 1, len(all_papers), p.url[:50] + "...")
            all_papers[i] = _fetch_arxiv_pdf_fulltext(p)
        elif p.source == "biorxiv":
            logger.info("Fetching bioRxiv .full.pdf for paper %d/%d: %s", i + 1, len(all_papers), p.url[:50] + "...")
            all_papers[i] = _fetch_biorxiv_pdf_fulltext(p)

    _log_collection_sources(all_papers)

    return all_papers


def paper_to_dict(p: Paper, topic: str | None = None) -> dict:
    """Serialize a paper to JSON with keys: topic, paper_name, paper_authors, published, journal, abstract, fulltext, url. Strings are sanitized (no null bytes)."""
    def _s(x): return _sanitize_for_db(x) if isinstance(x, str) else x
    authors_safe = p.authors if not isinstance(p.authors, list) else [_s(a) for a in p.authors]
    return {
        "topic": _s(topic or ""),
        "paper_name": _s(p.title),
        "paper_authors": authors_safe,
        "published": _s(p.published_date) if isinstance(p.published_date, str) else p.published_date,
        "journal": _s(p.journal),
        "abstract": _sanitize_for_db(p.abstract),
        "fulltext": _sanitize_for_db(p.full_text),
        "url": _s(p.url),
    }


def save_papers_to_supabase(
    papers: list[Paper],
    table: str = "papers",
    topic: str | None = None,
) -> int:
    """
    Upsert papers into a Supabase table. Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY).
    Table should have columns: topic, paper_name, paper_authors (jsonb), published, journal, abstract, fulltext, url.
    Uses url as unique key for upsert. Returns number of rows upserted.
    """
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY not set; skipping Supabase.")
        return 0
    try:
        from supabase import create_client
    except ImportError:
        logger.warning("supabase not installed; pip install supabase. Skipping Supabase.")
        return 0
    def build_rows(skip_columns: set[str] | None = None) -> list[dict]:
        skip = skip_columns or set()
        out = []
        for p in papers:
            # Sanitize strings so PostgreSQL text accepts them (no \\u0000 or other problematic control chars)
            def _s(x): return _sanitize_for_db(x) if isinstance(x, str) else x
            authors_safe = p.authors if not isinstance(p.authors, list) else [_s(a) for a in p.authors]
            row = {
                "topic": _s(topic or ""),
                "paper_name": _s(p.title),
                "paper_authors": authors_safe,
                "published": _s(p.published_date) if isinstance(p.published_date, str) else p.published_date,
                "journal": _s(p.journal),
                "abstract": _sanitize_for_db(p.abstract),
                "fulltext": _sanitize_for_db(p.full_text),
                "url": _s(p.url),
            }
            for col in skip:
                row.pop(col, None)
            out.append(row)
        return out

    client = create_client(url, key)
    skipped_columns: set[str] = set()
    max_retries = 5
    use_upsert = True  # try upsert first; on 42P10 (no unique on url) fall back to insert
    for attempt in range(max_retries):
        rows = build_rows(skip_columns=skipped_columns)
        try:
            if use_upsert:
                client.table(table).upsert(rows, on_conflict="url").execute()
            else:
                client.table(table).insert(rows).execute()
            count = len(rows)
            if not use_upsert:
                logger.warning(
                    "Table %s has no UNIQUE constraint on url; used INSERT (duplicates possible). Add one: ALTER TABLE %s ADD CONSTRAINT papers_url_key UNIQUE (url);",
                    table,
                    table,
                )
            if skipped_columns:
                logger.info(
                    "Upserted %d papers to Supabase table %s (omitted columns not in table: %s).",
                    count,
                    table,
                    ", ".join(sorted(skipped_columns)),
                )
            else:
                logger.info("Upserted %d papers to Supabase table %s.", count, table)
            return count
        except Exception as e:
            err_str = str(e)
            # 42P10: no unique or exclusion constraint matching ON CONFLICT — fall back to insert
            if use_upsert and ("42P10" in err_str or "unique or exclusion constraint" in err_str.lower()):
                use_upsert = False
                logger.warning(
                    "Table %s has no UNIQUE constraint on url; using INSERT instead of upsert. To get upsert-by-url, run: ALTER TABLE %s ADD CONSTRAINT papers_url_key UNIQUE (url);",
                    table,
                    table,
                )
                continue
            # PGRST204 or similar: column not found in schema — retry without that column
            match = re.search(r"Could not find the ['\"](\w+)['\"] column", err_str)
            if match and ("PGRST204" in err_str or "Could not find" in err_str):
                col = match.group(1)
                skipped_columns.add(col)
                logger.warning(
                    "Column %r missing on table %s; adding it to the schema will store this data. Retrying without it. Run: ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s text;",
                    col,
                    table,
                    table,
                    col,
                )
                continue
            logger.warning("Supabase upsert failed: %s", e)
            return 0
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Research paper harness: arXiv, bioRxiv, internet (Stagehand); Anthropic filter; JSON + optional Supabase."
    )
    parser.add_argument(
        "prompt",
        type=str,
        help="Research topic or query (e.g. 'machine learning neurodegenerative disease')",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=int(os.environ.get("CANDIDATE_COUNT", "50")),
        metavar="N",
        help="Candidates to fetch per source: arXiv, bioRxiv, internet (default 50)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=int(os.environ.get("TOP_K", "20")),
        metavar="K",
        help="Max number of papers to return (default 20)",
    )
    parser.add_argument(
        "--max-age-months",
        type=int,
        default=int(os.environ.get("MAX_AGE_MONTHS", "0")),
        metavar="N",
        help="Keep only papers from the last N months (0 = no recency filter)",
    )
    parser.add_argument(
        "--no-supabase",
        action="store_true",
        help="Do not write results to Supabase even if SUPABASE_* env vars are set",
    )
    parser.add_argument(
        "--supabase-table",
        type=str,
        default=os.environ.get("SUPABASE_TABLE", "papers"),
        help="Supabase table name for upsert (default: papers)",
    )
    args = parser.parse_args()

    papers = run_harness(
        prompt=args.prompt,
        candidate_count=args.candidates,
        top_k=args.top,
        max_age_months=args.max_age_months,
    )

    print(json.dumps([paper_to_dict(p, topic=args.prompt) for p in papers], indent=2))

    if not args.no_supabase:
        save_papers_to_supabase(
            papers,
            table=args.supabase_table,
            topic=args.prompt,
        )


if __name__ == "__main__":
    main()
