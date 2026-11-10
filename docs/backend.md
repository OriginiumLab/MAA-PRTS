# backend —— 服务端

## 用途

一个进程同时干三件事：

1. `http.server` 起本地服务，托管 `frontend/` 并提供 `/api/*` 接口
2. 用 pywebview 开一个 WebView2 原生窗口指向它
3. 在后台线程里跑耗时任务（同步账号、写入 MAA），进度通过任务日志接口吐给前端

不依赖 Flask/Django，全部用标准库 + pywebview。

## 文件

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：找端口、起服务、开窗口、日志、原生桥 `Bridge` |
| `core/settings.py` | 路径解析（运行根、`config/`）、`settings.json` 读写、旧目录迁移 |
| `core/api.py` | 所有 `/api/*` 的实现与路由表，静态文件 MIME 表 |
| `core/accounts.py` | 读 `config/*.json` 账号文件、token 密文（skd 段）读取、后台同步 |
| `core/updater.py` | token → 账号配置的完整构建（练度、缺卡、统计、增量对比） |
| `core/operbox.py` | MAA 干员识别数据（`data/OperBoxData.json`）解析 → 账号练度导入 |
| `core/skland.py` | 森空岛鉴权链：token → grant → cred → 签名请求 → 绑定/角色数据 |
| `core/gacha.py` | 抽卡记录（明日方舟 / 终末地）：通行证 grant → 角色 token → 记录分页 → 本地增量缓存与统计 |
| `core/signin.py` | 森空岛每日签到（明日方舟 / 终末地）：两套接口口径，先查状态再签，全局 / 单号两种跑法 |
| `core/copilot.py` | 作业站接口 + 干员要求匹配算法 |
| `core/resources.py` | 读 `resources/arknights.json`：关卡索引、干员表、技能/模组名、活动正式名与代号前缀 |
| `core/version.py` | 版本管理：程序版本、内置资源包版本、资源更新检测与提示 |
| `core/packsync.py` | 资源包自动更新：读远端 manifest → 校验 sha256/条数 → 原子替换 |
| `core/appupdate.py` | 程序本体自动更新（GitHub Releases）：查版本 → 下载校验 → 退出后由 install.ps1 替换 |
| `core/maa.py` | MAA 进程控制、`gui.new.json` 读写、作业文件下载 |
| `core/tasks.py` | 内存任务表：状态、日志、结果，供前端轮询 |

## 启动流程

```
main()
 ├─ find_port()            8787 起，被占用就 +1（Windows 用独占绑定，防止双开抢端口）
 ├─ Thread: serve(port)    ThreadingHTTPServer，HTTP/1.1 长连接
 ├─ 建窗口 webview.create_window(js_api = Bridge())
 └─ webview.start()        窗口关闭后 _quit()：等后台任务收尾（最多 5 秒）→ shutdown → 退出
```

日志同时写 `debug/app.log` 和控制台，超过 2 MB 轮转为 `app.log.1`。

## 路径规则（重点）

`settings.py` 在导入时算一次，之后全进程复用：

| 用途 | 源码运行 | 打包运行 |
|---|---|---|
| 只读资源 | 代码所在仓库根（按 `__file__` 上溯） | `sys._MEIPASS`（PyInstaller 解包目录） |
| 可写数据 | `%LOCALAPPDATA%\MAA-PRTS\dev` | exe 所在目录 |

可写数据只认「数据根」，**任何模式都不向上探测**。打包运行取 exe 所在目录
（绿色软件：拷给别人数据跟 exe 走，和源码运行互不干扰）；源码运行落
`%LOCALAPPDATA%\MAA-PRTS\dev`——不落仓库，否则从仓库根一跑就在仓库根拉出
`config/debug`（2026-11-08 改；此前用启动时的工作目录）。设环境变量
`MAA_PRTS_DATA_ROOT` 可把源码模式的数据根指到别处（冒烟测试用它隔离），
打包模式忽略该变量。

## 接口一览

`api.py` 的 `ROUTES` 注册全部 `/api/*` 接口（`/api/ping` 在 `main.py` 里特判，
不在此表）。路由本身不分 GET/POST——同一处理函数两种方法都能进。表中的
方法按语义标注（POST = 需要带 body）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/bootstrap` | 一次性拉：设置、账号列表、MAA 状态、活动、路径、资源包信息、资源版本、首启标记 |
| POST | `/api/settings` | 保存设置（白名单 `maaDir` / `profile` / `autoStartMaa` / `lastAccount`） |
| GET | `/api/accounts` | 账号列表（含每个账号的统计摘要） |
| GET | `/api/account?file=` | 单个账号完整配置：account / stats / missing / chars |
| POST | `/api/account/maa` | 把 `maa.profile` 等用户字段写回账号 JSON（同步不覆盖的那几个） |
| POST | `/api/account/remove` | 删除账号：不真删，移入 `config/_removed/` 备份。**预留，无界面入口** |
| POST | `/api/account/refresh` | 同步账号。带 `token` 同步单个（密文写进账号文件 `skd` 段）；不带则按各账号文件 ∪ 旧 `tokens.json` 全部同步。返回 `taskId` |
| POST | `/api/account/import-maa` | 从账号绑定的 MAA 目录读干员识别结果（`done=false` 残缺名单拒绝），覆盖练度/统计/缺卡（账号信息与 `skd` 保留），同步任务直接返回摘要 |
| POST | `/api/account/create-from-maa` | 不用森空岛 token：用 `dir` 指定 MAA 目录的干员识别结果直接建本地账号（合成 uid、昵称取备注或目录名、无 `skd` 段、重名顺延 (2)(3)），返回建号摘要 |
| POST | `/api/search` | 搜作业 + 练度匹配（body：`q` / `file` / `pages` / `minRating` / `limit`） |
| GET | `/api/activities` | 近期活动列表。**预留，无界面入口**（前端从 bootstrap 拿活动数据） |
| GET | `/api/gamedata` | 资源包版本状态（含 `pending` 更新提示）。**预留，无界面入口**（前端从 bootstrap 拿，确认更新走 `/api/gamedata/ack`） |
| POST | `/api/gamedata/ack` | 清掉待提示的资源更新 |
| GET | `/api/pack/status` | 远端有没有新的资源包（只读；`?force=1` 给更长超时） |
| POST | `/api/pack/update` | 下载并安装新资源包，返回新的 `pack` 与 `gamedata` |
| GET | `/api/app/status` | 程序本体有没有新版本（只读；`?force=1` 跳过 6 小时节流） |
| POST | `/api/app/download` | 下载安装包并校验，**不替换**，等用户点「重启并安装」 |
| POST | `/api/app/install` | 解压 + 拉起 install.ps1 + 退出程序（源码模式直接拒绝） |
| POST | `/api/app/open` | 用系统浏览器打开 Release 页 |
| GET | `/api/op?id=` | 作业详情（干员与要求、动作数、`maa://` 代码） |
| GET | `/api/maa/status` | MAA 是否运行、配置列表、当前配置 |
| GET | `/api/maa/probe?dir=` | 检测目录是不是 MAA 根目录（引导/设置用）；附干员识别数据 `operbox/operboxCount/operboxSyncTime` |
| POST | `/api/maa/close` | 关闭正在运行的 MAA 进程。**预留，无界面入口**（`/api/maa/apply` 流程内部复用同函数） |
| POST | `/api/maa/open` | 启动 `MAA.exe`。**预留，无界面入口**（同上，apply 流程内部复用） |
| POST | `/api/maa/apply` | 写入作业集：关 MAA → 备份 → 下载 → 写 TaskList → 重启。返回 `taskId` |
| POST | `/api/maa/clear` | 清空某配置的 `Copilot.TaskList`。**预留，无界面入口** |
| GET | `/api/maa/tasklist?profile=` | 读 MAA 某配置当前的 `TaskList`。**预留，无界面入口** |
| GET | `/api/task?id=&since=` | 后台任务状态与增量日志（`since` 是日志游标） |
| GET | `/api/gacha?file=` | 抽卡统计视图：读本地缓存算汇总（总抽数/星级/出率/保底），**不发网络请求** |
| POST | `/api/gacha/sync` | 从森空岛拉抽卡记录并并入缓存（增量，按 `分类｜池｜干员｜时间戳｜pos` 去重）。返回 `taskId` |
| POST | `/api/signin/run` | 森空岛签到：`{file}` 为空 = 全部账号（方舟 + 终末地），传账号文件 = 只签这一个。返回 `taskId`（日志在前端面板里刷） |
| POST | `/api/export` | 导出「作业站本地导入」格式到 `config/作业站/`。**预留，无界面入口**（导出的只有干员练度，不含 token） |

所有响应统一为 `{"ok": true, "data": ...}` 或 `{"ok": false, "error": "..."}`。

### 防跨站（`main.py::trusted_request`）

接口无鉴权（本机桌面应用定位），但 `do_GET` / `do_POST` 入口会校验，不过关直接 403：

- **Host** 必须是 `127.0.0.1:<port>` / `localhost:<port>`——挡 DNS 重绑定；
- 带 **Origin** 时必须同源——挡 fetch / XHR / 表单 POST 类跨站请求；
- 带 **Sec-Fetch-Site** 时不得是 `cross-site`——挡 `<img>` / `<script>` / 顶层导航
  这类不带 Origin 的盲打（curl 和老客户端没有这个头，不拦）。

pywebview 窗口与浏览器直开都是同源，不受影响。

## 版本管理（`version.py`）

分两层，互不相干：

| | 来源 | 用途 |
|---|---|---|
| 程序版本 `app_version()` | 发布包根 `version.json`（发版 CI 按 PR 标题版号写入）；缺失即 dev 版 | 日志、设置页、请求头 `Server` |
| 资源版本 | `resources/arknights.json` 的 `version` + `fingerprint` | 判断游戏数据有没有变 |

资源包由 `make_pack.py` 生成时会写入 `version`（**游戏版本日期**，如 `2026.10.29`，
取自上游 `version` 文件）和 `fingerprint`（纯内容哈希：生成日期和本机学到的分支名都不参与，
同一份数据在哪台机器上生成都一样）。`version.check()` 把它和 `config/gamedata.json`
里记的「上次见到的版本」比对：

| 情况 | 行为 |
|---|---|
| 没有 `gamedata.json`（首次运行） | 静默记下当前版本，不提示 |
| 版本与指纹都一致 | 什么都不做 |
| 不一致 | 写一条 `pending`：新增干员/关卡数 + 新活动列表，前端顶部出现提示条 |

`pending` 会一直留着，直到用户点「知道了」（`/api/gamedata/ack`）——
不点就每次启动都提醒一次，避免用户错过。

资源包装载顺序是「项目目录 `resources/arknights.json` → 程序内置那份」，
所以想更新游戏数据，把新的 `arknights.json` 覆盖到项目 `resources/` 里就行，
不用重新打包；下次启动就会看到更新提示，新活动也会排到快捷栏最前面。

### 资源包自动更新（`packsync.py`）

启动约 2.5 秒后前端调 `/api/pack/status`：读 `pack-data` 分支上的 `pack-manifest.json`，
拿 `fingerprint` 和本地那份比；不一样就 `/api/pack/update` 下载、校验、替换。
**检查失败一律静默**——没网也要能正常用旧包。

| 环节 | 规则 |
|---|---|
| 清单源 | `raw.githubusercontent` → `gh-proxy` → `jsDelivr`，逐个回退（都指向 `pack-data` 分支） |
| 校验 | 字节数 + `sha256` 必须和 manifest 一致；不是合法 json、缺 `chars/stages` 直接拒 |
| 防退化 | 各表条数不得比本地少（半截包会把数据清零）；悖论条数同理 |
| 落盘 | 写 `<运行根>/resources/*.json`，旧文件留 `.bak`，`os.replace` 原子替换 |
| 生效 | `arknights.json` → `resources.clear_cache()`；`paradox.json` 本就按 mtime 重读 |
| 源码模式 | 默认不覆盖（免得弄脏工作区），`MAA_PRTS_PACK_FORCE=1` 可强制 |

分支名可用环境变量覆盖：`MAA_PRTS_PACK_REPO` / `MAA_PRTS_PACK_BRANCH`（默认
`OriginiumLab/MAA-PRTS` + `pack-data`），本地模拟远端时用得上。

上游那半边在 GitHub Actions（`.github/workflows/pack.yml`，每天北京时间 12:00 / 16:00）：
取上游 commit sha → 和包里的 `sourceCommit` 比 → 变了才按 sha 下载 9 个 json →
重新生成 → 条数校验 → 推到 `pack-data` 分支（单提交强推）。
**不推 `main`**：`main` 的规则集强制 PR + 审批，bot 不在绕过名单里。

### 程序本体自动更新（`appupdate.py`）

和资源包是两条完全独立的链路：资源包 0.6 MB、静默换掉；程序 13 MB、
**必须用户自己点重启**。启动 4 秒后查一次，成功检查后 6 小时内不再自动查。

| 环节 | 规则 |
|---|---|
| 查版本 | `HEAD github.com/.../releases/latest` 读 302 的 `Location` 拿 tag，**不消耗 API 配额** |
| 拿详情 | 版本确实更新了才请求 `api.github.com/.../releases/latest`（changelog + `assets[].digest`） |
| 降级 | API 不可用时仍提示版本号，但只给「前往下载页」，不给安装按钮（拿不到校验值） |
| 版本比较 | 按数字比（`2.10 > 2.9`）；预发布 `-beta.N` 永远小于同号正式；解析失败一律不触发 |
| 下载 | `github.com` 直链 → `gh-proxy` → `ghfast`，校验字节数 + sha256 + zip 内含 `MAA-PRTS.exe` |
| 解压 | 拒绝绝对路径与 `..`（zip-slip），落到 `<运行根>/update/stage/` |
| 替换 | 程序退出后由 `update/install.ps1` 执行：按 PID 等进程退出 → 备份 exe → `robocopy /MIR` 镜像 `_internal` → 覆盖根级文件 → 校验版本号 → 重启，失败则回滚并重新拉起旧版 |
| 不动的东西 | `config/`、`debug/`、`resources/`——那是用户数据，脚本里一个字都不出现 |
| 结算 | 写 `update/pending.json`；新版本启动时版本号对上才算成功，否则日志留警告 |

`install()` 需要让整个进程退出，而 api 层不能 import main，所以 main 启动时
把退出函数注册进 `appupdate.set_quit_hook()`。源码模式一律拒绝自动替换。

## 匹配算法（`copilot.py`）

逐份作业、逐个干员比对，判定分四档：

| 结果 | 含义 |
|---|---|
| `ready` | 干员都有、练度都够，可以直接抄 |
| `borrow` | 只差 1 个干员（可借），其余满足 |
| `train` | 人齐但练度/专精/模组不够 |
| `blocked` | 缺 2 人以上，或超过 13 人队伍上限 |

练度不按「等级数值」直接比，而是折算成**进度分**：先用干员星级 + 等级推断其应有的精英阶段，
再按 `精二 / 精一 / 精零` 分段加权，避免「精一 70 级」被误判为高于「精二 1 级」。

### 没有技能数据的账号只比等级

MAA 干员识别（`operbox.py`）拿不到技能等级、专精、模组，导入后的账号这三项是空的。
如果照实判「不满足」，这类账号搜出来的作业会被全部判成 `train` 再被过滤掉，等于不可用。
所以 `build_owned()` 给每个干员标 `skillKnown`（`skills` 数组是否为空），`req_met()` 遇到
`skillKnown=False` 时**只比精英化与等级，技能与模组一律当已拥有**：

| 作业 | 有技能数据的账号 | 没有技能数据的账号 |
|---|---|---|
| v1（无 `requirements`） | 按星级补默认练度 **+ 技能**（六星专三） | 只补默认**等级**，不提技能 |
| v2（写了 `requirements`） | `elite`/`level`/`skill_level`/`module` 全判 | 只判 `elite`/`level` |

这是有意识的妥协：可能推出实际打不了的作业，界面上（导入确认框、建号日志、作业详情）
都会说明「技能模组按已拥有处理」，让用户自己去 MAA 里确认。`source=yituliu` 的识别数据
带技能与模组，不受影响，仍走严格判定。

两个容易写错的地方，代码里有注释：

- **技能序号在作业条目顶层**（`op.skill`），不在 `requirements` 里。取错会让所有「专三二技能」
  的作业被判成练度不足。
- **作业站 `levelKeyword` 对部分关卡不生效**（例如 `tr_04` 会返回全站热门作业），
  所以每份作业都要校验 `content.stage_name`，不匹配时改用 `document=<关卡代号>` 重搜。

## MAA 写入流程（`maa.py`）

```
taskkill MAA.exe
  → 备份 config/gui.new.json.bak-app-<时间戳>（只留最近 2 份，多余的自动清掉）
  → 下载作业 JSON 到 config/copilot/<关卡>_<id>.json
  → 写 Configurations.<配置名>.Copilot.TaskList
      Name = 关卡代号（悖论模拟模式用干员名，此处是关卡）
      CopilotId / FilePath / IsChecked / Index
  → 启动 MAA.exe（可在界面关掉）
```

`_prune_backups()` 只清理本程序生成的 `.bak-app-<YYYYMMDD-HHMMSS>`，
MAA 自己和用户手写的 `.bak` / `.bak-auto` / `.bak-before-xxx` 一律不碰。

MAA 运行时会周期性回写配置文件，所以必须先关进程再改，否则改动会被覆盖。

## 抽卡记录（`gacha.py`）

两条游戏共用同一套通行证，只有「角色 token 之后的最后一段」不一样：

```
森空岛 token
  └─ grant  POST as.hypergryph.com/user/oauth2/v2/grant
            {appCode: be36d44aa36bfb5b, token, type: 1}   ← 两游戏共用，写 endfield 会 400
      ├─ binding_list?token=<grant>&appCode=arknights|endfield   → 角色 uid
      └─ u8_token_by_uid {uid, token: grant}                     → 角色 token（u8）

明日方舟：role/login → Set-Cookie ak-user-center
          → gacha/cate?uid=            分类是动态的，只能查（限定/标准/中坚…）
          → gacha/history?uid=&category=&size=&pos=&gachaTs=
          请求头 X-Role-Token + Cookie，两样都要

终末地：  ef-webview.hypergryph.com/api/record/char?token=&lang=zh-cn&server_id=1&pool_type=&seq_id=
          record/weapon?token=&lang=zh-cn&server_id=1&seq_id=      武器申领（不吃 pool_type）
```

踩过的坑：

- `gachaTs` 是 **13 位毫秒字符串**，分页游标要 `gachaTs` + `pos` 一起带；
  `hasMore=false` 才是到底
- 明日方舟 `size` 只认 1~100，传 500 不报错、直接返回空列表（静默截断）
- 明日方舟 `rarity` 0 基（3 = 四星）；**终末地是 1 基**（3=三星 … 6=六星），
  实测分布只出现 4/5/6，套明日方舟那套 +1 会把六星顶成七星——两套归一分开写
- 终末地必须用绑定的 **uid** 调 `u8_token_by_uid`，传 `roleId` 报 109「角色不存在」
- 记录只保留最近一段窗口，所以缓存要**只增不减**：每次按
  `分类｜池｜干员｜时间戳｜pos` 合并，旧记录留在本地

### 终末地分页（2026-11 实测）

- **必须传 `seq_id`**：不带游标时接口永远只返回最新 5 条，`hasMore` 还是 `true`；
  `size` / `limit` / `pageSize` / `page` / `cursor` 全部被忽略，每页固定 5 条
- `seq_id` 的语义是「取 seqId **小于**它的最近 5 条」，所以要拿上一页最后一条的
  `seqId` 当下一页游标一路往前翻
- 角色池枚举只认 5 个（Standard / Special / Beginner / Joint / Rerun），
  其它值一律 `40000 Invalid pool_type`；庆典池之类的记录归在其中某一类里
- 武器申领走 `/api/record/weapon`，**不吃 `pool_type`**（传什么都返回同一批），
  记录序列空间与角色独立（`seqId` 各自计数），字段是 `weaponId` / `weaponName`
- 非抽卡记录要过滤：`kind` 除 `draw` 之外还有 `gift_intel_book` 这种赠礼，
  `rarity` 为空，混进统计会把出率算歪
- 接口只保留最近窗口（实测老号：角色 400+ 抽、武器 160 抽），更早的拿不到。
  增量同步用本地已存的最大 `seqId` 当断点（`stop_below`），没新抽卡时 6 个请求就回来

### 统计口径

- 明日方舟：全部记录一套口径
- 终末地：**角色寻访与武器申领分开算**（角色六星约 1.9%、武器约 5%，混在一起
  两头都不对）。`summary.char` / `summary.weapon` 各是一份**完整**汇总（卡片数字、
  星级分布、六星时间线、按卡池、最近记录都在里面），界面按两个 tab 分别整块渲染；
  顶层 `total` / `sixCount` / `rate6` 等仍是两者之和，给账号卡这种只要一个总数的地方用
- `summary.weapon` 在没有任何武器记录时是 `null`（前端据此判断要不要给武器池 tab）
- 终末地记录没有 `pos` 字段，拿 `seqId` 当去重键里的「次序」（存进 `pos`）

缓存落在 `config/gacha/<账号文件名>.json`（不碰账号 json）。统计口径：
保底按**池子 id** 连续计数、出六星清零；展示按**池名**合并——「标准寻访」
在记录里会拆成好几期，按 id 分开列等于同一名字刷屏。
卡池按**最近一次抽卡时间倒序**排——正在抽/刚抽完的池子永远在最上面（角色池、
武器池同一口径），同一分钟再按抽数多的在前，没有时间的池子垫底
（`lastMs` 只用于排序，返回给前端前已 pop 掉）。

## 森空岛签到（`signin.py`）

和抽卡一样走 `core/skland.py` 的鉴权链（token → grant → cred → 签名），
但**两个游戏的签到接口不是同一个**：

```
明日方舟  GET  /api/v1/game/attendance?uid=<uid>&gameId=1     查今天签没签
          POST /api/v1/game/attendance  {"uid": "...", "gameId": 1}

终末地    GET  /api/v1/game/endfield/attendance                 查 hasToday
          POST /api/v1/game/endfield/attendance   （没有请求体）
                 header  sk-game-role: 3_<roleId>_<serverId>   ← 身份全在头里
```

踩过的坑：

- 终末地**不吃 uid**：拿 uid 去打 `/api/v1/game/attendance` 一律 **HTTP 403**，
  必须换成 `/game/endfield/attendance` + `sk-game-role`（`gameId_roleId_serverId`，
  gameId 就是 binding_list 里给的 3）；签名串里的 body 段是空的
- 今天签过之后再 POST 返回的是 **HTTP 403**，不是 `code=10001` 这类业务错误。
  所以先查状态（方舟看 `records[].ts` 有没有今天的日期、终末地看 `data.hasToday`）
  再决定签不签；真撞上 403 记成「今天已签到」，别当失败吓人
- 签到链的 403 不重试（`retries=1`）：重试只是多喂几次风控
- 日期按**上海时区**比，不是「24 小时内」——早上 5 点签完，前一天签的还在窗口里
- 一个 token 可能绑多个角色（官服 + B 服、方舟 + 终末地），按账号文件逐个签，
  同一 `(游戏, uid)` 只签一次；同一个 token 只登录一次（坏的 token 也只撞一次）

两种跑法（`run(file)`）：

| 入口 | 范围 | 日志 |
|---|---|---|
| 全局签到 | 账号列表里所有账号，方舟 + 终末地一起签 | 每个号一行，最后一行汇总「成功 / 已签到 / 失败 / 跳过」 |
| 单号签到 | 只签当前选中的这个账号（用它自己的 token） | 同一格式，只有这一个号 |

跳过的三种情况：MAA 导入的账号没有 token、token 跨机器解不开、终末地账号缺 `roleId`/`serverId`
（老版本建的终末地账号没有这两个字段，重新同步一次账号即可）。
签到结果不落盘（只活在任务的日志里）——这是每天本来就该点一次的操作，不存历史反而省得纠结。

## 依赖

- Python 3.10+
- `pywebview`（自带 pythonnet + WebView2；WebView2 Runtime 是 Win10/11 系统组件）
- 打包用 `pyinstaller`

其余全部是标准库。
