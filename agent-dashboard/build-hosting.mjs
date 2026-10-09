import { mkdir, copyFile, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const backend = path.join(here, 'backend');
const output = path.join(here, '.hosting-assets');
const dataOutput = path.join(output, 'data');
const reportOutput = path.join(dataOutput, 'reports');
const runOutput = path.join(dataOutput, 'runs');
const reportFiles = ['dashboard.json', 'migration.json'];
const order = ['migration_agent', 'demo_e2e', 'migration_ner'];

await mkdir(output, { recursive: true });
for (const name of await readdir(output)) {
  if (!['index.html', 'app.js', 'styles.css', 'results.json', 'data'].includes(name)) {
    throw new Error(`Unexpected hosting asset: ${name}`);
  }
}
await rm(dataOutput, { recursive: true, force: true });
await mkdir(reportOutput, { recursive: true });
await mkdir(runOutput, { recursive: true });

for (const file of ['index.html', 'styles.css', 'results.json']) {
  await copyFile(path.join(here, file), path.join(output, file));
}
const app = await readFile(path.join(here, 'app.js'), 'utf8');
if (!app.includes('const HOSTED_RECORDING = false;')) throw new Error('Could not prepare hosted replay controls.');
await writeFile(path.join(output, 'app.js'), app.replace('const HOSTED_RECORDING = false;', 'const HOSTED_RECORDING = true;'));

const reportsDir = path.join(backend, 'reports');
const runsDir = path.join(backend, 'results');
const reports = [];
const publishedRuns = new Set();
for (const id of await readdir(reportsDir)) {
  if (!/^[\w.-]+$/.test(id)) continue;
  const dir = path.join(reportsDir, id);
  if (!(await stat(dir)).isDirectory()) continue;
  let sourceName = null;
  let report = null;
  for (const name of reportFiles) {
    try {
      const candidate = JSON.parse(await readFile(path.join(dir, name), 'utf8'));
      if (candidate.comparison && Array.isArray(candidate.candidates)) {
        sourceName = name;
        report = candidate;
        break;
      }
    } catch { /* Ignore reports that are not in a dashboard-compatible shape. */ }
  }
  if (!sourceName) continue;

  await copyFile(path.join(dir, sourceName), path.join(reportOutput, `${id}.json`));
  const seen = new Map();
  for (const name of await readdir(dir)) {
    if (!name.startsWith('compare_') || !name.endsWith('.json')) continue;
    let comparison;
    try { comparison = JSON.parse(await readFile(path.join(dir, name), 'utf8')); } catch { continue; }
    const repaired = name.includes('fix') || name.includes('edit');
    const related = [[comparison.candidate_file, repaired ? `fix re-run (${name.replace(/^compare_|\.json$/g, '')})` : 'candidate run']];
    for (const file of String(comparison.baseline_file || '').split(' + ')) related.push([file.trim(), 'baseline run']);
    for (const [file, role] of related) {
      const basename = path.basename(String(file || ''));
      if (!basename.endsWith('.jsonl') || basename === '.jsonl' || seen.has(basename)) continue;
      const source = path.join(runsDir, basename);
      try {
        const contents = await readFile(source);
        const rows = contents.toString('utf8').split('\n').filter(Boolean).length;
        await copyFile(source, path.join(runOutput, basename));
        seen.set(basename, { file: basename, role, rows });
        publishedRuns.add(basename);
      } catch { /* A report can reference results that were not retained. */ }
    }
  }
  const roleOrder = { 'baseline run': 0, 'candidate run': 1 };
  const runs = [...seen.values()].sort((a, b) => (roleOrder[a.role] ?? 2) - (roleOrder[b.role] ?? 2) || a.file.localeCompare(b.file));
  const metadata = await stat(path.join(dir, sourceName));
  reports.push({ id, title: report.pr?.title || id, task: report.task || null, group: report.group || 'Migrations', label: report.label || null, updated: metadata.mtimeMs, runs });
}
const rank = id => { const index = order.findIndex(prefix => id.startsWith(prefix)); return index < 0 ? order.length : index; };
reports.sort((a, b) => rank(a.id) - rank(b.id) || a.id.localeCompare(b.id));
await writeFile(path.join(dataOutput, 'manifest.json'), JSON.stringify({ reports }));
console.log(`Prepared ${reports.length} selectable reports and ${publishedRuns.size} replay files. No backend code or API keys are published.`);
