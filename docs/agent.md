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

## 工具集（白名单，共 28 个）

**配置类**（改 MAA 档案 / 跑 GUI 那套任务队列）：

| 工具 | 做什么 | 风险 |
|---|---|---|
| `list_accounts` | 左侧账号 + 进度摘要 + MAA 档案绑定 + 当前选中那个 | 只读 |
| `list_activities` | 最近开启的活动（正式名/代号前缀/关数） | 只读 |
| `search_stages` | 关键词 → 真实关卡（代号/内部 id/难度/是否剧情关）。**模组 / 干员 / 玩法这类不是关卡名的词会返回 0 关**，这时别回「没有作业」，改用 `find_ops` | 只读 |
| `find_ops` | 这些关有哪些作业，按该账号练度分档。关键词不是关卡名也能搜（作业站全文搜，跨关卡列出）；「关键词 + 关卡代号」是强制锁定（`结城理模组 4-3`） | 只读 |
| `op_detail` | 一份作业缺什么：练度不够（橙）、要借谁（紫） | 只读 |
| `account_progress` | 主线推到哪、剿灭几张没满、活动剩几个区 | 只读 |
| `run_status` | MAA 现在跑到第几关、卡在哪个节点 | 只读 |
| `maa_capabilities` | 这台 MAA 能配哪些任务、有哪些预设 | 只读 |
| `list_profiles` | MAA 里有哪些档案、当前是哪个、几个作业 | 只读 |
| `plan_multi_account` | 给多个账号排方案（**只生成**，不写盘） | 只读 |
| **`build_run`** | **主力**：配任务 + 写作业 + 直连跑起来 | **写**，需确认 |
| `apply_plan` | 把 plan_multi_account 的方案跑起来 | **写**，需确认 |
| `add_stages` | 单账号挂一组关卡（`build_run` 的快捷方式） | **写**，需确认 |
| `set_account_name` | 存切号名（用户报的手机号尾号） | 写（不碰 MAA） |
| `bind_emulator` | 把账号钉到某台模拟器（`maa.adbAddress`） | 写（不碰 MAA） |
| `stop_run` | 停挂机；**`account` 给谁就只停谁**，不给才全停 | **写**，需确认 |

**skill 类**（逐个能力直连驱动，见 `skills.py`；`SKILLS` 里每个都有一项）：

| 工具 | 做什么 | 风险 |
|---|---|---|
| `skill_wakeup` | 唤醒到主界面（带 `accountName` 就切号） | 免确认 |
| `skill_navigate` | 导航到活动 / 分区（含 EX、S、MO）/ 主线章节；主线 9~14 章带 `difficulty` 顺便切档 | 免确认 |
| `skill_difficulty` | 切主线 9~14 章的作战环境（剧情 / 标准 / 磨难） | 免确认 |
| `skill_signin` | 森空岛签到 | 免确认 |
| `skill_claim` | 领活动奖励（进奖励页点「一键领取」） | 免确认 |
| `skill_fight` | 自动战斗一组关（编队/借人/吃药） | **写**，需确认 |
| `skill_skip_story` | 过 ST 剧情关 | **写**，需确认 |
| `skill_weekly` | 一个活动从头打到尾 | **写**，需确认 |
| `skill_train` | 干员培养（精英化/技能/专精） | **写**，需确认 |
| `skill_sweep` | 扫图：普通 → EX → 突袭 → S | **写**，需确认 |
| `skill_tool` | 牛杂小工具（战时新闻委员会 / 喀兰贸易技术研发部 / 活动商店 / 绿票商店 / 黄票商店） | **写**；黄票商店强制确认（258 黄票） |
| `run_steps` | 一个账号按顺序跑一串 skill（多账号自动分组） | **写**，需确认 |

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

`multiacct._match()` 还留了一条按昵称对档案的兜底（容忍 `示例昵称#6408` ↔ `示例昵称`），对不上的才进 `missing`。

问的时候**别让用户拼星号**，直接问「手机号后 4 位是多少」。用户给全号、带横线、星号数量不对、全角数字都行，`acctname.normalize()` 会统一成 `前3****后4`；给 3~4 位片段就原样保留交给 MAA 子串匹配。

> 2026-11-17：这之前校验写死成 `^\d{3}\*{4}\d{4}$`，用户照 MAA 文档只给尾号就被判非法，助手只能反复追问，任务最后出不来。规则现在只有一份：`backend/core/acctname.py`，别再在别处写第二份正则。

> 2026-11-22：`_roster` 原来完全禁读用户的 MAA 档案（怕档案和账号对不上号）。现在不是猜——哪份档案由用户在设置里点，程序照他点的读；他手改过的切号名（`accountNameFrom=manual`）优先，不让档案里的旧号码盖回去。

另外 `maaProfile` 是**档案名**（`moren` / `136`），不是切号名。曾经 `multiacct.plan()` 里写成 `names.get(...) or c['maaProfile']`，等于把档案名当账号名写进 StartUp，MAA 切号必然失败——整轮挂机死在「登录」这一步。

流程：

```
list_accounts → plan_multi_account → 缺名字就问 → 用户确认 → apply_plan
```

`plan_multi_account` 每个账号搜**一次**作业站（关键词会展开成整段关卡，`TL-` → 11 关），再按该账号自己练度挑能打的那份。

**串行**：一个模拟器同时只能登一个号，所以 A 跑完 → 切档案 → B。`multiacct.run()`
用 `watch` 判完成，一个账号结束就切下一个。

**每个账号可以有自己的 MAA**：`account['maa']['dir']` 指定这台账号归哪份 MAA
目录管，`account['maa']['profile']` 指定用哪份档案；留空就回落全局
`settings.maaDir` + MAA 当前的 `Current`。读这两个字段只有一个入口——
`acctbind.dir_of(file)` / `acctbind.profile_of(file)`，别在别处再写一份。

## 多 MAA / 多模拟器：`run_steps`（2026-11-22）

用户的原话：「如果两个账号属于不同的模拟器，则直接多开控制两个账号；如果 5 个
账号都是一个 MAA 和一个模拟器控制的，就切号 → 挂普通 → 挂突袭 → 挂 S → 领奖励
→ 搬商店 → 再切下一个号，循环到所有号跑完。Agent 调用技能要灵活一点。」

所以**没有**写死「活动一条龙」这个流程，只给一个能自由拼的工具：

```json
{"accounts": ["主号.json", "abc示例.json"],
 "steps": ["wakeup", "navigate", "fight SV-EX-1~7", "claim", "tool 活动商店"]}
```

- `steps` 每项一个 skill 名，空格后面跟参数（`fight SV-EX-1~7`、
  `navigate OS`、`tool 活动商店`、`wait 60`）。想加/减/换顺序就直接改这个列表。
- 不传 `accounts` = 左侧当前选中的那个账号（`settings.lastAccount`）。
- **分组在 `maaplan.plan_groups()`**：键是
  `(normcase(abspath(maa_dir)), adb address)`。键相同的账号**串行**（同一台
  模拟器不能两条 adb 同时戳，表现是「点了没反应」），键不同的**并行走**——
  这就是「多开」。
- 每个账号的所有 step 在同一个后台任务里跑，日志以 `[昵称]` 开头，
  右侧面板能分清是谁；完整日志另存 `debug/tasks/<任务号>.log`。
- 切号不是单独一步：`wakeup` 带 `accountName` 就在 StartUp 里换号。所以
  「同一模拟器 5 个号」的写法是给每个账号各排一份 steps，每份的第一步都是
  带自己切号名的 `wakeup`。

**分组对了不等于连得对**（2026-11-22 第二次修）：`run_core` 原来不认账号的
档案，永远用 MAA 的 `Current`——八份档案全写 `emulator-5554` 时，b 服那条
虽然在另一台 MAA 目录里也会被拉回 5554，用户看到的就是「一台在跑、另一台
不动」。现在每个 spec 都带 `profile`，`run_core` 还认 `spec['adbAddress']`；
`Touch(strict=True)` 连不上就报错，绝不自作主张换设备。

**要真的并行两台，光靠档案常常不够**（用户的档案可能全指向同一台）。用户说
「这个号用第二台 / 用 5556 / 别和那个号挤一台」时，**先调 `bind_emulator`**：

```json
{"account": "abc示例#0384 bilibili服.json", "address": "emulator-5556"}
```

它把地址写进账号设置（`maa.adbAddress`），以后每轮跑机都连这台。只传一次
`emulators` 是**临时**的——下一轮助手忘了，两个号又挤回同一台，这就是
2026-11-23 用户报的「又忘了实例控制」。钉住之后再跑就不用带 `emulators`：

```json
{"accounts": ["abc示例#7986 官服.json", "abc示例#0384 bilibili服.json"],
 "steps": [...]}
```

`list_accounts` 里 `emulator` 是这个号实际会连的那台，`emulatorPinned` 表示
用户钉过没有。规则（`skills.device_of`）：

- 钉过的地址**绝不自愈**：那台不在线就报错停下，不会换一台在线设备顶上；
- 没钉过、档案地址过期时，只有**一台**在线才顶上；两台在线会直接拦下，
  这时要问用户「哪个号用哪台」，问清了就 `bind_emulator` 钉住；
- `emulators` 仍然可以传，优先级最高，但只作用于这一次。

`run_steps` 是写操作，确认卡会列「几号 / 几台模拟器 / 每步做什么」，用户点头
才动模拟器。

**官服 / b 服不用你操心**：连接时的客户端类型由账号的 `channelName` 决定
（`Official` / `Bilibili`）。2026-11-23 之前这里有个 bug，把 MAA 档案里的
模拟器类型（`LDPlayer`）当成客户端类型传了，b 服账号按官服包启动卡在登录。
现在日志会写「连接 emulator-5556（b服）」；看到渠道写错说明账号文件的
`channelName` 不对，让用户在设置里改。

## 多账号时默认走导航（2026-11-22）

用户口径：**只要不是只跑一个号**，就别假设界面停在关卡列表——上一个号可能
停在关卡列表、主页、基建、甚至结算页，状态无从判断。所以 `run_batch` 在账号
数 > 1 时，`fight` 步骤会自动补 `wakeup + navigate`（任务日志会写「多账号：
fight 默认先唤醒 + 导航」）。

两种例外，由助手把用户原话翻成参数：

| 用户说 | steps 里写 |
|---|---|
| 「我已经在关卡列表了」 | `{"skill":"fight","assumeStageList":true}` |
| 「不要切号，直接开始」 | `{"skill":"fight","noSwitch":true}` |

`skill_fight` 单独调用时，**代码默认仍是直跑**（老行为——2026-11-17 用户不想
被强行唤醒）；但从 **2026-11-24** 起提示词改成**默认要唤醒 + 导航**，由模型按
用户原话传 `wakeup=true`。这两个键仍然**优先于** `wakeup`：用户亲口说
「在关卡列表 / 别切号」就不导航。

单账号要不要也由程序兜底补？先不动 `_nav_first` 的 `many` 判断——单账号被
强行唤醒正是用户吐槽过的，现在交给模型按他的话判断更准。

## 每个号的第一件事是「开始唤醒」切号（2026-11-25）

用户第三次报「助手又没加切号自动唤醒」，所以这条从提示词约束升级成**程序
兜底**：只要一次 `run_steps` 里出现两个及以上账号，`run_batch` 会给步骤表
**最前面插一步 `wakeup`**，每个号开跑前都走一遍（日志第一行写「多账号：每个号
开跑前先「开始唤醒」切号」）。

- 第一步自己会唤醒的不重复插（`skills._wakes_itself`）：`wakeup`、`sweep` /
  `weekly` / `train`（默认 `wakeup=true`）、`fight` 带 `wakeup=true`、
  `claim` 带 `wakeup=true`。这样不会白等一遍客户端重启。
- 用户明说「不用切号 / 都是同一个号 / 已经切好了」→ 助手传
  `switchAccount=false`，程序不插。
- 单账号仍然不兜底（2026-11-17 用户吐槽过单号被强行唤醒）。

提示词里同步写死了这条硬规矩（`## 硬规矩` 第一条），并明确「走 run_steps 不用
自己写 wakeup、一个号一个号调 skill 时每个号第一条必须 skill_wakeup 或传
wakeup=true」。因为「刚唤醒过一次就以为一直有效」是模型最容易犯的错——中间换
过号，之前那次唤醒就作废了。

**单账号反过来：先看界面，别急着重启客户端（2026-11-26）**。用户原话「每个
单账号任务的运行前检查当前明日方舟界面的位置，必须回到明日方舟主页，再开始
导航……减小重复自动唤醒的时间」：单号跑任务时界面很可能本来就在主页，跑一遍
StartUp 等于白重启一次客户端（40~80 秒）。现在 `skills.ensure_home` 先看一眼
——在家直接走，在别处按返回键走回来，只有游戏没开 / 走不回去才真唤醒。
多账号不受影响（切号必须真重启），实现上由 `_mark_switch` 给每一步盖
`switch=true` 把这条捷径关掉。

## 三条口径（2026-11-24，用户游戏理解，已写进提示词）

1. **点名账号 = 默认切号**：没明说「不用切号 / 已经开着这个号」，就先走
   「开始唤醒」（`wakeup=true`）。直跑模式下 `run_core` 会把 StartUp 摘掉，
   不切号就可能打到界面上正在跑的那个号上。**多账号联排时每个号都要重新唤醒
   一次**（上面那节，程序已兜底）。
2. **默认带导航**：没明说「直接跑 / 已经在关卡列表」，作业前必须有一道导航。
   没有导航直接排 Copilot，等于站在当前界面上瞎点，必卡死。
3. **联排按模拟器分配**：`list_accounts` 的 `emulator` 相同的号串起来、不同的
   并行；每个号的导航是它自己那组的第一步；一个号跑完先回主页（StartUp 本来
   就会回主页）再切下一个。


## 关键词不是关卡名也能搜（2026-11-26）

用户现场：「搜索中文不能检索到对应关卡……我让 agent 帮我打模组任务，他一直会说
没有模组任务的作业」。`resources.resolve('结城理模组')` 返回 `unknown` 是**对的**
——那不是关卡名；错的是到这里就收手。作业站支持全文搜（`document`），带这个词
的作业有十几份，横跨 4-3 / VEC-01 / H6-2…

`copilot.search` 现在有兜底（`copilot.keyword_search`）：

| 输入 | 走哪条 | 结果 |
|---|---|---|
| `结城理模组` | `document` 全文搜，按作业的 `stage_name` 分组 | `mode='keyword'`，N 关各一行，每行都带关卡代号 |
| `结城理模组 4-3` | `levelKeyword` + `document` **强制锁定**（就是网页上搜索框 + 关卡下拉） | `mode='stage'`，只出这一关，`locked` 字段带关键词 |
| `4-3` | 老路径（`resources.resolve`） | 不变 |

对齐到工具上：

- `search_stages` 仍只认关卡代号，认不出时 `note` 里会直接告诉模型「改用
  `find_ops`」——就是为了别再回一句「没有作业」；
- `find_ops` 自动享受兜底，跨关卡列出；
- **`build_run` / `plan_multi_account` 遇到 `keyword` 结果会拒绝排计划**，把命中的
  关卡列给用户，要他补一个关卡代号（「结城理模组 4-3」）——横跨十来关的作业照单
  全挂，等于替用户决定打一堆他没说的关。

## 主线也能自动导航了（2026-11-24）

`15-3`、`0-11` 这种主线关以前是「不导航，直接让 Copilot 自己试」——Copilot
只会在关卡选择页横向滑，从主页出发必然失败。现在 `stagenav.goto_stage_list`
把主线分给 `mainnav`：终端 → 曲谱 → 乐章收录 → 默认进度 → 按章号点 EPxx 卡 →
右下「前往章节」→ 该章关卡列表，然后交给 Copilot 的 `ClickStageName`。

对助手来说没有新工具、没有新参数，`skill_navigate` 传 `stage` 就行。两条要
注意：

1. **一章一次导航**：连续打同一章的多关时，第二关起界面已经停在关卡列表上，
   再导航会先退回主界面重走一遍（约 20~40 秒）。批量打同一章建议用
   `skill_fight` 的 `stages` 一次性交出去，让 Copilot 连着跑。
2. **突袭关（`H15-1`）也只导航到第 15 章的关卡列表**，切突袭是关卡列表里的
   事，不由导航负责。

## 主线 9~14 章的作战环境（2026-11-24）

主线挂机分两套，差别就在**中间有没有难度切换**：

- **0~8 章、15~17 章**：只有普通 / 突袭。导航到关卡列表 → 开作业集就是全部。
- **9~14 章**：多一层「作战环境」——剧情体验 / 标准实战 / 磨难险地。
  第 9 章只有前两档（标准那档有突袭关），第 10~14 章才三档。

三档在游戏里是**同一个关卡列表的不同档**（磨难那档是另一批关卡 id），界面停在
别的档上，打下去就是另一档的关。所以第 9~14 章必须显式指定难度：

```json
{"stages": ["10-1", "10-2"], "difficulty": "hard", "wakeup": true}
```

程序在导航到那一章的关卡列表之后自动切档，日志写「作战环境：标准实战环境 →
磨难险地环境」；切不过去直接报错。用户没说难度时按**标准实战环境**传
`difficulty:"normal"`，并在回复里说明「你没说难度，我按标准实战环境打」。

只想换档、不重排作业（「先切到磨难我看看」）就调 `skill_difficulty`。

**「导航过去顺便切档」一次调用做完**：`skill_navigate(stage="13-1", difficulty="hard")`
——导航到位后自己切。别拆成 skill_navigate + skill_difficulty 两个调用：2026-11-25
用户实测，拆开发出去的两个任务同时动同一台模拟器，切难度那一路在 MAA 还在登录时
就查游戏进程，查到空直接抛「游戏没在跑」，导航一次都没做。**同一个账号的 skill 是
串行的**（后发的会排队等前面的跑完，日志写「这个账号还有一路在跑…等它跑完」），
所以能一次调用解决就别拆——拆了只会排队，还多一层出错机会。

**游戏没开不用先喊唤醒**：`skill_navigate` / `skill_difficulty` 一进场发现游戏
进程不在，会自己走一遍唤醒（有切号名 = MAA 开始唤醒，没有 = 重启客户端）再干活
（2026-11-25 用户原话「adb 正常后就应该启动 maa 的自动唤醒了，而不是干等」）。
所以别为了「先把游戏拉起来」多排一个 skill_wakeup —— 那会跟导航排队互等；用户
明确要求切号（换账号登录）时才需要单独调 `skill_wakeup`。

## 用户当前点开的那个账号（2026-11-22）

左侧列表选中的账号存在 `settings.lastAccount`。`list_accounts` 返回里有顶层
`current`，每行也有 `current: true`，提示词写死了两条：

1. 用户没说哪个号 → 以 `current` 为准，**先问「就这个号吧？」**，别默认全账号跑；
2. `skill_*` / `build_run` 的 `account` 留空时，后端 `skills._account_file('')`
   本来也落到 `lastAccount`，所以问完直接调就行。

另外 `list_accounts` 每行带 `maaDir` 和 `emulator`（这台 MAA 档案里配的
adb 路径 + 实例号）：`emulator` 相同 = 只能排队，不同 = 可以多开。

## 逐关成败不要报数（2026-11-22）

用户报「每次挂完都显示一关都没挂」。查下来是 `_mark_clear()` 只认
`StageDrops` 回调，而 Copilot 任务**根本不发**这个回调（本机 asst.log 里 255 条
`SubTaskExtraInfo` 全是 `CopilotAction` / `BattleFormation` / `ExceededLimit`，
0 条 `StageDrops`），所以成功数恒为 0。

按用户口径「判断不出来就别统计」，逐关成功数**整个删掉**了。现在只报三种
拿得准的事实：

| 信号 | 说成 |
|---|---|
| `CopilotListLoadTaskFileSuccess` | 「跑过的关：SV-EX-1、SV-EX-2…」 |
| `TaskChainError` | 「✘ SV-EX-3 翻车：原因」 |
| `AllTasksCompleted` / `TaskChainStopped` | 「本轮任务链正常结束」/ 停在哪个节点 |

提示词硬规矩里有一条：**绝不能说「一关都没挂」「成功 0 关」**，也不能编
「打了 5 关成功 4 关」。用户要的是「跑了哪些关、卡在哪、为什么」。

## S 关走单作业模式（2026-11-23）

用户 2026-11-23 报「MAA 无法识别 S 关卡」。原因是 OS 的 S 关**不在关卡列表里**：
活动地图上点「喀兰之心」进去是一张雪山图，五个 `OS-S-x` 按钮浮在上面，而
MaaCore 的 `Copilot@ClickStageName` 只在关卡列表里整屏 OCR。

处理方式（`skills.skill_fight` 自动分派 → `maaplan.run_scatter`）：

- 判据：`mode=copy` 且所有关卡都是 `<前缀>-S-<数字>`，且 `scatter.LAYOUTS` 里
  有这个前缀。**`OS-EX-1` 不会误入**（`cell_index` 只认 `-S-` 形状）。
- 流程：`eventnav.run` 到 S 关界面 → 定点模板认五个格子（认不到的当未解锁，
  跳过）→ 点格子 → 点「开始行动」/「突袭模式→开始突袭」→ 等编队页 →
  一关排一个 Copilot 任务（**只给 `filename`，不给 `copilot_list`**，MaaCore
  就不做导航）→ 打完点屏幕回 S 关界面 → 下一关。
- **单作业模式不判三星**：asst.log 里 `BattleProcessTask` 完成就直接
  `TaskChainCompleted`，没有 `StageDrops` / `EndOfAction`。所以日志只说
  「打完了（单作业模式不判三星）」，助手也**不许**报「几关三星通过」。
- **三种收尾要分清**（2026-11-23）：编队缺干员是 `SubTaskError(OperatorMissing)`
  + `TaskChainStopped`，日志会写「缺干员：圣聆初雪」（这时该换作业或让用户练，
  别说是超时）；用户点「停止挂机」是 `eng.stopped`，日志写「已停止（你按了
  停止）」，**不是超时也不是失败**，别报「跑满 120 分钟」。
- 用户排 `stages=['OS-S-1@normal', …]` 照常可用：普通/突袭各占一条作业，
  突袭那条会自动点「突袭模式」再点「开始突袭」。**S 关普通和突袭两档都有**，
  别按「复刻活动所以没突袭」去推断。

### 停止要按账号说清楚（2026-11-23）

`stop_run(account=...)` 给谁就只停谁：本进程里的 MaaCore 实例和多开时的独立
子进程都按账号筛，别的号照跑。**用户点名了哪个号就必须带 `account`**——
那次他说「停 b 服的 maa」，助手调了全停，官服那台正在打的关一起断了，他还
反问「怎么都停了」。

- 名字 → 账号文件走 `skills.match_accounts`：完全相等优先，子串对上多个直接
  报错（宁可问一句「你说的是哪个」，也别一次停三个号）
- 停完照实说：返回里 `accounts / engines / processes` 是实际停掉的；没停到
  就说「这个号现在没有在跑的任务」，别报「已停下」
- 只有用户说「全停 / 全部停下」才不带 `account`

### 难度只照用户原话（2026-11-23 事故，两次）

用户说「帮我挂 OS-S 关 1 到 5 普通」，那一轮助手两处错：

1. 对话里编了一句「OS 是复刻活动，S 关只有普通难度，没有突袭」——**S 关有突袭**。
2. 排单时没传 `raid`，程序当时默认 `auto` = 只打突袭，于是 5 关全按突袭排。

同一天下午反过来又炸一次：用户说「OS-EX-8 打突袭」，两个号「导入的都是普通
EX-8」。根因是**提示词和代码的默认值对不上**——工具描述里还写着「EX 关默认只打
突袭」，代码在上午那次事故后已经把 `auto` 改成了普通，助手照描述办、不传
`raid`，排下去自然是普通，嘴上还在说突袭。现在描述、硬规则、代码统一成一条：

- `raid` **省略 = 只打普通**（不是突袭，也不是自动）。用户说「突袭」时必须显式
  传 `raid='raid'` 或把那一关写成 `@raid`；说「普通」→ `'normal'`；说「都打」
  → `'both'`。`build_run` / `add_stages` 也收 `raid` 了（以前它俩压根没这个
  参数，永远取每关第一行，也就是普通）。
- `skill_fight` 返回 `difficulties`（逐关实际排的档位）和 `raidMissing`
  （要突袭但作业站只有普通的那几关）。**回复里说难度只能引用它们**：
  `raidMissing` 非空就照实说「这几关只有普通作业」，别顺着自己刚才的话继续吹。
- **不许**断言「某关有没有突袭」「复刻活动所以没有突袭」。难度以实际排单为准，
  `skill_fight` 会打一行「难度：普通 OS-S-1、OS-S-2 …」，照着念。
- 开跑前把「哪几关 / 什么难度 / 哪个号」跟用户原话对一遍，对不上别开跑。

### OS 当期该打什么（2026-11-23 用户口径）

雪山降临 1101 现在值得打的就四件，别自己加戏：

| 段 | 打到哪 | 难度 |
|---|---|---|
| 普通 | OS-1 ~ OS-10 | 普通 |
| EX | OS-EX-1 ~ OS-EX-8 | 普通 |
| 单关 | **OS-EX-8** | 突袭（只这一关补突袭） |
| S | OS-S-1 ~ OS-S-5 | **普通** |

**S 关突袭没有奖励**——用户原话「太变态了，刷那个没有奖励」。所以：
- 引导卡片里不出现 S 突袭；
- `_sweep_segments` 的 S 段**永远普通**，`raid='raid'` 只作用于 EX 段
  （要 S 突袭得显式 `sRaid=true`，正常对话别传）；
- 助手别主动建议「S 关要不要打下突袭」，也别把它写进示例。
- 收尾照旧：领活动奖励 + 搬商店（黑名单默认那四个）。

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
`ops --account 主号.json TL-5` 会把账号名当成关卡。踩过的坑：`plan --stages TL-5`
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

- 活动关精确到「哪一关没打」：森空岛只给 zone 级计数，且 `totalStage` 含剧情/突袭，口径对不上
- 逐关战斗成败：MaaCore 在 Copilot 任务里不发 `StageDrops`，「成功几关」拿不到（2026-11-22 已按用户要求去掉该统计，只报跑过的关 + 失败原因）
- `run_steps` 的步骤目前只能是 `SKILLS` 里已有的名字，还没有「同一个号内并发」——同一台模拟器本来也只能串行，暂时够用

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
