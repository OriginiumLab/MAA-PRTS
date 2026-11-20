/* ============================================================
   MAA-PRTS 控制台前端
   ============================================================ */
'use strict';

const S = {
  settings: {}, accounts: [], current: '', detail: null,
  maa: {}, activities: [],
  basket: [], result: null, appInfo: null,
  paths: {}, pack: {}, setup: {}, gamedata: {}, onboarded: false,
  appUpdate: null, updateBusy: false,
  paradoxPending: [],
  authorNames: {},   // 作业站作者 id → 昵称（搜索时顺手学，黑名单弹窗显示用）
  module: 'copilot', // 当前模块：copilot / gacha / signin / garrison（配置修改按需求去掉了）
  signBusy: '',      // 正在跑哪一块签到：''/all/one——同一 token 并发签到会被风控，只能一个一个来
  gar: null,         // /api/garrison/status 的最近一次响应（当前账号那份）
  garOpts: {},       // file → 用户改过的速刷连接选项（没改过的账号用后端从 MAA 抄的默认值）
  gacha: null,       // /api/gacha 的最近一次响应（当前账号那份）
  gachaOf: {},       // file → 已加载过的抽卡视图：切账号/切菜单回来直接复用，不再转圈
  lastOfGame: { arknights: '', endfield: '' },  // 每栏上次看的是哪个号（切回这一栏时还原）
  gachaTab: 'char',  // 抽卡统计里看哪个池：char 角色寻访 / weapon 武器申领（终末地专用）
  sideGame: 'arknights',   // 左侧账号列表当前看哪一栏：arknights / endfield
  gachaByGame: false,      // 抽卡统计是因为选中终末地账号才打开的（切回方舟时自动回作业获取）
};

/* 账号属于哪个游戏（老账号没有 game 字段，一律当明日方舟） */
function gameOf(a) {
  return (a && a.game === 'endfield') ? 'endfield' : 'arknights';
}

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

async function api(path, body, { signal } = {}) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  if (signal) opt.signal = signal;   // 调用方可中止在途请求（搜索被新输入作废时用）
  const r = await fetch(path, opt);
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('服务返回异常（' + r.status + '）'); }
  if (!j.ok) throw new Error(j.error || '未知错误');
  return j.data;
}

/* 设置保存统一走这里（#45）：
   - 串行排队：后端是整键替换，两个在途保存以陈旧基底各发各的会互相覆盖
     （快速连续拉黑丢前一条就是这类），排队 + 先把 patch 乐观并进 S.settings，
     后一个保存就能以前一个的结果为基底；
   - 成功用服务端返回的全量视图刷新 S.settings；失败先拉一次全量恢复现场
     （乐观值可能已经写进 UI），再把错误抛给调用方。 */
let _saveSettingsQ = Promise.resolve();
function saveSettings(patch) {
  const run = async () => {
    Object.assign(S.settings, patch);
    try {
      const d = await api('/api/settings', patch);
      S.settings = d.settings;
    } catch (e) {
      try { S.settings = (await api('/api/settings', {})).settings; } catch (_) { }
      throw e;
    }
  };
  _saveSettingsQ = _saveSettingsQ.then(run, run);
  return _saveSettingsQ;
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
  if (opts.cls) mask.firstElementChild.classList.add(opts.cls);   // 设置那种宽对话框用
  $('#modal-root').appendChild(mask);
  const close = () => {
    mask.style.transition = '.14s ease';
    mask.style.opacity = '0';
    setTimeout(() => mask.remove(), 140);
    // 关窗回调（设置页用它刷新列表 / 条数；点遮罩、点按钮都算一次）
    if (opts.onClose) { try { opts.onClose(); } catch (e) { } }
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
    // 左侧默认停在当前账号那一栏（上次用的是终末地就停在终末地）
    S.sideGame = gameOf(d.accounts.find(a => a.file === S.current) || d.accounts[0]);
    $('#basket-autostart').checked = S.settings.autoStartMaa !== false;   // 设置里的默认开关真正生效
    renderAccounts();
    renderUpdate();
    renderQuick();
    renderMaaState();
    fillBasketProfiles();
    // 助手配置（设置弹窗与聊天面板都要用；拿不到不影响主流程）
    api('/api/agent/status').then(d => { S.agent = d; chatInit(); }).catch(() => { });
    if (S.current) await selectAccount(S.current, true);
    // ?mod=gacha 直接打开抽卡统计（做快捷方式用，和 ?q=TL-10 一样）
    // 其余情况模块显隐由 selectAccount → applyModuleAvailability 决定，别重复渲染
    const mod0 = new URLSearchParams(location.search).get('mod');
    if (mod0 === 'gacha' || mod0 === 'signin' || mod0 === 'chat') setModule(mod0);
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
/* 侧栏顶部两个分组标签（明日方舟 / 终末地）：数量实时跟着 S.accounts 走。
   某一组没有账号时标签照样在（点了给空态提示），不然会让人以为账号丢了。 */
function renderGameSwitch() {
  const box = $('#game-switch');
  if (!box) return;
  const n = { arknights: 0, endfield: 0 };
  S.accounts.forEach(a => { n[gameOf(a)] += 1; });
  $$('.gswitch', box).forEach(b => {
    const g = b.dataset.game, on = g === S.sideGame;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    const c = $('.gcount', b);
    if (c) c.textContent = n[g];
  });
}

/* 切换分组：当前选中的账号不在这一组时，顺手选这组的第一个，
   否则左边是终末地、右边主区还挂着方舟账号，看着对不上。 */
function setSideGame(g) {
  if (g !== 'arknights' && g !== 'endfield') return;
  const changed = S.sideGame !== g;
  S.sideGame = g;
  const cur = currentAccount();
  if (changed && S.current && gameOf(cur) !== g) {
    // 先回这一栏上次看的号（切走前在看谁，切回来还是谁），没有才取第一个
    const pick = S.accounts.find(a => a.file === S.lastOfGame[g] && gameOf(a) === g)
      || S.accounts.find(a => gameOf(a) === g);
    if (pick) { selectAccount(pick.file); return; }   // selectAccount 里会重建列表
  }
  renderAccounts();
}

function renderAccounts() {
  const box = $('#account-list');
  renderGameSwitch();
  if (!S.accounts.length) {
    box.innerHTML = '<div style="padding:14px 10px;font-size:12px;color:var(--text-3);line-height:1.8">还没有账号<br>点下面的按钮用森空岛 token 添加</div>';
    return;
  }
  const list = S.accounts.filter(a => gameOf(a) === S.sideGame);
  if (!list.length) {
    box.innerHTML = S.sideGame === 'endfield'
      ? '<div style="padding:14px 10px;font-size:12px;color:var(--text-3);line-height:1.8">这一栏还没有终末地账号<br>用森空岛 token 同步账号时，token 绑定了终末地角色就会自动生成</div>'
      : '<div style="padding:14px 10px;font-size:12px;color:var(--text-3);line-height:1.8">这一栏还没有明日方舟账号<br>点下面的按钮用森空岛 token 添加</div>';
    return;
  }
  box.innerHTML = list.map(a => {
    const st = a.stats || {};
    const ef = a.game === 'endfield';                 // 终末地：没有练度数据，副标题换口径
    const cls = ef ? 'ef' : (st.sixStarCount >= 120 ? 'r6' : (st.sixStarCount >= 60 ? 'r5' : ''));
    const ch = (a.nickName || '?').replace(/[#＃].*$/, '').slice(0, 1);
    // 渠道名单独上色：B 站服粉、官服亮白，其它渠道不动（同 style.css .acct-ch）
    const chName = a.channelName || '官服';
    const chCls = /bilibili/i.test(chName) ? ' bili' : (/^官服/.test(chName) ? ' official' : '');
    const chHtml = `<span class="acct-ch${chCls}">${esc(chName)}</span>`;
    const sub = ef
      ? `${chHtml} · Lv${a.level || '?'}`
      : `${chHtml} · <b>${st.charCount || 0}</b>/${st.charTotal || 440} 干员`;
    return `
      <button class="acct ${a.file === S.current ? 'active' : ''}" data-file="${esc(a.file)}"
        draggable="true" title="点击选中，拖动可排序${ef ? '（终末地账号，不含作业获取）' : ''}">
        <span class="avatar ${cls}">${esc(ch)}</span>
        <span class="acct-main">
          <span class="acct-name">${esc(a.nickName)}${ef ? '<i class="acct-tag">终末地</i>' : ''}</span>
          <span class="acct-sub">${sub}</span>
        </span>
        <span class="acct-del" data-file="${esc(a.file)}" role="button" title="删除这个账号">✕</span>
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
    // 列表里只有当前分组（明日方舟或终末地）。把这一组的新序填回原位，
    // 另一组的位置保持不动——否则拖一次就把另一组的排序整体冲掉了
    const moved = $$('.acct', box).map(b => b.dataset.file);
    const pick = new Set(moved);
    let k = 0;
    const order = S.accounts.map(a => (pick.has(a.file) ? moved[k++] : a.file));
    const byFile = new Map(S.accounts.map(a => [a.file, a]));
    // 内存数组同步成新序——否则下一次 renderAccounts（点选别的账号就会触发）
    // 还按旧序重建，界面看起来像「拖完又弹回去」
    S.accounts = order.map(f => byFile.get(f)).filter(Boolean);
    try {
      await saveSettings({ accountOrder: order });
    } catch (e) { /* 保存失败不回滚视觉顺序，刷新后会还原 */ }
  };

  box.addEventListener('click', e => {
    // 删除 ✕ 先于选号拦截：点的是卡片里的 ✕ 就不该再把账号切成选中
    const del = e.target.closest('.acct-del');
    if (del) { deleteAccount(del.dataset.file); return; }
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

/* 删除账号：卡片 hover 出 ✕，点后二次确认。删就是删——账号文件直接
   删除，不留备份（要找回只能重新同步）。
   onDone：设置页要拿它刷新自己那份账号列表，主界面传不传都一样。 */
async function deleteAccount(file, onDone) {
  const a = S.accounts.find(x => x.file === file);
  if (!a) return;
  const game = gameOf(a);
  const wasCurrent = S.current === file;
  modal(`
    <h3>删除账号</h3>
    <div class="sub">确定删除「${esc(a.nickName || file)}」？删除后不可恢复，练度数据需要重新同步。</div>
    <div class="modal-foot">
      <button class="btn-plain" data-close>取消</button>
      <button class="btn-solid" id="acct-del-yes">删除</button>
    </div>`, {
    onMount(el, close) {
      $('#acct-del-yes', el).addEventListener('click', async () => {
        const yes = $('#acct-del-yes', el);
        yes.disabled = true;
        try {
          await api('/api/account/remove', { file });
          close();
          const acc = await api('/api/accounts');
          S.accounts = acc.accounts;
          if (wasCurrent) {
            // 换到同栏剩下的第一个；同栏空了就另一栏；全空清掉选中
            const next = (S.accounts.find(x => gameOf(x) === game) || S.accounts[0] || {}).file || '';
            if (next) await selectAccount(next, true);
            else {
              S.current = ''; S.detail = null; S.sideGame = 'arknights';
              renderHero();
              applyModuleAvailability();
            }
          }
          renderAccounts();
          if (onDone) onDone();
          toast(`已删除「${a.nickName || file}」`);
        } catch (e) {
          yes.disabled = false;
          toast('删除失败：' + e.message, 'err', 5000);
        }
      });
    }
  });
}

/* 切号请求的代数：快速连点 A→B 时两个请求并发，晚归的旧响应不能
   覆盖新选择（详情串台：头像/统计是 A 的、高亮在 B 上） */
let _selectSeq = 0;

async function selectAccount(file, silent) {
  if (!file) return;
  const seq = ++_selectSeq;
  const prev = S.current;
  const changed = S.current && S.current !== file;
  S.current = file;
  // 选中的账号在哪一栏，左侧就切到哪一栏（新加/刚同步出来的账号可能属于另一组）
  const acc0 = S.accounts.find(a => a.file === file);
  if (acc0) {
    const g0 = gameOf(acc0);
    S.sideGame = g0;
    S.lastOfGame[g0] = file;      // 切走再切回这一栏时回到这个号，而不是永远第一个
  }
  renderAccounts();
  if (changed && S.basket.length) {
    S.basket = [];
    renderBasket();
    toast('已切换账号，作业集重新开始', 'warn');
  }
  if (changed) {
    // 悖论 stage 跟着账号走：切号不清理的话，A 挑的悖论写入时会记到
    // 当前账号（B）的已导入表里
    S.paradoxPending = [];
  }
  try {
    const d = await api('/api/account?file=' + encodeURIComponent(file));
    if (seq !== _selectSeq) return;   // 已被更新的选择取代，丢弃旧响应
    S.detail = d;
    // 抽卡视图跟着账号走：这个号之前看过就直接复用内存里的那份（本地缓存本来就在，
    // 只是别再闪一次「读取中」），没看过才交给 renderGacha 去读
    S.gacha = S.gachaOf[file] || null;
    S.gachaTab = 'char';                                    // 换号回角色池，别停在武器池
    renderHero();
    updateParadoxChip();
    applyModuleAvailability();        // 终末地账号隐藏作业获取，并自动切到抽卡统计
    // 保存 lastAccount 顺带用响应里的全量 settings 刷新 S.settings——
    // 修复启动后第一次拉黑以「无 opBlacklist 的陈旧基底」整键覆盖的窗口期（#45）
    await saveSettings({ lastAccount: file });
    refreshMaaStatus();          // 状态卡与配置下拉切到这个账号绑定的 MAA
    if (!silent) {
      const r = $('#results');
      if (r.dataset.mode) doSearch();
    }
  } catch (e) {
    if (seq !== _selectSeq) return;   // 有更新选择在途，交给它收尾
    S.current = prev;                 // 失败回滚高亮，别停在半切换状态
    renderAccounts();                 // （作业集已清空的不恢复，宁空勿串）
    toast('读取账号失败：' + e.message, 'err');
  }
}

function renderHero() {
  const d = S.detail;
  const a0 = (d && d.account) || {};
  const ef = !!(d && (d.game === 'endfield' || a0.game === 'endfield'));
  const importBtn = $('#btn-import-maa');
  if (importBtn) importBtn.hidden = !d || ef;   // 终末地没有 MAA 练度可导
  const algoSel = $('#algo-select');
  if (algoSel) {
    // 算法下拉跟账号走：没有账号、或终末地账号（不搜作业）都不可用
    algoSel.disabled = !d || ef;
    if (d) algoSel.value = d.preferAlgo || 'rating';
  }
  if (!d) {
    // 没有选中账号（删光了）：回到 index.html 的初始占位，别残留上一个
    // 账号的头像/统计
    $('#hero-name').textContent = '未选择账号';
    $('#hero-sub').textContent = '用森空岛 token 添加一个账号开始';
    $('#hero-avatar').textContent = '?';
    $('#hero-avatar').classList.remove('ef');
    $('#hero-stats').innerHTML = '';
    return;
  }
  const a = d.account || {}, st = d.stats || {};
  const name = a.nickName || '未知账号';
  $('#hero-name').textContent = name;
  $('#hero-avatar').textContent = name.replace(/[#＃].*$/, '').slice(0, 1);
  $('#hero-avatar').classList.toggle('ef', ef);   // 终末地：头像配色跟着换

  if (ef) {
    // 终末地：没有干员图鉴，只展示身份 + 抽卡概况
    const bits = [a.channelName || '', a.serverName || '', 'Lv' + (a.level || '?'),
                  'uid ' + (a.uid || '')];
    $('#hero-sub').textContent = bits.filter(Boolean).join(' · ');
    const g = (S.gacha && S.gacha.file === S.current && S.gacha.summary) || null;
    $('#hero-stats').innerHTML = `
      <div class="stat"><b>${a.level || '—'}</b><span>等级</span></div>
      <div class="stat"><b>${esc(a.serverName || '—')}</b><span>区服</span></div>
      <div class="stat mint"><b>${g ? g.total : 0}</b><span>抽卡</span></div>
      <div class="stat ${g && g.sixCount ? 'amber' : ''}"><b>${g ? g.sixCount : 0}</b><span>六星</span></div>
    `;
    return;
  }

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
    <div class="onb-hint bad">! 会覆盖这个账号当前的练度（包括森空岛同步来的）。本地识别不含技能、专精、模组数据，更新后作业匹配只比精英化与等级，技能模组一律按已拥有处理，可能推给你实际打不了的作业——抄之前请在 MAA 里确认。</div>
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
          // force：用户在这个弹窗里点了确认，才允许覆盖森空岛同步的练度
          const r = await api('/api/account/import-maa', { file: S.current, force: true });
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
      // 安装脚本失败时会留下原因（rc / 被占用的 DLL）：一并说出来，
      // 用户才知道是「MAA 没关」而不是「更新坏了」
      const why = li.error ? '：' + li.error : '';
      toast(`上次更新到 ${li.to} 没有生效${why}`, 'err', 9000);
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
  // 设置里「版本更新」那一页也要点一个：只有外面齿轮上的红点时，用户点进来
  // 面对六页签根本不知道该看哪一页。设置没开时这行什么也选不到，无害。
  $$('[data-tab-dot="update"]').forEach(d => { d.hidden = !has; });
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
          px: true,   // 悖论来源标记：作业集里删掉它时联动清 paradoxPending
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
/* 输入停约 0.5s 自动搜索：每次键入重置计时，清空输入不搜也不弹提示。
   搜索落地后顺手在后台预载下一关（见 preloadNext），点「下一关」时秒开。 */
let searchTimer = 0;
let searchAbort = null;   // 在途搜索的 AbortController；配合 searchKey 给 doSearch 去重
let searchKey = '';       // 在途搜索的「账号|关键词」
let preloaded = null;     // 下一关的后台预载 { key, ac, promise }：只存数据，不碰 UI

/* 立刻作废还在飞行的搜索：中止请求（活动检索后端要跑十几秒，等它跑完再搜新的没意义）。
   被中止的一方走 doSearch catch 的 aborted 分支安静退场，不碰 UI。预载一并作废。 */
function stopSearch() {
  if (searchAbort) { const a = searchAbort; searchAbort = null; searchKey = ''; a.abort(); }
  if (preloaded) { const p = preloaded; preloaded = null; p.ac.abort(); }
}

/* 键入即停在途搜索；预载先缓一口气——防抖落地后 doSearch 按新关键词决定认领它
   （键入的正是预载代号）还是作废它（stopSearch），不白白重发一遍 */
function queueAutoSearch() {
  clearTimeout(searchTimer);
  if (searchAbort) { const a = searchAbort; searchAbort = null; searchKey = ''; a.abort(); }
  searchTimer = setTimeout(() => {
    if ($('#search-input').value.trim()) doSearch();
    else {
      stopSearch();   // 输入清空了：预载也没有存在的意义
      // 旧搜索被作废后输入又清空了：骨架屏不能一直转下去，落回可解释的空态
      if ($('#results').dataset.mode === 'loading') {
        const box = $('#results');
        box.dataset.mode = '';
        box.innerHTML = '<div class="empty-state"><h3>已停止检索</h3><p>输入关卡或活动代号，停顿后自动搜索</p></div>';
      }
    }
  }, 500);
}

/* 后台预拉下一关：结果只挂在 preloaded 上等 doSearch 认领，绝不渲染、不碰输入框。
   认领 = 点「下一关」或键入同一代号时按 key 命中，接着等同一个在飞请求；
   命不中就被 stopSearch 中止——预载跟在途搜索一样，被任何新键入打断。 */
function preloadNext(code, file) {
  const key = file + '|' + code;
  if (searchAbort && searchKey === key) return;   // 下一关已在飞（用户抢先键入了同一代号）
  if (preloaded) { const old = preloaded; preloaded = null; old.ac.abort(); }
  const ac = new AbortController();
  const promise = api('/api/search', { file, q: code, pages: 2, limit: 10 }, { signal: ac.signal });
  promise.catch(() => {});   // 后台失败别当未捕获异常刷屏；真被认领时 await 会正常拿到
  preloaded = { key, ac, promise };
}

/* 「下一关」按钮：搜索结果带着 next（同系列同难度的下一关）就亮、带上代号，
   系列末尾或没搜到就灰。点击 = 填入代号直接搜，下一关通常已预载好、秒出。 */
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
  const key = S.current + '|' + q;
  if (searchAbort && searchKey === key) return;   // 同账号同关键词已在途，别重复发
  // 预载命中（点了「下一关」，或键入的正是预载代号）：认领这个请求接着等，结果秒出
  const pre = (preloaded && preloaded.key === key) ? preloaded : null;
  if (pre) preloaded = null;   // 命中才摘走；没命中留给 stopSearch 中止，别让它漏网白跑
  stopSearch();   // 顶掉仍在飞行的旧搜索与没认领的预载：Enter / 切号 / 拉黑重搜都走这里
  const ac = pre ? pre.ac : new AbortController();
  searchAbort = ac;
  searchKey = key;
  const file0 = S.current;      // 发起搜索时的账号：结束时变了要补搜（#59，静默切号不会中止在途搜索）
  const box = $('#results');
  box.dataset.mode = 'loading';
  box.innerHTML = `<div class="result-head"><div class="result-title">正在检索「${esc(q)}」…<span>比对干员与练度中</span></div></div>
    <div class="loading-row"><span class="spinner"></span>正在从作业站拉取作业，活动关卡多时可能要十几秒</div>
    <div class="ops-grid">${'<div class="skeleton"></div>'.repeat(6)}</div>`;
  try {
    const d = pre ? await pre.promise
      : await api('/api/search', { file: S.current, q, pages: 2, limit: 10 }, { signal: ac.signal });
    S.result = d;
    box.dataset.mode = d.mode;
    renderResult(d);
    setNextBtn(d.next);
    box.scrollTop = 0;
    // 后台预载下一关：输入或账号中途变过就不预了——那是过气结果的下一关
    if (d.next && d.next.code && $('#search-input').value.trim() === q && S.current === file0)
      preloadNext(d.next.code, file0);
  } catch (e) {
    if (ac.signal.aborted) return;   // 被新输入 / 新搜索作废：安静退场，UI 由后来者接管
    box.dataset.mode = 'error';   // 留个非空标记：之后切号仍会触发重搜
    box.innerHTML = `<div class="empty-state"><h3>没搜到</h3><p>${esc(e.message)}</p></div>`;
    setNextBtn(null);
    toast(e.message, 'err', 4000);
  } finally {
    if (searchAbort === ac) { searchAbort = null; searchKey = ''; }
    // 正常完成后输入或账号变了 → 防抖补搜：不然旧账号的比对结果挂在新账号名下。
    // 被中止的不补——中止它的那一方（键入 / 新 doSearch）自会安排后续搜索
    if (!ac.signal.aborted && ($('#search-input').value.trim() !== q || S.current !== file0)) queueAutoSearch();
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

/* 干员 chips 全量铺开（容器 flex-wrap 换行）：折叠成 +N 会把缺的干员藏起来，
   一眼看不出这份作业到底缺谁。命中替换规则（#92，o.sub）的按「可用」上色，
   列表保持无感，由谁顶替只在详情弹窗里说 */
function opChips(op) {
  return visOpers(op.opers).map(o => {
    let cls = 'own';
    if (!o.owned && !o.sub) cls = 'lack';
    else if (!o.ok) cls = 'train';
    return `<span class="op-chip ${cls}" title="${esc(chipTitle(o))}">${esc(o.name)}</span>`;
  }).join('');
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
  if (o.sub) return `由${o.sub.name}${o.sub.skill || ''}替换出战${needTxt}`;
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

// 结果区顶部的剧情关提示：这一段里还有几个剧情关，挂到那里会停。
// MAA 的关卡导航是「关卡列表内右滑找关卡名」的局部搜索，一旦停在剧情界面
// 就不认识任何按钮，只能等重试耗尽然后整队终止——提前说比让用户猜有用
function storyTipHtml(d) {
  const list = d.stories || [];
  if (!list.length) return '';
  const codes = list.map(s => s.code).filter(Boolean);
  return `<div class="story-tip">
    <span class="badge story">剧情</span>
    <span>本段还有 ${list.length} 个剧情关${codes.length ? '：' + codes.map(esc).join('、') : ''}</span>
    <em>挂机会停在这里，需要手动继续</em>
  </div>`;
}

function renderResult(d) {
  // 顺手学作者 id → 昵称：作者黑名单弹窗显示名字靠这个缓存
  (d.stages || []).forEach(s => {
    [s.best].concat(s.ops || []).forEach(op => {
      if (op && op.uploaderId && op.uploader) S.authorNames[op.uploaderId] = op.uploader;
    });
  });
  const box = $('#results');
  // 剧情关：只有剧情没有战斗，作业站上永远没有作业。给一张说明卡，顺带
  // 告诉用户首次通关送一颗源石（以前这类关被整条过滤，搜 9-1 会落空）
  if (d.mode === 'story' && d.story) {
    const s = d.story;
    box.innerHTML = `<div class="empty-state story-card">
      <h3>${esc(s.code || '')} ${esc(s.name || '')}<em class="badge story">剧情</em></h3>
      <p>这一关只有剧情、没有战斗，作业站上没有作业可抄。</p>
      <p class="story-note">首次观看送 <b>1 颗源石</b>。挂机队列经过这里会停顿，需要点一下跳过。</p>
    </div>`;
    return;
  }
  if (!d.stages || !d.stages.length) {
    box.innerHTML = '<div class="empty-state"><h3>没有结果</h3><p>换个关卡代号试试，例如 TL-10 或 act55side</p></div>';
    return;
  }
  const tip = storyTipHtml(d);

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
      box.innerHTML = tip + head + '<div class="empty-state"><h3>这个关卡还没有作业</h3><p>作业站上暂时没人上传，换个关卡吧</p></div>';
      return;
    }
    box.innerHTML = tip + head + '<div class="ops-grid">' +
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
  const opts = importOptions(d.stages);
  const head = `
    <div class="result-head">
      <div class="result-title">${esc(d.title)}
        <span>${codes.length} 关 · ${totalOps} 份作业 · ${readyStages} 关可直接抄${raidRows ? ` · ${raidRows} 个突袭/险地行` : ''}</span>
      </div>
      <div class="result-actions">
        <div class="add-split">
          <button class="ghost-btn mint" id="add-all-best" title="每关只加真正要打的难度：0-8/9 章普通+突袭，10-14 章只加磨难，15 章起只加险地（简单不加；某档没作业就跳过不降档）">全部加入（每关要打的档）</button>
          <button class="ghost-btn mint add-caret" id="add-all-more" title="按难度 / 关卡类型分开导入" aria-haspopup="true" aria-expanded="false">▾</button>
          <div class="add-menu" id="add-menu" hidden>
            ${opts.map((o, i) => `<button class="add-menu-item" data-pick="${i}">${esc(o.label)}</button>`).join('')}
          </div>
        </div>
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
            ${b ? `<button class="mini-btn accent" data-add-id="${b.id}" data-add-variant="${esc(st.variant || '')}" data-stage="${esc(st.stageId)}" data-add-label="加入" data-added-label="已在" ${cls === 'blocked' ? 'title="这一关打不了：批量加入会跳过它；点这里是自己手动加进去"' : ''} ${inBasket(b.id, st.variant) ? 'disabled' : ''}>${inBasket(b.id, st.variant) ? '已在' : '加入'}</button>` : ''}
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

  box.innerHTML = tip + head + '<div class="stage-list">' + rows + '</div>';

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
  // 一关真正要打的难度（用户口径）：0-8 普通+突袭、9 标准+突袭、
  // 10-14 只打磨难、15+ 只打险地。简单永远不打；同一关出现磨难/险地行
  // 时普通/标准行就不算「要打的档」了（不擅自降档：磨难行没作业就空着）
  const hasHardRow = {};
  d.stages.forEach(s => {
    if (s.stage.difficulty === 'tough' || s.stage.difficulty === 'risky')
      hasHardRow[s.stage.code] = true;
  });
  const bestPick = s => s.stage.difficulty !== 'easy'
    && !(s.stage.difficulty === 'normal' && hasHardRow[s.stage.code]);
  const all = $('#add-all-best');
  if (all) all.addEventListener('click', () => addRows(d.stages, bestPick, '每关要打的档'));
  // 倒三角：按难度 / 关卡类型分开导入（选项随结果集变，见 importOptions）
  const more = $('#add-all-more');
  const menu = $('#add-menu');
  if (more && menu) more.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    more.setAttribute('aria-expanded', String(!menu.hidden));
  });
  $$('.add-menu-item', menu).forEach(btn => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const opt = opts[Number(btn.dataset.pick)];
    menu.hidden = true;
    if (opt) addRows(d.stages, opt.pick, opt.label);
  }));
}

/* 「全部加入」菜单选项：按当前结果集的关卡类型给不同档位
   活动（TL 这类）按基础 / EX / S 分；主线按章走三套难度体系
   （0-9 普通+突袭、10-14 简单/标准/磨难、15+ 险地）。选项只影响这次批量
   导入的范围，逐关「加入」按钮照旧 */
function stageIsMain(st) { return /^(?:main|easy|tough)_/.test(st.stageId || ''); }
function stageIsEx(code) { return /-EX-?\d+$/i.test(code || ''); }
function stageIsS(code) { return /-S-?\d+$/i.test(code || ''); }

function importOptions(rows) {
  const first = (rows.find(r => r.stage) || {}).stage;
  if (!first) return [];
  // 主线结果集里会夹着 TR 教学关 / S 支线关（搜章节号 2 时第一行就是 TR-11），
  // 章节号得按第一个主线关算，不能直接看第一行，否则会误判成活动、菜单项全错
  const mainRow = rows.find(r => r.stage && stageIsMain(r.stage));
  if (mainRow) {
    const ch = Number((String(mainRow.stage.code).match(/^(\d+)-/) || [])[1]);
    if (ch >= 10 && ch <= 14) return [
      { label: '全部导入（简单难度关卡）', pick: s => s.stage.difficulty === 'easy' },
      { label: '全部导入（标准难度关卡）', pick: s => s.stage.difficulty === 'normal' },
      { label: '全部导入（磨难难度关卡）', pick: s => s.stage.difficulty === 'tough' },
    ];
    if (ch >= 15) return [
      { label: '全部导入（险地难度关卡）', pick: s => s.stage.difficulty === 'risky' },
      { label: '全部导入（普通关卡）', pick: s => s.stage.difficulty === 'normal' },
    ];
    return [
      { label: '全部导入（普通关卡）', pick: s => s.stage.difficulty === 'normal' && s.stage.variant !== 'raid' },
      { label: '全部导入（突袭关卡）', pick: s => s.stage.variant === 'raid' },
      { label: '全部导入（普通与突袭关卡）', pick: s => s.stage.difficulty === 'normal' },
    ];
  }
  return [
    { label: '全部导入（基础关卡与 TR）', pick: s => !stageIsEx(s.stage.code) && !stageIsS(s.stage.code) },
    { label: '全部导入（EX 关卡普通与突袭）', pick: s => stageIsEx(s.stage.code) },
    { label: '全部导入（S 关卡普通与突袭）', pick: s => stageIsS(s.stage.code) },
  ];
}

/* 批量加入：每行取 best，按「关卡 + 难度行」去重，已在作业集的跳过。
   blocked（打不了）一律不加：一键导入是「照着打一遍」，塞进去也是白写进
   MAA；想收这份作业的人自己点那一行的「加入」，然后手动调顺序 */
function addRows(rows, pick, label) {
  let n = 0;
  let blocked = 0;
  const seen = new Set();
  const prepareIds = [];   // 一键加入的也走隐式预下载（addToBasket 不经过这条路）
  rows.forEach(s => {
    if (!s.best || !pick(s)) return;
    if (s.best.mode === 'blocked') { blocked++; return; }
    const key = s.stage.stageId + '|' + (s.stage.variant || '');
    if (seen.has(key) || inBasket(s.best.id, s.stage.variant || undefined)) return;
    seen.add(key);
    prepareIds.push(s.best.id);
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
  if (prepareIds.length) api('/api/copilot/prepare', { ids: prepareIds }).catch(() => { });
  renderBasket();
  const skipNote = blocked ? `，跳过 ${blocked} 关打不了的` : '';
  if (!n) {
    toast(blocked ? `${label}：${blocked} 关都打不了，没有可加的` : '这些作业都已在作业集里', 'warn');
    return n;
  }
  toast(`${label}：加入 ${n} 份${skipNote}`, blocked ? 'warn' : 'ok');
  return n;
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
          await saveSettings({ [key]: next });
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
        /* 缺谁：owned=false 是没这个人，owned && !ok 是练度不到。
           只有「差一人可借」这一档才说得出具体的借人对象（助战位只能带一个），
           其余情况分开列：没的人和练度不够的人不是一回事。
           命中替换规则（o.sub，#92）的不算缺：写入 MAA 时会被替补顶掉 */
        const bad = d.compared ? shown.filter(o => !o.sub && (o.owned === false || o.ok === false)) : [];
        const borrowOne = (op.mode === 'borrow' && bad.length === 1) ? bad[0].name : '';
        const rest = bad.filter(o => o.name !== borrowOne);
        const lackOwn = rest.filter(o => o.owned === false).map(o => o.name);
        const lackTrain = rest.filter(o => o.owned !== false).map(o => o.name);
        const rows = shown.map(o => {
          const req = o.req || {};   // 与搜索侧同键名（#63）
          const bits = [];
          if (req.elite != null) bits.push('精' + req.elite);
          if (req.level != null) bits.push(req.level + ' 级');
          if (req.skill_level != null) bits.push(req.skill_level >= 8 ? ('专' + (req.skill_level - 7) + ' 技能' + (req.skill || 1)) : ('技能 ' + req.skill_level));
          if (req.module) bits.push('模组 ' + req.module);
          if (req.implied) bits.push('老作业未标注，按星级默认');
          // 名字上色：借的那个人紫、缺练度的橙，跟顶部提示一对；
          // 替换条目（#92）绿字 + 「由黍1替换」标注
          const isBorrow = borrowOne && o.name === borrowOne;
          const isSub = !!o.sub;
          const cls = isBorrow ? ' borrow' : (isSub ? ' subbed'
            : (o.owned === false || o.ok === false ? ' train' : ''));
          const tail = isBorrow ? '<em>借</em>'
            : (isSub ? `<em>由${esc(o.sub.name)}${o.sub.skill || ''}替换</em>`
              : (o.owned === false ? '<em>没有</em>' : (o.ok === false ? '<em>练度不足</em>' : '')));
          return `<div style="display:flex;justify-content:space-between;gap:12px;border-bottom:1px dashed rgba(255,255,255,.06);padding:5px 0">
            <span class="op-name${cls}">${esc(o.name)}${tail}</span>
            <span style="color:var(--text-3);font-size:11.5px">${esc(bits.join(' · ') || '无特殊要求')}</span>
          </div>`;
        }).join('');
        const flags =
          (borrowOne ? `<div class="op-flag borrow">需要借 1 人：<b>${esc(borrowOne)}</b>（把助战位留给他就行，MAA 会自动借）</div>` : '') +
          (lackTrain.length ? `<div class="op-flag train">练度不够：<b>${esc(lackTrain.join('、'))}</b>（按这份作业的要求差了一档，补练度或者换一份）</div>` : '') +
          (lackOwn.length ? `<div class="op-flag train">还缺 <b>${esc(lackOwn.join('、'))}</b>（助战位只够补一个，这份作业现在打不了）</div>` : '');
        const skillless = shown.some(o => o.skillKnown === false);
        $('#op-detail-body', el).innerHTML =
          flags +
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
  // 隐式预下载：加入即把作业 json 取好修好（后台排队，失败不吭声，写入时现场兜底），
  // 点「写入 MAA」时就只剩关 MAA → 写配置 → 重启
  api('/api/copilot/prepare', { ids: [op.id] }).catch(() => { });
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
    const idx = Number(b.dataset.del);
    const item = S.basket[idx];
    S.basket.splice(idx, 1);
    // 悖论作业删出作业集：对应 stage 也从「写完记已导入」清单移除，
    // 不然没写入的悖论会被标成已导入（#59）
    if (item && item.px && item.stageId && (S.paradoxPending || []).includes(item.stageId)) {
      S.paradoxPending = S.paradoxPending.filter(s => s !== item.stageId);
    }
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
    <div class="sub">关闭 MAA → 备份配置 → 写入 ${items.length} 份作业（多数已在加入时预下载）${payload.autoStart ? ' → 重启 MAA' : ''}</div>
    <div class="log-box" id="task-log"></div>
    <div class="modal-foot"><button class="btn-plain" data-close>后台执行</button></div>
  `);

  try {
    const { taskId } = await api('/api/maa/apply', payload);
    await pollTask(taskId, m.el);
    // 写完自动收弹窗：日志末尾的「✓ 完成」给读者留一眼的工夫；
    // 之前这里不关，弹窗会一直挂着等用户手点「后台执行」
    setTimeout(() => m.close(), 900);
    // 按实际写进 MAA 的条目核对后再记「已导入」：paradoxPending 可能带着
    // 别的账号挑的、或已从作业集删掉的 stage（#59）
    const written = new Set(items.map(o => o.stageCode));
    const px = (S.paradoxPending || []).filter(s => written.has(s));
    S.paradoxPending = [];
    S.basket = [];
    renderBasket();
    toggleBasket(false);          // 窄屏写完顺手收起抽屉
    refreshMaaStatus();
    if (S.result) doSearch();
    // 悖论：写完才记「已导入」，下次打开面板默认不勾
    if (px.length) {
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

/* root 里放日志的容器：默认 #task-log；签到面板两块日志各有各的 id，
   所以允许传 sel 指定（否则只能给两块日志复制一套轮询逻辑） */
function pollTask(tid, root, sel) {
  return new Promise((resolve, reject) => {
    let since = 0;
    let done = false;
    const box = root ? $(sel || '#task-log', root) : null;
    const tick = async () => {
      if (done) return;
      // 承载日志的弹窗被关掉了就别再空转（任务本身在后台跑，不用我们管）
      if (root && root.isConnected === false) { done = true; return; }
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
  let aaGoBusy = false;
  $('#aa-go', m).addEventListener('click', async () => {
    // 第一行就锁：btn.disabled 要等 probe/存目录两个 await 之后才置上，
    // 窗口期内双击会启动两条 token 同步循环，交错写同一个日志框（#59）
    if (aaGoBusy) return;
    aaGoBusy = true;
    try {
      await aaGoRun();
    } finally {
      aaGoBusy = false;
    }
  });
  const aaGoRun = async () => {
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
      }
      const acc = await api('/api/accounts');
      S.accounts = acc.accounts;
      renderAccounts();
      renderMaaState();
      const st = await api('/api/settings', {});
      S.settings = st.settings;

      // 用 MAA 识别结果兜底练度，只在「只加了一个号、且它同步后没有练度」时做。
      // OperBoxData.json 一个 MAA 目录只有一份，就是当时登录在 MAA 里的那个号：
      // 批量加号时挨个导入，会把所有账号写成同一份练度（2026-11-15 反馈「添加
      // 两个账号显示同一个练度」）；而 token 同步来的练度含技能/专精/模组，
      // 被本地识别顶掉是纯降级。需要覆盖时走账号页的手动导入（那里会确认）。
      if (dir && operboxReady) {
        const ok = added.filter(Boolean);
        const one = ok.length === 1 ? ok[0] : '';
        const hasChars = one
          && (((S.accounts.find(a => a.file === one) || {}).stats || {}).charCount || 0) > 0;
        if (one && !hasChars) {
          taskLogLine(logBox, `从 MAA 导入干员信息（${one}）…`);
          try {
            const r = await api('/api/account/import-maa', { file: one });
            if (r.skippedBy === 'token') {
              taskLogLine(logBox, `· 跳过 MAA 识别结果：${r.reason || '练度已是森空岛同步的结果'}`);
            } else {
              importedTotal += r.imported || 0;
              taskLogLine(logBox, `✓ 已导入 ${r.imported || 0} 名干员${r.skipped ? `（跳过 ${r.skipped} 名）` : ''}，来源 ${r.source === 'yituliu' ? '一图流' : 'MAA 本地识别'}`);
            }
          } catch (e) {
            taskLogLine(logBox, `✗ ${one} 干员信息导入失败：${e.message}`, true);
          }
        } else if (ok.length > 1) {
          taskLogLine(logBox, '! 一次添加了多个账号，未从 MAA 导入练度——MAA 只有一份识别结果（当时登录的那个号），套到多个账号上会显示同一个练度', true);
        } else {
          taskLogLine(logBox, '· 练度已由森空岛同步，未用 MAA 识别结果覆盖（本地识别不含技能/专精/模组；确需替换可在账号页手动导入）');
        }
      } else if (dir) {
        taskLogLine(logBox, '! 该 MAA 还没有干员识别数据（data/OperBoxData.json），可先在 MAA 小工具里识别一次，之后从森空岛页手动导入', true);
      }
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
  };

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

/* ------------------------------------------------------------ 设置
   2026-11-18：一列到底改成「左侧 tab + 右侧内容」。
   原来账号 / MAA / 版本 / 助手 / 目录 / 黑名单全堆在一个 520px 的弹窗里，
   一滚就不知道上面还有什么，二级弹窗套二级弹窗（助手、黑名单）也难找。
   现在按「管什么」拆成 6 页，每页自己滚；页内改动即时保存，不再有跨页的
   一个大「保存」——改了 MAA 又切到版本页，很容易把前一次保存漏掉。 */

const SET_ICONS = {
  accounts: '<path d="M8 8.1a2.55 2.55 0 1 0 0-5.1 2.55 2.55 0 0 0 0 5.1Z"/>'
    + '<path d="M3.2 13.4c0-2.05 2.15-3.35 4.8-3.35s4.8 1.3 4.8 3.35"/>',
  maa: '<rect x="2.4" y="4.4" width="11.2" height="8.6" rx="1.4"/>'
    + '<path d="M5.2 7.3 6.9 9 5.2 10.7M8.5 10.7h2.2"/>',
  update: '<path d="M13.1 8a5.1 5.1 0 1 1-1.55-3.63"/><path d="M13.3 2.7v3.6H9.7"/>',
  agent: '<path d="M8 2.6 9.35 5.9 12.6 7.25 9.35 8.6 8 11.9 6.65 8.6 3.4 7.25 6.65 5.9Z"/>'
    + '<path d="M12.4 11.2v2.2M11.3 12.3h2.2"/>',
  data: '<ellipse cx="8" cy="4.5" rx="5" ry="1.9"/>'
    + '<path d="M3 4.5v6.9c0 1.05 2.24 1.9 5 1.9s5-.85 5-1.9V4.5"/>'
    + '<path d="M3 8c0 1.05 2.24 1.9 5 1.9s5-.85 5-1.9"/>',
  blacklist: '<circle cx="8" cy="8" r="5.2"/><path d="M4.35 11.65 11.65 4.35"/>',
};

function setIcon(k) {
  return `<svg class="si" viewBox="0 0 16 16" fill="none" stroke="currentColor"
    stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round">${
    SET_ICONS[k] || ''}</svg>`;
}

function fmtSync(t) {
  return t ? String(t).replace('T', ' ').slice(0, 16) : '—';
}

const SET_TABS = [
  ['accounts', '账号管理'],
  ['maa', 'MAA 路径'],
  ['update', '版本更新'],
  ['agent', '助手设置'],
  ['data', '数据与日志'],
  ['blacklist', '黑名单'],
];

function openSettings() {
  const s = S.settings;
  const p = S.paths || {};
  const pk = S.pack || {};
  const gd = S.gamedata || {};
  const counts = pk.counts || {};
  // 目录按钮一律渲染：桌面窗口里能直接打开，浏览器预览里点了给一句提示
  // （早先是 nativeApi 存在才渲染，结果网页里这一栏看着像缺了按钮）
  const openBtn = (which, label) =>
    `<button class="btn-plain mini" data-open="${which}">${label}</button>`;
  // 代理拆成 地址+端口 两框回填；没配过给地址预填本机默认，端口空=直连
  const pv = String(s.proxy || '').trim();
  const pm = pv.match(/^(.*?):(\d+)$/);
  const proxyHost = pm ? pm[1] : 'http://127.0.0.1';
  const proxyPort = pm ? pm[2] : '';

  const acctRows = () => (S.accounts || []).map(a => {
    const st = a.stats || {};
    const ef = (a.game || 'arknights') === 'endfield';
    const ch = (a.nickName || '?').replace(/[#＃].*$/, '').slice(0, 1);
    const cls = st.sixStarCount >= 120 ? 'r6' : (st.sixStarCount >= 60 ? 'r5' : '');
    const chName = a.channelName || '官服';
    const chCls = /bilibili/i.test(chName) ? ' bili' : (/^官服/.test(chName) ? ' official' : '');
    return `
    <div class="set-card set-acct" data-file="${esc(a.file)}">
      <div class="set-acct-top">
        <span class="avatar ${cls}">${esc(ch)}</span>
        <div class="set-acct-id">
          <div class="set-acct-name">${esc(a.nickName)}
            <span class="acct-ch${chCls}">${esc(chName)}</span>
            ${ef ? '<i class="acct-tag">终末地</i>' : ''}</div>
          <div class="set-acct-meta">UID ${esc(a.uid || '—')} · ${
            ef ? 'Lv' + esc(a.level || '?') : `${st.charCount || 0}/${st.charTotal || 440} 干员`
          } · 同步于 ${esc(fmtSync(a.syncedAt))}</div>
        </div>
        <button class="btn-plain mini set-danger" data-act="del">删除账号</button>
      </div>
      <div class="set-token">
        <div class="set-token-head"><span>森空岛 token</span>
          <em class="set-token-state" data-state>读取中…</em></div>
        <div class="path-row">
          <input type="text" data-token readonly spellcheck="false" autocomplete="off">
          <button class="btn-plain mini" data-act="reveal" disabled>显示</button>
          <button class="btn-plain mini" data-act="check" disabled>验证</button>
          <button class="btn-plain mini" data-act="edit" disabled>修改</button>
          <button class="btn-plain mini" data-act="save" hidden>保存</button>
          <button class="btn-plain mini" data-act="cancel" hidden>取消</button>
        </div>
        <div class="set-tip" data-tip></div>
      </div>
    </div>`;
  }).join('') || '<div class="onb-hint">还没有账号。用左上角「添加账号」粘一个森空岛 token，或直接从 MAA 导入。</div>';

  const maaRows = () => (S.accounts || []).map(a => `
    <div class="set-card set-maarow" data-file="${esc(a.file)}">
      <div class="set-maarow-head">
        <b>${esc(a.nickName)}</b>
        <span class="set-muted">${esc(a.channelName || '官服')}</span>
        ${a.maaProfile ? `<span class="set-chip">档案 ${esc(a.maaProfile)}</span>` : ''}
      </div>
      <label class="field"><span>切号名（手机号后 4 位；agent 启动这个账号时写进 MAA「开始唤醒」的就是它）</span>
        <input type="text" data-f="accountName" value="${esc(a.maaAccountName || '')}"
               placeholder="后 4 位，如 5505" spellcheck="false" autocomplete="off"></label>
      <label class="field"><span>这个账号的 MAA 路径（留空 = 用上面的默认目录）</span>
        <div class="path-row">
          <input type="text" data-f="dir" value="${esc(a.maaDir || '')}" placeholder="${esc(s.maaDir || '沿用默认目录')}">
          <button class="btn-plain mini" data-act="pick">浏览</button>
          <button class="btn-plain mini" data-act="probe">检测</button>
          <button class="btn-plain mini" data-act="profiles">读档案</button>
        </div></label>
      <div class="set-maarow-foot">
        <span class="onb-hint" data-state></span>
        <button class="btn-solid mini" data-act="save">保存</button>
      </div>
    </div>`).join('') || '<div class="onb-hint">还没有账号。</div>';

  // 条数要读活的 S.settings：子弹窗里增删后 saveSettings 会换掉整个对象，
  // 拿打开设置那一刻的引用（s）去数会一直停在旧值
  const blOf = () => {
    const cur = S.settings || {};
    return {
      char: (cur.charBlacklist || []).length,
      author: (cur.authorBlacklist || []).length,
      op: (cur.opBlacklist || []).length,
    };
  };
  const bl = blOf();

  modal(`
    <div class="set-shell">
      <aside class="set-side">
        <div class="set-side-title">设置</div>
        ${SET_TABS.map(([id, name], i) => `
          <button class="set-tab${i === 0 ? ' active' : ''}" data-tab="${id}">
            ${setIcon(id)}<span>${name}</span><span class="set-tab-tail">${
              id === 'update'
                ? '<i class="set-tab-dot" data-tab-dot="update" hidden></i>' : ''
            }<em class="set-tab-n" data-tab-n="${id}"></em></span>
          </button>`).join('')}
        <div class="set-side-foot">改动即时生效<br>数据都留在本机</div>
      </aside>
      <section class="set-body">
        <div class="set-panes">

          <div class="set-pane active" data-pane="accounts">
            <div class="set-head">
              <h3>账号管理</h3>
              <p><b>token</b> 是森空岛的账号凭证，签到、抽卡统计、后台同步都靠它——加密存在本机账号文件里
                （Windows DPAPI，仅本机当前用户可解），不会上传。默认只读，只有「验证」确认失效后才解锁修改。
                删除账号会连练度快照一起删掉，不可恢复。</p>
            </div>
            <div class="set-acct-list" id="set-acct-list">${acctRows()}</div>
          </div>

          <div class="set-pane" data-pane="maa">
            <div class="set-head">
              <h3>MAA 路径</h3>
              <p>每个账号可以绑自己的 MAA（包含 <code>MAA.exe</code> 的目录），没绑就用下面的默认目录。
                <b>切号名</b>是 MAA「开始唤醒 → 账号切换」里要比对的那行：官服填手机号后 4 位（如 <code>5505</code>），
                B 服填游戏内用户名。填错的代价是 MAA 在账号列表里死找、4 分钟超时后整轮挂机作废。</p>
            </div>
            <div class="set-card">
              <label class="field"><span>默认 MAA 根目录（账号没单独绑定就用它）</span>
                <div class="path-row">
                  <input type="text" id="set-maa" value="${esc(s.maaDir || '')}">
                  <button class="btn-plain mini" data-pick="set-maa">浏览</button>
                  <button class="btn-plain mini" id="set-probe">检测</button>
                  <button class="btn-plain mini" id="set-maa-save">保存</button>
                </div></label>
              <div class="onb-hint" id="set-maa-state">${
                S.maa && S.maa.exeExists ? '✓ MAA.exe 已找到' : '✗ 尚未检测到 MAA.exe'}</div>
              <label class="check-row" style="margin-top:12px">
                <input type="checkbox" id="set-autostart" ${s.autoStartMaa ? 'checked' : ''}>
                <span>写完作业后自动启动 MAA</span>
              </label>
            </div>
            <div class="set-sub-title">各账号的绑定</div>
            <div class="set-maa-list" id="set-maa-list">${maaRows()}</div>
          </div>

          <div class="set-pane" data-pane="update">
            <div class="set-head">
              <h3>版本更新</h3>
              <p>两条链各自独立：<b>游戏资源</b>是关卡 / 干员 / 活动数据（几百 KB，随程序发版更新），
                <b>程序本体</b>是 MAA-PRTS 自己（从 GitHub Releases 取，有新版本只提醒，装不装你说了算）。</p>
            </div>
            <div class="set-card">
              <div class="set-ver">
                <div class="set-ver-id">
                  <b>游戏资源</b>
                  <code id="set-pack-msg">v${esc(gd.version || pk.version || '?')} · ${
                    counts.chars || 0} 干员 · ${counts.stages || 0} 关卡</code>
                </div>
                <button class="btn-plain mini" id="set-pack-check">检查更新</button>
              </div>
              <div class="set-ver">
                <div class="set-ver-id">
                  <b>程序版本</b>
                  <code id="set-app-msg">MAA-PRTS ${esc(
                    gd.app || (S.appInfo && S.appInfo.version) || '')}${appVerSuffix()}</code>
                </div>
                <button class="btn-plain mini" id="set-app-check">${
                  S.appUpdate && S.appUpdate.ok && S.appUpdate.hasUpdate ? '查看新版本' : '检查更新'}</button>
              </div>
              <label class="check-row" style="margin-top:14px">
                <input type="checkbox" id="set-autopack" ${s.autoUpdatePack === false ? '' : 'checked'}>
                <span>启动后自动更新游戏资源（新关卡 / 新干员 / 新活动）</span>
              </label>
              <label class="check-row" style="margin-top:10px">
                <input type="checkbox" id="set-autocheck" ${s.autoCheckAppUpdate === false ? '' : 'checked'}>
                <span>启动后检查程序新版本（6 小时最多一次，只提醒）</span>
              </label>
            </div>
            <div class="set-sub-title">网络代理</div>
            <div class="set-card">
              <div class="set-tip" style="margin-bottom:10px">只在连不上 GitHub 时才需要。仅用于上面两条检查 / 下载链路；
                端口留空 = 直连。改完点「保存并测试」，不可用也会保存（更新时会自动直连回退）。</div>
              <div class="path-row">
                <input type="text" id="set-proxy-host" value="${esc(proxyHost)}" placeholder="http://127.0.0.1">
                <input type="text" id="set-proxy-port" value="${esc(proxyPort)}" placeholder="7890"
                       inputmode="numeric" style="flex:0 0 92px">
                <button class="btn-plain mini" id="set-proxy-save">保存并测试</button>
              </div>
            </div>
          </div>

          <div class="set-pane" data-pane="agent">
            <div class="set-head">
              <h3>助手设置</h3>
              <p>内置助手用 OpenAI 兼容协议，DeepSeek / 通义 / Kimi / 中转站 / 本地 Ollama 都能填。
                API Key 用 Windows DPAPI 加密存在本机，换电脑解不开、也不会上传。配好之后在顶部「助手」标签页里对话。</p>
            </div>
            <div class="set-card">
              <label class="field"><span>Base URL（ai 的接口地址）</span>
                <input type="text" id="set-ag-url" placeholder="https://api.deepseek.com/v1" spellcheck="false"></label>
              <label class="field"><span>模型名</span>
                <input type="text" id="set-ag-model" placeholder="deepseek-chat" spellcheck="false"></label>
              <label class="field"><span>API Key</span>
                <div class="path-row">
                  <input type="password" id="set-ag-key" value="" autocomplete="off" spellcheck="false">
                  <button class="btn-plain mini" id="set-ag-clear" disabled>清除</button>
                </div></label>
              <label class="check-row">
                <input type="checkbox" id="set-ag-auto">
                <span>写 MAA 时不再问我，直接执行（关掉更安全：每次写入前都弹确认）</span>
              </label>
              <div class="set-maarow-foot">
                <span class="onb-hint" id="set-ag-state"></span>
                <button class="btn-solid mini" id="set-ag-save">保存</button>
              </div>
            </div>
          </div>

          <div class="set-pane" data-pane="data">
            <div class="set-head">
              <h3>数据与日志</h3>
              <p>账号快照、配置、日志都在本机，删程序不会带走；想备份就整目录拷走。
                日志是纯文本，报问题时把 <code>app.log</code> 发过来最有用。</p>
            </div>
            <div class="set-card">
              <div class="path-list" style="border-top:none;padding-top:0">
                <div class="path-item"><span>账号数据</span><code>${esc(p.accounts || '')}</code>${openBtn('accounts', '打开')}</div>
                <div class="path-item"><span>配置目录</span><code>${esc(p.config || '')}</code>${openBtn('config', '打开')}</div>
                <div class="path-item"><span>日志目录</span><code>${esc(p.logs || '')}</code>${openBtn('logs', '打开')}</div>
                <div class="path-item"><span>运行根目录</span><code>${esc(p.root || '')}</code>${openBtn('root', '打开')}</div>
              </div>
              <div class="set-tip" style="margin-top:12px">账号数据与配置在同一个目录（每个账号一个 json，
                同目录还有 <code>settings.json</code> 与 <code>navcache.json</code>）。token 密文就在各自账号 json 的
                <code>skd</code> 段里，和账号绑定——拷到别的电脑解不开是正常的。</div>
            </div>
          </div>

          <div class="set-pane" data-pane="blacklist">
            <div class="set-head">
              <h3>黑名单</h3>
              <p>黑名单只影响<b>显示与推荐</b>：被拉黑的干员 / 作者 / 作业不会出现在搜索结果里，
                账号数据、练度比对结论都不受影响。三类互不相干，按需要各管各的。</p>
            </div>
            <div class="set-bls">
              <div class="set-card set-bl">
                <div class="set-bl-main">
                  <b>干员黑名单 <span class="set-chip" data-bl-count="char">${bl.char}</span></b>
                  <div class="set-tip">屏蔽某个干员（或它的某几个技能）参与的作业：搜索与详情里不再显示，
                    但比对结论不变。适合「没练 / 不想用」的干员。</div>
                </div>
                <button class="btn-plain mini" data-bl="char">管理</button>
              </div>
              <div class="set-card set-bl">
                <div class="set-bl-main">
                  <b>作者黑名单 <span class="set-chip" data-bl-count="author">${bl.author}</span></b>
                  <div class="set-tip">按作业站作者屏蔽：这个作者发的所有作业都不再出现。
                    适合「路线不合口味 / 老是要求高练度」的作者。</div>
                </div>
                <button class="btn-plain mini" data-bl="author">管理</button>
              </div>
              <div class="set-card set-bl">
                <div class="set-bl-main">
                  <b>作业黑名单 <span class="set-chip" data-bl-count="op">${bl.op}</span></b>
                  <div class="set-tip">按 MAA 代码（<code>maa://12345</code>）屏蔽单个作业，
                    软件级、不跟账号走。作业详情里点「拉黑作业」也会加到这儿。</div>
                </div>
                <button class="btn-plain mini" data-bl="op">管理</button>
              </div>
            </div>
          </div>

        </div>
        <div class="set-foot">
          <span>设置修改后立即生效，没有总的「保存」按钮。</span>
          <button class="btn-plain" data-close>关闭</button>
        </div>
      </section>
    </div>
  `, {
    cls: 'modal-set',
    onMount(el, close) {
      /* ---------------- 页签 ---------------- */
      const panes = $$('.set-pane', el);
      const tabs = $$('.set-tab', el);
      const show = id => {
        tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === id));
        panes.forEach(p2 => p2.classList.toggle('active', p2.dataset.pane === id));
        if (id === 'accounts') {
          $$('.set-acct', el).forEach(card => { if (!card.dataset.loaded) fillToken(card); });
        }
        if (id === 'maa') suggestNames();
        if (id === 'blacklist') paintBlCounts();
      };
      tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.tab)));

      // 页签上的小数字：账号数、黑名单总条数——不点进去也知道有没有东西
      const paintTabN = () => {
        const an = $('[data-tab-n="accounts"]', el);
        if (an) an.textContent = (S.accounts || []).length || '';
        const c = blOf();
        const bn = $('[data-tab-n="blacklist"]', el);
        if (bn) bn.textContent = (c.char + c.author + c.op) || '';
      };
      paintTabN();
      // 打开设置的那一刻可能已经查到新版本了（启动 4 秒后查一次），
      // 红点要立刻亮，不能等下一次轮询
      renderAppDot();

      /* ---------------- 通用：打开目录 / 选目录 ---------------- */
      el.addEventListener('click', async e => {
        const ob = e.target.closest('[data-open]');
        if (ob) {
          const api2 = nativeApi();
          if (api2 && api2.open_folder) {
            const r = await api2.open_folder(ob.dataset.open);
            if (r && r.ok === false) toast(r.error || '打开失败', 'err');
          } else {
            toast('当前不是桌面窗口，无法打开目录', 'err');
          }
          return;
        }
        const pk2 = e.target.closest('[data-pick]');
        if (pk2) {
          const input = $('#' + pk2.dataset.pick, el);
          if (input) await pickFolder(input);
        }
      });

      /* ---------------- 账号管理：token ---------------- */
      function setState(card, txt, cls) {
        const box = $('[data-state]', card);
        box.textContent = txt || '';
        box.className = 'set-token-state' + (cls ? ' ' + cls : '');
      }
      function setTip(card, txt, cls) {
        const box = $('[data-tip]', card);
        box.textContent = txt || '';
        box.className = 'set-tip' + (cls ? ' ' + cls : '');
      }
      // 掩码显示成「••••••oKPE」：后端给的是「…oKPE」，直接摆出来像个
      // 被截断的字符串，补几个圆点才看得出「这是一段被藏起来的凭证」
      const masked = m => (m && m.charAt(0) === '…'
        ? '•'.repeat(16) + m.slice(1) : (m || ''));
      function paintToken(card, d) {
        const input = $('[data-token]', card);
        const edit = $('[data-act="edit"]', card);
        const rev = $('[data-act="reveal"]', card);
        const chk = $('[data-act="check"]', card);
        card.dataset.token = d.token || '';
        card.dataset.mask = d.mask || '';
        card.dataset.reveal = '0';
        input.readOnly = true;
        input.value = d.has ? (d.mask ? masked(d.mask) : '（密文）') : '';
        input.placeholder = d.has ? '' : '这个账号没有 token';
        rev.disabled = !d.token;
        rev.textContent = '显示';
        chk.disabled = !d.token;
        if (!d.has) {
          setState(card, '没有 token', 'bad');
          setTip(card, '用 MAA 干员识别导入的账号没有森空岛 token，签到 / 抽卡统计用不了；'
            + '点「添加」粘一个就行（同一账号练度数据不会被覆盖）。');
          edit.disabled = false;
          edit.textContent = '添加';
        } else if (d.broken) {
          setState(card, '✗ 密文解不开', 'bad');
          setTip(card, d.error || '本机解不开这份密文');
          edit.disabled = false;
          edit.textContent = '重填';
        } else {
          setState(card, '已保存 · ' + (d.mask || ''), '');
          setTip(card, '默认只读。点「验证」确认还能用；确认失效后才会解锁「修改」。');
          edit.disabled = true;
          edit.textContent = '修改';
        }
      }
      async function fillToken(card) {
        card.dataset.loaded = '0';
        try {
          const d = await api('/api/account/token', { file: card.dataset.file });
          paintToken(card, d);
          card.dataset.loaded = '1';
        } catch (e2) {
          setState(card, '读取失败', 'bad');
          setTip(card, e2.message, 'bad');
        }
      }
      function enterEdit(card) {
        const input = $('[data-token]', card);
        input.readOnly = false;
        input.value = '';
        input.placeholder = '粘贴新的森空岛 token';
        input.focus();
        ['reveal', 'check', 'edit'].forEach(a2 => { $('[data-act="' + a2 + '"]', card).hidden = true; });
        ['save', 'cancel'].forEach(a2 => { $('[data-act="' + a2 + '"]', card).hidden = false; });
        setState(card, '等待新 token', '');
        setTip(card, '粘进来点保存：会先验证 token 是否有效、绑的是不是这个号，确认后才写回账号文件。');
      }
      function exitEdit(card) {
        ['reveal', 'check', 'edit'].forEach(a2 => { $('[data-act="' + a2 + '"]', card).hidden = false; });
        ['save', 'cancel'].forEach(a2 => { $('[data-act="' + a2 + '"]', card).hidden = true; });
        fillToken(card);
      }

      $('#set-acct-list', el).addEventListener('click', async ev => {
        const card = ev.target.closest('.set-acct');
        const btn = ev.target.closest('[data-act]');
        if (!card || !btn) return;
        const file = card.dataset.file;
        const act = btn.dataset.act;
        const input = $('[data-token]', card);
        if (act === 'reveal') {
          const on = card.dataset.reveal === '1';
          card.dataset.reveal = on ? '0' : '1';
          input.value = on ? masked(card.dataset.mask) : (card.dataset.token || '');
          btn.textContent = on ? '显示' : '隐藏';
          return;
        }
        if (act === 'edit') { enterEdit(card); return; }
        if (act === 'cancel') { exitEdit(card); return; }
        if (act === 'save') {
          const token = input.value.trim();
          if (!token) { toast('先粘贴 token', 'warn'); input.focus(); return; }
          btn.disabled = true;
          btn.textContent = '验证中…';
          try {
            const d = await api('/api/account/token/set', { file, token });
            ['save', 'cancel'].forEach(a2 => { $('[data-act="' + a2 + '"]', card).hidden = true; });
            ['reveal', 'check', 'edit'].forEach(a2 => { $('[data-act="' + a2 + '"]', card).hidden = false; });
            paintToken(card, { has: true, token, mask: d.mask, broken: false, error: '' });
            setState(card, '✓ 已更新 · ' + (d.mask || ''), 'ok');
            toast('token 已更新');
          } catch (e2) {
            toast('保存失败：' + e2.message, 'err', 6000);
          } finally {
            btn.disabled = false;
            btn.textContent = '保存';
          }
          return;
        }
        if (act === 'check') {
          btn.disabled = true;
          const old = btn.textContent;
          btn.textContent = '验证中…';
          setState(card, '验证中…（最长十几秒）', '');
          try {
            const d = await api('/api/account/token/check', { file });
            if (d.ok) {
              setState(card, '✓ 有效', 'ok');
              setTip(card, 'token 还能用，不用改。');
            } else {
              setState(card, '✗ 已失效', 'bad');
              setTip(card, (d.error || 'token 已失效') + '——点「修改」粘贴新的。', 'bad');
              const edit = $('[data-act="edit"]', card);
              edit.disabled = false;
              edit.textContent = '修改';
            }
          } catch (e2) {
            setState(card, '验证失败（网络？）', 'bad');
            setTip(card, e2.message + '：没确认失效就不解锁修改，可以稍后再试。', 'bad');
          } finally {
            btn.disabled = false;
            btn.textContent = old;
          }
          return;
        }
        if (act === 'del') {
          deleteAccount(file, () => {
            $('#set-acct-list', el).innerHTML = acctRows();
            $$('.set-acct', el).forEach(c => fillToken(c));
            // MAA 那页用的是另一份列表，删号后也得跟着少一行
            $('#set-maa-list', el).innerHTML = maaRows();
            paintBlCounts();
            paintTabN();
          });
        }
      });

      /* ---------------- MAA 路径 ---------------- */
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
        } catch (e2) {
          box.className = 'onb-hint bad';
          box.textContent = '检测失败：' + e2.message;
        }
      });
      $('#set-maa-save', el).addEventListener('click', async () => {
        const dir = $('#set-maa', el).value.trim();
        const btn = $('#set-maa-save', el);
        btn.disabled = true;
        try {
          const d = await saveSettings({ maaDir: dir });
          S.settings = d.settings || S.settings;
          renderMaaState();
          fillBasketProfiles();
          toast('默认 MAA 目录已保存');
        } catch (e2) {
          toast('保存失败：' + e2.message, 'err');
        } finally { btn.disabled = false; }
      });
      $('#set-autostart', el).addEventListener('change', async ev => {
        try { await saveSettings({ autoStartMaa: ev.target.checked }); }
        catch (e2) { ev.target.checked = !ev.target.checked; toast('保存失败：' + e2.message, 'err'); }
      });

      $('#set-maa-list', el).addEventListener('click', async ev => {
        const card = ev.target.closest('.set-maarow');
        const btn = ev.target.closest('[data-act]');
        if (!card || !btn) return;
        const file = card.dataset.file;
        const input = $('[data-f="dir"]', card);
        const nameInput = $('[data-f="accountName"]', card);
        const state = $('[data-state]', card);
        if (btn.dataset.act === 'pick') { await pickFolder(input); return; }
        if (btn.dataset.act === 'probe' || btn.dataset.act === 'profiles') {
          const dir = input.value.trim() || (S.settings.maaDir || '');
          if (!dir) { state.className = 'onb-hint bad'; state.textContent = '先填目录（或设置默认目录）'; return; }
          state.className = 'onb-hint';
          state.textContent = '检测中…';
          try {
            const r = await api('/api/maa/probe?dir=' + encodeURIComponent(dir));
            if (!r.exeExists) {
              state.className = 'onb-hint bad';
              state.textContent = '✗ 这个目录里没有 MAA.exe';
              return;
            }
            state.className = 'onb-hint ok';
            state.textContent = `✓ MAA.exe 在，配置 ${r.profiles.length} 个`;
            if (btn.dataset.act === 'profiles') {
              const pf = await api('/api/maa/profiles?file=' + encodeURIComponent(file));
              const rows = pf.profiles || [];
              const acc = (S.accounts || []).find(x => x.file === file) || {};
              const hit = rows.find(x => x.name === acc.maaProfile)
                || rows.find(x => x.name === pf.current)
                || (rows.length === 1 ? rows[0] : null);
              if (hit && hit.accountName) {
                nameInput.value = hit.accountName;
                state.textContent = `✓ 从档案「${hit.name}」读到切号名 ${hit.accountName}，点保存生效`;
              } else if (rows.length) {
                toast('MAA 里有这些档案：' + rows.map(x => `${x.name}（${x.accountName || '空'}）`).join('、')
                  + '，没敢替你猜，手动填一下切号名', 'warn', 7000);
                state.textContent = '✓ 读到 ' + rows.length + ' 个档案，切号名请手动确认';
              } else {
                state.className = 'onb-hint bad';
                state.textContent = '这个 MAA 里还没有配置档案';
              }
            }
          } catch (e2) {
            state.className = 'onb-hint bad';
            state.textContent = '检测失败：' + e2.message;
          }
          return;
        }
        if (btn.dataset.act === 'save') {
          btn.disabled = true;
          btn.textContent = '保存中…';
          try {
            const patch = { dir: input.value.trim(), accountName: nameInput.value.trim() };
            const r = await api('/api/account/maa', { file, patch });
            const row = (S.accounts || []).find(x => x.file === file);
            if (row) {
              row.maaDir = (r.maa || {}).dir || '';
              row.maaAccountName = (r.maa || {}).accountName || '';
            }
            state.className = 'onb-hint ok';
            state.textContent = '✓ 已保存';
            toast('「' + ((row && row.nickName) || '账号') + '」的 MAA 绑定已保存');
          } catch (e2) {
            state.className = 'onb-hint bad';
            state.textContent = '✗ ' + e2.message;
            toast('保存失败：' + e2.message, 'err', 6000);
          } finally {
            btn.disabled = false;
            btn.textContent = '保存';
          }
        }
      });

      /* ---------------- MAA 绑定：没填切号名时给个提示 ----------------
         切号名是唯一「必须问用户」的字段，猜错的代价又特别大（MAA 在账号
         列表里死找、4 分钟超时）。所以不做自动填写，只在 MAA 档案里恰好
         记着的时候提一句，让用户自己点「读档案」确认。 */
      async function suggestNames() {
        for (const card of $$('.set-maarow', el)) {
          if (card.dataset.sug || $('[data-state]', card).textContent) continue;
          if ($('[data-f="accountName"]', card).value.trim()) continue;
          card.dataset.sug = '1';
          try {
            const pf = await api('/api/maa/profiles?file=' + encodeURIComponent(card.dataset.file));
            const rows = pf.profiles || [];
            const acc = (S.accounts || []).find(x => x.file === card.dataset.file) || {};
            const hit = rows.find(x => x.name === acc.maaProfile)
              || rows.find(x => x.name === pf.current);
            if (hit && hit.accountName) {
              const st = $('[data-state]', card);
              st.className = 'onb-hint';
              st.textContent = `MAA 档案「${hit.name}」里记着切号名 ${hit.accountName}，点「读档案」填入`;
            }
          } catch (e2) { /* 没装 MAA / 配置读不到：不打扰 */ }
        }
      }

      /* ---------------- 版本更新 ---------------- */
      $('#set-pack-check', el).addEventListener('click', async () => {
        const btn = $('#set-pack-check', el);
        const msg = $('#set-pack-msg', el);
        btn.disabled = true;
        msg.textContent = '检查中…';
        try {
          const r = await packCheckNow();
          msg.textContent = r.text;
          toast(r.text, r.ok ? 'ok' : 'err', 4000);
        } catch (e2) {
          msg.textContent = '检查失败';
          toast('检查失败：' + e2.message, 'err');
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
        } catch (e2) {
          msg.textContent = old;
          toast('检查失败：' + e2.message, 'err');
        } finally {
          btn.disabled = false;
        }
      });
      $('#set-autopack', el).addEventListener('change', async ev => {
        try { await saveSettings({ autoUpdatePack: ev.target.checked }); }
        catch (e2) { ev.target.checked = !ev.target.checked; toast('保存失败：' + e2.message, 'err'); }
      });
      $('#set-autocheck', el).addEventListener('change', async ev => {
        try { await saveSettings({ autoCheckAppUpdate: ev.target.checked }); }
        catch (e2) { ev.target.checked = !ev.target.checked; toast('保存失败：' + e2.message, 'err'); }
      });
      $('#set-proxy-save', el).addEventListener('click', async () => {
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
        const btn = $('#set-proxy-save', el);
        btn.disabled = true;
        try {
          await saveSettings({ proxy });
          if (!proxy) { toast('已改为直连'); return; }
          try {
            const r = await api('/api/proxy/probe', { proxy });
            toast(r.ok ? `代理可用（${r.tcpMs}ms 连上）`
              : `代理不可用：${r.error}。已保存，更新时自动直连`,
              r.ok ? 'ok' : 'warn', 5200);
          } catch (e2) {
            toast('代理已保存，但检测失败：' + e2.message, 'warn', 5200);
          }
        } catch (e2) {
          toast('保存失败：' + e2.message, 'err');
        } finally { btn.disabled = false; }
      });

      /* ---------------- 助手设置 ---------------- */
      const ag = S.agent || {};
      const agState = $('#set-ag-state', el);
      const paintAgent = d => {
        agState.className = 'onb-hint ' + (d && d.configured ? 'ok' : 'bad');
        agState.textContent = !d ? ''
          : d.configured ? `✓ 已就绪，用 ${d.model}`
            : (d.hasKey ? '✗ 还差模型名' : '✗ 还差 API Key');
      };
      const fillAgent = d => {
        if (!d) return;
        $('#set-ag-url', el).value = d.baseUrl || '';
        $('#set-ag-model', el).value = d.model || '';
        $('#set-ag-auto', el).checked = !!d.autoApply;
        $('#set-ag-clear', el).disabled = !d.hasKey;
        $('#set-ag-key', el).value = '';
        $('#set-ag-key', el).placeholder = d.hasKey ? '已保存，留空不改' : 'sk-...';
        paintAgent(d);
      };
      (async () => {
        try {
          const d = await api('/api/agent/status');
          S.agent = d;
          fillAgent(d);
        } catch (e2) { agState.textContent = '读取状态失败：' + e2.message; }
      })();
      $('#set-ag-clear', el).addEventListener('click', async () => {
        try {
          await api('/api/agent/config', { apiKey: '' });
          S.agent = await api('/api/agent/status');
          fillAgent(S.agent);
          chatInit();
          toast('已清除 API Key');
        } catch (e2) { toast(e2.message, 'err'); }
      });
      $('#set-ag-save', el).addEventListener('click', async () => {
        const url = $('#set-ag-url', el).value.trim();
        const model = $('#set-ag-model', el).value.trim();
        const key = $('#set-ag-key', el).value.trim();
        if (!url) { toast('先填 Base URL', 'warn'); $('#set-ag-url', el).focus(); return; }
        if (!model) { toast('先填模型名', 'warn'); $('#set-ag-model', el).focus(); return; }
        const btn = $('#set-ag-save', el);
        btn.disabled = true;
        btn.textContent = '保存中…';
        try {
          const patch = { baseUrl: url, model, autoApply: $('#set-ag-auto', el).checked };
          if (key) patch.apiKey = key;
          await api('/api/agent/config', patch);
          S.agent = await api('/api/agent/status');
          fillAgent(S.agent);
          chatInit();
          toast(S.agent.configured ? '助手已就绪' : '已保存，但还差 API Key',
            S.agent.configured ? 'ok' : 'warn');
        } catch (e2) {
          toast('保存失败：' + e2.message, 'err');
        } finally {
          btn.disabled = false;
          btn.textContent = '保存';
        }
      });

      /* ---------------- 黑名单 ---------------- */
      function paintBlCounts() {
        const c = blOf();
        ['char', 'author', 'op'].forEach(k => {
          const box = $('[data-bl-count="' + k + '"]', el);
          if (box) box.textContent = c[k];
        });
        paintTabN();
      }
      el.addEventListener('click', ev => {
        const b = ev.target.closest('[data-bl]');
        if (!b) return;
        const kind = b.dataset.bl;
        if (kind === 'op') openOpBlacklist(paintBlCounts);
        else openBlacklist(kind, paintBlCounts);
      });

      // 默认页就是账号管理：打开先把 token 读出来（读的是本机密文，很快）。
      // 之前只在切页时触发，结果打开设置看到一排「读取中…」永远不动。
      $$('.set-acct', el).forEach(c => fillToken(c));
    },
  });
}

/* ------------------------------------------------------------ 内置助手连接参数（设置 → 二级弹窗）
   单独一页的理由：塞进主设置弹窗会把它撑到 900px 以上，API Key 输入框和「保存」
   全被挤到折叠线以下——用户看到的现象就是「点不到、也没有保存按钮」。
   另外 API Key 是 password，早先只给 input[type=text] 写了样式，密码框会露出
   浏览器默认的白底，在深色弹窗里像个空洞。两处都修了。 */
function openAgentSettings(onSaved) {
  const ag = S.agent || {};
  const m = modal(`
    <h3>内置助手</h3>
    <div class="sub">OpenAI 兼容协议，DeepSeek / 通义 / Kimi / 中转站 / 本地 Ollama 都能填。
      API Key 用 Windows DPAPI 加密存在本机，换电脑解不开、也不会上传。</div>
    <label class="field"><span>Base URL</span>
      <input type="text" id="ag-url" value="${esc(ag.baseUrl || '')}"
             placeholder="https://api.deepseek.com/v1"></label>
    <label class="field"><span>模型名</span>
      <input type="text" id="ag-model" value="${esc(ag.model || '')}"
             placeholder="deepseek-chat"></label>
    <label class="field"><span>API Key${ag.hasKey ? '（已保存，留空表示不改）' : ''}</span>
      <div class="path-row">
        <input type="password" id="ag-key" value="" autocomplete="off" spellcheck="false"
               placeholder="${ag.hasKey ? '已保存，留空不改' : 'sk-...'}">
        <button class="btn-plain mini" id="ag-clear"
                ${ag.hasKey ? '' : 'disabled'} title="删掉已保存的 Key">清除</button>
      </div></label>
    <label class="check-row">
      <input type="checkbox" id="ag-auto" ${ag.autoApply ? 'checked' : ''}>
      <span>写 MAA 时不再问我，直接执行（关掉更安全：每次写入前都弹确认）</span>
    </label>
    <div class="onb-hint" id="ag-state" style="margin-top:12px"></div>
    <div class="modal-foot">
      <button class="btn-plain" data-close>取消</button>
      <button class="btn-solid" id="ag-save">保存</button>
    </div>
  `, {
    onMount(el, close) {
      const state = $('#ag-state', el);
      const paint = d => {
        state.className = 'onb-hint ' + (d && d.configured ? 'ok' : 'bad');
        state.textContent = !d ? ''
          : d.configured ? `✓ 已就绪，用 ${d.model}`
            : (d.hasKey ? '✗ 还差模型名' : '✗ 还差 API Key');
      };
      paint(ag);

      $('#ag-clear', el).addEventListener('click', async () => {
        try {
          await api('/api/agent/config', { apiKey: '' });
          S.agent = await api('/api/agent/status');
          $('#ag-key', el).value = '';
          $('#ag-clear', el).disabled = true;
          paint(S.agent);
          chatInit();
          if (onSaved) onSaved();
          toast('已清除 API Key');
        } catch (e) { toast(e.message, 'err'); }
      });

      $('#ag-save', el).addEventListener('click', async () => {
        const url = $('#ag-url', el).value.trim();
        const model = $('#ag-model', el).value.trim();
        const key = $('#ag-key', el).value.trim();
        if (!url) { toast('先填 Base URL', 'warn'); $('#ag-url', el).focus(); return; }
        if (!model) { toast('先填模型名', 'warn'); $('#ag-model', el).focus(); return; }
        const btn = $('#ag-save', el);
        btn.disabled = true;
        btn.textContent = '保存中…';
        try {
          const patch = { baseUrl: url, model, autoApply: $('#ag-auto', el).checked };
          if (key) patch.apiKey = key;
          await api('/api/agent/config', patch);
          S.agent = await api('/api/agent/status');
          chatInit();
          if (onSaved) onSaved();
          close();
          toast(S.agent.configured ? '助手已就绪' : '已保存，但还差 API Key',
            S.agent.configured ? 'ok' : 'warn');
        } catch (e) {
          toast('保存失败：' + e.message, 'err');
          btn.disabled = false;
          btn.textContent = '保存';
        }
      });
    },
  });
  return m;
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
function openBlacklist(kind, onDone) {
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
    onClose: onDone,
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
function openOpBlacklist(onDone) {
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
    onClose: onDone,
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
          await saveSettings({ opBlacklist: next });
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

/* ------------------------------------------------------------ 模块切换
   菜单：作业获取 / 抽卡统计 / 配置修改 / 森空岛签到。
   后两个还没做，只占了位置（disabled + 待做角标）；终末地账号没有作业获取
   （MAA 只跑明日方舟），选中终末地时那个 tab 直接不显示，并自动切到抽卡统计。 */

function currentAccount() {
  return S.accounts.find(a => a.file === S.current) || null;
}

function currentGame() {
  const a = currentAccount();
  if (a && a.game) return a.game;
  return (S.detail && S.detail.game) || 'arknights';
}

function applyModuleAvailability() {
  const ef = currentGame() === 'endfield';
  // 终末地账号只留助手与抽卡统计：作业获取、森空岛签到、卫戍协议三枚 tab 都不显示
  // （签到本身支持终末地，全局签到照样会签，只是不给终末地账号开这块界面）
  ['copilot', 'signin', 'garrison'].forEach(m => {
    const b = $('.modtab[data-mod="' + m + '"]');
    if (b) b.hidden = ef;
  });
  if (ef && S.module !== 'gacha' && S.module !== 'chat') {
    S.gachaByGame = true;              // 终末地没有作业获取，只能看抽卡统计（记一笔是被账号逼的）
    setModule('gacha', true);
  } else if (!ef && S.gachaByGame) {
    S.gachaByGame = false;             // 从终末地切回方舟：别把人留在抽卡统计里，回作业获取
    setModule('copilot', true);
  } else if (S.module === 'gacha') {
    // 已经停在抽卡统计：换号就得重画（终末地之间互切也走这里，
    // 之前只判断了 !ef，结果终末地换号后还挂着上一个号的数据）
    renderGacha();
  } else if (S.module === 'signin') {
    renderSignin();                    // 签到面板上的「当前账号」跟着换
  } else if (S.module === 'garrison') {
    resetGarView();                    // 卫戍协议：换号重描面板（轮询不用重开）
    renderGarrison();
  } else if (S.module === 'chat') {
    chatInit();                        // 换号后助手面板里的上下文提示要跟着变
  }
}

function setModule(mod, byGame) {
  if (!byGame) S.gachaByGame = false;   // 用户自己点的模块，之后别被账号切走
  // 助手跟游戏无关，终末地账号也能用，别被强行拽回抽卡统计
  if (mod !== 'gacha' && mod !== 'chat' && currentGame() === 'endfield') mod = 'gacha';
  S.module = mod;
  $$('.modtab').forEach(b => {
    const on = b.dataset.mod === mod && !b.disabled;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  const cp = $('#mod-copilot'), gg = $('#mod-gacha'), sg = $('#mod-signin');
  const ct = $('#mod-chat');
  const ga = $('#mod-garrison');
  if (cp) cp.hidden = mod !== 'copilot';
  if (gg) gg.hidden = mod !== 'gacha';
  if (sg) sg.hidden = mod !== 'signin';
  if (ct) ct.hidden = mod !== 'chat';
  if (ga) ga.hidden = mod !== 'garrison';
  // 抽卡统计/签到/助手/卫戍协议都用不到右栏作业集：收掉第三列，主区拿满宽度
  $('#app').classList.toggle('no-basket', mod !== 'copilot');
  if (mod !== 'copilot') toggleBasket(false);
  if (mod === 'gacha') renderGacha();
  if (mod === 'signin') renderSignin();
  if (mod === 'garrison') { resetGarView(); renderGarrison(); garStartPolling(); }
  else garStopPolling();
  if (mod === 'chat') enterChat(); else leaveChat();
}

/* ------------------------------------------------------------ 森空岛签到 */
function renderSignin() {
  const acc = currentAccount();
  const gname = a => (gameOf(a) === 'endfield' ? '终末地' : '明日方舟');
  const sub = $('#sign-sub');
  if (sub) sub.textContent = acc ? `${acc.nickName} · ${gname(acc)}` : '未选择账号';

  const one = $('#sign-one-name');
  if (one) {
    one.textContent = acc
      ? `${acc.nickName}（${gname(acc)}${acc.channelName ? ' · ' + acc.channelName : ''}）`
      : '未选择账号';
    one.title = (acc && !acc.hasToken) ? 'MAA 导入的账号没有森空岛 token，签不了' : '';
  }

  const withTok = S.accounts.filter(a => a.hasToken);
  const note = $('#sign-all-note');
  if (note) {
    note.textContent = S.accounts.length
      ? `所有账号 · 明日方舟 + 终末地（${withTok.length}/${S.accounts.length} 个有 token）`
      : '还没有账号';
  }

  const bAll = $('#btn-sign-all'), bOne = $('#btn-sign-one');
  if (bAll) bAll.disabled = !withTok.length || !!S.signBusy;
  if (bOne) bOne.disabled = !acc || !acc.hasToken || !!S.signBusy;
  const tip = $('#sign-tip');
  if (tip) tip.textContent = S.signBusy ? '正在签到…' : '';
}

/* scope: 'all' 全部账号 / 'one' 当前账号。两块日志各写自己那块，互不覆盖 */
async function signinRun(scope) {
  if (S.signBusy) return;
  const boxSel = scope === 'all' ? '#sign-log-all' : '#sign-log-one';
  const box = $(boxSel);
  if (!box) return;
  if (scope === 'one' && !S.current) {
    toast('先在左侧选一个账号', 'err');
    return;
  }
  S.signBusy = scope;
  box.innerHTML = '';
  renderSignin();
  try {
    const { taskId } = await api('/api/signin/run', { file: scope === 'one' ? S.current : '' });
    const res = (await pollTask(taskId, document, boxSel)) || {};
    const c = res.counts || {};
    const line = `成功 ${c.ok || 0} · 已签到 ${c.already || 0} · 失败 ${c.fail || 0}`;
    toast('签到完成：' + line, c.fail ? 'err' : 'ok', 4200);
  } catch (e) {
    const d = document.createElement('div');
    d.className = 'err';
    d.textContent = '✗ ' + e.message;
    box.appendChild(d);
    box.scrollTop = box.scrollHeight;
    toast('签到失败：' + e.message, 'err', 5000);
  } finally {
    S.signBusy = '';
    renderSignin();
  }
}

/* ------------------------------------------------------------ 卫戍协议
   策略刻章速刷：每个账号一个速刷后端（Node 子进程，后端懒启动），面板轮询
   /api/garrison/status 拿状态。连接默认值由后端从该账号绑定 MAA 的连接配置
   里抄（MuMu → 快速截图，雷电 → ldplayer，PC → pc；极速 / 1.5× / 3 胜），
   用户改过的选项按账号记在 S.garOpts 里，切回来还在。 */
const GAR_STATUS_CN = {
  idle: '空闲', starting: '启动中', running: '运行中', stopping: '停止中',
  failed: '失败', completed: '已完成', done: '已完成',
};
const GAR_KIND_CN = {
  MuMuEmulator12: 'MuMu 模拟器 12', MuMuArm: 'MuMu ARM 版', LDPlayer: '雷电模拟器',
  PC: '明日方舟 PC 版', BlueStacks: '蓝叠', Nox: '夜神', XYAZ: '逍遥',
};
const GAR_BUSY = ['running', 'starting', 'stopping'];

let _garTimer = null;    // 面板轮询句柄：只在卫戍协议 tab 激活时跑
let _garSeq = 0;         // 请求代次：切账号/离开面板后丢弃旧响应
let _garLogSeq = {};     // file → 核心日志已追加到哪条 seq（按 seq 去重）
let _garDlStuck = 0;     // 「已下载却仍要下载」连续次数：超过阈值就停下报错，别无限重试

function resetGarView() {
  const box = $('#gar-log');
  if (box) box.innerHTML = '<span class="sign-idle">点「开始速刷」后这里显示进度日志。</span>';
  _garLogSeq[S.current] = 0;
}

function garCurrentOpts() {
  const d = (S.gar && S.gar.file === S.current) ? (S.gar.defaults || null) : null;
  return S.garOpts[S.current] || (d ? { ...d } : null);
}

/* 把选项描进表单：用户改过用改过的，否则用后端从 MAA 抄的默认值 */
function paintGarInputs() {
  const o = garCurrentOpts();
  if (!o) return;
  const set = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = String(v); };
  set('#gar-device', o.device);
  set('#gar-mode', o.mode);
  set('#gar-adb', o.adbAddress);
  set('#gar-wins', o.medalWins);
  set('#gar-speed', o.speed);
  set('#gar-scale', o.pcUiScale);
  set('#gar-shot', o.screenshotMethod);
}

function collectGarOpts() {
  const v = id => $(id) ? $(id).value : '';
  const o = {
    device: v('#gar-device'), mode: v('#gar-mode'), adbAddress: v('#gar-adb').trim(),
    medalWins: Number(v('#gar-wins')) || 3, speed: Number(v('#gar-speed')) || 1.5,
    pcUiScale: Number(v('#gar-scale')) || 90, screenshotMethod: v('#gar-shot'),
  };
  S.garOpts[S.current] = o;   // 记住用户的改动，切号再回来还在
  return o;
}

function garAppendLog(items) {
  const box = $('#gar-log');
  if (!box || !Array.isArray(items)) return;
  const idle = box.querySelector('.sign-idle');
  if (idle) idle.remove();
  let max = _garLogSeq[S.current] || 0;
  for (const it of items) {
    const seq = (it && it.seq) || 0;
    const text = (typeof it === 'string') ? it : (it.text ?? it.message ?? it.msg ?? JSON.stringify(it));
    if (!text) continue;
    if (seq && seq <= max) continue;
    if (seq) max = seq;
    const div = document.createElement('div');
    if ((it && it.err) || /失败|错误|error|停止|超时/.test(text)) div.className = 'err';
    div.textContent = seq ? `[${String(seq).padStart(4)}] ${text}` : text;
    box.appendChild(div);
  }
  while (box.childNodes.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  _garLogSeq[S.current] = max;
}

function paintGarState(d) {
  const st = d.state || {};
  const busy = GAR_BUSY.includes(st.status);
  S.garBusy = busy;
  const badge = $('#gar-badge');
  if (badge) {
    badge.className = 'gar-badge ' + (st.status || 'idle');
    badge.textContent = GAR_STATUS_CN[st.status] || st.status || '空闲';
  }
  const msg = $('#gar-msg');
  if (msg) msg.textContent = st.message || '';
  const set = (id, v, dash) => { const el = $(id); if (el) el.textContent = (v === undefined || v === null || v === '') ? (dash || '–') : v; };
  set('#gar-strategy', st.strategy);
  set('#gar-round', st.round);
  set('#gar-level', st.level);
  set('#gar-matches', st.matches, 0);
  const total = st.total || 31, done = st.completed || 0;
  const bar = $('#gar-progress');
  if (bar) bar.style.width = (total ? (done / total) * 100 : 0) + '%';
  const pt = $('#gar-prog-text');
  if (pt) pt.textContent = `进度 ${done} / ${total}` + (st.lastMatch ? ` · 上局：${st.lastMatch}` : '');
  garAppendLog(st.log);
  const tip = $('#gar-tip');
  if (tip) tip.textContent = busy ? '速刷运行中…' : '';
  const bt = $('#gar-test'), bs = $('#gar-start'), br = $('#gar-resume'), bp = $('#gar-stop');
  if (bt) bt.disabled = busy;
  if (bs) bs.disabled = busy;
  if (br) br.disabled = busy || !d.hasCheckpoint;
  if (bp) bp.disabled = !busy;
  ['#gar-device', '#gar-mode', '#gar-adb', '#gar-wins', '#gar-speed', '#gar-scale', '#gar-shot']
    .forEach(id => { const el = $(id); if (el) el.disabled = busy; });
  // 来源标注：让用户知道这份默认值是从哪个 MAA 配置抄的
  const src = $('#gar-src');
  if (src) {
    const fm = d.fromMaa || {};
    src.textContent = fm.kind
      ? `默认值抄自 MAA${fm.profile ? `「${fm.profile}」` : ''} · ${GAR_KIND_CN[fm.kind] || fm.kind}`
      : '默认值（没读到 MAA 连接配置）';
    src.title = (fm.maaDir || '') + (d.defaults && d.defaults.adbPath ? '\nadb：' + d.defaults.adbPath : '');
  }
}

/* 环境不齐（缺 Node / 后端起不来）时的指引空态（核心缺失走下载视图，不在这） */
function showGarEmpty(d, title) {
  const box = $('#gar-empty'), grid = $('#gar-grid'), logCard = $('#gar-log-card');
  if (!box) return;
  if (grid) grid.hidden = true;
  if (logCard) logCard.hidden = true;
  let html = '';
  if (d && !d.nodeOk) {
    html = `<h3>还缺 Node.js</h3><p>速刷核心是一个 Node 程序（需要 Node.js 20 以上）。
      装好 Node 后回来点「重试」。<button class="link-btn" data-url="https://nodejs.org">打开 nodejs.org</button></p>`;
  } else {
    html = `<h3>${esc(title || '速刷后端起不来')}</h3><p>${esc((d && d.error) || '未知原因')}</p>`;
  }
  box.innerHTML = html + '<button class="ghost-btn mint" id="gar-retry">重试</button>';
  box.hidden = false;
  const r = $('#gar-retry');
  if (r) r.addEventListener('click', () => renderGarrison(true));
  const link = box.querySelector('.link-btn');
  if (link) link.addEventListener('click', () => openUrl(link.dataset.url));
}

/* 核心包下载视图：自动开始（records → 官方直链），进度随面板轮询刷新 */
async function garStartDownload() {
  try { await api('/api/garrison/download/start', {}); }
  catch (e) { /* 失败原因下一轮轮询会带出来 */ }
}

/* needsDownload 的统一处理：正常放行下载；若后端连着回「已完成却仍缺核心」
   （版本目录识别失败之类），最多再催两次，之后停在错误态。
   旧版这里无条件调 start，会永远卡在「核心就绪，正在启动…」。 */
function garHandleMissing(d) {
  showGarDownload(d);
  const stage = (d.download || {}).stage;
  if (stage === 'idle') { _garDlStuck = 0; garStartDownload(); return; }
  if (stage !== 'done') { if (stage !== 'error') _garDlStuck = 0; return; }
  if (_garDlStuck < 2) { _garDlStuck++; garStartDownload(); return; }
  showGarDownload(Object.assign({}, d, {
    download: {
      stage: 'error',
      error: '核心包已下载，但程序没找到它（版本目录识别失败）。请点重试；'
        + '仍不行就删掉 garrison 目录后重下。',
    },
  }));
}

function showGarDownload(d) {
  const box = $('#gar-empty'), grid = $('#gar-grid'), logCard = $('#gar-log-card');
  if (!box) return;
  if (grid) grid.hidden = true;
  if (logCard) logCard.hidden = true;
  const dl = d.download || {};
  let pct = 0, text = '准备下载…';
  if (dl.stage === 'connecting') {
    text = `连接下载源（${dl.version ? 'v' + dl.version + ' · ' : ''}${dl.source || '官方源'}）…`;
  } else if (dl.stage === 'downloading') {
    pct = dl.total ? 100 * dl.done / dl.total : 0;
    text = `下载中 ${pct.toFixed(1)}%（${dl.done >> 20} / ${(dl.total || dl.done) >> 20} MB）`;
  } else if (dl.stage === 'verifying') {
    pct = dl.filesTotal ? 100 * dl.files / dl.filesTotal : 0;
    text = `校验解压中 ${dl.files} / ${dl.filesTotal || '…'} 个文件`;
  } else if (dl.stage === 'done') {
    pct = 100;
    text = '核心就绪，正在启动…';
  } else if (dl.stage === 'error') {
    text = '';
  }
  const err = dl.stage === 'error'
    ? `<p class="gar-dl-err">${esc(dl.error || '下载失败')}</p><button class="ghost-btn mint" id="gar-dl-retry">重试</button>`
    : '';
  box.innerHTML = `
    <h3>首次使用：下载核心包</h3>
    <p>约 61MB，来自小黑盒官方 CDN（装过小黑盒会自动用你本机的最新版记录）。
      下载后逐文件校验再安装，中断了会断点续传，不用手动管。</p>
    <div class="gar-dl-bar"><i style="width:${Math.min(100, pct).toFixed(1)}%"></i></div>
    <div class="gar-dl-text">${esc(text)}</div>
    ${err}`;
  box.hidden = false;
  const r = $('#gar-dl-retry');
  if (r) r.addEventListener('click', () => { _garDlStuck = 0; garStartDownload(); });
}

function hideGarEmpty() {
  const box = $('#gar-empty');
  if (box) box.hidden = true;
  const grid = $('#gar-grid'), logCard = $('#gar-log-card');
  if (grid) grid.hidden = false;
  if (logCard) logCard.hidden = false;
}

async function renderGarrison(force) {
  const sub = $('#gar-sub');
  const acc = currentAccount();
  if (sub) sub.textContent = acc ? `${acc.nickName} · 明日方舟` : '未选择账号';
  const an = $('#gar-account');
  if (an) an.textContent = acc ? acc.nickName : '未选择账号';
  if (!S.current) {
    showGarEmpty({ error: '先在左侧选一个明日方舟账号，再回来开速刷。' }, '还没有账号');
    return;
  }
  const seq = ++_garSeq;
  if (force) hideGarEmpty();
  paintGarInputs();   // 先用缓存/旧默认值描一遍，不闪空表单
  try {
    const d = await api('/api/garrison/status?file=' + encodeURIComponent(S.current));
    if (seq !== _garSeq || S.module !== 'garrison') return;
    d.file = S.current;
    S.gar = d;
    if (d.needsDownload) {
      garHandleMissing(d);
      return;
    }
    _garDlStuck = 0;
    if (!d.nodeOk || !d.ready) { showGarEmpty(d); return; }
    hideGarEmpty();
    paintGarInputs();
    paintGarState(d);
  } catch (e) {
    if (seq !== _garSeq) return;
    showGarEmpty({ error: e.message });
  }
}

/* 面板激活期间的状态轮询（后端本身就是转发，本地往返很便宜） */
function garStartPolling() {
  garStopPolling();
  _garTimer = setInterval(garTick, 1400);
  garTick();
}
function garStopPolling() {
  if (_garTimer) { clearInterval(_garTimer); _garTimer = null; }
}
async function garTick() {
  if (S.module !== 'garrison' || !S.current) return;
  if (!S.gar || S.gar.file !== S.current) return;   // 首份状态还没拿到，等 renderGarrison
  const seq = _garSeq;
  try {
    const d = await api('/api/garrison/status?file=' + encodeURIComponent(S.current));
    if (seq !== _garSeq || S.module !== 'garrison') return;
    d.file = S.current;
    const wasDownloading = !!S.gar.needsDownload;
    S.gar = d;
    if (d.needsDownload) {
      garHandleMissing(d);
      return;
    }
    _garDlStuck = 0;
    if (wasDownloading) { renderGarrison(true); return; }   // 刚下载完：重建整面板
    if (!d.ready) return;
    paintGarState(d);
  } catch (e) { /* 断线别刷屏：下一轮再试 */ }
}

async function garAction(kind) {
  if (!S.current) { toast('先在左侧选一个账号', 'err'); return; }
  const opts = collectGarOpts();
  const body = { file: S.current, options: opts };
  const tip = $('#gar-tip');
  try {
    if (kind === 'stop') {
      await api('/api/garrison/stop', body);
      toast('已发送停止指令');
    } else if (kind === 'test') {
      if (tip) tip.textContent = '正在测试连接…';
      const r = await api('/api/garrison/test', body);
      toast('✓ ' + (r.message || '连接成功'), 'ok', 4200);
      garAppendLog([{ text: '[测试] ' + (r.message || '连接成功') }]);
    } else {
      const r = await api('/api/garrison/' + kind, body);
      toast(kind === 'resume' ? '已从检查点恢复' : '已开始速刷');
      garAppendLog([{ text: `=== ${kind === 'resume' ? '恢复' : '开始'}速刷 ${r.taskId || ''} ===` }]);
    }
    garTick();
  } catch (e) {
    toast('✗ ' + e.message, 'err', 5000);
    garAppendLog([{ text: '✗ ' + e.message, err: true }]);
    if (tip) tip.textContent = '';
  }
}

/* ------------------------------------------------------------ 抽卡统计 */
let _gachaSeq = 0;
let _gachaBusy = false;

const fmtPct = x => (x ? (x * 100).toFixed(2) : '0.00') + '%';

async function renderGacha(force) {
  const body = $('#gacha-body');
  if (!body) return;
  const acc = currentAccount();
  const sub = $('#gacha-sub');
  if (sub) {
    sub.textContent = acc
      ? `${acc.nickName} · ${acc.game === 'endfield' ? '终末地' : '明日方舟'}`
      : '';
  }
  if (!S.current) {
    body.innerHTML = `<div class="gacha-empty"><h3>还没有账号</h3>
      <p>先用森空岛 token 添加一个账号，抽卡记录才有地方读。</p></div>`;
    const up = $('#gacha-updated');
    if (up) up.textContent = '';
    return;
  }
  if (!force && S.gacha && S.gacha.file === S.current) { paintGacha(S.gacha); return; }
  const seq = ++_gachaSeq;
  body.innerHTML = `<div class="gacha-cards">${'<div class="gacha-card"><b>—</b><span>读取中</span></div>'.repeat(5)}</div>
    <div class="loading-row"><span class="spinner"></span>读取本地抽卡缓存…</div>`;
  try {
    const d = await api('/api/gacha?file=' + encodeURIComponent(S.current));
    if (seq !== _gachaSeq) return;
    S.gacha = d;
    if (d && d.file) S.gachaOf[d.file] = d;   // 切走再回来直接用这份，不再请求
    paintGacha(d);
    if (currentGame() === 'endfield') renderHero();   // 头像卡里的抽卡/六星跟着刷新
  } catch (e) {
    if (seq !== _gachaSeq) return;
    body.innerHTML = `<div class="gacha-empty"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
  }
}

/* 空口径：终末地没抽过武器时，武器池 tab 也得能点开（显示 0，不炸） */
const GACHA_BLANK = {
  total: 0, sixCount: 0, fiveCount: 0, rate6: 0, avgPity: 0, lastSixGap: 0,
  byRarity: { '1': 0, '2': 0, '3': 0, '4': 0, '5': 0, '6': 0 },
  firstAt: '', lastAt: '', sixStars: [], pools: [], records: [],
};

function paintGacha(d) {
  const body = $('#gacha-body');
  if (!body) return;
  const up = $('#gacha-updated');
  if (up) up.textContent = d.updatedAt ? '上次同步 ' + d.updatedAt.replace('T', ' ').slice(0, 16) : '';

  if (!d.hasToken) {
    body.innerHTML = `<div class="gacha-empty">
      <h3>这个账号读不到抽卡记录</h3>
      <p>抽卡记录必须用森空岛 token 查。这个账号是用 MAA 的干员识别结果建的（或 token 已失效）。
      用「添加账号 → 森空岛 token」再同步一次即可，会复用同一个账号文件。</p>
      <button class="ghost-btn mint" id="gacha-add">去添加 token</button></div>`;
    const b = $('#gacha-add');
    if (b) b.addEventListener('click', openAddAccount);
    return;
  }

  const s = d.summary;
  if (!s || !s.total) {
    const ef = d.game === 'endfield';
    // 已经同步过还是 0 条：这个号确实没记录（接口只留最近一段），别再让人反复点拉取
    const synced = !!d.updatedAt;
    const at = synced ? esc(d.updatedAt.replace('T', ' ').slice(0, 16)) : '';
    const tip = synced
      ? `上次同步 ${at}，接口返回 0 条：这个号确实没有抽卡记录，不用再拉。`
      : (ef
        ? '终末地的记录接口也能读：点下面按钮拉一次。如果这个角色确实没抽过，这里会一直是 0 条。'
        : '点下面的按钮从森空岛拉一次。首次会把所有寻访分类翻完，几秒钟。');
    body.innerHTML = `<div class="gacha-empty">
      <h3>${synced ? '这个账号没有抽卡记录' : '还没有抽卡记录'}</h3>
      <p>${tip}</p>
      <button class="ghost-btn mint" id="gacha-sync-empty">${synced ? '再拉一次' : '拉取抽卡记录'}</button></div>`;
    const b = $('#gacha-sync-empty');
    if (b) b.addEventListener('click', gachaSync);
    return;
  }

  // 终末地：角色寻访 / 武器申领 是两套记录、两套保底，分两个 tab 看（明日方舟只有一套）
  if (d.game !== 'endfield' || S.gachaTab !== 'weapon') S.gachaTab = 'char';
  paintGachaBody();
}

function paintGachaBody() {
  const d = S.gacha;
  const body = $('#gacha-body');
  if (!d || !body) return;
  const s = d.summary || {};
  const ef = d.game === 'endfield';
  const tab = ef ? S.gachaTab : 'char';
  // 终末地每个 tab 只在 S.gacha.summary.char / .weapon 里取；方舟就是顶层那份
  const cur = ef ? ((tab === 'weapon' ? s.weapon : s.char) || GACHA_BLANK) : s;

  const rarOrder = [6, 5, 4, 3, 2, 1];
  const byRarity = cur.byRarity || GACHA_BLANK.byRarity;
  const rarMax = Math.max(1, ...rarOrder.map(r => byRarity[String(r)] || 0));
  const bars = rarOrder.map(r => {
    const n = byRarity[String(r)] || 0;
    const pct = (n / rarMax) * 100;
    return `<div class="rar-bar">
      <i class="r${r}">${r}★</i>
      <div class="rar-track"><div class="rar-fill r${r}" style="width:${pct.toFixed(1)}%"></div></div>
      <b>${n} 次<small>${cur.total ? ((n / cur.total) * 100).toFixed(1) : '0.0'}%</small></b>
    </div>`;
  }).join('');

  // 一个池子一张卡片，池子下面直接挂这个池出的六星（抽卡记录 App 的常见排版）。
  // 明日方舟和终末地同一套版式：方舟的「标准寻访」会合并好几期，一个池里可能
  // 出 7、8 个六星，所以先列最近 8 个，再多就只给一句「还有 N 个」。
  const poolCards = (cur.pools || []).slice(0, 12).map(p => {
    const items = p.sixItems || [];
    const shown = items.slice(0, 8);
    const rows = shown.map(x => `
      <div class="six-row">
        <span class="six-star">★★★★★★</span>
        <span class="six-name">${esc(x.charName)}${x.isNew ? '<i class="six-new">new</i>' : ''}</span>
        <span class="six-pity">第 <b>${x.pity}</b> 抽</span>
        <span class="six-at">${esc(x.at)}</span>
      </div>`).join('');
    const more = items.length > shown.length
      ? `<div class="pool-more">还有 ${items.length - shown.length} 个六星没列出</div>` : '';
    const none = items.length ? '' : '<div class="pool-none">这个池还没出六星</div>';
    return `<div class="pool-card">
      <div class="pool-head">
        <b>${esc(p.poolName)}</b>
        <span>${p.count} 抽 · 六星 <b>${p.six}</b> · ${fmtPct(p.rate6)}</span>
      </div>
      ${p.firstTs ? `<div class="pool-time">${esc(p.firstTs)} — ${esc(p.lastTs)}</div>` : ''}
      ${rows}${more}${none}
    </div>`;
  }).join('');

  const recRows = (cur.records || []).slice(0, 60).map(r => `
    <tr>
      <td class="c-name r${r.rarity}">${esc(r.charName)}${r.isNew ? ' <span class="six-new">new</span>' : ''}</td>
      <td class="c-dim">${'★'.repeat(Math.max(1, Math.min(6, r.rarity || 1)))}</td>
      <td class="c-dim">${esc(r.poolName)}</td>
      <td>${esc(r.at)}</td>
    </tr>`).join('');

  const tabs = ef ? `<div class="gacha-tabs">
      ${[['char', '角色寻访', (s.char || {}).total || 0],
         ['weapon', '武器申领', (s.weapon || {}).total || 0]]
        .map(([k, label, n]) => `<button class="gacha-tab${tab === k ? ' active' : ''}"
          data-gtab="${k}" aria-selected="${tab === k}">${label}<i>${n}</i></button>`).join('')}
    </div>` : '';

  const cards = `
    <div class="gacha-card"><b>${cur.total}</b><span>${ef
      ? (tab === 'weapon' ? '武器申领（抽）' : '角色寻访（抽）') : '总抽卡'}</span></div>
    <div class="gacha-card gold"><b>${cur.sixCount}</b><span>${ef
      ? (tab === 'weapon' ? '六星武器' : '六星角色') : '六星'}</span></div>
    <div class="gacha-card mint"><b>${fmtPct(cur.rate6)}</b><span>六星出率</span></div>
    <div class="gacha-card"><b>${cur.avgPity || '—'}</b><span>平均出六星（抽）</span></div>
    <div class="gacha-card"><b>${cur.lastSixGap}</b><span>距上次六星（抽）</span></div>`;

  const poolSec = (cur.pools || []).length ? `
    <div class="gacha-sec">
      <div class="gacha-sec-head"><b>按卡池</b><span>按最近抽卡时间排序${(cur.pools.length > 12)
        ? `，只列最近 12 个（共 ${cur.pools.length} 个）` : ''}</span></div>
      <div class="pool-list">${poolCards}</div>
    </div>` : '';

  const rarSec = `
    <div class="gacha-sec">
      <div class="gacha-sec-head"><b>星级分布</b><span>${esc(cur.firstAt)} — ${esc(cur.lastAt)}</span></div>
      <div class="rar-bars">${bars}</div>
    </div>`;

  const recSec = `
    <div class="gacha-sec">
      <div class="gacha-sec-head"><b>最近记录</b><span>最新在前${cur.total > 60 ? `，只列最近 60 条（共 ${cur.total} 条）` : ''}</span></div>
      <table class="rec-table">
        <thead><tr><th>${ef ? (tab === 'weapon' ? '武器' : '角色') : '干员'}</th><th>星级</th><th>卡池</th><th>时间</th></tr></thead>
        <tbody>${recRows}</tbody>
      </table>
    </div>`;

  // 两个游戏同一套版式：按卡池（六星挂在池下）→ 星级分布 → 最近记录
  const secs = [poolSec, rarSec, recSec];

  body.innerHTML = `
    ${tabs}
    <div class="gacha-cards">${cards}</div>
    ${ef ? `<div class="gacha-window">官方记录接口只保留最近一段：角色 ${(s.char || {}).total || 0} 抽 · 武器 ${(s.weapon || {}).total || 0} 抽。
      更早的记录接口不返回，所以这里比实际总抽数少（不是统计漏了）。</div>` : ''}

    ${secs.join('')}

    <div class="gacha-note">
      ! 保底按卡池连续计数（同池每抽 +1，出六星清零）；抽卡记录只保留最近一段窗口，
      最早几次六星的「第几抽」只是窗口内的下限，不是真实垫抽数。
      ${ef ? '角色寻访与武器申领是两套保底、两套概率，所以分开两个 tab 统计，别把它们加在一起算。' : ''}
    </div>
  `;

  $$('.gacha-tab', body).forEach(b => b.addEventListener('click', () => {
    if (S.gachaTab === b.dataset.gtab) return;
    S.gachaTab = b.dataset.gtab;
    paintGachaBody();
    const sc = $('.gacha-scroll');
    if (sc) sc.scrollTop = 0;
  }));
}

async function gachaSync() {
  if (!S.current) { toast('先在左边选一个账号', 'warn'); return; }
  if (_gachaBusy) return;
  _gachaBusy = true;
  const btn = $('#btn-gacha-sync');
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.textContent = '同步中…'; }
  const body = $('#gacha-body');
  body.innerHTML = `<div class="loading-row"><span class="spinner"></span>正在从森空岛拉取抽卡记录…</div>
    <div class="gacha-log" id="task-log"></div>`;
  try {
    const { taskId } = await api('/api/gacha/sync', { file: S.current });
    await pollTask(taskId, body);
    toast('抽卡记录已更新');
    await renderGacha(true);
  } catch (e) {
    toast('同步失败：' + e.message, 'err', 6000);
    const box = $('#task-log', body);
    if (box && !box.textContent.trim()) {
      body.insertAdjacentHTML('beforeend',
        `<div class="gacha-note">同步没跑起来：${esc(e.message)}</div>`);
    }
  } finally {
    _gachaBusy = false;
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml; }
  }
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
  $('#game-switch').addEventListener('click', e => {
    const b = e.target.closest('.gswitch');
    if (b) setSideGame(b.dataset.game);
  });
  $$('.modtab').forEach(b => b.addEventListener('click', () => {
    if (b.disabled) return;
    setModule(b.dataset.mod);
  }));
  $('#btn-gacha-sync').addEventListener('click', gachaSync);

  $('#btn-sign-all').addEventListener('click', () => signinRun('all'));

  $('#btn-sign-one').addEventListener('click', () => signinRun('one'));

  // 卫戍协议：四个动作 + 表单改动随手记（不提交就切号也不丢）
  $('#gar-test').addEventListener('click', () => garAction('test'));
  $('#gar-start').addEventListener('click', () => garAction('start'));
  $('#gar-resume').addEventListener('click', () => garAction('resume'));
  $('#gar-stop').addEventListener('click', () => garAction('stop'));
  const garForm = $('.gar-form');
  if (garForm) garForm.addEventListener('change', collectGarOpts);
  $('#btn-add-account').addEventListener('click', openAddAccount);
  $('#btn-import-maa').addEventListener('click', importFromMaa);
  $('#btn-settings').addEventListener('click', openSettings);
  $('#maa-state').addEventListener('click', async () => {
    await refreshMaaStatus();
    toast(S.maa.running ? 'MAA 正在运行' : 'MAA 未运行');
  });
  $('#btn-apply').addEventListener('click', applyToMaa);
  $('#btn-clear-basket').addEventListener('click', () => {
    S.basket = [];
    S.paradoxPending = [];   // 作业集清空，「写完记已导入」的悖论清单一并清（#59）
    renderBasket();
  });
  $('#btn-refresh-all').addEventListener('click', async () => {
    const btn = $('#btn-refresh-all');
    if (btn.disabled) return;   // 按钮原本无任何禁用，双击=两个并发全量同步+两个弹窗（#59）
    btn.disabled = true;
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
    finally { btn.disabled = false; }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      // 「全部加入」的倒三角菜单开着时先收菜单，别顺手把作业集抽屉也关了
      const menu = $('#add-menu');
      if (menu && !menu.hidden) { menu.hidden = true; return; }
      toggleBasket(false); return;
    }
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
      e.preventDefault();
      $('#search-input').focus();
    }
  });
  // 点别处收「全部加入」菜单（结果区每次重绘都换 DOM，用文档级监听 + 类选择器）
  document.addEventListener('click', e => {
    if (e.target.closest('#add-menu') || e.target.closest('#add-all-more')) return;
    const menu = $('#add-menu');
    if (menu && !menu.hidden) menu.hidden = true;
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

/* ============================================================ 内置助手 */

/* 轻量 markdown 渲染：整篇先做 HTML 转义再按行解析，所以不存在标签注入。
   助手的回复主要是加粗 / 列表 / 表格 / 代码，支持这些就够；
   星号等原样标记绝不直接透给用户（2026-11-17 用户反馈「不能显示那星号」）。 */
function mdToHtml(src) {
  const escd = esc(String(src || ''));
  const lines = escd.split('\n');
  let out = '';
  let inCode = false;
  let codeBuf = [];
  let listTag = null;          // 'ul' | 'ol'
  let paraBuf = [];
  let tableBuf = null;

  const mdInline = s => s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/__([^_]+)__/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<i>$2</i>')
    .replace(/~~([^~]+)~~/g, '<s>$1</s>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const closeList = () => { if (listTag) { out += '</' + listTag + '>'; listTag = null; } };
  const flushPara = () => {
    if (paraBuf.length) { out += '<p>' + paraBuf.map(mdInline).join('<br>') + '</p>'; paraBuf = []; }
  };
  const flushTable = () => {
    if (!tableBuf) return;
    const rows = tableBuf; tableBuf = null;
    const cells = r => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(s => s.trim());
    let head = null, from = 0;
    if (rows.length >= 2 && /^[\s|:\-]+$/.test(rows[1])) { head = cells(rows[0]); from = 2; }
    out += '<table class="md-table">';
    if (head) out += '<thead><tr>' + head.map(c => '<th>' + mdInline(c) + '</th>').join('') + '</tr></thead>';
    out += '<tbody>' + rows.slice(from).map(r =>
      '<tr>' + cells(r).map(c => '<td>' + mdInline(c) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (inCode) {
      if (/^```/.test(line)) { inCode = false; out += '<pre><code>' + codeBuf.join('\n') + '</code></pre>'; codeBuf = []; }
      else codeBuf.push(line);
      continue;
    }
    if (/^```/.test(line)) { flushPara(); flushTable(); closeList(); inCode = true; continue; }
    // 表格行：|a|b| 形式，连续收集
    if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); closeList(); (tableBuf = tableBuf || []).push(line.trim()); continue; }
    flushTable();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); closeList();
      const lvl = Math.min(4, m[1].length + 1);   // # 用 h2，往下 h3/h4，别压过页面标题
      out += '<h' + lvl + ' class="md-h">' + mdInline(m[2]) + '</h' + lvl + '>';
      continue;
    }
    if (/^(&gt;|\>)\s?/.test(line)) {
      flushPara(); closeList();
      out += '<blockquote>' + mdInline(line.replace(/^(&gt;|\>)\s?/, '')) + '</blockquote>';
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flushPara(); closeList(); out += '<hr>'; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      flushPara();
      if (listTag !== 'ul') { closeList(); out += '<ul>'; listTag = 'ul'; }
      out += '<li>' + mdInline(m[1]) + '</li>';
      continue;
    }
    if ((m = line.match(/^\s*\d+[.、]\s+(.*)$/))) {
      flushPara();
      if (listTag !== 'ol') { closeList(); out += '<ol>'; listTag = 'ol'; }
      out += '<li>' + mdInline(m[1]) + '</li>';
      continue;
    }
    if (!line.trim()) { flushPara(); closeList(); continue; }
    closeList();
    paraBuf.push(line);
  }
  if (inCode) out += '<pre><code>' + codeBuf.join('\n') + '</code></pre>';
  flushPara(); flushTable(); closeList();
  return out;
}

const CHAT = {
  session: '',
  since: 0,
  timer: null,
  open: false,
  sending: false,
  nodes: new Map(),      // 事件序号 → DOM，流式文本要靠它原地更新
  sideTasks: new Set(),  // 已在右栏跟踪的任务 taskId，重渲染别重复跟
  cfg: null,
};

/* 助手引导卡片：示例问句必须和「现在真能做的事」对得上——早先那几条
   （「我还差哪些关没打」「推荐养哪些干员」）助手根本没有对应数据源，点了
   只会兜圈子。下面每一条都对着一个已实现的 skill，点下去就能真跑。 */
const CHAT_STARTERS = [
  ['帮我把最新活动 S 关卡挂完',
   '帮我把最新活动的 S 关卡挂完'],
  ['帮我把将进酒活动普通关卡挂完',
   '帮我把将进酒活动的普通关卡挂完'],
  ['所有账号森空岛签到',
   '帮我把所有账号的森空岛签到都签了'],
  ['活动商店清一遍（别买龙门币、固源岩）',
   '清一下最新活动的商店，黑名单里加龙门币、固源岩、技巧概要、赤金'],
  ['把泥岩精二，三技能专三',
   '把泥岩精二，三技能专三，材料不够就停下来告诉我'],
  ['现在跑到哪了？卡住了吗？',
   '现在跑到哪了？卡住了吗？'],
];

function chatEmptyHtml() {
  return `<div class="chat-beta">
      <b>测试版</b>
      <span>助手还在 Beta，功能会变，<b>也真的可能出错</b>。
        自动战斗、切号这类操作跑起来时，请瞄一眼右边的「任务进度」；
        跑歪了把日志发出来，我们照着改。</span>
    </div>
    <p>现在能做的事（点一条试试）：</p>`
    + CHAT_STARTERS.map(([label, ask]) =>
      `<button class="chat-chip" data-ask="${esc(ask)}">${esc(label)}</button>`).join('');
}

/* 只在空态里补内容：一旦有对话记录，chatRender 会把整块删掉 */
function paintChatEmpty() {
  const el = $('#chat-empty');
  if (el && !el.dataset.painted) {
    el.dataset.painted = '1';
    el.innerHTML = chatEmptyHtml();
    bindChatChips(el);
  }
}

function chatScroll(force) {
  const box = $('#chat-body');
  if (!box) return;
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  if (force || near) box.scrollTop = box.scrollHeight;
}

/* 右栏执行日志跟随滚动（日志在底部追加，用户往上翻了就不打扰） */
function chatSideScroll() {
  const box = $('#chat-side-log');
  if (!box) return;
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (near) box.scrollTop = box.scrollHeight;
}

/* 一条事件怎么显示：user 是气泡；say/error 是 markdown 渲染后的正文
   （助手回报告常用 **加粗** 和表格，纯文本会把星号漏给用户）；
   tool/tool_result 回到聊天流（左侧小字一行）。
   tool_result 带 taskId 时，右栏自动跟踪这个任务的进度日志——
   原本只有点「看进度」弹窗才能看到的日志，现在直接流到右边。 */
function chatNode(ev) {
  const wrap = document.createElement('div');
  const kind = ev.kind;
  if (kind === 'user') {
    wrap.className = 'msg user' + (ev.streaming ? ' streaming' : '');
    wrap.innerHTML = '<div class="msg-body">' + esc(ev.text || '') + '</div>';
  } else if (kind === 'say' || kind === 'error') {
    wrap.className = 'msg ' + kind + (ev.streaming ? ' streaming' : '');
    wrap.dataset.raw = ev.text || '';
    wrap.innerHTML = '<div class="msg-body">' + mdToHtml(ev.text || '') + '</div>';
  } else if (kind === 'tool') {
    wrap.className = 'msg tool' + (ev.streaming ? ' streaming' : '');
    wrap.innerHTML = '<span class="t-dot"></span><span class="t-text">'
      + esc(toolLabel(ev.name, ev.args)) + '</span>';
  } else if (kind === 'tool_result') {
    // 报错的那条别用绿点：工具失败在面板上得一眼能看出来
    const bad = /"error"\s*:/.test(ev.text || '');
    wrap.className = 'msg tool done' + (bad ? ' bad' : '');
    const tid = chatTaskId(ev);
    wrap.innerHTML = '<span class="t-dot"></span><span class="t-text">'
      + esc(toolResultLabel(ev)) + '</span>'
      + (tid ? '<button class="t-go">看进度</button>' : '');
    if (tid) {
      wrap.querySelector('.t-go').addEventListener('click', () => {
        chatTaskModal(tid, ev.name);
      });
      sideTaskWatch(tid, ev.name);
    }
  } else if (kind === 'need_confirm') {
    wrap.className = 'msg';
    wrap.innerHTML = '<div class="chat-confirm">'
      + '<div class="c-title">需要你确认</div>'
      + '<div class="c-text">' + esc(ev.text || '') + '</div>'
      + '<div class="c-btns"><button class="go">确认执行</button>'
      + '<button class="no">取消</button></div></div>';
    wrap.querySelector('.go').addEventListener('click', confirmChat);
    wrap.querySelector('.no').addEventListener('click', cancelChat);
  } else {
    return null;
  }
  wrap.dataset.i = ev.i;
  return wrap;
}

const TOOL_NAMES = {
  list_accounts: '看账号列表',
  list_activities: '看有哪些活动',
  search_stages: '核对关卡号',
  find_ops: '找作业',
  op_detail: '看作业缺什么',
  account_progress: '看游戏进度',
  run_status: '看 MAA 跑到哪了',
  maa_capabilities: '查 MAA 能配什么',
  list_profiles: '看配置档案',
  plan_multi_account: '排多账号方案',
  build_run: '配任务并开跑',
  apply_plan: '开跑',
  add_stages: '挂关卡',
  stop_run: '停止挂机',
  // 挂关 skill 化（2026-11-17）
  skill_wakeup: '唤醒游戏',
  skill_navigate: '导航活动',
  skill_skip_story: '过剧情关',
  skill_fight: '自动战斗',
  skill_weekly: '周目编排',
  skill_signin: '森空岛签到',
};

function toolLabel(name, args) {
  const base = TOOL_NAMES[name] || ('调用 ' + name);
  const kw = args && (args.keyword || args.copilot_id || args.plan_id);
  return kw ? base + '（' + kw + '）' : base;
}

function toolResultLabel(ev) {
  const base = TOOL_NAMES[ev.name] || ev.name;
  let t = ev.text || '';
  let brief = '';
  let bad = false;
  try {
    const d = JSON.parse(t.length >= 1200 ? t.slice(0, 1200) : t);
    if (d.error) { brief = d.error; bad = true; }
    else if (d.count != null) brief = d.count + ' 条';
    else if (d.summary) brief = d.summary;
    else if (d.rows) brief = d.rows.length + ' 关';
    else if (d.accounts) brief = d.accounts.length + ' 个账号';
    else if (d.explain) brief = d.explain.text || '';
    else if (d.state) brief = d.state;
  } catch (e) { /* 截断的 JSON 解析不了，就不带摘要 */ }
  return base + (brief ? ' · ' + brief : '') + (bad ? ' ✗' : ' ✓');
}

/* 写入类工具会把活丢给后台任务（写 MAA 要关客户端、重启、跑图，几十秒到几十分钟），
   返回的是 taskId 而不是结果。面板上原本没地方看这个任务，就在那条记录右侧挂个入口。
   JSON 可能被截断（后端只传前 1200 字），直接正则捞，别指望 JSON.parse 成功。 */
function chatTaskId(ev) {
  const m = (ev.text || '').match(/"taskId"\s*:\s*"([^"]+)"/);
  return m ? m[1] : '';
}

/* ---------------- skill 联调测试 ----------------
   目的：不用聊天、不用摆确认卡，直接把一句话喂给模型，看它选哪个 skill、
   参数抽得对不对，勾「直接跑」还能立刻动模拟器（例如停在主页测导航）。
   走 POST /api/skill/try（agent.try_skill）：只挂指定工具 / 全工具任选。 */
const SKILL_TEST_OPTS = [
  ['', '全部（让模型自己选）'],
  ['skill_navigate', '导航到活动 · skill_navigate'],
  ['skill_wakeup', '唤醒游戏 · skill_wakeup'],
  ['skill_fight', '自动战斗 · skill_fight'],
  ['skill_skip_story', '过剧情关 · skill_skip_story'],
  ['skill_signin', '森空岛签到 · skill_signin'],
  ['skill_weekly', '周目编排 · skill_weekly'],
];

function chatSkillTest() {
  const opts = SKILL_TEST_OPTS.map(([v, t]) =>
    '<option value="' + esc(v) + '">' + esc(t) + '</option>').join('');
  const m = modal(`
    <h3>skill 联调测试</h3>
    <div class="sub">把一句话交给大模型，看它选哪个 skill、参数抽得对不对。勾「直接跑」会立刻动模拟器。</div>
    <div class="st-row">
      <span class="st-label">限定 skill</span>
      <select id="st-skill">${opts}</select>
    </div>
    <div class="st-row">
      <span class="st-label">说一句话</span>
      <input id="st-text" type="text" placeholder="例如：我现在停在主页，帮我导航到 MN 界面" />
    </div>
    <label class="st-check"><input type="checkbox" id="st-run" /> 直接跑（跳过确认卡）</label>
    <div class="st-out" id="st-out" hidden></div>
    <div class="modal-foot">
      <button class="btn-plain" data-close>关闭</button>
      <button class="btn-primary" id="st-go">让模型解析</button>
    </div>
  `);
  const go = m.el.querySelector('#st-go');
  const out = m.el.querySelector('#st-out');
  const input = m.el.querySelector('#st-text');
  input.focus();
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); go.click(); }
  });
  go.addEventListener('click', async () => {
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    go.disabled = true;
    out.hidden = false;
    out.innerHTML = '<div class="st-dim">问模型中…</div>';
    try {
      const r = await api('/api/skill/try', {
        text: text,
        skill: m.el.querySelector('#st-skill').value || '',
        run: m.el.querySelector('#st-run').checked,
      });
      let h = '';
      if (r.picked) {
        h += '<div class="st-pick">选了 <b>' + esc(r.picked) + '</b>'
          + (r.matched === false ? ' <span class="st-bad">（不是指定的那个）</span>' : '')
          + '</div>';
        h += '<div class="st-dim">参数</div><pre class="st-args">'
          + esc(JSON.stringify(r.args || {}, null, 2)) + '</pre>';
      }
      if (r.said) h += '<div class="st-dim">模型的说明</div><div class="st-said">'
        + mdToHtml(r.said) + '</div>';
      if (r.note) h += '<div class="st-note">' + esc(r.note) + '</div>';
      if (r.taskId) {
        h += '<div class="st-run-row"><span>taskId ' + esc(r.taskId) + '</span>'
          + '<button class="t-go" id="st-prog">看进度</button></div>';
      }
      out.innerHTML = h;
      if (r.taskId) {
        sideTaskWatch(r.taskId, r.picked);
        const b = out.querySelector('#st-prog');
        if (b) b.addEventListener('click', () => chatTaskModal(r.taskId, r.picked));
      }
    } catch (e) {
      out.innerHTML = '<div class="st-note">' + esc(String(e.message || e)) + '</div>';
    } finally {
      go.disabled = false;
    }
  });
  return m;
}

function chatTaskModal(tid, name) {
  const title = TOOL_NAMES[name] || name || '任务';
  const m = modal(`
    <h3>${esc(title)} · 进度</h3>
    <div class="sub">活已经交给后台了，关掉这个窗口不影响它继续跑</div>
    <div class="log-box chat-task-log" id="task-log"></div>
    <div class="modal-foot"><button class="btn-plain" data-close>在后台跑</button></div>
  `);
  pollTask(tid, m.el).catch(() => { });
  return m;
}

/* 右栏跟踪一个后台任务的进度日志：skill / build_run 一启动，日志就流进来，
   不用再点「看进度」开弹窗（弹窗还在，想单独看大图可以用）。
   一个任务只跟一遍（Set 挡重渲染重复跟踪）；多个任务并发时日志按到达顺序
   混排，每段开头有任务名一行做分隔。 */
function sideTaskWatch(tid, name) {
  if (!tid || CHAT.sideTasks.has(tid)) return;
  CHAT.sideTasks.add(tid);
  const box = $('#chat-side-log');
  if (!box) return;
  const empty = $('#chat-side-empty');
  if (empty) empty.remove();
  const head = document.createElement('div');
  head.className = 'side-task-head';
  head.textContent = '▶ ' + (TOOL_NAMES[name] || name || '任务')
    + ' · ' + tid.slice(0, 8);
  box.appendChild(head);
  chatSideScroll();
  // root 给 body（永远 isConnected），让 pollTask 把日志写进右栏盒子
  pollTask(tid, document.body, '#chat-side-log').catch(() => { });
}

function chatRender(events) {
  const body = $('#chat-body');
  if (!body) return;
  const empty = $('#chat-empty');
  if (empty && events.length) empty.remove();
  let streaming = null;
  for (const ev of events) {
    let node = CHAT.nodes.get(ev.i);
    if (!node) {
      node = chatNode(ev);
      if (!node) continue;
      CHAT.nodes.set(ev.i, node);
      // 所有事件都进聊天流（tool/tool_result 是左侧小字行）；
      // 右栏只放后台任务的进度日志，由 sideTaskWatch 负责
      body.appendChild(node);
    } else if (ev.kind === 'say' || ev.kind === 'user' || ev.kind === 'error') {
      if (ev.kind === 'user') {
        const b = node.querySelector('.msg-body');
        if (b && b.textContent !== (ev.text || '')) b.textContent = ev.text || '';
      } else if (node.dataset.raw !== (ev.text || '')) {
        // say / error：markdown 渲染，流式更新时按最新全文重渲
        node.dataset.raw = ev.text || '';
        const b = node.querySelector('.msg-body');
        if (b) b.innerHTML = mdToHtml(ev.text || '');
      }
    }
    if (ev.kind === 'say') streaming = node;
    chatScroll(false);
  }
  // 除了最后一条，之前的「正在输入」光标都收掉
  body.querySelectorAll('.msg.say.streaming').forEach(n => {
    if (n !== streaming) n.classList.remove('streaming');
  });
  if (streaming && CHAT.sending) streaming.classList.add('streaming');
  chatScroll(false);
}

function chatPoll() {
  if (!CHAT.open) return;
  clearTimeout(CHAT.timer);
  const tick = async () => {
    if (!CHAT.open) return;     // 面板被关掉就彻底停下，别再空转
    try {
      const d = await api('/api/chat/poll?session=' + encodeURIComponent(CHAT.session)
        + '&since=' + CHAT.since);
      if (d.events && d.events.length) {
        CHAT.since = Math.max(CHAT.since, d.next || 0);
        chatRender(d.events);
      }
      if (d.events && d.events.length) {
        const last = d.events[d.events.length - 1];
        if (last.kind === 'done') CHAT.sending = false;
      }
      CHAT.sending = !!d.running;
      chatBusy(d.running);
      // 流式输出时要勤查，空闲时可以放慢
      CHAT.timer = setTimeout(tick, d.running ? 320 : 1600);
    } catch (e) {
      CHAT.timer = setTimeout(tick, 2000);
    }
  };
  tick();
}

function chatBusy(on) {
  const b = $('#chat-send');
  if (b) b.disabled = !!on || !$('#chat-input').value.trim();
}

async function chatInit() {
  try {
    CHAT.cfg = await api('/api/agent/status');
  } catch (e) { CHAT.cfg = null; }
  const ok = !!(CHAT.cfg && CHAT.cfg.configured);
  const m = $('#chat-model');
  if (m) m.textContent = ok ? CHAT.cfg.model : '未配置模型';

  // 没配好就只留「去配置」这一条：示例问句点了也只会报错，不如不给
  const empty = $('#chat-empty');
  if (empty) empty.hidden = !ok;
  const input = $('#chat-input'), send = $('#chat-send');
  if (input) {
    input.disabled = !ok;
    input.placeholder = ok
      ? '说人话就行，例如：帮我把最新活动 S 关卡挂完'
      : '先配置模型才能用';
  }
  if (send) send.disabled = !ok;

  const body = $('#chat-body');
  if (!ok && body && !body.querySelector('.chat-setup')) {
    const tip = document.createElement('div');
    tip.className = 'chat-empty chat-setup';
    tip.innerHTML = '<p>还没配置模型，配好就能用了：</p>'
      + '<button class="chat-chip" id="chat-go-setup">配置模型（Base URL / 模型名 / API Key）</button>';
    body.appendChild(tip);
    const btn = $('#chat-go-setup');
    if (btn) btn.addEventListener('click', () => openAgentSettings());
  } else if (ok) {
    // 配好了就把那条提示撤掉，别留在面板上
    const t = $('#chat-body .chat-setup');
    if (t) t.remove();
  }
}

/* 助手是模块 tab 里的一页，不再有开/关——切到这一页就开始轮询，切走就停。
   之前是右下角浮层，`position: fixed` 压不住设置弹窗（层级打过架），
   而且用户想找它得先在角落里发现一个气泡。 */
function enterChat() {
  CHAT.open = true;
  chatInit().then(() => {
    chatScroll(true);
    chatPoll();
    setTimeout(() => {
      const i = $('#chat-input');
      if (i && !CHAT.sending) i.focus();
    }, 60);
  });
}

function leaveChat() {
  CHAT.open = false;
  clearTimeout(CHAT.timer);
}

async function sendChat(text) {
  text = String(text || '').trim();
  if (!text || CHAT.sending) return;
  const input = $('#chat-input');
  if (input) { input.value = ''; input.style.height = 'auto'; }
  CHAT.sending = true;
  chatBusy(true);
  try {
    const r = await api('/api/chat', { session: CHAT.session, text });
    CHAT.session = r.session || CHAT.session;
    chatPoll();
  } catch (e) {
    CHAT.sending = false;
    toast(e.message, 'err');
  }
}

async function confirmChat() {
  if (!CHAT.session) return;
  try {
    await api('/api/chat/confirm', { session: CHAT.session });
    CHAT.sending = true;
    chatPoll();
  } catch (e) { toast(e.message, 'err'); }
}

async function cancelChat() {
  try { await api('/api/chat/cancel', { session: CHAT.session }); } catch (e) { }
  chatPoll();
}

async function resetChat() {
  try { await api('/api/chat/reset', { session: CHAT.session }); } catch (e) { }
  CHAT.nodes.forEach(n => n.remove());
  CHAT.nodes.clear();
  CHAT.since = 0;
  const body = $('#chat-body');
  if (body) {
    body.innerHTML = '<div class="chat-empty" id="chat-empty"></div>';
    paintChatEmpty();
  }
  // 右栏执行日志一起清掉
  const side = $('#chat-side-log');
  if (side) side.innerHTML = '<div class="chat-side-empty" id="chat-side-empty">skill / 后台任务的进度日志会显示在这里。</div>';
  chatInit();
}

function bindChatChips(root) {
  $$('.chat-chip[data-ask]', root || document).forEach(b => {
    if (b.dataset.bound) return;
    b.dataset.bound = '1';
    b.addEventListener('click', () => sendChat(b.dataset.ask));
  });
}

function bindChat() {
  const input = $('#chat-input');
  if (!input) return;
  $('#chat-reset').addEventListener('click', resetChat);
  const st = $('#chat-skilltest');
  if (st) st.addEventListener('click', chatSkillTest);
  const logClear = $('#chat-log-clear');
  if (logClear) logClear.addEventListener('click', () => {
    const side = $('#chat-side-log');
    if (side) side.innerHTML = '<div class="chat-side-empty" id="chat-side-empty">skill / 后台任务的进度日志会显示在这里。</div>';
  });
  $('#chat-send').addEventListener('click', () => sendChat(input.value));
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(118, input.scrollHeight) + 'px';
    chatBusy(CHAT.sending);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendChat(input.value);
    }
  });
  bindChatChips(document);
  paintChatEmpty();     // 引导卡片是 JS 生成的，index.html 里只留一个空壳
}

/* 侧栏：MAA 现在跑到哪（读 /api/watch/status，只读，不碰 MAA） */
let _runStripTimer = null;
async function refreshRunStrip() {
  const box = $('#run-strip');
  if (!box) return;
  try {
    const d = await api('/api/watch/status');
    const ex = d.explain || {};
    if (!d.state || d.state === 'idle') {
      box.className = 'run-strip';
      box.textContent = '';
      return;
    }
    const level = ex.level || 'ok';
    box.className = 'run-strip ' + level;
    let html = esc(ex.text || d.state);
    if (d.resumeCount) html += ' · 已自动恢复 ' + d.resumeCount + ' 次';
    if (ex.hint && level !== 'ok') html += '<span class="rs-hint">' + esc(ex.hint) + '</span>';
    box.innerHTML = html;
  } catch (e) {
    box.textContent = '';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  bindChat();
  refreshRunStrip();
  _runStripTimer = setInterval(refreshRunStrip, 15000);
});
