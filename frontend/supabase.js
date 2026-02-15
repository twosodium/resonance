/* ============================================================
   supabase.js — Shared Supabase client + auth helpers + API calls
   Loaded by every page that needs auth or DB access.
   ============================================================ */

const SUPABASE_URL = 'https://qirsshatwdjdpcfctyza.supabase.co';
const SUPABASE_ANON_KEY = 'sb_publishable_gZ8clJHZGBhbEVgD9yWi6Q_qb2CR89P';

// Backend API base URL (Flask server)
const API_BASE = 'http://localhost:5000';

const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);


// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

/** Sign up with email + password, storing first/last name + role/bio in user metadata. */
async function authSignUp({ email, password, firstName, lastName, role, bio }) {
  const { data, error } = await sb.auth.signUp({
    email,
    password,
    options: {
      data: {
        first_name: firstName,
        last_name: lastName,
        role: role || '',
        bio: bio || '',
      },
    },
  });
  return { data, error };
}

/** Sign in with email + password. */
async function authSignIn({ email, password }) {
  const { data, error } = await sb.auth.signInWithPassword({ email, password });
  return { data, error };
}

/** Sign out the current user. */
async function authSignOut() {
  const { error } = await sb.auth.signOut();
  return { error };
}

/** Get the currently logged-in user (or null). */
async function getUser() {
  const { data: { user } } = await sb.auth.getUser();
  return user;
}

/** Fetch the user's profile row from the `profiles` table. */
async function getProfile(userId) {
  const { data, error } = await sb.from('profiles')
    .select('*')
    .eq('id', userId)
    .single();
  return { data, error };
}


// ---------------------------------------------------------------------------
// Backend API helpers  (call the Flask server)
// ---------------------------------------------------------------------------

/**
 * Trigger the full scrape + debate pipeline on the backend.
 * Returns immediately — the pipeline runs in a background thread.
 */
async function triggerSearch(topic) {
  const user = await getUser();
  const userId = user?.id || null;

  const resp = await fetch(`${API_BASE}/api/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic, user_id: userId }),
  });

  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  return resp.json();
}

/**
 * Check pipeline job status for a given topic.
 * Returns: { status: "scraping"|"debating"|"complete"|"error"|"unknown", ... }
 */
async function checkSearchStatus(topic) {
  const user = await getUser();
  const userId = user?.id || null;

  const params = new URLSearchParams({ topic });
  if (userId) params.set('user_id', userId);

  const resp = await fetch(`${API_BASE}/api/status?${params}`);
  if (!resp.ok) return { status: 'unknown' };
  return resp.json();
}


// ---------------------------------------------------------------------------
// Data helpers — papers & debates (with user_id filtering)
// ---------------------------------------------------------------------------

/**
 * Fetch the distinct topics the current user has searched.
 * Looks at BOTH the papers table and the debates table to catch
 * topics that are still being processed (papers exist, debates don't yet).
 */
async function fetchTopics() {
  const user = await getUser();
  const userId = user?.id;

  // Get topics from papers table
  let papersQuery = sb.from('papers')
    .select('topic, published')
    .order('published', { ascending: false });
  if (userId) papersQuery = papersQuery.eq('user_id', userId);
  const { data: paperRows } = await papersQuery;

  // Get topics from debates table
  let debatesQuery = sb.from('debates')
    .select('topic, created_at')
    .order('created_at', { ascending: false });
  if (userId) debatesQuery = debatesQuery.eq('user_id', userId);
  const { data: debateRows } = await debatesQuery;

  // Merge into a map keyed by topic
  const map = new Map();

  // Papers first (so topics appear even before debates are done)
  for (const row of (paperRows || [])) {
    const t = row.topic;
    if (!t) continue;
    if (!map.has(t)) {
      map.set(t, { topic: t, last_run: row.published, paper_count: 0, debate_count: 0 });
    }
    map.get(t).paper_count++;
  }

  // Enrich with debate info
  for (const row of (debateRows || [])) {
    const t = row.topic;
    if (!t) continue;
    if (!map.has(t)) {
      map.set(t, { topic: t, last_run: row.created_at, paper_count: 0, debate_count: 0 });
    }
    const entry = map.get(t);
    entry.debate_count++;
    // Use debate created_at as "last run" if it's newer
    if (row.created_at && (!entry.last_run || row.created_at > entry.last_run)) {
      entry.last_run = row.created_at;
    }
  }

  return Array.from(map.values());
}

/**
 * Fetch papers for a given topic from the `papers` table.
 */
async function fetchPapersByTopic(topic) {
  const user = await getUser();
  let query = sb.from('papers')
    .select('*')
    .ilike('topic', `%${topic}%`)
    .order('published', { ascending: false });
  if (user?.id) query = query.eq('user_id', user.id);

  const { data, error } = await query;
  if (error) { console.error('fetchPapersByTopic:', error); return []; }
  return data || [];
}

/**
 * Fetch debates for a given topic, joined with their paper data.
 */
async function fetchDebatesByTopic(topic) {
  const user = await getUser();
  let query = sb.from('debates')
    .select('*, papers(*)')
    .ilike('topic', `%${topic}%`)
    .order('confidence', { ascending: false });
  if (user?.id) query = query.eq('user_id', user.id);

  const { data, error } = await query;
  if (error) { console.error('fetchDebatesByTopic:', error); return []; }
  return data || [];
}

/**
 * Fetch a single debate by id, with its paper.
 */
async function fetchDebateById(debateId) {
  const { data, error } = await sb.from('debates')
    .select('*, papers(*)')
    .eq('id', debateId)
    .single();

  if (error) { console.error('fetchDebateById:', error); return null; }
  return data;
}


// ---------------------------------------------------------------------------
// Profile helpers (role + bio)
// ---------------------------------------------------------------------------

/**
 * Fetch the user's profile (role, bio) from the backend API.
 */
async function fetchProfileAPI(userId) {
  const resp = await fetch(`${API_BASE}/api/profile?user_id=${encodeURIComponent(userId)}`);
  if (!resp.ok) throw new Error(`Profile fetch error: ${resp.status}`);
  return resp.json();
}

/**
 * Update the user's profile (role, bio, first_name, last_name).
 */
async function updateProfileAPI(userId, updates) {
  const resp = await fetch(`${API_BASE}/api/profile`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, ...updates }),
  });
  if (!resp.ok) throw new Error(`Profile update error: ${resp.status}`);
  return resp.json();
}


// ---------------------------------------------------------------------------
// Paper chat helpers
// ---------------------------------------------------------------------------

/**
 * Send a message to the per-paper Claude chat.
 * Returns { reply: "..." }
 */
async function chatWithPaper(paperId, message, paper) {
  const resp = await fetch(`${API_BASE}/api/papers/${paperId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, paper }),
  });
  if (!resp.ok) throw new Error(`Chat error: ${resp.status}`);
  return resp.json();
}

/**
 * Clear the chat history for a paper.
 */
async function clearPaperChat(paperId) {
  await fetch(`${API_BASE}/api/papers/${paperId}/chat`, { method: 'DELETE' });
}


// ---------------------------------------------------------------------------
// Delete paper
// ---------------------------------------------------------------------------

/**
 * Delete a paper and its debates from the database.
 */
async function deletePaper(paperId) {
  const resp = await fetch(`${API_BASE}/api/papers/${paperId}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error(`Delete error: ${resp.status}`);
  return resp.json();
}


// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

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


// ---------------------------------------------------------------------------
// Auth guard — redirect to login if not signed in
// ---------------------------------------------------------------------------

async function requireAuth() {
  const user = await getUser();
  if (!user) {
    window.location.href = 'login.html';
    return null;
  }
  return user;
}
