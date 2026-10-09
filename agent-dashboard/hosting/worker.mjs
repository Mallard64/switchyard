const headers = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'X-Robots-Tag': 'noindex, nofollow, noarchive',
  'Referrer-Policy': 'no-referrer',
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
}

async function asset(env, path) {
  const response = await env.ASSETS.fetch(new Request(new URL(path, 'https://assets.invalid')));
  if (!response.ok) return null;
  return response;
}

async function manifest(env) {
  const response = await asset(env, '/data/manifest.json');
  return response ? response.json() : { reports: [] };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/api/')) {
      if (request.method === 'GET' && url.pathname === '/api/reports') {
        const data = await manifest(env);
        return json(data.reports.map(({ runs, ...report }) => report));
      }
      if (request.method === 'GET' && url.pathname === '/api/report') {
        const { reports } = await manifest(env);
        const id = url.searchParams.get('id');
        if (!reports.some(report => report.id === id)) return json({ error: 'unknown report' }, 404);
        const response = await asset(env, `/data/reports/${id}.json`);
        return response ? new Response(response.body, { headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } }) : json({ error: 'report unavailable' }, 404);
      }
      if (request.method === 'GET' && url.pathname === '/api/replays') {
        const { reports } = await manifest(env);
        const report = reports.find(item => item.id === url.searchParams.get('report'));
        return json({ runs: report?.runs || [], active: null });
      }
      if (url.pathname === '/api/replay/stop' && request.method === 'POST') return json({ ok: true });
      if (url.pathname === '/api/replay' && request.method === 'POST') {
        if (!String(request.headers.get('content-type') || '').startsWith('application/json')) return json({ error: 'expected JSON' }, 415);
        const origin = request.headers.get('origin');
        if (origin && origin !== url.origin) return json({ error: 'same-origin request required' }, 403);
        const text = await request.text();
        if (text.length > 10000) return json({ error: 'request too large' }, 413);
        let body;
        try { body = JSON.parse(text || '{}'); } catch { return json({ error: 'invalid JSON' }, 400); }
        const { reports } = await manifest(env);
        const report = reports.find(item => item.id === body.report);
        const run = report?.runs.find(item => item.file === body.file);
        if (!run) return json({ error: 'unknown run for this report' }, 404);
        const response = await asset(env, `/data/runs/${encodeURIComponent(run.file)}`);
        if (!response) return json({ error: 'recorded run unavailable' }, 404);
        const rows = (await response.text()).split('\n').filter(Boolean).flatMap(line => {
          try { return [JSON.parse(line)]; } catch { return []; }
        });
        const seconds = [0, 20, 60].includes(body.seconds) ? body.seconds : 20;
        return json({ ok: true, file: run.file, rows, seconds });
      }
      if (url.pathname === '/api/live' && request.method === 'GET') {
        const payload = `event: hello\ndata: ${JSON.stringify({ backend: 'recorded reports', recent: [], replay: null })}\n\n`;
        return new Response(payload, { headers: { ...headers, 'Content-Type': 'text/event-stream; charset=utf-8', Connection: 'keep-alive' } });
      }
      return json({ error: 'not found' }, 404);
    }
    if (!['GET', 'HEAD'].includes(request.method)) return new Response('Method not allowed', { status: 405, headers });
    const response = await env.ASSETS.fetch(request);
    const result = new Response(response.body, response);
    for (const [name, value] of Object.entries(headers)) result.headers.set(name, value);
    return result;
  },
};
