# v2.6 开发计划：进度对齐 · 观测自愈 · 内置助手

> **进度（2026-11-16）**：M1–M5 全部落地，本机已跑通（未提交）。
> 新增 `core/progress.py` / `asstlog.py` / `watch.py` / `multiacct.py` / `agent.py` /
> `backend/cli.py`，前端加了助手面板与侧栏跑机状态条，冒烟到 B32（33 项全绿）。
> 设计细节见 `docs/agent.md`，下面保留原始计划与决策依据。

## 总览

三块能力有严格依赖，顺序不能倒：

```
M1 关卡/进度数据 ─────────────→ M4 助手（"我还差哪几关"）
M2 日志观测 ──→ M3 自动恢复 ──→ M4 助手（"昨天为什么失败"）
```

没有 M1、M2，M4 的对话只能把按钮翻译成人话——用户试两次就回去点按钮了。所以先做数据，再做观测，最后做对话。

| 里程碑 | 内容 | 主要交付 |
|---|---|---|
| M1 | 剧情关恢复 + 森空岛进度对齐 | `resources.py` 双视图、`progress.py` |
| M2 | asst.log 事件流 | `asstlog.py`、运行状态机 |
| M3 | 停滞检测 + 重启续跑 | `watch.py`、`maa.resume()` |
| M4 | 内置助手 | `agent.py`、`/api/chat`、聊天面板 |
| M5 | 对接 MAA 接口（按需） | 内部 CLI / MaaFw |

---

## M1 关卡层：剧情关与森空岛口径

### 已核实

- `resources/arknights.json` 里 **3647 条关卡全部带 `story` 字段**，其中 208 条为 `true`。资源包不需要重做。
- `core/resources.py:93` 的 `_is_story()` 把它们整条丢弃，所以 `_stages()` 只返回 2651 条（3647 减去 208 条剧情关、788 条 `#f#` 突袭变体）。
- `skland.player_info(uid)` 直接返回 raw data，里面已有 `status.mainStageProgress`、`activity[].zones[].clearedStage/totalStage`、`campaign.records[].maxKills`，只是没人解析。
- 实测 98 个 zone 里 46 个能对上我们的 `zoneId`（`act13d2_zone1` 这种格式完全一致）；对上的里面总数对不齐，因为森空岛把剧情关、突袭关也算进 `totalStage`。

### 任务

1. **`core/resources.py`：加双视图**

   ```python
   def _stages(include_story=False)   # 默认保持现在的行为
   def story_of(sid) -> bool
   ```

   默认不传参即现状，现有调用（`resolve` / `next_stage` / `activity_list`）行为完全不变，零回归。需要剧情关的地方显式传 `True`。

2. **`next_stage()` 不用改**：它走默认视图，剧情关天然不在序列里。这一点有 `B29` 兜着——将来谁把 `_stages()` 的默认关了，测试会立刻炸。

3. **`resolve()` 加剧情关精确命中**（`mode='story'`）

   `TL-ST-1` / `EG-3` 这类代号只属于剧情关，以前搜了直接落空。现在返回一张剧情卡，前端标「剧情 · 送 1 源石 · 不可挂机」。

   有个取舍：`9-1` / `12-1` 既是剧情关代号、又是章节前缀（`12-10 ~ 12-19` 都以它开头），这种让位给系列展开，不抢。纯剧情代号才给卡。

3. **新增 `core/progress.py`**

   `parse(player_info_data)` → 统一进度模型：

   | 来源 | 字段 | 用途 |
   |---|---|---|
   | `status.mainStageProgress` | `st_09-01` | 主线推到哪，配合 `_flow` 算后续关卡 |
   | `campaign.records[].maxKills` | `< 400` 即未打满 | 剿灭缺口 |
   | `activity[].zones[]` | `clearedStage` / `totalStage` | 活动还有几关没打 |

   `match_zone(zoneReplicaId)` 负责对齐我们的 `zoneId`。

4. **zone 对账**

   对不上的 52 个 zone 写进 `debug/progress-miss.json`，不静默丢弃。**口径差异必须显式记录**：森空岛 `totalStage` 含剧情关和突袭关，我们的表不含，所以只做 zone 级"还有剩"判断，**不拿计数直接切关卡列表**（会错位）。

5. **剧情关的用途分离**

   | 用途 | `include_story` | 展示方式 |
   |---|---|---|
   | 作业搜索 / 推荐 | `False` | 不出现 |
   | 活动展开 | `True` | 标"剧情"徽标，无「加入」按钮 |
   | 进度核对 | `True` | 只算数，不列表 |
   | 下一关 | `False` | 跳过 |

6. **前端**：`frontend/style.css` 加 `.stage-badge.story` 静音样式；活动展开里剧情行不可点。

### 验收

- `_stages()` 仍是 3439 条，`_stages(True)` 是 3647 条
- 搜 `TL-` 的结果里不出现 `TL-ST-1`
- 活动展开能看到 `TL-ST-1`，但点不动
- 用真实账号同步一次，主线进度点、剿灭 `maxKills`、活动 zone 计数都能读出来
- `tools/smoketest/smoke_test.py` 补断言：剧情关不进搜索、计数一致

### 风险

- `activity_list()` 的 `count` 会从"能打的关"变成"含剧情的关"，前端"共 N 关"的文案要区分
- 用户本地挂旧资源包时没有 `story` 字段 → `_is_story()` 的兜底正则已覆盖 208 条，不用动

---

## M2 观测层：asst.log 事件流

### 已核实

`asst.log` 是 TRC 级，但**回调 JSON 被 GUI 原样打印**，这是一条现成的结构化事件流：

```
CopilotListLoadTaskFileSuccess   {"file_name":"TL-S-1_110074.json","id":0,"stage_name":"act55side_s01"}
SubTaskStart / SubTaskCompleted  {"task":"BattleStartRaid","taskid":1,"action":"ClickSelf"}
ProcessTask                      {"cur_task":"Copilot@WaitUntilEndOfAction",
                                  "details":{"cur_retry":0,"retry_times":20,
                                             "to_be_recognized":["Copilot@SkipThePreBattlePlot", ...]}}
MultiCopilotTask SubTaskCompleted
```

即"现在跑第几个作业、哪一关、卡在哪个节点、正在识别什么、重试到第几次"，全部可读，一个像素都不用看。

### 任务

1. **`core/asstlog.py`**

   - 增量读：记 byte offset，每次只读新增部分
   - 处理轮转：文件变小 → 从头读（`asst.bak.log` 会到 60MB+）
   - 只解析含 `append_callback` 且带 JSON 的行，其余丢弃
   - 输出事件：`{t, kind, taskid, stage, detail}`

2. **运行状态机**

   ```
   idle → running → (done | stalled | failed)
   ```

   | 状态 | 判据 |
   |---|---|
   | `running` | 60 秒内有新事件 |
   | `stalled` | 180 秒无事件，且最后节点是 `ProcessTask` 且 `cur_retry/retry_times > 0.7` |
   | `failed` | 候选全部重试耗尽，或 MAA 进程消失 |
   | `done` | `MultiCopilotTask` 完成 |

   阈值为什么是 180 秒：MAA 自己 `retry_times: 20`、每次约 0.7 秒，**14 秒就放弃**；它的停滞提醒是 30 分钟。中间这一大段空白正好是我们要补的。设太短会和 MAA 自己的重试打架。

3. **与 `tasks.py` 集成**：新增 `core/watch.py`，后台线程 5 秒轮询，状态变化写进任务日志。

### 验收

- 用现有 `asst.bak.log`（含一次完整多作业运行）回放，能还原"跑了哪几关、每关起止时间"
- 人为截断日志模拟卡住，状态机 180 秒后翻 `stalled`
- CPU 占用可忽略（只读增量）

### 风险

**日志格式随 MAA 版本变。** 所以解析要宽松：只认 JSON 里的 `taskchain` / `subtask` / `what` 字段，不认整行前缀；识别不到的事件直接跳过，不抛异常。MAA 升级后最坏结果是"事件变少"，不能是"程序报错"。

---

## M3 自愈层：停滞判定与重启续跑

### 任务

1. **`maa.resume(items, profile)`**：复用 `apply_plan`，只写未完成的条目
2. **恢复流程**

   ```
   检测到 stalled
     → 记下当前 stage_name
     → 关 MAA（复用 _close_maa）
     → 重写配置：已完成的不写，当前关及之后的重新写
     → 启动 MAA（走 StartUp，把游戏拉回主界面）
   ```

3. **次数与退避**：同一关连续失败 3 次 → 停止自动恢复，转人工
4. **UI**：跑机面板显示"第 3/8 关 · TL-5"；恢复时提示"卡在 TL-ST-1，已重启续跑"

### 为什么是重启而不是点一下

- MAA 的 `FullStageNavigation` 是"关卡列表内右滑 10 次 + 检测关卡名 + 左滑"的**局部搜索**，没有"从任意界面回到关卡列表"的能力
- 重启会走 `StartUp`，把游戏拉回主界面——**用已知状态覆盖未知状态**
- 一个动作覆盖剧情、弹窗、网络、编队所有卡住；点跳过只能救剧情一种

### 代价

当前那一关从头再打，最多浪费一关的理智。这条要写进 UI 文案，别让用户以为是 bug。

### 验收

- 造真实卡住场景（连挂 TL-5 + TL-6，中间过剧情），看能否自动续跑完成
- 连续失败 3 次后确实停手，并报出卡在哪一关

---

## M4 助手层：内置 agent loop

### 工具范围

| 期 | 工具 | 风险 |
|---|---|---|
| P0 | `list_stages` `find_ops` `op_detail` `progress` `plan_suggest` | 零，全只读 |
| P1 | `add_plan`（必须带确认弹窗） | 低，有备份 |
| 永不给 | 文件系统、shell、任意 HTTP | — |

### 任务

1. `core/agent.py`：loop + 工具注册表，`max_turns = 12`，超限强制收口
2. `POST /api/chat`：SSE 流式（复用现有 `net.py`，已带代理支持）
3. 设置页：Base URL / Key / 模型名三件套（OpenAI 兼容协议），key 走 `vault.py` 的 DPAPI
4. 前端：右下角面板，只在作业、抽卡、签到三个页面出现
5. 降级：检测到模型不支持 function calling → 只问答不执行

### 依赖

- "还差哪几关" → M1
- "昨天为什么失败" → M2

### 验收

- 问"这周活动我还差哪几关"能答对，且答案里的关卡号都能在关卡表里查到
- 问"昨天为什么失败"能说出卡在哪一关、卡在什么节点
- 模型不支持工具调用时不崩、自动降级

---

## M5 对接 MAA 接口（按需）

- 先把 M1–M4 用到内部能力做成 `python -m backend.cli`，供外部 harness（Claude Code / WorkBuddy）调用
- MaaFw 只在 MAA 覆盖不到的地方引（活动入口、领活动奖励、活动图导航），LGPL 作为依赖调用没问题
- 不引 maa-cli：我们走的是"写配置 + 启动 MAA.exe"，再引一套平行的命令行驱动会多一个 20–30MB 的维护面

---

## 版本节奏

| 版本 | 内容 |
|---|---|
| v2.6.0 | M1 |
| v2.6.1 | M2 + M3 |
| v2.7.0 | M4 |

## 不做

- 通用识图兜底（每种界面一套模板，MAA 一改版全废）
- 自己写 OpenCV / PaddleOCR
- 养成（`OperProgress` 另立项，先实测 MAA 认不认这个任务类型）
- 多账号自动切号（手机号前 3 后 4 绕不开，只能用户自己在 MAA 里配好）

## 待确认

1. 停滞阈值取 180 秒
2. 剧情关在界面上显示但不可加入
3. 卡住后自动重启续跑，接受当前关重打
