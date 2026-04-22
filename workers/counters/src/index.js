const PATH_RE = /^\/(blog|thoughts)\/[A-Za-z0-9-]+\/$|^\/interview\/[A-Za-z0-9-]+\/[A-Za-z0-9-]+\/$/;

async function sha256Hex(message) {
  const data = new TextEncoder().encode(message);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function todayUTC() {
  return new Date().toISOString().slice(0, 10);
}

function allowedOrigins(env) {
  return (env.ALLOWED_ORIGINS || '').split(',').map((s) => s.trim()).filter(Boolean);
}

function corsHeaders(origin, env) {
  const headers = {
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    Vary: 'Origin',
  };
  if (allowedOrigins(env).includes(origin)) {
    headers['Access-Control-Allow-Origin'] = origin;
  }
  return headers;
}

function json(data, status, extraHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
  });
}

// Vote hash is stable per (IP, path) forever -- one vote per visitor per post.
// View hash rotates daily -- one counted view per visitor per post per UTC day.
async function getVisitorHashes(request, env) {
  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  const stable = await sha256Hex(`${ip}:${env.COUNTER_SALT}`);
  const daily = await sha256Hex(`${ip}:${env.COUNTER_SALT}:${todayUTC()}`);
  return { stable, daily };
}

async function readStats(env, path, voterHash) {
  const [viewsRow, votesRows, myVoteRow] = await Promise.all([
    env.DB.prepare('SELECT COUNT(*) AS n FROM page_views WHERE path = ?').bind(path).first(),
    env.DB.prepare('SELECT vote, COUNT(*) AS n FROM votes WHERE path = ? GROUP BY vote').bind(path).all(),
    env.DB.prepare('SELECT vote FROM votes WHERE path = ? AND visitor_hash = ?').bind(path, voterHash).first(),
  ]);

  let upvotes = 0;
  let downvotes = 0;
  for (const row of votesRows.results || []) {
    if (row.vote === 1) upvotes = row.n;
    if (row.vote === -1) downvotes = row.n;
  }

  return {
    views: viewsRow ? viewsRow.n : 0,
    upvotes,
    downvotes,
    userVote: myVoteRow ? myVoteRow.vote : 0,
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin') || '';
    const cors = corsHeaders(origin, env);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }

    if (!allowedOrigins(env).includes(origin)) {
      return json({ error: 'forbidden' }, 403, cors);
    }

    if (url.pathname === '/view' && request.method === 'POST') {
      const body = await request.json().catch(() => ({}));
      const path = typeof body.path === 'string' ? body.path : '';
      if (!PATH_RE.test(path)) return json({ error: 'invalid path' }, 400, cors);

      const { stable, daily } = await getVisitorHashes(request, env);
      await env.DB.prepare(
        'INSERT OR IGNORE INTO page_views (path, visitor_hash, day) VALUES (?, ?, ?)'
      ).bind(path, daily, todayUTC()).run();

      return json(await readStats(env, path, stable), 200, cors);
    }

    if (url.pathname === '/counts' && request.method === 'GET') {
      const path = url.searchParams.get('path') || '';
      if (!PATH_RE.test(path)) return json({ error: 'invalid path' }, 400, cors);

      const { stable } = await getVisitorHashes(request, env);
      return json(await readStats(env, path, stable), 200, cors);
    }

    if (url.pathname === '/vote' && request.method === 'POST') {
      const body = await request.json().catch(() => ({}));
      const path = typeof body.path === 'string' ? body.path : '';
      const vote = body.vote;
      if (!PATH_RE.test(path)) return json({ error: 'invalid path' }, 400, cors);
      if (![1, -1, 0].includes(vote)) return json({ error: 'invalid vote' }, 400, cors);

      const { stable } = await getVisitorHashes(request, env);

      if (vote === 0) {
        await env.DB.prepare('DELETE FROM votes WHERE path = ? AND visitor_hash = ?')
          .bind(path, stable).run();
      } else {
        await env.DB.prepare(
          `INSERT INTO votes (path, visitor_hash, vote, updated_at) VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(path, visitor_hash) DO UPDATE SET vote = excluded.vote, updated_at = excluded.updated_at`
        ).bind(path, stable, vote).run();
      }

      return json(await readStats(env, path, stable), 200, cors);
    }

    return json({ error: 'not found' }, 404, cors);
  },
};
