/* ============================================================
   app.js — Dashboard interactivity
   Pulls real data from Supabase via helpers in supabase.js.
   ============================================================ */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let topics = [];          // [{ topic, last_run, paper_count }]
let activeTopic = null;   // currently viewed topic string
let activeDebates = [];   // debates for the active topic
let activePanel = null;   // currently open debate object


// ---------------------------------------------------------------------------
// Init — check auth, then set up the page
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
  // Auth guard: redirect to login if not signed in
  const user = await requireAuth();
  if (!user) return;

  // Fetch profile for the greeting
  const { data: profile } = await getProfile(user.id);
  const firstName = profile?.first_name
    || user.user_metadata?.first_name
    || user.email?.split('@')[0]
    || 'there';

  setGreeting(firstName);
  setupSearch();

  // Load real topics from DB
  await loadTopics();
});


// ---------------------------------------------------------------------------
// Greeting
// ---------------------------------------------------------------------------

function setGreeting(name) {
  const hour = new Date().getHours();
  const timeOfDay = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';

  const greetEl = document.getElementById('greeting');
  greetEl.querySelector('h1').textContent = `Good ${timeOfDay}, ${name}`;

  const avatar = document.getElementById('avatar');
  avatar.textContent = name.charAt(0).toUpperCase();
}


// ---------------------------------------------------------------------------
// Load topics from Supabase
// ---------------------------------------------------------------------------

async function loadTopics() {
  topics = await fetchTopics();
  renderSidebar();
}


// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function renderSidebar() {
  const container = document.getElementById('sidebar-topics');
  container.innerHTML = '';

  if (topics.length === 0) {
    container.innerHTML = '<div class="sidebar-empty">No searches yet</div>';
    return;
  }

  topics.forEach(t => {
    const el = document.createElement('div');
    el.className = 'sidebar-item' + (activeTopic === t.topic ? ' active' : '');
    el.innerHTML = `<span class="icon">◇</span> ${t.topic}`;
    el.onclick = () => selectTopic(t.topic);
    container.appendChild(el);
  });
}


// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

function setupSearch() {
  const input = document.getElementById('topic-input');
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') submitTopic();
  });
}

async function submitTopic() {
  const input = document.getElementById('topic-input');
  const query = input.value.trim();
  if (!query) return;

  input.value = '';

  // Check if this topic already exists in our loaded topics
  const match = topics.find(
    t => t.topic.toLowerCase() === query.toLowerCase()
  );

  if (match) {
    await selectTopic(match.topic);
  } else {
    // Show the topic view even if there are no results yet
    // (user would then click "Rerun search" to trigger the pipeline)
    await selectTopic(query);
  }
}


// ---------------------------------------------------------------------------
// Topic view
// ---------------------------------------------------------------------------

async function selectTopic(topicName) {
  activeTopic = topicName;

  // Hide search, show topic view
  document.getElementById('search-area').style.display = 'none';
  const tv = document.getElementById('topic-view');
  tv.classList.add('active');

  // Update header
  document.getElementById('topic-title').textContent = topicName;
  document.getElementById('topic-meta').textContent = 'Loading…';

  // Render sidebar highlight
  renderSidebar();

  // Update greeting subtitle
  document.getElementById('greeting').querySelector('p').textContent =
    `Viewing results for "${topicName}"`;

  // Rerun button
  document.getElementById('rerun-btn').onclick = () => {
    alert(`Rerun search for "${topicName}" — this will trigger the pipeline.`);
  };

  // Fetch debates (with joined paper data) for this topic
  activeDebates = await fetchDebatesByTopic(topicName);

  // If no debates exist, try to at least show papers from the papers table
  if (activeDebates.length === 0) {
    const papers = await fetchPapersByTopic(topicName);
    if (papers.length > 0) {
      // Show papers without debate results
      document.getElementById('topic-meta').textContent =
        `${papers.length} paper${papers.length !== 1 ? 's' : ''} · no debate results yet`;
      renderPapersOnly(papers);
      return;
    }
    document.getElementById('topic-meta').textContent = 'No papers found for this topic';
    renderPapersOnly([]);
    return;
  }

  // Find last_run from the topic metadata
  const topicInfo = topics.find(t => t.topic === topicName);
  const lastRun = topicInfo ? timeAgo(topicInfo.last_run) : 'unknown';

  document.getElementById('topic-meta').textContent =
    `${activeDebates.length} paper${activeDebates.length !== 1 ? 's' : ''} debated · last run ${lastRun}`;

  renderDebates(activeDebates);
}

function showHome() {
  activeTopic = null;
  activeDebates = [];
  document.getElementById('search-area').style.display = '';
  document.getElementById('topic-view').classList.remove('active');
  document.getElementById('greeting').querySelector('p').textContent =
    'What research direction would you like to explore?';
  renderSidebar();
}


// ---------------------------------------------------------------------------
// Papers table — with debate results (main view)
// ---------------------------------------------------------------------------

function renderDebates(debates) {
  const tbody = document.getElementById('papers-tbody');
  tbody.innerHTML = '';

  if (debates.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align:center; padding:48px 16px; color:var(--text-tertiary);">
          No debate results yet — click <strong>Rerun search</strong> to analyse papers.
        </td>
      </tr>`;
    return;
  }

  debates.forEach(debate => {
    const paper = debate.papers || {};  // FK join: debate.papers is the joined paper row
    const tr = document.createElement('tr');
    tr.onclick = () => openPanel(debate);

    const verdict = debate.verdict || 'UNCERTAIN';
    const verdictClass = verdict.toLowerCase();
    const confPct = Math.round((debate.confidence || 0) * 100);
    const paperName = paper.paper_name || debate.topic || 'Unknown';
    const authors = paper.paper_authors || [];
    const published = paper.published || '';

    tr.innerHTML = `
      <td class="paper-title-cell">${paperName}</td>
      <td class="paper-authors-cell">${formatAuthors(authors)}</td>
      <td class="paper-date-cell">${formatDate(published)}</td>
      <td><span class="verdict-badge ${verdictClass}">${verdict}</span></td>
      <td>
        ${confPct}%
        <span class="confidence-bar">
          <span class="confidence-bar-fill" style="width:${confPct}%"></span>
        </span>
      </td>`;

    tbody.appendChild(tr);
  });
}

/**
 * Fallback: render papers that have no debate results yet.
 */
function renderPapersOnly(papers) {
  const tbody = document.getElementById('papers-tbody');
  tbody.innerHTML = '';

  if (papers.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align:center; padding:48px 16px; color:var(--text-tertiary);">
          No papers yet — click <strong>Rerun search</strong> to fetch results.
        </td>
      </tr>`;
    return;
  }

  papers.forEach(paper => {
    const tr = document.createElement('tr');
    tr.onclick = () => openPaperOnlyPanel(paper);

    tr.innerHTML = `
      <td class="paper-title-cell">${paper.paper_name || 'Unknown'}</td>
      <td class="paper-authors-cell">${formatAuthors(paper.paper_authors)}</td>
      <td class="paper-date-cell">${formatDate(paper.published)}</td>
      <td><span class="verdict-badge uncertain">Pending</span></td>
      <td>—</td>`;

    tbody.appendChild(tr);
  });
}


// ---------------------------------------------------------------------------
// Side panel — debate detail
// ---------------------------------------------------------------------------

function openPanel(debate) {
  activePanel = debate;
  const paper = debate.papers || {};
  const panel = document.getElementById('side-panel');
  const overlay = document.getElementById('panel-overlay');
  const body = document.getElementById('panel-body');

  const paperName = paper.paper_name || debate.topic || 'Unknown';
  const verdict = debate.verdict || 'UNCERTAIN';
  const confPct = Math.round((debate.confidence || 0) * 100);

  // Parse JSON fields that might be stored as strings
  const keyStrengths = parseJsonField(debate.key_strengths);
  const keyRisks = parseJsonField(debate.key_risks);
  const suggestedVerticals = parseJsonField(debate.suggested_verticals);
  const followUpQuestions = parseJsonField(debate.follow_up_questions);

  body.innerHTML = `
    <div class="panel-title">${paperName}</div>

    <div class="panel-meta">
      <div class="panel-meta-row">
        <span class="label">Authors</span>
        <span class="value">${formatAuthors(paper.paper_authors)}</span>
      </div>
      <div class="panel-meta-row">
        <span class="label">Published</span>
        <span class="value">${formatDate(paper.published)}</span>
      </div>
      ${paper.journal ? `
      <div class="panel-meta-row">
        <span class="label">Journal</span>
        <span class="value">${paper.journal}</span>
      </div>` : ''}
      <div class="panel-meta-row">
        <span class="label">Verdict</span>
        <span class="value"><span class="verdict-badge ${verdict.toLowerCase()}">${verdict}</span> · ${confPct}% confidence</span>
      </div>
    </div>

    ${debate.one_liner ? `
    <div class="panel-section">
      <h4>One-liner</h4>
      <p>${debate.one_liner}</p>
    </div>` : ''}

    ${paper.abstract ? `
    <div class="panel-section">
      <h4>Abstract</h4>
      <p>${paper.abstract}</p>
    </div>` : ''}

    ${keyStrengths.length ? `
    <div class="panel-section">
      <h4>Key strengths</h4>
      <ul>
        ${keyStrengths.map(s => `<li>${s}</li>`).join('')}
      </ul>
    </div>` : ''}

    ${keyRisks.length ? `
    <div class="panel-section">
      <h4>Key risks</h4>
      <ul>
        ${keyRisks.map(r => `<li>${r}</li>`).join('')}
      </ul>
    </div>` : ''}

    ${suggestedVerticals.length ? `
    <div class="panel-section">
      <h4>Suggested verticals</h4>
      <div class="panel-tags">
        ${suggestedVerticals.map(v => `<span class="panel-tag">${v}</span>`).join('')}
      </div>
    </div>` : ''}

    ${followUpQuestions.length ? `
    <div class="panel-section">
      <h4>Follow-up questions</h4>
      <ul>
        ${followUpQuestions.map(q => `<li>${q}</li>`).join('')}
      </ul>
    </div>` : ''}

    ${paper.url ? `
    <a class="panel-link" href="${paper.url}" target="_blank" rel="noopener">
      View paper ↗
    </a>` : ''}
  `;

  panel.classList.add('open');
  overlay.classList.add('open');
  overlay.onclick = closePanel;
}

/**
 * Panel for papers that don't have debate results yet.
 */
function openPaperOnlyPanel(paper) {
  activePanel = paper;
  const panel = document.getElementById('side-panel');
  const overlay = document.getElementById('panel-overlay');
  const body = document.getElementById('panel-body');

  body.innerHTML = `
    <div class="panel-title">${paper.paper_name || 'Unknown'}</div>

    <div class="panel-meta">
      <div class="panel-meta-row">
        <span class="label">Authors</span>
        <span class="value">${formatAuthors(paper.paper_authors)}</span>
      </div>
      <div class="panel-meta-row">
        <span class="label">Published</span>
        <span class="value">${formatDate(paper.published)}</span>
      </div>
      ${paper.journal ? `
      <div class="panel-meta-row">
        <span class="label">Journal</span>
        <span class="value">${paper.journal}</span>
      </div>` : ''}
      <div class="panel-meta-row">
        <span class="label">Verdict</span>
        <span class="value"><span class="verdict-badge uncertain">Pending debate</span></span>
      </div>
    </div>

    ${paper.abstract ? `
    <div class="panel-section">
      <h4>Abstract</h4>
      <p>${paper.abstract}</p>
    </div>` : ''}

    ${paper.url ? `
    <a class="panel-link" href="${paper.url}" target="_blank" rel="noopener">
      View paper ↗
    </a>` : ''}

    <div class="panel-section" style="margin-top: 32px; text-align: center; color: var(--text-tertiary);">
      <p>Run the debate pipeline to get AI analysis of this paper.</p>
    </div>
  `;

  panel.classList.add('open');
  overlay.classList.add('open');
  overlay.onclick = closePanel;
}

function closePanel() {
  activePanel = null;
  document.getElementById('side-panel').classList.remove('open');
  document.getElementById('panel-overlay').classList.remove('open');
}

// Close panel on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closePanel();
});


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatAuthors(authors) {
  if (!authors) return 'Unknown';
  if (typeof authors === 'string') {
    try { authors = JSON.parse(authors); } catch { return authors; }
  }
  if (!Array.isArray(authors)) return String(authors);
  if (authors.length === 0) return 'Unknown';
  if (authors.length <= 2) return authors.join(' & ');
  return `${authors[0]} et al.`;
}

function formatDate(dateStr) {
  if (!dateStr) return '—';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/**
 * Parse a field that might be a JSON string or already an array/object.
 */
function parseJsonField(val) {
  if (!val) return [];
  if (Array.isArray(val)) return val;
  if (typeof val === 'string') {
    try { return JSON.parse(val); } catch { return []; }
  }
  return [];
}

/**
 * Human-readable relative time from an ISO string.
 */
function timeAgo(isoString) {
  if (!isoString) return 'unknown';
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffMs = now - then;

  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days}d ago`;

  return formatDate(isoString);
}


// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

async function handleLogout() {
  await authSignOut();
  window.location.href = 'index.html';
}
