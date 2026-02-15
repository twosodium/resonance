# 🌿 Resonance

**Spot tomorrow's breakthroughs today.**

Resonance is an AI-powered research discovery platform that scrapes papers from multiple academic sources, runs multi-agent debates to evaluate each paper's promise, and presents results in an interactive dashboard with a connected mind map of your best ideas. Built at **TreeHacks 2026**.

---

## How It Works

```
 User enters a topic
        │
        ▼
 ┌──────────────┐     arXiv API ──────────┐
 │  Research     │     OpenAlex API ───────┤
 │  Harness      │──▶  Semantic Scholar ───┼──▶ Papers stored in Supabase
 │  (scraper)    │     bioRxiv (Browser) ──┤      (papers table)
 └──────────────┘     Internet (Browser) ──┘
        │
        ▼
 ┌──────────────┐     🔍 Scout Agent ──────┐
 │  Multi-Agent  │     ✅ Advocate Agent ───┼──▶ Debate rounds
 │  Debate       │     ❌ Skeptic Agent ────┤
 │  (agents.py)  │     ⚖️  Moderator ───────┘──▶ Verdict + confidence
 └──────────────┘                                stored in Supabase
        │                                        (debates table)
        ▼
 ┌──────────────┐
 │  Dashboard    │──▶  Paper list with verdicts, confidence, topicality
 │  (frontend)   │──▶  Per-paper chat with Claude
 │               │──▶  Top Ideas mind map (connected graph)
 └──────────────┘
        │
        ▼
 ┌──────────────┐
 │  Poke MCP     │──▶  Text-based agent via iMessage / SMS / Slack
 │  (optional)   │     Queries sync to your dashboard
 └──────────────┘
```

### Pipeline

1. **Scraping** — `research_harness.py` queries arXiv, OpenAlex, and Semantic Scholar via their REST APIs. bioRxiv and internet search use Browserbase/Stagehand (optional, toggleable). Papers are filtered for relevance by Claude, then stored in the Supabase `papers` table.

2. **Debating** — `agents.py` runs a multi-agent debate on each paper. A **Scout** evaluates novelty, an **Advocate** argues for the paper's promise, and a **Skeptic** challenges it. After configurable rounds, a **Moderator** synthesizes a verdict (Promising / Interesting / Uncertain / Weak), confidence score, topicality score, key strengths, risks, big ideas, and follow-up questions. Results are stored in the Supabase `debates` table.

3. **Dashboard** — The frontend shows your search history, paper details, debate verdicts, and lets you chat with Claude about any individual paper. The **Top Ideas** page renders a physics-based mind map of your most promising discoveries across all searches, with AI-generated connection labels.

4. **Poke** (optional) — A Poke MCP server lets you interact with Resonance via text message. Ask it to research a topic, check status, or browse results — all synced to your dashboard.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | Vanilla HTML/CSS/JS, Supabase JS client |
| **Backend API** | Flask (Python), Flask-CORS |
| **Agents** | Anthropic Claude (Haiku for speed, Sonnet for quality) |
| **Scraping** | arXiv API, OpenAlex API, Semantic Scholar API, Browserbase/Stagehand |
| **Database** | Supabase (PostgreSQL) with RLS |
| **Auth** | Supabase Auth (email/password) |
| **Messaging** | Poke MCP + Poke Python SDK |
| **Config** | `config.json` + `.env` |

---

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/your-org/treehacks26.git
cd treehacks26

python3 -m venv venv
source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SUPABASE_KEY=eyJ...                          # anon key (for frontend)

# Optional — Browserbase (for bioRxiv + internet scraping)
BROWSERBASE_API_KEY=...
BROWSERBASE_PROJECT_ID=...
SKIP_BROWSERBASE=1                            # set to 1 to disable Browserbase

# Optional — higher rate limits
OPENALEX_MAILTO=you@example.com
SEMANTIC_SCHOLAR_API_KEY=...

# Optional — Poke integration
POKE_API_KEY=pk_...
```

### 3. Supabase tables

Run the following SQL in the Supabase SQL editor to create the required tables:

```sql
-- Papers table
CREATE TABLE IF NOT EXISTS papers (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  topic text,
  paper_name text NOT NULL,
  paper_authors jsonb DEFAULT '[]',
  published text,
  journal text,
  abstract text,
  fulltext text,
  url text NOT NULL,
  user_id uuid REFERENCES auth.users(id),
  created_at timestamptz DEFAULT now(),
  CONSTRAINT papers_url_user_key UNIQUE (url, user_id)
);

-- Debates table
CREATE TABLE IF NOT EXISTS debates (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  paper_id bigint REFERENCES papers(id),
  verdict text,
  confidence real DEFAULT 0,
  topicality real DEFAULT 0,
  one_liner text DEFAULT '',
  key_strengths text DEFAULT '[]',
  key_risks text DEFAULT '[]',
  big_ideas text DEFAULT '[]',
  follow_up_questions text DEFAULT '[]',
  debate_log text DEFAULT '[]',
  raw_verdict text DEFAULT '',
  created_at timestamptz DEFAULT now(),
  user_id uuid REFERENCES auth.users(id),
  topic text
);

-- Profiles table
CREATE TABLE IF NOT EXISTS profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id),
  first_name text DEFAULT '',
  last_name text DEFAULT '',
  role text DEFAULT '',
  bio text DEFAULT '',
  link_token text,
  link_token_expires timestamptz,
  poke_api_key text DEFAULT ''
);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO profiles (id, first_name, last_name, role, bio)
  VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data->>'first_name', ''),
    COALESCE(NEW.raw_user_meta_data->>'last_name', ''),
    COALESCE(NEW.raw_user_meta_data->>'role', ''),
    COALESCE(NEW.raw_user_meta_data->>'bio', '')
  );
  RETURN NEW;
EXCEPTION WHEN OTHERS THEN
  RAISE LOG 'Profile creation failed for user %: %', NEW.id, SQLERRM;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- RLS policies
ALTER TABLE papers ENABLE ROW LEVEL SECURITY;
ALTER TABLE debates ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own papers" ON papers FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users insert own papers" ON papers FOR INSERT WITH CHECK (true);
CREATE POLICY "Users delete own papers" ON papers FOR DELETE USING (auth.uid() = user_id);

CREATE POLICY "Users see own debates" ON debates FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Allow insert debates" ON debates FOR INSERT WITH CHECK (true);

CREATE POLICY "Users see own profile" ON profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Users update own profile" ON profiles FOR UPDATE USING (auth.uid() = id);
```

### 4. Update `frontend/supabase.js`

Make sure the Supabase URL and anon key in `frontend/supabase.js` match your project.

---

## Running Locally

You need **three terminals** to run everything:

### Terminal 1 — Frontend (static file server)

```bash
cd frontend
python3 -m http.server 8080
```

Then open [http://localhost:8080](http://localhost:8080) in your browser.

### Terminal 2 — Backend API

```bash
cd /path/to/treehacks26
source venv/bin/activate
python3 api.py
```

This starts the Flask API on port **5000**. The frontend calls this for search, status, settings, chat, and ideas endpoints.

### Terminal 3 — Poke MCP Server (optional)

```bash
lsof -ti:8765 | xargs kill -9 2>/dev/null
cd /path/to/treehacks26/poke-mcp && python3 server.py
```

This starts the MCP server on port **8765**. Then in a **fourth terminal**, expose it via Poke's tunnel:

```bash
npx poke tunnel http://localhost:8765/mcp -n "Resonance"
```

This outputs a tunnel URL like:
```
Tunnel URL: https://tunnel.poke.com/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/mcp
```

Paste this URL into your Poke recipe's integration settings at [poke.com](https://poke.com) to connect.

---

## Configuration

### `config.json`

Runtime configuration that the dashboard settings page reads and writes:

| Key | Default | Description |
|-----|---------|-------------|
| `topic` | `"mechanistic interpretability"` | Default search topic |
| `candidate_count` | `5` | Papers to fetch per source |
| `top_k` | `5` | Papers to keep after filtering |
| `debate_rounds` | `1` | Rounds of agent debate per paper |
| `skip_browserbase` | `false` | Skip Browserbase-dependent sources |
| `sources` | all five | Which sources to scrape |

### Settings page

Users can configure API keys, toggle sources, and adjust pipeline parameters from the **Settings** page in the dashboard (`/settings.html`). Changes persist to both `config.json` and `.env`.

---

## Project Structure

```
treehacks26/
├── api.py                  # Flask backend API
├── agents.py               # Multi-agent debate system (Scout, Advocate, Skeptic, Moderator)
├── pipeline.py             # Orchestrates scraping → debating → storing
├── research_harness.py     # Web scraping from 5 sources + Claude filtering
├── config.json             # Runtime configuration
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (not committed)
│
├── frontend/
│   ├── index.html          # Landing page
│   ├── login.html          # Login / signup (with role + bio)
│   ├── dashboard.html      # Main dashboard
│   ├── settings.html       # User preferences & API keys
│   ├── ideas.html          # Top Ideas mind map
│   ├── how-it-works.html   # About page
│   ├── app.js              # Dashboard interactivity
│   ├── supabase.js         # Supabase client + API helpers
│   ├── styles.css          # All styles
│   └── theme.js            # Dark/light theme toggle
│
├── poke-mcp/
│   ├── server.py           # Poke MCP server (exposes tools to Poke AI)
│   └── requirements.txt    # MCP server dependencies
│
└── tests/
    └── test_research_harness.py
```

---

## Per-Paper Chat Memory

Each paper has its own conversation thread with Claude. When you click the 💬 icon next to a paper, a floating chat panel opens with:
- The paper's full context (title, abstract, verdict, strengths, risks) pre-loaded as a system prompt
- Agent-generated follow-up question suggestions
- Markdown rendering and typing indicators
- Conversation history persists in-memory on the Flask server for the duration of the session

---

## Credits

Built at **TreeHacks 2026** · Powered by [Claude](https://anthropic.com), [Browserbase](https://browserbase.com), [Poke](https://poke.com) & [Supabase](https://supabase.com)

Thank you to arXiv for use of its open access interoperability.
