/* ============================================================
   app.js — Dashboard interactivity
   Pulls real data from Supabase via helpers in supabase.js.
   Triggers the backend pipeline when the user submits a topic.
   ============================================================ */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let topics = [];          // [{ topic, last_run, paper_count, debate_count }]
let activeTopic = null;   // currently viewed topic string
let activeDebates = [];   // debates for the active topic
let activePanel = null;   // currently open debate object
let _pollTimer = null;    // polling interval id

// Chat state
let _chatPaperId = null;
let _chatPaperData = null;
let _chatHistory = [];    // [{role, content}]
let _chatSuggestions = [];


// ---------------------------------------------------------------------------
// Init — check auth, then set up the page
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
  const user = await requireAuth();
  if (!user) return;

  const { data: profile } = await getProfile(user.id);
  const firstName = profile?.first_name
    || user.user_metadata?.first_name
    || user.email?.split('@')[0]
    || 'there';

  setGreeting(firstName);
  setupSearch();
  setupSidebarToggle();
  await loadDashboardSources();
  setupDashboardSourceToggles();

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
// Sidebar toggle
// ---------------------------------------------------------------------------

function setupSidebarToggle() {
  const stored = localStorage.getItem('papermint-sidebar-collapsed');
  if (stored === 'true') {
    document.querySelector('.dashboard').classList.add('sidebar-collapsed');
  }
}

function toggleSidebar() {
  const dash = document.querySelector('.dashboard');
  dash.classList.toggle('sidebar-collapsed');
  localStorage.setItem('papermint-sidebar-collapsed', dash.classList.contains('sidebar-collapsed'));
}

// ---------------------------------------------------------------------------
// Dashboard source toggles (under search bar)
// ---------------------------------------------------------------------------

const DASHBOARD_SOURCE_IDS = ['arxiv', 'biorxiv', 'openalex', 'semantic_scholar', 'internet'];

async function loadDashboardSources() {
  const el = document.getElementById('search-sources');
  if (!el) return;
  try {
    const s = await getSettings();
    const sources = s.sources || DASHBOARD_SOURCE_IDS;
    DASHBOARD_SOURCE_IDS.forEach(name => {
      const cb = document.getElementById('ds-source-' + name);
      if (cb) cb.checked = sources.includes(name);
    });
  } catch (_) {
    DASHBOARD_SOURCE_IDS.forEach(name => {
      const cb = document.getElementById('ds-source-' + name);
      if (cb) cb.checked = true;
    });
  }
}

let _sourceSaveTimer = null;
function getEnabledSourceNames() {
  const labels = { arxiv: 'arXiv', biorxiv: 'bioRxiv', openalex: 'OpenAlex', semantic_scholar: 'Semantic Scholar', internet: 'Internet' };
  return DASHBOARD_SOURCE_IDS
    .filter(name => document.getElementById('ds-source-' + name)?.checked)
    .map(name => labels[name] || name);
}

function getEnabledSourceIds() {
  return DASHBOARD_SOURCE_IDS.filter(name => {
    const cb = document.getElementById('ds-source-' + name);
    return cb && cb.checked;
  });
}

/** Save current source checkboxes to config.json and return immediately. */
async function saveSourcesNow() {
  const sources = getEnabledSourceIds();
  try {
    await putSettings({ sources: sources.length ? sources : DASHBOARD_SOURCE_IDS });
  } catch (_) { /* best-effort */ }
}

function setupDashboardSourceToggles() {
  const el = document.getElementById('search-sources');
  if (!el) return;
  DASHBOARD_SOURCE_IDS.forEach(name => {
    const cb = document.getElementById('ds-source-' + name);
    if (cb) {
      cb.addEventListener('change', () => {
        clearTimeout(_sourceSaveTimer);
        _sourceSaveTimer = setTimeout(saveSourcesNow, 400);
      });
    }
  });
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

  // Check if this topic already exists
  const match = topics.find(t => t.topic.toLowerCase() === query.toLowerCase());

  if (match) {
    // Go directly to the existing topic view
    await selectTopic(match.topic);
  } else {
    // NEW topic → trigger the full pipeline
    await startSearch(query);
  }
}


// ---------------------------------------------------------------------------
// Start a new search (calls the backend API)
// ---------------------------------------------------------------------------

async function startSearch(topic) {
  // Switch to topic view with loading state
  activeTopic = topic;

  // Add topic to sidebar immediately
  if (!topics.find(t => t.topic.toLowerCase() === topic.toLowerCase())) {
    topics.unshift({ topic, last_run: new Date().toISOString(), paper_count: 0, debate_count: 0 });
    renderSidebar();
  }

  // Switch UI
  document.getElementById('search-area').style.display = 'none';
  const tv = document.getElementById('topic-view');
  tv.classList.add('active');
  document.getElementById('topic-title').textContent = topic;
  document.getElementById('topic-meta').textContent = '';

  document.getElementById('greeting').querySelector('p').textContent =
    `Searching for "${topic}"…`;

  // Show loading state (only the sources that are actually enabled)
  const sourceNames = getEnabledSourceNames();
  showLoading('Scraping papers from ' + (sourceNames.length ? sourceNames.join(', ') : 'sources') + '…');
  showCancelSearchButton(true);

  // IMPORTANT: Flush source selection to config before triggering the pipeline
  clearTimeout(_sourceSaveTimer);
  await saveSourcesNow();

  try {
    await triggerSearch(topic);
  } catch (err) {
    console.error('triggerSearch failed:', err);
    showLoading('⚠ Could not reach the backend. Is the API server running on port 5000?');
    return;
  }

  // Start polling for results
  startPolling(topic);
}


// ---------------------------------------------------------------------------
// Rerun search for the active topic
// ---------------------------------------------------------------------------

async function rerunSearch() {
  if (!activeTopic) return;
  showLoading('Re-running pipeline…');
  showCancelSearchButton(true);
  try {
    await triggerSearch(activeTopic);
  } catch (err) {
    console.error('rerunSearch failed:', err);
    showLoading('⚠ Could not reach the backend. Is the API server running?');
    showCancelSearchButton(false);
    return;
  }
  startPolling(activeTopic);
}


// ---------------------------------------------------------------------------
// Polling — watch for papers / debates appearing in Supabase
// ---------------------------------------------------------------------------

function startPolling(topic) {
  stopPolling();
  pollOnce(topic); // initial check
  _pollTimer = setInterval(() => pollOnce(topic), 4000);
}

function stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

async function pollOnce(topic) {
  // 1. Check backend status
  let status;
  try {
    status = await checkSearchStatus(topic);
  } catch {
    status = { status: 'unknown' };
  }

  // 2. Fetch whatever data exists so far
  const papers = await fetchPapersByTopic(topic);
  const debates = await fetchDebatesByTopic(topic);

  // 3. Update loading message based on phase
  const phase = status.status;
  if (phase === 'scraping') {
    const sourceNames = getEnabledSourceNames();
    showLoading(`Scraping from ${sourceNames.length ? sourceNames.join(', ') : 'sources'}… ${papers.length} found so far`);
  } else if (phase === 'debating') {
    const debated = status.debated ?? debates.length;
    const total   = status.total_papers ?? papers.length;
    showLoading(`Debating papers… ${debated}/${total} analysed`);
  } else if (phase === 'complete' || phase === 'error' || phase === 'cancelled') {
    hideLoading();
    stopPolling();
    showCancelSearchButton(false);
    if (phase === 'cancelled') {
      document.getElementById('topic-meta').textContent =
        papers.length > 0 || debates.length > 0
          ? `Search cancelled · ${(debates.length || papers.length)} paper(s) so far`
          : 'Search cancelled';
      if (debates.length > 0) {
        activeDebates = debates;
        renderDebates(debates);
      } else if (papers.length > 0) {
        renderPapersOnly(papers);
      } else {
        renderPapersOnly([]);
      }
    }
  } else if (papers.length > 0 || debates.length > 0) {
    // Status might be "unknown" if we reloaded — that's fine, show data
    hideLoading();
  }

  // 4. Render current data
  if (debates.length > 0) {
    activeDebates = debates;
    document.getElementById('topic-meta').textContent =
      `${debates.length} paper${debates.length !== 1 ? 's' : ''} debated` +
      (papers.length > debates.length ? ` · ${papers.length - debates.length} pending` : '') +
      (phase === 'debating' ? ' · analysing…' : '');
    renderDebates(debates);
  } else if (papers.length > 0) {
    document.getElementById('topic-meta').textContent =
      `${papers.length} paper${papers.length !== 1 ? 's' : ''} scraped` +
      (phase === 'debating' ? ' · debating in progress…' : ' · awaiting debate');
    renderPapersOnly(papers);
  } else if (phase === 'complete') {
    document.getElementById('topic-meta').textContent = 'Search complete — no papers found';
    renderPapersOnly([]);
  }

  // 5. Refresh sidebar topic counts
  const topicEntry = topics.find(t => t.topic === topic);
  if (topicEntry) {
    topicEntry.paper_count = papers.length;
    topicEntry.debate_count = debates.length;
  }

  if (phase === 'error') {
    hideLoading();
    showCancelSearchButton(false);
    document.getElementById('topic-meta').textContent =
      `⚠ Pipeline error: ${status.error || 'unknown'}`;
  }
}


// ---------------------------------------------------------------------------
// Loading indicator
// ---------------------------------------------------------------------------

function showLoading(message) {
  let el = document.getElementById('loading-indicator');
  if (!el) {
    el = document.createElement('div');
    el.id = 'loading-indicator';
    el.className = 'loading-indicator';
    document.getElementById('topic-view').prepend(el);
  }
  el.innerHTML = `
    <div class="loading-spinner"></div>
    <span>${message || 'Working…'}</span>
  `;
  el.style.display = 'flex';
}

function hideLoading() {
  const el = document.getElementById('loading-indicator');
  if (el) el.style.display = 'none';
}

function showCancelSearchButton(show) {
  const btn = document.getElementById('cancel-search-btn');
  if (btn) btn.style.display = show ? 'inline-flex' : 'none';
}

async function cancelSearch() {
  if (!activeTopic) return;
  const topic = activeTopic;
  try {
    stopPolling();
    showCancelSearchButton(false);
    document.getElementById('topic-meta').textContent = 'Cancelling…';
    await cancelSearchAPI(topic);
    // Refresh UI with whatever data we have and show cancelled state
    const papers = await fetchPapersByTopic(topic);
    const debates = await fetchDebatesByTopic(topic);
    if (debates.length > 0) {
      activeDebates = debates;
      document.getElementById('topic-meta').textContent =
        `Search cancelled · ${debates.length} paper${debates.length !== 1 ? 's' : ''} debated`;
      renderDebates(debates);
    } else if (papers.length > 0) {
      document.getElementById('topic-meta').textContent =
        `Search cancelled · ${papers.length} paper${papers.length !== 1 ? 's' : ''} scraped`;
      renderPapersOnly(papers);
    } else {
      document.getElementById('topic-meta').textContent = 'Search cancelled';
      renderPapersOnly([]);
    }
    hideLoading();
  } catch (e) {
    console.error('Cancel failed:', e);
    document.getElementById('topic-meta').textContent = 'Search cancelled';
    hideLoading();
    stopPolling();
    showCancelSearchButton(false);
  }
}


// ---------------------------------------------------------------------------
// Topic view
// ---------------------------------------------------------------------------

async function selectTopic(topicName) {
  activeTopic = topicName;
  stopPolling();

  // Hide search, show topic view
  document.getElementById('search-area').style.display = 'none';
  const tv = document.getElementById('topic-view');
  tv.classList.add('active');

  document.getElementById('topic-title').textContent = topicName;
  document.getElementById('topic-meta').textContent = 'Loading…';

  renderSidebar();

  document.getElementById('greeting').querySelector('p').textContent =
    `Viewing results for "${topicName}"`;

  // Rerun button
  document.getElementById('rerun-btn').onclick = () => rerunSearch();

  // Fetch debates (with joined paper data)
  activeDebates = await fetchDebatesByTopic(topicName);

  if (activeDebates.length === 0) {
    const papers = await fetchPapersByTopic(topicName);
    if (papers.length > 0) {
      document.getElementById('topic-meta').textContent =
        `${papers.length} paper${papers.length !== 1 ? 's' : ''} · no debate results yet`;
      renderPapersOnly(papers);

      // If pipeline might be running, start polling
      try {
        const st = await checkSearchStatus(topicName);
        if (st.status === 'scraping' || st.status === 'debating') {
          startPolling(topicName);
        }
      } catch { /* ignore */ }
      return;
    }
    document.getElementById('topic-meta').textContent = 'No papers found for this topic';
    renderPapersOnly([]);
    return;
  }

  const topicInfo = topics.find(t => t.topic === topicName);
  const lastRun = topicInfo ? timeAgo(topicInfo.last_run) : 'unknown';

  document.getElementById('topic-meta').textContent =
    `${activeDebates.length} paper${activeDebates.length !== 1 ? 's' : ''} debated · last run ${lastRun}`;

  renderDebates(activeDebates);
}

function showHome() {
  activeTopic = null;
  activeDebates = [];
  stopPolling();
  hideLoading();
  showCancelSearchButton(false);
  document.getElementById('search-area').style.display = '';
  document.getElementById('topic-view').classList.remove('active');
  document.getElementById('greeting').querySelector('p').textContent =
    'What research direction would you like to explore?';
  renderSidebar();
}

async function clearAllTopics() {
  if (!confirm('Clear all previous topics? This will remove all your papers and debate results.')) return;
  try {
    await clearAllTopicsAPI();
    topics = [];
    renderSidebar();
    showHome();
  } catch (err) {
    console.error('clearAllTopics:', err);
    alert('Failed to clear topics: ' + (err.message || 'unknown'));
  }
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
        <td colspan="7" style="text-align:center; padding:48px 16px; color:var(--text-tertiary);">
          No debate results yet — click <strong>Rerun search</strong> to analyse papers.
        </td>
      </tr>`;
    return;
  }

  debates.forEach((debate, idx) => {
    const paper = debate.papers || {};
    const tr = document.createElement('tr');
    tr.onclick = () => openPanel(debate);

    const verdict = debate.verdict || 'UNCERTAIN';
    const verdictClass = verdict.toLowerCase();
    const confPct = Math.round((debate.confidence || 0) * 100);
    const paperName = paper.paper_name || debate.topic || 'Unknown';
    const authors = paper.paper_authors || [];
    const published = paper.published || '';
    const paperId = paper.id || debate.paper_id;
    const url = paper.url || paper.paper_url || '';

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
      </td>
      <td class="paper-link-cell">${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="paper-link" onclick="event.stopPropagation()">View</a>` : '—'}</td>
      <td class="row-actions">
        ${paperId ? `<button class="chat-icon-btn" title="Chat about this paper" data-idx="${idx}">💬</button>` : ''}
        ${paperId ? `<button class="delete-btn" title="Delete paper" data-paper-id="${paperId}">✕</button>` : ''}
      </td>`;

    // Attach handlers via JS to avoid inline escaping issues
    const chatBtn = tr.querySelector('.chat-icon-btn');
    if (chatBtn) {
      chatBtn.addEventListener('click', e => {
        e.stopPropagation();
        const suggestions = parseJsonField(debate.follow_up_questions);
        openChatPanel(paperId, paperName, paper, suggestions);
      });
    }
    const delBtn = tr.querySelector('.delete-btn');
    if (delBtn) {
      delBtn.addEventListener('click', e => {
        e.stopPropagation();
        handleDeletePaper(paperId, activeTopic);
      });
    }

    tbody.appendChild(tr);
  });
}

function renderPapersOnly(papers) {
  const tbody = document.getElementById('papers-tbody');
  tbody.innerHTML = '';

  if (papers.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="7" style="text-align:center; padding:48px 16px; color:var(--text-tertiary);">
          No papers yet — click <strong>Rerun search</strong> to fetch results.
        </td>
      </tr>`;
    return;
  }

  papers.forEach(paper => {
    const tr = document.createElement('tr');
    tr.onclick = () => openPaperOnlyPanel(paper);
    const paperId = paper.id;
    const url = paper.url || paper.paper_url || '';

    tr.innerHTML = `
      <td class="paper-title-cell">${paper.paper_name || 'Unknown'}</td>
      <td class="paper-authors-cell">${formatAuthors(paper.paper_authors)}</td>
      <td class="paper-date-cell">${formatDate(paper.published)}</td>
      <td><span class="verdict-badge uncertain">Pending</span></td>
      <td>—</td>
      <td class="paper-link-cell">${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="paper-link" onclick="event.stopPropagation()">View</a>` : '—'}</td>
      <td class="row-actions">
        ${paperId ? `<button class="chat-icon-btn" title="Chat about this paper">💬</button>` : ''}
        ${paperId ? `<button class="delete-btn" title="Delete paper">✕</button>` : ''}
      </td>`;

    const chatBtn = tr.querySelector('.chat-icon-btn');
    if (chatBtn) {
      chatBtn.addEventListener('click', e => {
        e.stopPropagation();
        openChatPanel(paperId, paper.paper_name || 'Unknown', paper, []);
      });
    }
    const delBtn = tr.querySelector('.delete-btn');
    if (delBtn) {
      delBtn.addEventListener('click', e => {
        e.stopPropagation();
        handleDeletePaper(paperId, activeTopic);
      });
    }

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

  const keyStrengths = parseJsonField(debate.key_strengths);
  const keyRisks = parseJsonField(debate.key_risks);
  const bigIdeas = parseJsonField(debate.big_ideas);
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
      ${debate.topicality != null ? `
      <div class="panel-meta-row">
        <span class="label">Topicality</span>
        <span class="value">${Math.round(debate.topicality * 100)}% relevant to search topic</span>
      </div>` : ''}
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

    ${bigIdeas.length ? `
    <div class="panel-section">
      <h4>Big ideas</h4>
      <div class="panel-tags">
        ${bigIdeas.map(v => `<span class="panel-tag">${v}</span>`).join('')}
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

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const chatOpen = document.getElementById('chat-panel')?.classList.contains('open');
    if (chatOpen) {
      closeChatPanel();
    } else {
      closePanel();
    }
  }
});


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeAttr(s) {
  if (!s) return '';
  return s.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

function escapeHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

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
// Floating Chat Panel
// ---------------------------------------------------------------------------

function openChatPanel(paperId, paperName, paperData, suggestions) {
  _chatPaperId = paperId;
  _chatPaperData = paperData;
  _chatHistory = [];
  _chatSuggestions = suggestions || [];

  // Update header
  document.getElementById('chat-title').textContent = paperName || 'Chat';

  // Clear body
  const body = document.getElementById('chat-body');
  body.innerHTML = `
    <div class="chat-bubble assistant">
      <div class="chat-bubble-avatar">AI</div>
      <div class="chat-bubble-content">
        Hi! I can help you explore <strong>${paperName || 'this paper'}</strong>.
        Ask me anything — follow-up questions, experiment ideas, or how this connects to other research.
      </div>
    </div>`;

  // Render suggestion chips
  renderSuggestions();

  // Show panel
  document.getElementById('chat-panel').classList.add('open');
  document.getElementById('chat-input-field').focus();
}

function closeChatPanel() {
  document.getElementById('chat-panel').classList.remove('open');
}

function resetChat() {
  if (!_chatPaperId) return;
  clearPaperChat(_chatPaperId).catch(() => {});
  openChatPanel(_chatPaperId, document.getElementById('chat-title').textContent, _chatPaperData, _chatSuggestions);
}

function renderSuggestions() {
  const container = document.getElementById('chat-suggestions');
  container.innerHTML = '';

  const questions = (_chatSuggestions || []).slice(0, 3);
  questions.forEach(q => {
    const btn = document.createElement('button');
    btn.className = 'chat-suggestion';
    btn.textContent = q;
    btn.title = q;
    btn.addEventListener('click', () => {
      document.getElementById('chat-input-field').value = q;
      sendChat();
    });
    container.appendChild(btn);
  });
}

async function sendChat() {
  const input = document.getElementById('chat-input-field');
  const sendBtn = document.getElementById('chat-send-btn');
  const msg = (input.value || '').trim();
  if (!msg || !_chatPaperId) return;

  input.value = '';
  sendBtn.disabled = true;

  // Hide suggestions after first message
  document.getElementById('chat-suggestions').innerHTML = '';

  // Render user message
  appendBubble('user', msg);

  // Show typing indicator
  const typingEl = appendTypingIndicator();

  try {
    const { reply } = await chatWithPaper(_chatPaperId, msg, _chatPaperData);
    typingEl.remove();
    appendBubble('assistant', reply, true);
  } catch (err) {
    typingEl.remove();
    appendBubble('assistant', '⚠ ' + (err.message || 'Something went wrong'), false);
  }

  sendBtn.disabled = false;
  input.focus();
}

function appendBubble(role, text, renderMarkdown = false) {
  const body = document.getElementById('chat-body');
  const wrapper = document.createElement('div');
  wrapper.className = `chat-bubble ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'chat-bubble-avatar';
  avatar.textContent = role === 'assistant' ? 'AI' : 'U';

  const content = document.createElement('div');
  content.className = 'chat-bubble-content';

  if (role === 'assistant' && renderMarkdown && typeof marked !== 'undefined') {
    content.innerHTML = marked.parse(text);
  } else {
    content.textContent = text;
  }

  wrapper.appendChild(avatar);
  wrapper.appendChild(content);
  body.appendChild(wrapper);
  body.scrollTop = body.scrollHeight;
}

function appendTypingIndicator() {
  const body = document.getElementById('chat-body');
  const wrapper = document.createElement('div');
  wrapper.className = 'chat-bubble assistant';
  wrapper.id = 'typing-bubble';

  const avatar = document.createElement('div');
  avatar.className = 'chat-bubble-avatar';
  avatar.textContent = 'AI';

  const content = document.createElement('div');
  content.className = 'chat-bubble-content';
  content.innerHTML = `<div class="typing-indicator"><span></span><span></span><span></span></div>`;

  wrapper.appendChild(avatar);
  wrapper.appendChild(content);
  body.appendChild(wrapper);
  body.scrollTop = body.scrollHeight;
  return wrapper;
}


// ---------------------------------------------------------------------------
// Delete Paper
// ---------------------------------------------------------------------------

async function handleDeletePaper(paperId, topic) {
  if (!confirm('Delete this paper and its debate results?')) return;
  try {
    await deletePaper(paperId);
    // Refresh the current topic view
    if (topic) {
      await selectTopic(topic);
    }
    await loadTopics();
  } catch (err) {
    alert('Failed to delete: ' + err.message);
  }
}


// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

function toggleAvatarMenu() {
  const menu = document.getElementById('avatar-menu');
  if (menu) menu.classList.toggle('open');
}

function closeAvatarMenu() {
  const menu = document.getElementById('avatar-menu');
  if (menu) menu.classList.remove('open');
}

document.addEventListener('click', (e) => {
  const dropdown = document.querySelector('.avatar-dropdown');
  if (dropdown && !dropdown.contains(e.target)) closeAvatarMenu();
});

async function handleLogout() {
  closeAvatarMenu();
  await authSignOut();
  window.location.href = 'index.html';
}


// ---------------------------------------------------------------------------
// Handle deep links (e.g. ideas.html linking to dashboard.html#topic=...)
// ---------------------------------------------------------------------------

(function handleDeepLink() {
  const hash = window.location.hash;
  if (hash.startsWith('#topic=')) {
    const topic = decodeURIComponent(hash.slice(7));
    if (topic) {
      // Wait for DOMContentLoaded to finish, then select the topic
      setTimeout(() => selectTopic(topic), 500);
    }
  }
})();
