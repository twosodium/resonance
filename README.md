# Research Paper Harness

Finds **relevant** research papers by **accumulating** candidates from **arXiv**, **bioRxiv**, **OpenAlex**, **Semantic Scholar**, and the **internet** (Google/Scholar via Browserbase/Stagehand), then runs a single **Claude** (Anthropic) filter to select the best N papers (`--top`). Outputs **JSON** with topic, paper_name, paper_authors, published, journal, abstract, fulltext, url.

## Setup

```bash
python -m venv .venv
```

**Use the venv** (from the project folder):

- **Option A – Activate, then run commands:**  
  - PowerShell (after `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` if needed): `.venv\Scripts\Activate.ps1`  
  - CMD: `.venv\Scripts\activate.bat`  
  - macOS/Linux: `source .venv/bin/activate`  
  Then: `pip install -r requirements.txt`.

- **Option B – No activation (Windows):** run the venv’s tools by path:
  ```cmd
  .venv\Scripts\pip.exe install -r requirements.txt
  .venv\Scripts\python.exe research_harness.py "your topic"
  ```

Copy `.env.example` to `.env` and set:

- **BROWSERBASE_API_KEY** and **BROWSERBASE_PROJECT_ID** — required for **bioRxiv** and **internet** search (Stagehand). If not set, only API sources (arXiv, OpenAlex, Semantic Scholar) are used.
- **ANTHROPIC_API_KEY** — required for Stagehand (bioRxiv + internet) and for the final **Claude filter**. Optional: **FILTER_LLM_MODEL** (default `claude-haiku-4-5`).
- **OPENALEX_MAILTO** (optional) — email for OpenAlex polite pool; improves rate limits.
- **SEMANTIC_SCHOLAR_API_KEY** (optional) — for higher Semantic Scholar rate limits.
- **SUPABASE_URL** and **SUPABASE_SERVICE_ROLE_KEY** (or **SUPABASE_KEY**) — optional; when set, results are upserted to the **papers** table (use **SUPABASE_TABLE** or `--supabase-table` to override). Use `--no-supabase` to skip.

## Run command

From the project folder (with venv activated or using the venv’s `python.exe`):

```bash
python research_harness.py "<YOUR_TOPIC>" [OPTIONS]
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--paragraph` | — | Treat the prompt as a paragraph: Claude summarizes it to a short research topic, then the harness runs on that topic. |
| `--sources LIST` | all | Comma-separated list of sources to use: `arxiv`, `biorxiv`, `openalex`, `semantic_scholar`, `internet`. Default uses all. Env: **SOURCES**. |
| `--candidates N` | 50 | Number of candidates to fetch from **each** source per round. |
| `--top K` | 20 | Number of best papers to return after preprocessing and final Claude filter. |
| `--max-age-months N` | 0 | Keep only papers from the last N months (0 = no filter). |
| `--no-supabase` | — | Do not write to Supabase even if env is set. |
| `--supabase-table NAME` | papers | Supabase table name for upsert. |

**Examples:**

```bash
# Basic run (outputs JSON to stdout)
python research_harness.py "CRISPR gene editing"

# Paragraph mode: summarize a long description into a topic, then run the harness
python research_harness.py "I am interested in how armies were organized and supplied in China during the Ming and Qing dynasties, and how that affected military outcomes." --paragraph

# Fewer candidates, fewer papers returned
python research_harness.py "single cell RNA" --candidates 30 --top 10

# Only papers from the last 12 months
python research_harness.py "your topic" --max-age-months 12

# Use only arXiv and OpenAlex (no bioRxiv, Semantic Scholar, or internet)
python research_harness.py "your topic" --sources arxiv,openalex
```

## Output (JSON)

Each paper in the JSON array includes:

| Field            | Description                                                  |
|------------------|--------------------------------------------------------------|
| `topic`          | The user's search topic query                                |
| `paper_name`     | Paper title                                                  |
| `paper_authors`  | List of author names                                         |
| `published`      | Publication date (YYYY-MM-DD) when known                     |
| `journal`        | Journal or "arXiv" / "bioRxiv" / "" (internet)              |
| `abstract`       | Abstract (from arXiv API only; null for bioRxiv)             |
| `fulltext`       | Always null (per-paper scraping removed for speed/relevance) |
| `url`            | Link to the paper                                            |

## How it works

1. **Source selection:** Only the sources you enable (via `--sources` or env **SOURCES**) are queried. Options: `arxiv`, `biorxiv`, `openalex`, `semantic_scholar`, `internet`. Default is all.
2. **Iterative candidate accumulation:**  
   - **Round 0:** Fetch up to `--candidates` from each enabled source (arXiv, bioRxiv, OpenAlex, Semantic Scholar, internet).  
   - **Direct-relevance preprocessing:** Claude keeps only papers **directly** about the topic (discards tangentially related ones).  
   - If the number of directly relevant papers is below `--top`, another round fetches more candidates from API sources (with pagination); bioRxiv and internet are only used in round 0.  
   - Rounds repeat until there are at least `--top` directly relevant papers or a maximum number of rounds (or no new papers).  
   - **arXiv / OpenAlex / Semantic Scholar** use pagination (start/page/offset) in later rounds; **bioRxiv** and **internet** run only in the first round.
3. **Combine** all candidates, deduplicate by URL; apply optional **recency** filter (`--max-age-months`), then the direct-relevance filter above.
4. **Final Claude filter:** From the directly relevant set, Anthropic selects the **best `--top` papers** (relevance and quality). **ANTHROPIC_API_KEY** required; optional **FILTER_LLM_MODEL**.
5. **Return:** The selected papers as JSON. If Supabase is configured, results are written to the table (by `url`).

**Supabase:** If **SUPABASE_URL** and **SUPABASE_SERVICE_ROLE_KEY** are set, the script writes each paper to the table (default `papers`). It uses **upsert** when the table has a `UNIQUE` constraint on `url`; otherwise it falls back to **insert** (duplicates possible). Create the table in the Supabase SQL editor, e.g.:

```sql
create table papers (
  id uuid primary key default gen_random_uuid(),
  topic text,
  paper_name text not null,
  paper_authors jsonb default '[]',
  published text,
  journal text,
  abstract text,
  fulltext text,
  url text not null,
  constraint papers_url_key unique (url)
);
```

If your table already exists without `unique (url)`, add it for upsert behavior: `ALTER TABLE papers ADD CONSTRAINT papers_url_key UNIQUE (url);`  
Use `--no-supabase` to skip writing.

Thank you to arXiv for use of its open access interoperability.
