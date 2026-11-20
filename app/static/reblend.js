const root = document.querySelector('.reblend');
const runId = root.dataset.runId;
const feedback = document.querySelector('#plan-feedback');
const statusBox = document.querySelector('#plan-status');
const summary = document.querySelector('#plan-summary');
let currentPlan = null;
let busy = false;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function table(headers, rows) {
  if (!rows.length) return '<div class="empty">无记录。</div>';
  return `<table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>` +
    rows.map((r) => `<tr>${r.map((c) => `<td title="${esc(c)}">${esc(c)}</td>`).join('')}</tr>`).join('') +
    '</tbody></table>';
}

function badge(status) {
  const cls = status === '已分配' ? 'sev--ok' : status === '未分配' ? 'sev--severe' : 'sev--warn';
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function panel(title, span, bodyHtml) {
  return `<section class="panel ${span ? 'span-all' : ''}"><div class="panel__head"><h2>${esc(title)}</h2></div><div class="scroll">${bodyHtml}</div></section>`;
}

async function call(url, payload) {
  const opt = payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)};
  const res = await fetch(url, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || '请求失败'), {status: res.status});
  return data;
}

function renderPlan(plan) {
  currentPlan = plan;
  const metricText = plan.metrics.map((m) => `${m[0]} ${m[1]}${m[2] || ''}`).join('　·　');
  summary.textContent = metricText;
  statusBox.textContent = `方案号 ${plan.plan_hash}　确认键 ${plan.confirm_key}`;

  const planRows = plan.rows.map((r) => [
    r['炉次'], r['优先级'], r['交期'], r['干重_kg'], r['当前验收'], r['分配状态'],
    r['选用材料'], r['补料量_kg'], r['补后区间'], r['补后验收'], r['分配原因']]);
  const invRows = plan.inventory.map((m) => [
    m['材料'], m['材料名称'], m['现有库存_kg'], m['本方案占用_kg'], m['方案后余量_kg']]);

  const detailRows = plan.rows.flatMap((r) =>
    (r['候选材料'] || []).map((o) => [
      r['炉次'], o['材料'], o['可行'] ? '可行' : '不可行',
      o['最小补料_kg'] ?? '—', o['最大补料_kg'] ?? '—',
      (o['补后成分'] || []).map((c) => `${c['元素']} ${c['补后下界_pct']}~${c['补后上界_pct']}% ${c['补后验收']}`).join('；'),
      o['不可行原因']]));

  const grid = document.querySelector('.panel-grid');
  const anchor = grid.querySelector('.span-all');
  grid.querySelectorAll('.reblend-panel').forEach((n) => n.remove());
  const blocks = [
    panel('整组补料方案（各炉分配依据）', true,
      table(['炉次', '优先级', '交期', '干重kg', '当前验收', '分配状态', '选用材料', '补料kg', '补后成分上下界', '补后验收', '为何分到这些料'],
        planRows.map((r, i) => {
          const copy = r.slice();
          copy[5] = badge(plan.rows[i]['分配状态']);
          return copy;
        }))),
    panel('共用补料库存（确认前占用为预占）', false,
      table(['材料', '名称', '现有库存kg', '本方案占用kg', '确认后余量kg'], invRows)),
    panel('逐炉候选材料与补后验收重算', false,
      table(['炉次', '候选材料', '可行性', '最小剂量kg', '最大剂量kg', '补后成分上下界/验收', '不可行原因'], detailRows)),
  ];
  blocks.forEach((html) => {
    const node = document.createElement('div');
    node.className = 'reblend-panel span-all-wrap';
    node.innerHTML = html;
    grid.insertBefore(node.firstElementChild, anchor);
  });
}

function renderInventory(view, confirmations) {
  const grid = document.querySelector('.panel-grid');
  const anchor = grid.querySelector('.span-all');
  grid.querySelectorAll('.reblend-live').forEach((n) => n.remove());
  const invRows = view.materials.map((m) => [m.material_id, m.material_name, m.on_hand_kg]);
  const orderRows = view.orders.map((o) => [o.batch, o.due_date || '未登记', o.priority]);
  const ledgerRows = view.ledger.map((l) =>
    [l.created_at, l.material_id, l.change_kg, l.balance_kg, l.ref_type, l.ref_key, l.note]);
  const confRows = confirmations.map((c) =>
    [c.created_at, c.confirm_key, c.run_id, c.plan_hash,
      c.allocations.map((a) => `${a[0]}←${a[1]} ${a[2]}kg`).join('；')]);
  const wrap = document.createElement('div');
  wrap.className = 'reblend-live';
  wrap.innerHTML =
    panel('补料库存实时余量（已扣减）', false, table(['材料', '名称', '余量kg'], invRows)) +
    panel('急单交期表', false, table(['炉次', '交期', '优先级'], orderRows)) +
    panel('库存台账（含确认扣料/入库/重置）', true,
      table(['时间UTC', '材料', '变动kg', '结余kg', '类型', '单据', '备注'], ledgerRows)) +
    panel('已确认补料单（重试不重复扣料）', true,
      table(['时间UTC', '确认键', '运行批次', '方案号', '扣料明细'], confRows));
  grid.insertBefore(wrap, anchor);
}

async function loadAll() {
  if (busy) return;
  busy = true;
  feedback.textContent = '正在按最新库存重算整组方案…';
  try {
    const [plan, view, confirmations] = await Promise.all([
      call('/api/reblend/plan', {run_id: runId}),
      call('/api/reblend/inventory'),
      call('/api/reblend/confirmations')]);
    renderPlan(plan);
    renderInventory(view, confirmations);
    feedback.textContent = '';
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    busy = false;
  }
}

document.querySelector('#plan-load').addEventListener('click', loadAll);
document.querySelector('#plan-demo').addEventListener('click', async () => {
  if (busy) return;
  busy = true;
  feedback.textContent = '正在载入多炉次演示数据并复算…';
  try {
    const res = await fetch('/static/demo/reblend_demo.csv');
    if (!res.ok) throw new Error('演示数据读取失败');
    const csv = await res.text();
    const run = await call('/api/run', {csv});
    window.location.href = `/work/7?run_id=${encodeURIComponent(run.manifest.run_id)}`;
  } catch (error) {
    feedback.textContent = error.message;
    busy = false;
  }
});
document.querySelector('#plan-confirm').addEventListener('click', async () => {
  if (busy || !currentPlan) return;
  if (!window.confirm('确认后将立即扣减共用库存并重算验收，确认提交整组补料方案？')) return;
  busy = true;
  feedback.textContent = '正在提交确认（并发安全，重复提交不会重复扣料）…';
  try {
    const data = await call('/api/reblend/confirm', {
      run_id: runId, confirm_key: currentPlan.confirm_key});
    feedback.textContent = data.idempotent
      ? '该方案已确认过：本次为幂等重试，未重复扣料。'
      : '确认成功：共用库存已扣减，补后验收已重算。';
    await loadAll();
  } catch (error) {
    feedback.textContent = error.status === 409
      ? `${error.message}（他人刚占用了库存，请刷新方案）` : error.message;
  } finally {
    busy = false;
  }
});

loadAll();
