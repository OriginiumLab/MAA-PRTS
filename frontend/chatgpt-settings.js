/* ChatGPT controls use only public connection status from the local backend. */
const CHATGPT_USAGE = 'https://chatgpt.com/settings/usage';
const CHATGPT_WELCOMED = new Set();

function bindChatGPTSettings(root) {
  const provider = $('#set-ag-provider', root);
  const hint = $('#set-ag-provider-state', root);
  const keyCard = $('#set-ag-api', root);
  const card = $('#set-ag-chatgpt', root);
  let busy = false;
  let catalog = null;
  card.innerHTML = `
    <p class="muted">授权后，助手使用你可用的 ChatGPT 套餐额度。发送消息时，当前对话和相关游戏工具的结果会发给 OpenAI。</p>
    <label class="field" id="chatgpt-account-field"><span>ChatGPT 连接</span>
      <select id="chatgpt-account"></select>
    </label>
    <div class="chatgpt-actions">
      <button class="btn-plain chatgpt-signin" data-oai-siwc-button id="chatgpt-login">Continue with ChatGPT</button>
      <button class="btn-plain" id="chatgpt-reconnect" hidden>重新授权</button>
      <button class="btn-plain" id="chatgpt-logout" hidden>退出登录</button>
    </div>
    <label class="field" id="chatgpt-model-field" hidden><span>模型</span>
      <select id="chatgpt-model"></select>
    </label>
    <div class="chatgpt-actions">
      <button class="btn-plain mini" id="chatgpt-refresh" hidden>刷新模型</button>
      <a href="${CHATGPT_USAGE}" id="chatgpt-usage">管理用量</a>
      <button class="btn-plain mini" id="chatgpt-resume" hidden>恢复套餐请求</button>
    </div>
    <p class="onb-hint" id="chatgpt-state" role="status"></p>
  `;
  const account = $('#chatgpt-account', card);
  const model = $('#chatgpt-model', card);
  const state = $('#chatgpt-state', card);
  const login = $('#chatgpt-login', card);
  const reconnect = $('#chatgpt-reconnect', card);
  const logout = $('#chatgpt-logout', card);
  const refresh = $('#chatgpt-refresh', card);
  const resume = $('#chatgpt-resume', card);

  const request = (action, options = {}) => api('/api/agent/chatgpt', { action, ...options });
  const status = () => api('/api/agent/status');
  const active = () => S.agent?.chatgpt?.active;

  function paint(d) {
    S.agent = d;
    const g = d.chatgpt || {};
    const pending = !!g.login?.running;
    provider.value = d.provider;
    provider.disabled = busy || pending;
    card.hidden = d.provider !== 'chatgpt';
    keyCard.hidden = !card.hidden;
    if (card.hidden) return;
    $('#chatgpt-account-field', card).hidden = !g.accounts?.length;
    account.replaceChildren(...(g.accounts || []).map(a =>
      new Option(`连接 ${a.number} · ${a.label}${a.connected ? '' : ' · 套餐未启用'}`, a.id)));
    account.value = g.active || '';
    account.disabled = busy || pending;
    login.disabled = busy || pending;
    login.textContent = pending ? '等待浏览器授权…' : 'Continue with ChatGPT';
    login.title = g.accounts?.length ? '添加另一个 ChatGPT 账号或工作区' : '在系统浏览器中登录';
    reconnect.hidden = !g.active;
    logout.hidden = !g.signedIn;
    refresh.hidden = !g.permission;
    resume.hidden = !g.limited;
    reconnect.disabled = logout.disabled = refresh.disabled = resume.disabled = busy || pending;
    $('#chatgpt-model-field', card).hidden = !g.permission;
    const list = catalog?.id === g.active && catalog?.connection === g.connection ? catalog.list : [];
    model.replaceChildren(new Option('选择模型', ''), ...list.map(m => new Option(m.label, m.id)));
    if (g.model && !list.some(m => m.id === g.model)) model.add(new Option(g.model, g.model));
    model.value = g.model || '';
    model.disabled = busy || pending || !list.length;
    state.textContent = g.error || g.login?.error || (pending ? '请在系统浏览器完成授权。'
      : g.limited ? '套餐请求已暂停。查看用量后可手动恢复。'
      : g.permission ? (g.model ? '已授权使用 ChatGPT 套餐' : '已授权，请选择模型')
      : g.signedIn ? '已登录。请选择重新授权并允许使用套餐额度。'
      : '请登录并授权使用套餐额度。');
  }

  async function loadModels(d) {
    const g = d.chatgpt;
    if (!g?.permission || d.provider !== 'chatgpt') return d;
    if (catalog?.id === g.active && catalog?.connection === g.connection) return d;
    const result = await request('models', { profile: g.active });
    catalog = { id: g.active, connection: g.connection, list: result.models };
    return d;
  }

  async function change(work) {
    busy = true;
    hint.textContent = '';
    if (S.agent) paint(S.agent);
    try {
      await work();
      if (root.isConnected) paint(await status());
    } catch (e) {
      hint.textContent = e.message;
    } finally {
      busy = false;
      if (root.isConnected && S.agent) paint(S.agent);
      chatInit();
    }
  }

  async function watchLogin(d) {
    paint(d);
    while (root.isConnected && d.chatgpt?.login?.running) {
      await new Promise(resolve => setTimeout(resolve, 800));
      d = await status();
      if (root.isConnected) paint(d);
    }
    if (!root.isConnected) return;
    const g = d.chatgpt;
    if (g?.login?.newConnection === g?.connection && g?.connection
        && !CHATGPT_WELCOMED.has(g.connection)) {
      CHATGPT_WELCOMED.add(g.connection);
      modal(`<h3>已启用 ChatGPT 套餐</h3>
        <p>在此应用中的符合条件的请求将使用你的 ChatGPT 套餐额度。你可以在 ChatGPT 设置中管理用量。</p>
        <div class="modal-foot"><button class="btn-solid" data-close>知道了</button></div>`);
    }
    await loadModels(d);
  }

  provider.addEventListener('change', () => {
    const value = provider.value;
    change(async () => {
      await api('/api/agent/config', { provider: value });
      const d = await status();
      paint(d);
      await watchLogin(d);
    });
  });
  login.onclick = () => change(async () => {
    await request('login');
    await watchLogin(await status());
  });
  reconnect.onclick = () => change(async () => {
    await request('login', { profile: active(), consent: !S.agent.chatgpt.permission });
    await watchLogin(await status());
  });
  account.onchange = () => {
    const profile = account.value;
    change(async () => {
      await request('select', { profile });
      const d = await status();
      paint(d);
      await loadModels(d);
    });
  };
  model.onchange = () => {
    const selected = model.value;
    if (!selected) { paint(S.agent); return; }
    change(() => request('model', { profile: active(), model: selected }));
  };
  refresh.onclick = () => change(async () => {
    catalog = null;
    await loadModels(await status());
  });
  resume.onclick = () => change(() => request('resume', { profile: active() }));
  logout.onclick = () => change(async () => {
    const result = await request('logout', { profile: active() });
    if (!result.revoked) hint.textContent = '已在本机退出。OpenAI 尚未确认远端撤销，可到 ChatGPT 设置断开连接。';
  });
  $('#chatgpt-usage', card).onclick = event => {
    event.preventDefault();
    openUrl(CHATGPT_USAGE);
  };
  change(async () => {
    const d = await status();
    if (!root.isConnected) return;
    paint(d);
    if (d.provider === 'chatgpt') await watchLogin(d);
  });
}
