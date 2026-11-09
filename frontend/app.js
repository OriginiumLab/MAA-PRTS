/* ============================================================
   MAA-PRTS 控制台前端
   ============================================================ */
'use strict';

const S = {
  settings: {}, accounts: [], current: '', detail: null,
  maa: {}, activities: [],
  basket: [], result: null, searching: false, appInfo: null,
  paths: {}, pack: {}, setup: {}, gamedata: {}, onboarded: false,
  appUpdate: null, updateBusy: false,
  paradoxPending: [],
  authorNames: {},   // 作业站作者 id → 昵称（搜索时顺手学，黑名单弹窗显示用）
};

/* ------------------------------------------------------ 黑名单（干员 / 作者 / 作业）
   干员黑名单是纯显示层过滤：比对结论（可直接抄 / 差一人 / 需练度）不变，
   只是屏蔽的干员 / 技能不再出现在搜索卡片与作业详情里。
   条目 {name, skills}：skills 空 = 整个干员；勾了技能 = 只隐藏带对应技能
   的要求（作业 chips、详情行有技能号，按它对位）。「缺 X / 练度不足 X」
   徽标只跟干员走（缺人是缺这个人，与技能无关），只有整个干员被屏蔽才隐藏。
   作者 / 作业黑名单都在后端过滤（fetch_ops 出口按 uploader_id / maa://id 滤），
   软件级、不跟账号走，这里不参与。作业黑名单条目直接存 MAA 代码（maa://12345）。 */
const charBL = () => {
  const m = new Map();
  (S.settings.charBlacklist || []).forEach(row => {
    if (typeof row === 'string') m.set(row, []);          // 脏数据兜底
    else if (row && row.name) m.set(row.name, row.skills || []);
  });
  return m;
};
const operHidden = o => {
  const sk = charBL().get(o.name);
  return sk !== undefined && (!sk.length || sk.includes(Number(o.skill) || 1));
};
const nameHidden = n => {
  const sk = charBL().get(n);
  return sk !== undefined && !sk.length;
};
const visOpers = arr => (arr || []).filter(o => !operHidden(o));
const visNames = arr => (arr || []).filter(n => !nameHidden(n));

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('服务返回异常（' + r.status + '）'); }
  if (!j.ok) throw new Error(j.error || '未知错误');
  return j.data;
}

/* ------------------------------------------------------------ 提示 */
function toast(msg, type = 'ok', ms = 2600) {
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'ok' ? '' : type);
  el.innerHTML = '<span class="bar"></span><span>' + esc(msg) + '</span>';
  $('#toast-root').appendChild(el);
  setTimeout(() => {
    el.style.transition = '.25s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateY(6px)';
    setTimeout(() => el.remove(), 260);
  }, ms);
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast((label || '已复制') + ' ' + text);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast((label || '已复制') + ' ' + text); }
    catch (err) { toast('复制失败，请手动复制：' + text, 'err'); }
    ta.remove();
  }
}

/* ------------------------------------------------------------ 弹窗 */
function modal(html, opts = {}) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = '<div class="modal">' + html + '</div>';
  $('#modal-root').appendChild(mask);
  const close = () => {
    mask.style.transition = '.14s ease';
    mask.style.opacity = '0';
    setTimeout(() => mask.remove(), 140);
  };
  mask.addEventListener('click', e => { if (e.target === mask) close(); });
  mask.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', close));
  if (opts.onMount) opts.onMount(mask, close);
  return { el: mask, close };
}

/* ------------------------------------------------------------ 启动 */
function waitPywebview(ms = 2000) {
  return new Promise(resolve => {
    const t0 = Date.now();
    const tick = () => {
      if (window.pywebview && window.pywebview.api) return resolve(window.pywebview.api);
      if (Date.now() - t0 > ms) return resolve(null);
      setTimeout(tick, 60);
    };
    tick();
  });
}

async function boot() {
  try {
    const bridge = await waitPywebview();
    if (bridge) {
      document.documentElement.classList.add('native');
      try { S.appInfo = await bridge.app_info(); } catch (e) { }
    }
    const d = await api('/api/bootstrap');
    S.settings = d.settings;
    S.accounts = d.accounts;
    S.maa = d.maa;
    S.activities = d.activities || [];
    S.paths = d.paths || {};
    S.pack = d.pack || {};
    S.gamedata = d.gamedata || {};
    S.setup = d.setup || {};
    S.current = d.lastAccount || (d.accounts[0] && d.accounts[0].file) || '';
    $('#basket-autostart').checked = S.settings.autoStartMaa !== false;   // 设置里的默认开关真正生效
    renderAccounts();
    renderUpdate();
    renderQuick();
    renderMaaState();
    fillBasketProfiles();
    if (S.current) await selectAccount(S.current, true);
    startPing();
    // 资源包后台自动更新：晚几秒，别和首屏渲染抢网络
    setTimeout(packAutoSync, 2500);
    // 程序本体检查更晚一点：两条链路错开，别同时占网络
    setTimeout(appAutoCheck, 4000);

    // 首次使用：先让用户填 token 与 MAA 目录，再进主流程
    // （needsMaa 按上次账号绑定的 MAA 判断——账号各自配了 MAA 就不再拦）
    if ((S.setup && (S.setup.needsToken || S.setup.needsMaa)) || !S.accounts.length) {
      openOnboarding();
      return;
    }

    // 支持 ?q=TL-10 直接搜索（做快捷方式用）
    const q0 = new URLSearchParams(location.search).get('q');
    if (q0 && S.current) {
      $('#search-input').value = q0;
      doSearch();
    }
  } catch (e) {
    toast('启动失败：' + e.message, 'err', 6000);
  }
}

function startPing() {
  const ping = () => fetch('/api/ping', { method: 'POST' }).catch(() => {});
  ping();
  setInterval(ping, 20000);
}

/* ------------------------------------------------------------ 账号 */
function renderAccounts() {
  const box = $('#account-list');
  if (!S.accounts.length) {
    box.innerHTML = '<div style="padding:14px 10px;font-size:12px;color:var(--text-3);line-height:1.8">还没有账号<br>点下面的按钮用森空岛 token 添加</div>';
    return;
  }
  box.innerHTML = S.accounts.map(a => {
    const st = a.stats || {};
    const cls = (st.sixStarCount >= 120 ? 'r6' : (st.sixStarCount >= 60 ? 'r5' : ''));
    const ch = (a.nickName || '?').replace(/[#＃].*$/, '').slice(0, 1);
    return `
      <button class="acct ${a.file === S.current ? 'active' : ''}" data-file="${esc(a.file)}"
        draggable="true" title="点击选中，拖动可排序">
        <span class="avatar ${cls}">${esc(ch)}</span>
        <span class="acct-main">
          <span class="acct-name">${esc(a.nickName)}</span>
          <span class="acct-sub">${esc(a.channelName || '官服')} · <b>${st.charCount || 0}</b>/${st.charTotal || 440} 干员</span>
        </span>
      </button>`;
  }).join('');
}

/* 账号列表排序：原生 HTML5 拖拽直接改序（同作业集 bindBasketDrag 的做法，
   浏览器自己区分「点一下选中」和「按下拖动」，无需长按门槛、拖完也不会
   补发 click）。顺序存 settings.accountOrder（list_accounts 按它排，
   没排过的账号按默认序跟在后面）。绑在静态容器 #account-list 上，
   renderAccounts 重建内部节点不影响。 */
function bindAccountOrder(box) {
  let dragEl = null;

  const saveOrder = async () => {
    const files = $$('.acct', box).map(b => b.dataset.file);
    // 内存数组同步成新序——否则下一次 renderAccounts（点选别的账号就会触发）
    // 还按旧序重建，界面看起来像「拖完又弹回去」
    S.accounts = files.map(f => S.accounts.find(a => a.file === f)).filter(Boolean);
    try {
      S.settings = (await api('/api/settings', { accountOrder: files })).settings;
    } catch (e) { /* 保存失败不回滚视觉顺序，刷新后会还原 */ }
  };

  box.addEventListener('click', e => {
    const b = e.target.closest('.acct');
    if (b) selectAccount(b.dataset.file);
  });

  box.addEventListener('dragstart', e => {
    const item = e.target.closest('.acct');
    if (!item) return;
    dragEl = item;
    item.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', item.dataset.file); } catch (_) { }
  });
  box.addEventListener('dragend', () => {
    if (!dragEl) return;
    dragEl.classList.remove('dragging');
    dragEl = null;
    saveOrder();
  });
  box.addEventListener('dragover', e => {
    if (!dragEl) return;
    const el = e.target.closest('.acct');
    if (!el || el === dragEl) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const r = el.getBoundingClientRect();
    const after = (e.clientY - r.top) > r.height / 2;
    box.insertBefore(dragEl, after ? el.nextSibling : el);
  });
}

async function selectAccount(file, silent) {
  if (!file) return;
  const changed = S.current && S.current !== file;
  S.current = file;
  renderAccounts();
  if (changed && S.basket.length) {
    S.basket = [];
    renderBasket();
    toast('已切换账号，作业集重新开始', 'warn');
  }
  try {
    const d = await api('/api/account?file=' + encodeURIComponent(file));
    S.detail = d;
    renderHero();
    updateParadoxChip();
    await api('/api/settings', { lastAccount: file });
    S.settings.lastAccount = file;
    refreshMaaStatus();          // 状态卡与配置下拉切到这个账号绑定的 MAA
    if (!silent) {
      const r = $('#results');
      if (r.dataset.mode) doSearch();
    }
  } catch (e) {
    toast('读取账号失败：' + e.message, 'err');
  }
}

function renderHero() {
  const d = S.detail;
  const importBtn = $('#btn-import-maa');
  if (importBtn) importBtn.hidden = !d;   // 有选中账号才给「从 MAA 更新干员信息」入口
  const algoSel = $('#algo-select');
  if (algoSel) {
    algoSel.disabled = !d;                        // 算法下拉跟账号走，没账号不可用
    if (d) algoSel.value = d.preferAlgo || 'rating';
  }
  if (!d) return;
  const a = d.account || {}, st = d.stats || {};
  const name = a.nickName || '未知账号';
  $('#hero-name').textContent = name;
  $('#hero-avatar').textContent = name.replace(/[#＃].*$/, '').slice(0, 1);
  const bits = [a.channelName || '', 'Lv' + (a.level || '?'), 'uid ' + (a.uid || '')];
  const sec = (a.secretary || {}).name;
  if (sec) bits.splice(2, 0, '看板 ' + sec);
  $('#hero-sub').textContent = bits.filter(Boolean).join(' · ');

  const total = st.charTotal || 440;
  const miss = st.missingCount || 0;
  const missing = d.missing || [];
  const missNames = missing.map(m => m.name).join('、');
  $('#hero-stats').innerHTML = `
    <div class="stat"><b>${st.charCount || 0}</b><span>干员 / ${total}</span></div>
    <div class="stat mint"><b>${st.e2Count || 0}</b><span>精二</span></div>
    <div class="stat"><b>${st.sixStarCount || 0}<i style="font-size:11px;color:var(--text-3);font-style:normal">/${st.sixStarTotal || 141}</i></b><span>六星</span></div>
    <button class="stat ${miss > 0 ? 'amber' : 'mint'} clickable" id="stat-missing"
            title="${miss ? '点击查看缺哪 ' + miss + ' 名干员' : '图鉴已满'}">
      <b>${miss}</b><span>缺卡</span>
    </button>
  `;
  const sm = $('#stat-missing');
  if (sm) sm.addEventListener('click', showMissing);
}

/* 缺卡清单：只列名字，按星级分组 */
function showMissing() {
  const d = S.detail || {};
  const list = d.missing || [];
  const name = (d.account || {}).nickName || '';
  if (!list.length) {
    toast('这个账号图鉴已满，没有缺卡', 'ok');
    return;
  }
  const groups = {};
  list.forEach(m => (groups[m.rarity] = groups[m.rarity] || []).push(m));
  const rarities = Object.keys(groups).map(Number).sort((a, b) => b - a);
  const body = rarities.map(r => `
    <div class="miss-group">
      <div class="miss-head"><b>${CN_RARITY[r] || r + '星'}</b>
        <span>${groups[r].length} 名</span></div>
      <div class="miss-names">${groups[r].map(m =>
        `<span class="miss-name r${r}" title="${esc(m.profession || '')}">${esc(m.name)}</span>`).join('')}</div>
    </div>`).join('');
  modal(`
    <h3>缺卡 · ${list.length} 名</h3>
    <div class="sub">${esc(name)} 的图鉴里还没有这些干员（按星级分组，共 ${list.length} 名）。</div>
    <div class="miss-wrap">${body}</div>
    <div class="modal-foot"><button class="btn-solid" data-close>知道了</button></div>
  `);
}

/* ------------------------------------------------------------ 从 MAA 更新干员信息
   已有账号的手动更新入口：用当前账号绑定的 MAA 目录里的干员识别结果
   （data/OperBoxData.json）刷新练度。不需要森空岛 token，后端同步处理
   直接返回结果（不走任务轮询）。注意本地识别不含技能/专精/模组，
   更新后这类要求按未练处理，须明示。（向导里新建账号的场景仍是「导入」） */
async function importFromMaa() {
  if (!S.current) { toast('先在左侧选择一个账号', 'warn'); return; }
  // 与后端 _maa_dir_for 同语义：账号绑定的目录优先，没绑定回退全局默认
  const dir = String((((S.detail || {}).maa || {}).dir) || S.settings.maaDir || '').trim();
  if (!dir) { toast('该账号没有绑定 MAA 目录，也没有设置全局默认', 'err'); return; }
  let pr;
  try {
    pr = await api('/api/maa/probe?dir=' + encodeURIComponent(dir));
  } catch (e) { toast('检测 MAA 目录失败：' + e.message, 'err'); return; }
  if (!pr.exeExists) { toast('这个 MAA 目录里没有 MAA.exe：' + dir, 'err'); return; }
  if (!pr.operbox) { toast('MAA 目录下没有 data/OperBoxData.json（先在 MAA 小工具里跑一次干员识别）', 'err', 4200); return; }
  const fmtTime = t => (t ? String(t).replace('T', ' ').slice(0, 16) : '—');
  const operboxFile = dir.replace(/[\\/]+$/, '') + '\\data\\OperBoxData.json';
  const who = (((S.detail || {}).account || {}).nickName) || S.current;
  modal(`
    <h3>从 MAA 更新干员信息</h3>
    <div class="sub">用这个 MAA 的干员识别结果更新「${esc(who)}」的练度，不需要森空岛 token。</div>
    <div class="path-list">
      <div class="path-item"><span>MAA 目录</span><code>${esc(dir)}</code></div>
      <div class="path-item"><span>识别文件</span><code>${esc(operboxFile)}</code></div>
      <div class="path-item"><span>识别结果</span><code>${
        pr.operboxCount != null ? pr.operboxCount + ' 名干员' : '内容待更新时解析'
      }${pr.operboxSyncTime ? ' · ' + esc(fmtTime(pr.operboxSyncTime)) + ' 识别' : ''}</code></div>
    </div>
    <div class="onb-hint bad">! 本地识别不含技能、专精、模组数据。更新后作业匹配只比精英化与等级，技能模组一律按已拥有处理，可能推给你实际打不了的作业——抄之前请在 MAA 里确认。</div>
    <div class="modal-foot">
      <button class="btn-plain" data-close>取消</button>
      <button class="btn-solid" id="btn-import-go">开始更新</button>
    </div>
  `, {
    onMount(el, close) {
      $('#btn-import-go', el).addEventListener('click', async () => {
        const btn = $('#btn-import-go', el);
        btn.disabled = true;
        btn.textContent = '更新中…';
        try {
          const r = await api('/api/account/import-maa', { file: S.current });
          const st = r.stats || {};
          const warns = (r.warnings || []).map(w => `<div class="onb-hint bad">! ${esc(w)}</div>`).join('');
          close();
          modal(`
            <h3>更新完成</h3>
            <div class="sub">已用 MAA 的干员识别结果更新 ${esc(r.file || S.current)}。</div>
            <div class="path-list">
              <div class="path-item"><span>更新干员</span><code>${r.imported || 0} 名${r.skipped ? '（跳过 ' + r.skipped + ' 名）' : ''}</code></div>
              <div class="path-item"><span>数据来源</span><code>${r.source === 'yituliu' ? '一图流' : 'MAA 本地识别'}</code></div>
              <div class="path-item"><span>识别时间</span><code>${esc(fmtTime(r.syncTime))}</code></div>
              <div class="path-item"><span>练度概况</span><code>${st.charCount || 0} 干员 · 精二 ${st.e2Count || 0} · 六星 ${st.sixStarCount || 0} · 缺卡 ${st.missingCount || 0}</code></div>
            </div>
            ${warns}
            <div class="modal-foot"><button class="btn-solid" data-close>知道了</button></div>
          `);
          toast('已从 MAA 更新 ' + (r.imported || 0) + ' 名干员');
          const acc = await api('/api/accounts');
          S.accounts = acc.accounts;
          renderAccounts();
          await selectAccount(S.current, true);
        } catch (e) {
          toast('更新失败：' + e.message, 'err', 6000);
          btn.disabled = false;
          btn.textContent = '重试';
        }
      });
    }
  });
}

const CN_RARITY = { 6: '六星', 5: '五星', 4: '四星', 3: '三星', 2: '二星', 1: '一星' };

/* ------------------------------------------------------------ MAA 状态 */
function renderMaaState() {
  const el = $('#maa-state');
  const m = S.maa || {};
  const txt = $('.txt', el);
  el.classList.remove('on', 'off', 'warn');
  if (!m.exeExists) {
    el.classList.add('warn');
    txt.textContent = 'MAA 路径无效';
  } else if (m.running) {
    el.classList.add('on');
    txt.textContent = 'MAA 运行中 · ' + (m.current || '');
  } else {
    el.classList.add('off');
    txt.textContent = 'MAA 未运行 · ' + (m.current || '');
  }
  el.title = (m.maaDir || '') + '\n点击刷新状态';
}

async function refreshMaaStatus() {
  try {
    // 带上当前账号：返回的是它绑定的 MAA 的状态（没绑定就是全局默认）
    const q = S.current ? '?file=' + encodeURIComponent(S.current) : '';
    S.maa = await api('/api/maa/status' + q);
    renderMaaState();
    fillBasketProfiles();
  } catch (e) { /* 静默 */ }
}

/* ------------------------------------------------------------ 资源版本 */
const GD_CN = { chars: '干员', stages: '关卡', equips: '模组', skills: '技能', zones: '区域' };

function gdDeltaText(delta) {
  return Object.keys(delta || {})
    .filter(k => GD_CN[k] && delta[k])
    .map(k => (delta[k] > 0 ? '+' : '') + delta[k] + ' ' + GD_CN[k])
    .join(' · ');
}

function newActIds() {
  const p = (S.gamedata || {}).pending;
  return ((p && p.newActs) || []).map(a => a.id);
}

/* 资源包更新提示条：换上新资源后第一次打开就会出现，点「知道了」才消失 */
function renderUpdate() {
  const bar = $('#update-bar');
  if (!bar) return;
  const g = S.gamedata || {};
  const p = g.pending;
  if (!p) { bar.hidden = true; bar.innerHTML = ''; return; }

  const all = p.newActs || [];
  const head = all.slice(0, 4);
  const acts = head
    .map(a => esc(a.name) + (a.prefix ? `<i>${esc(a.prefix)}</i>` : '')).join('、')
    + (all.length > 4 ? ` 等 ${all.length} 个活动` : '');
  const plain = head.map(a => a.name + (a.prefix ? ' ' + a.prefix : '')).join('、')
    + (all.length > 4 ? ` 等 ${all.length} 个活动` : '');
  const delta = gdDeltaText(p.delta);
  const tip = [delta, plain ? '新活动 ' + plain : ''].filter(Boolean).join(' · ');
  bar.hidden = false;
  bar.innerHTML = `
    <span class="ub-ico">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <path d="M12 3v12"/><path d="m7.5 8.5 4.5-4.5 4.5 4.5"/><path d="M4 15v3.5A2.5 2.5 0 0 0 6.5 21h11A2.5 2.5 0 0 0 20 18.5V15"/>
      </svg>
    </span>
    <div class="ub-main">
      <b>游戏资源已更新到 ${esc(p.to)}${p.from ? `（原 ${esc(p.from)}）` : ''}</b>
      <span title="${esc(tip)}">${delta ? esc(delta) + ' · ' : ''}${acts ? '新活动 ' + acts : '关卡与干员数据已刷新'}</span>
    </div>
    <button class="ub-btn" id="ub-ack">知道了</button>`;
  const btn = $('#ub-ack', bar);
  if (btn) btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      S.gamedata = await api('/api/gamedata/ack', {});
      renderUpdate();
      renderQuick();
    } catch (e) {
      btn.disabled = false;
      toast('操作失败：' + e.message, 'err');
    }
  });
}

/* ------------------------------------------------------------ 资源包自动更新 */
/* 服务器每次同步完会把新包提交到仓库，这里启动几秒后悄悄比对一次：
   有新包就下下来直接换上（0.6 MB，很快），失败一律静默——没网也要能正常用旧包。 */
async function packAutoSync() {
  if (S.settings && S.settings.autoUpdatePack === false) return;
  try {
    const st = await api('/api/pack/status');
    S.packsync = st;
    if (!st || !st.ok || !(st.available || []).length) return;
    const r = await api('/api/pack/update', {});
    if (!r.result || !(r.result.applied || []).length) return;
    S.pack = r.pack || S.pack;
    S.gamedata = r.gamedata || S.gamedata;
    renderUpdate();
    renderQuick();
    const v = (r.result.applied.find(a => a.pack === 'arknights') || {}).version || '';
    toast(v ? `游戏资源已更新到 ${v}` : '游戏资源已更新', 'ok', 5000);
  } catch (e) { /* 静默：检查更新不该打扰用户 */ }
}

/* 设置页「检查更新」：强制查一次，有就装，返回结果文案 */
async function packCheckNow() {
  const st = await api('/api/pack/status?force=1');
  S.packsync = st;
  if (!st.ok) return { text: '检查失败：' + (st.error || '网络不可用'), ok: false };
  if (!(st.available || []).length) {
    const cur = ((st.current || {}).arknights || {});
    return { text: `已是最新（${cur.version || '?'}）`, ok: true };
  }
  const r = await api('/api/pack/update', {});
  S.pack = r.pack || S.pack;
  S.gamedata = r.gamedata || S.gamedata;
  renderUpdate();
  renderQuick();
  const applied = (r.result && r.result.applied) || [];
  const skipped = (r.result && r.result.skipped) || [];
  if (!applied.length) {
    return { text: '下载失败：' + (skipped.map(s => s.reason).join('；') || (r.result || {}).error || '未知原因'), ok: false };
  }
  const v = (applied.find(a => a.pack === 'arknights') || {}).version || '';
  return { text: `已更新${v ? '到 ' + v : ''}`, ok: true };
}

/* ------------------------------------------------------------ 程序本体更新 */
/* 海外源：GitHub Releases。启动 4 秒后查一次（后端 6 小时节流），
   有新版本只在齿轮上点个红点，绝不自动下载——
   13 MB 的包和一次重启，必须让用户自己点头。 */
async function appAutoCheck(force) {
  if (!force && S.settings && S.settings.autoCheckAppUpdate === false) return null;
  try {
    const st = await api('/api/app/status' + (force ? '?force=1' : ''));
    S.appUpdate = st;
    renderAppDot();
    // 上次「重启并安装」没生效（版本号没变）：说一次就够，不要每次启动都弹
    const li = st && st.lastInstall;
    if (li && li.state === 'stale' && !S.staleNoticed) {
      S.staleNoticed = true;
      toast(`上次更新到 ${li.to} 没有生效，可以再试一次`, 'err', 6000);
    }
    return st;
  } catch (e) {
    return null;   // 没网就静默，别打扰
  }
}

function renderAppDot() {
  const dot = $('#app-dot');
  const btn = $('#btn-settings');
  const u = S.appUpdate || {};
  const has = !!(u.ok && u.hasUpdate);
  if (dot) dot.hidden = !has;
  if (btn) btn.title = has ? `设置（新版本 ${u.tag || u.latest} 可用）` : '设置';
}

/* 设置页版本行后缀：只有查到更新才追加「→ vX.Y.Z」 */
function appVerSuffix() {
  const u = S.appUpdate || {};
  if (!(u.ok && u.hasUpdate)) return '';
  const tag = esc(u.tag || ('v' + u.latest));
  return ` <b class="app-new">→ ${tag}</b>`;
}

/* 设置页「检查更新」：强制查一次（跳过 6 小时节流），返回给按钮用的文案 */
async function appCheckNow() {
  const st = await appAutoCheck(true);
  if (!st) return { text: '检查失败：连不上 GitHub', ok: false };
  if (!st.ok) return { text: '检查失败：' + (st.error || '网络不可用'), ok: false };
  if (!st.hasUpdate) return { text: `已是最新（${st.current}）`, ok: true };
  return { text: `发现新版本 ${st.tag}`, ok: true, hasUpdate: true };
}

/* 版本弹窗：下载 → 校验 → 提示「重启并安装」→ 替换由退出后的脚本完成 */
function openUpdateDialog() {
  const u = S.appUpdate || {};
  const cur = u.current || (S.appInfo && S.appInfo.version) || '';
  const tag = u.tag || (u.latest ? 'v' + u.latest : '');
  const notes = (u.notes || '').trim().replace(/^##\s*更新内容\s*$/m, '').trim();
  const asset = u.asset || {};
  const frozen = !!u.frozen;
  const when = u.publishedAt ? String(u.publishedAt).slice(0, 10) : '';
  const size = asset.size ? (asset.size / 1048576).toFixed(1) + ' MB' : '';

  modal(`
    <h3>发现新版本 ${esc(tag)}</h3>
    <div class="sub">当前 ${esc(cur)}${when ? ` · 发布于 ${esc(when)}` : ''}${
      u.prerelease ? ' · 预发布版' : ''}${size ? ` · 安装包 ${esc(size)}` : ''}</div>
    <div class="upd-notes">${notes ? esc(notes) : (u.degraded ? '（网络受限，没取到更新说明）' : '（这个版本没有写更新说明）')}</div>
    ${u.degraded ? '<div class="onb-hint" style="margin:10px 0 0">拿不到 GitHub API（可能被网络拦了），已按发版命名直接拼出安装包地址；下载后只校验压缩包结构，没有官方 sha256。</div>' : ''}
    ${frozen ? '' : '<div class="onb-hint bad" style="margin:10px 0 0">源码模式不支持自动替换，请用「前往下载页」手动覆盖</div>'}
    <div class="modal-foot">
      <button class="btn-plain" data-close>以后再说</button>
      <button class="btn-plain" id="upd-page">前往下载页</button>
      <button class="btn-solid" id="upd-go"${
        frozen || u.ready ? '' : ' disabled'}>${
        u.ready ? '重启并安装' : '下载并安装'}</button>
    </div>
  `, {
    onMount(el) {
      $('#upd-page', el).addEventListener('click', async () => {
        try {
          const r = await api('/api/app/open', {});
          if (r && r.ok === false) toast(r.error || '打开失败', 'err');
        } catch (e) { toast('打开失败：' + e.message, 'err'); }
      });

      const go = $('#upd-go', el);
      if (!go) return;
      go.addEventListener('click', async () => {
        if (S.updateBusy) return;
        S.updateBusy = true;
        const label = go.textContent;
        try {
          // 第一次点：只下载，不重启。下完按钮变「重启并安装」，让用户再确认一次
          if (!S.appUpdate || !S.appUpdate.ready) {
            go.textContent = '下载中…';
            go.disabled = true;
            // 缓存里可能只有版本号、没有下载地址（上次检查时 API 不通）：
            // 点这一下先强制重查，拿到 asset 再下载，别让按钮白点
            if (!((S.appUpdate && S.appUpdate.asset && S.appUpdate.asset.urls) || []).length) {
              go.textContent = '获取下载地址…';
              const st = await appAutoCheck(true);
              if (st && st.ok && st.hasUpdate) {
                S.appUpdate = Object.assign({}, S.appUpdate, st);
              }
              go.textContent = '下载中…';
            }
            const r = await api('/api/app/download', {});
            if (!r || !r.ok) {
              toast((r && r.error) || '下载失败', 'err', 6000);
              go.textContent = label;
              go.disabled = false;
              return;
            }
            S.appUpdate.ready = true;
            go.textContent = '重启并安装';
            go.disabled = false;
            toast(r.degraded
              ? '安装包已下载，但没拿到官方 sha256，只校验了包结构；点「重启并安装」继续'
              : '安装包已下载并校验通过，点「重启并安装」完成替换',
            r.degraded ? 'warn' : 'ok', 6000);
            return;
          }
          // 第二次点：解压 + 拉起脚本，后端会让窗口自己关掉
          go.textContent = '正在重启…';
          go.disabled = true;
          const r = await api('/api/app/install', {});
          if (!r || !r.ok) {
            toast((r && r.error) || '安装失败', 'err', 6000);
            go.textContent = label;
            go.disabled = false;
            return;
          }
          toast('正在安装，窗口即将关闭…', 'ok', 8000);
        } catch (e) {
          toast('操作失败：' + e.message, 'err');
          go.textContent = label;
          go.disabled = false;
        } finally {
          S.updateBusy = false;
        }
      });
    },
  });
}


/* ------------------------------------------------------------ 快捷入口 */
function renderQuick() {
  const row = $('#quick-row');
  const news = newActIds();
  // 顺序由后端给：进行中的活动在最前，其次按开始时间倒序。
  // 这里不再按编号重排，否则一堆 NEW 会把当前活动挤到滚动区外面去。
  const acts = (S.activities || []);

  let html = '<span class="quick-label">快捷</span>' + paradoxChip();
  acts.slice(0, 12).forEach(a => {
    const isNew = news.indexOf(a.id) >= 0;
    html += `<button class="chip${isNew ? ' is-new' : ''}" data-q="${esc(a.id)}"
      title="${esc(a.name)}${a.prefix ? ' · ' + esc(a.prefix) : ''}（${a.count} 关）">
      <span class="chip-name">${esc(a.name || a.id)}</span>
      ${a.prefix ? `<em class="chip-code">${esc(a.prefix)}</em>` : ''}
      ${isNew ? '<i class="chip-new">NEW</i>' : ''}
    </button>`;
  });
  row.innerHTML = html;
  $$('#quick-row .chip[data-q]').forEach(c => c.addEventListener('click', () => {
    $('#search-input').value = c.dataset.q;
    doSearch();
  }));
  const px = $('#chip-paradox');
  if (px) px.addEventListener('click', openParadox);
}

/* ------------------------------------------------------------ 悖论模拟
   入口沿用「快捷」这一行：它本来就是「按活动直接搜」，悖论是同一类入口，
   放这里不用新增侧栏/顶栏元素，界面不会有异物感。
   数字角标 = 练度够开打、且没导入过的数量。 */
function paradoxChip() {
  const st = (S.detail && S.detail.paradox) || null;
  const todo = st ? (st.todo || 0) : 0;
  const tip = st
    ? `可开打 ${st.ready} 个，其中 ${todo} 个还没导入过 —— 点开按练度推算的清单`
    : '按当前账号练度推算哪些悖论模拟能开打，一键加进作业集';
  return `<button class="chip paradox" id="chip-paradox" title="${esc(tip)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><circle cx="12" cy="12" r="5"/>
      </svg>
      <span class="chip-name">悖论模拟</span>
      ${todo ? `<i class="px-badge">${todo}</i>` : ''}
    </button>`;
}

function updateParadoxChip() {
  const cur = $('#chip-paradox');
  if (!cur) { renderQuick(); return; }
  const wrap = document.createElement('div');
  wrap.innerHTML = paradoxChip();
  cur.replaceWith(wrap.firstElementChild);
  $('#chip-paradox').addEventListener('click', openParadox);
}

function pxEliteText(need) {
  return { 0: '无需精英化', 1: '精一', 2: '精二' }[need] || ('精英化 ' + need);
}

async function openParadox() {
  if (!S.current) { toast('先选一个账号', 'warn'); openAddAccount(); return; }
  const m = modal(`
    <h3>悖论模拟</h3>
    <div class="sub">按你的练度算出哪些悖论能开打，勾选想打的（默认不勾），加入后自动去作业站取作业。
      解锁条件只看对应干员的精英化，关卡内是官方固定阵容。</div>
    <div id="px-body"><div class="loading-row"><span class="spinner"></span>正在按练度推算…</div></div>`);
  m.el.querySelector('.modal').classList.add('modal-wide');
  try {
    const d = await api('/api/paradox/plan', { file: S.current });
    renderParadoxPanel(m.el, d, m.close);
  } catch (e) {
    $('#px-body', m.el).innerHTML = `<div class="empty-state"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
  }
}

function renderParadoxPanel(root, d, close) {
  const items = d.items || [], st = d.stats || {};
  const body = $('#px-body', root);
  if (!items.length) {
    body.innerHTML = '<div class="empty-state"><h3>这个账号还没有可开打的悖论</h3>'
      + '<p>悖论模拟需要先把干员精英化：三星精一、四星以上精二。</p></div>';
    return;
  }

  const ver = (d.table && d.table.gameVersion) ? String(d.table.gameVersion).slice(0, 8) : '';
  body.innerHTML = `
    <div class="px-stats">
      <span><b>${st.ready}</b> 可开打</span>
      <span><b class="mint">${st.todo}</b> 未导入</span>
      <span><b>${st.locked}</b> 练度不够</span>
      <span><b>${st.notOwned}</b> 未拥有</span>
      ${ver ? `<em class="px-ver">数据 ${esc(ver)}</em>` : ''}
    </div>
    <div class="px-tools">
      <input type="text" id="px-q" placeholder="搜干员名 / 悖论名 / mem 代号">
      <label class="px-check"><input type="checkbox" id="px-only-todo" checked><span>只看未导入</span></label>
      <button class="btn-plain mini" id="px-all">全选</button>
      <button class="btn-plain mini" id="px-none">清空</button>
    </div>
    <div class="px-list" id="px-list"></div>
    <div class="modal-foot">
      <span class="px-sel" id="px-sel">已选 0 个</span>
      <button class="btn-plain" data-close>取消</button>
      <button class="btn-solid" id="px-add">加入作业集</button>
    </div>`;

  const list = $('#px-list', root);
  const sel = $('#px-sel', root);
  const addBtn = $('#px-add', root);

  // 底栏是弹窗创建之后才注入的，modal() 当时绑不到这里的 data-close，这里补上
  $$('.modal-foot [data-close]', root).forEach(b => b.addEventListener('click', close));

  // 用「选择集」记住用户勾选的项：默认一个都不选，由用户自己挑没打过的；
  // 搜索/切换筛选重绘时勾选状态不会丢。导入过的行默认灰着（想重跑可手动勾）。
  const on = new Set();

  const visible = () => {
    const q = ($('#px-q', root).value || '').trim().toLowerCase();
    const onlyTodo = $('#px-only-todo', root).checked;
    let rows = items;
    if (onlyTodo) rows = rows.filter(r => r.ready && !r.imported);
    if (q) rows = rows.filter(r => (r.name + ' ' + r.pname + ' ' + r.stage).toLowerCase().indexOf(q) >= 0);
    return rows;
  };

  const paint = () => {
    const rows = visible();
    if (!rows.length) {
      list.innerHTML = '<div class="px-empty">没有符合条件的条目</div>';
    } else {
      list.innerHTML = rows.map(r => {
        const tag = r.ready
          ? (r.imported ? `<i class="px-tag done" title="${esc(r.importedAt)} 导入过">已导入</i>` : '<i class="px-tag">未导入</i>')
          : `<i class="px-tag lock">${esc(pxEliteText(r.needElite))}${r.needLevel > 1 ? ' Lv' + r.needLevel : ''} 可开</i>`;
        const checked = r.ready && on.has(r.stage);
        return `<label class="px-row${r.ready ? '' : ' is-locked'}">
          <input type="checkbox" data-stage="${esc(r.stage)}" ${checked ? 'checked' : ''} ${r.ready ? '' : 'disabled'}>
          <span class="px-r r${r.rarity}">${r.rarity}★</span>
          <span class="px-name">${esc(r.name)}</span>
          <span class="px-pname">${esc(r.pname)}</span>
          <span class="px-code">${esc(r.stage)}</span>
          ${tag}
        </label>`;
      }).join('');
    }
    syncSel();
  };

  const selected = () => items.filter(r => r.ready && on.has(r.stage)).map(r => r.stage);

  function syncSel() {
    const n = selected().length;
    sel.textContent = `已选 ${n} 个`;
    addBtn.disabled = n === 0;
    addBtn.textContent = n ? `加入作业集（${n}）` : '加入作业集';
  }

  list.addEventListener('change', e => {
    const box = e.target;
    if (!box || box.type !== 'checkbox' || !box.dataset.stage) return;
    if (box.checked) on.add(box.dataset.stage);
    else on.delete(box.dataset.stage);
    syncSel();
  });
  $('#px-q', root).addEventListener('input', paint);
  $('#px-only-todo', root).addEventListener('change', paint);
  // 全选/清空只作用在当前筛选可见的条目上，避免误改看不见的那一堆
  $('#px-all', root).addEventListener('click', () => {
    visible().forEach(r => { if (r.ready) on.add(r.stage); });
    paint();
  });
  $('#px-none', root).addEventListener('click', () => {
    visible().forEach(r => { if (r.ready) on.delete(r.stage); });
    paint();
  });

  addBtn.addEventListener('click', async () => {
    const stages = selected();
    if (!stages.length) return;
    addBtn.disabled = true;
    addBtn.textContent = '正在取作业…';
    try {
      const r = await api('/api/paradox/pick', { file: S.current, stages });
      const ops = [];
      (r.ok || []).forEach(x => {
        const meta = items.find(i => i.stage === x.stage) || {};
        ops.push({
          id: x.copilotId, title: `${meta.name || ''} · ${meta.pname || ''}`.trim(),
          rating: x.rating, mode: 'ready',
          stageCode: x.stage, stageId: x.stage,
          // MAA 靠 TaskList 的 Name 导航到悖论关卡，这里必须只是干员名；
          // 界面里显示的是 title / stageCode，不受影响。
          name: meta.name || '',
          difficulty: x.difficulty, difficultyText: x.difficultyText,
          maaCode: 'maa://' + x.copilotId,
        });
      });
      const added = addManyToBasket(ops);
      S.paradoxPending = (S.paradoxPending || []).concat((r.ok || []).map(x => x.stage));
      const failed = r.failed || [];
      close();
      if (added) toast(`已加入 ${added} 个悖论作业` + (failed.length ? `，${failed.length} 个没有作业` : ''));
      else toast('都没能加入，看看下面的原因', 'warn', 4000);
      if (failed.length) {
        modal(`<h3>这 ${failed.length} 个没加进去</h3>
          <div class="sub">作业站没有对应关卡的作业，只能等有人上传，或者手动打一次。</div>
          <div class="px-list">${failed.map(f => {
            const meta = items.find(i => i.stage === f.stage) || {};
            return `<div class="px-row"><span class="px-r r${meta.rarity || 0}">${meta.rarity || ''}★</span>
              <span class="px-name">${esc(meta.name || f.stage)}</span>
              <span class="px-pname">${esc(f.reason || '')}</span></div>`;
          }).join('')}</div>
          <div class="modal-foot"><button class="btn-plain" data-close>知道了</button></div>`);
      }
    } catch (e) {
      toast(e.message, 'err', 4000);
      addBtn.disabled = false;
      syncSel();
    }
  });

  paint();
}

/* ------------------------------------------------------------ 搜索 */
/* 输入停约 0.5s 自动搜索：每次键入重置计时，清空输入不搜也不弹提示 */
let searchTimer = 0;
function queueAutoSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if ($('#search-input').value.trim()) doSearch();
  }, 500);
}

/* 「下一关」按钮：搜索结果带着 next（同系列同难度的下一关）就亮、带上代号，
   系列末尾或没搜到就灰。点击 = 填入代号直接搜。 */
function setNextBtn(next) {
  const btn = $('#btn-next');
  if (!btn) return;
  if (next && next.code) {
    btn.disabled = false;
    btn.dataset.code = next.code;
    btn.textContent = '下一关 ' + next.code;
    btn.title = next.difficultyCN
      ? `跳到 ${next.code}（${next.difficultyCN}）`
      : `跳到 ${next.code}`;
  } else {
    btn.disabled = true;
    delete btn.dataset.code;
    btn.textContent = '下一关';
    btn.title = '当前已是这一系列的最后一关（或没搜到关卡）';
  }
}

async function doSearch() {
  const q = $('#search-input').value.trim();
  if (!q) { toast('先输入关卡或活动', 'warn'); return; }
  if (!S.current) { toast('先选一个账号', 'warn'); openAddAccount(); return; }
  if (S.searching) return;
  S.searching = true;
  const box = $('#results');
  box.dataset.mode = 'loading';
  box.innerHTML = `<div class="result-head"><div class="result-title">正在检索「${esc(q)}」…<span>比对干员与练度中</span></div></div>
    <div class="loading-row"><span class="spinner"></span>正在从作业站拉取作业，活动关卡多时可能要十几秒</div>
    <div class="ops-grid">${'<div class="skeleton"></div>'.repeat(6)}</div>`;
  try {
    const d = await api('/api/search', { file: S.current, q, pages: 2, limit: 10 });
    S.result = d;
    box.dataset.mode = d.mode;
    renderResult(d);
    setNextBtn(d.next);
    box.scrollTop = 0;
  } catch (e) {
    box.dataset.mode = '';
    box.innerHTML = `<div class="empty-state"><h3>没搜到</h3><p>${esc(e.message)}</p></div>`;
    setNextBtn(null);
    toast(e.message, 'err', 4000);
  } finally {
    S.searching = false;
    // 搜索进行中输入又变了（防抖那次会被上面的 S.searching 拦掉）→ 完事后补搜最新词
    if ($('#search-input').value.trim() !== q) queueAutoSearch();
  }
}

function stars(n) {
  n = Math.max(0, Math.min(10, n || 0));
  if (!n) return '<span class="no-rate">暂无评分</span>';
  const five = Math.round(n / 2);
  return `<b class="score">${n}</b>` + '★'.repeat(five) +
    '<span style="opacity:.18">' + '★'.repeat(5 - five) + '</span>';
}

const MODE_CN = { ready: '可直接抄', borrow: '差一人可借', train: '需补练度', blocked: '打不了' };

/* 难度：1 普通、2 突袭、3 普通+突袭兼容（作业里 difficulty 位标志，冗余 0/缺失 视为普通）。
   EX 关才谈得上突袭，其它关卡即使标了普通也不显眼地摆个徽标。 */
function difficultyTag(op, code) {
  const f = Number(op.difficulty) || 1;
  const c = String(code || op.stageCode || '');
  if (f === 1 && !/-EX-|_ex\d|EX-\d/i.test(c)) return '';
  const cls = f === 2 ? 'raid' : (f === 3 ? 'both' : 'normal');
  const txt = op.difficultyText || (f === 2 ? '突袭' : (f === 3 ? '通用' : '普通'));
  const tip = f === 3 ? '这份作业普通与突袭都能用' : (f === 2 ? '只适用于突袭' : '只适用于普通难度');
  return `<span class="badge diff ${cls}" title="${tip}">${esc(txt)}</span>`;
}

/* 行级难度说明（10-14 章磨难/简单、15+ 章险地、0-9 章普通/突袭） */
function diffTip(st) {
  switch (st.difficultyCN) {
    case '磨难': return '磨难难度（10-14 章第三档）。游戏里章节页要切到磨难，MAA 才会进磨难关卡';
    case '标准': return '标准难度（这一章的中间档）';
    case '简单': return '简单模式（10-14 章第一档），作业较少';
    case '险地': return '险地难度（15 章起）。写入 MAA 后会自动切到险地再开打';
    default: return st.variant === 'raid' ? '突袭难度，写入 MAA 时自动选突袭' : '普通难度';
  }
}

/* 难度徽标配色：磨难亮红、标准亮蓝、简单灰；险地/突袭沿用 raid 红 */
function diffBadgeCls(dcn, variant) {
  if (dcn === '磨难') return 'tough';
  if (dcn === '标准') return variant === 'raid' ? 'raid' : 'std';
  if (dcn === '简单') return 'normal';
  return variant === 'raid' ? 'raid' : 'normal';
}

/* 作业集里的难度标签：9 章这类「标准档的突袭」要跟标准普通行分得开 */
function diffLabel(dcn, variant) {
  if (variant === 'raid') return dcn && dcn !== '险地' ? `${dcn}·突袭` : (dcn || '突袭');
  return dcn || '';
}

function opChips(op, max = 8) {
  const list = visOpers(op.opers).slice();
  const chips = list.slice(0, max).map(o => {
    let cls = 'own';
    if (!o.owned) cls = 'lack';
    else if (!o.ok) cls = 'train';
    return `<span class="op-chip ${cls}" title="${esc(chipTitle(o))}">${esc(o.name)}</span>`;
  });
  if (list.length > max) chips.push(`<span class="op-chip more">+${list.length - max}</span>`);
  return chips.join('');
}

function chipTitle(o) {
  if (o.group) return '组合：满足任一即可';
  const e = ['精零', '精一', '精二'][o.elite] || '';
  const m = (o.mastery || []).map((x, i) => x ? `专${'一二三'[i] || ''}${x}` : '').filter(Boolean).join(' ');
  // 练度要求（v1 老作业没写的会补默认值，带 implied 标记）
  const req = o.req || {};
  const need = [];
  if (req.elite != null) need.push('精' + req.elite);
  if (req.level != null) need.push(req.level + '级');
  if (req.implied) need.push('默认');
  const needTxt = need.length ? ` · 要求 ${need.join(' ')}` : '';
  if (!o.owned) return '未拥有' + needTxt;
  return `${e} ${o.level} 级${m ? ' · ' + m : ''}${needTxt}`;
}

/* 浏览量展示：过万缩写成 x.x万 */
function fmtViews(n) {
  n = Number(n) || 0;
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + '万';
  return n ? String(n) : '';
}

/* 作业简介取第一行有信息量的文字：跳过作者署名、群号、任何带链接的推广行 */
function briefOf(op) {
  const lines = (op.details || '').split('\n').map(s => s.trim()).filter(Boolean);
  const line = lines.find(s => s.length >= 4 && !/http|qm\.qq\.com/.test(s)
    && !/^(原作者|json作者|作者|作业交流群|MAA|maa|【)/.test(s)) || '';
  return line.slice(0, 60);
}

function opCard(op, opts = {}) {
  const mode = op.mode || 'blocked';
  return `
    <div class="op-card ${opts.best ? 'best' : ''}" data-op="${op.id}" data-mode="${mode}">
      <div class="op-top">
        <span class="stars">${stars(op.rating)}</span>
        <span class="op-id">#${op.id}</span>
        <span class="op-up" title="作者：${esc(op.uploader || '佚名')}">@${esc(op.uploader || '佚名')}</span>
      </div>
      <div class="op-title" title="${esc(op.title)}">${esc(op.title)}</div>
      <div class="badges">
        <span class="badge ${mode}">${MODE_CN[mode]}</span>
        ${difficultyTag(op, op.stageCode)}
        <span class="badge plain">${op.total} 人</span>
        ${(() => { const miss = visNames(op.missing); return miss.length ? `<span class="badge blocked">缺 ${esc(miss.join('、'))}</span>` : ''; })()}
        ${(() => { const train = visNames(op.training); return train.length ? `<span class="badge train">练度不足 ${esc(train.slice(0, 2).join('、'))}</span>` : ''; })()}
      </div>
      <div class="opers">${opChips(op)}</div>
      <div class="op-foot">
        <div class="op-meta">${op.views ? '浏览 ' + op.views : ''}</div>
        <div class="op-btns">
          <button class="mini-btn" data-act="detail">详情</button>
          <button class="mini-btn" data-act="copy">复制代码</button>
          <button class="mini-btn accent" data-act="add" data-add-id="${op.id}" data-add-variant="${esc(opts.variant || '')}" data-add-label="加入" data-added-label="已在作业集" ${opts.inBasket ? 'disabled' : ''}>${opts.inBasket ? '已在作业集' : '加入'}</button>
        </div>
      </div>
    </div>`;
}

function bindOpCards(root, ops, variant) {
  $$('.op-card', root).forEach(card => {
    const id = Number(card.dataset.op);
    const op = ops.find(o => Number(o.id) === id);
    if (!op) return;
    card.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) { showOpDetail(op, variant); return; }
      const act = btn.dataset.act;
      if (act === 'copy') copyText(op.maaCode, 'MAA 代码已复制');
      else if (act === 'add') addToBasket(op, variant);   // 按钮状态由 renderBasket 统一回刷
      else if (act === 'detail') showOpDetail(op, variant);
    });
  });
}

function renderResult(d) {
  // 顺手学作者 id → 昵称：作者黑名单弹窗显示名字靠这个缓存
  (d.stages || []).forEach(s => {
    [s.best].concat(s.ops || []).forEach(op => {
      if (op && op.uploaderId && op.uploader) S.authorNames[op.uploaderId] = op.uploader;
    });
  });
  const box = $('#results');
  if (!d.stages || !d.stages.length) {
    box.innerHTML = '<div class="empty-state"><h3>没有结果</h3><p>换个关卡代号试试，例如 TL-10 或 act55side</p></div>';
    return;
  }

  if (d.stages.length === 1) {
    const s = d.stages[0];
    const ops = s.ops || [];
    const dcn = s.stage.difficultyCN || '';
    const dBadge = dcn
      ? ` <em class="badge diff ${diffBadgeCls(dcn, s.stage.variant)}" title="${esc(diffTip(s.stage))}">${esc(diffLabel(dcn, s.stage.variant))}</em>`
      : '';
    const head = `
      <div class="result-head">
        <div class="result-title">${esc(s.stage.code)} ${esc(s.stage.name)}${dBadge}
          ${s.stage.activity ? `<em class="result-act">${esc(s.stage.activity)}</em>` : ''}
          <span>${s.total} 份作业 · 可直接抄 ${s.counts.ready} · 差一人 ${s.counts.borrow} · 需练度 ${s.counts.train}</span>
        </div>
        <div class="result-actions">
          ${s.best ? `<button class="ghost-btn mint" id="add-best" data-add-id="${s.best.id}" data-add-variant="${esc(s.stage.variant || '')}" data-add-label="加入推荐作业" data-added-label="已加入">加入推荐作业</button>` : ''}
        </div>
      </div>`;
    if (!ops.length) {
      box.innerHTML = head + '<div class="empty-state"><h3>这个关卡还没有作业</h3><p>作业站上暂时没人上传，换个关卡吧</p></div>';
      return;
    }
    box.innerHTML = head + '<div class="ops-grid">' +
      ops.map((op, i) => opCard(op, {
        best: i === 0 && op.mode !== 'blocked' && op.mode !== 'train',
        inBasket: inBasket(op.id, s.stage.variant),
        variant: s.stage.variant,
      })).join('') + '</div>';
    bindOpCards(box, ops, s.stage.variant);
    const ab = $('#add-best');
    if (ab && s.best) ab.addEventListener('click', () => addToBasket(s.best, s.stage.variant));
    return;
  }

  // 活动视图（有突袭版的关卡会拆成「普通」「突袭」两行，通用作业两边都在）
  const codes = [...new Set(d.stages.map(s => s.stage.code))];
  const opIds = new Set();
  d.stages.forEach(s => (s.ops || []).forEach(o => opIds.add(o.id)));
  const totalOps = opIds.size;
  const readyStages = codes.filter(code => d.stages.some(s =>
    s.stage.code === code && s.best && s.best.mode === 'ready')).length;
  const raidRows = d.stages.filter(s => s.stage.variant === 'raid').length;
  const head = `
    <div class="result-head">
      <div class="result-title">${esc(d.title)}
        <span>${codes.length} 关 · ${totalOps} 份作业 · ${readyStages} 关可直接抄${raidRows ? ` · ${raidRows} 个突袭/险地行` : ''}</span>
      </div>
      <div class="result-actions">
        <button class="ghost-btn mint" id="add-all-best" title="每关只加真正要打的难度：0-8/9 章普通+突袭，10-14 章只加磨难，15 章起只加险地（简单不加；某档没作业就跳过不降档）">全部加入（每关要打的档）</button>
      </div>
    </div>`;
  const rows = d.stages.map(s => {
    const b = s.best;
    const st = s.stage;
    const cls = b ? b.mode : 'blocked';
    const has = (s.ops || []).length;
    const isRaid = st.variant === 'raid';
    // 难度行徽标配色见 diffBadgeCls；文本见 diffLabel（「标准·突袭」这类）
    const dcn = st.difficultyCN || '';
    const vCls = diffBadgeCls(dcn, st.variant);
    const vTxt = diffLabel(dcn, st.variant) || (isRaid ? '突袭' : '普通');
    const variant = st.variant
      ? `<em class="badge diff ${vCls}" title="${esc(diffTip(st))}">${vTxt}</em>`
      : '';
    return `
      <div class="stage-row${isRaid ? ' is-raid' : ''}" data-stage="${esc(st.stageId)}" data-variant="${esc(st.variant || '')}">
        <div class="stage-main">
          <div class="stage-code">${esc(st.code)}<small class="sc-sub">${esc(st.name || '')}${variant}</small></div>
          <div class="stage-mid">
            <div class="stage-best-line">
              <div class="stage-best">${b ? esc(b.title) : '（暂无可用作业）'}</div>
              ${b ? `<div class="best-meta"><b>@${esc(b.uploader || '佚名')}</b>${briefOf(b) ? ' · ' + esc(briefOf(b)) : ''}</div>` : ''}
            </div>
            <div class="stage-sub">
              ${b ? `<span class="stars">${stars(b.rating)}</span><span>${b.total} 人</span><span>浏览 ${fmtViews(b.views) || 0}</span>` : ''}
              <span>共 ${s.total} 份</span>
              ${s.counts.ready ? `<span style="color:var(--mint)">可抄 ${s.counts.ready}</span>` : ''}
              ${s.counts.borrow ? `<span style="color:var(--indigo)">借人 ${s.counts.borrow}</span>` : ''}
              ${s.counts.train ? `<span style="color:var(--amber)">需练度 ${s.counts.train}</span>` : ''}
            </div>
          </div>
          <div class="stage-right">
            <span class="badge ${cls}">${MODE_CN[cls]}</span>
            ${b ? `<button class="mini-btn accent" data-add-id="${b.id}" data-add-variant="${esc(st.variant || '')}" data-stage="${esc(st.stageId)}" data-add-label="加入" data-added-label="已在" ${inBasket(b.id, st.variant) ? 'disabled' : ''}>${inBasket(b.id, st.variant) ? '已在' : '加入'}</button>` : ''}
          </div>
        </div>
        <div class="stage-ops">
          ${(s.ops || []).slice(0, 6).map(op => `
            <div class="row-op" data-op="${op.id}">
              <span class="stars" style="width:112px">${stars(op.rating)}</span>
              <span class="badge ${op.mode}">${MODE_CN[op.mode]}</span>
              ${difficultyTag(op, st.code)}
              <span class="t" title="${esc(op.title)}">${esc(op.title)}</span>
              <span class="up" title="作者：${esc(op.uploader || '佚名')}">@${esc(op.uploader || '佚名')}</span>
              <span class="vw">浏览 ${fmtViews(op.views) || 0}</span>
              <span style="font-size:11px;color:var(--text-3)">${op.total} 人</span>
              <button class="mini-btn" data-copy="${esc(op.maaCode)}">复制</button>
              <button class="mini-btn accent" data-add-id="${op.id}" data-add-variant="${esc(st.variant || '')}" data-stage="${esc(st.stageId)}" data-add-label="加入" data-added-label="已在" ${inBasket(op.id, st.variant) ? 'disabled' : ''}>${inBasket(op.id, st.variant) ? '已在' : '加入'}</button>
            </div>`).join('') || `<div style="font-size:12px;color:var(--text-3)">没有${dcn || (isRaid ? '突袭' : '')}作业</div>`}
        </div>
      </div>`;
  }).join('');

  box.innerHTML = head + '<div class="stage-list">' + rows + '</div>';

  const allOps = [];
  d.stages.forEach(s => (s.ops || []).forEach(o => allOps.push(o)));

  $$('.stage-row', box).forEach(row => {
    $('.stage-main', row).addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      row.classList.toggle('open');
    });
  });
  $$('[data-add-id]', box).forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const op = allOps.find(o => Number(o.id) === Number(b.dataset.addId));
    if (!op) return;
    // 加入后由 renderBasket → syncAddButtons 回刷；同一份作业在普通/突袭
    // 两行各有一组按钮，回刷时按各自的 variant 判断，不会串行
    addToBasket(op, b.dataset.addVariant || '');
  }));
  $$('[data-copy]', box).forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    copyText(b.dataset.copy, 'MAA 代码已复制');
  }));
  $$('.row-op .t', box).forEach(t => t.addEventListener('click', () => {
    const row = t.closest('.row-op');
    const op = allOps.find(o => Number(o.id) === Number(row.dataset.op));
    const vr = t.closest('.stage-row');
    if (op) showOpDetail(op, (vr && vr.dataset.variant) || '');
  }));
  const all = $('#add-all-best');
  if (all) all.addEventListener('click', () => {
    let n = 0;
    const seen = new Set();
    // 一关真正要打的难度（用户口径）：0-8 普通+突袭、9 标准+突袭、
    // 10-14 只打磨难、15+ 只打险地。简单永远不打；同一关出现磨难/险地行
    // 时普通/标准行就不算「要打的档」了（不擅自降档：磨难行没作业就空着）
    const hasHardRow = {};
    d.stages.forEach(s => {
      if (s.stage.difficulty === 'tough' || s.stage.difficulty === 'risky')
        hasHardRow[s.stage.code] = true;
    });
    d.stages.forEach(s => {
      if (!s.best) return;
      if (s.stage.difficulty === 'easy') return;
      if (s.stage.difficulty === 'normal' && hasHardRow[s.stage.code]) return;
      // 按「关卡 + 难度行」去重：同一关同一档只加一份
      const key = s.stage.stageId + '|' + (s.stage.variant || '');
      if (seen.has(key) || inBasket(s.best.id, s.stage.variant || undefined)) return;
      seen.add(key);
      const dcn = s.best.difficultyCN || '';
      const dlab = diffLabel(dcn, s.stage.variant);
      S.basket.push({
        id: s.best.id, copilotId: s.best.id, title: s.best.title, rating: s.best.rating,
        mode: s.best.mode, stageCode: s.best.stageCode || '', stageId: s.best.stageId || '',
        name: dlab ? `${s.best.stageCode || ''}·${dlab}` : '',
        maaCode: s.best.maaCode,
        difficulty: Number(s.best.difficulty) || 1,
        difficultyText: s.best.difficultyText || '普通',
        difficultyCN: dcn,
        variant: s.stage.variant || '',
      });
      n++;
    });
    renderBasket();
    toast(n ? `已加入 ${n} 份作业` : '这些作业都已在作业集里', n ? 'ok' : 'warn');
  });
}

/* ------------------------------------------------------------ 作业详情 */
async function showOpDetail(op, variant) {
  // 作业黑名单条目就是 MAA 代码，直接比对；作者黑名单按作业站数字 id 屏蔽，
  // 老作业没有 uploader_id 就不给拉黑作者入口
  const opBL = S.settings.opBlacklist || [];
  const opBanned = opBL.includes(op.maaCode);
  const banned = op.uploaderId && (S.settings.authorBlacklist || []).some(a => String(a.id) === op.uploaderId);
  const m = modal(`
    <h3>${esc(op.title)}</h3>
    <div class="sub">作业 #${op.id} · ${esc(op.stageCode || '')} ${esc(op.stageName || '')} · ${esc(op.difficultyText || '普通')} · ${op.total} 人 · 评分 ${op.rating}</div>
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="btn-plain" id="copy-code">复制 ${esc(op.maaCode)}</button>
      <button class="btn-solid" id="add-op">加入作业集</button>
      <button class="btn-plain" id="ban-op"${opBanned ? ' disabled title="已在黑名单里，可在设置 → 作业黑名单移除"' : ''}>${opBanned ? '已拉黑作业' : '拉黑作业'}</button>
      ${op.uploaderId ? `<button class="btn-plain" id="ban-author"${banned ? ' disabled title="已在黑名单里，可在设置 → 作者黑名单移除"' : ''}>${banned ? '已拉黑作者' : '拉黑作者'}</button>` : ''}
    </div>
    <label class="field"><span>干员与要求</span></label>
    <div id="op-detail-body" style="font-size:12px;color:var(--text-2);line-height:1.9">
      <div class="loading-row"><span class="spinner"></span>拉取作业详情…</div>
    </div>
  `, {
    onMount(el, close) {
      $('#copy-code', el).addEventListener('click', () => copyText(op.maaCode, 'MAA 代码已复制'));
      $('#add-op', el).addEventListener('click', () => { addToBasket(op, variant || ''); close(); });
      /* 两个拉黑共用一套动作：不关弹窗（可以把作业和作者一起拉），按钮原地
         翻成「已拉黑」态；过滤在后端，改完重搜一次当前结果让它立即生效 */
      const banFlow = async (btn, key, entry, doneText, tip) => {
        const next = (S.settings[key] || []).concat([entry]);
        const old = btn.textContent;
        btn.disabled = true;   // 请求在途防重复点
        try {
          const d = await api('/api/settings', { [key]: next });
          S.settings = d.settings;
          btn.textContent = doneText;
          btn.title = tip;
          toast(`${doneText}，可在设置里移除`, 'ok');
          if (S.result && $('#search-input').value.trim()) doSearch();
        } catch (e) {
          btn.disabled = false;
          btn.textContent = old;
          toast('保存失败：' + e.message, 'err');
        }
      };
      $('#ban-op', el).addEventListener('click', () => {
        if ((S.settings.opBlacklist || []).includes(op.maaCode)) return;
        banFlow($('#ban-op', el), 'opBlacklist', op.maaCode, '已拉黑作业', '已在黑名单里，可在设置 → 作业黑名单移除');
      });
      const banBtn = $('#ban-author', el);
      if (banBtn) banBtn.addEventListener('click', () => {
        const id = op.uploaderId;
        if (!id || !(S.settings.authorBlacklist || []).every(a => String(a.id) !== id)) return;
        if (op.uploader) S.authorNames[id] = op.uploader;   // 顺手学 id → 昵称，黑名单弹窗显示用
        banFlow(banBtn, 'authorBlacklist', { id, name: op.uploader || '' }, '已拉黑作者', '已在黑名单里，可在设置 → 作者黑名单移除');
      });
      // 带上 file：MAA 导入的账号没有技能/模组数据，详情里不展示这类要求
      const opUrl = '/api/op?id=' + op.id + (S.current ? '&file=' + encodeURIComponent(S.current) : '');
      api(opUrl).then(d => {
        const body = d.content || {};
        const shown = visOpers(d.opers);
        const rows = shown.map(o => {
          const req = o.requirements || {};
          const bits = [];
          if (req.elite != null) bits.push('精' + req.elite);
          if (req.level != null) bits.push(req.level + ' 级');
          if (req.skill_level != null) bits.push(req.skill_level >= 8 ? ('专' + (req.skill_level - 7) + ' 技能' + (req.skill || 1)) : ('技能 ' + req.skill_level));
          if (req.module) bits.push('模组 ' + req.module);
          if (req.implied) bits.push('老作业未标注，按星级默认');
          return `<div style="display:flex;justify-content:space-between;gap:12px;border-bottom:1px dashed rgba(255,255,255,.06);padding:5px 0">
            <span style="color:var(--text)">${esc(o.name)}</span>
            <span style="color:var(--text-3);font-size:11.5px">${esc(bits.join(' · ') || '无特殊要求')}</span>
          </div>`;
        }).join('');
        const skillless = shown.some(o => o.skillKnown === false);
        $('#op-detail-body', el).innerHTML =
          (rows || '<div style="color:var(--text-3)">无干员信息</div>') +
          (skillless
            ? '<div class="onb-hint bad" style="margin-top:10px">! 该账号用 MAA 干员识别建号，没有技能 / 专精 / 模组数据：判定只比精英化与等级，技能模组按已拥有处理，实际打之前请在 MAA 里确认。</div>'
            : '') +
          `<div style="margin-top:12px;font-size:11.5px;color:var(--text-3)">
             动作 ${d.actions} 条 · 关卡 ${esc(d.stageName || '')}
             <br>MAA 代码：<code style="color:var(--mint)">${esc(d.maaCode)}</code>
           </div>`;
      }).catch(e => {
        $('#op-detail-body', el).innerHTML = '<span style="color:var(--rose)">' + esc(e.message) + '</span>';
      });
    }
  });
  return m;
}

/* ------------------------------------------------------------ 作业集 */
/* variant：'normal' / 'raid' 表示从哪一行加的（只写对应那条）；不传表示不区分 */
function inBasket(id, variant) {
  return S.basket.some(o => Number(o.id) === Number(id)
    && (variant === undefined || (o.variant || '') === (variant || '')));
}

/* 「加入」按钮的状态以 S.basket 为唯一来源，统一回刷。
   以前只在点击那一刻单向置灰：从作业集删掉一份作业后，原来那一关的
   按钮还停在「已在」，点不动也加不回去。
   不给 variant 时按「任意难度行已加入」判断（单关卡页没有分行的概念）。 */
function syncAddButtons(root) {
  $$('[data-add-id]', root || document).forEach(b => {
    const v = b.dataset.addVariant || '';
    const on = v ? inBasket(b.dataset.addId, v) : inBasket(b.dataset.addId);
    b.disabled = on;
    b.textContent = on ? (b.dataset.addedLabel || '已在') : (b.dataset.addLabel || '加入');
  });
}

function addToBasket(op, variant, quiet) {
  const v = variant || op.variant || '';
  if (inBasket(op.id, v || undefined)) {
    if (!quiet) toast(v === 'raid' ? '这份作业已经在作业集的突袭里了' : (v === 'normal' ? '这份作业已经在作业集的普通里了' : '已经在作业集里了'), 'warn');
    return false;
  }
  const dcn = op.difficultyCN || '';
  S.basket.push({
    id: op.id, copilotId: op.id, title: op.title, rating: op.rating,
    mode: op.mode, stageCode: op.stageCode || '', stageId: op.stageId || '',
    // 磨难/险地/简单行在 MAA 列表里的标签带上难度，同一代号三行也能分清
    name: dcn ? `${op.stageCode || ''}·${dcn}` : (op.name || ''),
    maaCode: op.maaCode,
    difficulty: Number(op.difficulty) || 1,
    difficultyText: op.difficultyText || '普通',
    difficultyCN: dcn,
    variant: v,
  });
  if (quiet) return true;
  renderBasket();
  const fab = $('#basket-fab');
  if (fab) { fab.classList.remove('pulse'); void fab.offsetWidth; fab.classList.add('pulse'); }
  toast(v === 'raid' ? `已加入作业集${dcn ? '（' + dcn + '）' : '（突袭）'}` : '已加入作业集');
  return true;
}

/* 批量加入（悖论模拟一次可能几十上百条）：不逐条弹提示，也不逐条重绘 */
function addManyToBasket(ops) {
  let n = 0;
  (ops || []).forEach(op => { if (addToBasket(op, op.variant || '', true)) n++; });
  if (!n) return 0;
  renderBasket();
  const fab = $('#basket-fab');
  if (fab) { fab.classList.remove('pulse'); void fab.offsetWidth; fab.classList.add('pulse'); }
  return n;
}

function basketDiffTag(o) {
  // 磨难/险地/简单/标准行：难度就是行的身份，直接标难度（配色/文本见 diffBadgeCls、diffLabel）
  if (o.difficultyCN) {
    return `<i class="diff-pill ${diffBadgeCls(o.difficultyCN, o.variant)}">${esc(diffLabel(o.difficultyCN, o.variant))}</i>`;
  }
  if (o.variant === 'raid') return '<i class="diff-pill raid">突袭</i>';
  if (o.variant === 'normal') return '<i class="diff-pill normal">普通</i>';
  const f = Number(o.difficulty) || 1;
  if (!(f & 2)) return '';
  const txt = f & 1 ? '普通+突袭' : '突袭';
  return `<i class="diff-pill ${f & 1 ? 'both' : 'raid'}">${txt}</i>`;
}

/* 实际写进 MAA 的条数：带 variant 的只写一条，其余按难度标志展开 */
function basketEntryCount(o) {
  if (o.variant === 'raid' || o.variant === 'normal') return 1;
  const f = Number(o.difficulty) || 1;
  return [1, 2].filter(b => f & b).length;
}

function renderBasket() {
  const list = $('#basket-list');
  $('#basket-count').textContent = S.basket.length;
  const entries = S.basket.reduce((n, o) => n + basketEntryCount(o), 0);
  const applyBtn = $('#btn-apply');
  applyBtn.disabled = S.basket.length === 0;
  applyBtn.textContent = entries ? `写入 MAA（${entries} 条）` : '写入 MAA 并重启';
  $('#basket-empty').style.display = S.basket.length ? 'none' : 'flex';
  const fab = $('#fab-count');
  if (fab) {
    fab.textContent = S.basket.length;
    fab.hidden = S.basket.length === 0;
  }
  list.innerHTML = S.basket.map((o, i) => `
    <div class="basket-item" draggable="true" data-idx="${i}">
      <span class="drag-dots" title="拖动调整顺序">⠿</span>
      <span class="basket-idx">${i + 1}</span>
      <span class="basket-code">${esc(o.stageCode || o.id)}${basketDiffTag(o)}</span>
      <span class="basket-name" title="${esc(o.title)}">${esc(o.title)}</span>
      <button class="icon-btn small" data-del="${i}" title="移除">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
      </button>
    </div>`).join('');
  $$('[data-del]', list).forEach(b => b.addEventListener('click', () => {
    S.basket.splice(Number(b.dataset.del), 1);
    renderBasket();
  }));
  bindBasketDrag(list);
  // 作业集是所有「加入」按钮的唯一真源：移除 / 清空 / 批量加入之后
  // 都要把外面那些按钮的状态拉回来
  syncAddButtons();
}

/* 拖动排序：直接挪 DOM，松手后再把 S.basket 按 DOM 顺序重排 */
let _dragEl = null;

function bindBasketDrag(list) {
  $$('.basket-item', list).forEach(el => {
    el.addEventListener('dragstart', e => {
      _dragEl = el;
      el.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', el.dataset.idx); } catch (_) { }
    });
    el.addEventListener('dragend', () => {
      el.classList.remove('dragging');
      _dragEl = null;
      syncBasketOrder();
    });
    el.addEventListener('dragover', e => {
      if (!_dragEl || _dragEl === el) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const r = el.getBoundingClientRect();
      const after = (e.clientY - r.top) > r.height / 2;
      list.insertBefore(_dragEl, after ? el.nextSibling : el);
    });
  });
}

function syncBasketOrder() {
  // 用 DOM 里各块的原始序号还原新顺序：同一份作业可能加两次（普通/突袭），不能用 id
  const order = $$('#basket-list .basket-item').map(el => Number(el.dataset.idx));
  if (!order.length || order.length !== S.basket.length) return;
  const next = order.map(i => S.basket[i]).filter(Boolean);
  if (next.length !== S.basket.length) return;
  const changed = next.some((o, i) => o !== S.basket[i]);
  S.basket = next;
  if (changed) setTimeout(renderBasket, 0);   // 重排后刷新序号
}

function fillBasketProfiles() {
  const sel = $('#basket-profile');
  const list = (S.maa && S.maa.profiles) || [];
  // 列表没变就不重建：这个下拉每 30 秒被 refreshMaaStatus 重建一次，
  // 无条件 innerHTML 会把用户选中的档案弹回第一项，之后点「写入 MAA」
  // 就写进第一个档案的配置里（#51）
  const html = list.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('')
    || '<option value="">（无配置）</option>';
  if (sel.dataset.list === html) return;
  const keep = sel.value;
  sel.innerHTML = html;
  sel.dataset.list = html;
  // 列表变了也尽量保住选中项（保不住再默认选第一个）
  if (keep && list.includes(keep)) sel.value = keep;
}

/* ------------------------------------------------------------ 写入 MAA */
async function applyToMaa() {
  if (!S.basket.length) return;
  const btn = $('#btn-apply');
  btn.disabled = true;
  const items = S.basket.map(o => ({
    copilotId: o.copilotId, stageCode: o.stageCode, title: o.title,
    name: o.name || '', variant: o.variant || '',
  }));
  const payload = {
    items,
    file: S.current || '',
    profile: $('#basket-profile').value,
    mode: $('#basket-mode').value,
    autoStart: $('#basket-autostart').checked,
  };

  const m = modal(`
    <h3>正在写入 MAA</h3>
    <div class="sub">关闭 MAA → 备份配置 → 下载 ${items.length} 份作业 → 写回作业列表${payload.autoStart ? ' → 重启 MAA' : ''}</div>
    <div class="log-box" id="task-log"></div>
    <div class="modal-foot"><button class="btn-plain" data-close>后台执行</button></div>
  `);

  try {
    const { taskId } = await api('/api/maa/apply', payload);
    await pollTask(taskId, m.el);
    // 写完自动收弹窗：日志末尾的「✓ 完成」给读者留一眼的工夫；
    // 之前这里不关，弹窗会一直挂着等用户手点「后台执行」
    setTimeout(() => m.close(), 900);
    const px = S.paradoxPending || [];
    S.basket = [];
    renderBasket();
    toggleBasket(false);          // 窄屏写完顺手收起抽屉
    refreshMaaStatus();
    if (S.result) doSearch();
    // 悖论：写完才记「已导入」，下次打开面板默认不勾
    if (px.length) {
      S.paradoxPending = [];
      api('/api/paradox/mark', { file: S.current, stages: px })
        .then(() => api('/api/paradox/plan', { file: S.current }))
        .then(p => { if (S.detail) S.detail.paradox = p.stats; updateParadoxChip(); })
        .catch(() => { });
    }
  } catch (e) {
    toast('写入失败：' + e.message, 'err', 5000);
    btn.disabled = false;
  }
}

function pollTask(tid, root) {
  return new Promise((resolve, reject) => {
    let since = 0;
    let done = false;
    const box = root ? $('#task-log', root) : null;
    const tick = async () => {
      if (done) return;
      try {
        const t = await api('/api/task?id=' + tid + '&since=' + since);
        since = t.logCount || since;
        if (box && t.logs && t.logs.length) {
          t.logs.forEach(line => {
            const div = document.createElement('div');
            const bad = /失败|错误|error|Traceback|✗|退出码/.test(line);
            div.className = bad ? 'err' : '';
            div.textContent = line;
            box.appendChild(div);
          });
          box.scrollTop = box.scrollHeight;
        }
        if (t.status === 'done') {
          done = true;
          if (box) {
            const ok = document.createElement('div');
            ok.className = 'ok';
            ok.textContent = '✓ 完成';
            box.appendChild(ok);
          }
          resolve(t.result);
          return;
        }
        if (t.status === 'error') {
          done = true;
          if (box) {
            const er = document.createElement('div');
            er.className = 'err';
            er.textContent = '✗ ' + (t.error || '失败');
            box.appendChild(er);
          }
          reject(new Error(t.error || '任务失败'));
          return;
        }
        setTimeout(tick, 700);
      } catch (e) {
        done = true;
        reject(e);
      }
    };
    tick();
  });
}

/* 向向导的任务日志框补一行提示（pollTask 只写后台任务日志，
   同步之外的动作——绑定 MAA、导入干员信息——的结果写这里） */
function taskLogLine(box, text, bad) {
  if (!box) return;
  const div = document.createElement('div');
  if (bad) div.className = 'err';
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------ 添加账号 */
const TOKEN_GUIDE = `
  <b>三步拿到 token</b>
  <ol>
    <li>浏览器打开<button class="link-btn" data-url="https://www.skland.com/">森空岛社区</button>并登录自己的账号</li>
    <li>按 <code>F12</code> 打开开发者工具，切到 <b>Network</b> 后刷新页面</li>
    <li>点开任意一个发往 <code>zonai.skland.com</code> 的请求，请求头里找 <code>token</code>，整段复制</li>
  </ol>
  <span>token 就是账号凭证，加密保存在本机的账号文件里（Windows DPAPI，仅本机可解），不会上传。失效了重新取一次即可。</span>`;

/* ------------------------------------------------------------ 账号向导（首次引导 / 添加账号共用）
   两个入口是同一套逻辑，共用这一个两页向导组件：
     第 1 页：选 MAA 根目录——已保存的 / 运行中新发现的（排除已保存）/
              手动填写 + 检测；检测同时探测练度数据（data/OperBoxData.json）。
     第 2 页：二选一——
       方式一 森空岛 token：内置登录 / 手动粘贴，一行一个可批量导入
              （逐个同步、失败单独重试），成功后自动从 MAA 导入练度；
       方式二 直接从选定的 MAA 导入：完全不用 token，用识别结果直接
              建本地账号（识别数据不含技能/专精/模组）。
   mode='onboard'（首次引导）：多「稍后设置」，成功后把目录存为全局默认；
   mode='add'（日常添加）：标题文案不同，其余一致。
   MAA 不去重——多个账号可以共用一个，也可以各配各的。 */
function openAccountWizard(mode) {
  const onboard = mode === 'onboard';
  const p = S.paths || {};
  const saved = (S.settings.maaDirs || []).filter(d => d && typeof d === 'string');
  const savedKeys = new Set(saved.map(d => d.replace(/[\\/]+$/, '').toLowerCase()));
  const m = document.createElement('div');
  m.className = 'onboard';
  m.innerHTML = `
    <div class="onb-card">
      <div class="onb-head">
        <div class="brand-mark">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5z"/><path d="M8.5 13.5 12 16l3.5-2.5"/>
          </svg>
        </div>
        <div>
          <h2>${onboard ? '两步就能开始' : '添加账号'}</h2>
          <p>${onboard
            ? '先指一下 MAA 目录，再选：用森空岛 token 完整同步，或直接用 MAA 的干员识别结果。'
            : '先给这个账号指定用哪个 MAA，再选：森空岛 token，或直接用 MAA 的干员识别结果。'}</p>
        </div>
      </div>

      <div class="onb-page" data-page="1">
        <div class="onb-step">
          <div class="onb-idx">1</div>
          <div class="onb-body">
            <h3>${onboard ? 'MAA 根目录' : '这个账号用哪个 MAA'}</h3>
            <p>${onboard
              ? '包含 <code>MAA.exe</code> 的那个文件夹，用来写入多个作业并重启 MAA。'
              : '多个账号可以共用同一个 MAA；要分开养号就给这个账号单独配一个。'}</p>
            <div class="onb-box">
              <div class="onb-box-head"><span>已保存的 MAA</span></div>
              <div id="aa-saved" class="onb-list">${
                saved.length ? '' : '<span class="onb-hint">还没有保存过的 MAA 路径。</span>'
              }</div>
            </div>
            <div class="onb-box">
              <div class="onb-box-head">
                <span>正在运行的 MAA（新发现）</span>
                <button class="btn-plain mini" id="aa-procs-refresh">刷新</button>
              </div>
              <div id="aa-procs" class="onb-list"><span class="onb-hint">检测中…</span></div>
            </div>
            <div class="onb-manual">
              <h4>自行选择 MAA 路径</h4>
              <div class="path-row">
                <input type="text" id="aa-maa" placeholder="例如 D:\\MAA\\MAA-v6.20.1-win-x64">
                <button class="btn-plain mini" id="aa-pick">浏览</button>
                <button class="btn-plain mini" id="aa-probe">检测</button>
              </div>
              <div class="onb-hint" id="aa-maa-state">从上面列表选一个，或手动填写后点「检测」；通过后才能进下一步。</div>
            </div>
          </div>
        </div>
      </div>

      <div class="onb-page" data-page="2" hidden>
        <div class="onb-step">
          <div class="onb-idx">2</div>
          <div class="onb-body">
            <h3>怎么添加这个账号</h3>
            <p>两条路：拿森空岛 token 做完整同步（账号信息 + 练度），或者不用 token、直接用第 1 步 MAA 的干员识别结果。</p>

            <div class="onb-box">
              <div class="onb-box-head"><span>方式一：森空岛 token（完整同步）</span></div>
              <div class="onb-login">
                <div class="onb-login-main">
                  <div>
                    <b>在内置窗口登录（推荐）</b>
                    <span>打开森空岛官网的登录页，登录成功后自动抓取 token 填到下方，不用自己翻 F12。</span>
                  </div>
                  <button class="btn-solid mini" id="onb-login-btn">打开登录窗口</button>
                </div>
                <div class="onb-hint" id="onb-login-state">小窗会要求重新登录一次森空岛——token 只在登录的瞬间出现，抓到就自动验证并填到下方。</div>
              </div>
              <div class="onb-manual">
                <h4>自行输入森空岛 token</h4>
                <textarea id="onb-token" rows="3" spellcheck="false"
                  placeholder="一行一个 token（回车换行），可一次粘贴多个账号"></textarea>
                <div class="path-row">
                  <input type="text" id="onb-alias" placeholder="备注（可选，例如：大号）">
                  <button class="btn-plain mini" id="onb-token-help">怎么拿 token</button>
                </div>
                <div class="onb-hint token-guide" id="onb-token-guide" hidden>${TOKEN_GUIDE}</div>
              </div>
            </div>

            <div class="onb-box">
              <div class="onb-box-head"><span>方式二：直接从选定的 MAA 导入（不用 token）</span></div>
              <div class="onb-manual">
                <p>用第 1 步选定 MAA 的干员识别结果直接建号。本地识别不含技能、专精、模组数据；之后想补全，随时再用方式一同步一次。</p>
                <div class="path-row">
                  <button class="btn-solid mini" id="aa-maa-go" disabled>用这个 MAA 直接添加</button>
                  <span class="onb-hint" id="aa-maa-hint">在第 1 步检测 MAA 后，这里会显示练度数据概况。</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="log-box" id="task-log" style="display:none"></div>

      <div class="onb-foot">
        <span class="onb-note">数据目录 <code>${esc(p.accounts || 'config/')}</code></span>
        <div class="onb-btns">
          <button class="btn-plain" id="${onboard ? 'onb-skip' : 'aa-cancel'}">${onboard ? '稍后设置' : '取消'}</button>
          <button class="btn-plain" id="aa-back" hidden>上一步</button>
          <button class="btn-solid" id="aa-next">下一步</button>
          <button class="btn-solid" id="aa-go" hidden>保存并同步账号</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(m);

  const stopLoginPoll = wireSklandLogin(m, '✓ token 已验证并填好，点「保存并同步账号」。');

  const close = () => {
    stopLoginPoll();
    api('/api/skland/login/cancel', {}).catch(() => { });
    m.style.transition = '.18s ease';
    m.style.opacity = '0';
    setTimeout(() => m.remove(), 180);
  };
  const showPage = n => {
    $$('[data-page]', m).forEach(el => { el.hidden = +el.dataset.page !== n; });
    $('#aa-next', m).hidden = n !== 1;
    $('#aa-go', m).hidden = n !== 2;
    $('#aa-back', m).hidden = n !== 2;
  };
  const state = $('#aa-maa-state', m);
  const input = $('#aa-maa', m);
  // 第 1 步检测结果：选定的 MAA 有没有练度数据（决定方式二可用）
  let operboxReady = false;
  const maaGoBtn = $('#aa-maa-go', m);
  const maaHint = $('#aa-maa-hint', m);
  const setMaaImport = r => {
    operboxReady = !!(r && r.operbox);
    maaGoBtn.disabled = !operboxReady;
    if (operboxReady) {
      maaHint.className = 'onb-hint ok';
      maaHint.innerHTML = `✓ 有练度数据（${r.operboxCount != null ? r.operboxCount + ' 名' : ''}${r.operboxSyncTime ? '，' + String(r.operboxSyncTime).slice(0, 10) + ' 识别' : ''}），点左边按钮直接建号`
        + `<br>! 识别结果不含技能 / 专精 / 模组，建号后作业匹配只比精英化与等级，技能模组按已拥有处理`;
    } else if (r) {
      maaHint.className = 'onb-hint bad';
      maaHint.textContent = '! 该 MAA 还没有干员识别数据（data/OperBoxData.json）——先在 MAA 小工具里跑一次干员识别，或改用方式一';
    } else {
      maaHint.className = 'onb-hint';
      maaHint.textContent = '在第 1 步检测 MAA 后，这里会显示练度数据概况。';
    }
  };

  const probe = async () => {
    const dir = input.value.trim();
    if (!dir) { state.className = 'onb-hint'; state.textContent = '先填一个目录再检测，或从上面列表选一个。'; setMaaImport(null); return null; }
    try {
      const r = await api('/api/maa/probe?dir=' + encodeURIComponent(dir));
      if (r.exeExists && r.configExists) {
        state.className = 'onb-hint ok';
        state.innerHTML = `✓ 找到 MAA.exe，配置 ${r.profiles.length} 个：<code>${esc(r.profiles.slice(0, 4).join(' / '))}</code>`
          + (r.operbox
            ? `<br>该 MAA 有练度数据（${r.operboxCount != null ? r.operboxCount + ' 名' : ''}${r.operboxSyncTime ? '，' + esc(String(r.operboxSyncTime).slice(0, 10)) + ' 识别' : ''}）`
            : '');
        setMaaImport(r);
      } else if (r.exeExists) {
        state.className = 'onb-hint bad';
        state.textContent = '! 找到 MAA.exe，但还没有 config/gui.new.json（先启动一次 MAA 就好）';
        setMaaImport(null);
      } else {
        state.className = 'onb-hint bad';
        state.textContent = '✗ 这个目录里没有 MAA.exe，请选包含 MAA.exe 的文件夹';
        setMaaImport(null);
      }
      return r;
    } catch (e) {
      state.className = 'onb-hint bad';
      state.textContent = '检测失败：' + e.message;
      setMaaImport(null);
      return null;
    }
  };
  const pickRow = (dir, extra = '') => {
    const lab = document.createElement('label');
    lab.className = 'onb-proc';
    lab.innerHTML = `<input type="radio" name="aa-dir">
      <code title="${esc(dir)}">${esc(dir)}</code>${extra}`;
    lab.querySelector('input').addEventListener('change', () => {
      input.value = dir;
      probe();
    });
    return lab;
  };

  // 已保存列表（历史 maaDirs）
  const savedBox = $('#aa-saved', m);
  if (saved.length) {
    saved.forEach(d => savedBox.appendChild(pickRow(d)));
    input.value = saved[0];
  }

  // 新发现的正在运行 MAA（排除已保存的）
  const loadProcs = async () => {
    const box = $('#aa-procs', m);
    box.innerHTML = '<span class="onb-hint">检测中…</span>';
    try {
      const r = await api('/api/maa/running');
      const rows = (r.list || []).filter(
        row => !savedKeys.has(row.dir.replace(/[\\/]+$/, '').toLowerCase()));
      if (!rows.length) {
        box.innerHTML = '<span class="onb-hint">没有新发现——正在运行的 MAA 都已在上面列表里，或没有 MAA 在跑。</span>';
        return;
      }
      box.innerHTML = '';
      rows.forEach(row => {
        box.appendChild(pickRow(row.dir, `<span class="onb-proc-pid">PID ${row.pids.join(' / ')}</span>`));
      });
      if (!input.value.trim()) { input.value = rows[0].dir; probe(); }
    } catch (e) {
      box.innerHTML = `<span class="onb-hint bad">检测失败：${esc(e.message)}</span>`;
    }
  };

  $('#onb-token-help', m).addEventListener('click', () => {
    const g = $('#onb-token-guide', m);
    g.hidden = !g.hidden;
    if (!g.hidden) $('#onb-token', m).focus();
  });
  $$('[data-url]', m).forEach(b => b.addEventListener('click', () => openUrl(b.dataset.url)));
  input.addEventListener('input', () => {
    $$('input[name="aa-dir"]', m).forEach(r => { r.checked = false; });
  });
  $('#aa-procs-refresh', m).addEventListener('click', loadProcs);
  $('#aa-pick', m).addEventListener('click', async () => {
    await pickFolder(input);
    if (input.value.trim()) { $$('input[name="aa-dir"]', m).forEach(r => { r.checked = false; }); probe(); }
  });
  $('#aa-probe', m).addEventListener('click', probe);
  loadProcs();

  const cancelBtn = $('#aa-cancel', m);
  if (cancelBtn) cancelBtn.addEventListener('click', close);
  const skipBtn = $('#onb-skip', m);
  if (skipBtn) skipBtn.addEventListener('click', close);
  $('#aa-back', m).addEventListener('click', () => showPage(1));
  $('#aa-next', m).addEventListener('click', async () => {
    const dir = input.value.trim();
    if (!dir) { toast(onboard ? '先选好 MAA 根目录，或点「稍后设置」' : '先给这个账号选一个 MAA 根目录', 'warn'); input.focus(); return; }
    const ok = await probe();
    if (!ok || !ok.exeExists) { toast('MAA 目录没通过检测，改一下再试', 'err'); return; }
    showPage(2);
    setTimeout(() => { const t = $('#onb-token', m); if (t && !t.value.trim()) t.focus(); }, 80);
  });

  // 首次引导：选定目录同时存为全局默认（失败不拦后面的添加）
  const saveOnboardDir = async dir => {
    if (!onboard || !dir) return;
    try {
      S.settings = (await api('/api/settings', { maaDir: dir })).settings;
      S.maa = await api('/api/maa/status');
      fillBasketProfiles();
    } catch (e) { /* 存目录失败不拦同步/建号 */ }
  };

  // 方式一：森空岛 token，一行一个批量导入，失败的不拖累后面的行
  let pendingRetry = [];
  $('#aa-go', m).addEventListener('click', async () => {
    const btn = $('#aa-go', m);
    const tokens = pendingRetry.length ? pendingRetry
      : [...new Set($('#onb-token', m).value.split(/\r?\n/).map(s => s.trim()).filter(Boolean))];
    const alias = $('#onb-alias', m).value.trim();
    const dir = input.value.trim();
    if (!tokens.length) { toast('先把森空岛 token 粘进来，或用方式二直接从 MAA 导入', 'warn'); showPage(2); $('#onb-token', m).focus(); return; }
    if (dir) {
      const ok = await probe();
      if (!ok || !ok.exeExists) { toast('MAA 目录没通过检测，改一下再试', 'err'); showPage(1); return; }
    }
    await saveOnboardDir(dir);
    pendingRetry = [];
    btn.disabled = true;
    const logBox = $('#task-log', m);
    logBox.style.display = 'block';
    logBox.innerHTML = '';
    const many = tokens.length > 1;
    if (many && alias) {
      const div = document.createElement('div');
      div.textContent = '一次添加多个账号时「备注」不生效（各账号用森空岛昵称区分），已忽略。';
      logBox.appendChild(div);
    }
    const added = [];
    const failed = [];
    for (let i = 0; i < tokens.length; i++) {
      btn.textContent = many ? `同步中…（${i + 1}/${tokens.length}）` : '同步中…';
      try {
        const { taskId } = await api('/api/account/refresh', { token: tokens[i], alias: many ? '' : alias });
        const result = await pollTask(taskId, m);
        added.push((result && result.newest) || '');
      } catch (e) {
        failed.push({ token: tokens[i], error: e.message });
      }
    }
    let importedTotal = 0;
    if (added.length) {
      // 给新加的账号们绑定选定的 MAA；留空就不绑定（回退全局默认）
      if (dir) {
        for (const file of added) {
          if (file) await api('/api/account/maa', { file, patch: { dir } }).catch(() => { });
        }
        // 新账号顺手导入 MAA 里的干员识别结果（不依赖 token；失败不影响账号添加）
        if (operboxReady) {
          for (const file of added) {
            if (!file) continue;
            taskLogLine(logBox, `从 MAA 导入干员信息（${file}）…`);
            try {
              const r = await api('/api/account/import-maa', { file });
              importedTotal += r.imported || 0;
              taskLogLine(logBox, `✓ 已导入 ${r.imported || 0} 名干员${r.skipped ? `（跳过 ${r.skipped} 名）` : ''}，来源 ${r.source === 'yituliu' ? '一图流' : 'MAA 本地识别'}`);
            } catch (e) {
              taskLogLine(logBox, `✗ ${file} 干员信息导入失败：${e.message}`, true);
            }
          }
        } else {
          taskLogLine(logBox, '! 该 MAA 还没有干员识别数据（data/OperBoxData.json），可先在 MAA 小工具里识别一次，之后从森空岛页手动导入', true);
        }
      }
      const acc = await api('/api/accounts');
      S.accounts = acc.accounts;
      renderAccounts();
      renderMaaState();
      const st = await api('/api/settings', {});
      S.settings = st.settings;
    }
    if (failed.length) {
      toast(`成功 ${added.length} 个，失败 ${failed.length} 个，详见日志`, 'err', 6000);
      failed.forEach(f => {
        const div = document.createElement('div');
        div.className = 'err';
        div.textContent = `✗ token ${f.token.slice(0, 6)}…：${f.error}`;
        logBox.appendChild(div);
      });
      pendingRetry = failed.map(f => f.token);
      btn.disabled = false;
      btn.textContent = `重试失败的 ${failed.length} 个`;
      return;
    }
    close();
    const extra = importedTotal > 0 ? '，练度已从 MAA 导入' : '';
    toast(many ? `${added.length} 个账号已添加${extra}` : `账号已添加${extra}`);
    const target = added[added.length - 1];
    if (target) await selectAccount(target, true);
  });

  // 方式二：不用 token，直接用选定 MAA 的识别结果建本地账号
  $('#aa-maa-go', m).addEventListener('click', async () => {
    const dir = input.value.trim();
    const alias = $('#onb-alias', m).value.trim();
    if (!dir || !operboxReady) { toast('先在第 1 步选一个有练度数据的 MAA 目录', 'warn'); showPage(1); return; }
    maaGoBtn.disabled = true;
    maaGoBtn.textContent = '导入中…';
    const logBox = $('#task-log', m);
    logBox.style.display = 'block';
    logBox.innerHTML = '';
    try {
      await saveOnboardDir(dir);
      const r = await api('/api/account/create-from-maa', { dir, alias });
      taskLogLine(logBox, `✓ 已创建账号 ${r.file}（${r.imported || 0} 名干员${r.skipped ? '，跳过 ' + r.skipped + ' 名' : ''}，来源 ${r.source === 'yituliu' ? '一图流' : 'MAA 本地识别'}）`);
      if (r.source !== 'yituliu') {
        taskLogLine(logBox, '! MAA 本地识别不含技能 / 专精 / 模组：作业匹配只比精英化与等级，技能模组按已拥有处理，抄之前请在 MAA 里确认', true);
      }
      (r.warnings || []).forEach(w => taskLogLine(logBox, '! ' + w, true));
      const acc = await api('/api/accounts');
      S.accounts = acc.accounts;
      renderAccounts();
      renderMaaState();
      const st = await api('/api/settings', {});
      S.settings = st.settings;
      close();
      toast('账号已添加（从 MAA 导入）');
      if (r.file) await selectAccount(r.file, true);
    } catch (e) {
      taskLogLine(logBox, '✗ 建号失败：' + e.message, true);
      toast('建号失败：' + e.message, 'err', 6000);
      maaGoBtn.disabled = false;
      maaGoBtn.textContent = '用这个 MAA 直接添加';
    }
  });

  showPage(1);
  setTimeout(() => { const t = $('#aa-maa', m); if (t) t.focus(); }, 80);
}

function openAddAccount() { openAccountWizard('add'); }

function openOnboarding() {
  if (S.onboarded) return;
  S.onboarded = true;
  openAccountWizard('onboard');
}

/* ------------------------------------------------------------ 内置森空岛登录
   首次引导 / 添加账号两个向导共用：打开登录小窗，轮询状态，
   成功就把验证过的 token 回填到向导里的 #onb-token。返回停止轮询函数。 */
function wireSklandLogin(m, doneHtml) {
  let timer = null;
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  const loginState = $('#onb-login-state', m);
  const setLoginState = (cls, html) => {
    loginState.className = 'onb-hint' + (cls ? ' ' + cls : '');
    loginState.innerHTML = html;
  };
  $('#onb-login-btn', m).addEventListener('click', async () => {
    const btn = $('#onb-login-btn', m);
    btn.disabled = true;
    btn.textContent = '登录窗口已打开…';
    try {
      await api('/api/skland/login', {});
    } catch (e) {
      btn.disabled = false;
      btn.textContent = '打开登录窗口';
      setLoginState('bad', '✗ ' + esc(e.message));
      return;
    }
    setLoginState('', '在弹出的森空岛小窗里登录，成功后会自动验证 token、关掉小窗并填到下方。');
    stop();
    timer = setInterval(async () => {
      let st;
      try { st = await api('/api/skland/login/status'); } catch (e) { return; }
      if (st.phase === 'verifying') {
        setLoginState('', '已捕获请求，正在验证 token…');
      } else if (st.phase === 'done' && st.token) {
        stop();
        $('#onb-token', m).value = st.token;
        btn.disabled = false;
        btn.textContent = '再抓一次';
        setLoginState('ok', doneHtml || '✓ token 已验证并填好，继续操作即可。');
        toast('森空岛 token 已自动获取');
      } else if (st.phase === 'failed') {
        stop();
        btn.disabled = false;
        btn.textContent = '打开登录窗口';
        setLoginState('bad', '✗ ' + esc(st.error || '内置登录出错') + '，也可以在下面手动粘贴 token。');
      } else if (st.phase === 'closed') {
        stop();
        btn.disabled = false;
        btn.textContent = '打开登录窗口';
        setLoginState('', '登录窗口已关闭。没抓到的话重新打开，或在下面手动粘贴。');
      }
    }, 900);
  });
  return stop;
}

/* ------------------------------------------------------------ 设置 */
function nativeApi() { return (window.pywebview && window.pywebview.api) || null; }

async function openUrl(url) {
  const n = nativeApi();
  if (n && n.open_url) {
    try {
      const r = await n.open_url(url);
      if (r && r.ok === false) toast(r.error || '打开失败', 'err');
      return;
    } catch (e) { /* 落到 window.open */ }
  }
  window.open(url, '_blank');
}

async function pickFolder(inputEl) {
  const n = nativeApi();
  if (!n || !n.choose_folder) {
    toast('当前不是桌面窗口，请手动粘贴路径', 'err');
    return;
  }
  const r = await n.choose_folder('选择文件夹', inputEl.value.trim());
  if (r && r.path) inputEl.value = r.path;
  else if (r && r.error) toast('选择失败：' + r.error, 'err');
}

function openSettings() {
  const s = S.settings;
  // 代理拆成 地址+端口 两框回填；没配过给地址预填本机默认，端口空=直连
  const pv = String(s.proxy || '').trim();
  const pm = pv.match(/^(.*?):(\d+)$/);
  const proxyHost = pm ? pm[1] : 'http://127.0.0.1';
  const proxyPort = pm ? pm[2] : '';
  const p = S.paths || {};
  const pk = S.pack || {};
  const gd = S.gamedata || {};
  const counts = pk.counts || {};
  const n = nativeApi();
  const openBtn = (which, label) => n
    ? `<button class="btn-plain mini" data-open="${which}">${label}</button>` : '';
  modal(`
    <h3>设置</h3>
    <div class="sub">改动立即生效。账号数据与配置都保存在本机项目目录里，不会上传。</div>
    <label class="field"><span>默认 MAA 根目录（包含 MAA.exe；账号没单独指定就用它）</span>
      <div class="path-row">
        <input type="text" id="set-maa" value="${esc(s.maaDir || '')}">
        <button class="btn-plain mini" data-pick="set-maa">浏览</button>
        <button class="btn-plain mini" id="set-probe">检测</button>
      </div></label>
    <div class="onb-hint" id="set-maa-state">${
      S.maa && S.maa.exeExists ? '✓ MAA.exe 已找到' : '✗ 尚未检测到 MAA.exe'}</div>
    <label class="check-row" style="margin-bottom:14px">
      <input type="checkbox" id="set-autostart" ${s.autoStartMaa ? 'checked' : ''}>
      <span>写完作业后自动启动 MAA</span>
    </label>
    <label class="check-row" style="margin-bottom:14px">
      <input type="checkbox" id="set-autopack" ${s.autoUpdatePack === false ? '' : 'checked'}>
      <span>启动后自动更新游戏资源（新关卡 / 新干员 / 新活动）</span>
    </label>
    <label class="check-row" style="margin-bottom:14px">
      <input type="checkbox" id="set-autocheck" ${s.autoCheckAppUpdate === false ? '' : 'checked'}>
      <span>启动后检查程序新版本（6 小时最多一次，只提醒，装不装由你定）</span>
    </label>

    <div class="bl-entry-row">
      <button class="btn-plain" id="set-bl-char">干员黑名单</button>
      <button class="btn-plain" id="set-bl-op">作业黑名单</button>
      <button class="btn-plain" id="set-bl-author">作者黑名单</button>
    </div>

    <div class="path-list">
      <div class="path-item"><span>账号数据</span><code>${esc(p.accounts || '')}</code>${openBtn('accounts', '打开')}</div>
      <div class="path-item"><span>配置与日志</span><code>${esc(p.config || '')}</code>${openBtn('config', '打开')}</div>
      <div class="path-item"><span>游戏资源</span><code id="set-pack-msg">v${esc(gd.version || pk.version || '?')} · ${counts.chars || 0} 干员 · ${counts.stages || 0} 关卡</code>
        <button class="btn-plain mini" id="set-pack-check">检查更新</button></div>
      <div class="path-item"><span>程序版本</span><code id="set-app-msg">MAA-PRTS ${esc(
        gd.app || (S.appInfo && S.appInfo.version) || '')}${appVerSuffix()}</code>
        <button class="btn-plain mini" id="set-app-check">${
          S.appUpdate && S.appUpdate.ok && S.appUpdate.hasUpdate ? '查看新版本' : '检查更新'}</button></div>
    </div>

    <label class="field" style="margin-top:14px"><span>网络代理（连不上 GitHub 时才需要；只用于上面两项检查 / 下载更新，端口留空 = 直连）</span>
      <div class="path-row">
        <input type="text" id="set-proxy-host" value="${esc(proxyHost)}" placeholder="http://127.0.0.1">
        <input type="text" id="set-proxy-port" value="${esc(proxyPort)}" placeholder="7890" inputmode="numeric" style="flex:0 0 92px">
      </div></label>

    <div class="modal-foot">
      <button class="btn-plain" data-close>取消</button>
      <button class="btn-solid" id="btn-save-set">保存</button>
    </div>
  `, {
    onMount(el, close) {
      $$('[data-pick]', el).forEach(b => b.addEventListener('click', async () => {
        const input = $('#' + b.dataset.pick, el);
        await pickFolder(input);
      }));
      $$('[data-open]', el).forEach(b => b.addEventListener('click', async () => {
        const api2 = nativeApi();
        if (api2 && api2.open_folder) {
          const r = await api2.open_folder(b.dataset.open);
          if (r && r.ok === false) toast(r.error || '打开失败', 'err');
        }
      }));
      $('#set-probe', el).addEventListener('click', async () => {
        const box = $('#set-maa-state', el);
        const dir = $('#set-maa', el).value.trim();
        if (!dir) { box.className = 'onb-hint bad'; box.textContent = '先填目录'; return; }
        try {
          const r = await api('/api/maa/probe?dir=' + encodeURIComponent(dir));
          box.className = 'onb-hint ' + (r.exeExists && r.configExists ? 'ok' : 'bad');
          box.textContent = r.exeExists
            ? (r.configExists ? `✓ 找到 MAA.exe，配置 ${r.profiles.length} 个` : '✗ 缺 config/gui.new.json')
            : '✗ 这个目录里没有 MAA.exe';
        } catch (e) {
          box.className = 'onb-hint bad';
          box.textContent = '检测失败：' + e.message;
        }
      });
      $('#set-pack-check', el).addEventListener('click', async () => {
        const btn = $('#set-pack-check', el);
        const msg = $('#set-pack-msg', el);
        btn.disabled = true;
        msg.textContent = '检查中…';
        try {
          const r = await packCheckNow();
          msg.textContent = r.text;
          toast(r.text, r.ok ? 'ok' : 'err', 4000);
        } catch (e) {
          msg.textContent = '检查失败';
          toast('检查失败：' + e.message, 'err');
        } finally {
          btn.disabled = false;
        }
      });
      $('#set-app-check', el).addEventListener('click', async () => {
        const btn = $('#set-app-check', el);
        const msg = $('#set-app-msg', el);
        // 启动时已经查到有更新：直接开弹窗，不用再查一遍
        if (S.appUpdate && S.appUpdate.ok && S.appUpdate.hasUpdate) {
          close();
          setTimeout(openUpdateDialog, 160);
          return;
        }
        btn.disabled = true;
        const old = msg.textContent;
        msg.textContent = '检查中…';
        try {
          const r = await appCheckNow();
          msg.textContent = old;
          renderAppDot();
          toast(r.text, r.ok ? 'ok' : 'err', 4000);
          if (r.hasUpdate) {
            close();
            setTimeout(openUpdateDialog, 160);
          }
        } catch (e) {
          msg.textContent = old;
          toast('检查失败：' + e.message, 'err');
        } finally {
          btn.disabled = false;
        }
      });
      $('#set-bl-char', el).addEventListener('click', () => openBlacklist('char'));
      $('#set-bl-author', el).addEventListener('click', () => openBlacklist('author'));
      $('#set-bl-op', el).addEventListener('click', openOpBlacklist);
      $('#btn-save-set', el).addEventListener('click', async () => {
        const host = $('#set-proxy-host', el).value.trim();
        const port = $('#set-proxy-port', el).value.trim();
        if (port && !/^\d{1,5}$/.test(port)) {
          toast('端口只能是数字（如 7890）', 'warn');
          $('#set-proxy-port', el).focus();
          return;
        }
        if (port && host.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').includes(':')) {
          toast('地址里不用带端口，端口填右边那一格', 'warn');
          $('#set-proxy-host', el).focus();
          return;
        }
        // 两个都填了才启用代理；只填地址没填端口 = 直连
        const proxy = port ? ((host || 'http://127.0.0.1') + ':' + port) : '';
        try {
          const d = await api('/api/settings', {
            maaDir: $('#set-maa', el).value.trim(),
            autoStartMaa: $('#set-autostart', el).checked,
            autoUpdatePack: $('#set-autopack', el).checked,
            autoCheckAppUpdate: $('#set-autocheck', el).checked,
            proxy,
          });
          S.settings = d.settings;
          S.maa = d.maa;
          renderMaaState();
          fillBasketProfiles();
          close();
          if (proxy) {
            // 保存后探测一次真可用性；不可用也已保存——更新链路会自动直连回退
            try {
              const r = await api('/api/proxy/probe', { proxy });
              toast(r.ok ? `代理可用（${r.tcpMs}ms 连上）`
                : `代理不可用：${r.error}。已保存，更新时自动直连`,
                r.ok ? 'ok' : 'warn', 5200);
            } catch (e) { toast('代理已保存，但检测失败：' + e.message, 'warn', 5200); }
          } else {
            toast('设置已保存');
          }
        } catch (e) { toast('保存失败：' + e.message, 'err'); }
      });
    }
  });
}

/* ------------------------------------------------------------ 黑名单管理（设置 → 二级弹窗）
   条目统一按 {id, name} 处理：干员 id=name=干员名，另带 skills（勾选的
   技能号，空 = 屏蔽整个干员）；作者 id 是作业站数字 id（唯一键）、name
   只为显示。
   新增走搜索下拉：干员支持拼音首字母（lyyh→凛御银灰，/api/chars/suggest），
   从候选选中后按星级给技能勾选（6星三个 / 4·5星两个 / 3星一个 / 1·2星无），
   不勾=整个干员；作者输名字反查（/api/author/suggest），输纯数字直接当 id。
   增删改即时保存；干员黑名单变了立即回刷当前搜索结果（纯显示过滤），
   作者黑名单变了在关闭时重搜一次（过滤在后端）。 */
function openBlacklist(kind) {
  const isChar = kind === 'char';
  const key = isChar ? 'charBlacklist' : 'authorBlacklist';
  const items = () => {
    if (isChar) return (S.settings.charBlacklist || []).map(row => (
      typeof row === 'string' ? { id: row, name: row, skills: [] }
        : { id: row.name, name: row.name, skills: row.skills || [] }));
    return (S.settings.authorBlacklist || []).map(
      a => ({ id: a.id, name: a.name || S.authorNames[a.id] || '' }));
  };
  let editing = -1;
  let dirty = false;
  let selectedChar = null;      // 从候选选中的 {name, rarity}；null = 手输原文
  const skillsSel = new Set();  // 新增区勾选的技能号
  let editSkills = new Set();   // 编辑行勾选的技能号
  const SKILL_CN = { 1: '一技能', 2: '二技能', 3: '三技能' };
  // 星级 → 可选技能（与后端 _fix_oper 同一套规则：6星 1-3、4/5星 1-2、3星 1）
  const skillChoices = rarity => {
    const r = Number(rarity);
    if (r >= 5) return [1, 2, 3];
    if (r >= 3) return [1, 2];
    if (r === 2) return [1];
    return [];
  };
  const skillsLabel = (sel, choices) => {
    if (!sel || !sel.size) return '无';
    if (choices && choices.length && sel.size === choices.length) return '全部';
    return [...sel].sort().map(s => SKILL_CN[s] || s).join('、');
  };
  modal(`
    <h3>${isChar ? '干员黑名单' : '作者黑名单'}</h3>
    <div class="sub">${isChar
      ? '加入后，搜索结果与作业详情里不再显示（比对结论不变）；勾了技能就只屏蔽对应技能的要求，不勾 = 整个干员。'
      : '按作业站作者屏蔽：该作者的作业不再出现在搜索结果里。'}</div>
    <div class="bl-add-wrap">
      <div class="path-row" style="margin-bottom:12px">
        <input type="text" id="bl-add" placeholder="${isChar
          ? '拼音首字母或名字，如：lyyh / 凛御银灰'
          : '作者名字搜索，或直接输数字 id'}" autocomplete="off">
        ${isChar ? `
        <div class="bl-skill-pick" id="bl-skill-pick">
          <button class="btn-plain mini" id="bl-skill-btn" disabled>技能：全部</button>
          <div class="bl-skill-drop" id="bl-skill-drop" hidden></div>
        </div>` : ''}
        <button class="btn-plain mini" id="bl-add-btn" style="flex:0 0 auto">添加</button>
      </div>
      <div class="bl-drop" id="bl-drop" hidden></div>
    </div>
    <div class="bl-list" id="bl-list"></div>
    <div class="modal-foot"><button class="btn-plain" id="bl-close">关闭</button></div>
  `, {
    onMount(el, close) {
      const render = () => {
        const list = items();
        const box = $('#bl-list', el);
        if (!list.length) {
          box.innerHTML = `<div class="onb-hint">${isChar
            ? '还没有屏蔽任何干员。' : '还没有屏蔽任何作者。'}</div>`;
          return;
        }
        box.innerHTML = list.map((it, i) => i === editing ? `
          <div class="bl-item edit">
            <input type="text" class="bl-eid" value="${esc(it.id)}" placeholder="${isChar ? '干员名' : '数字 id'}">
            ${isChar ? `
            <span class="bl-skill-inline" title="勾选 = 要屏蔽的技能；全勾 = 整个干员">
              ${[1, 2, 3].map(s => `<button type="button" class="sk-toggle${editSkills.has(s) ? ' on' : ''}" data-sk="${s}">${'一二三'[s - 1]}</button>`).join('')}
            </span>` : ''}
            ${isChar ? '' : `<input type="text" class="bl-ename" value="${esc(it.name)}" placeholder="名字（可选）">`}
            <span class="bl-ops">
              <button class="btn-plain mini" data-act="ok">确定</button>
              <button class="btn-plain mini" data-act="cancel">取消</button>
            </span>
          </div>` : `
          <div class="bl-item" data-i="${i}">
            <span class="bl-name" title="${esc(it.name || it.id)}">${esc(it.name || it.id)}${isChar ? '' : ` <small>#${esc(it.id)}</small>`}</span>
            ${isChar ? `<span class="bl-sk-view" title="亮起的是被屏蔽的技能">${[1, 2, 3].map(s => `<i class="${!(it.skills || []).length || (it.skills || []).includes(s) ? 'on' : ''}">${'一二三'[s - 1]}</i>`).join('')}</span>` : ''}
            <span class="bl-ops">
              <button class="btn-plain mini" data-act="edit">编辑</button>
              <button class="btn-plain mini" data-act="del">删除</button>
            </span>
          </div>`).join('');
        $$('.bl-item[data-i]', box).forEach(row => {
          row.addEventListener('click', e => {
            const btn = e.target.closest('[data-act]');
            if (!btn) return;
            const i = Number(row.dataset.i);
            const list2 = items();
            if (btn.dataset.act === 'del') {
              list2.splice(i, 1);
              editing = -1;
              persist(list2);
            } else {
              // skills 空 = 整个干员 → 迁移成全勾，进编辑就是可反选的状态
              const sk = list2[i].skills || [];
              editSkills = sk.length ? new Set(sk) : new Set([1, 2, 3]);
              editing = i;
              render();
            }
          });
        });
        const okBtn = $('[data-act="ok"]', box);
        if (okBtn) {
          // 编辑行的技能 toggle（内联小按钮，避免浮层被滚动容器裁掉）
          $$('.sk-toggle', box).forEach(b => {
            b.addEventListener('click', () => {
              const s = Number(b.dataset.sk);
              if (editSkills.has(s)) editSkills.delete(s); else editSkills.add(s);
              b.classList.toggle('on', editSkills.has(s));
            });
          });
          okBtn.addEventListener('click', () => {
            const id = $('.bl-eid', box).value.trim();
            const name = isChar ? id : ($('.bl-ename', box).value.trim() || S.authorNames[id] || '');
            if (isChar ? !id : !/^\d+$/.test(id)) {
              toast(isChar ? '干员名不能为空' : '作者 id 只能是数字', 'warn');
              return;
            }
            const list2 = items();
            if (list2.some((x, j) => x.id === id && j !== editing)) {
              toast('这个条目已经在列表里了', 'warn');
              return;
            }
            if (isChar && !editSkills.size) {
              toast('至少保留一个要屏蔽的技能，或全勾（整个干员）', 'warn');
              return;
            }
            list2[editing] = isChar
              ? { id, name: id, skills: editSkills.size >= 3 ? [] : [...editSkills].sort() }
              : { id, name };
            editing = -1;
            persist(list2);
          });
          $('[data-act="cancel"]', box).addEventListener('click', () => { editing = -1; render(); });
        }
      };
      const persist = async next => {
        try {
          const d = await api('/api/settings', {
            [key]: isChar ? next.map(x => ({ name: x.id, skills: x.skills || [] })) : next,
          });
          S.settings = d.settings;
          dirty = true;
          render();
          if (isChar && S.result) renderResult(S.result);
        } catch (e) { toast('保存失败：' + e.message, 'err'); }
      };
      /* 新增 = 搜索下拉：输入时联想（干员本地首字母 / 作者作业站反查），
         点候选或回车选中；没有候选时干员按原文加、作者只认纯数字 id */
      let dropItems = [];
      const renderDrop = list => {
        dropItems = list || [];
        const drop = $('#bl-drop', el);
        if (!dropItems.length) { drop.hidden = true; drop.innerHTML = ''; return; }
        drop.hidden = false;
        drop.innerHTML = dropItems.map((it, i) => isChar
          ? `<div class="bl-drop-item" data-i="${i}"><span>${esc(it.name)}</span><small class="stars">${'★'.repeat(Math.min((it.rarity || 0) + 1, 6))}</small></div>`
          : `<div class="bl-drop-item" data-i="${i}"><span>${esc(it.name)}</span><small>#${esc(it.id)}</small></div>`
        ).join('');
        $$('.bl-drop-item', drop).forEach(row => {
          // mousedown 抢在 input 失焦之前把候选吃掉
          row.addEventListener('mousedown', e => {
            e.preventDefault();
            pick(dropItems[Number(row.dataset.i)]);
          });
        });
      };
      /* 新增区技能下拉：选中的干员决定可选技能（按星级），多选勾 */
      const skillBtn = $('#bl-skill-btn', el);
      const skillDrop = $('#bl-skill-drop', el);
      const refreshSkillPick = () => {
        if (!isChar || !skillBtn) return;
        const choices = selectedChar ? skillChoices(selectedChar.rarity) : [];
        [...skillsSel].forEach(s => { if (!choices.includes(s)) skillsSel.delete(s); });
        skillBtn.disabled = !choices.length;
        skillBtn.textContent = '技能：' + skillsLabel(skillsSel, choices);
        skillDrop.innerHTML = choices.length
          ? choices.map(s => `<label class="bl-skill-opt"><input type="checkbox" value="${s}" ${
              skillsSel.has(s) ? 'checked' : ''}> ${SKILL_CN[s]}</label>`).join('')
          : `<div class="bl-skill-empty">${selectedChar ? '这个干员没有技能（1 / 2 星）' : '先从候选里选一个干员'}</div>`;
        $$('input[type=checkbox]', skillDrop).forEach(cb => cb.addEventListener('change', () => {
          const s = Number(cb.value);
          if (cb.checked) skillsSel.add(s); else skillsSel.delete(s);
          skillBtn.textContent = '技能：' + skillsLabel(skillsSel, choices);
        }));
      };
      if (skillBtn) {
        skillBtn.addEventListener('click', e => {
          e.stopPropagation();
          if (!skillBtn.disabled) skillDrop.hidden = !skillDrop.hidden;
        });
        skillDrop.addEventListener('click', e => e.stopPropagation());
        refreshSkillPick();
      }
      const pick = it => {
        if (!it) return;
        const id = isChar ? it.name : String(it.id);
        if (items().some(x => x.id === id)) { toast('这个条目已经在列表里了', 'warn'); return; }
        if (isChar) {
          // 选中进入「待添加」：名字填回输入框；技能默认全勾（=屏蔽整个
          // 干员），用户去反选掉不想屏蔽的技能
          selectedChar = { name: it.name, rarity: it.rarity };
          skillsSel.clear();
          skillChoices(it.rarity).forEach(s => skillsSel.add(s));
          $('#bl-add', el).value = it.name;
          refreshSkillPick();
          renderDrop([]);
          $('#bl-add', el).focus();
          return;
        }
        S.authorNames[id] = it.name || S.authorNames[id] || '';
        persist(items().concat([{ id, name: it.name || '' }]));
        $('#bl-add', el).value = '';
        renderDrop([]);
        toast(`已添加 @${it.name || id}`, 'ok');
        $('#bl-add', el).focus();
      };
      let suggestTimer = null;
      let suggestSeq = 0;   // 防竞态：连续输入时旧请求晚回也不能覆盖新结果
      $('#bl-add', el).addEventListener('input', () => {
        clearTimeout(suggestTimer);
        const q = $('#bl-add', el).value.trim();
        // 输入一变就先清掉旧候选，异步结果没回来前不能让人点到上一个词的干员
        renderDrop([]);
        // 选中的干员又被手动改动 → 技能选择失效，重置
        if (isChar && selectedChar && q !== selectedChar.name) {
          selectedChar = null;
          skillsSel.clear();
          refreshSkillPick();
        }
        if (!q || (!isChar && /^\d+$/.test(q))) return;
        const my = ++suggestSeq;
        suggestTimer = setTimeout(async () => {
          try {
            const r = await api((isChar ? '/api/chars/suggest?q=' : '/api/author/suggest?q=') + encodeURIComponent(q));
            if (my !== suggestSeq) return;
            renderDrop(r.items || []);
          } catch (e) { if (my === suggestSeq) renderDrop([]); }
        }, isChar ? 200 : 350);
      });
      let addBusy = false;
      const add = async () => {
        const q = $('#bl-add', el).value.trim();
        if (!q) { toast(isChar ? '先输入干员名' : '先输入作者名字或数字 id', 'warn'); return; }
        if (dropItems.length) { pick(dropItems[0]); return; }
        if (items().some(x => x.id === q)) { toast('这个条目已经在列表里了', 'warn'); return; }
        if (isChar) {
          // 勾选 = 要屏蔽的技能：默认全勾（整个干员），反选掉的不屏蔽。
          // 全勾存 []（=整个干员，与旧数据同构）；全不勾等于啥都不屏蔽，拦下
          const choices = selectedChar ? skillChoices(selectedChar.rarity) : [];
          if (choices.length && !skillsSel.size) {
            toast('至少保留一个要屏蔽的技能，或全勾（整个干员）', 'warn');
            return;
          }
          const skills = choices.length && skillsSel.size === choices.length
            ? [] : [...skillsSel].sort();
          persist(items().concat([{ id: q, name: q, skills }]));
          $('#bl-add', el).value = '';
          selectedChar = null;
          skillsSel.clear();
          refreshSkillPick();
        } else if (/^\d+$/.test(q)) {
          persist(items().concat([{ id: q, name: S.authorNames[q] || '' }]));
          $('#bl-add', el).value = '';
          toast(`已添加 作者 #${q}`, 'ok');
        } else if (!addBusy) {
          // 名字搜索在途时不能抢答「没找到」：等这一轮搜完再报结果。
          // 按钮转「找人…」防重复点击，也告诉用户在等什么
          addBusy = true;
          const btn = $('#bl-add-btn', el);
          const old = btn.textContent;
          btn.disabled = true;
          btn.textContent = '找人…';
          try {
            const r = await api('/api/author/suggest?q=' + encodeURIComponent(q));
            const found = (r.items || [])[0];
            if (found) pick(found);
            else toast('没搜到这个作者；确认名字，或直接输入数字 id', 'warn');
          } catch (e) {
            toast('搜索失败：' + e.message, 'err');
          } finally {
            addBusy = false;
            btn.disabled = false;
            btn.textContent = old;
          }
        }
      };
      $('#bl-add-btn', el).addEventListener('click', add);
      $('#bl-add', el).addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
      // 点下拉以外的地方收起候选与技能勾选
      el.addEventListener('click', e => {
        if (!e.target.closest('.bl-add-wrap')) renderDrop([]);
        if (skillDrop) skillDrop.hidden = true;
      });
      $('#bl-close', el).addEventListener('click', () => {
        close();
        // 作者黑名单的过滤在后端：改过就重搜一次当前结果让它立即生效
        if (!isChar && dirty && S.result && $('#search-input').value.trim()) doSearch();
      });
      render();
    }
  });
}

/* ------------------------------------------------------------ 作业黑名单（设置 → 二级弹窗）
   条目就是 MAA 代码（maa://12345），软件级、不跟账号走；过滤在后端
   （fetch_ops 出口按代码滤），与作者黑名单一样改完关闭时重搜一次。 */
function openOpBlacklist() {
  let dirty = false;
  modal(`
    <h3>作业黑名单</h3>
    <div class="sub">拉黑的作业不再出现在搜索结果里。条目按 MAA 代码存，软件级、不跟账号走。</div>
    <div class="path-row" style="margin-bottom:12px">
      <input type="text" id="obl-add" placeholder="MAA 代码，如 maa://12345（或直接输数字）" autocomplete="off">
      <button class="btn-plain mini" id="obl-add-btn" style="flex:0 0 auto">添加</button>
    </div>
    <div class="bl-list" id="obl-list"></div>
    <div class="modal-foot"><button class="btn-plain" id="obl-close">关闭</button></div>
  `, {
    onMount(el, close) {
      const render = () => {
        const list = S.settings.opBlacklist || [];
        const box = $('#obl-list', el);
        box.innerHTML = list.length ? list.map((code, i) => `
          <div class="bl-item" data-i="${i}">
            <span class="bl-name"><code>${esc(code)}</code></span>
            <span class="bl-ops">
              <button class="btn-plain mini" data-act="del">删除</button>
            </span>
          </div>`).join('')
          : '<div class="onb-hint">还没有拉黑任何作业。作业详情里点「拉黑作业」，或在这里粘贴 MAA 代码。</div>';
        $$('.bl-item[data-i]', box).forEach(row => {
          row.addEventListener('click', e => {
            if (!e.target.closest('[data-act="del"]')) return;
            persist((S.settings.opBlacklist || []).filter((_, j) => j !== Number(row.dataset.i)));
          });
        });
      };
      const persist = async next => {
        try {
          const d = await api('/api/settings', { opBlacklist: next });
          S.settings = d.settings;
          dirty = true;
          render();
        } catch (e) { toast('保存失败：' + e.message, 'err'); }
      };
      const add = () => {
        const inp = $('#obl-add', el);
        let q = inp.value.trim();
        if (!q) { toast('先输入 MAA 代码', 'warn'); return; }
        if (/^\d+$/.test(q)) q = 'maa://' + q;   // 只输数字也当作业 id，补全成代码
        const list = S.settings.opBlacklist || [];
        if (list.includes(q)) { toast('这个作业已经在列表里了', 'warn'); return; }
        if (!/^maa:\/\/\d+$/.test(q)) { toast('格式要像 maa://12345', 'warn'); return; }
        persist(list.concat([q]));
        inp.value = '';
      };
      $('#obl-add-btn', el).addEventListener('click', add);
      $('#obl-add', el).addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
      $('#obl-close', el).addEventListener('click', () => {
        close();
        // 过滤在后端：改过就重搜一次当前结果让它立即生效
        if (dirty && S.result && $('#search-input').value.trim()) doSearch();
      });
      render();
    }
  });
}

/* ------------------------------------------------------------ 事件绑定 */
function bindGlobal() {
  $('#btn-next').addEventListener('click', () => {
    const code = $('#btn-next').dataset.code;
    if (!code) return;
    $('#search-input').value = code;
    doSearch();
  });
  $('#search-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
  $('#search-input').addEventListener('input', queueAutoSearch);
  // 「优先作业算法」：跟账号存，切换后立刻按新算法重排当前结果
  $('#algo-select').addEventListener('change', async e => {
    if (!S.current) return;
    try {
      await api('/api/account/pref', { file: S.current, preferAlgo: e.target.value });
      if (S.result && $('#search-input').value.trim()) doSearch();
    } catch (err) {
      toast('保存算法偏好失败：' + err.message, 'err');
    }
  });
  bindAccountOrder($('#account-list'));
  $('#btn-add-account').addEventListener('click', openAddAccount);
  $('#btn-import-maa').addEventListener('click', importFromMaa);
  $('#btn-settings').addEventListener('click', openSettings);
  $('#maa-state').addEventListener('click', async () => {
    await refreshMaaStatus();
    toast(S.maa.running ? 'MAA 正在运行' : 'MAA 未运行');
  });
  $('#btn-apply').addEventListener('click', applyToMaa);
  $('#btn-clear-basket').addEventListener('click', () => { S.basket = []; renderBasket(); });
  $('#btn-refresh-all').addEventListener('click', async () => {
    const m = modal(`<h3>同步全部账号</h3>
      <div class="sub">按 tokens.json 逐个刷新，可能耗时 1~3 分钟。</div>
      <div class="log-box" id="task-log"></div>
      <div class="modal-foot"><button class="btn-plain" data-close>后台执行</button></div>`);
    try {
      const { taskId } = await api('/api/account/refresh', {});
      await pollTask(taskId, m.el);
      setTimeout(() => m.close(), 900);   // 同步完自动收弹窗（与写入 MAA 一致）
      const d = await api('/api/accounts');
      S.accounts = d.accounts;
      renderAccounts();
      toast('全部账号已同步');
      if (S.current) selectAccount(S.current, true);
    } catch (e) { toast('同步失败：' + e.message, 'err', 5000); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { toggleBasket(false); return; }
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
      e.preventDefault();
      $('#search-input').focus();
    }
  });
  // 窄屏抽屉：浮动按钮开关，点遮罩关闭，窗口拖宽后自动收起
  $('#basket-fab').addEventListener('click', () => toggleBasket());
  $('#basket-mask').addEventListener('click', () => toggleBasket(false));
  window.addEventListener('resize', () => {
    if (window.innerWidth > 1180) toggleBasket(false);
  });
  setInterval(refreshMaaStatus, 30000);
}

/* 窄屏抽屉开关：不传参数就切换 */
function toggleBasket(open) {
  const box = $('#basket');
  const mask = $('#basket-mask');
  if (!box) return;
  const on = open === undefined ? !box.classList.contains('show') : !!open;
  box.classList.toggle('show', on);
  if (mask) mask.classList.toggle('show', on);
  document.body.classList.toggle('drawer-open', on);
}

document.addEventListener('DOMContentLoaded', () => {
  bindGlobal();
  renderBasket();
  boot();
});
