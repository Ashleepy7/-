/* 灵犀相册 · 前端逻辑（原生 JS，无构建步骤） */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const PAGE_SIZE = 160;
const SECTION_PREVIEW = 48;

const S = {
  group: 'theme',          // theme | month | folder
  theme: '',               // 选中的主题 id，'' = 全部
  month: '',
  year: '',                // 侧栏「年→月」层级：选中的整年（与 month 互斥）
  shot: '',                // 'unknown' = 只看无 EXIF 拍摄时间的照片（与其他时间/主题筛选互斥）
  folder: '',
  root: '',
  q: '',
  order: 'taken',
  size: 'm',
  page: 1,
  total: 0,
  items: [],               // 当前平铺列表（供大图翻页用）
  state: null,
  lbIndex: -1,
  lbList: [],
  aiMode: false,           // 顶栏搜索框：关键词 / AI 查找
  aiText: '',
  ai: null,                // 最近一次 AI 返回（条件、命中、放宽）
  aiOff: [],               // 用户手动关掉的条件 cid
  aiKeep: [],              // 用户坚持不许放宽的条件 cid
  aiEngine: 'auto',        // auto 优先 AI 模型 | qwen 只用模型 | rules 只用本地规则
  aiExpand: false,         // AI 条件面板是否展开（默认折叠成一行摘要）
  llm: null,               // 后端回的配置（Key 已打码，只给后 4 位）
};

let io = null;             // IntersectionObserver（缩略图懒加载）
let jobTimer = null;
let renderSeq = 0;         // 渲染序号：只采纳最后一次请求的结果，避免旧响应覆盖新视图

/* ---------------------------------------------------------------- 工具 */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || data.message || `请求失败 (${res.status})`);
    err.data = data;                 // 阶段、类型这类细节附在错误上，不丢
    throw err;
  }
  return data;
}

function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let toastTimer = null;
function toast(msg, bad = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

function themeMeta(id) {
  const t = (S.state?.themes || []).find((x) => x.id === id);
  return t || { id: 'other', name: '未归类', emoji: '🗂️', color: '#94a3b8', count: 0 };
}

/* ---------------------------------------------------------------- 侧栏 / 顶栏 */

function renderSidebar() {
  const st = S.state;
  const total = st.stats.total || 0;
  const rows = [{ id: '', name: '全部照片', emoji: '🗃️', color: '#c7d2fe', count: total }, ...st.themes];

  $('#themeList').innerHTML = rows.map((t) => {
    const pct = total ? Math.round((t.count / total) * 100) : 0;
    const on = S.theme === t.id && !S.month && !S.year && !S.shot && !S.folder && !S.root;
    return `<li class="theme-item ${on ? 'on' : ''}" data-theme="${t.id}">
      <span class="em">${t.emoji}</span>
      <span class="nm"><b>${esc(t.name)}</b>
        <span class="bar"><i style="width:${pct}%;background:${t.color}"></i></span>
      </span>
      <span class="ct">${t.count}</span>
    </li>`;
  }).join('');

  // 先按年份、年份可展开再列月份（手风琴）：点年份=筛整年，展开后再点其中月份=筛该月
  const years = st.years || [];
  const isOpen = (y) => S.year === y || (S.month && String(S.month).slice(0, 4) === y);
  $('#monthList').innerHTML = years.map((yr) => {
    const open = isOpen(yr.year);
    const head = `<li class="mini-item ${S.year === yr.year ? 'on' : ''}" data-year="${esc(yr.year)}">
       <span>${open ? '▾' : '▸'} 🗓️ ${esc(yr.year)} 年</span><b>${yr.c}</b></li>`;
    const ms = yr.months.map((m) =>
      `<li class="mini-item ${S.month === m.key ? 'on' : ''}" data-month="${esc(m.key)}"
         style="padding-left:22px;${open ? '' : 'display:none'}">
         <span>📅 ${esc(String(m.key).slice(5))} 月</span><b>${m.c}</b></li>`).join('');
    return head + ms;
  }).join('') || '<li class="mini-item">—</li>';

  // 时间未知说明：避免用户疑惑“为什么某些照片在任何年份/月份都筛不出来”
  const unk = (st.stats && st.stats.shot_unknown) || 0;
  const tipEl = $('#timeTip');
  if (tipEl) {
    tipEl.hidden = !unk;
    const on = S.shot === 'unknown';
    tipEl.innerHTML = unk
      ? `🕓 另有 ${unk} 张没有 EXIF 拍摄时间，按时间查找时不纳入。`
        + `<a href="#" data-unknown="1" style="color:${on ? '#e0a458' : '#7c5cff'};text-decoration:none;margin-left:4px">${on ? '⟵ 查看全部' : '查看这些 →'}</a>`
      : '';
  }

  $('#folderList').innerHTML = st.folders.slice(0, 8).map((f) =>
    `<li class="mini-item ${S.folder === f.key ? 'on' : ''}" data-folder="${esc(f.key)}" title="${esc(f.key)}">
       <span>📁 ${esc(shortFolder(f.key))}</span><b>${f.c}</b></li>`).join('') || '<li class="mini-item">—</li>';

  $('#rootList').innerHTML = st.roots.length ? st.roots.map((r) =>
    `<li class="mini-item" data-root="${esc(r.root)}" title="${esc(r.root)}">
       <span>💽 ${esc(shortFolder(r.root))}</span>
       <button class="x" data-delroot="${esc(r.root)}" title="仅移除导入目录，照片列表保留">✕</button></li>`).join('')
    : '<li class="mini-item">还没有导入目录</li>';

  $('#statTotal').textContent = total;
  $('#statOrg').textContent = `${st.stats.organized} / ${total}`;
  $('#statBytes').textContent = fmtBytes(st.stats.bytes);

  const sugg = (st.suggested || []).concat(
    (st.roots || []).map((r) => ({ path: r.root, label: shortFolder(r.root) })));
  const uniq = [...new Map(sugg.map((s) => [s.path, s])).values()].slice(0, 6);
  $('#commonPaths').innerHTML = uniq.map((s) =>
    `<button class="chip" data-quick="${esc(s.path)}">➕ ${esc(s.label)} · ${esc(s.path)}</button>`).join('');
  $('#importChips').innerHTML = uniq.map((s) =>
    `<button class="chip" data-quick="${esc(s.path)}">${esc(s.path)}</button>`).join('');
}

function shortFolder(p) {
  const parts = String(p).replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

function renderCrumb() {
  let emoji = '🗃️', title = '全部照片';
  if (S.aiMode) {
    emoji = '✨';
    title = S.aiText ? `AI 查找：“${S.aiText}”` : 'AI 查找';
  } else if (S.theme) { const t = themeMeta(S.theme); emoji = t.emoji; title = t.name; }
  else if (S.year) { emoji = '🗓️'; title = `${S.year} 年`; }
  else if (S.month) { emoji = '📅'; title = S.month; }
  else if (S.shot) { emoji = '🕓'; title = '拍摄时间未知'; }
  else if (S.folder) { emoji = '📁'; title = shortFolder(S.folder); }
  else if (S.root) { emoji = '💽'; title = shortFolder(S.root); }
  else if (S.q) { emoji = '🔍'; title = `搜索“${S.q}”`; }
  $('#crumbEmoji').textContent = emoji;
  $('#crumbTitle').textContent = title;
  const scanned = S.ai && S.ai.scanned ? ` / 比对 ${S.ai.scanned} 张` : '';
  $('#crumbCount').textContent = `${S.total} 张${scanned}`;

  // 分组控件只在无筛选（分区视图）时有意义，筛选态下变淡提示
  const filtered = S.aiMode ? !!S.aiText
    : !!(S.theme || S.month || S.year || S.shot || S.folder || S.root || S.q);
  $('#groupSeg').classList.toggle('muted', filtered);
  $('#groupSeg').title = filtered ? '当前显示的是筛选结果，切换分组会清除筛选' : '';
}

/* ---------------------------------------------------------------- 照片墙 */

function cellHtml(p) {
  const t = themeMeta(p.theme);
  // 主题来自模型时，conf 是规则分类器的分数，跟当前显示的主题无关 → 不配显示百分比
  const src = p.theme_src || 'rule';
  const badge = p.match != null
    ? `<span class="mm${p.match >= 80 ? ' hi' : ''}">${p.match}%</span>`
    : src === 'ai' ? '<span class="cf" title="主题由模型判定，不显示规则置信度">🧠</span>'
    : src === 'manual' ? '<span class="cf" title="主题由人工设定">人工</span>'
    : (p.conf ? `<span class="cf">${p.conf}%</span>` : '');
  // 底部那条进度线也是 conf 的可视化，主题不是规则给的同理不能画
  const barW = src === 'rule' ? (p.conf || 0) : 0;
  const why = (p.hits && p.hits.length) ? `<span class="why">${p.hits.map(esc).join(' · ')}</span>` : '';
  const cap = p.caption ? `<span class="cap">🧠 ${esc(p.caption)}</span>` : '';
  const err = p.status === 'error' ? ' err' : '';
  const manual = p.manual ? ' manual' : '';
  const tip = p.name + (p.caption ? `\nAI 看到：${p.caption}` : '')
    + (p.hits && p.hits.length ? `\n命中：${p.hits.join('；')}` : '');
  return `<figure class="cell${err}${manual}" data-id="${p.id}" data-tsrc="${src}" title="${esc(tip)}">
    <div class="skeleton"></div>
    <img data-src="${p.thumb}" alt="${esc(p.name)}" loading="lazy">
    <span class="tag" style="color:${t.color}">${t.emoji} ${esc(t.name)}</span>${badge}
    <div class="cfl" style="width:${barW}%;background:${t.color};opacity:.55"></div>
    <div class="veil">
      <span class="fn">${esc(p.name)}</span>
      <span class="mt">${esc(p.taken || '')} · ${fmtBytes(p.bytes)} · ${p.width}×${p.height}</span>
      ${cap}${why}
    </div>
  </figure>`;
}

function mountCells(root) {
  root.querySelectorAll('img[data-src]').forEach((img) => io.observe(img));
  root.querySelectorAll('.cell').forEach((c) =>
    c.addEventListener('click', () => { syncVisibleList(); openLightbox(+c.dataset.id); }));
}

function buildGrid(list) {
  const frag = document.createElement('div');
  frag.innerHTML = list.map(cellHtml).join('');
  mountCells(frag);
  return frag.children;
}

async function loadFlat(reset = true) {
  const seq = ++renderSeq;
  let holder = $('#grid .cells.flat');
  if (reset || !holder) {
    if (reset) { S.page = 1; S.items = []; }
    $('#grid').innerHTML = '';
    holder = document.createElement('div');
    holder.className = `cells flat size-${S.size}`;
    $('#grid').append(holder);
  }
  const q = new URLSearchParams({
    theme: S.theme, month: S.month, year: S.year, shot: S.shot, folder: S.folder, root: S.root,
    q: S.q, order: S.order, page: S.page, size: PAGE_SIZE,
  });
  const data = await api(`/api/photos?${q}`);
  if (seq !== renderSeq) return;          // 期间又切了筛选，丢掉这次结果
  S.total = data.total;
  if (reset) S.items = data.items; else S.items = S.items.concat(data.items);
  holder.append(...buildGrid(data.items));
  $('#loadMoreWrap').hidden = S.items.length >= data.total;
  renderCrumb();
  toggleEmpty(data.total === 0);
}

async function loadSections() {
  const seq = ++renderSeq;
  S.items = [];
  $('#grid').innerHTML = '';
  $('#loadMoreWrap').hidden = true;
  const st = S.state;
  let sections = [];

  if (S.group === 'theme') {
    sections = st.themes.filter((t) => t.count > 0)
      .map((t) => ({ key: t.id, label: `${t.emoji} ${t.name}`, color: t.color, count: t.count, param: `theme=${t.id}` }));
  } else if (S.group === 'month') {
    sections = (st.years || []).filter((y) => y.c > 0).slice(0, 14)
      .map((y) => ({
        key: y.year, label: `🗓️ ${y.year} 年`, color: '#7c5cff', count: y.c,
        param: `year=${encodeURIComponent(y.year)}`,
        subs: y.months.map((m) => ({ p: `month=${encodeURIComponent(m.key)}`, t: String(m.key).slice(5) + ' 月', c: m.c })),
      }));
  } else {
    sections = st.folders.slice(0, 14)
      .map((f) => ({ key: f.key, label: `📁 ${shortFolder(f.key)}`, color: '#22d3ee', count: f.c, param: `folder=${encodeURIComponent(f.key)}` }));
  }

  for (const sec of sections) {
    const box = document.createElement('section');
    const subHtml = (sec.subs && sec.subs.length)
      ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 8px">` +
        sec.subs.map((s) => `<a href="#" data-jump="${esc(s.p)}" style="font-size:12px;color:#9aa6bf;text-decoration:none;border:1px solid #2b3140;border-radius:6px;padding:1px 8px">${esc(s.t)} · ${s.c}</a>`).join('') + `</div>`
      : '';
    box.innerHTML = `<h3 class="section-h"><i class="dot" style="background:${sec.color}"></i>
        ${esc(sec.label)} <span>${sec.count} 张</span>
        <a href="#" data-jump="${esc(sec.param)}" style="margin-left:auto;font-size:12px;color:#9aa6bf;text-decoration:none">查看完整 →</a></h3>
        ${subHtml}<div class="cells size-${S.size}"></div>`;
    $('#grid').append(box);
    const holder = box.querySelector('.cells');
    try {
      const data = await api(`/api/photos?${sec.param}&order=${S.order}&size=${SECTION_PREVIEW}`);
      if (seq !== renderSeq) return;
      S.items.push(...data.items);
      holder.append(...buildGrid(data.items));
    } catch (e) { /* 单个分区失败不影响其它 */ }
  }
  boxJumpHooks();
  renderCrumb();
  $('#crumbCount').textContent = `${st.stats.total} 张`;
  toggleEmpty(!st.stats.total);
}

function boxJumpHooks() {
  $$('[data-jump]').forEach((a) => a.addEventListener('click', (ev) => {
    ev.preventDefault();
    const params = new URLSearchParams(a.dataset.jump);
    S.theme = params.get('theme') || '';
    S.month = params.get('month') || '';
    S.year = params.get('year') || '';
    S.folder = params.get('folder') || '';
    S.root = '';
    renderSidebar();
    loadFlat();
  }));
}

async function renderMain() {
  if (S.aiMode) { await loadAi(); syncStickyOffset(); return; }
  const filtered = S.theme || S.month || S.folder || S.root || S.q;
  if (filtered) { await loadFlat(); } else { await loadSections(); }
  syncStickyOffset();
}

/* 分区标题的 sticky 位置要跟着工具栏实际高度走（窄屏换行会变高） */
function syncStickyOffset() {
  const tb = document.querySelector('.toolbar');
  if (!tb) return;
  document.documentElement.style.setProperty('--tb-h', `${tb.offsetHeight}px`);
}

/* ---------------------------------------------------------------- AI 查找 */

/* 从其它入口（侧栏筛选、分组、分区标题跳转）离开 AI 模式：只改状态不重绘 */
function leaveAi() {
  if (!S.aiMode) return;
  S.aiMode = false; S.aiText = ''; S.ai = null; S.aiOff = []; S.aiKeep = [];
  $$('#modeSeg button').forEach((x) => x.classList.toggle('on', x.dataset.mode === 'kw'));
  $('#searchIcon').textContent = '🔍';
  $('#searchInput').value = '';
  $('#searchInput').placeholder = '搜索文件名、文件夹或主题…';
  $('#searchKbd').hidden = false;
  $('#btnAiGo').hidden = true;
  $('#aiPanel').hidden = true;
}

function setAiMode(on) {
  const had = $('#searchInput').value.trim();
  S.aiMode = !!on;
  $$('#modeSeg button').forEach((x) => x.classList.toggle('on', (x.dataset.mode === 'ai') === S.aiMode));
  $('#searchIcon').textContent = S.aiMode ? '✨' : '🔍';
  $('#searchInput').placeholder = S.aiMode
    ? '描述一下：大概什么时候拍的、拍的是什么…（例：去年夏天在海边）'
    : '搜索文件名、文件夹或主题…';
  $('#searchKbd').hidden = S.aiMode;
  $('#btnAiGo').hidden = !S.aiMode;
  $('#aiPanel').hidden = !S.aiMode;
  S.aiOff = [];
  S.aiKeep = [];
  S.ai = null;
  if (S.aiMode) {
    S.theme = S.month = S.folder = S.root = S.year = S.shot = '';
    S.q = '';
    S.aiText = had;               // 输入框里已经有字，直接拿它当描述
  } else {
    S.aiText = '';
    $('#searchInput').value = '';
  }
  renderSidebar();
  renderMain();
}

function aiSubmit() {
  const v = $('#searchInput').value.trim();
  if (!v) { toast('先说说要找的照片，比如「去年夏天在海边拍的」', true); return; }
  S.aiText = v;
  S.aiOff = [];
  S.aiKeep = [];
  S.aiExpand = false;      // 每次新搜索都重新折叠成一行
  loadAi();
}

async function loadAi() {
  const seq = ++renderSeq;
  const box = $('#grid');
  $('#loadMoreWrap').hidden = true;
  if (!S.aiText) {
    S.items = []; S.total = 0; S.ai = null;
    box.innerHTML = '';
    renderAiPanel(null);
    renderCrumb();
    toggleEmpty(true);
    syncStickyOffset();
    return;
  }
  let d;
  try {
    d = await api('/api/ai/search', {
      method: 'POST',
      body: JSON.stringify({
        text: S.aiText, ignore: S.aiOff, keep: S.aiKeep,
        limit: 240, engine: S.aiEngine,
      }),
    });
  } catch (e) { toast(e.message, true); return; }
  if (seq !== renderSeq) return;
  S.ai = d;
  S.items = d.items;
  S.total = d.total;
  box.innerHTML = '';
  const holder = document.createElement('div');
  holder.className = `cells flat size-${S.size}`;
  box.append(holder);
  holder.append(...buildGrid(d.items));
  renderAiPanel(d);
  renderCrumb();
  toggleEmpty(!d.items.length);
  syncStickyOffset();
}

function renderAiPanel(d) {
  const panel = $('#aiPanel');
  const ex = (S.state && S.state.ai_examples) || [];
  $('#aiTries').innerHTML = '<span class="tries-lead">试试这样问：</span>'
    + ex.map((t) => `<button class="chip try" data-try="${esc(t)}">${esc(t)}</button>`).join('');

  // 说清这次是哪套理解在干活，回落时把原因一并摆出来
  const tag = $('#aiEngineTag');
  const eng = d && d.engine;
  const model = (S.llm && S.llm.text_model) || 'AI 模型';
  tag.hidden = !eng;
  tag.className = `engine-tag ${eng === 'qwen' ? 'qwen' : 'rules'}`;
  tag.textContent = eng === 'qwen' ? `🧠 ${model} 在理解这句话` : '🔧 本地规则在理解这句话';
  tag.title = (d && d.engine_note) || '';
  $('#btnAiEngine').textContent =
    { auto: '🧠 自动', qwen: '🧠 只用 AI 模型', rules: '🔧 只用本地规则' }[S.aiEngine];

  // 先算清长文案要不要显示（只有展开态才用得上）
  let noteText = '', noteShow = false;
  if (d && d.engine_note && d.engine === 'rules' && d.conds && d.conds.length) {
    noteText = d.engine_note + (d.note ? `；${d.note}` : ''); noteShow = true;
  } else if (d && d.note) {
    noteText = d.note; noteShow = true;
  } else if (d && !d.items.length) {
    noteText = '这些条件一张都没命中，点上面的条件可以先忽略它再找一次。'; noteShow = true;
  }
  // 本次用了时间条件时，补一句“还有 N 张时间未知的照片没纳入”
  const hasTime = d && d.conds && d.conds.some((c) => c.kind === 'time');
  const unkShot = (S.state && S.state.stats && S.state.stats.shot_unknown) || 0;
  if (hasTime && unkShot) {
    noteText = (noteText ? noteText + '；' : '') + `另有 ${unkShot} 张拍摄时间未知的照片未纳入`;
    noteShow = true;
  }
  const note = $('#aiNote');
  note.textContent = noteText;

  const chips = $('#aiConds');
  const toggle = $('#btnAiToggle');

  // 空闲 / 没听出条件：凑不成一套条件，不折叠，保持原样（示例仍可点）
  if (!d || !d.conds.length) {
    panel.classList.remove('collapsed');
    toggle.hidden = true;
    note.hidden = true;
    $('#aiTries').hidden = false;
    chips.innerHTML = !d
      ? '<span class="cond-none">说一句话，或点下面的例子试试</span>'
      : '<span class="cond-none">这句里没听出可用条件</span>';
    return;
  }

  const all = d.conds.map(buildCondChip);
  const collapsed = !S.aiExpand;
  panel.classList.toggle('collapsed', collapsed);
  toggle.hidden = false;
  if (collapsed) {
    chips.innerHTML = all.slice(0, 3).join('');   // 折叠：只摆前 3 个，仍可点忽略/坚持
    toggle.textContent = `展开全部 ${d.conds.length} 个条件`;
    note.hidden = true;                            // 折叠时藏长文案
    $('#aiTries').hidden = true;                   // 折叠时藏“试试这样问”
  } else {
    chips.innerHTML = all.join('');
    toggle.textContent = '收起';
    note.hidden = !noteShow;
    $('#aiTries').hidden = false;
  }
}

function buildCondChip(c) {
    // 三种“不在场”长得一样但意思不同，分开标：用户关的 / 系统自动放宽的 / 用户坚持要的
    const off = S.aiOff.indexOf(c.cid) >= 0;
    const kept = (S.aiKeep || []).indexOf(c.cid) >= 0;
    let cls = c.hard ? 'hard' : 'soft';
    let mark = c.hard ? '●' : '○';
    let state = 'ignore';            // 点下去要做什么
    let act = '点击忽略并重新找';
    if (kept) {
      cls = 'kept'; mark = '🔒'; state = 'drop'; act = '已坚持这个条件，点一下放回自动放宽';
    } else if (off) {
      cls = 'off'; mark = '✕'; state = 'drop'; act = '已忽略，点开恢复';
    } else if (c.ignored) {
      cls = 'relax'; mark = '↡'; state = 'keep'; act = '已被自动放宽，点一下坚持它（就不允许再放宽）';
    }
    const kind = { time: '时间', theme: '主题', keyword: '文字线索', visual: '画面感觉',
      daypart: '时段', meta: '拍摄信息', object: '画面内容', exclude: '不含' }[c.kind] || c.kind;
    const tip = `${kind} · 权重 ${c.weight}`
      + (c.hard ? ' · 硬性条件（不满足就不展示）' : ' · 加分条件')
      + ` · ${act}`;
    return `<button class="cond ${cls}" data-cid="${esc(c.cid)}" data-act="${state}" title="${esc(tip)}">`
      + `${mark} ${esc(c.label)}</button>`;
}

const EMPTY_DEFAULT = {
  emoji: '🖼️',
  title: '相册还空着',
  desc: '导入一个存放照片的文件夹，软件会自动分析每张照片的色彩、构图、纹理与 EXIF 特征，<br>'
    + '把它们分到「人像 / 自然风光 / 城市建筑 / 美食 / 动物 / 夜景 / 黑白 / 截图文档」等主题里。',
};

function toggleEmpty(isEmpty) {
  const st = S.state;
  const box = $('#emptyState');
  const filtered = !!(S.theme || S.month || S.folder || S.root || S.q);
  if (!isEmpty || st.job.active) { box.hidden = true; return; }
  if (S.aiMode && st.stats.total) {
    $('#emptyEmoji').textContent = S.aiText ? '🔎' : '✨';
    $('#emptyTitle').textContent = S.aiText ? '没找到符合的照片' : '描述一下要找的照片';
    $('#emptyDesc').innerHTML = S.aiText
      ? '可以把不重要的条件点掉（上面那些 chips），或者说得更宽一点：<br>'
        + '时间说「那段时间」，内容说「吃的」「出去玩」。'
      : '说一句大白话就能找到照片：时间（去年夏天、国庆、三个月前）、<br>'
        + '内容（火锅、截图、海边、宝宝）、画面感觉（夜景、黑白、很亮、竖图）。';
    $('#emptyActions').hidden = true;
    $('#commonPaths').hidden = true;
    box.hidden = false;
    return;
  }
  if (filtered && st.stats.total > 0) {
    $('#emptyEmoji').textContent = '🔍';
    $('#emptyTitle').textContent = '没有匹配的照片';
    $('#emptyDesc').innerHTML = '当前筛选条件下没有照片。<br>点侧栏「全部照片」或清空搜索词即可回到相册。';
    $('#emptyActions').hidden = true;
    $('#commonPaths').hidden = true;
  } else {
    $('#emptyEmoji').textContent = EMPTY_DEFAULT.emoji;
    $('#emptyTitle').textContent = EMPTY_DEFAULT.title;
    $('#emptyDesc').innerHTML = EMPTY_DEFAULT.desc;
    $('#emptyActions').hidden = false;
    $('#commonPaths').hidden = false;
  }
  box.hidden = false;
}

/* ---------------------------------------------------------------- 任务进度 */

function paintJob(job) {
  const bar = $('#jobbar');
  if (!job || (!job.active && job.phase === 'idle')) {
    const recently = job && job.finished_at && (Date.now() / 1000 - job.finished_at < 6);
    if (!recently) { bar.hidden = true; return; }
  }
  bar.hidden = false;
  const pct = job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
  $('#jobFill').style.width = `${job.active ? pct : (pct || 100)}%`;
  $('#jobPct').textContent = job.active ? `${pct}%` : '完成';
  const label = { scanning: '扫描文件夹', classifying: '识别照片主题',
    starting: '准备中', ai: 'AI 看图说话' }[job.phase] || '';
  $('#jobText').textContent = `${label} · ${job.message || ''}${job.current ? `（当前：${job.current}）` : ''}`;
  $('#btnCancelJob').hidden = !job.active;
}

function pollJob() {
  clearInterval(jobTimer);
  jobTimer = setInterval(async () => {
    let job;
    try { job = await api('/api/job'); } catch (e) { clearInterval(jobTimer); return; }
    paintJob(job);
    if (!job.active) {
      clearInterval(jobTimer);
      await refreshState();
      await renderMain();
      toast(job.message || '识别完成');
      setTimeout(() => { $('#jobbar').hidden = true; }, 5000);
    }
  }, 900);
}

async function refreshState() {
  S.state = await api('/api/state');
  S.llm = S.state.llm || null;
  renderSidebar();
  paintLlmButton();
  paintJob(S.state.job);
  if (S.state.job.active) pollJob();
}

/* ---------------------------------------------------------------- AI 模型设置 */

// 指到本机的服务一律算“本地模型”：免费、不需要 Key、多半比较慢
const LOCAL_BASE = /^(https?:\/\/)?(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(:\d+)?([/?]|$)/i;

function paintLlmButton() {
  const c = S.llm || {};
  let who = '本地规则';
  if (c.enabled) {
    who = c.needs_key ? '缺 API Key'
      : `${c.text_model || 'AI 模型'}${c.local ? '（本地）' : '（云端）'}`;
  }
  $('#btnLlm').textContent = `🧠 AI 设置 · ${who}`;
}

/* 面板顶部那行“当前用的是谁”：直接读表单，改哪个字段就跟着变 */
function paintLlmNow() {
  const on = $('#llmEnabled').checked;
  const local = LOCAL_BASE.test($('#llmBase').value.trim());
  const txt = $('#llmTextModel').value.trim() || '（未填）';
  const vl = $('#llmVlModel').value.trim() || '（未填）';
  $('#llmNow').textContent = on
    ? `当前：文本 ${txt} · 视觉 ${vl}　来源：${local ? '本机服务（免费，不需要 Key）' : '云端服务'}　超时 ${$('#llmTimeout').value || '—'} 秒`
    : '开关是关的：查找只用本地规则，照片也不会写 AI 描述。填好下面的地址再打开开关才会用模型。';
  $('#llmKeyHint').textContent = local
    ? '本机服务不需要 Key，留空即可'
    : ($('#llmKey').value.trim() ? '这是本次新填的，保存后生效'
      : (S.llm && S.llm.has_key
        ? `已保存，结尾 ${S.llm.key_tail}${S.llm.from_env ? '（环境变量 DASHSCOPE_API_KEY 也在生效）' : ''}`
        : '还没填（云端必填，本机不用）'));
  // “Key 与地域绑定”只是部分云端服务的坑，认不出来的服务别瞎提
  const regional = /dashscope/i.test($('#llmBase').value.trim());
  $('#llmBaseHint').textContent = local
    ? '本机服务：模型名要和你机器上已经装好的那个一致（Ollama 可用 ollama list 查），不需要 Key'
    : (regional ? '百炼的 Key 与地址必须同一个地域（北京 / 新加坡互不相通），否则报 401'
      : '云端服务需要 API Key；填本机地址（127.0.0.1）不需要 Key');
  $('#btnLlmLabel').textContent = local ? '✨ 本机看图写描述' : '✨ 看图写描述';
}

function fillLlm(cfg, ai, stat) {
  if (!cfg) return;
  S.llm = cfg;
  $('#llmEnabled').checked = !!cfg.enabled;
  $('#llmKey').value = '';
  $('#llmTextModel').value = cfg.text_model || '';
  $('#llmVlModel').value = cfg.vl_model || '';
  $('#llmBase').value = cfg.base_url || '';
  $('#llmWorkers').value = cfg.workers;
  $('#llmTimeout').value = cfg.timeout;
  $('#llmMaxPx').value = cfg.max_px;
  paintLlmNow();
  const total = (ai.done || 0) + (ai.error || 0) + (ai.none || 0);
  const st = stat || cfg.stat || { calls: 0, in_tok: 0, out_tok: 0, ms: 0 };
  const avg = st.calls ? Math.round(st.ms / st.calls) : 0;
  $('#llmCost').innerHTML = `已写描述：<b>${ai.done}</b> / ${total} 张（失败 ${ai.error}、未处理 ${ai.none}）<br>`
    + `本次开机累计调用 ${st.calls} 次，输入 ${st.in_tok} token / 输出 ${st.out_tok} token，平均 ${avg}ms 一次。`;
  paintLlmButton();
}

async function openLlmModal() {
  let d;
  try { d = await api('/api/llm/config'); } catch (e) { toast(e.message, true); return; }
  fillLlm(d.config, d.ai, d.stat);
  $('#llmMsg').className = 'llm-msg';
  $('#llmMsg').textContent = '';
  $('#llmModal').hidden = false;
}

function llmBody(extra) {
  return {
    enabled: $('#llmEnabled').checked,
    api_key: $('#llmKey').value.trim(),
    text_model: $('#llmTextModel').value.trim(),
    vl_model: $('#llmVlModel').value.trim(),
    base_url: $('#llmBase').value.trim(),
    workers: $('#llmWorkers').value,
    timeout: $('#llmTimeout').value,
    max_px: $('#llmMaxPx').value,
    ...(extra || {}),
  };
}

function llmMsg(msg, bad) {
  const el = $('#llmMsg');
  el.className = `llm-msg${bad ? ' bad' : ''}`;
  el.textContent = msg;
}

async function saveLlm(extra) {
  let d;
  try { d = await api('/api/llm/config', { method: 'POST', body: JSON.stringify(llmBody(extra)) }); }
  catch (e) { llmMsg(`保存失败：${e.message}`, true); return null; }
  fillLlm(d.config, d.ai, d.stat);
  await refreshState();
  return d;
}

async function testLlm() {
  llmMsg('正在试：先调文本模型、再拿一张 32×32 小图调视觉模型…');
  try {
    if ($('#llmKey').value.trim() || $('#llmEnabled').checked !== (S.llm || {}).enabled) {
      if (!await saveLlm()) return;           // 改了东西先存下再试，否则试的是旧配置
    }
    const d = await api('/api/llm/test', { method: 'POST', body: '{}' });
    llmMsg(`通了 ✅ 文本「${d.text.model}」回：${d.text.reply}（${d.text.ms}ms）；` +
      `视觉「${d.vision.model}」回：${d.vision.reply}（${d.vision.ms}ms）`);
  } catch (e) {
    const stage = e.data && e.data.stage === 'vision' ? '，文本模型是通的，卡在视觉模型' : '';
    llmMsg(`没通（先试的文本${stage ? '，文本过了' : ''}）：${e.message}${stage}`, true);
  }
}

async function startLabel() {
  if (!await saveLlm()) return;
  let job;
  try {
    job = await api('/api/ai/label', {
      method: 'POST', body: JSON.stringify({ force: $('#llmForce').checked }),
    });
  } catch (e) { llmMsg(e.message, true); return; }
  llmMsg(`已开始：${job.pending} 张照片排队等模型看图。可以关掉弹窗，进度在顶部条子上。`);
  toast(`开始为 ${job.pending} 张照片写描述…`);
  $('#llmModal').hidden = true;
  paintJob(job); pollJob();
}

async function resetAiLabel() {
  const btn = $('#btnLlmReset');
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.textContent = '再点一次确认清除';
    btn.classList.add('warn');
    setTimeout(() => { btn.dataset.armed = ''; btn.textContent = '清除 AI 描述'; btn.classList.remove('warn'); }, 4000);
    return;
  }
  btn.dataset.armed = '';
  btn.textContent = '清除 AI 描述';
  btn.classList.remove('warn');
  let d;
  try { d = await api('/api/ai/reset', { method: 'POST', body: '{}' }); }
  catch (e) { llmMsg(e.message, true); return; }
  llmMsg(d.note || '已清除。');
  try { const c = await api('/api/llm/config'); fillLlm(c.config, c.ai, c.stat); }
  catch (e) { /* 刷不出来不影响已完成的清除 */ }
  await refreshState();
  await renderMain();
}

/* ---------------------------------------------------------------- 大图查看 */

async function openLightbox(id) {
  // 上一次点的可能是另一批照片（切换筛选 / AI 结果），列表换过就别用旧缓存
  let list = S.lbList.length ? S.lbList : S.items;
  if (!list.some((p) => p.id === id)) list = S.items;
  S.lbList = list;
  S.lbIndex = Math.max(list.findIndex((p) => p.id === id), 0);
  const p = list[S.lbIndex];
  if (!p) return;
  $('#lightbox').hidden = false;
  $('#lbImage').src = `/api/file/${p.id}`;
  $('#lbOpen').href = `/api/file/${p.id}`;
  document.body.style.overflow = 'hidden';

  let d;
  try { d = await api(`/api/photo/${p.id}`); } catch (e) { toast(e.message, true); return; }
  const t = themeMeta(d.theme);

  $('#lbName').textContent = d.name;
  $('#lbBadge').textContent = `${t.emoji} ${t.name}`;
  $('#lbBadge').style.cssText = `color:${t.color};border-color:${t.color}66;background:${t.color}1a`;
  const src = d.theme_src || 'rule';
  $('#lbConf').textContent = src === 'ai' ? '主题：模型判定（不显示规则置信度）'
    : src === 'manual' ? '主题：人工设定'
    : (d.conf ? `置信度 ${d.conf}%` : '未识别');
  if (p.match != null) $('#lbConf').textContent += ` · AI 匹配 ${p.match}%`;
  $('#lbManual').hidden = !d.manual;
  const aiLine = d.caption
    ? `🧠 ${(S.llm && S.llm.enabled && S.llm.vl_model) || 'AI'} 看到：${esc(d.caption)}`
      + (d.tags && d.tags.length ? `　·　${d.tags.map(esc).join(' / ')}` : '') + '<br>'
    : (d.ai_status === 'error' ? `🧠 这张没标注成功：${esc(d.ai_err || '原因不明')}<br>` : '');
  const aiWhy = (p.hits && p.hits.length)
    ? `<i class="aihit">✨ 命中：${esc(p.hits.join('；'))}</i><br>` : '';
  const whyHtml = (d.reasons || []).length
    ? d.reasons.map((r) => `<i>· ${esc(r)}</i>`).join('<br>')
    : '<i>暂无判定依据</i>';
  $('#lbReason').innerHTML = aiLine + aiWhy + whyHtml;

  const scores = d.scores || {};
  const max = Math.max(1, ...Object.values(scores));
  $('#lbScores').innerHTML = Object.entries(scores)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => {
      const m = themeMeta(k);
      return `<div class="row"><span>${m.emoji} ${m.name}</span>
        <span class="t"><i style="width:${Math.round((v / max) * 100)}%;background:${m.color}"></i></span>
        <span style="text-align:right">${v.toFixed(1)}</span></div>`;
    }).join('');

  const ex = d.exif || {};
  const rows = [
    ['拍摄时间', d.taken || '<b style="color:#e0a458">未知</b>（无 EXIF 拍摄时间 · 不参与按时间查找）'],
    ['尺寸', `${d.width} × ${d.height}（${d.feats?.megapixels || '-'} MP）`],
    ['文件大小', fmtBytes(d.bytes)],
    ['来源文件夹', esc(shortFolder(d.folder))],
  ];
  if (ex.camera) rows.push(['相机', esc(ex.camera)]);
  if (ex.focal) rows.push(['焦距', esc(String(ex.focal))]);
  if (ex.iso) rows.push(['ISO', esc(String(ex.iso))]);
  if (ex.software) rows.push(['软件', esc(ex.software)]);
  if (ex.gps) rows.push(['GPS', esc(ex.gps)]);
  $('#lbMeta').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

  $('#lbPath').textContent = d.path;
  $('#lbThemeSel').innerHTML = (S.state.themes || []).map((x) =>
    `<option value="${x.id}" ${x.id === d.theme ? 'selected' : ''}>${x.emoji} ${x.name}</option>`).join('');
  $('#lbReset').hidden = !d.manual;
  $('#lbRemove').dataset.id = d.id;
  $('#lbApply').dataset.id = d.id;
}

function closeLightbox() {
  $('#lightbox').hidden = true;
  $('#lbImage').removeAttribute('src');
  document.body.style.overflow = '';
}

function lbStep(delta) {
  if (!S.lbList.length) return;
  S.lbIndex = (S.lbIndex + delta + S.lbList.length) % S.lbList.length;
  openLightbox(S.lbList[S.lbIndex].id);
}

/* ---------------------------------------------------------------- 事件绑定 */

function bind() {
  // 侧栏筛选
  $('#themeList').addEventListener('click', (e) => {
    const li = e.target.closest('[data-theme]');
    if (!li) return;
    leaveAi();
    S.theme = li.dataset.theme; S.month = S.folder = S.root = S.year = S.shot = '';
    renderSidebar(); renderMain();
  });
  $('#monthList').addEventListener('click', (e) => {
    const yb = e.target.closest('[data-year]');
    if (yb) {
      leaveAi();
      const y = yb.dataset.year;
      S.month = S.theme = S.folder = S.root = S.shot = '';
      S.year = (S.year === y) ? '' : y;   // 再点同一年标题 = 取消整年筛选并收起
      renderSidebar(); renderMain();
      return;
    }
    const mb = e.target.closest('[data-month]');
    if (mb) {
      leaveAi();
      S.month = mb.dataset.month; S.year = S.theme = S.folder = S.root = S.shot = '';
      renderSidebar(); renderMain();
    }
  });
  // 点侧栏「时间未知」提示：把这批没有 EXIF 拍摄时间的照片单独列出来核对
  $('#timeTip').addEventListener('click', (e) => {
    const a = e.target.closest('[data-unknown]');
    if (!a) return;
    e.preventDefault();
    leaveAi();
    S.shot = (S.shot === 'unknown') ? '' : 'unknown';
    S.theme = S.month = S.year = S.folder = S.root = '';
    renderSidebar(); renderMain();
  });
  $('#folderList').addEventListener('click', (e) => {
    const li = e.target.closest('[data-folder]');
    if (!li) return;
    leaveAi();
    S.folder = li.dataset.folder; S.theme = S.month = S.root = S.year = S.shot = '';
    renderSidebar(); renderMain();
  });
  $('#rootList').addEventListener('click', async (e) => {
    const del = e.target.closest('[data-delroot]');
    if (del) {
      e.stopPropagation();
      await api(`/api/root?root=${encodeURIComponent(del.dataset.delroot)}`, { method: 'DELETE' });
      toast('已移除该目录记录（原图不受影响）');
      await refreshState(); renderMain();
      return;
    }
    const li = e.target.closest('[data-root]');
    if (!li) return;
    leaveAi();
    S.root = li.dataset.root; S.theme = S.month = S.folder = S.year = S.shot = '';
    renderSidebar(); renderMain();
  });

  // 顶栏
  $('#btnImport').addEventListener('click', () => { $('#importModal').hidden = false; $('#importPaths').focus(); });
  $('#btnImport2').addEventListener('click', () => { $('#importModal').hidden = false; $('#importPaths').focus(); });
  $$('[data-close]').forEach((b) => b.addEventListener('click', () => { $(`#${b.dataset.close}`).hidden = true; }));
  $('#importModal').addEventListener('click', (e) => { if (e.target.id === 'importModal') $('#importModal').hidden = true; });

  document.body.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-quick]');
    if (!chip) return;
    const ta = $('#importPaths');
    const lines = ta.value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!lines.includes(chip.dataset.quick)) lines.push(chip.dataset.quick);
    ta.value = lines.join('\n');
    ta.focus();
  });

  $('#btnDoImport').addEventListener('click', async () => {
    const paths = $('#importPaths').value.split('\n').map((x) => x.trim().replace(/^"|"$/g, '')).filter(Boolean);
    if (!paths.length) { toast('请先填写至少一个文件夹路径', true); return; }
    try {
      const job = await api('/api/import', {
        method: 'POST',
        body: JSON.stringify({ paths, recursive: $('#importRecursive').checked }),
      });
      $('#importModal').hidden = true;
      $('#importPaths').value = '';
      toast(`开始处理 ${paths.length} 个目录…`);
      paintJob(job); pollJob();
    } catch (e) { toast(e.message, true); }
  });

  $('#btnReclassify').addEventListener('click', async () => {
    try {
      const job = await api('/api/reclassify', { method: 'POST' });
      toast('正在按当前规则重新识别全部照片…');
      paintJob(job); pollJob();
    } catch (e) { toast(e.message, true); }
  });

  $('#btnDemo').addEventListener('click', async () => {
    try {
      const r = await api('/api/demo', { method: 'POST', body: '{}' });
      toast(`已生成 ${r.created} 张示例照片，正在识别…`);
      paintJob(r.job); pollJob();
    } catch (e) { toast(e.message, true); }
  });

  $('#btnCancelJob').addEventListener('click', async () => {
    paintJob(await api('/api/job/cancel', { method: 'POST' }));
  });

  // 搜索：关键词模式边打边查，AI 模式等回车或点「查找」
  let t = null;
  $('#searchInput').addEventListener('input', (e) => {
    if (S.aiMode) return;
    clearTimeout(t);
    t = setTimeout(() => { S.q = e.target.value.trim(); renderMain(); }, 380);
  });
  $('#searchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && S.aiMode) { e.preventDefault(); aiSubmit(); }
  });
  $('#modeSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    if ((b.dataset.mode === 'ai') === S.aiMode) return;
    setAiMode(b.dataset.mode === 'ai');
  });
  $('#btnAiGo').addEventListener('click', aiSubmit);
  $('#btnAiExit').addEventListener('click', () => setAiMode(false));
  // 点条件 chip = 在「正常 / 忽略 / 坚持不放宽」之间流转，马上重查
  $('#aiConds').addEventListener('click', (e) => {
    const b = e.target.closest('[data-cid]'); if (!b) return;
    const cid = b.dataset.cid;
    const act = b.dataset.act;
    const rm = (arr, v) => { const i = arr.indexOf(v); if (i >= 0) arr.splice(i, 1); };
    if (act === 'ignore') S.aiOff.push(cid);
    else if (act === 'keep') S.aiKeep.push(cid);
    else { rm(S.aiOff, cid); rm(S.aiKeep, cid); }
    loadAi();
  });
  $('#aiTries').addEventListener('click', (e) => {
    const b = e.target.closest('[data-try]'); if (!b) return;
    $('#searchInput').value = b.dataset.try;
    aiSubmit();
  });
  // 折叠/展开条件面板：只重画面板、不重新请求，展开态那套忽略/坚持照旧能用
  $('#btnAiToggle').addEventListener('click', () => {
    S.aiExpand = !S.aiExpand;
    renderAiPanel(S.ai);
  });
  // 自动 → 只用模型 → 只用本地规则 → 自动（两边效果可以马上对比）
  $('#btnAiEngine').addEventListener('click', () => {
    S.aiEngine = S.aiEngine === 'auto' ? 'qwen' : (S.aiEngine === 'qwen' ? 'rules' : 'auto');
    if (S.aiText) loadAi(); else renderAiPanel(null);
  });

  // AI 模型设置
  $('#btnLlm').addEventListener('click', openLlmModal);
  $('#llmModal').addEventListener('click', (e) => { if (e.target.id === 'llmModal') $('#llmModal').hidden = true; });
  $('#btnLlmSave').addEventListener('click', async () => {
    if (await saveLlm()) llmMsg('已保存到 ' + ((S.llm && S.llm.conf_path) || 'data/llm.json') + '。');
  });
  $('#btnLlmTest').addEventListener('click', testLlm);
  $('#btnLlmLabel').addEventListener('click', startLabel);
  $('#btnLlmReset').addEventListener('click', resetAiLabel);
  $('#btnLlmClearKey').addEventListener('click', async () => {
    $('#llmKey').value = '';
    if (await saveLlm({ clear_key: true })) llmMsg('已清除本机保存的 API Key。'
      + (S.llm && S.llm.from_env ? '（但环境变量 DASHSCOPE_API_KEY 还在，想彻底关掉得先清掉那个变量）' : ''));
  });
  // 地址一改，Key 是不是必填、提示怎么说都跟着变，所以当场重画
  $('#llmModal').addEventListener('input', paintLlmNow);
  $$('#llmModal .preset button[data-base]').forEach((b) => {
    b.addEventListener('click', () => {
      $('#llmBase').value = b.dataset.base;
      paintLlmNow();
      if (LOCAL_BASE.test(b.dataset.base)) {
        llmMsg('已填入本机服务地址：把文本 / 视觉模型名改成你机器上已经装好的那个，'
          + '打开开关就能用（不需要 Key，也不花钱）。');
      }
    });
  });

  $('#groupSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    $$('#groupSeg button').forEach((x) => x.classList.toggle('on', x === b));
    leaveAi();
    S.group = b.dataset.group;
    S.theme = S.month = S.folder = S.root = S.year = S.shot = '';
    renderSidebar(); renderMain();
  });

  $('#sizeSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    $$('#sizeSeg button').forEach((x) => x.classList.toggle('on', x === b));
    S.size = b.dataset.size;
    S.lbList = [];
    $$('.cells').forEach((c) => {
      c.classList.remove('size-s', 'size-m', 'size-l');
      c.classList.add(`size-${S.size}`);
    });
  });

  $('#orderSel').addEventListener('change', (e) => { S.order = e.target.value; renderMain(); });
  $('#btnLoadMore').addEventListener('click', () => { S.page++; loadFlat(false); });

  // 大图
  $('#lbClose').addEventListener('click', closeLightbox);
  $('#lbPrev').addEventListener('click', () => lbStep(-1));
  $('#lbNext').addEventListener('click', () => lbStep(1));
  $('#lightbox').addEventListener('click', (e) => { if (e.target.id === 'lightbox') closeLightbox(); });
  $('#lbApply').addEventListener('click', async (e) => {
    const id = +e.target.dataset.id;
    const theme = $('#lbThemeSel').value;
    await api(`/api/photo/${id}/theme`, { method: 'POST', body: JSON.stringify({ theme }) });
    toast(`已归入「${themeMeta(theme).name}」（只改相册分组，原图未移动）`);
    patchItem(id, { theme, manual: true, theme_src: 'manual' });
    const cell = document.querySelector(`.cell[data-id="${id}"]`);
    if (cell) {
      const t = themeMeta(theme);
      const tag = cell.querySelector('.tag');
      tag.textContent = `${t.emoji} ${t.name}`;
      tag.style.color = t.color;
      cell.classList.add('manual');
      const bar = cell.querySelector('.cfl');
      if (bar) bar.style.background = t.color;
    }
    await refreshState();
    await openLightbox(id);            // 重拉详情，右侧面板同步刷新
  });
  $('#lbReset').addEventListener('click', async () => {
    const id = +$('#lbApply').dataset.id;
    const r = await api(`/api/photo/${id}/auto`, { method: 'POST' });
    toast(`已恢复自动识别：${r.theme_name || ''}（置信 ${r.conf || 0}%）`);
    patchItem(id, { theme: r.theme, manual: false, conf: r.conf || 0, theme_src: 'rule' });
    await refreshState();
    await renderMain();
    await openLightbox(id);
  });

  // “从相册移除”为两步确认，避免误点
  let removeArmed = 0;
  $('#lbRemove').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const id = +btn.dataset.id;
    if (removeArmed !== id) {
      removeArmed = id;
      btn.textContent = '再点一次确认移除';
      btn.classList.add('warn');
      setTimeout(() => { removeArmed = 0; btn.textContent = '从相册移除'; btn.classList.remove('warn'); }, 4000);
      return;
    }
    removeArmed = 0;
    btn.textContent = '从相册移除';
    btn.classList.remove('warn');
    await api(`/api/photo/${id}`, { method: 'DELETE' });
    toast('已从相册移除（磁盘上的原图仍然保留）');
    closeLightbox();
    await refreshState();
    removeCell(id);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeLightbox(); $('#importModal').hidden = true; $('#llmModal').hidden = true; }
    if (!$('#lightbox').hidden) {
      if (e.key === 'ArrowLeft') lbStep(-1);
      if (e.key === 'ArrowRight') lbStep(1);
    } else if (e.key === '/' && document.activeElement !== $('#searchInput')) {
      e.preventDefault(); $('#searchInput').focus();
    }
  });

  window.addEventListener('resize', syncStickyOffset);
  const tb = document.querySelector('.toolbar');
  if (tb && 'ResizeObserver' in window) new ResizeObserver(syncStickyOffset).observe(tb);
}

/* 局部更新（避免整页重绘） */
function patchItem(id, patch) {
  const p = S.items.find((x) => x.id === id);
  if (p) Object.assign(p, patch);
  const q = S.lbList.find((x) => x.id === id);
  if (q) Object.assign(q, patch);
}

function removeCell(id) {
  const cell = document.querySelector(`.cell[data-id="${id}"]`);
  if (cell) {
    const sec = cell.closest('.cells');
    cell.remove();
    if (sec && !sec.children.length) sec.closest('section')?.remove();
  }
  S.items = S.items.filter((p) => p.id !== id);
  S.lbList = S.lbList.filter((p) => p.id !== id);
}

/* 收集当前可见照片给大图导航用 */
function syncVisibleList() {
  const ids = $$('#grid .cell').map((c) => +c.dataset.id);
  S.lbList = ids.map((id) => S.items.find((p) => p.id === id)).filter(Boolean);
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      const img = en.target;
      img.src = img.dataset.src;
      img.addEventListener('load', () => {
        img.classList.add('ready');
        img.parentElement.querySelector('.skeleton')?.remove();
      }, { once: true });
      img.addEventListener('error', () => { img.dataset.src = ''; img.remove(); }, { once: true });
      io.unobserve(img);
    });
  }, { rootMargin: '320px 0px' });

  bind();
  await refreshState();
  await renderMain();
}

boot().catch((e) => {
  console.error(e);
  toast(`初始化失败：${e.message}`, true);
});
