/* ============================================================
   supabase.js — Shared Supabase client + auth helpers
   Loaded by every page that needs auth or DB access.
   ============================================================ */

const SUPABASE_URL = 'https://qirsshatwdjdpcfctyza.supabase.co';
const SUPABASE_ANON_KEY = 'sb_publishable_gZ8clJHZGBhbEVgD9yWi6Q_qb2CR89P';

const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);


// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

/** Sign up with email + password, storing first/last name in user metadata. */
async function authSignUp({ email, password, firstName, lastName }) {
  const { data, error } = await sb.auth.signUp({
    email,
    password,
    options: {
      data: {                       // goes into auth.users.raw_user_meta_data
        first_name: firstName,      // the DB trigger copies this to profiles
        last_name: lastName,
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
// Data helpers — papers & debates
// ---------------------------------------------------------------------------

/**
 * Fetch the distinct topics the current user has debated.
 * Returns an array of objects: [{ topic, last_run, paper_count }]
 * We derive topics from the `debates` table (only topics with debate results).
 */
async function fetchTopics() {
  // Get all debates, select only topic + created_at
  const { data, error } = await sb.from('debates')
    .select('topic, created_at')
    .order('created_at', { ascending: false });

  if (error || !data) return [];

  // Group by topic to get last_run and count
  const map = new Map();
  for (const row of data) {
    const t = row.topic;
    if (!map.has(t)) {
      map.set(t, { topic: t, last_run: row.created_at, paper_count: 0 });
    }
    map.get(t).paper_count++;
  }
  return Array.from(map.values());
}

/**
 * Fetch papers for a given topic from the `papers` table.
 */
async function fetchPapersByTopic(topic) {
  const { data, error } = await sb.from('papers')
    .select('*')
    .eq('topic', topic)
    .order('published', { ascending: false });

  if (error) { console.error('fetchPapersByTopic:', error); return []; }
  return data || [];
}

/**
 * Fetch debates for a given topic, joined with their paper data.
 * Returns rows like { id, verdict, confidence, one_liner, key_strengths, ..., papers: { paper_name, ... } }
 */
async function fetchDebatesByTopic(topic) {
  const { data, error } = await sb.from('debates')
    .select('*, papers(*)')
    .eq('topic', topic)
    .order('confidence', { ascending: false });

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
// Auth guard — redirect to login if not signed in
// ---------------------------------------------------------------------------

/**
 * Call this at the top of any protected page.
 * Returns the user object if logged in, otherwise redirects to login.html.
 */
async function requireAuth() {
  const user = await getUser();
  if (!user) {
    window.location.href = 'login.html';
    return null;
  }
  return user;
}

