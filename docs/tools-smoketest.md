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
| B | DPAPI 加解密往返、密文篡改必报错；token 密文随账号文件 `skd` 段（跨文件去重、备注保留、不进 API 视图、坏条目逐个跳过点名警告）；旧 `tokens.txt` 三种写法自动迁移；旧布局 `config/skland` 压平迁移（同名跳过、搬空才删、幂等）；MAA 干员识别 JSON（新 JObject / 旧裸数组）解析成练度结构；悖论清单与解锁推算（B10）；从 MAA 直接建号/无 token（B15）；账号手动排序 accountOrder（B16）；作业筛选（v1 默认练度、MAA 导入只比等级、剧情关不进搜索）；主线难度变体（简单/标准/磨难/险地、`#s` 独立成行、下一关推进）；关卡流程顺序（10-14 章系列视图带简单行、TR 与主线 S 关按 `unlockCondition` 插到前置关后面、`TR-S-1` 链式锚点、搜章节号返回整章含 S/TR、`TR_AFTER`/`SUB_AFTER` 改完清缓存即生效）；作业黑名单（`maa://` 代码归一化 + 出口过滤，B23）；推荐排序分档（可直接抄 9 分以上才压过借人作业）与作业详情缺练度/缺人（B28）；抽卡统计（星级归一 0 基/1 基兼容、保底按池计数、同名池合并、缓存视图、终末地快照、跨游戏 uid 隔离，B24）；森空岛签到（方舟 uid+gameId 与终末地 `sk-game-role` 两套口径、今天已签就不发 POST、403 记成已签到、缺 token/roleId 跳过、坏 token 只登录一次、同角色去重，B25）；资源包幂等（`version` 取游戏版本、指纹只看内容）与换包校验（sha256 / 条数不许变少）；程序自更新（`2.10 > 2.9`、预发布小于同号正式、脏 tag 不触发；安装包 sha256/体积/缺 exe 全拒、zip-slip 拦截；安装脚本只碰 `MAA-PRTS.exe` 与 `_internal`，绝不出现 `config/`、`debug/`、`resources/`）；日志观测（B30：增量读只算新行、文件被轮转能重读、`act55side_s03` 这类事件能还原成「跑到第几关/节点/重试几次」，200 秒没动静判停滞并给 hint、链跑完判 done、认不出的行一律跳过不抛）；多账号编排（B31：`1815505` → `181****5505`、档案名撞了加序号、一账号一档案且 `AccountName` 写进 StartUpTask、模板档案不被改动、`set_tasklist` 索引重排）；助手与 CLI（B32：工具表与实现一一对应、未知工具/坏参数兜成 error、autoApply 关时写操作只返 need_confirm、`apiKey` 绝不回显、CLI 输出形状与退出码 0/1/2/3，缺参数回 2 且带 hint）；活动关导航（B34：代号→区域行号 `TL-S-1`→第 3 区、`TL-EX-1`→第 2 区、`TL-5`→第 1 区，主线关 `1-7`/`S4-1`/`R8-2` 与作业内部 id 一律返回不导航；`probe()` 造图钉阈值——底部全暗判活动地图、半白判主界面、大半白+中部亮判主题页、中部成片纯白判活动商店；主线关调 `goto_stage_list` 必须零点击，绝不能对主线关乱点活动入口）；设置页接口（B38：token 三态读写、掩码只留尾 4 位、切号名归一与空串放行、bootstrap 带 logs）；培养干员（B39：专精绝对等级的多种写法互通、已达标项剔除、材料不足暂停并把剩余计划落盘续跑、MAA 日志桥去噪、战斗过程有进度日志、`SubTaskStart` 按真实 asst.log 的 first[0] 对账到中文阶段）；多 MAA 多模拟器编排（B52：账号自己那份 MAA 目录/档案优先、默认账号=左侧选中、按「MAA 目录 + adb 地址」分组、同组串行跨组并行、`run_steps` 步骤解析、`claim` 免确认且不在写处理器里、`skill_claim`/`run_steps` 都进白名单）；多开三处硬伤（B53：分区直点不走老路径、profile/adbAddress 真的传进 MaaCore、点名设备不许自动换台、任务日志落盘、组线程异常留痕）；导航从任意页起步（B54：活动提示条定点比对、不在主界面不许点坐标、已在地图不绕路、probe_bytes 与 probe 同源、粗扫留前 3 名后整屏匹配不退化）；右栏日志按账号分栏（B55：`_public` 白名单不带文件句柄、任务 `owners` 精确过滤、`tasks._decorate` 给多开子进程每行补 `[昵称]`、`group_tag` 单号组取昵称、worker 命令与 main 入口分支都在）；页面识别与日志样式（B56：主页=齿轮+邮件双图标、基建确认框双圆钮定位（一整块红色必须判否）、结算页两颗星识别 + 必须先离开结算页才能重挂、to_home 不在主页盲按、多账号 fight 默认走导航、`@skill` 注册表派生、MAA 风格「选择干员/当前步骤/每关完成」+ 子任务回调码钉在 20000 段/老 70000 段也认/子任务失败翻人话/练度修正不再进 notes、翻车重挂只换翻车那关且没候选时同一份再试） |
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
- **结算页（`is_result_bytes`）**：把 `result_star1` / `result_star2` 按相对
  坐标贴回 1920×1080 → 认得出；只贴一颗星、主页、深色关卡列表必须判否。
  `_leave_result` 给「结算页帧 → 深色地图帧」，必须点一次就放行并打
  「结算页已点掉 / 已回到关卡列表」；本来就给关卡列表帧时一次都不许点


## 什么时候跑

- 改了 `vault.py` / `accounts.py` / `settings.py` / `main.py` 之后
- 改了打包、启动脚本或 `.gitignore` 之后（A 组和 C 组的目录零污染检查）
- 发版前过一遍

依赖只用到程序自身的运行环境（B/C 不会联网调森空岛接口），用 `.venv` 的
Python 跑即可；C 组会真实调用 Windows DPAPI，换电脑/换用户跑 B 组是
「加密绑定本机」的活体验证。
