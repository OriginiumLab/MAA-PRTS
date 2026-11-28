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
| B | DPAPI 加解密往返、密文篡改必报错；token 密文随账号文件 `skd` 段（跨文件去重、备注保留、不进 API 视图、坏条目逐个跳过点名警告）；旧 `tokens.txt` 三种写法自动迁移；旧布局 `config/skland` 压平迁移（同名跳过、搬空才删、幂等）；MAA 干员识别 JSON（新 JObject / 旧裸数组）解析成练度结构；悖论清单与解锁推算（B10）；从 MAA 直接建号/无 token（B15）；账号手动排序 accountOrder（B16）；作业筛选（v1 默认练度、MAA 导入只比等级、剧情关不进搜索）；主线难度变体（简单/标准/磨难/险地、`#s` 独立成行、下一关推进）；关卡流程顺序（10-14 章系列视图带简单行、TR 与主线 S 关按 `unlockCondition` 插到前置关后面、`TR-S-1` 链式锚点、搜章节号返回整章含 S/TR、`TR_AFTER`/`SUB_AFTER` 改完清缓存即生效）；作业黑名单（`maa://` 代码归一化 + 出口过滤，B23）；推荐排序分档（可直接抄 9 分以上才压过借人作业）与作业详情缺练度/缺人（B28）；抽卡统计（星级归一 0 基/1 基兼容、保底按池计数、同名池合并、缓存视图、终末地快照、跨游戏 uid 隔离，B24）；森空岛签到（方舟 uid+gameId 与终末地 `sk-game-role` 两套口径、今天已签就不发 POST、403 记成已签到、缺 token/roleId 跳过、坏 token 只登录一次、同角色去重，B25）；资源包幂等（`version` 取游戏版本、指纹只看内容）与换包校验（sha256 / 条数不许变少）；程序自更新（`2.10 > 2.9`、预发布小于同号正式、脏 tag 不触发；安装包 sha256/体积/缺 exe 全拒、zip-slip 拦截；安装脚本只碰 `MAA-PRTS.exe` 与 `_internal`，绝不出现 `config/`、`debug/`、`resources/`）；日志观测（B30：增量读只算新行、文件被轮转能重读、`act55side_s03` 这类事件能还原成「跑到第几关/节点/重试几次」，200 秒没动静判停滞并给 hint、链跑完判 done、认不出的行一律跳过不抛）；多账号编排（B31：`1815505` → `181****5505`、档案名撞了加序号、一账号一档案且 `AccountName` 写进 StartUpTask、模板档案不被改动、`set_tasklist` 索引重排）；助手与 CLI（B32：工具表与实现一一对应、未知工具/坏参数兜成 error、autoApply 关时写操作只返 need_confirm、`apiKey` 绝不回显、CLI 输出形状与退出码 0/1/2/3，缺参数回 2 且带 hint）；活动关导航（B34：代号→区域行号 `TL-S-1`→第 3 区、`TL-EX-1`→第 2 区、`TL-5`→第 1 区，主线关 `1-7`/`S4-1`/`R8-2` 与作业内部 id 一律返回不导航；`probe()` 造图钉阈值——底部全暗判活动地图、半白判主界面、大半白+中部亮判主题页、中部成片纯白判活动商店；主线关调 `goto_stage_list` 必须零点击，绝不能对主线关乱点活动入口）；设置页接口（B38：token 三态读写、掩码只留尾 4 位、切号名归一与空串放行、bootstrap 带 logs）；培养干员（B39：专精绝对等级的多种写法互通、已达标项剔除、材料不足暂停并把剩余计划落盘续跑、MAA 日志桥去噪、战斗过程有进度日志、`SubTaskStart` 按真实 asst.log 的 first[0] 对账到中文阶段）；多 MAA 多模拟器编排（B52：账号自己那份 MAA 目录/档案优先、默认账号=左侧选中、按「MAA 目录 + adb 地址」分组、同组串行跨组并行、`run_steps` 步骤解析、`claim` 免确认且不在写处理器里、`skill_claim`/`run_steps` 都进白名单）；多开三处硬伤（B53：分区直点不走老路径、profile/adbAddress 真的传进 MaaCore、点名设备不许自动换台、任务日志落盘、组线程异常留痕）；导航从任意页起步（B54：活动提示条定点比对、不在主界面不许点坐标、已在地图不绕路、probe_bytes 与 probe 同源、粗扫留前 3 名后整屏匹配不退化）；右栏日志按账号分栏（B55：`_public` 白名单不带文件句柄、任务 `owners` 精确过滤、`tasks._decorate` 给多开子进程每行补 `[昵称]`、`group_tag` 单号组取昵称、worker 命令与 main 入口分支都在）；页面识别与日志样式（B56：主页=齿轮+邮件双图标、基建确认框双圆钮定位（一整块红色必须判否）、翻车只认 MaaCore 回调 + 重挂前「点一下再判活动关卡界面」、to_home 不在主页盲按、多账号 fight 默认走导航、`@skill` 注册表派生、MAA 风格「选择干员/当前步骤/每关完成」+ 子任务回调码钉在 20000 段/老 70000 段也认/子任务失败翻人话/练度修正不再进 notes、翻车重挂只换翻车那关且没候选时同一份再试、跨分区分段导航、散点关卡（S 关）认格子→点进编队→单作业模式一关一趟） |
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
- **切号名来源**（2026-11-22 改）：没绑配置文件时 `maa.accountName` 读到就返回、
  没有给空串，并把 `maa._load_cfg` mock 成「一调用就炸」——**这种情况全程不许
  读用户的 MAA 配置档案**；绑了配置文件（用户点名的那份）才读它 StartUp 的
  `AccountName`，此时把 `maa._save_cfg` mock 成「一调用就炸」——**只读不写**。
  `accountNameFrom=manual` 的值优先，档案里的旧号码不许盖回去。**绝不用昵称
  顶**（官服切号认手机号）。防「读错字段切号名永远空、唤醒 2 秒完成、号没切」
  和「手改的值被档案覆盖」两类回归
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

- 默认 `raid='auto'`：**只排普通**（2026-11-23 改；原先只排突袭，导致用户
  说「挂 1 到 5 普通」被排成突袭）
- `raid='raid'`：显式只排突袭
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
  `raid='normal'` 塌成三段；`tiers=['ex']` 只出 EX 两段；剧情关（ST）不进计划；
  **`raid='raid'` 时 S 段仍是 normal**（S 突袭没奖励），要突袭得显式
  `s_raid=True` 才会变 `raid`

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
- 重挂前回活动关卡界面：`_leave_result` 第一下必须点在 `_RESULT_TAP`
  （MAA ClickCorner，在地图上点了也不误触），**第一帧不许用 probe 判**；
  点完 `probe == 'map'` 就放行且不再导航；点满 3 次仍不是地图时退回
  `to_home` + `_navigate` 重新到位
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

### B46 牛杂小工具（2026-11-19）

入口（战时新闻委员会小游戏 / 活动商店）都长在当期活动地图上，点进去之后
交给 MAA 的 `Custom` 任务。测试不连模拟器，全部打桩。2026-11-22 起用 OS
（三个入口：喀兰贸易技术研发部 / 观测站后勤处 / 神谕谜题）当样本：

- 模板：`OS_mini.png` / `OS_shop.png` / `OS_reward.png` 都在包里，`roi` + `tap`
  齐全，识别区域不能互相盖住（小游戏在右上、商店在左侧）
- `run_tool`：先 `_to_map` 到活动地图，再点 `tools` 里配的节点；日志里带的
  是节点的中文名（「喀兰贸易技术研发部」）；没配的入口要明确报错，不能乱点
- 名字：中文名 / 别名（「小游戏」「商店」）/ 键 都能解析，认不出抛错
- **黑名单只能挂在 `params.event_shop.blacklist`**：顶层出现 `blacklist`、
  空数组、或元素不是字符串，都会被 MaaCore 静默忽略（等于买光无限池），
  所以只认 `params.event_shop.blacklist` 且非空全字符串
- 空黑名单 `build_params('event_shop', [])` 必须抛错
- `skill_tool` 串联：导航用的是 `ent['nav']`（minigame/shop）；`spec['task']`
  就是 maatools 拼的那份；黑名单支持逗号串（`'龙门币,固源岩'`）
- **卡教程**：`run_custom` 返回 `stuck` 时，`skill_tool` 的结果里要带
  `hint` 并在日志里出现「教程」二字（用户口径：告诉他手动过一遍再挂）

### B50 切号名绑定（2026-11-22）

设置里每个账号多了「配置文件 + 切号名」两栏，助手也多了 `set_account_name`。
这块错一个字的代价是 MAA 在账号列表里死找 4 分钟，所以全部打桩钉死：

- 归一：`5505` 原样、`1573635` / `157 3635` / `157****3635` 都出 `157****3635`
- `set_account_name`：给昵称能认到文件、写进 `maa.accountName` 且标
  `accountNameFrom=manual`；官服填昵称要报错；两个账号又没点名时返回
  `need_info=account`（让模型先问用户，别乱写）
- 配置文件优先：造一份 `gui.new.json`（`moren` 的 StartUp 写 `181****5505`），
  账号绑上它 → `acctbind.resolve` 出 `('181****5505','profile')`；
  `_tool_list_accounts` 和 `multiacct.candidates` 里也必须是这个生效值
  （否则模型看到空值又去问一遍）
- 手填优先：同一账号改成 `accountNameFrom=manual` + `5505` → 出 `5505`
- 读不到档案（目录被删/档案没建）→ 回落手填值，不抛
- 只读不写：`maa._save_cfg` mock 成「一调用就炸」，解析全程不许写用户的
  `gui.new.json`

### B51 复刻活动 OS + TL 收尾（2026-11-22）

用户这一天给了 OS（雪山降临1101，复刻）整套导航截图，同时要求 TL 结束之后
只留商店。这条用例只查数据和模板（真点要模拟器）：

- OS 标了 `reprint`，`zones` = base/ex/s，`tools` = minigame/shop/reward，
  还有 `claim`（奖励页的「一键领取」）
- OS 的九个模板（entry / z3 个分区 / 三个工具 / claim）文件都在包里——
  打包漏一张，这里当场红
- TL：`closed=True`、`zones` 空、`tools` 只剩 shop；`run(None,'TL-5')` 要在
  导航之前就返回「已经结束了」，不能白跑一趟地图
- 指代：「雪山降临」→ OS-1；「最新活动」→ OS-1（已结束的 TL 排后面）；
  `active()[0]` 是 OS
- 资源库：`activity_list()` 里 `act46side.reprint=True`、`act55side` 是 False

### B52 多 MAA 多模拟器编排 + 领奖 skill + 默认用当前账号（2026-11-22）

用户要的是「5 个号在同一台就切号循环跑完；分在不同模拟器就多开并行」，
这条用例把分组、串联、默认账号三件事一次钉住（不动真模拟器，全部打桩）：

- **账号自己的 MAA**：`acctbind.dir_of` / `profile_of` 优先读账号
  `maa.dir` / `maa.profile`，空时回落 `settings.maaDir` / 档案 `Current`
- **默认账号 = 左侧选中**：`_account_file('')` 在没给账号名时落到
  `settings.lastAccount`；`_tool_list_accounts` 返回顶层 `current`
- **分组**：`maaplan.plan_groups` 按 `(normcase(abspath(dir)), adb address)` 分；
  同目录同地址的两个号进同一组（串行），不同地址 / 不同目录分开（并行）。
  比较路径时用 `os.path.normpath(...).lower()`，别拿 `D:/X` 和 `D:\X` 直接比
- **串联**：`skills.run_batch` 按组跑，日志以 `[昵称]` 开头（昵称取
  `snap['account']['nickName']`，不是文件名——第一版写错过）；`_norm_steps`
  认 `fight OS-EX-1~7` / `tool 活动商店` / `wait 60` 和中文别名
- **领奖**：`SKILLS['claim']` 已注册、在 `_NEEDS_GAME` 里、**不在**
  `_WRITE_HANDLERS` 里（免确认）
- **助手工具**：`skill_claim`（只读处理器）+ `run_steps`（写处理器）都在
  白名单里，漏一个模型就调不到

### B53 多开三处硬伤（2026-11-22 晚，用户实测报的）

用户报「雪山降临走的老导航」「说好两台并行，第二台一动不动」。这条用例把
背后的判定全钉住（`_to_map` / `_tap` / `_moved` 全部打桩，不动模拟器）：

- **分区直点**：`eventnav.run(OS-1)` 只点「圣巡之旅」、`OS-EX-1` 只点
  「变革之路」、`OS-S-1` 只点「喀兰之心」。断言 `taps == [目标节点]`——
  以前是「先基础区再切」，EX/S 的模板会落到关卡列表上按兜底坐标乱戳
- **不退回老路径**：eventnav 返回失败时 `goto_stage_list` 必须直接失败并把
  原因带出来（把 `goto_story_activity` 换成「一调用就 AssertionError」，
  确保它真的没被走）
- **地址传到 MaaCore**：`run_core({'profile': 'b服', 'adbAddress': 'emulator-5556'})`
  取连接时必须用这个 profile + 地址（Engine 换成「一建就抛」的假类）
- **严格不换设备**：`Touch(strict=True)` 连不上 `emulator-5556` 时返回失败、
  **地址不变**，并且把在线设备列在原因里；非 strict 才允许自愈到 5554
- **点名地址参与分组**：`plan_groups` 认 `r['address']`，两行同目录不同地址
  必须分成两组
- **日志落盘**：`debug/tasks/<tid>.log` 真的写出来了，里面能看到任务标题和
  日志行——窗口关掉之后唯一的现场
- **组线程崩了要留痕**：让 `envcheck.ensure` 抛异常，任务日志里必须有
  「这一组出错」，不能静默死掉


### B54 导航从任意页起步（2026-11-22 续，B53 的补丁）

B53 把「点哪」钉住了，这条钉的是「**从哪一页开始**」和「怎么判自己在哪页」。
全部用 PIL 现场拼 1920×1080 合成图（灰底 + 把真模板按像素坐标贴上去），
不碰真机：

- **提示条走定点比对**：把 `OS_entry.png` 贴到 (150,805) → `_entry_here` 命中、
  分数 <3、中心 (350,827)；贴到 (700,300) → 不认；同一片区域贴别的模板
  （`OS_z1.png`）→ 分数超过 `_ENTRY_MAX`
- **不在主界面系就不许点坐标**：`at_home` 打桩成 False 时 `_to_terminal`
  必须一次坐标都不点（`taps` 为空），只走 `_to_home`；退不回来就老实返回空
- **在主界面系且提示条可见**：一次都不用点，直接返回
- **已在地图就别绕路**：分区节点认得到时 `_near_map` 返回截图且**不按返回键**
- **关卡列表**：认不到节点、`probe` 判 `map` → 只按一下返回，不许点坐标
- **`probe_bytes` 与 `probe` 同源**：三种页面（地图 / 主界面 / 空灰图）判定一致
- **整屏匹配没被改歪**：`find_card_bytes` 在合成图上仍 0 分左右命中
  (1388,894)±8（粗扫留前 3 名之后单峰场景不许退化）


### B55 右栏日志按账号分栏（2026-11-22，用户报「日志都消失了」+「只在自己的 tab 里」）

两件事都不碰真机，全靠合成数据：

- **接口不能再 500**：`tasks.get()` 的返回必须 `json.dumps` 得过、且**一个
  下划线开头的内部字段都不带**（`_fh` / `_prefix`）。当初把文件句柄塞进任务
  dict，`/api/task` 直接序列化失败，前端每 700ms 收一次错误，右栏只剩任务名
- **前缀补齐**：`tasks.create(prefix='[昵称]')` 后，`tasks.log()` 自动给无前缀
  行补前缀、把 `[组 emulator-5556]` 改写成 `[昵称]`；已有 `[昵称]` / `[MAA]`
  原样不动。挂在 `tasks` 层才拦得住 `maaplan.run_core` 直接写的那些行
- **owners 过滤**：单账号任务只出现在那个号的 `for_owner()` 结果里；
  `owners` 为空的全局任务所有 tab 都可见
- **多开子进程入口还在**：`multiproc.worker_cmd()` 带 `--skill-worker`，
  `backend/main.py` 里也有对应分支——少一个，多开就会静默退回单进程
- `group_tag()` 一组一个号时取昵称、多号组返回空（每行本来就有各自前缀）


### B56 页面识别 + 基建确认框 + 多账号默认导航 + 日志样式（2026-11-22 晚）

- **主页判据**：合成 1920×1080 图，贴「设置齿轮 + 邮件」两个模板才算主页；
  只贴齿轮（设置页那种情况）必须判否——只要一个就会把设置页认成主页
- **基建确认框**：把 `confirm_cancel` / `confirm_ok` 两个模板按相对坐标贴回
  一张 1920×1080 的图上 → `confirm_leave_bytes()` 返回「✓」的中心（±3px）；
  只贴一个圆钮、主页、以及**一整块红色**（关卡列表误判过的老坑）都必须判否；
  再验兜底 `_bar_bytes()` 的色彩签名（左半暗 + 右半红）能定位、主页不触发
- **to_home 流程**：给两帧（设置页 → 主页），按一次返回就停，不许在主页上
  再按（会弹「退出游戏」）；给两帧（确认框 → 主页），必须点在「✓」上且
  `backs == 0`（这个弹窗按返回 / ESC 都退不出去）
- **多账号默认导航**：`_nav_first(steps, many=True)` 给 fight 补
  `wakeup + navigate`；`assumeStageList` / `noSwitch` 两种例外不补；单账号不动
- **注册表**：`@skill` 登记的都在 `SKILLS` / `_NEEDS_GAME`，`signin` 不在
  环境闸里，中文别名（扫图）能解析成正式名
- **回调码**：`MSG_SUBTASK_START/EXTRA/ERROR` 必须等于 `20001/20003/20000`
  （MaaCore 5.x/6.x 的 `AsstMsg`；写 70001 就是「一场仗打完日志一个字都没有」
  那个坑）；`_translate` 对新旧两段都出 node/extra 事件；
  `sub_error` + `Copilot@WaitUntilEndOfAction` → 「结算不是三星」那句人话；
  装置占位那条 ERR 要翻成「不影响打」
- **练度修正静默**：`_fix_oper` 补完 `requirements.elite` 后 notes 里不许出现
  「精英要求」（用户 2026-11-22 说这是噪音），但技能/星级不合法照旧要记
- **日志样式**：`BattleFormation` → `[芬, 斑点, 黑角]`；
  `BattleFormationSelected` → `选择干员: 芬`；`CopilotAction` →
  `当前步骤: 切换倍速 / 部署 黑角 / 开始挂机`；换作业时先 `✓ OS-1 完成`
  再更新 `state['stage']`，并往 `results` 写一条 `ok=True`（重挂要靠它判过没过）
- **翻车重挂只动翻车那关**：三关（OS-6 翻车、OS-7/OS-8 没打）→ 重排时 OS-6
  换成候选 #401，OS-7 / OS-8 **保持原来那份**；没有候选池时同一份再试一次
  （日志「没有别的作业，同一份 #301 再试一次」，`same_retry` 里留痕）；
  失败原因是「理智不够」时直接不重挂
- **翻车回关卡界面（`_leave_result`）**：先验「失败只认 MAA 回调」——
  `chain_failed` 事件进来后 `results` 里必须出现一条 `ok=False`，且
  `WaitUntilEndOfAction` 能翻译成「结算不是三星」；再验界面流程——给一张
  深色地图帧（真 `probe_bytes` 判 `map`），点一次 `_RESULT_TAP` 就放行并打
  「已回到活动关卡界面」；把 `probe_bytes` 桩成 `home` 时要连点 3 次，
  再回落到 `to_home` + `_navigate`
- **结算页自识别必须已经删掉**：`pages.is_result_bytes` / `pages.result_cover`
  / `navtpl/pages/result_title.png` 都不该存在（2026-11-22 用户要求：失败
  以 MAA 通知为准，别自己识别）

### B57 跨分区分段导航（2026-11-22 晚）

`segments_of` 纯逻辑，不用 MaaCore：

- `OS-1/OS-2/OS-10/OS-EX-1/OS-EX-2` → **2 段**（`OS/base` 3 关、`OS/ex` 2 关），
  段上带 `from`/`to` 首末关
- 同分区 3 关 → 1 段（不许每关都导航一次）
- `OS-EX-1/OS-S-1` → 2 段；`OS-1/OS-EX-1/OS-2` → **3 段且顺序原样**（绝不重排）
- 主线 `1-7/2-1` → 1 段；`OS-ST-1/OS-1` → 1 段且归 `OS/base`（剧情关口径要
  和 `eventnav.run` 一致，否则会切出没标定分区节点的段）

`_run_tail` 用假 Engine + 桩掉 `_navigate`/`_leave_result`/`_wait_run`：

- 跨 2 段：`append` 两次（3 关 + 2 关），**段间 `_leave_result('OS-10')`
  一次**、`_navigate('OS-EX-1')` 一次；`can_reuse_nav=True` 时首段不导航；
  日志有「跨 2 个分区」「切到新分区完成」
- 段间导航失败**不许 raise**：照样 `append` 两段、返回 True、日志留「警告」
- 抄作业前面排了别的任务（Recruit）→ `can_reuse_nav` 失效，**首段也要导航**
  （断言 `naved == ['OS-1', 'OS-EX-1']`，append 顺序 Recruit→Copilot→Copilot）
- 单分区普通情况：只 `append` 一次，零导航、零点结算页

### B58 散点关卡（S 关，2026-11-23）

用户原话：「MAA 无法识别 S 关卡」。OS 的 S 关浮在雪山地图上（积雪清理系统），
MaaCore 的 `ClickStageName` 只在关卡列表里 OCR，认不出。这条路自己点到编队页，
再一关排一个单作业模式的 Copilot 任务。

纯逻辑 + 假 Touch/Engine：

- **形状解析**：`OS-S-3` → 3；`OS-EX-1` / `OS-1` / `1-7` 一律 0（否则挂 EX 会
  点成 S-1）；`has()` 能吃完整代号（以前只认前缀，分派永远不成立）
- **模板齐全**：5 个格子 + 开始行动 / 突袭模式 / 开始突袭 / 快捷编队
- **认格子**：把真模板贴回 1920 画布的对应位置 → 5 个全部认出；空图 → 一个都
  没有；**S2 的模板贴到 S5 的位置必须不算**（ROI 把位置也锁死）
- **详情面板 / 编队页判据**：「开始行动」在 → `has_detail`；「快捷编队」在 →
  `in_formation`
- **单作业参数**：`_single_params` 里**不许有 `copilot_list`**（有它就走
  MultiCopilot 的 ClickStageName 导航），必须带 `filename`
- **循环**（`run_scatter` + 假 Engine）：S1 普通 → S1 突袭 → S2 普通 三趟，
  `open_stage` 依次收到 `(1,False)/(1,True)/(2,False)`，`stage_trail` 三条；
  界面上没有的格子（未解锁）跳过不排作业；点开了但没进编队（理智不够）记进
  `stages_failed` 并接着跑下一关

### B59 定时任务预设（2026-11-23）

定时任务从「只能填路径」扩成「预设里挑」，预设只负责猜程序在哪。用例钉四件
事，不碰真实安装位置（临时目录造假 exe）：

- **预设表**：`custom` 打头、`self` 第二，`maa` 与六个其它游戏预设都在；
  `preset_id('MAA')` 大小写不敏感，乱填的 id 回落 `custom`
- **不猜的地方不猜**：`custom` 没填程序必须报错，不许偷偷换成别的 exe
- **MAA 预设**：把 `maaDir` 指到临时目录里的假 `MAA.exe` → `detect('maa')`
  命中的就是它；任务里 `program` 空着，`_argv` 跑时按预设补上同一个路径
- **扫盘**：给定目录里造 `BetterGI/bin/BetterGI.exe` → 按文件名找到；换名字
  找不到；没给 exe 名就一次都不扫
- **本体**：`fill('self')` 给的是本进程 python（源码模式带 `main.py` 参数）；
  用户手填的 `program`/`args` 不许被 `resolve()` 覆盖
- **自己的预设**：`config/schedule-presets.json` 能新增、能同 id 覆盖内置
  （覆盖时不写 `exes` 就为空，不把内置的 exe 名带回来）
- **保存清洗**：认识的 `preset` 留着，乱写回落 `custom`，老配置没这字段也对
- **脱敏守卫**：遍历 `backend/` `frontend/` `docs/` `tools/`，三方项目名、真实
  账号昵称、本人机器路径一个都不许出现（关键词在用例里拆成两半写，否则本文件
  自己就先违规）

### B60 实例绑定与渠道（2026-11-23）

用户报「指定 5554 却影响 5556」和「启动不看 b 服还是官服」，一个用例钉住两条
链路，全程不动真模拟器（`_devices` / `read_config` 换成假数据）：

- **连接参数 vs 客户端类型**：假档案返回 `Config: LDPlayer` + `clientType:
  Bilibili` → `_conn_for` 出来的 `config` 必须是 `LDPlayer`（那是 AsstConnect
  认的连接配置名）、`client` 才是 `Bilibili`；档案没写连接配置时兜底 `General`
- **地址校验**：`emulator-5556` / `127.0.0.1:16384` 收，`5556` 当场报错并提示
  正确写法
- **绑定落盘**：`/api/account/maa` 写 `adbAddress` → `acctbind.addr_of` /
  `skills._addr_of` 都读到；本次点名（`_ADDR.map`）优先于绑定；`list_accounts`
  带出 `maaAdbAddress`
- **绝不换台**：钉的那台不在线 → `device_of` 原样返回钉的地址（不换成在线的
  那台，交给 `envcheck` 按实例号去启动它，见 B62）；解绑后按档案走
- **档案过期**：档案地址不在线时，两台在线 → 拦下要用户指定；只剩一台 → 顶上；
  一台都没开 → 返回空交给 `envcheck` 去启动
- **设备下拉**：`/api/account/devices` 返回在线列表（地址 + 状态）、本机实例
  （`instances`，含没开的）和当前绑定

### B61 客户端类型进任务参数 / 停止能按账号点名（2026-11-23）

同一条跑机链上两个洞，一个用例一起钉住：

- **渠道判定一个入口**：`resolve_client` 认 `Bilibili` / `bilibili服` / `官服`
  / 乱写（→ `Official`）/ 空；只给 `accountFile` 时也能从账号的 `channelName`
  回查出 `Bilibili`——上游漏传 `clientType` 也不会再按官服跑
- **落进任务参数**：b 服账号的 `core_tasks` 里 `StartUp.client_type` 必须是
  `Bilibili`（asst.log 里那个值才决定 `am start` 哪个包），官服账号是
  `Official`；`_brief` 说的是人话「b服」
- **停止按账号**：`owner_match` 认带/不带 `.json` 的文件名、不认空归属；
  `stop_live([这个号])` 只停它一个，全停才把剩下的停掉，已停过的不重复计数
- **名字解析**：`match_accounts` 完全相等优先，子串对上多个 → 报错（停错号
  代价太大）

### B62 多开启动：按地址反查实例号（2026-11-23）

用户报「只能帮我启动 5554 的模拟器，启动不了 5556 的」。假 `ldconsole`（把
`envcheck._run` 换掉，`list2` 回两个实例、`get-serialno` 回
`emulator-5554/5556/5558`）钉住四件事：

- **地址 → 序号**：`emulator-5556` → 实例 1；`127.0.0.1:5557`（adb 口）也是
  实例 1；本机没有的 `emulator-9999` 和「明确是别的模拟器」都返回 `None`
- **序号压过档案**：假档案 `InstanceIndex: 0`，`read_config(address=
  'emulator-5556')` 必须给出 `index=1 / indexFrom='地址'`
- **启动那一台**：`ensure(address='emulator-5556')` 时只有 5554 在线 →
  `launch` 拿到的序号是 **1**（不是档案里的 0），日志说「实例 1」，并且
  `wait_ready` 的 `wants` 里带着 `emulator-5556`
- **三种边界**：目标已在线 → 放行且**零日志**；关了自动启动 → 只提示不动手；
  反查不到是哪台实例 → 不猜也不启动，回「没在线」让用户自己开
- **下拉里能选到没开的实例**：`/api/account/devices` 的 `instances` 同时给出
  5554（开着）和 5556（`running=false`）——设置页要能选到关着的第二台
- **界面接口**：`/api/run/stop {file}` 的 `accounts` 只含这个号；`{all:true}`
  才是全停
- **多开子进程**：`multiproc.stop_groups` 按这一组的 `files` 匹配，命中才
  `kill`，并把 key 记进 `_STOPPED`（`run_group` 据此返回 `stopped=True`，
  不记「子进程异常」）

### B63 一路一个进程：多开第二个号不把前面那路跑挂（2026-11-23）

用户报「我多开另一个号，他把我前面的停了」。asst.log 里没有 `TaskChainError`，
只有 `Stop | block` + `~Assistant`：同一进程里开第二个 MaaCore，第一个正在跑的
链被进程级全局资源重置打断，上层只看到 `TaskChainStopped`。用例钉住：

- **名额只有一个**：`maacore.claim_inproc()` 第二次返回 `False`，`release` 之后
  又能抢到（不释放就是「以后的跑机永远走子进程」）
- **`live_others`**：本进程活着的实例计数，`exclude=` 不算自己（`Engine.open`
  的闸门按它判）
- **`run_skill` 让位**：主线程先占住名额 → 跑 `wakeup` 时必须走
  `multiproc.run_group(kind='one')`，`_skill_body` 一次都不能在本进程被调用，
  结果从子进程的 `results` 取回
- **单组批量让位**：同样占住名额 → `run_batch` 走 `_run_group_proc`；没人占时
  仍在本进程跑（别为单开白起一个进程）
- **`run_plan` 单台同理**：占住名额 → 走 `multiproc.run_group(kind='plan')`
- **停止不再默认全停**：`/api/run/stop` 既没 `file` 也没 `all` → 回 `error`
  且 `stopped=0`

### B64 退出确认点 ✕：不再按 ESC 把游戏关掉（2026-11-23）

用户报「S 关入口没认出来 → 一路按 ESC → 主页也没认出来 → 停在『是否确认退出
游戏？』→ 继续按 ESC，游戏直接退出」。两种确认弹窗的按钮条一模一样（左 ✕ 右 ✓），
以前统一当基建框点了右边 ✓ = 替用户退出。用例用 PIL 合成帧（真模板原位贴，
底色亮/暗两版）钉住：

- **亮底 + 按钮条 = 退出确认**：`exit_confirm_bytes` 返回左半 ✕ 的坐标；
  `confirm_leave_bytes` 也认得（两者都命中，谁先谁后由调用方保证——`to_home`
  里退出确认排在前面）
- **暗底 + 按钮条 = 基建确认**：`exit_confirm_bytes` 必须返回 `None`，
  `confirm_leave_bytes` 返回右边 ✓（别把「离开基建」也取消了）
- **没有按钮条的亮帧两个都不认**（别在普通页面上乱点）
- **`to_home` 一次返回/ESC 都不许按**：喂「亮底按钮条 → 空白帧」，`to_home`
  返回 `True`、`keys == []`、只 tap 一次且 x≈0.3464w

### B65 难度口径：要突袭就真排突袭（2026-11-23）

用户报「我让他打 OS-EX-8 突袭，结果两个账号导入的都是普通 EX-8」。根因是提示词
写着「EX 关默认只打突袭」、代码默认已是普通（`auto→normal`），助手照描述办不传
`raid`。用例钉住三处：

- `_variant_arg`：`raid/突袭/only → raid`，`普通 → normal`，空 → 不筛
- `_prepare_ops` 的第三个返回值 `raidMissing`：要突袭却只拿到普通 → 列出那几关；
  本来要普通、或真排到突袭 → 必须为空
- `add_stages` 真的按 `raid` 筛行：突袭行没有可用作业时**报错 + raidMissing、
  一份都不写**（绝不拿普通顶上）；两行都有作业时只写突袭那条，
  `difficulties` 回 `{关卡: '突袭'}`

### B67 主线导航：章号 → 网格格子 → 点卡 → 「前往章节」（2026-11-24）

用户标注的主线四步（终端 / 曲谱 / 乐章收录 / 默认进度）和活动卡导航共用，
之后按章号在第 5 列网格里找 EPxx 卡、点开章节详情、再点右下「前往章节」。
用例全用合成帧 + 假 Touch，不碰真机：

- **代号解析**：`15-3`→15、`0-11`→0、`17-4`→17、突袭 `H15-1`→15；
  `GT-1` / `MN-1-7` / `TR-1` / `EP-1` / `18-1` / 光秃秃的 `17` 都必须返回 `None`
- **模板齐全**：18 张 `navtpl/mainstory/EP00~17.png` 都在，且都是 265×265
- **格子定位**：合成帧里贴第 0/5/17 章，`_rows_of` 要认出 261 和 576 两行；
  `_best_in` 用第 17 章模板命中 (862,708) 附近（±2）；换第 1 章模板打同一帧
  必须 > 12 分（EP00/EP01 都是「黑暗时代」，实测 22.6，阈值放过去就会点错章）
- **分派**：`goto_stage_list('17-3')` 必须走 `mainnav.run`；`GT-1` 必须仍走
  老故事集那条路，不能被主线接管
- **主页图标标定**：按 `pages._HOME_ICONS` 贴图 → 认主页；把邮件挪回 2026-11-22
  那份错的坐标 (188,32) → 认不出。这条防的是「坐标跟真实 UI 脱节，所有回主页
  的导航退化成一味按 ESC，最后把游戏按关」

## 什么时候跑

- 改了 `vault.py` / `accounts.py` / `settings.py` / `main.py` 之后
- 改了打包、启动脚本或 `.gitignore` 之后（A 组和 C 组的目录零污染检查）
- 发版前过一遍

依赖只用到程序自身的运行环境（B/C 不会联网调森空岛接口），用 `.venv` 的
Python 跑即可；C 组会真实调用 Windows DPAPI，换电脑/换用户跑 B 组是
「加密绑定本机」的活体验证。
### B68 主线作战环境（剧情 / 标准 / 磨难）

合成帧（把真机模板贴回原位）验识别，再用一个假界面验状态机：

- 写法（剧情 / 标准 / 磨难 / story / hard）→ 内部 key；乱写的返回空；
- 只有第 9 章（两档）和 10~14 章（三档）能切，0~8 / 15~17 一档都没有；
- 三张难度模板尺寸 = 几何盒子尺寸（换模板必须同步改几何）；
- 贴哪一档认哪一档（0 分），别的档不许认成它；空白页 / 展开态认不出；
- 切档：收起点按钮 → 点 tab；已展开就直接点 tab（点按钮会选中第一个 tab）；
  第 9 章切剧情要在弹窗里再点一次左边按钮；
- 认不出那行字时明确报错、一下都不点；
- 接线：`difficulty` skill 在册、`skill_fight` 带 `difficulty` 参数、
  `DIFF_OF_MODE` 三档映射、提示词里有「9~14 章」和「作战环境」。

### B69 同账号串行 + 导航带难度（2026-11-25）

用户当天报的两个现场：说「导航到 13 章 + 切磨难」，助手拆成两个调用并发发出去，
切难度那一路在 MAA 还在登录时报「游戏没在跑」，导航一次没做；第二次只调了导航，
档没切。钉住：

- `_wait_idle`：同账号有**更早开工**的任务在跑就等（日志「等它跑完」），等到了
  接着跑；别的账号不等（多开该并行）；只有比自己晚开工的不等（互相等会死锁）；
  没有 owner 的全局任务直接放行；**同一毫秒建的两个任务**（把 `startedAt` 强行
  设成一样）靠创建序号仍要分出先后——这条防的是 2026-11-25 CI 上的
  `AssertionError: []`（Windows 时钟粒度 15.6ms，本机偶尔能过、CI 必挂）；
  等日志别写死 `sleep(0.3)`，轮询到出现为止（最多 8 秒）；
- `_ensure_game`：游戏没在跑要**自己叫唤醒**（日志「开始唤醒（MAA 自动唤醒，
  不干等）」），唤醒后进程起来了就放行，不干等满 90 秒；唤醒抛异常但进程起来
  了照样放行（只是记一行「自动唤醒没成功」）；一直不起来才返回 False；所有
  轮询都走 `game_alive(quiet=...)`（安静模式）；
- `skill_navigate` 带 `difficulty`：第 13 章 + 磨难 = 导航完自己切（只调一次
  `set_mode`）；不带就不切；第 5 章（没有这条栏）忽略而不是报错；写法认不出
  报错；非主线关报错；切档失败要 `RuntimeError` 而不是静默成功；游戏进程不在
  时导航入口真的把唤醒调到了（唤醒后继续导航）；
- `set_mode` 两轮都切不过去时返回说明（以前这里引用未定义的 `last` → NameError）；
- 接线：`skill_navigate` 工具带 `difficulty` 枚举、提示词里有
  `skill_navigate(stage="13-1", difficulty="hard")` 和「同一个账号的 skill 是**串行**」。

真机复验（5554，2026-11-25）：`goto_stage_list('13-1')` 54 秒到位，
`set_mode('hard')` 9 秒切成（左下角变红字「磨难险地环境」），验完切回标准。

### B70 游戏没跑就自己唤醒（2026-11-25）

用户截图报：adb 已就绪（`adb: adb 正常（emulator-5554）`）之后，日志只剩一排
「adb 返回 1：」和「游戏还没起来，等一会儿（最多 90 秒）…」，MAA 的自动唤醒
一直没动。钉住：

- `stagenav.Touch.game_alive(quiet=True)`：查不到进程（`pidof` 返回码 1）是正常
  情况，**一行日志都不写**；不传 `quiet` 时仍然留证据（`adb 调用失败…`）；
- `skill_navigate` / `skill_difficulty` 的源码里必须出现 `_ensure_game`、不能
  再出现 `_wait_game`（防止以后又被改回「干等」）。

真机复验（5554，2026-11-25）：游戏没跑时 `_ensure_game` 立刻开始唤醒，56 秒后
客户端回到主界面、返回 True；安静模式下查询日志零条。


### B71 结束活动的商店导航（2026-11-25）

用户报「TL 商店导航失效」。活动结束后终端卡片被盖上「作战已结束」灰条、
地图商店节点多了剩余时间横幅，模板全过期：entry 定点比 46.8 分（阈值 16）→
判定终端页没这个活动，`_to_map` 直接失败；节点 14.1 分（阈值 12）→ 只走兜底
坐标盲点。重抠模板之外，钉住「模板和 roi 不能脱钩」：

- `TL` 的 `closed` 为真且 `zones` 为空（关卡导航确实关着）；
- 模板尺寸 ≈ `roi` × 1.5（差超过 3 像素就当标定漂了）；
- 按 `roi` 位置贴到合成图上，`_entry_here` 必须认到（≤8 分）且中心对得上；
  **挪 20 像素必须认不到**（定点比对不接受整屏搜索兜底）；
- 商店节点贴在自己的 `roi` 里 `_find` 认得到（≤8 分、中心落在 roi 内），
  贴到别处（差 400 像素）必须认不到。

真机复验（5554，2026-11-25）：`run_tool('shop', prefix='TL')` 12 秒进
「炮火所掩埋的」商店（关闭时间 2026/11/29 03:59），日志「点活动提示条
（3.0 分，(1145,540)）」→「图像识别到「炮火所掩埋的」（2.7 分）」。

### B72 多账号每个号先唤醒切号（2026-11-25）

用户第三次报「助手又没有加切号自动唤醒」，所以这条从提示词升级成程序兜底，
用例把判定逻辑和接线一起钉住：

- `_wakes_itself`：`wakeup` / `sweep` / `weekly` / `train` / `claim`（带
  `wakeup=true`）/ `fight`（带 `wakeup` 且没有 `noSwitch` / `assumeStageList`）
  算自己会唤醒；`sweep` 显式 `wakeup=false`、`fight` 不带 wakeup、
  `tool` / `navigate` / 空步骤都算不会唤醒；
- `_with_auto_wake`：多账号 + 不唤醒的步骤要在最前面补一步 `wakeup`；连跑两次
  不能叠两个；单账号 / `switch=False` / 第一步是 `sweep` 都不补；**补的时候不许
  改动传进来的原列表**（纯函数，调用方还要拿原表算 `nav_added`）；
- 接线：`run_steps` 工具暴露 `switchAccount`、`_tool_run_steps` 源码里必须有
  `switch=sw`（防「参数加了但没传下去」）、`SYSTEM` 里有「一次任务涉及两个及
  以上账号」这句硬规矩；
- 引导卡片：第一条仍是「你可以帮我干什么？」，并且有「TL 活动商店清空」那条，
  黑名单正好三样（数据增补条 / 数据增补 / 家具零件），**不能混进龙门币**。

### B73 主题包与开机自启（2026-11-25）

用户这轮的两条需求：「主题要黑色/白色/第三方，第三方放 pack-data 点下载才能用」，
「开机自启默认不勾选，我极度厌恶没同意就自启的软件」。用例把「默认关」和
「按需下载」钉死：

- **自启默认值**：`DEFAULTS['launchAtLogin'] is False`（真 `False`，不是 `'false'`）；
  `'false'` / `'0'` / `'no'` / `0` / `None` / `''` 存进去读回来都必须是 `False`，
  `'true'` / `True` 才是 `True`。
- **源码模式不碰注册表**：`supported()` 为假、`exe_path()` 为空；`sync(True)`
  返回 `skipped` 且 `why` 里有「源码」；`sync(False)` 同样 skipped；`enable()`
  必须抛 `RuntimeError`。**关键一条**：这些调用前后 `_read()` 的返回值必须
  一模一样——开发时跑一次就把用户打包版那条自启删掉，是这里最容易犯的错。
- **主题 id 归一**：`' CLOSURE '` → `closure`，`''`/`None`/`'DARK'` → `dark`。
- **安装链路（假网络，不真连 GitHub）**：内存里造一个 zip + manifest，`_get`
  对 zip 返回字节、对 `extra` 里的 webm 一律抛错 → `install()` 必须成功、
  `extraFailed` 非空、`extra` 为空（降级成纯配色，不报错也不回滚）；
  `_read_meta` 读回来的 `pet.every` 要对得上。
- **sha256 不对**：同一份 manifest 改掉 sha 再装必须抛错，而且**原来那份还能用**
  （`usable()` 仍为真）——下歪了覆盖掉已经能用的主题是最差结果。
- **内置主题不许装/删**：`dark` / `light` / `a/b` / `../x` / `''` 全部 `ValueError`。
- **防穿越**：`file_of('demo', '../../settings.json')`、`'..\\..\\settings.json'`、
  `'/settings.json'`、`''` 全返回 `None`；zip 里塞 `../evil.txt`，
  `_safe_extract` 必须在解压前抛 `RuntimeError`。
- **远端读不到不裸奔**：`fetch_manifest` 抛错时 `catalog()` 仍返回内置两套，
  `remoteError` 带原因，`remote` 空列表（**这个键必须永远存在**，前端和用例都
  直接取）。
- **接线**：`/api/themes`、`/api/theme/install`、`/api/theme/remove`、
  `/api/autostart` 四个路由都在 `ROUTES` 里；`main.py` 有 `/themes/` 前缀分支
  且走 `themes.file_of`；启动时有 `_auto.sync`；`app.js` 里有色块垫底
  （`theme-swatch`）、三个按钮的 data 属性、`syncThemePet`、`set-launch-login`，
  `SET_TABS` 第一项是常规设置；**`THEME_FIXED` 两条都必须带 `installed: true`**
  （少了卡片会渲染成「下载」，点下去后端回「内置主题不能安装」）；
  `style.css` 里不许再出现 `rgba(255, 255, 255`，`#0e121a` 只许在 `--panel-3`
  出现一次（写死就换不了浅色主题）。

真机复验（2026-11-25）：`themes.install('closure')` 走真实镜像，70.4 秒装好
（0.8 MB 包 + 四个 webm 共 10.5 MB），`extraFailed: []`；`/themes/closure/`
下 `theme.css`（12 KB）、`preview.jpg`（23 KB）、`pet/idle.webm`（1.39 MB）都是 200；
`/themes/closure/../../etc/passwd` 是 404。

### B74 主题切换不报错（2026-11-25 用户反馈「点白色主题报错」）

现场是两条独立故障叠在一起，看着像「白色和卫戍协议混了」：

1. **`saveSettings()` 不返回响应**。调用方写的是
   `const d = await saveSettings(...); S.settings = d.settings`，d 是
   `undefined` → 每次抛 `Cannot read properties of undefined (reading
   'settings')`。主题其实已经切换成功并写进 settings.json，但 catch 里
   `applyTheme(prev)` 又回滚，用户看到的是「点了白色，闪一下还是原色，弹一句报错」。
2. **卡片依赖全量清单**（要联网拉远端 manifest，超时 6~8 秒）。远端慢的时候
   设置页只有深色/白色两张卡，已装的「卫戍协议」不出现，状态看着就错乱。

用例钉住：

- `saveSettings` 的函数体里必须有 `return d;`（否则 `d.settings` 必炸）；
  两个调用点必须是 `(d && d.settings) || S.settings`。
- `themes.catalog(local_only=True)` **不联网**：把 `fetch_manifest` 换成抛错，
  仍然返回内置两套 + 已装主题，且 `partial == True`、`remote == []`、
  `remoteError == ''`。
- 全量 `catalog()` 断网时 `remoteError` 带原因，但本地主题一个都不能少。
- `api_themes({'local': '1'})` 走本地快照；不带参数才联网。
- 前端有 `paintThemeGrid()`、先请求 `/api/themes?local=1`、卡片在
  「设置里的主题不可用」时高亮深色（`if (S.themes && !themeInfoOf(cur)) cur`），
  切到未下载主题时明确提示「先点卡片上的『下载』」。

真机复验：深色 → 白色 → 卫戍协议 → 白色 连续切换，无报错；刷新后仍是最后一次
选的主题；切回白色时 `<link id="theme-pack">` 和桌宠 `<video>` 都正确移除。

### B75 国内源 Mirror 酱（2026-11-25 接入）

对方 PR #137 加了上传 workflow（RID `MAA-PRTS`、`os=win`、`arch=x64`），
这里测的是客户端那半。**一个网络请求都不发**：`mirror._request` 换成假响应。

- **更新源**：`DEFAULTS['updateSource'] == 'auto'`；`' MIRROR '` 归一成 `mirror`；
  `'nonsense'` 回落到 `auto`（拼错一个字段不该把更新检查弄死）。
- **版本名格式**：`_fmt_current('2.7.0') == 'v2.7.0'`（我们发的 tag 带 v，
  `current_version` 格式不一致就永远命中不了增量包）；`'dev'` → 空
  （源码模式不该拿去问服务端）。
- **错误码**：7001→expired / 7002→invalid / 7003→quota / 7004→mismatch /
  7005→blocked，8001、1001、0、未知码一律 `error`。界面只认这几个词，
  错误码表变了不用动前端。`cdk_error` 只为 7001~7005 为真。
- **到期时间戳**：秒和毫秒都归一成同一个日期；空值返回空串。
- **响应解析**：不填 CDK 时能拿到 `version_name` 但没有 `url`（所以**不能拿
  url 判断有没有更新**）；`sha256` 转小写、`filesize` 转 int；
  缺 `version_name` 必须抛错（不能当成「已是最新」）。
- **不泄露**：7002 的错误信息是中文，且**不含 `cdk=` 也不含完整 API URL**；
  `probe()` 的返回里搜不到传进去的明文 CDK。
- **CDK 存储**（Windows 才测，非 Windows 跳过）：`mirror.key` 里搜不到明文、
  只回掩码且露得不超后 4 位、`settings.json` 里搜不到、清除后密文文件消失、
  带空格的 CDK 被拒。
- **时效链接不落盘**：`_persistable()` 剥掉 mirror 源的 `asset.urls`，
  但保留 `sha256` / `size`，GitHub 兜底的 `github.urls` 不能被剥；
  GitHub 直链的 asset 原样保留。落盘后 `appupdate.json` 里搜不到加速域名。
- **按源校验**：`_download_plan()` 里 mirror 地址配 mirror 的 sha256，
  GitHub 兜底配空 sha256（降级成结构校验）。这条是「混用哈希 → 稳定报
  sha256 不匹配 → 用户以为下载坏了」的唯一防线。
- **接线**：`/api/mirror/cdk`、`/api/mirror/probe`、`/api/mirror/status`
  三个路由都在；`mirror.py` 里 `OS = 'win'` / `ARCH = 'x64'`（不是文档里的
  `windows` / `amd64`）；`app.js` 有 `updateSource` / 三个接口 / `mirrorMsgText` /
  `data-src=`；`style.css` 有 `.src-opt`。上传 workflow 还没合并时跳过那三条断言。

真机复验（2026-11-25）：`mirror.check()` 直达 `mirrorchyan.com` 返回
`v2.7.0` + 完整 release_note，无 CDK 时 `url` 为空；假 CDK 返回 7002
并被翻成「CDK 不对，请检查后重新填写」；`POST /api/mirror/cdk` 存完掩码是
`········1234`，清除后 `hasCdk: false`。

### B77 多开分组只认设备（2026-11-26 用户现场「唤醒把游戏关掉」）

用户的账号分别绑在 `MAA-v6.20.1-win-x64` 和它的 `…-1` 副本上。旧的分组键
是 `(MAA 目录, 地址)`，于是 `emulator-5554` 和 `emulator-5556` 各被拆成两组，
同一台模拟器上两个独立进程同时点账号列表 —— MAA 报「点登录 没成功」→
`Login failed, entering game-restart loop` → 反复关掉并重启游戏，8 个号全挂。

- `maaplan.addr_key`（`skills._norm_addr` 是薄包装）：`127.0.0.1:5557` →
  `emulator-5556`（雷电 adb 口 = 控制台口 + 1）、`EMULATOR-5554 ` →
  `emulator-5554`、空 / None → 空串。
- `skills._emulator_key`：两个号绑不同 MAA 目录、连同一台 → key 相同；
  目录仍然**按号返回**（组里混绑目录时不能拿组的目录跑别人的号）；
  换地址写法还是同 key；不同设备必须不同 key。
- `maaplan.plan_groups`（一键挂机）同一条规矩：同地址不同目录合并成一组、
  两台设备仍分两组。
- 不连模拟器：`_maa_dir_of` / `_conn_of` 换假的，`plan_groups` 靠 `address`
  字段短路，不会去读真实 MAA 配置。

真机验证（2026-11-26，emulator-5554）：单号带切号名 2144 连跑两次都
`TaskChainCompleted`（冷启动 84 秒 / 已在目标号主界面 37 秒），说明切号本身
没问题，失败只在「两个进程抢同一台」时出现。

### B78 绿票 / 黄票商店（2026-11-26）

MAA 牛杂里那两个常驻商店接进助手。和前三项（活动里的小游戏 / 活动商店）
最大的不同：**入口不在活动地图上**，任务自己从主界面走「终端 → 仓库 →
凭证交易所」，所以我们只要保证「人在主界面」。

- **任务名**：`GreenTicket@Store@Begin` / `YellowTicket@Store@Begin`——写错了
  MAA 当不存在，跑完什么也没买还不报错，所以这两条断言钉死。
- **不传 params**：绿票默认就是 MAA 内置流程（一层全买、二层只买单抽 +
  招募许可），正好是用户要的口径；断言 `build_params` 里没有 `params`。
- **不导航**：两项 `nav` 为空、`home` 为真；`skill_tool` 走
  `stagenav.reach_home`，**不许**再进 `eventnav.run_tool`。
- **黄票高危**：没带 `confirm` 时直接返回 `need_confirm`，测试把
  `stagenav.Touch` 换成「一构造就断言失败」的桩，确保它连模拟器都没碰；
  带了 `confirm` 才走到 `run_custom`，且 `spec['task']` 就是那条任务名。
- **助手层**：`autoApply` 开着，`_tool_skill_tool` 也必须返回 `need_confirm`
  （`_guard(force=True)`），卡片上要写明 258 黄票。
- **批量**：`_tool_step_need_confirm` 能从 `run_steps` 的步骤里认出黄票，
  绿票 / 扫图这些不算高危。
- **唤醒口径**：`_wakes_itself([('tool', {})])` 为假（单号不白等一次客户端
  重启），带 `wakeup` 才为真；多账号自动补一步 `wakeup`，`switch=False` 不补。

> B77 号留给还没合进来的「多开分组只认设备」分支，这里直接排 B78。

### B79 单账号先看界面再干活（2026-11-26）

用户口径：「每个单账号任务的运行前检查当前明日方舟界面的位置，必须回到明日
方舟主页，再开始导航」，目的是**减小重复自动唤醒的时间**——MAA 的 StartUp
每次都要重启客户端（冷启动 40~80 秒），单号连着跑几个任务就白等好几遍。

- **哪些 skill 要做**：`_home_check_on` 只对 `navigate / sweep / weekly /
  train / claim / tool / difficulty / skip_story` 生效；`wakeup`（自己就是
  唤醒）、`signin`（不碰游戏）不做；`fight` 默认 `noNav` 直跑也不做（用户可能
  正停在关卡列表，拉回主页反而弄坏），带 `wakeup` / `navigate` 才做。
- **几条不许拦的**：参数里有 `switch`（多账号）、`assumeStageList` /
  `noSwitch` / `direct`（用户说已经在关卡列表 / 别切号）。
- **`ensure_home` 四种走法**：在主界面 → `already-home`（不截图之外零动作）；
  在别处 → `reach-home`（只按返回键）；走不回去 → 抛错（`wake=True` 才真唤醒）；
  游戏没开 → 抛错（同样只有 `wake=True` 才唤醒）。
- **`skill_wakeup` 的快速返回**：单账号在主界面时直接返回 `already-home`，
  `maaplan.run_core` 一次都不许调；带 `switch=True` 时必须走 `maa-startup`。
- **落点两处**：`_skill_body`（单个 skill 任务）和 `_run_group` 的步骤循环（`run_steps` 单账号批量）都调一次；多账号的步骤被 `_mark_switch` 盖了 `switch=true`，判定直接为假。
- **多账号把捷径关掉**：`_with_auto_wake` 补的那一步是
  `('wakeup', {'switch': True})`，`run_batch` 再用 `_mark_switch` 给每一步盖
  `switch=True`——否则第二个号会在第一个号的账号上开打。

全程打桩：假 `Touch` + 假 `run_core`，不连模拟器。

### B80 中文关键词兜底（2026-11-26）

用户现场：搜「结城理模组」没有结果，助手帮着打模组任务时只会说「没有作业」。
根因不是作业站没有，而是 `resources.resolve` 认不出**不是关卡名**的词就直接
`mode='unknown'`。

- **前提**：`resolve('结城理模组')` 仍然是 `unknown`（这条不许改，它本来就不是
  关卡）；兜底在 `copilot` 里。
- **拆关键词**：`结城理模组 4-3` / `4-3 结城理模组` 都能拆出关卡（`main_04-03`），
  剩下的字当全文关键词；`结城理模组` 拆不出关卡；`模组3` 这种带数字的不许被
  当成关卡。
- **纯关键词**：只传 `document`，按作业自己的 `stage_name` 分组 → `mode='keyword'`，
  每行都带关卡代号，空行（某关没作业）不留，`next` 为空。
- **关键词 + 关卡**：传 `levelKeyword` + `document` 强制锁定；作业站混进来的
  别关作业必须被 `stage_name` 二次校验挡掉，只剩锁定那一关，`locked` 带关键词。
- **真搜不到**：仍旧 `mode='unknown'`，界面照旧「没有结果」。
- **助手**：`build_run` 遇到 `keyword` 结果**拒绝排计划**，把命中的关卡列回去，
  让模型问清是哪一关再重排（横跨十来关照单全挂 = 替用户决定打一堆他没说的关）。

作业站用打桩 `_pull`，不联网。

### B81 多账号一键长草（2026-11-26）

用户现场：多账号日常是「我们的导航 + 作业站」在打关卡，MAA 同时在跑它自己的
日常队列，两条腿抢同一台模拟器 → MAA 报错。改成读每号自己配置的关卡，用 MAA
的一键长草跑。

- **白本**：`resources/daily-plan.json` 的六个任务顺序 + 从 181 抄的参数（公招
  `times=4`、基建 `drones=CombatRecord`、信用黑名单、领奖全开）；切号名 / 地址 /
  关卡必须是空的。
- **档案映射**：只认勾了的任务（并列的「日常/活动/剿灭」三档里没勾的不算）；
  `Copilot` 直接跳过（进不了核心队列）；只剩「开始唤醒」的档案要能被识别成「没配
  日常」（detect 会退回白本）。
- **核心任务链**：`StartUp/Recruit/Infrast/Fight/Mall/Award` 六条，没有 Copilot；
  `Fight.times` 不能写 0（单关要按 999 刷到理智不足），勾了次数限制就照档案的
  数字；`Recruit` 用 `times` 不是老的 `max_times`。
- **探测**：有档案的号 `source=profile` 并带上关卡与模拟器地址；没档案的号
  `source=default` 且进 `needStage`。`plan` 给了 `stage` 就统一覆盖；只有没档案
  的号、又不给关卡时直接说排不出来（不拿白本的空关卡顶上）。
- **推荐关卡**：只挑没结束的活动，默认 `-8`；关卡代号资源包认不出就不推荐。
- **逐号 spec**：`run_plan` 分组后每个号的 `tasks`/`stages`/`taskParams`/`profile`
  都是各自的；官服按 `Official`、b 服按 `Bilibili`；不许带 `noNav`。
- **接线**：`daily_check` 在只读表、`daily_run` 在写操作表（要确认），提示词里
  点名这两个工具，前端第一条引导是「帮我使用推荐设置一键长草刷左侧所有账号」。

全程打桩（假 candidates / 假 `_load_cfg` / 假 `run_core`），不连模拟器、不读真配置。

### B83 换完 exe 刷图标缓存（2026-11-28）

v2.8.7 装好后桌面快捷方式还是旧图。`install.ps1` 换完文件直接起新进程，全程
没通知 shell；本机 `iconcache_*.db` 时间戳停在 11-25 02:49，而系统 11-26 12:23
重启过——**重启电脑不会重建图标缓存**，只能主动发 `SHCNE_ASSOCCHANGED`。

- **时机**：`on_start()` 里「`pending.to` 等于当前版本」那条分支，`refresh_icon_cache()`
  紧跟着清理备份执行；版本对不上（没装成功）一律不刷，免得白扫一遍缓存目录。
- **实现**：`ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)`，
  失败只吞掉异常返回 False，不许连累启动结算。
- **不在安装脚本里做**：`install.ps1` 无窗口跑，`Add-Type` 编译 C# 还可能被安全
  软件拦；放进新版本启动路径更稳，而且这时候 exe 已经是新的，扫出来就是新图。

打桩 `update_dir/_current/_state/_save_state`，只在临时目录写 `pending.json`，
不碰真实 `update/` 和 `config/appupdate.json`；最后真调一次函数验 API 可用。

### B84 干员头像按需下载（2026-11-28）

抽卡界面要显示干员头像，但 469 张 96px WebP 约 2MB，不能进安装包。做法是
「用到哪张下哪张」，B84 钉的是这条链路的边界：

- **id 校验**：只认 `[A-Za-z0-9_]`，`../x`、`a/b`、超长串一律当场 `KeyError`，
  别指望后面的路径拼接兜住穿越；
- **缓存优先**：命中 `config/cache/avatar/<id>.webp` 就直接返回，不再发请求；
- **索引决定要不要走镜像**：`pack-data` 上 `resources/avatars/avatar-index.json` 里有这张才去试
  三个镜像，没有就直接走 PRTS 兜底——否则每张都要白等三次 404/超时（实测首张 5.7s→0.7s）；
- **兜底**：镜像全挂时从 `torappu.prts.wiki/assets/char_avatar/<id>.png` 取原图，
  服务端转 96px WebP；转不了就存原 PNG，宁可大一点也别丢功能；
- **负缓存**：两条路都挂记 5 分钟，第二次请求不再打网络（列表页一屏十几张，不能每刷一次重打一遍）；
- **不认假图**：空包、HTML 错误页、只有 `RIFF` 头的假 WebP 都不许落盘。

打桩 `settings.CONFIG_DIR`、`avatar_index`、`_from_mirrors`、`_from_prts`，不联网、不写真缓存。

### B85 账号头像（2026-11-28，2026-11-29 改口径）

森空岛同步时本来就存了两样：`account.avatar`（游戏内头像直链）和
`account.secretary`（看板娘 charId）。B85 钉三条：后端 `list_accounts` 把
`secretary` 带出来（没有的账号给空对象，不是缺键）；前端侧栏 / 设置页 / 顶部
三处统一走 `acctAvatarUrl()` + `acctAvatar()`，按设置 `acctAvatar` 选来源
（`secretary` 走 `/api/avatar/<charId>`，默认 `game` 走 `/api/gasset/img?u=`），
图片规则必须带 `onerror="this.remove()"`（拿不到图就露出首字，不裂图）；
头像只认 `pack-data`，`packrepo` 不许再留 `AVATAR_BRANCH` / `avatar_urls` 这条岔路。

### B86 作业详情带简介与干员头像（2026-11-28）

弹窗原来只有「干员与要求」。B86 钉的是「字段有没有从头传到尾」：

- **简介**：`/api/op` 把 `doc.details` 原样放进 `desc`，换行不许被吃掉（前端靠
  `white-space: pre-wrap` 排版），也不截断——太长由前端按实际高度折叠；
- **charId**：`detail_opers` 从资源包索引里补 `charId` / `stars`，名字认不出
  （改名、异格没进包）时 charId 留空串，前端 `opAv` 自然退回首字底块，绝不瞎编；
- **前端**：简介块 `#op-desc` 默认 hidden，没简介就不占位；干员行改 `.op-row`
  + `.op-av`，边框用 `rgba(var(--tint), a)`，不许再写死白色（浅色主题会露黑线）。

打桩 `copilot.operation_detail` 返回假 content，不联网。

### B87 卫戍协议战绩（2026-11-28）

这一页的立论是「官方资源接口把图标 CDN 直链发下来」，所以整页的图一张都不进
安装包。B87 把立论和这轮踩的坑一起钉住：

- **图标白名单**：`gasset.normalize()` 只放行 `web.hycdn.cn` / `bbs.hycdn.cn`，
  `http://` 归一成 `https://`；`ftp://`、`evil.com`、`web.hycdn.cn.evil.com`、
  `127.0.0.1`、空串一律返回空串（这个 url 由前端传，不卡就是 SSRF）；
- **富文本清洗**：`<b>` / `<color=…>` 洗掉，`<战斗中>` 换成 `〈战斗中〉` 保留；
- **分队名 vs 技能名**：分队名是带队的干员 / NPC（「复奏」），「二重奏」是分队
  技能；两个字段反过，整排分队都会显示成技能名；
- **没生效的盟约不算常用**：`activate=0` 或 `layer=0` 的条目必须被排除，
  否则「最高 0」会混进常用盟约；
- **首领名**：资源没名字时按 `enemyId` 查 `BOSS_NAMES`，不许把
  `enemy_1521_dslily` 甩到界面上；
- **统计口径**：`total` / `completeRate` 按返回的对局算，生涯总数单独放
  `totalAll`（样例：2 场、50%、生涯 30 场）；
- **界面结构**：两个 tab（`data-garview`）+ `#gs-root`；详情干员卡必须有
  `.gs-dt-opers` 网格容器（漏了会一张卡撑满一行、立绘撑成方块）；精锐标记用
  内联 SVG（`GS_GOLDEN`）；
- **盟约绿底**：`#123a30` 至少三处，旧的黑底取值一个都不许留；近 10 场叠层的
  点也要铺绿底（`fill="#123a30"`），半透明底会把白色线稿吃掉；
- **叫法**：左栏是「常用分队」、详情是「分队与盟约」，不许再出现「常用策略」；
  详情里分队技能要单独标「技能：」；
- **证章不是模组**（2026-11-29 补）：样例里塞一场带 `uniequip_001_gravel` 的
  对局，`module` 必须是 `None`；资源里查不到的技能 / 模组要按 id 拼 CDN 图兜底
  （`/char_skill/sk_vonly.png`、`/uniequip/uniequip_002_unknown.png`），
  `chessSquad` 里夹 `null` 退成「未知干员」——原先 `None.get()` 直接让整页
  变成「拉取失败」；
- **切号**（2026-11-29 补）：`garPaintHead()` 必须存在且 `gsEnsure()` 开头就调
  （页头是速刷 / 战绩共用的，只知道在速刷那边刷，停在战绩切号名字不变）；
  `garStatBusy === want` 与 `S.garStatOf[want] = d` 钉住「请求期间切号不串号」；
  文案里必须有「这是接口数据的问题，不是 token」，不许再把非鉴权异常说成 token；

打桩 `accounts.token_for` / `get_account` / `garrisonstat.guide` / `resources` /
`_session`，不联网，也不写真缓存。


### B88 干员练度 + 账号头像设置（2026-11-29）

两块东西一起钉：

- **头像设置**：`settings.DEFAULTS['acctAvatar'] == 'game'`（默认游戏内）；
  `save({'acctAvatar': 'Secretary'})` 要归一成小写，写别的（`看板娘`）必须回
  `game`——写坏的值让界面认不出，头像会整片空掉；`list_accounts` 要把
  `account.avatar` 带出来，前端才有游戏内头像可用；
- **练度表**：桩掉 `accounts.get_account` 喂一份快照，查 `training.view()`：
  证章（`kind == '证章'`）不进模组列、模组按等级降序、技能 id 里的方括号转义成
  `%5B/%5D`（不转 CDN 直接 404）、`moduleCount` 只数真模组、默认序星级优先；
- **界面与接口**：`data-mod="training"` + `#mod-training` + `#btn-tr-refresh` +
  `#tr-search`；`/api/training` 与 `/api/training/refresh` 已注册；
  `/api/gasset/img` 通用代理路径存在；`acctAvatarUrl` 有 `secretary` 分支且
  `game` 是兜底；
- **样式**：`--mint-rgb` 在两个主题里都定义了，`rgba(var(--mint),` 这种非法写法
  一个都不许留（分页高亮曾经因此一直不生效）。
- **公共技能的等级后缀**（2026-11-29 补，用户报「部分图标丢失」）：喂一个技能 id
  是裸 `skcom_heal_up` 的快照 + 一个 `char_table` 里给 `skcom_heal_up[3]` 的桩，
  断言出来的 `id/icon/name` 都变成带后缀那份（`skcom_heal_up%5B3%5D.png`、
  「治疗强化·γ型」）；同时检查 `resources.char_table()` 里真有带 `[` 的 id、
  `make_pack.py` 里有生成 `chars[].skills` 的代码——只修一半（改了客户端没改
  资源包）照样满屏空格；
- **图标重试**：`trImgFail` 必须存在且挂在 `onerror` 上——直接 `remove()` 会把
  偶发超时变永久缺图。

全部打桩，不联网、不写真账号。
