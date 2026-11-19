# tools/smoketest —— 冒烟测试

## 用法

```bat
.venv\Scripts\python.exe tools\smoketest\smoke_test.py
```

跑完打印每项 PASS/FAIL，全部通过退出码 0。不碰真实用户数据：
B 组把 `core.settings` 的数据路径指到临时目录，C 组整个项目拷到临时目录再启动。

## 检查项

| 组 | 内容 |
|---|---|
| A | 全仓库 `.bat`（不止根目录三个）是 UTF-8 无 BOM + CRLF（错编码会让 `call :标签` 错位）；全部 `.py` 可编译 |
| B | DPAPI 加解密往返、密文篡改必报错；token 密文随账号文件 `skd` 段（跨文件去重、备注保留、不进 API 视图、坏条目逐个跳过点名警告）；旧 `tokens.txt` 三种写法自动迁移；旧布局 `config/skland` 压平迁移（同名跳过、搬空才删、幂等）；MAA 干员识别 JSON（新 JObject / 旧裸数组）解析成练度结构；悖论清单与解锁推算（B10）；从 MAA 直接建号/无 token（B15）；账号手动排序 accountOrder（B16）；作业筛选（v1 默认练度、MAA 导入只比等级、剧情关不进搜索）；主线难度变体（简单/标准/磨难/险地、`#s` 独立成行、下一关推进）；关卡流程顺序（10-14 章系列视图带简单行、TR 与主线 S 关按 `unlockCondition` 插到前置关后面、`TR-S-1` 链式锚点、搜章节号返回整章含 S/TR、`TR_AFTER`/`SUB_AFTER` 改完清缓存即生效）；作业黑名单（`maa://` 代码归一化 + 出口过滤，B23）；推荐排序分档（可直接抄 9 分以上才压过借人作业）与作业详情缺练度/缺人（B28）；抽卡统计（星级归一 0 基/1 基兼容、保底按池计数、同名池合并、缓存视图、终末地快照、跨游戏 uid 隔离，B24）；森空岛签到（方舟 uid+gameId 与终末地 `sk-game-role` 两套口径、今天已签就不发 POST、403 记成已签到、缺 token/roleId 跳过、坏 token 只登录一次、同角色去重，B25）；资源包幂等（`version` 取游戏版本、指纹只看内容）与换包校验（sha256 / 条数不许变少）；程序自更新（`2.10 > 2.9`、预发布小于同号正式、脏 tag 不触发；安装包 sha256/体积/缺 exe 全拒、zip-slip 拦截；安装脚本只碰 `MAA-PRTS.exe` 与 `_internal`，绝不出现 `config/`、`debug/`、`resources/`）；日志观测（B30：增量读只算新行、文件被轮转能重读、`act55side_s03` 这类事件能还原成「跑到第几关/节点/重试几次」，200 秒没动静判停滞并给 hint、链跑完判 done、认不出的行一律跳过不抛）；多账号编排（B31：`1815505` → `181****5505`、档案名撞了加序号、一账号一档案且 `AccountName` 写进 StartUpTask、模板档案不被改动、`set_tasklist` 索引重排）；助手与 CLI（B32：工具表与实现一一对应、未知工具/坏参数兜成 error、autoApply 关时写操作只返 need_confirm、`apiKey` 绝不回显、CLI 输出形状与退出码 0/1/2/3，缺参数回 2 且带 hint）；活动关导航（B34：代号→区域行号 `TL-S-1`→第 3 区、`TL-EX-1`→第 2 区、`TL-5`→第 1 区，主线关 `1-7`/`S4-1`/`R8-2` 与作业内部 id 一律返回不导航；`probe()` 造图钉阈值——底部全暗判活动地图、半白判主界面、大半白+中部亮判主题页、中部成片纯白判活动商店；主线关调 `goto_stage_list` 必须零点击，绝不能对主线关乱点活动入口）；设置页接口（B38：token 三态读写、掩码只留尾 4 位、切号名归一与空串放行、bootstrap 带 logs）；培养干员（B39：专精绝对等级的多种写法互通、已达标项剔除、材料不足暂停并把剩余计划落盘续跑、MAA 日志桥去噪、战斗过程有进度日志、`SubTaskStart` 按真实 asst.log 的 first[0] 对账到中文阶段） |
| C | 临时副本里 `--no-window` 拉起服务，探活 `/api/ping`、`/api/bootstrap`、`/api/gamedata`；bootstrap 报告的账号数据路径在 `config/` 本层且 `config/skland` 不再被创建；日志落在 `debug/app.log`；结束后副本根目录除 `config/`、`debug/` 外零新增 |

### B36 打关预检（2026-11-18）

不联网：用假的作业查询结果验证 `core.stageplan` 的判定链——

- 代号分区：`BB-S-3`→s、`MN-EX-1`/`OF-EX1`→ex、`BB-MO-1`→mo、`BB-1`→base
- 前置链：`chain('BB-S-3')` = S-1/S-2/S-3；`expand(['BB-S-3'])` 里前两关标
  `prereq=True`；跨分区按游戏顺序（EX 在 S 前）；主线 `1-7` 不展开
- 连锁：S-2 判「练度不足」时，S-1 仍可打，S-3/S-4 变「前置未通」，且
  `playable` 只留 S-1
- 一关都打不了时 `skill_fight` 必须抛「一关都打不了」，绝不把空队列交给 MAA
- 卡在哪关：`maaplan._stage_of_file` 能把 `BB-S-3_10531.json` 还原成代号，
  `run_core` 返回值里有 `failed_stage`
- 助手侧：`_skill_via_guard` 在一关都打不了时返回 `error` + `precheck`，
  不摆确认卡

### B37 环境检测（2026-11-18）

同样不联网：把 `envcheck.read_config / adb_now / instance_up / launch` 全部
mock 掉，验证闸门口径——

- 读不到 MAA 配置（还没引导）必须**放行**，不能因为检测本身有问题把所有任务堵死
- adb 已通时 `ensure()` **一行日志都不写**（用户要求「不显示出来」）
- 关了 `emulatorAutoStart` 只提示不点启动器；开了就替用户启动，日志里要点明
  「帮你启动」并在 adb 就绪后放行
- 目录不存在 / 模拟器类型不认识 / 进程在但等不到 adb：三种原因分开说
- 闸门覆盖表 `_NEEDS_GAME` = 除 `signin` 外的全部 skill；`SKILLS` 仍是
  `(desc, fn)` 二元组
- 真挡一次：环境没过时任务直接 `error`，skill 本体一次都不该被调

### B38 设置页接口（2026-11-18）

设置弹窗改成左 tab 右内容后，账号页与 MAA 页都能直接改账号文件，这组按
「别把别的号凭证写进来」的口径断言：

- `token_state` 分得清「没存过 / 存了但解不开 / 正常」，密文坏了要提示
  「本机解不开」而不是「没有 token」
- `set_token` 落盘是密文、能读回、`mask` 只留尾 4 位；账号本体（uid /
  昵称）原样
- `api_account_maa` 的切号名：`181 5505` → `181****5505`；**空串放行**
  （清空是合法操作）；官服填昵称要报 `切号名`；B 服用户名原样不动
- `bootstrap.paths` 带 `logs` / `appLog`（「数据与日志」页要用）

### B39 培养干员（2026-11-18）

「培养干员」是唯一会改玩家账号资源（理智/材料）的新 skill，这组把四个口径
钉死：

- 专精目标是**绝对等级**：`[0,3,0]` / `{"2":3}` / `"2:3"` / `"技能2专精3"` /
  `"二技能专三"` 全部解析成二技能专三；`[0,9,0]`、`{"9":1}` 这种越界要报错
- 已达标项从计划里剔掉（精二 / 技能已到 / 已专到），一条不剩就不跑 MAA；
  账号快照里没有的干员名字当场报错
- `core_tasks`：`plans` 原样进 `OperProgress`；`noStartUp` 时不补 `StartUp`，
  给了切号名才排 `StartUp + OperProgress`
- 材料不足 → `pause` 置位 + `AsstStop`（不是失败）；`_remain_plans` 相减后
  落盘 `config/train-plan.json`，`_load_train_plan` 能读回来，做完自动删
- `maalog.Tap`：认不出关卡名翻成中文并带关卡号；`roi is empty` /
  `Unknown task` / TRC 行一律不吐
- 战斗过程日志：编队 / 部署 / 开技能各一条，同一干员重复部署不刷屏；
  `Output` / `SkillUsage` 这类内部动作不写进面板
- **`SubTaskStart` 全链路对账**：拿真实 `asst.log` 里出现过的 `first[0]`
  （`Copilot@WaitUntilEndOfAction` / `Copilot@StageNavigationSlowlySwipeLeft` /
  `BattleStartAll` / `BattleQuickFormation` / `UseMedicine` /
  `OperProgress@EliteUp`）走 `maacore._translate` → `_on_core_event`，必须翻出
  中文；裸名与带 `<Chain>@` 前缀的都要能命中；`BattleQuickFormationRole-*`
  统一归「编队·按职业筛选」；插件类名节点静默。这条专门防「照源码猜
  pipeline 名、跟真实日志对不上、战斗一整段没日志」的回归
- **切号名唯一来源 = 账号绑定**（2026-11-18 晚定版）：`maa.accountName` 读得
  到就返回；没绑定给空串，且把 `maa._load_cfg` mock 成「一调用就炸」——
  **全程不许读用户的 MAA 配置档案**（agent 模式跑临时配置=StartUp 任务参数，
  不碰用户的 gui.new.json）。**绝不用昵称顶**（官服切号认手机号）。防「读错
  字段切号名永远空、唤醒 2 秒完成、号没切」和「从档案猜尾号猜错号」两类回归
- **StartUp 直连参数口径**：MaaCore 的 `StartUpTask::set_params` 只认
  `account_name` / `client_type` / `start_game_enabled` 三个键（给了非空
  `account_name` 就切号），`core_tasks` 不得发 `account_switch_enabled` 这类
  MAA 不认的假参数；`switchAccount=false` 时整个不发 `account_name`
- **`Engine.wait` 启动宽限**：mock `AsstRunning` 前 4 次 False——「还没开始
  跑」不能当「跑完」（AsstStart 异步生效）；跑过之后变 False 才算停。防
  「培养任务排进队列同一毫秒就被 close 杀掉」的回归
- **`Engine.start` 收尾重试**：mock `AsstStart` 在 running 时返回 false、
  `AsstRunning` 递减到 False——start 要等它退干净后重试成功（调用两次）；
  一直 running（真在跑别的任务）时必须返回 False，不许假装成功。防
  「唤醒刚完成就排培养 → AsstStart 返回失败、培养一步没跑」的回归
- **账号列表带切号名**：`_tool_list_accounts` 的每行必须有 `accountName`，
  `hint` 里要写明「非空直接沿用、别再问」。防「设置里配过手机尾号、助手
  每次还追问」的回归
- **突袭行优先**：mock copilot.search 返回 normal/raid 两行同 band 的通用位
  作业，`_lookup` 必须选 `variant=raid`；raid 行 blocked、normal 行 ready 时
  仍选 normal（可打性优先于变体）。防「挂 IW-EX 打的全是普通难度」的回归

### B40 全局 json 不炸账号列表（2026-11-19）

用户报「突然无法自动战斗」——唤醒 / 战斗 / 培养一起报
`AttributeError: 'str' object has no attribute 'get'`。`config/train-plan.json`
（`account` 是文件名字符串）被当成账号文件，把 `list_accounts` 整个炸掉。

- 目录里摆上 `train-plan.json`、`{"account": "oops"}`、`{"account": ["oops"]}`、
  `stats`/`maa`/`skd` 都是字符串的文件、顶层数组 json
- `list_accounts` 只能列出真账号且**一个都不许抛**；字符串段当空处理
  （`stats.charCount` 为 None、`maaAccountName` 为空）
- `train-plan.json` 必须在 `updater.NON_ACCOUNT_JSON` 里（不是账号）
- `skills._account_file('')`（每个 skill 开跑第一句）要能正常给出默认账号
- `updater.scan_accounts` 同样不许被这些文件带崩

### B41 每关只打要打的档（2026-11-19 03:13 定稿）

用户实测：「说好只排突袭，结果又从头挂了一遍普通 EX-1」。asst.log 01:42 证明
MaaCore 拿到 `is_raid=true` 会自己点 `ChangeToRaidDifficulty` 切难度，不需要
前面垫一条普通；上一版排「普通 → 突袭」两条就是白烧一次理智。

- 默认 `raid='auto'`：有突袭作业的关**只排突袭**一条
- `raid='both'`：`普通 → 突袭` 两条，顺序不能反
- `raid=False` / `'normal'`：只排普通
- 逐关覆盖：`wants={'SV-EX-2': 'both'}` → EX-1 只突袭、EX-2 两条
- `_parse_stages`：`@raid` / `@both` / `@normal` 后缀与 `{'raid': True}` 都认
- `prepare_copilot` 不再自动补普通（断言日志里没有「补一条普通」）
- 主线险地（`#s`）仍是单条 `is_raid=true`
- B9 的 `entry_variants` 断言改成「选哪一行写哪一行」：`(2,'normal')=[False]`、
  `(1,'raid')=[True]`，防止再拿作业位标志改主意

### B43 逐关战报 + 失败换作业重挂 + 分段扫图（2026-11-19）

用户四条需求（翻车看得见哪关 / 挂完一关报成功 / 失败换作业重挂且不重打已成功
的关 / 普通→突袭→S 分段联动）。

- `_mark_clear`：`StageDrops` → `results` 记成功 + 日志「✔ X 战斗成功（掉落…）」；
  同一关再结算一次**不重复喊**
- `_mark_fail`：Copilot 链失败 → 「✘ X 翻车：原因」；非 Copilot 链（如 StartUp）
  不记到关卡上（没有「哪一关」）
- `_tally`：同一关先失败后成功算成功
- `_retry_failed`：已成功的关不进重挂队列；只 append **一次** Copilot 任务，
  且作业换成了候选里的下一份（`_202.json`）；日志有「换作业重挂：SV-EX-2」、
  绝不出现已成功的 `SV-EX-1`；返回值 `mark` 非 None
- `_sweep_segments`：`['SV-1','SV-2','SV-EX-1','SV-S-1']` → 段名
  `普通关 / EX 关·普通 / EX 关·突袭 / S 关`，variant `normal/normal/raid/normal`；
  `raid='normal'` 塌成三段；`tiers=['ex']` 只出 EX 两段；剧情关（ST）不进计划

### B44 当期活动导航（2026-11-19）

用户给了当期活动（TL「时间啊，向前！」，有效期到 11-25）的逐屏步骤，要
「说最新活动就用这套导航，老的别删，会复刻」。

- 有效期：TL 到 `2026-11-25` 不过期、`2026-11-26` 过期；`active()` 过了那一天
  就不返回它
- 指代：`最新活动 / 当期活动 / 时间啊，向前！ / 时间啊向前` 都落到 `TL-1`；
  `skills.norm_stage` 同样认
- 模板：`_entry_track.png` / `TL_z1~z3.png` 必须在包里；分区必须是
  `base / ex / s`，每个都有兜底 `tap` 和 `roi`，EX 与 S 的 ROI 不能重叠
  （否则同一个按钮两段互相抢）
- 路由：`goto_stage_list('TL-EX-1')` 先走 `eventnav.run`，不再去点老的活动大图
- 重挂前点结算页：`_leave_result` 第一下必须点在 `_RESULT_TAP`（MAA 的
  ClickCorner 中心），`probe == 'map'` 就停在关卡列表不再导航；不在关卡列表
  时退回 `_navigate` 重新到位
- 名单核对：`_tidy_stage_list(['SV-EX-1','SV-EX-2','SV-EX-7'], 用户点名
  ['SV-EX-1','SV-EX-2'])` → 剔掉 EX-7 并写日志；用户点名却没排进去的关也点名

离线核对模板（不连模拟器也能验）：把用户截图喂给 `find_card_bytes`，四张模板
都拿 0.0 分、命中位置就是红框中心。

### B45 资源包缺分区（2026-11-19）

用户实测：「还是不会选突袭，说的和实际给我选择的关不一样」。资源包是
2026.10.29 的，当期活动只有 `TL-1~TL-10`，没有 `TL-S-4`，于是搜索拿
「全文搜第一条的 stage_name」顶上，把 S-4 认成了 `act55side_s02`。

- 推断：`infer_stage('TL-S-4') → act55side_s04`、`TL-EX-3 → act55side_ex03`、
  `TL-MO-1 → act55side_mo01`；`TL-1` 和乱码返回 `None`（不瞎推）；
  `resolve('TL-S-4')` 必须 `mode='stage'` 且 stageId 正确
- 预检：`row_of('TL-S-4')` 认得出；`prereq_codes('TL-S-4')` 是
  `S-1~S-4`；`expand(with_prereq=False)` 只出自己
- 名单核对：`['TL-S-1'...'TL-S-5']` 对点名 `['TL-S-3','TL-S-4']` → 剔掉 S-5，
  S-1~S-4 留着（同分区前面的关算前置）
- 突袭不降级：突袭作业 `mode='blocked'` 时首轮仍排 `variant='raid'`
  （日志告警但不改普通）；`prereq` 里的关只排普通；`@both` 出 normal + raid
  两条；这关压根没有突袭作业时才退回普通
- 预检全判「缺干员」时不拒绝（缺人能借，照排）；判「练度不足」时仍然报
  「一关都打不了」

## 什么时候跑

- 改了 `vault.py` / `accounts.py` / `settings.py` / `main.py` 之后
- 改了打包、启动脚本或 `.gitignore` 之后（A 组和 C 组的目录零污染检查）
- 发版前过一遍

依赖只用到程序自身的运行环境（B/C 不会联网调森空岛接口），用 `.venv` 的
Python 跑即可；C 组会真实调用 Windows DPAPI，换电脑/换用户跑 B 组是
「加密绑定本机」的活体验证。
