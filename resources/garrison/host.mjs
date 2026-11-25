#!/usr/bin/env node
// 卫戍协议·策略刻章速刷 —— MAA-PRTS 子进程宿主
//
// 由 backend/core/garrison.py 按账号懒启动（一个账号一个进程，多账号天然并行）：
//   node host.mjs --core <核心目录> --checkpoint <检查点文件> --token <随机串> \
//                 [--parent-pid <pid>] [--port 0]
//
// 与独立版 farm-ui.mjs 的区别：没有网页（前端就是 MAA-PRTS 自己的界面），
// 端口由 OS 分配（--port 0），启动后往 stdout 打一行 `READY <port> <token>`
// 作为握手；所有 /api/* 请求必须带 x-garrison-token 头，防止误连/串台。
// 另有 /api/shutdown 供 Python 退出时优雅收尾（stop → dispose → exit）。
//
// 核心目录（service.mjs，GPL-3.0-or-later）不在本仓库、不进安装包，
// 由 garrison.py 在运行根 garrison/core 等约定位置检测。

import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import fs from 'node:fs';

const argv = process.argv.slice(2);
function flagVal(name) {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
}
const coreDir = path.resolve(flagVal('--core') || '');
const CHECKPOINT_FILE = path.resolve(flagVal('--checkpoint') || path.join(coreDir, '..', 'checkpoint-last.json'));
const TOKEN = flagVal('--token') || null;
const PARENT_PID = Number(flagVal('--parent-pid')) || 0;
const PORT = Number(flagVal('--port') || 0);

function log(...args) {
  // 运行轨迹走 stderr：stdout 只留 READY 握手行，Python 按行解析
  console.error('[host]', new Date().toISOString().slice(11, 19), ...args);
}

if (!fs.existsSync(path.join(coreDir, 'service.mjs'))) {
  console.error(`找不到核心: ${path.join(coreDir, 'service.mjs')}`);
  process.exit(1);
}

// ---------- 加载核心 ----------
const core = await import(pathToFileURL(path.join(coreDir, 'service.mjs')).href);

const diagBuffer = []; // 最近诊断 {t, name, data}
function pushDiag(name, data) {
  diagBuffer.push({ t: Date.now(), name, data });
  if (diagBuffer.length > 400) diagBuffer.splice(0, diagBuffer.length - 400);
}

let service;
service = await core.create({
  emit(event, data) {
    if (event === 'diagnostic') pushDiag(data?.note ?? data?.name ?? JSON.stringify(data).slice(0, 80), data);
    else if (event === 'checkpoint') {
      writeFile(CHECKPOINT_FILE, JSON.stringify(data, null, 2))
        .catch(() => {})
        .finally(() =>
          service
            .confirmCheckpoint({ taskId: data.taskId, seq: data.seq })
            .catch(() => {})
        );
      pushDiag('checkpoint.saved', { seq: data.seq });
    }
    // "state" 事件无需处理：前端轮询 service.state() 即可
  },
});
log('核心已加载:', coreDir);

const BUSY = ['running', 'stopping', 'starting'];
async function busy() {
  const s = await service.state();
  return BUSY.includes(s.status);
}

// ---------- HTTP 服务 ----------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const authed = !TOKEN || req.headers['x-garrison-token'] === TOKEN;
  try {
    if (!authed) {
      sendJson(res, 403, { error: 'token 不对（不是拉给自己的后端）' });
    } else if (req.method === 'GET' && url.pathname === '/api/state') {
      const [state, checkpoint] = await Promise.all([
        service.state(),
        fs.existsSync(CHECKPOINT_FILE)
          ? readFile(CHECKPOINT_FILE, 'utf8').then((t) => JSON.parse(t)).catch(() => null)
          : null,
      ]);
      sendJson(res, 200, { state, diag: diagBuffer.slice(-80), hasCheckpoint: !!checkpoint });
    } else if (req.method === 'POST' && url.pathname === '/api/test') {
      if (await busy()) return sendJson(res, 409, { error: '速刷运行中，请先停止' });
      const body = await readBody(req);
      const { device, options } = buildOptions(body);
      const r = await service.testConnection({ device, options });
      sendJson(res, 200, { message: r?.message ?? '连接成功' });
    } else if (req.method === 'POST' && url.pathname === '/api/start') {
      if (await busy()) return sendJson(res, 409, { error: '速刷任务已在运行' });
      const body = await readBody(req);
      const { device, options } = buildOptions(body);
      const taskId = `prts-${Date.now()}`;
      service
        .start({ taskId, device, options })
        .then((r) => { pushDiag('farm.finished', r ?? {}); log('速刷结束:', JSON.stringify(r ?? {}).slice(0, 300)); })
        .catch((e) => { pushDiag('farm.error', { error: String(e?.message ?? e) }); log('速刷出错:', e?.message ?? e); });
      sendJson(res, 200, { ok: true, taskId });
    } else if (req.method === 'POST' && url.pathname === '/api/resume') {
      if (await busy()) return sendJson(res, 409, { error: '速刷任务已在运行' });
      if (!fs.existsSync(CHECKPOINT_FILE)) return sendJson(res, 404, { error: '没有已保存的检查点' });
      const checkpoint = JSON.parse(await readFile(CHECKPOINT_FILE, 'utf8'));
      const body = await readBody(req);
      const { device, options } = buildOptions(body);
      const taskId = `prts-${Date.now()}`;
      service
        .start({ taskId, device, options, resume: checkpoint.checkpoint ?? checkpoint })
        .then((r) => { pushDiag('farm.finished', r ?? {}); log('从检查点恢复后结束:', JSON.stringify(r ?? {}).slice(0, 300)); })
        .catch((e) => { pushDiag('farm.error', { error: String(e?.message ?? e) }); log('恢复出错:', e?.message ?? e); });
      sendJson(res, 200, { ok: true, taskId });
    } else if (req.method === 'POST' && url.pathname === '/api/stop') {
      if (!(await busy())) return sendJson(res, 409, { error: '当前没有运行中的任务' });
      service.stop().catch(() => {});
      sendJson(res, 200, { ok: true });
    } else if (req.method === 'POST' && url.pathname === '/api/shutdown') {
      // Python 退出/回收时调用：先回执再异步收尾，别让调用方等 dispose。
      // 稍等一拍再退出，确保回执的 TCP 段先发出去（立即 exit 会掐掉响应）
      sendJson(res, 200, { ok: true });
      setTimeout(() => shutdown(0), 100);
    } else {
      sendJson(res, 404, { error: 'not found' });
    }
  } catch (e) {
    sendJson(res, 500, { error: String(e?.message ?? e) });
  }
});

function sendJson(res, code, obj) {
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let buf = '';
    req.on('data', (c) => (buf += c));
    req.on('end', () => {
      try { resolve(buf ? JSON.parse(buf) : {}); } catch (e) { reject(e); }
  });
    req.on('error', reject);
  });
}
// goal 白名单：medal=策略徽章（默认）；task-N=叠层目标（v1.1.7 起支持）。
// 与后端 garrison.py 的 GOALS 保持一致。
const GOALS = ['medal', 'task-1', 'task-2', 'task-3', 'task-4', 'task-5', 'task-6', 'task-7'];

function buildOptions(body = {}) {
  const device = body.device ?? 'mumu';
  // 模拟模式（v1.1.10 起上游拆成两套口径）：策略徽章读 medalMode，叠层任务
  // 读 mode+level（1/5/12）；旧核心（≤1.1.7）只认 mode。两套一起发。
  const mode = body.mode === 'standard' ? 'standard' : 'quick';
  const level = [1, 5, 12].includes(Number(body.level)) ? Number(body.level) : 1;
  const options = {
    goal: GOALS.includes(body.goal) ? body.goal : 'medal',
    medalWins: Number(body.medalWins) === 6 ? 6 : 3,
    mode,
    medalMode: mode,
    level,
    debug: Boolean(body.debug),
    verifyUpgrade: true, identifyUnknown: true, fullBoard: true, useEquipment: true,
    speed: Math.min(5, Math.max(0.5, Number(body.speed) || 1.0)),
    device,
    adbAddress: String(body.adbAddress ?? '127.0.0.1:16384').trim(),
    adbPath: String(body.adbPath ?? '').trim(),
    screenshotMethod: ['auto', 'fast', 'adb'].includes(body.screenshotMethod) ? body.screenshotMethod : 'auto',
    inputMethod: 'auto',
    pcInputMethod: ['cursor', 'message'].includes(body.pcInputMethod) ? body.pcInputMethod : 'cursor',
    pcUiScale: Number(body.pcUiScale) === 100 ? 100 : 90,
  };
  return { device, options };
}

server.on('error', (e) => {
  console.error(`HTTP 服务出错: ${e?.message ?? e}`);
  process.exit(1);
});

server.listen(PORT, '127.0.0.1', () => {
  const port = server.address().port;
  // 握手行：Python 从 stdout 读到它才认为后端就绪（token 一并带回）
  process.stdout.write(`READY ${port} ${TOKEN ?? '-'}\n`);
  log(`已监听 127.0.0.1:${port}，检查点 ${CHECKPOINT_FILE}`);
});

// ---------- 存活与退出 ----------
// Python 主进程不在了（崩溃/被杀）就自己收尾，别变成孤儿继续点游戏
if (PARENT_PID > 0) {
  setInterval(() => {
    try { process.kill(PARENT_PID, 0); } catch { log('父进程已退出，跟随退出'); shutdown(0); }
  }, 5000).unref();
}

let exiting = false;
async function shutdown(code) {
  if (exiting) return;
  exiting = true;
  log('正在停止…');
  try { if (await busy()) { log('有任务在跑，先停任务…'); await service.stop(); } } catch {}
  try { await core.dispose(service); } catch {}
  process.exit(code);
}
process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));
process.on('exit', () => { try { server.close(); } catch {} });
