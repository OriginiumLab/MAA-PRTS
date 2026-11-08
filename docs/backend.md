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
| `core/copilot.py` | 作业站接口 + 干员要求匹配算法 |
| `core/resources.py` | 读 `resources/arknights.json`：关卡索引、干员表、技能/模组名、活动正式名与代号前缀 |
| `core/version.py` | 版本管理：程序版本、内置资源包版本、资源更新检测与提示 |
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
| 只读资源 | 运行根 | `sys._MEIPASS`（PyInstaller 解包目录） |
| 可写数据 | 运行根 | 运行根（exe 所在目录） |

「运行根」＝程序在哪个目录运行，数据就落在哪个目录，**任何模式都不向上探测**：
源码运行取启动时的工作目录（`run.bat` 已先 `cd` 到仓库根），打包运行取 exe 所在目录。
所以在仓库里跑 exe，数据写在 `build/MAA-PRTS/` 自己的 `config/` 里，和源码运行互不
干扰；拷给别人也一样，数据始终跟 exe 走。程序**不会**写运行根以外的地方。

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
| POST | `/api/account/remove` | 删除账号：不真删，移入 `config/_removed/` 备份 |
| POST | `/api/account/refresh` | 同步账号。带 `token` 同步单个（密文写进账号文件 `skd` 段）；不带则按各账号文件 ∪ 旧 `tokens.json` 全部同步。返回 `taskId` |
| POST | `/api/account/import-maa` | 从账号绑定的 MAA 目录读干员识别结果（`done=false` 残缺名单拒绝），覆盖练度/统计/缺卡（账号信息与 `skd` 保留），同步任务直接返回摘要 |
| POST | `/api/account/create-from-maa` | 不用森空岛 token：用 `dir` 指定 MAA 目录的干员识别结果直接建本地账号（合成 uid、昵称取备注或目录名、无 `skd` 段、重名顺延 (2)(3)），返回建号摘要 |
| POST | `/api/search` | 搜作业 + 练度匹配（body：`q` / `file` / `pages` / `minRating` / `limit`） |
| GET | `/api/activities` | 近期活动列表（快捷栏数据源） |
| GET | `/api/gamedata` | 资源包版本状态（含 `pending` 更新提示） |
| POST | `/api/gamedata/ack` | 清掉待提示的资源更新 |
| GET | `/api/op?id=` | 作业详情（干员与要求、动作数、`maa://` 代码） |
| GET | `/api/maa/status` | MAA 是否运行、配置列表、当前配置 |
| GET | `/api/maa/probe?dir=` | 检测目录是不是 MAA 根目录（引导/设置用）；附干员识别数据 `operbox/operboxCount/operboxSyncTime` |
| POST | `/api/maa/close` | 关闭正在运行的 MAA 进程 |
| POST | `/api/maa/open` | 启动 `MAA.exe` |
| POST | `/api/maa/apply` | 写入作业集：关 MAA → 备份 → 下载 → 写 TaskList → 重启。返回 `taskId` |
| POST | `/api/maa/clear` | 清空某配置的 `Copilot.TaskList` |
| GET | `/api/maa/tasklist?profile=` | 读 MAA 某配置当前的 `TaskList` |
| GET | `/api/task?id=&since=` | 后台任务状态与增量日志（`since` 是日志游标） |
| POST | `/api/export` | 导出「作业站本地导入」格式到 `config/作业站/` |

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

资源包由 `make_pack.py` 生成时会写入 `version`（生成日期，如 `2026.11.05`）和
`fingerprint`（内容指纹，同一天多次生成也能区分）。`version.check()` 把它和
`config/gamedata.json` 里记的「上次见到的版本」比对：

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

## 依赖

- Python 3.10+
- `pywebview`（自带 pythonnet + WebView2；WebView2 Runtime 是 Win10/11 系统组件）
- 打包用 `pyinstaller`

其余全部是标准库。
