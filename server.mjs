import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { open, readFile, readdir, stat } from 'node:fs/promises';

const here = path.dirname(fileURLToPath(import.meta.url));
// The migration backend (samegrade). Reports and live run results are read from it; nothing is written.
const SAMEGRADE = path.resolve(process.env.SAMEGRADE_DIR || path.join(here, '..', 'samegrade'));
const files = {'/':'index.html','/index.html':'index.html','/app.js':'app.js','/styles.css':'styles.css','/results.json':'results.json'};
const types = {html:'text/html; charset=utf-8',js:'text/javascript; charset=utf-8',css:'text/css; charset=utf-8',json:'application/json'};
const port = Number(process.env.PORT || 4173);

// ---- Reports -----------------------------------------------------------------------------
// A report is reports/<name>/dashboard.json (agent reports), or reports/<name>/migration.json when it is
// already in the dashboard's migration shape (NER migrate.py output).
const ORDER = ['migration_agent', 'demo_e2e', 'migration_ner'];
async function listReports() {
  const dir = path.join(SAMEGRADE, 'reports');
  let names = [];
  try { names = await readdir(dir); } catch { return []; }
  const out = [];
  for (const name of names) {
    for (const file of ['dashboard.json', 'migration.json']) {
      const full = path.join(dir, name, file);
      try {
        const raw = JSON.parse(await readFile(full, 'utf8'));
        if (!raw.comparison || !Array.isArray(raw.candidates)) continue;
        out.push({id: name, title: raw.pr?.title || name, task: raw.task, updated: (await stat(full)).mtimeMs, file: full});
        break;
      } catch { /* not present or not a dashboard report */ }
    }
  }
  const rank = id => { const i = ORDER.findIndex(p => id.startsWith(p)); return i < 0 ? ORDER.length : i; };
  return out.sort((a, b) => rank(a.id) - rank(b.id) || a.id.localeCompare(b.id));
}

// ---- Live results ------------------------------------------------------------------------
// Polls samegrade/results/*.jsonl once a second and streams rows appended after the server started.
// Files present at startup are history (read from their end); files created later stream from the start.
const clients = new Set(), offsets = new Map(), recent = [];
let started = false;
function summarize(file, row) {
  const ents = Array.isArray(row.output) ? row.output.map(e => `${e.text}/${e.label}`).join(', ') : null;
  const label = file.startsWith('judge__') ? `judge: ${row.winner ?? '?'}`
    : row.task === 'agent' ? `${row.category ?? '?'} / ${row.decision?.action ?? '?'}`
    : row.task === 'textcat' ? String(row.gold_score?.pred ?? '') : ents;
  return {file, at: Date.now(), task: row.task || (file.startsWith('judge__') ? 'judge' : null),
    input_id: row.input_id ?? null, run: row.run ?? null, model: row.config || row.model || null, label,
    passed: typeof row.passed_all === 'boolean' ? row.passed_all : null,
    score: typeof row.gold_score?.score === 'number' ? row.gold_score.score : null,
    error: row.error ? String(row.error).slice(0, 240) : null};
}
function send(res, event, payload) { res.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`); }
async function scan() {
  const dir = path.join(SAMEGRADE, 'results');
  let names = [];
  try { names = (await readdir(dir)).filter(n => n.endsWith('.jsonl')); } catch { return; }
  for (const name of names) {
    const full = path.join(dir, name);
    let size;
    try { size = (await stat(full)).size; } catch { continue; }
    if (!offsets.has(name)) offsets.set(name, started ? 0 : size);
    const from = offsets.get(name);
    if (size < from) { offsets.set(name, size); continue; }
    if (size === from) continue;
    const fh = await open(full, 'r');
    const buf = Buffer.alloc(size - from);
    await fh.read(buf, 0, buf.length, from);
    await fh.close();
    const text = buf.toString('utf8'), end = text.lastIndexOf('\n');
    if (end < 0) continue;                                              // wait for a complete line
    offsets.set(name, from + Buffer.byteLength(text.slice(0, end + 1)));
    for (const line of text.slice(0, end).split('\n')) {
      if (!line.trim()) continue;
      let row; try { row = JSON.parse(line); } catch { continue; }
      const ev = summarize(name, row);
      recent.push(ev); if (recent.length > 300) recent.shift();
      for (const res of clients) send(res, 'row', ev);
    }
  }
  started = true;
}
await scan();
setInterval(scan, 1000);
setInterval(() => { for (const res of clients) res.write(': keep-alive\n\n'); }, 15000);

// ---- Server ------------------------------------------------------------------------------
function sendJSON(res, status, body) {
  res.writeHead(status, {'Content-Type': types.json, 'Cache-Control': 'no-store'});
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
}
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/api/reports') {
    return sendJSON(res, 200, (await listReports()).map(({file, ...r}) => r));
  }
  if (url.pathname === '/api/report') {
    const hit = (await listReports()).find(r => r.id === url.searchParams.get('id'));   // whitelist: no paths
    if (!hit) return sendJSON(res, 404, {error: 'unknown report'});
    try { return sendJSON(res, 200, await readFile(hit.file, 'utf8')); }
    catch { return sendJSON(res, 500, {error: 'could not read report'}); }
  }
  if (url.pathname === '/api/live') {
    res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive'});
    send(res, 'hello', {backend: SAMEGRADE, recent: recent.slice(-60)});
    clients.add(res);
    req.on('close', () => clients.delete(res));
    return;
  }
  const file = files[url.pathname];
  if (!file) { res.writeHead(404); res.end('Not found'); return; }
  try {
    const body = await readFile(new URL(file, import.meta.url));
    res.writeHead(200, {'Content-Type': types[file.split('.').pop()], 'Cache-Control': 'no-store'});
    res.end(body);
  } catch { res.writeHead(500); res.end('Could not read project file'); }
}).listen(port, '127.0.0.1', () => console.log(`Switchyard preview: http://localhost:${port}  (backend: ${SAMEGRADE})`));
