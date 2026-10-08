// Savings, evidence and pull-request views for reports that carry a `demo` block (results_demo.py).
// app.js inserts demoSection() under the report intro and calls bindDemo() after each render.
// Every number shown comes from data.demo / data.candidates; the only input the viewer supplies is request volume.

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const usd = (n, d = 2) => typeof n === 'number' ? '$' + n.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d}) : 'n/a';
const pct = n => typeof n === 'number' ? `${(n * 100).toFixed(1)}%` : 'n/a';
const frac = o => o ? `${o.n}/${o.of}` : '–';
const TABS = [['savings', 'Savings'], ['evidence', 'Evidence'], ['pr', 'Pull request']];

export const demoState = {
  tab: TABS.some(([id]) => location.hash === '#' + id) ? location.hash.slice(1) : 'savings',
  volume: null, decisions: {}, editing: null, edits: {},
};

function headline(d) {
  const rec = d.recommended, old = d.totals_usd_per_1k.all_old;
  return `Switch to <span class="dm-mono">${esc(rec.assignment.classify)}</span> with a 1-line prompt fix: ` +
    `${Math.round((1 - rec.usd_per_1k / old) * 100)}% lower model cost, 0 regressions on ${d.confidence.dev_tickets + d.confidence.heldout_tickets} tickets`;
}

function savingsView(data) {
  const d = data.demo, old = d.totals_usd_per_1k.all_old, rec = d.recommended, q = d.quality;
  const recKey = rec.config === 'mix' ? 'mix' : rec.config === 'all_new' ? 'all_new' : 'all_old';
  const vol = demoState.volume ?? d.monthly_requests_default;
  const max = Math.max(...d.per_step.map(s => s.all_old?.usd_per_1k || 0));
  const bar = (v, cls) => `<span class="dm-bar ${cls}" style="width:${Math.max(1, (v || 0) / max * 100)}%"></span>`;
  const rows = d.per_step.map(s => {
    const after = s[recKey];
    const save = s.all_old && after ? 1 - after.usd_per_1k / s.all_old.usd_per_1k : null;
    return `<tr><th scope="row" class="dm-mono">${esc(s.step)}</th>
      <td>${esc(s.all_old?.model)}</td><td>${esc(after?.model)}</td>
      <td class="dm-num">${usd(s.all_old?.usd_per_1k)}</td><td class="dm-num">${usd(after?.usd_per_1k)}</td>
      <td class="dm-num dm-good">${save === null ? 'n/a' : '−' + Math.round(save * 100) + '%'}</td>
      <td class="dm-bars" aria-hidden="true">${bar(s.all_old?.usd_per_1k, 'old')}${bar(after?.usd_per_1k, 'new')}</td></tr>`;
  }).join('');
  const qbar = (label, v, cls) => `<div class="dm-qrow"><span>${label}</span><span class="dm-qtrack"><span class="dm-qfill ${cls}" style="width:${v * 100}%"></span></span><strong>${pct(v)}</strong></div>`;
  const mix = d.mix;
  return `
  <div class="dm-kpis">
    <div class="dm-kpi"><span>Model cost per 1,000 tickets</span><strong>${usd(old)} → ${usd(rec.usd_per_1k)}</strong><small>gpt-4 today → ${esc(rec.assignment.classify)} + fix</small></div>
    <div class="dm-kpi"><span>Tickets that got worse</span><strong>${d.confidence.regressed_dev + d.confidence.regressed_heldout_before} → ${data.candidates[0].summary.verdict_counts_after_fix?.REGRESSED ?? '–'}</strong><small>of ${d.confidence.dev_tickets + d.confidence.heldout_tickets}, before → after the fix</small></div>
    <div class="dm-kpi"><span>Replies passing every check</span><strong>${pct(q.old.passed_all)} → ${pct(q.new_fixed.passed_all)}</strong><small>${pct(q.new_as_is.passed_all)} if you switch without the fix</small></div>
  </div>
  <div class="dm-grid">
    <section class="dm-card" aria-labelledby="dm-calc">
      <h3 id="dm-calc">Your monthly savings</h3>
      <label class="dm-label" for="dm-volume">Tickets per month</label>
      <input id="dm-volume" class="dm-input" type="number" min="0" step="1000" inputmode="numeric" value="${vol}">
      <dl class="dm-money" aria-live="polite">
        <div><dt>Today (gpt-4)</dt><dd id="dm-today">${usd(vol * old / 1000, 0)}</dd></div>
        <div><dt>After the switch</dt><dd id="dm-after">${usd(vol * rec.usd_per_1k / 1000, 0)}</dd></div>
        <div class="dm-save"><dt>You save</dt><dd><span id="dm-save">${usd(vol * (old - rec.usd_per_1k) / 1000, 0)}</span> / month</dd></div>
      </dl>
      <p class="dm-foot">Volume is your number. Cost per ticket is measured: real token counts per step × list prices (${esc(Object.values(d.prices)[0]?.source)}, checked ${esc(Object.values(d.prices)[0]?.checked)}). Model cost only.</p>
    </section>
    <section class="dm-card" aria-labelledby="dm-quality">
      <h3 id="dm-quality">Quality stays where it was</h3>
      ${qbar('gpt-4 (today)', q.old.passed_all, 'old')}
      ${qbar(esc(rec.assignment.classify) + ', no fix', q.new_as_is.passed_all, 'warn')}
      ${qbar(esc(rec.assignment.classify) + ' + fix', q.new_fixed.passed_all, 'new')}
      <p class="dm-foot">Share of ${q.new_fixed.runs} runs passing every hard check. Gold score (right category, order, action, reply content): ${q.old.gold.toFixed(3)} today, ${q.new_fixed.gold.toFixed(3)} after.</p>
    </section>
  </div>
  <section class="dm-card" aria-labelledby="dm-steps">
    <h3 id="dm-steps">Cost per step, per 1,000 tickets</h3>
    <div class="dm-scroll"><table class="dm-table">
      <thead><tr><th scope="col">Step</th><th scope="col">Model today</th><th scope="col">Model after</th><th scope="col" class="dm-num">Today</th><th scope="col" class="dm-num">After</th><th scope="col" class="dm-num">Saving</th><th scope="col"><span class="dm-key old"></span>today <span class="dm-key new"></span>after</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><th scope="row">Total</th><td></td><td></td><td class="dm-num">${usd(old)}</td><td class="dm-num">${usd(rec.usd_per_1k)}</td><td class="dm-num dm-good">−${Math.round((1 - rec.usd_per_1k / old) * 100)}%</td><td></td></tr></tfoot>
    </table></div>
    <p class="dm-foot">Also tested: the cheapest model per step (${Object.entries(mix.assignment).map(([s, m]) => `${esc(s)} → ${esc(m)}`).join(', ')}) at ${usd(d.totals_usd_per_1k.mix)} per 1,000. It is <strong>not recommended</strong>: ${mix.heldout.REGRESSED} held-out ticket${mix.heldout.REGRESSED === 1 ? '' : 's'} (${mix.heldout_regressed.map(esc).join(', ')}) got worse.</p>
  </section>
  ${poolMix(d)}
  ${modelTable(d)}`;
}

// Cost cell: dollars for API models; tokens for self-hosted ones (no list price, so no dollar figure).
const costCell = m => m.usd_per_1k != null ? usd(m.usd_per_1k) : m.tokens_per_ticket != null ? `${Math.round(m.tokens_per_ticket).toLocaleString('en-US')} tokens/ticket` : 'n/a';
const worse = v => v ? `<span class="${v.regressed ? 'dm-bad' : 'dm-good'}">${v.regressed}</span><span class="dm-of">/${v.of}</span>${v.errors ? ` <span class="dm-pill warn">${v.errors} errors</span>` : ''}` : '–';

function modelTable(d) {
  if (!d.models) return '';
  const rank = m => m.role === 'today' ? -1 : m.usd_per_1k ?? -0.5;
  const rows = [...d.models].sort((a, b) => rank(a) - rank(b)).map(m => `<tr${m.role === 'today' ? ' class="dm-today"' : ''}>
    <th scope="row" class="dm-mono">${esc(m.model)}${m.role === 'today' ? ' <span class="dm-pill">today</span>' : ''}</th>
    <td>${m.hosting === 'self-hosted' ? 'Your hardware (open weights)' : 'OpenAI API'}</td>
    <td class="dm-num">${costCell(m)}</td>
    <td class="dm-num">${worse(m.sets.dev)}</td><td class="dm-num">${worse(m.sets.hard)}</td><td class="dm-num">${worse(m.sets.holdout2)}</td>
    <td class="dm-num">${m.sets.dev ? pct(m.sets.dev.passed_all) : '–'}</td></tr>`).join('');
  return `<section class="dm-card" aria-labelledby="dm-models">
    <h3 id="dm-models">Every model we tested, alone</h3>
    <div class="dm-scroll"><table class="dm-table">
      <thead><tr><th scope="col">Model</th><th scope="col">Runs on</th><th scope="col" class="dm-num">Cost per 1,000 tickets</th>
        <th scope="col" class="dm-num">Worse: dev</th><th scope="col" class="dm-num">hard</th><th scope="col" class="dm-num">fresh</th><th scope="col" class="dm-num">Passing every check (dev)</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="dm-foot">Each model runs the whole agent with no prompt changes, 3 runs per ticket, compared with gpt-4 on 24 dev, 12 hard and 12 fresh tickets. ${esc(d.pool?.self_hosted_note || '')}</p>
  </section>`;
}

function poolMix(d) {
  const p = d.pool;
  if (!p) return '';
  const ladder = Object.entries(p.ladder).map(([s, tried]) => `<li><span class="dm-mono">${esc(s)}</span>: ${tried.map(t =>
    `<span class="${t.passed ? 'dm-good' : 'dm-bad'}">${esc(t.model)} ${t.passed ? '✓' : `✗ (${t.regressed} worse${t.errors ? ', errors' : ''})`}</span>`).join(' → ')}${tried.some(t => t.passed) ? '' : ` → stays on ${esc(p.base)}`}</li>`).join('');
  const m = p.mix;
  const verdict = !m ? `<p><strong>No cheaper mix passed the dev tickets</strong>, so the recommendation stays ${esc(p.base)} + fix.</p>` :
    `<p><strong>${m.accepted ? 'Passed' : 'Failed'}</strong> on 12 fresh tickets no selection step had seen: ${m.holdout2.verdict_counts.REGRESSED} got worse${m.holdout2.regressed.length ? ` (${m.holdout2.regressed.map(esc).join(', ')})` : ''}. Cost: ${usd(m.api_usd_per_1k)} per 1,000 tickets in API calls${m.self_hosted_steps.length ? ` plus ${Math.round(m.self_hosted_tokens_per_ticket).toLocaleString('en-US')} self-hosted tokens per ticket (${m.self_hosted_steps.map(esc).join(', ')})` : ''}.</p>
     <p class="dm-foot">Mix: ${Object.entries(m.assignment).map(([s, x]) => `${esc(s)} → <span class="dm-mono">${esc(x)}</span>`).join(', ')}. For comparison, ${esc(p.base)} + fix alone on the same fresh tickets: ${p.sol_patch_holdout2.regressed} got worse.</p>`;
  return `<section class="dm-card" aria-labelledby="dm-pool">
    <h3 id="dm-pool">Cheapest model per step, from a wider pool</h3>
    <ul class="dm-ladder">${ladder}</ul>
    ${verdict}
    <p class="dm-foot">${esc(p.rule)}</p>
  </section>`;
}

function evidenceView(data) {
  const d = data.demo, c = d.confidence, lines = d.lines, b = d.benchmark;
  const steps = d.per_step.map(s => `<tr class="${s.guilty ? 'dm-guilty' : ''}"><th scope="row" class="dm-mono">${esc(s.step)}${s.guilty ? ' <span class="dm-pill warn">cause</span>' : ''}</th>
    <td class="dm-num">${frac(s.repair)}</td><td class="dm-num">${s.guilty ? frac(s.reproduce) : '–'}</td></tr>`).join('');
  const lineRows = lines.results.map(r => `<li class="${r.repairs ? 'dm-guilty' : ''}"><span class="dm-ln">${r.line_index + 1}</span><span class="dm-mono">${esc(r.line)}</span><strong class="dm-num">${r.repairs}/${r.of}</strong></li>`).join('');
  const ex = d.examples.map(e => `<article class="dm-example">
    <h4>${esc(e.input_id)} · ${e.split === 'heldout' ? 'held-out ticket (the fixer never saw it)' : 'dev ticket'}</h4>
    <p class="dm-ticket">“${esc(e.text)}”</p>
    <div class="dm-three">
      <div><span class="dm-col">gpt-4 (today)</span><p>${esc(e.old.final_reply)}</p></div>
      <div class="bad"><span class="dm-col">${esc(d.pair.new)}, no fix</span><p>${esc(e.new.final_reply)}</p></div>
      <div class="good"><span class="dm-col">${esc(d.pair.new)} + fix</span><p>${esc(e.fixed.final_reply)}</p></div>
    </div></article>`).join('');
  return `
  <p class="dm-strip">${c.runs_per_ticket} runs per ticket · ${c.dev_tickets} dev tickets · ${c.heldout_tickets} held-out tickets · gpt-4 gave different results between runs on ${c.old_model_unstable_tickets} tickets · real model runs, hand-written tickets</p>
  <div class="dm-grid">
    <section class="dm-card" aria-labelledby="dm-which">
      <h3 id="dm-which">Which step broke</h3>
      <table class="dm-table"><thead><tr><th scope="col">Step</th><th scope="col" class="dm-num">Repair</th><th scope="col" class="dm-num">Reproduce</th></tr></thead><tbody>${steps}</tbody></table>
      <p class="dm-foot"><strong>Repair:</strong> new model everywhere, gpt-4 put back at this step only; how many of the ${c.regressed_dev} broken dev tickets pass again. <strong>Reproduce:</strong> gpt-4 everywhere, the new model at this step only; how many break again. 3 runs each.</p>
    </section>
    <section class="dm-card" aria-labelledby="dm-lines">
      <h3 id="dm-lines">Which line in <span class="dm-mono">${esc(lines.step)}</span></h3>
      <ol class="dm-lines">${lineRows}</ol>
      <p class="dm-foot">Each line removed on its own, new model everywhere, 3 runs: how many broken tickets it repairs.</p>
    </section>
  </div>
  <section class="dm-card" aria-labelledby="dm-ex"><h3 id="dm-ex">Before and after</h3>${ex}</section>
  <section class="dm-card dm-synthetic" aria-labelledby="dm-bench">
    <h3 id="dm-bench">How often the step-finder is right <span class="dm-pill">synthetic benchmark</span></h3>
    <p>On ${b.held_out_exact.of} tickets broken by 9 planted prompt bugs, held out: exact step <strong>${frac(b.held_out_exact)}</strong>, confirmed both ways <strong>${frac(b.two_way_confirmed)}</strong>, wrong step <strong>${frac(b.wrong_step)}</strong>.</p>
    <p class="dm-foot">Planted bugs, not real migrations (${esc(b.source)}).</p>
  </section>`;
}

function prChanges(data) {
  const d = data.demo, p = d.patch;
  return [
    {id: 'model', where: 'All 4 steps', summary: `Model ${d.pair.old} → ${d.pair.new}`,
     old: `model: ${d.pair.old}   temperature: 0`, new: `model: ${d.pair.new}   temperature: default (0 is rejected)`},
    ...p.edits.map((e, i) => ({id: 'line' + i, where: `${p.step} prompt, line ${e.line_index + 1}`,
      summary: 'Keep the refund timeline in the tone check', old: e.old, new: e.new})),
  ];
}

function prView(data) {
  const d = data.demo, p = d.patch, changes = prChanges(data);
  const total = d.confidence.dev_tickets + d.confidence.heldout_tickets;
  const status = id => demoState.decisions[id] || 'pending';
  const label = {pending: 'Waiting for review', accepted: 'Accepted', rejected: 'Rejected', edited: 'Edited: re-run needed'};
  const items = changes.map((ch, i) => {
    const text = demoState.edits[ch.id] ?? ch.new;
    const editing = demoState.editing === ch.id;
    return `<article class="dm-change" data-change="${ch.id}">
      <div class="dm-change-head"><div><span class="dm-col">Change ${i + 1} · ${esc(ch.where)}</span><h4>${esc(ch.summary)}</h4></div>
        <span class="dm-pill ${status(ch.id)}">${label[status(ch.id)]}</span></div>
      <pre class="dm-diff"><span class="del">− ${esc(ch.old)}</span>\n<span class="add">+ ${esc(text)}</span></pre>
      ${editing ? `<label class="dm-label" for="dm-edit-${ch.id}">Your version</label><textarea id="dm-edit-${ch.id}" class="dm-input dm-textarea" rows="3">${esc(text)}</textarea>
        <div class="dm-actions"><button class="button primary" data-act="save">Save edit</button><button class="button" data-act="cancel">Cancel</button></div>` :
      `<div class="dm-actions"><button class="button primary" data-act="accept">Accept</button><button class="button" data-act="edit">Edit</button><button class="button dm-reject" data-act="reject">Reject</button></div>`}
      ${status(ch.id) === 'edited' ? `<p class="dm-foot">An edited change hasn't been tested. Re-run the suite with your line before merging.</p>` : ''}
    </article>`;
  }).join('');
  const ready = changes.every(ch => status(ch.id) === 'accepted');
  return `
  <section class="dm-card dm-pr" aria-labelledby="dm-pr-title">
    <div class="dm-change-head"><div><span class="dm-col">Draft pull request · ${changes.length} changes</span><h3 id="dm-pr-title">${esc(data.pr.title)}</h3></div>
      <span class="dm-pill ${ready ? 'accepted' : 'pending'}" aria-live="polite">${ready ? 'Ready to merge' : 'Needs review'}</span></div>
    <ul class="dm-summary">
      <li><strong>Where:</strong> the <span class="dm-mono">${esc(p.step)}</span> step. Putting ${esc(d.pair.old)} back at that step alone repairs ${frac(d.per_step.find(s => s.guilty).repair)} broken tickets; no other step repairs any.</li>
      <li><strong>What:</strong> ${p.edits.length} prompt line, written by ${esc(p.fixer)} from the dev tickets only.</li>
      <li><strong>Proof:</strong> tickets that got worse ${d.confidence.regressed_dev + d.confidence.regressed_heldout_before} → ${data.candidates[0].summary.verdict_counts_after_fix?.REGRESSED} of ${total}, including ${p.heldout_before.REGRESSED} → ${p.heldout_after.REGRESSED} on ${d.confidence.heldout_tickets} held-out tickets the fixer never saw.</li>
      <li><strong>Cost:</strong> ${usd(d.totals_usd_per_1k.all_old)} → ${usd(d.recommended.usd_per_1k)} per 1,000 tickets.</li>
    </ul>
    ${items}
  </section>`;
}

export function demoSection(data) {
  if (!data?.demo) return '';
  const d = data.demo, t = demoState.tab;
  const view = t === 'evidence' ? evidenceView(data) : t === 'pr' ? prView(data) : savingsView(data);
  return `<section id="demo-panel" class="dm-panel" aria-labelledby="dm-title">
    <p class="eyebrow">Migration plan · ${esc(d.pair.old)} → ${esc(d.pair.new)}</p>
    <h2 id="dm-title" class="dm-title">${headline(d)}</h2>
    <p class="dm-prov"><span class="dm-pill">Real model runs</span> <span class="dm-pill">Hand-written tickets</span> Same prompts, tools and tickets on both sides; only the model changed.</p>
    <div class="dm-tabs" role="tablist" aria-label="Report views">${TABS.map(([id, name]) =>
      `<button role="tab" id="dm-tab-${id}" aria-controls="dm-view" aria-selected="${id === t}" tabindex="${id === t ? 0 : -1}" data-tab="${id}">${name}</button>`).join('')}</div>
    <div id="dm-view" role="tabpanel" aria-labelledby="dm-tab-${t}">${view}</div>
  </section>`;
}

export function bindDemo(root, data) {
  const panel = root.querySelector('#demo-panel');
  if (!panel) return;
  const redraw = () => { panel.outerHTML = demoSection(data); bindDemo(root, data); };
  const tabs = [...panel.querySelectorAll('[role=tab]')];
  tabs.forEach((b, i) => {
    b.onclick = () => { demoState.tab = b.dataset.tab; history.replaceState(null, '', '#' + b.dataset.tab); redraw(); root.querySelector(`#dm-tab-${demoState.tab}`)?.focus(); };
    b.onkeydown = e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length].click();
    };
  });
  const vol = panel.querySelector('#dm-volume');
  if (vol) vol.oninput = () => {
    const d = data.demo, v = Math.max(0, Number(vol.value) || 0), old = d.totals_usd_per_1k.all_old, rec = d.recommended.usd_per_1k;
    demoState.volume = v;
    panel.querySelector('#dm-today').textContent = usd(v * old / 1000, 0);
    panel.querySelector('#dm-after').textContent = usd(v * rec / 1000, 0);
    panel.querySelector('#dm-save').textContent = usd(v * (old - rec) / 1000, 0);
  };
  panel.querySelectorAll('.dm-change').forEach(el => {
    const id = el.dataset.change;
    el.querySelectorAll('[data-act]').forEach(btn => btn.onclick = () => {
      const act = btn.dataset.act;
      if (act === 'accept') demoState.decisions[id] = 'accepted';
      if (act === 'reject') demoState.decisions[id] = 'rejected';
      if (act === 'edit') demoState.editing = id;
      if (act === 'cancel') demoState.editing = null;
      if (act === 'save') {
        demoState.edits[id] = el.querySelector('textarea').value;
        demoState.decisions[id] = 'edited';
        demoState.editing = null;
      }
      redraw();
      root.querySelector(`[data-change="${id}"] ${act === 'edit' ? 'textarea' : 'button'}`)?.focus();
    });
  });
}
