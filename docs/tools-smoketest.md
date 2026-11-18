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
| B | DPAPI 加解密往返、密文篡改必报错；token 密文随账号文件 `skd` 段（跨文件去重、备注保留、不进 API 视图、坏条目逐个跳过点名警告）；旧 `tokens.txt` 三种写法自动迁移；旧布局 `config/skland` 压平迁移（同名跳过、搬空才删、幂等）；MAA 干员识别 JSON（新 JObject / 旧裸数组）解析成练度结构；悖论清单与解锁推算（B10）；从 MAA 直接建号/无 token（B15）；账号手动排序 accountOrder（B16）；作业筛选（v1 默认练度、MAA 导入只比等级、剧情关不进搜索）；主线难度变体（简单/标准/磨难/险地、`#s` 独立成行、下一关推进）；关卡流程顺序（10-14 章系列视图带简单行、TR 与主线 S 关按 `unlockCondition` 插到前置关后面、`TR-S-1` 链式锚点、搜章节号返回整章含 S/TR、`TR_AFTER`/`SUB_AFTER` 改完清缓存即生效）；作业黑名单（`maa://` 代码归一化 + 出口过滤，B23）；推荐排序分档（可直接抄 9 分以上才压过借人作业）与作业详情缺练度/缺人（B28）；抽卡统计（星级归一 0 基/1 基兼容、保底按池计数、同名池合并、缓存视图、终末地快照、跨游戏 uid 隔离，B24）；森空岛签到（方舟 uid+gameId 与终末地 `sk-game-role` 两套口径、今天已签就不发 POST、403 记成已签到、缺 token/roleId 跳过、坏 token 只登录一次、同角色去重，B25）；资源包幂等（`version` 取游戏版本、指纹只看内容）与换包校验（sha256 / 条数不许变少）；程序自更新（`2.10 > 2.9`、预发布小于同号正式、脏 tag 不触发；安装包 sha256/体积/缺 exe 全拒、zip-slip 拦截；安装脚本只碰 `MAA-PRTS.exe` 与 `_internal`，绝不出现 `config/`、`debug/`、`resources/`）；日志观测（B30：增量读只算新行、文件被轮转能重读、`act55side_s03` 这类事件能还原成「跑到第几关/节点/重试几次」，200 秒没动静判停滞并给 hint、链跑完判 done、认不出的行一律跳过不抛）；多账号编排（B31：`1815505` → `181****5505`、档案名撞了加序号、一账号一档案且 `AccountName` 写进 StartUpTask、模板档案不被改动、`set_tasklist` 索引重排）；助手与 CLI（B32：工具表与实现一一对应、未知工具/坏参数兜成 error、autoApply 关时写操作只返 need_confirm、`apiKey` 绝不回显、CLI 输出形状与退出码 0/1/2/3，缺参数回 2 且带 hint）；活动关导航（B34：代号→区域行号 `TL-S-1`→第 3 区、`TL-EX-1`→第 2 区、`TL-5`→第 1 区，主线关 `1-7`/`S4-1`/`R8-2` 与作业内部 id 一律返回不导航；`probe()` 造图钉阈值——底部全暗判活动地图、半白判主界面、大半白+中部亮判主题页、中部成片纯白判活动商店；主线关调 `goto_stage_list` 必须零点击，绝不能对主线关乱点活动入口）；设置页接口（B38：token 三态读写、掩码只留尾 4 位、切号名归一与空串放行、bootstrap 带 logs） |
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

## 什么时候跑

- 改了 `vault.py` / `accounts.py` / `settings.py` / `main.py` 之后
- 改了打包、启动脚本或 `.gitignore` 之后（A 组和 C 组的目录零污染检查）
- 发版前过一遍

依赖只用到程序自身的运行环境（B/C 不会联网调森空岛接口），用 `.venv` 的
Python 跑即可；C 组会真实调用 Windows DPAPI，换电脑/换用户跑 B 组是
「加密绑定本机」的活体验证。
