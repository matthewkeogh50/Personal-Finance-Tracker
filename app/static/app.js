/* Household Finances dashboard. Vanilla JS + Chart.js; talks to the same-origin /api. */
'use strict';

// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = new Intl.NumberFormat('de-CH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat('de-CH', { maximumFractionDigits: 0 });
const money = (v, cur = 'CHF') => (v == null ? '–' : `${cur} ${nf.format(v)}`);
const money0 = (v) => (v == null ? '–' : `CHF ${nf0.format(v)}`);
const kpiVal = (v, cur = 'CHF') => (v == null ? '–' : `<small>${cur}</small>${nf0.format(v)}`);
const pct = (v) => (v == null ? '–' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);
const signCls = (v) => (v == null || v === 0 ? '' : v > 0 ? 'pos' : 'neg');
const today = () => new Date().toISOString().slice(0, 10);
const monthLabel = (m, long) => new Date(m + '-15').toLocaleDateString('en-GB', long ? { month: 'long', year: 'numeric' } : { month: 'short', year: '2-digit' });
const shiftMonth = (m, d) => { const dt = new Date(Date.UTC(+m.slice(0, 4), +m.slice(5, 7) - 1 + d, 1)); return dt.toISOString().slice(0, 7); };
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const series = (i) => cssVar(`--s${i + 1}`);

let state = { month: new Date().toISOString().slice(0, 7), people: [], categories: [] };
let charts = [];
let actions = {};

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2600);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const key = localStorage.getItem('apiKey');
  if (key) headers['X-API-Key'] = key;
  if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.json); }
  const res = await fetch('/api' + path, { ...opts, headers });
  if (res.status === 401) {
    const k = prompt('This server requires an API key:');
    if (k) { localStorage.setItem('apiKey', k.trim()); return api(path, opts); }
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : d ? d.map((x) => `${x.loc.slice(1).join('.')}: ${x.msg}`).join('; ') : res.statusText);
  }
  return data;
}

async function loadRefs() {
  [state.people, state.categories] = await Promise.all([api('/people'), api('/categories')]);
}
const catOptions = (blank = '— none —') => [['', blank], ...state.categories.filter((c) => !c.archived).map((c) => [c.id, c.name])];
const personOptions = (blank) => [...(blank ? [['', blank]] : []), ...state.people.filter((p) => p.active).map((p) => [p.id, p.name])];
const CURRENCIES = [['CHF', 'CHF'], ['GBP', 'GBP'], ['EUR', 'EUR'], ['USD', 'USD']];

// ---------------------------------------------------------------- modal form
function openForm(title, fields, onSubmit, submitLabel = 'Save') {
  const dlg = $('#dlg'), form = $('#dlgform');
  form.innerHTML = `<h2>${esc(title)}</h2>` + fields.map((f) => {
    const req = f.required ? 'required' : '';
    const val = f.value ?? '';
    let input;
    if (f.type === 'select') {
      input = `<select name="${f.name}" ${req}>${f.options.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(val) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
    } else if (f.type === 'file') {
      input = `<input type="file" name="${f.name}" accept="image/*,application/pdf" ${req}>`;
    } else {
      input = `<input type="${f.type || 'text'}" name="${f.name}" value="${esc(val)}" ${req} ${f.type === 'number' ? `step="${f.step || '0.01'}"` : ''} ${f.placeholder ? `placeholder="${esc(f.placeholder)}"` : ''}>`;
    }
    return `<label>${esc(f.label)}${input}</label>`;
  }).join('') + `<div class="err" id="dlgerr"></div><div class="row"><button type="button" id="dlgcancel">Cancel</button><button class="primary" type="submit">${esc(submitLabel)}</button></div>`;
  $('#dlgcancel').onclick = () => dlg.close();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const out = {};
    for (const f of fields) {
      const el = form.elements[f.name];
      if (f.type === 'file') { out[f.name] = el.files[0]; continue; }
      const v = el.value.trim();
      if (v === '') out[f.name] = null;
      else if (f.type === 'number' || f.numeric) out[f.name] = Number(v);
      else out[f.name] = v;
    }
    try { await onSubmit(out); dlg.close(); toast('Saved'); render(); } catch (err) { $('#dlgerr').textContent = err.message; }
  };
  dlg.showModal();
}

async function confirmDelete(path, what) {
  if (!confirm(`Delete ${what}? This cannot be undone.`)) return;
  try { await api(path, { method: 'DELETE' }); toast('Deleted'); render(); } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- charts
function chart(id, type, data, extra = {}) {
  const multi = data.datasets.length > 1;
  const fmt = extra.fmt || ((v) => money(v));
  const c = new Chart($('#' + id), {
    type, data,
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: multi, position: 'bottom', labels: { color: cssVar('--ink-2'), usePointStyle: true, pointStyle: 'rectRounded', boxWidth: 10, boxHeight: 10, padding: 14 } },
        tooltip: {
          backgroundColor: cssVar('--surface'), titleColor: cssVar('--ink'), bodyColor: cssVar('--ink-2'), borderColor: cssVar('--axis'), borderWidth: 1, padding: 10,
          filter: (i) => i.parsed.y != null && (!extra.stacked || i.parsed.y !== 0),
          callbacks: {
            label: (i) => ` ${i.dataset.label}: ${fmt(i.parsed.y)}`,
            footer: extra.stacked ? (items) => `Total: ${fmt(items.reduce((s, i) => s + (i.parsed.y || 0), 0))}` : undefined,
          },
          footerColor: cssVar('--ink'),
        },
      },
      scales: {
        x: { stacked: !!extra.stacked, grid: { display: false }, border: { color: cssVar('--axis') }, ticks: { color: cssVar('--muted'), maxRotation: 0, autoSkipPadding: 12 } },
        y: { stacked: !!extra.stacked, beginAtZero: extra.zero !== false, border: { display: false }, grid: { color: cssVar('--grid') },
          ticks: { color: cssVar('--muted'), maxTicksLimit: 6, callback: extra.tick || ((v) => (Math.abs(v) >= 1000 ? `${nf0.format(v / 1000)}k` : v)) } },
      },
    },
  });
  charts.push(c);
}
const barSet = (label, data, color, stack) => ({ label, data, backgroundColor: color, borderRadius: 4, borderWidth: stack ? { top: 2 } : 0, borderColor: cssVar('--surface'), maxBarThickness: 34, categoryPercentage: 0.7, barPercentage: 0.9 });
const lineSet = (label, data, color, fill) => ({ label, data, borderColor: color, backgroundColor: fill ? color + '55' : color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 12, tension: 0.25, fill: fill || false, spanGaps: true });

/** Keep the first `max` entries as their own series and fold the rest into "Other". */
function foldSeries(items, getData, max = 7) {
  const keep = items.slice(0, max), rest = items.slice(max);
  const out = keep.map((it) => ({ label: it.label, data: getData(it) }));
  if (rest.length) {
    const len = getData(rest[0]).length;
    out.push({ label: 'Other', other: true, data: Array.from({ length: len }, (_, i) => rest.reduce((s, it) => s + (getData(it)[i] || 0), 0)) });
  }
  return out;
}

// ---------------------------------------------------------------- shared fragments
const card = (title, body, right = '', sub = '') => `<section class="card"><div class="card-head"><div><h2>${title}</h2>${sub ? `<div class="sub">${sub}</div>` : ''}</div><div class="filters">${right}</div></div>${body}</section>`;
const kpi = (label, value, hint = '', cls = '') => `<section class="card kpi"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="hint">${hint}</div></section>`;
const btn = (act, label, id = '', cls = 'small') => `<button type="button" class="${cls}" data-act="${act}" data-id="${id}">${label}</button>`;
const table = (heads, rowsHtml, empty = 'Nothing recorded yet.') => (rowsHtml.length ? `<div class="scroll"><table><thead><tr>${heads.map((h) => `<th class="${h.startsWith('#') ? 'num' : ''}">${h.replace(/^#/, '')}</th>`).join('')}</tr></thead><tbody>${rowsHtml.join('')}</tbody></table></div>` : `<div class="empty">${empty}</div>`);

function meters(items) {
  const list = items.filter((i) => i.budget_chf != null || i.spent_chf !== 0);
  if (!list.length) return '<div class="empty">No spending recorded for this month.</div>';
  const top = Math.max(...list.map((i) => i.spent_chf), 0);
  return list.map((i) => {
    const has = i.budget_chf != null;   // budgets can still be set over the API; shown when present
    const w = has ? Math.min(100, (i.spent_chf / i.budget_chf) * 100) : top > 0 ? (i.spent_chf / top) * 100 : 0;
    const note = !has ? '' : i.over_budget ? `⚠ Over by ${money(-i.remaining_chf)}` : `${money(i.remaining_chf)} left`;
    return `<div class="meter"><div class="meter-head"><span>${esc(i.category)}</span><span class="amt">${money0(i.spent_chf)}${has ? ` / ${nf0.format(i.budget_chf)}` : ''}</span></div>
      <div class="bar"><i class="${i.over_budget ? 'over' : ''}" style="width:${Math.max(0, w)}%;${has ? '' : 'background:var(--other)'}"></i></div>
      ${note ? `<div class="note ${i.over_budget ? 'over' : ''}">${note}</div>` : ''}</div>`;
  }).join('');
}

function txFields(t = {}) {
  return [
    { name: 'date', label: 'Date', type: 'date', value: t.date || (state.month === today().slice(0, 7) ? today() : state.month + '-01'), required: true },
    { name: 'description', label: 'Description', value: t.description, required: true },
    { name: 'amount', label: 'Amount (positive = spent, negative = refund)', type: 'number', value: t.amount, required: true },
    { name: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, value: t.currency || 'CHF' },
    { name: 'category_id', label: 'Category', type: 'select', options: catOptions(), value: t.category_id, numeric: true },
    { name: 'person_id', label: 'Who (optional)', type: 'select', options: personOptions('— joint —'), value: t.person_id, numeric: true },
    { name: 'account', label: 'Account (optional)', value: t.account },
    { name: 'note', label: 'Note (optional)', value: t.note },
  ];
}
function txRows(list, showCur) {
  return list.map((t) => `<tr><td>${t.date}</td><td>${esc(t.description)}${t.recurring ? ' <span class="tag">recurring</span>' : ''}${t.is_rollup ? ' <span class="tag ok">pulled from GBP</span>' : ''}${t.rollup_id ? ' <span class="tag">in UK expenses</span>' : ''}${t.note ? `<div class="sub" style="color:var(--muted);font-size:12px">${esc(t.note)}</div>` : ''}</td>
    <td>${t.category ? `<span class="tag">${esc(t.category)}</span>` : '<span class="tag bad">uncategorised</span>'}</td>
    ${showCur ? `<td class="num">${money(t.amount, t.currency)}</td>` : ''}
    <td class="num">${t.currency !== 'CHF' && !showCur ? `<span class="tag">${money(t.amount, t.currency)}</span> ` : ''}${nf.format(t.amount_chf)}</td>
    <td class="actions">${btn('edit-tx', 'Edit', t.id, 'small ghost')}${btn('del-tx', 'Delete', t.id, 'small ghost danger')}</td></tr>`);
}
const txActions = (list) => ({
  'add-tx': () => openForm('Add expense', txFields(), (v) => api('/transactions', { method: 'POST', json: v })),
  'edit-tx': (id) => openForm('Edit expense', txFields(list.find((t) => t.id == id)), (v) => api(`/transactions/${id}`, { method: 'PATCH', json: v })),
  'del-tx': (id) => confirmDelete(`/transactions/${id}`, 'this expense'),
});

// ---------------------------------------------------------------- pages
const pages = {};

pages.overview = async () => {
  const m = state.month;
  const [rep, ov] = await Promise.all([api(`/reports/monthly?month=${m}`), api(`/reports/overview?months=12&end=${m}`)]);
  const prev = ov.series[ov.series.length - 2];
  const nw = ov.net_worth, nwNow = nw.total_value_chf.at(-1), nwPrev = nw.total_value_chf.at(-2);
  const sv = rep.savings;
  const vs = (now, before, invert) => (before ? `<span class="${signCls((now - before) * (invert ? -1 : 1))}">${now >= before ? '▲' : '▼'} ${money0(Math.abs(now - before))}</span> vs ${monthLabel(shiftMonth(m, -1))}` : '&nbsp;');
  const empty = !ov.totals.income_chf && !ov.totals.expenses_chf && !nw.accounts.length;
  const recRows = rep.recurring.items.map((r) => `<tr><td>${esc(r.name)}</td><td class="num">${r.currency !== 'CHF' ? `<span class="tag">${money(r.expected_amount, r.currency)}</span> ` : ''}${nf.format(r.paid ? r.paid_chf : r.expected_chf ?? 0)}</td>
    <td class="num">${r.paid ? `<span class="tag ok">✓ paid ${r.paid_date.slice(8)}.${r.paid_date.slice(5, 7)}.</span>` : '<span class="tag bad">○ not paid</span>'}</td></tr>`);

  $('#view').innerHTML = `
    ${empty ? card('Getting started', `<p>No data yet. Your agent can load everything over the API on port 1311 — see <a href="/api/agent-guide" target="_blank">the agent guide</a>. You can also enter things by hand: start in <a href="#/settings">Settings</a> (names), then <a href="#/income">Salary</a>, <a href="#/recurring">Recurring</a> and <a href="#/investments">Investments</a>.</p>`) : ''}
    <div class="grid kpis">
      ${kpi(`Income · ${monthLabel(m)}`, kpiVal(rep.income.total_chf), rep.income.missing_for.length && rep.income.total_chf ? `Nothing yet for ${esc(rep.income.missing_for.join(', '))}` : vs(rep.income.total_chf, prev?.income_chf))}
      ${kpi(`Spent · ${monthLabel(m)}`, kpiVal(rep.expenses.total_chf), vs(rep.expenses.total_chf, prev?.expenses_chf, true))}
      ${kpi('Left over', kpiVal(sv.net_chf), sv.savings_rate_pct != null ? `${sv.savings_rate_pct}% of income saved` : '&nbsp;', signCls(sv.net_chf))}
      ${kpi('Net worth', kpiVal(nwNow), vs(nwNow, nwPrev))}
    </div>
    <div class="grid two">
      ${card('Income vs expenses', '<div class="chart"><canvas id="c1"></canvas></div>', '', 'Last 12 months, CHF')}
      ${card('Net worth', nw.accounts.length ? '<div class="chart"><canvas id="c2"></canvas></div>' : '<div class="empty">Add accounts under Investments to see this.</div>', '', 'Month-end value by account, CHF')}
    </div>
    <div class="grid two">
      ${card('Spending by category', meters(rep.expenses.by_category), '<a href="#/expenses">Expenses →</a>', monthLabel(m, true))}
      <div>
        ${card('To savings this month', `<table>
            <tr><td>Income</td><td class="num">${nf.format(rep.income.total_chf)}</td></tr>
            <tr><td>Expenses recorded</td><td class="num">− ${nf.format(rep.expenses.total_chf)}</td></tr>
            <tr><td>Recurring charges still to pay</td><td class="num">− ${nf.format(sv.unpaid_recurring_chf)}</td></tr>
            <tr><td><b>Available to save</b></td><td class="num"><b class="${signCls(sv.available_to_save_chf)}">${nf.format(sv.available_to_save_chf)}</b></td></tr>
            <tr><td>Already moved to savings / investments</td><td class="num">− ${nf.format(sv.already_transferred_chf)}</td></tr>
            <tr><td><b>Still to transfer</b></td><td class="num"><b>${nf.format(sv.remaining_to_transfer_chf)}</b></td></tr></table>`, '', 'CHF')}
        <div style="height:14px"></div>
        ${card('Recurring charges', table(['Charge', '#CHF', '#Status'], recRows, 'No recurring charges defined.'), '<a href="#/recurring">Manage →</a>', monthLabel(m, true))}
      </div>
    </div>`;

  const labels = ov.months.map((x) => monthLabel(x));
  chart('c1', 'bar', { labels, datasets: [barSet('Income', ov.series.map((s) => s.income_chf), series(0)), barSet('Expenses', ov.series.map((s) => s.expenses_chf), series(1))] });
  if (nw.accounts.length) {
    const sets = foldSeries(nw.accounts.map((a) => ({ label: a.name, a })), (it) => it.a.value_chf.map((v) => v ?? 0));
    chart('c2', 'line', { labels, datasets: sets.map((s, i) => lineSet(s.label, s.data, s.other ? cssVar('--other') : series(i), i ? '-1' : 'origin')) }, { stacked: true });
  }
};

pages.income = async () => {
  const year = +state.month.slice(0, 4);
  const [sum, entries, yearly] = await Promise.all([api(`/income/summary?year=${year}`), api(`/income?year=${year}`), api('/income/yearly')]);
  const kindLabel = { salary: 'Salary', bonus: 'Bonus', '13th_salary': '13th salary', other: 'Other' };
  const fields = (e = {}) => [
    { name: 'person_id', label: 'Person', type: 'select', options: personOptions(), value: e.person_id, numeric: true, required: true },
    { name: 'date', label: 'Pay date', type: 'date', value: e.date || today(), required: true },
    { name: 'amount', label: 'Net amount paid out', type: 'number', value: e.amount, required: true },
    { name: 'gross_amount', label: 'Gross amount (optional)', type: 'number', value: e.gross_amount },
    { name: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, value: e.currency || 'CHF' },
    { name: 'kind', label: 'Type', type: 'select', options: Object.entries(kindLabel), value: e.kind || 'salary' },
    { name: 'note', label: 'Note (optional)', value: e.note },
  ];
  const rows = entries.map((e) => `<tr><td>${e.date}</td><td>${esc(e.person)}</td><td><span class="tag">${kindLabel[e.kind]}</span></td><td>${esc(e.note)}</td>
    <td class="num">${e.gross_amount != null ? nf.format(e.gross_amount) : '–'}</td><td class="num">${e.currency !== 'CHF' ? `<span class="tag">${money(e.amount, e.currency)}</span> ` : ''}${nf.format(e.amount_chf)}</td>
    <td class="actions">${btn('edit', 'Edit', e.id, 'small ghost')}${btn('del', 'Delete', e.id, 'small ghost danger')}</td></tr>`);
  const names = sum.people.map((p) => p.person);
  const yRows = yearly.slice().reverse().map((y) => `<tr><td>${y.year}</td>${sum.people.map((p) => `<td class="num">${nf.format(y.people.find((x) => x.person_id === p.person_id)?.total_chf ?? 0)}</td>`).join('')}<td class="num"><b>${nf.format(y.total_chf)}</b></td></tr>`);

  $('#view').innerHTML = `
    <div class="grid kpis">
      ${sum.people.map((p) => kpi(`${esc(p.person)} · ${year}`, kpiVal(p.total_chf), p.gross_total ? `Gross ${money0(p.gross_total)}` : 'Net received')).join('')}
      ${kpi(`Household · ${year}`, kpiVal(sum.total_chf), `Average ${money0(sum.total_chf / Math.max(1, sum.monthly_total_chf.filter((x) => x > 0).length))} per paid month`)}
    </div>
    <div class="grid two">
      ${card(`Net income by month`, '<div class="chart"><canvas id="c1"></canvas></div>', '', `${year}, CHF — use the month picker to change year`)}
      ${card('Total per year', table(['Year', ...names.map((n) => '#' + esc(n)), '#Household'], yRows), '', 'Net, CHF')}
    </div>
    ${card(`Payments in ${year}`, table(['Date', 'Person', 'Type', 'Note', '#Gross', '#Net CHF', ''], rows, 'No income recorded for this year.'), btn('add', '+ Add salary / income', '', 'primary'))}`;
  chart('c1', 'bar', { labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], datasets: sum.people.map((p, i) => barSet(p.person, p.monthly_chf, series(i), i ? '-1' : 'origin')) }, { stacked: true });
  actions = {
    add: () => openForm('Add salary / income', fields(), (v) => api('/income', { method: 'POST', json: v })),
    edit: (id) => openForm('Edit income', fields(entries.find((e) => e.id == id)), (v) => api(`/income/${id}`, { method: 'PATCH', json: v })),
    del: (id) => confirmDelete(`/income/${id}`, 'this income entry'),
  };
};

pages.expenses = async () => {
  const m = state.month;
  const [rep, txs, trend, stmts] = await Promise.all([api(`/reports/monthly?month=${m}`), api(`/transactions?month=${m}`), api(`/reports/categories?months=12&end=${m}`), api(`/statements?month=${m}`)]);
  const f = pages.expenses.filter || (pages.expenses.filter = { cat: '', q: '' });
  const key = localStorage.getItem('apiKey');
  const stRows = stmts.map((s) => `<tr><td><a href="/api/statements/${s.id}/file${key ? `?api_key=${encodeURIComponent(key)}` : ''}" target="_blank">${esc(s.filename)}</a></td><td>${esc(s.note)}</td><td class="num">${s.transactions} transactions</td><td>${s.uploaded_at.slice(0, 10)}</td><td class="actions">${btn('del-st', 'Delete', s.id, 'small ghost danger')}</td></tr>`);
  const pulled = txs.filter((t) => t.rollup_id).length;
  const draw = () => {
    const q = f.q.toLowerCase();
    const list = txs.filter((t) => !t.rollup_id && (f.cat === '' || (f.cat === 'none' ? t.category_id == null : t.category_id == f.cat)) && (!q || `${t.description} ${t.note || ''}`.toLowerCase().includes(q)));
    $('#txtable').innerHTML = table(['Date', 'Description', 'Category', '#CHF', ''], txRows(list), 'No expenses match.') +
      (list.length ? `<div class="sub" style="color:var(--muted);margin-top:8px">${list.length} transactions · ${money(list.reduce((s, t) => s + t.amount_chf, 0))}${pulled ? ` · ${pulled} GBP expenses are inside the “UK expenses” line (<a href="#/gbp">see them</a>)` : ''}</div>` : '');
  };
  $('#view').innerHTML = `
    <div class="grid kpis">
      ${kpi('Total spent', kpiVal(rep.expenses.total_chf), `${rep.expenses.transactions} transactions`)}
      ${kpi('Recurring charges', kpiVal(rep.expenses.recurring_chf), rep.recurring.unpaid.length ? `${rep.recurring.unpaid.length} still unpaid` : 'All paid')}
      ${kpi('Everything else', kpiVal(rep.expenses.variable_chf), 'Day-to-day spending')}
      ${kpi('In GBP', kpiVal(rep.expenses.foreign_currency.find((x) => x.currency === 'GBP')?.total ?? 0, 'GBP'), `= ${money0(rep.expenses.foreign_currency.find((x) => x.currency === 'GBP')?.total_chf ?? 0)}`)}
    </div>
    <div class="grid two">
      ${card('Spending by category', '<div class="chart"><canvas id="c1"></canvas></div>', '', 'Last 12 months, CHF')}
      ${card('This month by category', meters(rep.expenses.by_category), '', monthLabel(m, true))}
    </div>
    ${card(`Expenses in ${monthLabel(m, true)}`, '<div id="txtable"></div>', `
      <input type="search" id="fq" placeholder="Search…" value="${esc(f.q)}">
      <select id="fcat"><option value="">All categories</option><option value="none">Uncategorised</option>${state.categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select>
      ${btn('pull-uk', 'Pull UK expenses', '', '')}
      ${btn('add-tx', '+ Add expense', '', 'primary')}`)}
    <div style="height:14px"></div>
    ${card('Bank screenshots / statements', table(['File', 'Note', '#Linked', 'Uploaded', ''], stRows, 'No screenshots uploaded for this month.'), btn('add-st', 'Upload', '', ''), 'Source documents the agent uploaded for this month')}`;
  $('#fcat').value = f.cat;
  $('#fcat').onchange = (e) => { f.cat = e.target.value; draw(); };
  $('#fq').oninput = (e) => { f.q = e.target.value; draw(); };
  draw();
  const sets = foldSeries(trend.categories.map((c) => ({ label: c.category, c })), (it) => it.c.spent_chf);
  chart('c1', 'bar', { labels: trend.months.map((x) => monthLabel(x)), datasets: sets.map((s, i) => barSet(s.label, s.data, s.other ? cssVar('--other') : series(i), i ? '-1' : 'origin')) }, { stacked: true });
  actions = {
    ...txActions(txs),
    'add-st': () => openForm('Upload bank screenshot', [{ name: 'file', label: 'Image or PDF', type: 'file', required: true }, { name: 'note', label: 'Note (optional)' }], (v) => {
      const fd = new FormData(); fd.append('file', v.file); fd.append('month', m); if (v.note) fd.append('note', v.note);
      return api('/statements', { method: 'POST', body: fd });
    }, 'Upload'),
    'del-st': (id) => confirmDelete(`/statements/${id}`, 'this file'),
    'pull-uk': async () => {
      const p = await api(`/transactions/rollup?month=${m}&currency=GBP`);
      if (!p.transactions) { toast(`No GBP expenses in ${monthLabel(m, true)}.`); return; }
      openForm(`Pull UK expenses · ${money(p.total, 'GBP')} (${p.transactions} expenses)`, [
        { name: 'fx_rate', label: "CHF per 1 GBP (today's rate — change it to match Wise if you like)", type: 'number', step: '0.0001', value: p.rate, required: true },
        { name: 'fee_pct', label: 'Added on top for Wise fees (%)', type: 'number', step: '0.1', value: p.fee_pct, required: true },
      ], (v) => api('/transactions/rollup', { method: 'POST', json: { ...v, month: m, currency: 'GBP' } }),
      p.existing ? `Replace the existing line (${money(p.existing.amount_chf)})` : 'Add “UK expenses” line');
    },
  };
};

pages.recurring = async () => {
  const m = state.month;
  const [st, defs] = await Promise.all([api(`/recurring/status?month=${m}`), api('/recurring')]);
  const fields = (r = {}) => [
    { name: 'name', label: 'Name (e.g. Rent)', value: r.name, required: true },
    { name: 'amount', label: 'Amount per month', type: 'number', value: r.amount, required: true },
    { name: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, value: r.currency || 'CHF' },
    { name: 'category_id', label: 'Category', type: 'select', options: catOptions(), value: r.category_id, numeric: true },
    { name: 'day_of_month', label: 'Usual day of month (optional)', type: 'number', step: '1', value: r.day_of_month },
    { name: 'start_month', label: 'First month (optional)', type: 'month', value: r.start_month },
    { name: 'end_month', label: 'Last month (optional)', type: 'month', value: r.end_month },
  ];
  const stRows = st.items.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.category ? `<span class="tag">${esc(r.category)}</span>` : ''}</td><td>${r.day_of_month ? r.day_of_month + '.' : '–'}</td>
    <td class="num">${money(r.expected_amount, r.currency)}</td><td class="num">${r.paid ? money(r.paid_amount, r.currency) : '–'}</td>
    <td>${r.paid ? `<span class="tag ok">✓ paid ${r.paid_date}</span>${r.difference ? ` <span class="tag bad">${r.difference > 0 ? '+' : ''}${nf.format(r.difference)}</span>` : ''}` : '<span class="tag bad">○ not paid</span>'}</td>
    <td class="actions">${r.paid ? '' : btn('pay', 'Mark paid', r.recurring_id)}</td></tr>`);
  const defRows = defs.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.category)}</td><td class="num">${money(r.amount, r.currency)}</td><td>${r.start_month || '–'} → ${r.end_month || 'ongoing'}</td>
    <td>${r.active ? '<span class="tag ok">active</span>' : '<span class="tag">inactive</span>'}</td>
    <td class="actions">${btn('edit', 'Edit', r.id, 'small ghost')}${btn('toggle', r.active ? 'Deactivate' : 'Activate', r.id, 'small ghost')}${btn('del', 'Delete', r.id, 'small ghost danger')}</td></tr>`);
  $('#view').innerHTML = `
    <div class="grid kpis">
      ${kpi('Expected this month', kpiVal(st.expected_chf), `${st.items.length} charges`)}
      ${kpi('Paid', kpiVal(st.paid_chf), `${st.items.filter((i) => i.paid).length} of ${st.items.length}`)}
      ${kpi('Still to pay', kpiVal(st.unpaid_chf), st.unpaid.length ? esc(st.unpaid.join(', ')) : 'Everything is paid', st.unpaid_chf ? 'neg' : '')}
    </div>
    ${card(`Status for ${monthLabel(m, true)}`, table(['Charge', 'Category', 'Day', '#Expected', '#Paid', 'Status', ''], stRows, 'No recurring charges apply to this month.'))}
    <div style="height:14px"></div>
    ${card('All recurring charges', table(['Name', 'Category', '#Amount', 'Period', 'Status', ''], defRows, 'Add your rent, car payment, insurance…'), btn('add', '+ Add recurring charge', '', 'primary'))}`;
  actions = {
    add: () => openForm('Add recurring charge', fields(), (v) => api('/recurring', { method: 'POST', json: v })),
    edit: (id) => openForm('Edit recurring charge', fields(defs.find((r) => r.id == id)), (v) => api(`/recurring/${id}`, { method: 'PATCH', json: v })),
    toggle: async (id) => { await api(`/recurring/${id}`, { method: 'PATCH', json: { active: !defs.find((r) => r.id == id).active } }); render(); },
    del: (id) => confirmDelete(`/recurring/${id}`, 'this recurring charge (past payments are kept)'),
    pay: (id) => {
      const r = st.items.find((x) => x.recurring_id == id);
      openForm(`Mark "${r.name}" as paid`, [
        { name: 'date', label: 'Payment date', type: 'date', value: `${m}-${String(Math.min(r.day_of_month || 1, 28)).padStart(2, '0')}`, required: true },
        { name: 'amount', label: `Amount paid (${r.currency})`, type: 'number', value: r.expected_amount, required: true },
      ], (v) => api(`/recurring/${id}/pay`, { method: 'POST', json: { ...v, month: m } }), 'Mark paid');
    },
  };
};

pages.investments = async () => {
  const m = state.month;
  const [sum, hist] = await Promise.all([api('/investments/summary'), api(`/investments/history?months=24&end=${m}`)]);
  const all = await api('/accounts');
  // start the charts at the first month that has any data
  const first = hist.months.findIndex((_, i) => hist.accounts.some((a) => a.value_chf[i] != null));
  if (first > 0) {
    hist.months = hist.months.slice(first);
    hist.accounts.forEach((a) => { a.value_chf = a.value_chf.slice(first); a.net_invested_chf = a.net_invested_chf.slice(first); });
  }
  const typeLabel = { cash: 'Cash', savings: 'Savings', stocks: 'Stocks', pension: 'Pension', crypto: 'Crypto', property: 'Property', other: 'Other' };
  const tracked = hist.accounts.filter((a) => a.net_invested_chf.some((v) => v != null));
  const sel = pages.investments.acc && tracked.some((a) => a.account_id == pages.investments.acc) ? pages.investments.acc : (tracked.find((a) => a.type === 'stocks') || tracked[0])?.account_id;
  const fields = (a = {}) => [
    { name: 'name', label: 'Account name', value: a.name, required: true },
    { name: 'type', label: 'Type', type: 'select', options: Object.entries(typeLabel), value: a.type || 'cash' },
    ...(a.id ? [] : [{ name: 'currency', label: 'Currency', type: 'select', options: CURRENCIES, value: 'CHF' }]),
    { name: 'institution', label: 'Bank / broker (optional)', value: a.institution },
    { name: 'person_id', label: 'Owner', type: 'select', options: personOptions('— joint —'), value: a.person_id, numeric: true },
  ];
  const rows = all.map((a) => `<tr style="${a.active ? '' : 'opacity:.55'}"><td>${esc(a.name)}${a.institution ? `<div style="color:var(--muted);font-size:12px">${esc(a.institution)}</div>` : ''}</td><td><span class="tag">${typeLabel[a.type]}</span></td><td>${esc(a.person || 'Joint')}</td>
    <td class="num">${a.currency !== 'CHF' && a.value != null ? `<span class="tag">${money(a.value, a.currency)}</span> ` : ''}${a.value_chf != null ? nf.format(a.value_chf) : '–'}</td>
    <td class="num">${a.gain_chf != null ? nf.format(a.net_invested_chf) : '–'}</td>
    <td class="num ${signCls(a.gain_chf)}">${a.gain_chf != null ? `${a.gain_chf > 0 ? '+' : ''}${nf.format(a.gain_chf)}` : '–'}</td>
    <td class="num ${signCls(a.return_pct)}">${pct(a.return_pct)}</td><td>${a.latest_date || '–'}</td>
    <td class="actions">${btn('val', 'Update value', a.id)}${btn('flow', 'Deposit / withdraw', a.id)}${btn('edit', 'Edit', a.id, 'small ghost')}${btn('toggle', a.active ? 'Close' : 'Reopen', a.id, 'small ghost')}</td></tr>`);
  $('#view').innerHTML = `
    <div class="grid kpis">
      ${kpi('Total value', kpiVal(sum.total_value_chf), Object.entries(sum.by_type_chf).map(([t, v]) => `${typeLabel[t]} ${nf0.format(v)}`).join(' · ') || 'No accounts yet')}
      ${kpi('Paid in (tracked accounts)', kpiVal(sum.net_invested_chf), 'Deposits minus withdrawals')}
      ${kpi('Gain / loss', kpiVal(sum.gain_chf), 'Value minus money paid in', signCls(sum.gain_chf))}
      ${kpi('Return', sum.return_pct == null ? '–' : pct(sum.return_pct), 'Simple return on money paid in', signCls(sum.return_pct))}
    </div>
    <div class="grid two">
      ${card('Value over time', hist.accounts.length ? '<div class="chart"><canvas id="c1"></canvas></div>' : '<div class="empty">Add an account and a first value.</div>', '', 'Month-end value by account, last 24 months, CHF')}
      ${card('Performance', tracked.length ? '<div class="chart"><canvas id="c2"></canvas></div>' : '<div class="empty">Record deposits on an account to compare its value with the money paid in.</div>',
        `<select id="acc">${tracked.map((a) => `<option value="${a.account_id}" ${a.account_id == sel ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}</select>`, 'Value vs money paid in, CHF')}
    </div>
    ${card('Accounts', table(['Account', 'Type', 'Owner', '#Value CHF', '#Paid in', '#Gain', '#Return', 'As of', ''], rows, 'No accounts yet — add your emergency cash, savings and stock accounts.'), btn('add', '+ Add account', '', 'primary'), 'Gain and return appear once deposits are recorded for an account')}`;
  const labels = hist.months.map((x) => monthLabel(x));
  if (hist.accounts.length) {
    const sets = foldSeries(hist.accounts.map((a) => ({ label: a.name, a })), (it) => it.a.value_chf.map((v) => v ?? 0));
    chart('c1', 'line', { labels, datasets: sets.map((s, i) => lineSet(s.label, s.data, s.other ? cssVar('--other') : series(i), i ? '-1' : 'origin')) }, { stacked: true });
  }
  if (tracked.length) {
    const a = tracked.find((x) => x.account_id == sel);
    chart('c2', 'line', { labels, datasets: [lineSet('Value', a.value_chf, series(0)), { ...lineSet('Paid in', a.net_invested_chf, cssVar('--ink-2')), borderDash: [5, 4], stepped: true, tension: 0 }] }, { zero: false });
    $('#acc').onchange = (e) => { pages.investments.acc = e.target.value; render(); };
  }
  const acc = (id) => all.find((a) => a.id == id);
  actions = {
    add: () => openForm('Add account', fields(), (v) => api('/accounts', { method: 'POST', json: v })),
    edit: (id) => openForm('Edit account', fields(acc(id)), (v) => api(`/accounts/${id}`, { method: 'PATCH', json: v })),
    toggle: async (id) => { await api(`/accounts/${id}`, { method: 'PATCH', json: { active: !acc(id).active } }); render(); },
    val: (id) => openForm(`Value of ${acc(id).name}`, [
      { name: 'date', label: 'As of', type: 'date', value: today(), required: true },
      { name: 'value', label: `Total value (${acc(id).currency})`, type: 'number', value: acc(id).value, required: true },
    ], (v) => api(`/accounts/${id}/valuations`, { method: 'POST', json: v })),
    flow: (id) => openForm(`Deposit / withdrawal · ${acc(id).name}`, [
      { name: 'date', label: 'Date', type: 'date', value: today(), required: true },
      { name: 'amount', label: `Amount in ${acc(id).currency} (negative = withdrawal)`, type: 'number', required: true },
      { name: 'note', label: 'Note (optional)' },
    ], (v) => api(`/accounts/${id}/flows`, { method: 'POST', json: v })),
  };
};

pages.gbp = async () => {
  const m = state.month;
  const [rep, txs, rates, cur] = await Promise.all([api(`/reports/foreign?currency=GBP&months=12&end=${m}`), api(`/transactions?currency=GBP&date_from=${shiftMonth(m, -11)}-01&date_to=${m}-31`), api('/fx?currency=GBP'), api('/fx/rate?currency=GBP').catch(() => null)]);
  const now = rep.series.at(-1);
  const recRows = rep.recurring.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.category)}</td><td class="num">${money(r.amount, 'GBP')}</td></tr>`);
  $('#view').innerHTML = `
    <div class="grid kpis">
      ${kpi(`Spent in GBP · ${monthLabel(m)}`, kpiVal(now.total, 'GBP'), `= ${money(now.total_chf)} · ${now.transactions} transactions`)}
      ${kpi('Last 12 months', kpiVal(rep.total, 'GBP'), `= ${money(rep.total_chf)}`)}
      ${kpi('GBP → CHF today', cur ? cur.rate.toFixed(4) : '–', cur ? '1 GBP in CHF' : 'No rate stored yet')}
    </div>
    <div class="grid two">
      ${card('UK spending by month', '<div class="chart short"><canvas id="c1"></canvas></div>', '', 'In pounds')}
      ${card('Exchange rate', rates.length > 1 ? '<div class="chart short"><canvas id="c2"></canvas></div>' : '<div class="empty">Rates appear here as GBP expenses are recorded.</div>', btn('rate', 'Set a rate'), 'CHF per 1 GBP, as stored')}
    </div>
    <div class="grid two">
      ${card('GBP expenses', table(['Date', 'Description', 'Category', '#GBP', '#CHF', ''], txRows(txs, true), 'No GBP expenses in the last 12 months.'), btn('add-gbp', '+ Add GBP expense', '', 'primary'), 'Last 12 months')}
      ${card('Recurring charges in GBP', table(['Charge', 'Category', '#Per month'], recRows, 'None. Add them under Recurring with currency GBP.'))}
    </div>`;
  chart('c1', 'bar', { labels: rep.months.map((x) => monthLabel(x)), datasets: [barSet('GBP', rep.series.map((s) => s.total), series(0))] }, { fmt: (v) => money(v, 'GBP') });
  if (rates.length > 1) chart('c2', 'line', { labels: rates.map((r) => r.date), datasets: [lineSet('CHF per GBP', rates.map((r) => r.rate), series(0))] }, { zero: false, fmt: (v) => v.toFixed(4), tick: (v) => v.toFixed(2) });
  actions = {
    ...txActions(txs),
    'add-gbp': () => openForm('Add GBP expense', txFields({ currency: 'GBP' }), (v) => api('/transactions', { method: 'POST', json: v })),
    rate: () => openForm('Set GBP → CHF rate', [{ name: 'date', label: 'Date', type: 'date', value: today(), required: true }, { name: 'rate', label: 'CHF per 1 GBP', type: 'number', step: '0.0001', value: cur?.rate, required: true }], (v) => api('/fx', { method: 'PUT', json: { ...v, currency: 'GBP' } })),
  };
};

pages.settings = async () => {
  const health = await api('/health');
  const pRows = state.people.map((p) => `<tr><td>${esc(p.name)}</td><td class="actions">${btn('rename-p', 'Rename', p.id)}</td></tr>`);
  const cRows = state.categories.map((c) => `<tr style="${c.archived ? 'opacity:.55' : ''}"><td>${esc(c.name)}</td><td class="actions">${btn('rename-c', 'Rename', c.id, 'small ghost')}${btn('arch-c', c.archived ? 'Restore' : 'Archive', c.id, 'small ghost')}</td></tr>`);
  const apiBase = `${location.protocol}//${location.hostname}:1311`;
  $('#view').innerHTML = `
    <div class="grid two">
      ${card('People', table(['Name', ''], pRows), '', 'The two salaries are tracked against these names')}
      ${card('For your agent', `<p>API base URL: <code>${esc(apiBase)}/api</code></p>
        <p><a href="/api/agent-guide" target="_blank">Agent guide (markdown)</a> · <a href="${esc(apiBase)}/docs" target="_blank">Interactive API docs</a> · <a href="/openapi.json" target="_blank">OpenAPI schema</a></p>
        <p>API key: ${health.auth_required ? 'required (set via the <code>API_KEY</code> environment variable)' : 'not required. Set the <code>API_KEY</code> environment variable on the container to require one.'}</p>
        <p>Data is stored in: ${health.storage === 'dynamodb' ? `DynamoDB table <code>${esc(health.table)}</code> (${esc(health.region)})` : 'a local SQLite file'}</p>
        ${health.auth_required ? btn('key', 'Change the key stored in this browser') : ''}`)}
    </div>
    ${card('Expense categories', table(['Category', ''], cRows), btn('add-c', '+ Add category', '', 'primary'), 'Archived categories keep their history but are hidden from pickers')}`;
  actions = {
    'rename-p': (id) => openForm('Rename person', [{ name: 'name', label: 'Name', value: state.people.find((p) => p.id == id).name, required: true }], (v) => api(`/people/${id}`, { method: 'PATCH', json: v })),
    'rename-c': (id) => openForm('Rename category', [{ name: 'name', label: 'Name', value: state.categories.find((c) => c.id == id).name, required: true }], (v) => api(`/categories/${id}`, { method: 'PATCH', json: v })),
    'arch-c': async (id) => { await api(`/categories/${id}`, { method: 'PATCH', json: { archived: !state.categories.find((c) => c.id == id).archived } }); render(); },
    'add-c': () => openForm('Add category', [{ name: 'name', label: 'Name', required: true }], (v) => api('/categories', { method: 'POST', json: v })),
    key: () => { const k = prompt('API key:'); if (k) { localStorage.setItem('apiKey', k.trim()); render(); } },
  };
};

// ---------------------------------------------------------------- router
const TITLES = { overview: 'Overview', income: 'Salary', expenses: 'Expenses', recurring: 'Recurring charges', investments: 'Investments', gbp: 'GBP expenses', settings: 'Settings' };

async function render() {
  const route = (location.hash.replace('#/', '') || 'overview').split('?')[0];
  const page = pages[route] ? route : 'overview';
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.getAttribute('href') === '#/' + page));
  $('#title').textContent = TITLES[page];
  $('#month').value = state.month;
  $('#monthnav').style.visibility = page === 'settings' ? 'hidden' : 'visible';
  charts.forEach((c) => c.destroy()); charts = []; actions = {};
  try {
    await loadRefs();
    await pages[page]();
  } catch (e) {
    $('#view').innerHTML = card('Something went wrong', `<p>${esc(e.message)}</p>`);
  }
}

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]');
  if (!el || !actions[el.dataset.act]) return;
  e.preventDefault();
  Promise.resolve(actions[el.dataset.act](el.dataset.id || undefined)).catch((err) => toast(err.message));
});
$('#prev').onclick = () => { state.month = shiftMonth(state.month, -1); render(); };
$('#next').onclick = () => { state.month = shiftMonth(state.month, 1); render(); };
$('#month').onchange = (e) => { if (e.target.value) { state.month = e.target.value; render(); } };
$('#theme').onclick = () => {
  const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
  const next = dark ? 'light' : 'dark';
  document.documentElement.dataset.theme = next; localStorage.setItem('theme', next); render();
};
if (localStorage.getItem('theme')) document.documentElement.dataset.theme = localStorage.getItem('theme');
window.addEventListener('hashchange', render);
render();
