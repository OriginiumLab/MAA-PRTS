# 内置助手与命令行（M4 / M5）

## 这东西解决什么

按钮能表达的是「确定的、我知道要做什么」；助手能表达的是另外三类：

- **说不清楚的**：「这周活动快结束了，帮我清一下还没打的关」
- **要判断的**：「昨天那两关为什么失败」「这两份作业哪个更适合我」
- **一次性的临时编排**：「12 个账号各挂一遍 TL-5」

如果什么流程都塞进聊天，最后会变成「用聊天替代按钮」——用户试两次就回去点按钮了，因为按钮更快更准。所以助手的价值在**数据和判断**，不在点按钮。

## 为什么不是 MCP

只有一个宿主（自己的窗口）。MCP 每个工具的 schema 是**常驻上下文**的，工具一多反而更费；CLI + 手写 schema 更省，而且能被 shell 权限规则逐条管住。

参考的 [MaaPilot](https://github.com/mansicer/MaaPilot) 在 4 个 harness 上验证了「CLI + SKILL.md 文档」这条路够用，它也没走 MCP。

**不引 maa-cli**：命令行那套我们自己做了一个更薄的（见 `backend/cli.py`），只包
我们自己的模块，不重复引 20–30MB 的 MaaCore 封装。

## 两条执行路线

挂机有两个完全不同的执行路径，**用错会一条都跑不起来**：

| | GUI 路线（`maaplan.ensure_profile`） | 直连路线（`maacore.Engine`） |
|---|---|---|
| 做什么 | 写 `<档案>.TaskQueue` + `RunDirectly`，MAA 自己连自己跑 | 自己 `AsstCreateEx` → `AsstConnect` → `AsstAppendTask` → `AsstStart` |
| 用户看得见 MAA 界面 | 看得见 | 看不见（进度看跑机面板） |
| 能抄作业 | **不能** | 能 |
| 能跑小活动（Custom） | 不能（GUI 队列没入口） | 能 |
| 用在哪 | 只配不跑（`/api/maa/build`） | 所有实际挂机（`build_run` / `/api/multi/apply`） |

**为什么 GUI 路线不能抄作业（实测 v6.20.1）：**
`TaskQueue` 里放一个 `$type: CopilotTask` 的任务，MAA 启动时
`TaskQueueViewModel.InitializeItems()` 直接抛 `NotImplementedException`——
不是「这个任务不执行」，而是**整个任务列表变空白**，连公招、基建一起消失。
所以 `build_queue()` 会把 Copilot 剔出去并记一条 skipped。

**为什么 copilot 的 filename 必须是绝对路径（实测 v6.22 核心）：**
相对路径不报错，`AsstAppendTask` 就是返回 0（静默不入队）。而且
`copilot_list` 每项的键是 `filename` / `stage_name` / `is_raid`——写成
`file_name` 会抛 C++ 异常把进程整个带崩（`0xe06d7363`）。这两条冒烟 B33 都钉住了。

**抄作业为什么要「登录 → 停 → 导航 → 开打」四段（2026-11-17 SV 真机）：**
Copilot 自己只会在**关卡选择页**横向滑（`StageNavigationSlowlySwipeLeft`），
老活动（MN/SV/WR…，共 46 个常驻活动，见 `stagenav.EVENTS`）MAA 资源里没有
入口模板，它会一路滑主线然后报失败。
所以 `run_core` 把 StartUp 和 Copilot 拆成两次 `AsstStart`：

```
append StartUp  → start → 等 StartUp 链结束（登录完成，回主界面）
AsstStop        → 把 MAA 停住（不停它会继续截屏/滑动，和我们的点击抢界面）
stagenav        → 曲谱 → 乐章收录 → 找卡片 → 进入活动 → 停在关卡地图
append Copilot  → start → ClickStageName 在地图上 OCR 到 SV-1 → 开始行动
```

Copilot 参数按用户在 MAA「自动战斗」页常勾的三项给：`formation=true`（自动编队）、
`support_unit_usage=1`（缺人时借干员）、`use_sanity_potion=true`（吃理智药）。
导航失败且这一关有卡片模板时直接抛错——交给 MAA 只会白滑 30 秒。
实测 SV-1：登录后 24 秒内 MAA 就 OCR 到 `SV-1` 并点「开始行动」，一局 2 分半跑完。

## 工具集（白名单，共 14 个）

| 工具 | 做什么 | 风险 |
|---|---|---|
| `list_accounts` | 左侧账号 + 进度摘要 + MAA 档案绑定 | 只读 |
| `list_activities` | 最近开启的活动（正式名/代号前缀/关数） | 只读 |
| `search_stages` | 关键词 → 真实关卡（代号/内部 id/难度/是否剧情关） | 只读 |
| `find_ops` | 这些关有哪些作业，按该账号练度分档 | 只读 |
| `op_detail` | 一份作业缺什么：练度不够（橙）、要借谁（紫） | 只读 |
| `account_progress` | 主线推到哪、剿灭几张没满、活动剩几个区 | 只读 |
| `run_status` | MAA 现在跑到第几关、卡在哪个节点 | 只读 |
| `maa_capabilities` | 这台 MAA 能配哪些任务、有哪些预设 | 只读 |
| `list_profiles` | MAA 里有哪些档案、当前是哪个、几个作业 | 只读 |
| `plan_multi_account` | 给多个账号排方案（**只生成**，不写盘） | 只读 |
| **`build_run`** | **主力**：配任务 + 写作业 + 直连跑起来 | **写**，需确认 |
| `apply_plan` | 把 plan_multi_account 的方案跑起来 | **写**，需确认 |
| `add_stages` | 单账号挂一组关卡（`build_run` 的快捷方式） | **写**，需确认 |
| `stop_run` | 停掉正在跑的 MAA | **写**，需确认 |

**永不给**：文件系统、shell、任意 HTTP。

### `build_run` 的四种 mode

| mode | 任务链 | 要关卡 |
|---|---|---|
| `copy` | 开始唤醒 → 自动战斗 | 要 |
| `fight` | 开始唤醒 → 理智作战 | 要（不抄作业，纯刷关） |
| `daily` | 开始唤醒 → 自动公招 → 基建换班 → 信用收支 → 领取奖励 | 不要 |
| `daily_copy` | 日常全清 + 自动战斗 | 要 |

不在这四种里的组合，用 `tasks` 参数直接给顺序（`startup recruit infrast
fight copilot mall award reclamation roguelike`）。

`start=false` 走 GUI 路线：只写 `PRTS-` 档案，不连模拟器。抄作业那部分配不进
档案（原因见上），返回里带 `copilotNote` 如实说明，不允许含糊成「都配好了」。

`run_plan` 开跑前会检查同目录的 MAA 进程，正在跑就先 `maa.close()`——同一个
模拟器不能被两条 adb 连接同时戳，否则表现成「点了没反应」。

**工具出参就是 `accounts.list_accounts()` / `progress.summary()` 的字段**，改这两个
函数的键名时要同步改工具，否则模型拿到的是 `null` 或直接 `KeyError`（2026-11-16
就踩过：工具里写 `c['name']`，而列表给的是 `nickName`/`alias`；又取不存在的
`mainLeft`/`activityZones`，每个账号都是 null，模型只好挨个再问一遍）。冒烟 B32
用真实账号文件钉住了这几个键。

`summary.mainKnown=false` 表示森空岛这次没给 `mainStageProgress`——`mainRemaining`
的 0 是「没数据」不是「打完了」，提示词里明确禁止据此断言已通关。

工具内部抛异常时回 `{error, hint}`，`hint` 写明「原样重试会得到同样结果」。模型
看到裸 `KeyError` 会连着重试两三次，最后把锅甩给用户。

两个写工具都**不等结果**：返回 `{started: true, taskId, note}` 就结束，活丢给后台线程。
写 MAA 要关客户端、备份、重启、跑图，几分钟到几十分钟，卡在工具调用里会让整轮对话
超时。前端拿 `taskId` 在 `tool_result` 那条右侧挂「看进度」（`chatTaskModal`）。

## 三道防线

1. **关卡必须过表**
   模型给的 stageCode 一律先进 `resources.resolve`，查不到就把候选回给它重选。**绝不直接透传给 MAA**——这是幻觉唯一会造成实际后果的地方。

2. **写前必须确认**
   `agent._guard()`：`settings.agent.autoApply` 关着时，写工具只返回 `need_confirm` + 一段人话摘要，前端渲染成确认卡片，用户点了才真执行。
   确认后 `_run_pending()` 临时把 autoApply 打开跑一次再恢复，跑完把结果喂回模型让它继续说话。

3. **工具白名单**
   模型能做的每一件事都是预先允许的。拿不到的东西就干不了，比提示词里写「不许」可靠一万倍。

## 协议与配置

OpenAI 兼容 `/chat/completions`，`stream=True` + `tools`：

- Base URL / 模型名存 `settings.json` 的 `agent` 段
- **apiKey 不进 settings.json**，走 `vault.protect()`（Windows DPAPI）单独存 `config/agent.key`，只进不出——接口只回 `hasKey` 布尔
- Base URL 容忍 `.../v1/chat/completions` 这种完整路径（自动去尾）
- `stream+tools` 被中转站拒绝时自动退回非流式

流式拼装：`tool_calls` 按 `index` 累积（名字和参数都是分片下发的，拼完才能解析）。同一轮的内容事件原地累加 `text`，带 `live` 标记，`Session.view()` 每次都带上它——否则前端 `since` 一前进就再也拿不到后续文字。

## 对话接口

| 接口 | 说明 |
|---|---|
| `GET /api/agent/status` | 配好没有（`configured` / `hasKey` / `model`） |
| `GET/POST /api/agent/config` | 读写连接参数（POST 带 `apiKey` 才改 key） |
| `POST /api/chat` | `{session, text}` → 起后台 loop |
| `GET /api/chat/poll` | `{session, since}` → 新增事件 |
| `POST /api/chat/confirm` | 执行待确认的写操作并继续 |
| `POST /api/chat/cancel` | 取消待确认 |
| `POST /api/chat/reset` | 清空这段对话 |

事件 `kind`：`user` / `say` / `tool` / `tool_result` / `need_confirm` / `error` / `done`。
前端按 `i` 索引 DOM，流式那条原地更新文字。

## 多账号怎么排

MAA 的多账号机制只有一条：**一个配置档案 = 一个账号**。

```json
{"$type": "StartUpTask", "AccountName": "181****5505",
 "AccountSwitchEnabled": true, ...}
```

`AccountName` 是 MAA 拿去和登录界面那行 OCR 结果做**子串匹配**的，所以四种写法都认（官方说明原话：「可输入 123****4567、4567 或 23****45」）：

- 手机号后 4 位 `5505` —— **对话里默认问这个**
- 前 3 位 + 后 4 位 `1573635` / `157 3635` —— 2026-11-22 用户要求也收
- 部分掩码 `23****45`
- 完整掩码 `181****5505` / `157****3635`

本机 MAA 里用户自己的档案写的就是 `136` / `3635` / `1686` / `181****5505`，混着用的。

**这个信息森空岛给不了**——要么问用户，要么照他绑的配置文件读。三条来源按顺序（`acctbind.resolve()`）：

1. 账号绑了 MAA 配置文件（设置 → MAA 路径 → 配置文件）→ 读那份档案 StartUp 的 `AccountName`。**只读**，MAA 里一个字段都不改。
2. 账号存的 `maa.accountName`（设置里手填的、或对话里报过后 `set_account_name` 存下来的）。
3. 都没有 → 问用户（官服问后 4 位；用户主动给 `157****3635` 也照收）。
   用户一报尾号就调 `set_account_name` 存进设置，下次不用再问。

`multiacct._match()` 还留了一条按昵称对档案的兜底（容忍 `小卡拉帕鲁灰#6408` ↔ `小卡拉帕鲁灰`），对不上的才进 `missing`。

问的时候**别让用户拼星号**，直接问「手机号后 4 位是多少」。用户给全号、带横线、星号数量不对、全角数字都行，`acctname.normalize()` 会统一成 `前3****后4`；给 3~4 位片段就原样保留交给 MAA 子串匹配。

> 2026-11-17：这之前校验写死成 `^\d{3}\*{4}\d{4}$`，用户照 MAA 文档只给尾号就被判非法，助手只能反复追问，任务最后出不来。规则现在只有一份：`backend/core/acctname.py`，别再在别处写第二份正则。

> 2026-11-22：`_roster` 原来完全禁读用户的 MAA 档案（怕档案和账号对不上号）。现在不是猜——哪份档案由用户在设置里点，程序照他点的读；他手改过的切号名（`accountNameFrom=manual`）优先，不让档案里的旧号码盖回去。

另外 `maaProfile` 是**档案名**（`moren` / `136`），不是切号名。曾经 `multiacct.plan()` 里写成 `names.get(...) or c['maaProfile']`，等于把档案名当账号名写进 StartUp，MAA 切号必然失败——整轮挂机死在「登录」这一步。

流程：

```
list_accounts → plan_multi_account → 缺名字就问 → 用户确认 → apply_plan
```

`plan_multi_account` 每个账号搜**一次**作业站（关键词会展开成整段关卡，`TL-` → 11 关），再按该账号自己练度挑能打的那份。

**串行**：一个模拟器同时只能登一个号，所以 A 跑完 → 切档案 → B。`multiacct.run()` 用 `watch` 判完成，一个账号结束就切下一个。

## 命令行（M5）

```
python -m backend.cli <命令>          # 源码
MAA-PRTS.exe --cli <命令>             # 打包后同一个入口

accounts / stages / ops / op / progress / status / watch / runs
plan / apply / ask / doctor
```

约定（照 MaaPilot 那套，agent 读起来最省事）：

- 输出永远是一个 JSON：`{ok, command, data, error}`
- 退出码 `0` 成功 / `1` 失败 / `2` 参数错 / **`3` 需要用户决定**
- `error.hint` 直接写下一步该干什么

`plan` 缺信息（比如账号名）时返回 `ok:true` + `missing` 且退出码 3——这不是失败，是「该问用户了」。

`plan` 的关键词写在位置参数上（`plan TL-5`）；`--stages TL-5` 这种写法也认，
但要注意 `--stages` 同时被当成「最多排几关」的数量上限（老写法），所以：

- 数字 → 数量上限，等价于 `--limit`
- 非数字 → 关卡关键词

位置参数解析（`_positionals`）会跳过 `--opt value` 里的 value，否则
`ops --account 小卡.json TL-5` 会把账号名当成关卡。踩过的坑：`plan --stages TL-5`
曾经直接 `int('TL-5')` 抛 `ValueError`，而那正是文档里写的示例。

CLI 直接 import 后端模块，**不依赖 HTTP 服务在跑**。

## 写 MAA 之前会发生什么

`apply_plan` / `add_stages` 最终都落到 `maa.apply_plan()`：

1. 取齐所有作业（还在途的等它做完）
2. 关 MAA（关不掉就报错，不硬写）
3. 备份 `gui.new.json`（只留最近 2 份）
4. 重读配置（MAA 退出时会回写一次）
5. 写作业文件 + `Copilot.TaskList`
6. 按需启动

**对话里 `replace` 会清空用户手动攒的作业列表**——所以助手的确认卡片必须把「给谁、挂哪几关」说清楚，用户看得到才会点。

## 失败处理

| 情况 | 表现 |
|---|---|
| 没配 Key / 模型名 | 聊天面板顶部显示「未配置模型」，点一下直接开设置 |
| 401 / 403 / 404 / 429 | 翻成人话（Key 不对 / 无权限 / URL 或模型名错 / 限流） |
| 模型不支持工具调用 | 自动退回非流式；再失败就只在对话里回答，不执行 |
| 死循环 | `MAX_TURNS = 12`，超了强制收口，告诉用户已经做了什么 |
| 工具内部异常 | 兜成 `{"error": ...}` 喂回模型，loop 不崩 |

## 还没做

- 养成（`OperProgress`）：要先实测 MAA 6.20 认不认这个任务类型
- 活动关精确到「哪一关没打」：森空岛只给 zone 级计数，且 `totalStage` 含剧情/突袭，口径对不上
- 并行多 MAA：需要多个 MAA 目录 + 多个模拟器，`multiacct.plan()` 会检测但默认串行

## skill 联调测试（2026-11-17）

助手页表头「skill 联调」按钮 / `POST /api/skill/try`：不走进聊天会话，把一句话
直接交给模型，看它选哪个工具、参数抽得对不对；勾「直接跑」立刻动模拟器
（跳过确认卡，因为测试就是要它立刻执行）。

```json
{"text": "我现在停在主页，帮我导航到 MN 界面", "skill": "skill_navigate", "run": false}
→ {"picked": "skill_navigate", "args": {"stage": "MN"}, "matched": true, "taskId": ""}
```

- `skill` 留空 = 全部工具任选（测「会不会自己选对」）；给了就只挂这一个工具
  （测「参数抽得准不准」），`matched=false` 表示模型没听指令选了别的
- `run=true` 时直接 `skills.run_skill`，返回 `taskId`，前端顺手把它挂到右侧
  任务进度栏跟踪
- 走 `agent.try_skill()`，PROBE_SYSTEM 强调「用户说别唤醒别导航就不要调
  wakeup/weekly」，否则「停在主页测导航」会被自作主张补唤醒

实测两例（deepseek-chat）：`停在主页→导航 MN` 选 `skill_navigate{stage:MN}`；
`已在 MN 关卡列表→抄作业打 MN-1~7` 选 `skill_fight{stages:[...],mode:copy}`，
都没多带 wakeup。

## 两种「回复不完整」

1. **流式尾巴丢字**（用户 2026-11-17 截图）：say 事件流式结束时会摘掉 `live`，
   若前端在这轮 poll 里 since 已越过它的 `i`（后面又 push 了 tool/done），就再也
   拿不到最终全文 → 表现为最后半句被吃掉。修在 `Session.view`：**最后一条 say
   始终返回**，不管 since（按 i 排序插回）。
2. **撞 max_tokens 被硬截断**：以前不给 `max_tokens`，用中转站默认值（常见
   512/1024），长回复半句断掉且无提示。现在 `_payload` 固定带 `max_tokens`
   （默认 4096，设置 `agent.maxTokens` 可覆盖），`_stream`/`_plain` 返回
   `finish_reason`，`_loop` 见 `length` 就补一条「撞上限了，说『继续』」。

## 打关预检：练度不够要当场说清楚（2026-11-18）

用户实测：助手把 BB-S-1~4 全排进去，账号练度不够，MAA 跑 8 分钟 0/1，
第一关就过不去，后面三关根本没解锁。用户的原话：「应该提醒我哪些关卡打不了，
而不是直接挂」。

`skill_fight` 现在**在摆确认卡之前**同步做一次预检（`agent._precheck_fight`
→ `stageplan.review`）：

1. 按游戏顺序排（基础 → EX → S / MO），把目标关前面的前置关补进计划；
2. 拿作业站作业的 `requirements` 比对这号干员（精英化 / 等级 / 专精 / 模组），
   分四档：可直接抄 / 借一人 / 练度不足 / 缺干员；
3. 练度不足或缺干员的关**不排队**，同活动后面的关连带标「前置未通」；
4. 一关都打不了时，工具直接返回 `error` + `precheck`，**不摆确认卡、不开打**；
5. 部分能打时，确认卡的 summary 里就带着这段预检，确认后只跑能打的那几关。

所以模型的回话里必须有这类信息：

> 预检：能打 2 关（BB-S-1、BB-S-2），打不了 2 关
> · BB-S-3 练度不足：星熊 需 精二 60 级 / 专三，你有 精一 70 级
> · BB-S-4 前置未通：BB-S-3 打不了
> 现在排的是 S-1、S-2；S-3 补完练度再用 `autoPrereq` 顺延。

不要只说「已在后台跑」。工具返回里的 `precheck` 字段就是给用户看的原文。

`skill_weekly` 不在确认前预检（整个活动几十关，一次查几十次作业站太慢），
它靠每个战斗批次开打前的预检：批次里有打不了的关就停，并把原因抛出来。
助手看到任务失败时，用 `run_status` 拿「卡在哪一关、什么原因」再回答用户。

## 复刻活动的突袭要先问一句（2026-11-22）

复刻活动的**突袭首通奖励只发一次**：去年打过的号，今年再打突袭只多花理智。
所以助手在用户说「刷 OS-EX / OS-S」这类**复刻活动**的关卡时，要先问两句：

1. 「要不要连突袭一起打？」
2. 「去年这个活动打过吗？」

去年打过 → 默认不加突袭；用户说不用 → 逐关写 `@normal`（或 `build_run` 传
`raid=normal`），**排进去的作业集里不要出现突袭行**。用户明确要打再按
`@raid` / `@both` 排。

「复刻」这个事实从三处拿得到（同一份数据，别自己猜）：

- `resources.activity_list()` 每条带 `reprint`（`RERUN_ACTS` 表，现在只有
  `act46side` = 雪山降临1101 / OS）
- `eventnav.ENTRIES[前缀]['reprint']`
- 助手工具 `list_activities` 返回的每条也有 `reprint`，`currentHint` 里写了
  该怎么问

注意两点：**「去年打过」只影响奖励，不影响能不能打**——别说「突袭打不了」；
**普通关和 S 关的突袭是两回事**（EX1~7、S1~5 各自有突袭），用户说「S 关不打
突袭」不代表 EX 也不打，逐关问清楚。
