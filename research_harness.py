"""
Research paper harness: arXiv, bioRxiv, OpenAlex, Semantic Scholar (APIs), and web (Stagehand/Browserbase); then Anthropic filter.
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
    source: str  # "arxiv" | "biorxiv" | "internet" | "openalex" | "semantic_scholar"
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


def _openalex_abstract_from_inverted_index(inv: dict[str, list[int]]) -> str | None:
    """Convert OpenAlex abstract_inverted_index (word -> positions) to plain text."""
    if not inv or not isinstance(inv, dict):
        return None
    pairs: list[tuple[int, str]] = []
    for word, positions in inv.items():
        if isinstance(positions, list) and positions:
            pairs.append((min(positions), word))
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])
    return " ".join(p[1] for p in pairs).strip() or None


def fetch_openalex(query: str, max_results: int = 20) -> list[Paper]:
    """Query OpenAlex works API; returns papers with title, authors, journal, abstract, url. No auth required (optional mailto for polite pool)."""
    papers: list[Paper] = []
    per_page = min(200, max(1, max_results))
    url = "https://api.openalex.org/works"
    params = {"search": query, "per-page": per_page, "sort": "relevance_score:desc"}
    mailto = (os.environ.get("OPENALEX_MAILTO") or "").strip()
    if mailto:
        params["mailto"] = mailto
    headers = {"User-Agent": "research-harness/1.0 (mailto:research@example.com)"}
    if mailto:
        headers["User-Agent"] = f"research-harness/1.0 (mailto:{mailto})"
    try:
        r = requests.get(url, params=params, timeout=30, headers=headers)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("OpenAlex API error: %s", e)
        return papers
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return papers
    for w in results[:max_results]:
        if not isinstance(w, dict):
            continue
        title = (w.get("display_name") or "").strip()
        if not title:
            continue
        authors_list: list[str] = []
        for a in w.get("authorships") or []:
            if isinstance(a, dict):
                author = a.get("author")
                if isinstance(author, dict) and author.get("display_name"):
                    authors_list.append(str(author.get("display_name", "")).strip())
        journal = ""
        pl = w.get("primary_location")
        if isinstance(pl, dict) and pl.get("source"):
            journal = (pl.get("source") or {}).get("display_name") or ""
        if isinstance(journal, dict):
            journal = journal.get("display_name") or ""
        journal = (journal or "").strip() or "OpenAlex"
        pub_date = (w.get("publication_date") or "").strip() or None
        abstract = None
        if w.get("abstract_inverted_index") and isinstance(w["abstract_inverted_index"], dict):
            abstract = _openalex_abstract_from_inverted_index(w["abstract_inverted_index"])
        work_id = (w.get("id") or "").strip()
        if work_id and work_id.startswith("http"):
            paper_url = work_id
        elif work_id:
            paper_url = f"https://openalex.org/{work_id}"
        else:
            doi = (w.get("doi") or "").strip().replace("https://doi.org/", "")
            paper_url = f"https://doi.org/{doi}" if doi else ""
        pdf_url = None
        content_url = (w.get("content_url") or "").strip()
        oa_key = (os.environ.get("OPENALEX_API_KEY") or "").strip()
        if content_url and (w.get("has_content") or {}).get("pdf"):
            pdf_url = content_url.rstrip("/") + ".pdf"
            if oa_key:
                pdf_url += "?" if "?" not in pdf_url else "&"
                pdf_url += f"api_key={oa_key}"
        if not pdf_url and isinstance(w.get("primary_location"), dict):
            pdf_url = (w["primary_location"].get("pdf_url") or "").strip() or None
        if not pdf_url and isinstance(w.get("best_oa_location"), dict):
            pdf_url = (w["best_oa_location"].get("pdf_url") or "").strip() or None
        if not pdf_url and isinstance(w.get("open_access"), dict):
            pdf_url = (w["open_access"].get("oa_url") or "").strip() or None
        if not pdf_url and isinstance(w.get("locations"), list):
            for loc in w["locations"]:
                if isinstance(loc, dict) and (loc.get("pdf_url") or "").strip():
                    pdf_url = (loc.get("pdf_url") or "").strip()
                    break
        if title and paper_url:
            papers.append(
                Paper(
                    title=title,
                    authors=authors_list,
                    journal=journal,
                    url=paper_url,
                    source="openalex",
                    published_date=pub_date,
                    abstract=abstract,
                    pdf_url=pdf_url,
                )
            )
    return papers


def fetch_semantic_scholar(query: str, max_results: int = 20) -> list[Paper]:
    """Query Semantic Scholar paper search API; returns papers with title, authors, journal, abstract, url."""
    papers: list[Paper] = []
    limit = min(100, max(1, max_results))
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": limit,
        "fields": "title,url,abstract,authors,year,publicationDate,venue,externalIds,openAccessPdf",
    }
    headers = {"User-Agent": "research-harness/1.0"}
    key = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    if key:
        headers["x-api-key"] = key
    try:
        r = requests.get(url, params=params, timeout=30, headers=headers)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Semantic Scholar API error: %s", e)
        return papers
    results = data.get("data") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return papers
    for p in results[:max_results]:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        if not title:
            continue
        authors_list = []
        for a in p.get("authors") or []:
            if isinstance(a, dict) and a.get("name"):
                authors_list.append(str(a.get("name", "")).strip())
        journal = (p.get("venue") or "").strip() or "Semantic Scholar"
        pub_date = (p.get("publicationDate") or "").strip()
        if not pub_date and p.get("year"):
            pub_date = str(p.get("year", ""))
        abstract = (p.get("abstract") or "").strip() or None
        paper_url = (p.get("url") or "").strip()
        if not paper_url and p.get("paperId"):
            paper_url = f"https://www.semanticscholar.org/paper/{p.get('paperId')}"
        if not paper_url:
            paper_url = (p.get("externalIds") or {}).get("DOI")
            if paper_url:
                paper_url = f"https://doi.org/{paper_url}" if not paper_url.startswith("http") else paper_url
        if not paper_url:
            paper_url = f"https://www.semanticscholar.org/paper/{p.get('paperId', '')}"
        pdf_url: str | None = None
        oa = p.get("openAccessPdf")
        if isinstance(oa, dict) and (oa.get("url") or "").strip():
            pdf_url = (oa.get("url") or "").strip()
        if title and paper_url:
            papers.append(
                Paper(
                    title=title,
                    authors=authors_list,
                    journal=journal,
                    url=paper_url,
                    source="semantic_scholar",
                    published_date=pub_date or None,
                    abstract=abstract,
                    pdf_url=pdf_url,
                )
            )
    return papers


def _get_pdf_url_from_page(page_url: str) -> str | None:
    """Fetch a page and look for a direct PDF link (href ending .pdf or with application/pdf)."""
    if not page_url or not page_url.startswith("http"):
        return None
    try:
        r = requests.get(page_url, timeout=15, headers={"User-Agent": "research-harness/1.0"})
        r.raise_for_status()
        html = r.text
    except Exception:
        return None
    # Prefer explicit PDF links (href with .pdf or URL containing pdf)
    for pattern in [
        r'href\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r'["\'](https?://[^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r'href\s*=\s*["\'](https?://[^"\']*pdf[^"\']*)["\']',
        r'"(https?://[^"]+\.pdf[^"]*)"',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            u = m.group(1).strip()
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http") and "pdf" in u.lower():
                return u
    return None


def _fetch_semantic_scholar_pdf_fulltext(paper: Paper) -> Paper:
    """
    Fetch full text for a Semantic Scholar paper: use pdf_url from API (openAccessPdf) if set,
    else try to scrape the article page for a PDF link, then download and extract text.
    """
    if paper.source != "semantic_scholar" or not paper.url:
        return paper
    pdf_url = getattr(paper, "pdf_url", None) or _get_pdf_url_from_page(paper.url)
    if not pdf_url or not pdf_url.startswith("http"):
        logger.debug("No PDF URL for Semantic Scholar paper: %s", paper.url[:50])
        return paper
    if pdf_url.startswith("http://"):
        pdf_url = "https://" + pdf_url[7:]
    try:
        r = requests.get(
            pdf_url,
            timeout=60,
            headers={"User-Agent": "research-harness/1.0 (https://www.semanticscholar.org)"},
        )
        r.raise_for_status()
        pdf_bytes = r.content
        if len(pdf_bytes) < 200:
            return paper
        if not pdf_bytes.startswith(b"%PDF") and "application/pdf" not in (r.headers.get("Content-Type") or "").lower():
            logger.debug("Semantic Scholar response may not be PDF: %s", pdf_url[:60])
            return paper
    except Exception as e:
        logger.debug("Could not fetch Semantic Scholar PDF %s: %s", pdf_url[:60], e)
        return paper
    full_text = _extract_text_from_pdf_bytes(pdf_bytes)
    if full_text:
        full_text = full_text.strip()
    if full_text:
        logger.info("Extracted %d chars full text from Semantic Scholar PDF: %s", len(full_text), paper.url[:50])
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
    return paper


def _fetch_openalex_pdf_fulltext(paper: Paper) -> Paper:
    """
    Fetch full text for an OpenAlex paper: use pdf_url from API (primary_location/best_oa_location/open_access)
    if set, else scrape the work page for a PDF link, then download and extract text.
    """
    if paper.source != "openalex" or not paper.url:
        return paper
    pdf_url = getattr(paper, "pdf_url", None) or _get_pdf_url_from_page(paper.url)
    if not pdf_url or not pdf_url.startswith("http"):
        logger.debug("No PDF URL for OpenAlex paper: %s", paper.url[:50])
        return paper
    if pdf_url.startswith("http://"):
        pdf_url = "https://" + pdf_url[7:]
    try:
        r = requests.get(
            pdf_url,
            timeout=60,
            headers={"User-Agent": "research-harness/1.0 (https://openalex.org)"},
        )
        r.raise_for_status()
        pdf_bytes = r.content
        if len(pdf_bytes) < 200:
            return paper
        if not pdf_bytes.startswith(b"%PDF") and "application/pdf" not in (r.headers.get("Content-Type") or "").lower():
            logger.debug("OpenAlex response may not be PDF: %s", pdf_url[:60])
            return paper
    except Exception as e:
        logger.debug("Could not fetch OpenAlex PDF %s: %s", pdf_url[:60], e)
        return paper
    full_text = _extract_text_from_pdf_bytes(pdf_bytes)
    if full_text:
        full_text = full_text.strip()
    if full_text:
        logger.info("Extracted %d chars full text from OpenAlex PDF: %s", len(full_text), paper.url[:50])
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
    return paper


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
    _LABELS = {"arxiv": "arXiv API", "biorxiv": "bioRxiv", "internet": "general search", "openalex": "OpenAlex", "semantic_scholar": "Semantic Scholar"}
    for p in papers:
        label = _LABELS.get(p.source, p.source)
        by_source.setdefault(label, []).append(p)
    counts = {label: len(group) for label, group in by_source.items()}
    logger.info("Collection: %s", ", ".join(f"{c} from {label}" for label, c in sorted(counts.items(), key=lambda x: -x[1])))


def _summarize_paragraph_to_topic(paragraph: str) -> str:
    """
    Use Claude to summarize a user-provided paragraph into a short research topic phrase
    suitable for feeding into the harness (e.g. "CRISPR gene editing", "early modern Chinese military history").
    Requires ANTHROPIC_API_KEY. On failure or missing key, returns paragraph truncated to ~100 chars.
    """
    paragraph = (paragraph or "").strip()
    if not paragraph:
        return ""
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not anthropic_key:
        logger.info("ANTHROPIC_API_KEY not set; using paragraph as topic (truncated).")
        return paragraph[:200].strip() or paragraph

    model = (os.environ.get("FILTER_LLM_MODEL") or "").strip() or "claude-haiku-4-5"
    user_content = (
        "The user has provided the following paragraph describing their research interest. "
        "Summarize it into a single, short research topic or query phrase (a few words to a short phrase) "
        "that would work well for searching academic papers. Examples: 'CRISPR gene editing', "
        "'early modern Chinese military history', 'single cell RNA sequencing cancer'. "
        "Return only the topic phrase, no quotation marks, no explanation.\n\n"
        f"{paragraph}"
    )
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model=model,
            max_tokens=128,
            messages=[{"role": "user", "content": user_content}],
        )
        text = (resp.content[0].text if resp.content else "").strip()
        if text:
            logger.info("Summarized paragraph to topic: %s", text[:80] + ("..." if len(text) > 80 else ""))
            return text
    except Exception as e:
        logger.warning("Paragraph summarization failed: %s. Using truncated paragraph.", e)
    return paragraph[:200].strip() or paragraph


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
        f"Below are candidate research papers from arXiv, bioRxiv, OpenAlex, Semantic Scholar, and the web. "
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
            "This page may be a research article, blog post, or academic page. Using only what you see on the page, "
            "extract whatever metadata is present. Do not assume any layout or format—infer from the visible content. "
            "Return only fields you can clearly identify; use null for anything missing or uncertain. "
            "Extract: title (article/paper title), authors (comma-separated names), journal or venue name, "
            "abstract or summary, fulltext or main body text (exclude navigation/ads), url (canonical or current page URL), "
            "published_date (YYYY-MM-DD if visible)."
        )
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "authors": {"type": "string"},
                "journal": {"type": "string"},
                "abstract": {"type": "string"},
                "fulltext": {"type": "string"},
                "url": {"type": "string"},
                "published_date": {"type": "string"},
            },
        }
        extra_title, extra_authors = "title", "authors"

    try:
        resp = await session.extract(instruction=instruction, schema=schema)
        data = _get_extract_result(resp)
        if isinstance(data, dict):
            date_val = (data.get("published_date") or "").strip() or None
            if date_val and len(date_val) > 10:
                date_val = date_val[:10]
            abst = (data.get("abstract") or "").strip() or None
            if abst:
                abst = abst[:12000]
            full = (data.get("full_text") or data.get("fulltext") or "").strip() or None
            title_out = (data.get("title") or "").strip() or paper.title
            authors_str = (data.get("authors") or "").strip()
            authors_out = [a.strip() for a in authors_str.split(",") if a.strip()] if authors_str else paper.authors
            journal_out = (data.get("journal") or "").strip() or paper.journal
            url_out = (data.get("url") or "").strip() or paper.url
            if url_out and not url_out.startswith("http"):
                url_out = paper.url
            return Paper(
                title=title_out,
                authors=authors_out,
                journal=journal_out,
                url=url_out,
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


def _get_extract_result(extract_response: Any) -> Any:
    """Get the extracted result from Stagehand extract() response; handles .data.result or .result."""
    if extract_response is None:
        return None
    raw = getattr(extract_response, "data", None)
    if raw is not None and hasattr(raw, "result"):
        return getattr(raw, "result", None)
    return getattr(extract_response, "result", None)


def _unwrap_extract_list(result: Any) -> list:
    """Return a list from Stagehand extract result; handles list or dict with result/items/data."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("result", "items", "data", "papers", "links", "results"):
            val = result.get(key)
            if isinstance(val, list):
                return val
        if result:
            first_val = next(iter(result.values()), None)
            if isinstance(first_val, list):
                return first_val
    return []


def _normalize_search_url(url: str) -> str | None:
    """Extract real URL from Google/Scholar redirect wrapper; require http(s) and min length."""
    url = (url or "").strip()
    if not url or len(url) < 8:
        return None
    if "google" in url and ("/url?" in url or "url?q=" in url):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        real = qs.get("q") or qs.get("url")
        if real and isinstance(real, list) and real[0]:
            url = real[0].strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url.lstrip("/")
    if len(url) < 12:
        return None
    return url


def _parse_search_results(result: Any, max_results: int) -> list[Paper]:
    """Parse extract result into list of Paper; dedupe by URL; cap at max_results. Lenient on keys and title."""
    papers: list[Paper] = []
    seen: set[str] = set()
    items = _unwrap_extract_list(result)
    for item in items:
        if len(papers) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url_raw = (item.get("url") or item.get("link") or item.get("href") or "").strip()
        url = _normalize_search_url(url_raw)
        if not url or url in seen:
            continue
        title = (item.get("title") or item.get("text") or item.get("name") or "").strip()
        if not title:
            title = url[:80] + ("..." if len(url) > 80 else "")
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
            await asyncio.sleep(2.0)
            extract_response = await session.extract(
                instruction=(
                    f"This is a Google search results page for \"{query_google}\". "
                    f"List the main search results (organic results, not ads). For each result extract: "
                    f"title (the blue headline/link text), url (the full href of the link - use the actual destination URL if you see a redirect). "
                    f"Include articles, papers, .edu, .org, journals, PDFs. Return a JSON array of objects with keys title and url (and authors if visible). "
                    f"Extract as many results as you see, up to {max_results}."
                ),
                schema=search_schema,
            )
            result = _get_extract_result(extract_response)
            papers = _parse_search_results(result, max_results)
            if len(papers) == 0:
                logger.info("Google extract returned 0 papers. Result type=%s.", type(result).__name__ if result is not None else "None")

            # 2) Google Scholar fallback if too few
            if len(papers) < 2:
                logger.info("Google returned %d; trying Google Scholar fallback.", len(papers))
                await session.navigate(url=f"https://scholar.google.com/scholar?q={encoded_topic}")
                await asyncio.sleep(2.0)
                extract_response = await session.extract(
                    instruction=(
                        f"This is a Google Scholar results page for \"{topic}\". "
                        f"List the search results. For each result extract: title, url (the link to the paper or abstract), authors if visible. "
                        f"Return a JSON array of objects with keys title and url. Extract as many as you see, up to {max_results}."
                    ),
                    schema=search_schema,
                )
                result = _get_extract_result(extract_response)
                scholar_papers = _parse_search_results(result, max_results)
                if len(scholar_papers) == 0:
                    logger.info("Scholar extract returned 0 papers. Result type=%s.", type(result).__name__ if result is not None else "None")
                seen_urls = {p.url for p in papers}
                for p in scholar_papers:
                    if len(papers) >= max_results:
                        break
                    if p.url not in seen_urls:
                        seen_urls.add(p.url)
                        papers.append(p)

            if not papers:
                logger.warning(
                    "Internet search returned 0 results for \"%s\". If Google/Scholar show captcha or consent, the extract may be empty. Check BROWSERBASE_* and ANTHROPIC_API_KEY.",
                    topic,
                )

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
    """Accumulate candidate_count from each of arXiv, bioRxiv, OpenAlex, Semantic Scholar, and internet; then Claude picks best top_k."""
    logger.info("Fetching up to %d candidates from each source (arXiv, bioRxiv, OpenAlex, Semantic Scholar, internet).", candidate_count)
    try:
        arxiv_papers = fetch_arxiv(prompt, max_results=candidate_count)
        logger.info("arXiv: %d candidates.", len(arxiv_papers))
    except Exception as e:
        raise RuntimeError(f"arXiv API error: {e}") from e

    openalex_papers: list[Paper] = []
    s2_papers: list[Paper] = []
    try:
        openalex_papers = fetch_openalex(prompt, max_results=candidate_count)
        logger.info("OpenAlex: %d candidates.", len(openalex_papers))
    except Exception as e:
        logger.warning("OpenAlex fetch failed: %s", e)
    try:
        s2_papers = fetch_semantic_scholar(prompt, max_results=candidate_count)
        logger.info("Semantic Scholar: %d candidates.", len(s2_papers))
    except Exception as e:
        logger.warning("Semantic Scholar fetch failed: %s", e)

    biorxiv_papers: list[Paper] = []
    internet_papers: list[Paper] = []
    try:
        biorxiv_papers, internet_papers = asyncio.run(_fetch_biorxiv_and_internet(prompt, candidate_count))
        logger.info("bioRxiv: %d, internet: %d candidates.", len(biorxiv_papers), len(internet_papers))
        if len(biorxiv_papers) == 0 and len(internet_papers) == 0:
            logger.warning(
                "No bioRxiv or internet candidates. For web results set BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, ANTHROPIC_API_KEY."
            )
    except Exception as e:
        logger.warning("Browserbase fetch failed: %s. Continuing with API sources only.", e)

    seen_urls: set[str] = set()
    all_papers: list[Paper] = []
    for p in arxiv_papers + openalex_papers + s2_papers + biorxiv_papers + internet_papers:
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
        elif p.source == "semantic_scholar":
            logger.info("Fetching Semantic Scholar PDF full text for paper %d/%d: %s", i + 1, len(all_papers), p.url[:50] + "...")
            all_papers[i] = _fetch_semantic_scholar_pdf_fulltext(p)
        elif p.source == "openalex":
            logger.info("Fetching OpenAlex PDF full text for paper %d/%d: %s", i + 1, len(all_papers), p.url[:50] + "...")
            all_papers[i] = _fetch_openalex_pdf_fulltext(p)

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
    use_upsert = True
    for attempt in range(max_retries):
        rows = build_rows(skip_columns=skipped_columns)
        try:
            total_upserted = 0
            for row in rows:
                if use_upsert:
                    client.table(table).upsert([row], on_conflict="url").execute()
                else:
                    client.table(table).insert([row]).execute()
                total_upserted += 1
            if not use_upsert:
                logger.warning(
                    "Table %s has no UNIQUE constraint on url; used INSERT (duplicates possible). Add one: ALTER TABLE %s ADD CONSTRAINT papers_url_key UNIQUE (url);",
                    table,
                    table,
                )
            if skipped_columns:
                logger.info(
                    "Upserted %d papers to Supabase table %s (omitted columns not in table: %s).",
                    total_upserted,
                    table,
                    ", ".join(sorted(skipped_columns)),
                )
            else:
                logger.info("Upserted %d papers to Supabase table %s.", total_upserted, table)
            return total_upserted
        except Exception as e:
            err_str = str(e)
            if use_upsert and ("42P10" in err_str or "unique or exclusion constraint" in err_str.lower()):
                use_upsert = False
                logger.warning(
                    "Table %s has no UNIQUE constraint on url; using INSERT instead of upsert. To get upsert-by-url, run: ALTER TABLE %s ADD CONSTRAINT papers_url_key UNIQUE (url);",
                    table,
                    table,
                )
                continue
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
        help="Research topic or query, or a paragraph if --paragraph is set",
    )
    parser.add_argument(
        "--paragraph",
        action="store_true",
        help="Treat prompt as a paragraph: Claude summarizes it to a research topic, then the harness runs on that topic",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=int(os.environ.get("CANDIDATE_COUNT", "50")),
        metavar="N",
        help="Candidates to fetch per source: arXiv, bioRxiv, OpenAlex, Semantic Scholar, internet (default 50)",
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

    topic = args.prompt
    if args.paragraph:
        topic = _summarize_paragraph_to_topic(args.prompt)
        if not topic:
            topic = args.prompt

    papers = run_harness(
        prompt=topic,
        candidate_count=args.candidates,
        top_k=args.top,
        max_age_months=args.max_age_months,
    )

    print(json.dumps([paper_to_dict(p, topic=topic) for p in papers], indent=2))

    if not args.no_supabase:
        save_papers_to_supabase(
            papers,
            table=args.supabase_table,
            topic=topic,
        )


if __name__ == "__main__":
    main()
