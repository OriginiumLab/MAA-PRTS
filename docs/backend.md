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
| `core/resources.py` | 读 `resources/arknights.json`：关卡索引（战斗关默认视图 / `include_story=True` 全量）、干员表、技能/模组名、活动正式名与代号前缀 |
| `core/version.py` | 版本管理：程序版本、内置资源包版本、资源更新检测与提示 |
| `core/packsync.py` | 资源包自动更新：读远端 manifest → 校验 sha256/条数 → 原子替换 |
| `core/appupdate.py` | 程序本体自动更新（GitHub Releases）：查版本 → 下载校验 → 退出后由 install.ps1 替换 |
| `core/maa.py` | MAA 进程控制、`gui.new.json` 读写、作业文件下载 |
| `core/tasks.py` | 内存任务表：状态、日志、结果，供前端轮询 |
| `core/progress.py` | 森空岛 `player/info` → 关卡进度：主线推到哪（含剧情关落点）、后续关卡、活动 zone 已打/总数、剿灭最高击杀、日常周常。只做 zone 级余量判断，不拿计数切关卡列表（两边口径含剧情关/突袭关，会错位） |
| `core/asstlog.py` | MAA 日志观测：`debug/asst.log` 的回调 JSON + `debug/gui.log` 的 `Start/Completed Task Chain` → 结构化事件流；增量读、轮转识别、状态推导（idle/running/stalled/failed/done）、`failures()` 还原「跑过哪几关、哪几关没跑完」 |
| `core/watch.py` | 自愈层：后台线程盯 `asstlog` 的状态，判停滞就关 MAA → 从当前关续写作业列表 → 重启。同一关连续 3 次救不回来就停手报给用户 |
| `core/acctname.py` | 切号名（MAA `AccountName`）的归一化与校验，**唯一一份规则**。MAA 是拿它和登录界面那行 OCR 做子串匹配，所以 `5505` / `136` / `23****45` / `181****5505` 都合法；用户给全号、带横线、全角数字、星号数量不对都归一成 `前3****后4`。官服只拦「填了昵称」（拿昵称切号会死循环几十次然后失败），B 服不拦 |
| `core/multiacct.py` | 多账号编排：MAA「一个配置档案 = 一个账号」，按各账号练度各挑作业，串行切换执行。档案一律新建 `PRTS-` 前缀的那份，用户自己的档案一个字段都不动。切号名统一走 `acctname.check()`；`maaProfile` 是档案名不是切号名，别混用 |
| `core/maaplan.py` | 把「用户想怎么挂」翻成可执行的东西，两条路线：① `build_queue`/`ensure_profile` 写 GUI 的 `TaskQueue` + `RunDirectly`（能看界面，但**不能抄作业**：MAA 的 `InitializeItems` 不认 CopilotTask，会抛 NotImplementedException 让整个任务列表空白）；② `core_tasks`/`run_core` 生成 MaaCore 的任务序列直接驱动（能抄作业、能跑 Custom 小活动、自己按开始）。`run_core` 跑抄作业固定四段：**登录（StartUp）→ `AsstStop` 把 MAA 停住 → 用 `stagenav` 点到关卡地图 → 再 append Copilot + `AsstStart`**。停这一步不能省：不停的话 MaaCore 还在按自己的逻辑截屏/滑动，和我们的 adb 点击抢界面。导航失败且这一关有卡片模板（老故事集）时**直接报错**，不把作业交给 MAA——它只会横向滑主线，白跑 30 秒再失败（2026-11-17 SV 实测：停住后 24 秒内 MAA 就在地图上 OCR 到 SV-1、点开始行动、自动编队开打） |
| `core/maacore.py` | MaaCore 直连：`AsstCreateEx` → `AsstLoadResource` → `AsstConnect` → `AsstAppendTask` → `AsstStart`，回调翻成事件流。三个坑写在文件头：`AsstCreateEx` 的 restype 必须是 `c_void_p`（否则 64 位句柄截断成 32 位，每次 append 都 access violation）、Copilot 的 `filename` 必须绝对路径、`copilot_list` 的键是 `filename` 不是 `file_name`（写错会让进程 C++ 异常崩溃） |
| `core/stagenav.py` | 把界面带到活动关的关卡列表。**MAA 自己做不到**：`TL-Open`/`TLChapterToTL` 那套模板图在当前活动 UI 上全匹配失败，Copilot 的 `FullStageNavigation` 又只会横向滑动切章节（这版游戏改成点底部 EPISODE 了）。这条路是真机一步步点出来的：底部「终端」tab → 主界面活动大图 → 主题页区域名 → 活动地图区域列表第 N 行。两个坑：① 主界面点大图是**记忆位置**的，所以流程按 `probe()` 判出的页面状态收敛，不是死按顺序点；② `AsstAsyncClick` 一次连接只能点中第一下，所以点击/截图都走 adb（`Touch`）。坐标 1280×720 口径，可用 `config/nav.json` 覆盖 |
| `core/stagenav.py`（老故事集） | 当期活动之外的第二条路：**曲谱 → 乐章收录 → 网格找卡片 → 进入活动 →（EX 关再点地图右下角圆按钮）**。老活动 MAA 资源里压根没有导航配置（`Fight` 直接拒收），只能从「曲谱」这个统一索引里进。网格里卡片位置跟滚动位置绑定、新活动还会把顺序挤动，所以**按模板图找**（`navtpl/<代号>.png`，46 张随包发布，覆盖全部有战斗关的常驻活动；代号是 2026-11-17 真机把 48 张卡逐个导航进图、读落图关卡前缀核出来的，易错对照见 `stagenav.EVENTS` 注释）。分发双闸：模板存在**或**代号在 `EVENTS` 表里都走这条路；`EVENTS` 里的活动导航失败会**直接报错停止**，绝不交给 MAA——它只会横向滑主线，白跑 30 秒再失败。匹配是「1/4 降采样粗扫 + 全分辨率两轮精修 + 全程只记最像那一屏」，粗扫明显不像（>32 分）就跳过精修：真卡 0~7 分、无关卡 21 分起，**阈值 12**（暗色模板在相邻暗卡上能拿 14~19 分，18 挡不住）。五个坑：① 主界面有两套视图，不带底部 tab 的那套要先点中央「终端」大卡（`at_home()` 用底部功能格白像素判，主界面 0.45 / 其它 ≤0.07；带白色 EX 徽章的活动地图上白像素也会超 0.2，所以回主界面的判据是 `at_home AND not in_story_page AND not in_story_dialog AND not on_login_page` 四条件——后两个是 2026-11-17 补的：明亮沙地 CG 回放连 `at_home` 都能骗过，登录页浅灰底 + 白 logo 也能骗过）；② **扫描步长别贪快**：整张表（主线 + 各 Story Line + 未归组的新活动）11 屏，`_SWIPE_STEP` 360 配 1.4 秒等待刚好一张不漏；560 那版实测会漏屏——玛莉娅·临光（MN）正好落在两张截图之间，翻完 34 屏报「没找到」，用 360 重扫第 5 屏 1.0 分命中；③ **`in_story_page()` 必须两条同时成立**（底部左 tab 白 <0.05 且左上灰度 <90）：只看灰度会把主界面判成曲谱页（皮肤立绘压在左边，灰度 98.5 卡在旧阈值 100 下面），于是登录后一步没走就开始翻屏，报「翻了几十屏没找到」；④ **滑动起点必须在最左边缘 x=20 的空白处**，压到卡片上会被判成点击、直接跳进那个活动的详情页，之后分数一动不动（`goto_story_activity` 里躺过一次）；⑤ 退回上一层**先按返回键、再按 ESC**（有些详情页对 `keyevent 4` 完全没反应，ESC 一下就退）；⑥ **剧情页判定换了三版才站住**：白像素占比会把主界面右上角的货币数字（合成玉/342）当成「跳过▶」，于是「剧情排水」在主界面按返回键弹「退出游戏」又点中确认——**游戏被自己关掉**；灰度模板又把 TG 的亮天空烤进了模板，暗背景剧情全失配（而且模板匹配锚点一度把「中心」当「左上角」，全体错位）；最终用**字形掩码 XOR**（两边 >235 二值化后比对差异占比，剧情页 0.00~0.14 / 非剧情 ≥1.0，阈值 0.55），背景无关且天然排除登录页；⑦ **登录页判定也得用模板**：「开始唤醒」按钮（navtpl/_wakeup.png，820,726 起 280×90）匹配认页，整屏均值判据对登录页失效（底部深色横带把均值拉到 ~150，过不了 >165）；⑧ **重启流程**（`restart_app`）：先 `pidof` 查游戏进程（不在就直接拉起，别在安卓桌面上按返回键空转），`am start` 后 30 秒内要看到进程、没看到再拉（最多 3 次），轮询 600 秒、每轮先 `game_alive`（游戏半路崩了要重新拉起——实测强杀后重启会走到 START 页又崩），轮询里**先认拦截页再判主界面**（顺序反了会在登录页误报回主界面）；START 页点**黄色菱形质心**（真实中心 (0.474w,0.949h)，按亮度判会把主界面亮皮肤/公告弹窗误判成 START 页、在主界面上连点几十下）；⑨ **`run_core` 的顺序铁律**：登录（StartUp）没跑完绝不能开始导航——两者并发时导航点在启动页甚至桌面上（nav-fail 现场图是一张桌面截图），作业也永远没排队；队列头是 StartUp 就只排它、等完、`eng.stop()`、导航、再排剩下的；作业列表给了但队列没勾「抄作业」时自动补一条 Copilot；B 服账号要把 `com.hypergryph.arknights.bilibili` 包名传给 Touch，否则「游戏进程不在」的检测永远为真。每一步点完都用 `_moved()`（截图差值 >3.0）验证页面真的切了——「OK」不等于「到对了」，RS/DL/BY 三个模板都曾经匹配到相邻错误卡上还报成功。模板本身也可能过期：卡片改成 214×204 的斜切框以后，早期按「卡片+周边背景」裁的 400×258 图会失配，正确裁法是取卡片中心 200×165、避开左上进度徽章和 NEW! 角标；新活动上线后按 `docs/config.md` 的裁法补一张即可 |
| `core/agent.py` | 内置助手：10 个工具的 agent loop、OpenAI 兼容流式调用、写操作确认闸门；apiKey 走 DPAPI 单独存 `config/agent.key` |
| `backend/cli.py` | 命令行入口（`python -m backend.cli` / `MAA-PRTS.exe --cli`），输出 `{ok, command, data, error}`，退出码 0/1/2/3 |

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
| POST | `/api/account/remove` | 删除账号：直接删除账号 JSON，不备份不可恢复。账号列表卡片悬停 ✕ 触发，前端有二次确认 |
| POST | `/api/account/refresh` | 同步账号。带 `token` 同步单个（密文写进账号文件 `skd` 段）；不带则按各账号文件 ∪ 旧 `tokens.json` 全部同步。返回 `taskId` |
| POST | `/api/account/import-maa` | 从账号绑定的 MAA 目录读干员识别结果（`done=false` 残缺名单拒绝），覆盖练度/统计/缺卡（账号信息与 `skd` 保留），同步任务直接返回摘要。**默认不动森空岛同步来的练度**：命中 `operbox._token_synced()` 时返回 `skippedBy=token`，只把 `force=true` 当作用户确认（账号页手动导入）才覆盖 |
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
| 预检 | `precheck()`：MAA.exe 在跑直接拒绝（它会把 `_internal/VCRUNTIME140*.dll` 加载进自己进程，镜像必失败）。不逐个试开 `_internal`——MAA-PRTS 自己就锁着二十多个 DLL，那样判定恒误报 |
| 替换 | 程序退出后由 `update/install.ps1` 执行：按 PID 等进程退出 → 备份 exe → `robocopy /MIR` 镜像 `_internal`（失败重试 4 次，仍失败把点名的文件写进 `install-error.txt`）→ 覆盖根级文件 → 校验版本号 → 重启，失败则整体回滚并重新拉起旧版 |
| 不动的东西 | `config/`、`debug/`、`resources/`——那是用户数据，脚本里一个字都不出现 |
| 结算 | 写 `update/pending.json`；新版本启动时版本号对上才算成功，否则日志留警告，并把 `install-error.txt` 的原因带进 `/api/app/status.lastInstall.error` |

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

### 推荐排序（`rank_key` / `mode_band`）

先按「练度可行性」分档（`mode_band`），档内再按账号选的算法（少人 / 单核 / 好评量）：

| 档 | 条件 |
|---|---|
| 0 | `ready` 且评分 ≥ `READY_MIN_RATING`（9） |
| 1 | 其余能打的：`ready` 但评分不够、`borrow` |
| 2 | `train` |
| 3 | `blocked` |

`ready` **不再无条件置顶**：低分的全齐作业会和「差一人可借」的高分作业一起按评分排。
此前 mode 是第一顺位，导致 3 分的全齐作业长期占住「最优作业推荐」，旁边 10 分只差一个
助战的作业反而沉底（用户反馈 2026-11-16）。想挪分水岭改 `READY_MIN_RATING` 即可，
三种算法共用这套分档；同分时 `ready` 仍排在 `borrow` 前面。

练度不按「等级数值」直接比，而是折算成**进度分**：先用干员星级 + 等级推断其应有的精英阶段，
再按 `精二 / 精一 / 精零` 分段加权，避免「精一 70 级」被误判为高于「精二 1 级」。

### 没有技能数据的账号只比等级

MAA 干员识别（`operbox.py`）拿不到技能等级、专精、模组，导入后的账号这三项是空的。
也正因为它是降级数据，识别结果只在账号**没有练度**时兜底，不覆盖森空岛同步来的练度
（详见上表 `/api/account/import-maa` 的 `force` 语义）。
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

## 关卡流程顺序（`resources.flow_key` / `next_stage`）

结果列表和「下一关」共用一套顺序（`row_sort_key` 的第一段键 = `flow_key()`），
按关卡在游戏流程里的先后来排，不再按 stageId 字符串排：

| 层级 | 依据 |
|---|---|
| 桶 | 一章一个桶（`main_`/`easy_`/`tough_`/`st_`/`sub_`/`hard_` 都归到章号），一个活动一个桶 |
| 桶内 | 先按 `_placement` 排骨架（基础关 → EX → S → 特殊，基础关里**剧情关排在同号主关前面**），再按 `unlockCondition` 把 S 支线关 / TR 教学关插到锚点后面 |
| 跨桶 | 主线按章号 0→17，活动按 `activity_table` 的开服时间新→旧 |

**TR 教学关和主线 S 关的位置不是猜的**：游戏数据 `stage_table` 里每关都有
`unlockCondition`，前置关就是流程里它前面那一关。两张表都是从这份数据导出来的：

| 表 | 内容 | 例子 |
|---|---|---|
| `TR_AFTER` | TR 教学关（93 条，含链式） | `tr_s_1 → tr_07`、`tr_13 → sub_02-07` |
| `SUB_AFTER` | 主线 S 支线关（49 条） | `sub_02-01 → main_02-01`、`sub_02-03 → sub_02-02` |

`_flow` 先把每章的骨架（基础关 → EX → S）按 `_placement` 排好，再按锚点做**树展开**，
整章顺序就是推图顺序：

```
TR-11 → 2-1 → S2-1 → 2-2 → S2-2 → S2-3 → S2-4 → 2-3 → TR-12 → 2-4 → S2-5 → …
```

- `TL-TR-1` 排在 `TL-1` **前面**（解锁条件是 `act55side_st01`）
- `0-1` 的下一关是 `TR-1`，`TR-1` 的下一关是 `0-2`
- `2-1` 的下一关是 `S2-1`，`S2-1` 的下一关是 `2-2`，`S2-4` 打完才轮到 `2-3`
- 表里没有的 TR（IG 联机教学、塔 `lt_tr_01`）按「位置未知」排到所在章最前面

序号是展开后的**整数下标**，不是小数插值——`S2-10` 这种长链不会因为「+0.1」累积越过
下一关。以前 S 关只算「章的尾巴」、TR 单独算小数偏移，所以搜 `TR-` 时 `TR-13`
（解锁于 S2-7）会跑到 `TR-14` 前面。游戏更新后往两张表补新关即可
（`resources.clear_cache()` 后生效，冒烟 B19 覆盖）。

**搜章节号会带上整章**：`2` / `2-` / `12` / `15-` 这类纯章节号按 flow 的桶过滤，返回
该章的普通关 + S 支线关 + TR 教学关（剧情关仍排除），顺序就是上面的推图顺序。
以前只能搜到 `2-1 … 2-10`，S2-1 和 TR-11 都搜不出来。活动关不受影响，仍按活动 id
前缀 / 活动名搜。

**10-14 章的「简单」行只在这几章进系列视图**：系列/活动视图一般只摆主难度行
（简单档作业常年个位数，多一行就是多一行「共 0 份」），但 10-14 章的导入菜单要按
简单/标准/磨难分开导入，所以 `_EASY_CHAPTERS` 里那几章保留简单行；9 章及以下仍不摆。

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

## 挂关 skill（`skills.py`）

2026-11-17 把挂关流程拆成六个可独立调度的 skill，由 `run_skill(name, params)`
统一跑成后台任务（`tasks.create`），返回 taskId 给前端 / 助手轮询 `/api/task`。
API：`POST /api/skill/run`（body：`{"skill": ..., "params": {...}}`）、`GET /api/skill/list`。

| skill | 能力 | 实现依托 |
|---|---|---|
| `wakeup` | 唤醒游戏到主界面；带 `accountName`（手机尾号等切号名）时走 MAA StartUp 换号 | `stagenav.restart_app` / `maaplan.run_core` |
| `navigate` | 从主界面导航到任意活动（老活动走曲谱、当期走区域列表、EX 也能到） | `stagenav.goto_stage_list` |
| `fight` | 自动战斗：编队 / 借助战 / 吃理智药 / 开打，一次一组关 | `maaplan.run_core`（Copilot 参数 formation/support/sanityPotion） |
| `skip_story` | 过剧情关（MN-ST-1 这类 ST 关）：MAA fight 单关当「点关探针」，剧情播放页出现后 `stagenav._skip_story_replay` 连续跳幕 | adb 跳幕 |
| `signin` | 森空岛签到（单号 / 全部） | `signin.run` |
| `weekly` | 周目编排：ST 关自动过剧情、连续战斗关合并成 fight 批次、EX 收尾；**任何一步失败立即停**并在日志写明卡在哪关 | 本文件 `_cycle_rows` + `_cycle_sort_key` |

排序规则（`_cycle_sort_key`）：`ST-N → 主线 1..N → EX`，正好是 MN 这类周目的
推图顺序；关卡清单来自 `resources._stages(True)`（含 story=True 的 208 条剧情关，
resolve 的默认视图查不到它们）。

助手侧（`agent.py`）注册了对应工具：`skill_wakeup / skill_navigate / skill_signin`
免确认直接跑；`skill_fight / skill_skip_story / skill_weekly` 走 `_guard` 确认卡
（确认标记 `_confirmed` 放进 pending args，重入直接跑不再二次确认）。
MAA 事件日志通过 tid 透传进同一条任务日志，右栏日志栏与助手都能看到卡在哪。

## 依赖

- Python 3.10+
- `pywebview`（自带 pythonnet + WebView2；WebView2 Runtime 是 Win10/11 系统组件）
- 打包用 `pyinstaller`

其余全部是标准库。

## run_core 直跑模式 noNav（2026-11-17 晚）

skill_fight 默认**不唤醒不导航**：spec 带 `'noNav': True` 时，run_core 会把
core_tasks 强制补进来的 StartUp 摘掉（`seq = [(t,p) for t,p in seq if t !=
'StartUp']`），并且跳过 `elif first:` 的导航分支——Copilot 从当前界面直接
接手（当期活动 MAA 自己认路；老活动要调用方先到位）。skill_fight 里
`wakeup=true` / `navigate=true` 参数可以退回全套流程。skip_story / weekly
不带 noNav，行为不变（它们依赖导航）。用户实测吐槽「skill 化了怎么还在
唤醒+导航」就是这条缺的口子。

### skill_fight 空队列（noNav 第二坑）

core_tasks 的 Copilot 分支要求 spec['copilotList'] 非空否则 continue；skill_fight
以前只传 stages，队列里只剩强制补的 StartUp → noNav 摘掉后报「没有可执行的
任务」。修复：skill_fight 在 mode=copy 时自动把 stages 转成 copilotList——
按代号搜作业站取第一个作业（_prepare_ops → copilot.search + prepare_copilot），
也接受调用方直接给 ops / copilotList。fight 纯刷关走 Fight 任务，不需要作业。

### /api/skill/try（联调测试，2026-11-17）

`POST` body `{text, skill?, run?}` → `{text, said, picked, args, matched, taskId, note}`。
不建会话、不摆确认卡；`run=true` 直接跑并返回 taskId。实现见
`agent.try_skill`（PROBE_SYSTEM 强调别自作主张补唤醒/导航）。

## skill_navigate 的输入规范化（2026-11-17 深夜）

用户实测：助手说「导航愚人号」，两次都报「没进到活动地图（活动可能已结束）」。
日志 `导航到「act1d3」：活动第 1 个区域` 说明**模型把活动 id 当关卡号传下来**，
`event_of()` 认不出（正则要求 `PREFIX-数字`），`zone_rows_of()` 又对未知串兜底成
「第 1 区」，于是掉进「当期活动」分支去点活动大图——老活动当然进不去。

修法（三层）：
1. `skills.norm_stage(raw)`：中文活动名 / 活动 id（act25side）/ 关卡 id
   （act25side_01）/ 代号前缀（CW）/ 关卡号（MN-EX-1）统一换成真实关卡代号，
   逐个 `resolve`；认不出就报错并列出近期活动名。`skill_navigate` 的 stage 与
   activity 两个参数都过这里。
2. `stagenav.goto_stage_list`：开头加形态闸。不是 `PREFIX-数字` 形态的输入，
   纯前缀（CW）自动补成 `CW-1`，其余**直接返回明确错误**，不再拿原始串去
   `zone_rows_of` 兜底乱点。
3. 助手侧：`skill_navigate` 工具描述强调「直接传中文名」，
   `search_stages` 返回补 `prefix` / `activity`（EVENTS 反查），SYSTEM 硬规矩
   加「活动代号一律查，不许凭记忆换算（孤星=CW，覆潮之下=SV，愚人号=SN）」。

真机验证（dev 环境、灰bao 官服、游戏停在主界面）：
`POST /api/skill/run {"skill":"navigate","params":{"stage":"孤星"}}` →
`「孤星」→ CW-1（孤星（29 关））` → 走曲谱 → 第 9 屏命中（1.5 分）→ 进图，
落图确认是孤星关卡地图（CW-1/CW-2/CW-ST-1 + 右下三个区域）。全程约 2.5 分钟。
模型侧：`/api/skill/try`「帮我导航到孤星」→ `skill_navigate{"stage":"孤星"}`；
「停在主页，导航到孤星，不要唤醒」→ 同样只调 navigate，没有多带 wakeup。

另外 `skill_navigate` 加了游戏存活检查：`game_alive()` 为假时直接报
「游戏没在跑——先调 skill_wakeup」，不再白转一分钟恢复循环后含糊报「回不到主界面」。

## 乐章收录网格的四个坑（2026-11-18 凌晨）

用户实测密林悍将归来（`RI`）导航失败，现场截图停在网格的「完成进度」标签上。
顺着挖出四个坑，全部落在 `stagenav.goto_story_activity`：

1. **进网格必须点一下「默认进度」**（`tabProgress`，1280 口径 (741,45)，1920 口径
   (1111,67)）。这一页会记住上次选的标签，停在「完成进度」/「奖励进度」时每张卡
   左上角都盖着进度徽章（38/39 那种蓝菱形 + 勾），卡片图被改掉一块，模板匹配从
   5 分涨到 20 分以上——整轮扫描白跑，最后报「翻到底没找到」。现在点完「乐章收录」
   先用 `in_score_grid()` 确认真的进了网格（判据：顶部三个标签必有一个是蓝字 +
   蓝下划线，实测蓝像素占比 网格 0.035 / 时间轴 0.000，阈值 0.008），再点标签，
   没进网格会重试一次、还不行就明确报错，不会傻翻 40 屏。
2. **截图要等滑动惯性停住**。`input swipe` 之后画面要 1.8~2.2 秒才停
   （逐帧差 22 → 11 → 3.8 → 2.1 → 1.6），1.4 秒时截到的还是拖影帧（卡片被拉成
   两半、顶部多一条半透明标题带），匹配分数虚高。`_settled_shot()` 现在固定等
   2.2 秒拍一帧；整屏连一个候选都没有（999，多半是缩略图还没加载）时补拍一帧。
   拖影帧本身也有兜底——步长 240 小于卡片在屏内的可见高度，同一张卡在相邻 3~4
   屏里都会完整出现，糊掉一屏不影响。
   **别改成「连拍两帧比差」**：卡片有呼吸/闪光动画，同一位置连拍两帧也有 1.76 的
   差，判据只能定到 2.5，几乎每屏都判「还在动」，每屏白多等 1.5 秒（实测 7.8 秒/屏）。
3. **到顶要甩够、到底要停**。整页约 15 屏（顶部还有「主题曲」EP 网格，约 5400px /
   1080 口径，以前按「40 屏」估会导致到底后空转）。旧的「16 × 440」只走 10560px（720 口径），从列表底部出发到不了顶，
   扫描就从中间开始，上半部分的卡（RI 在第 13 屏）永远扫不到——`_scroll_top()`
   改成 12 × 800 的快速拖拽。反过来，以前靠固定 56 屏兜底，到底后还在原地空转
   40 屏（一屏 4 秒，白烧三分钟）；现在连着两屏「画面没动」（差 < 3.0）就判定到底。
4. **`IW`（将进酒）的卡片模板过期了**。离线全量评估（40 张网格截图 × 46 张模板）
   只有它 18.6 分掉出阈值 12，其余全部 ≤ 7。原因是早期裁图带进了周边背景、
   尺寸也和现在的 296×150 卡片对不上。按当前卡片中心重裁一张（避开左上进度徽章、
   NEW! 角标）：自匹配 0.0，同一张卡换个滚动位置 2.6。

顺带修的两处性能账（导航一屏要多次 adb，每次进程启动就是 0.5~0.8 秒）：
`Touch.swipe` 不再补发 `_release()`——滑动自带抬起，那两条 `sendevent` 每条
0.25 秒，翻十几屏就是十几秒白烧；扫描前不再单独「预滚一遍」（20 屏 30 秒），
图片加载竞态改由逐屏 2.2 秒等待 + 999 补拍兜住。

顺带加了「卡片位置缓存」（`config/navcache.json` 的 `cardOffset`）：扫到卡时记下
它离网格顶部多少像素（1080 口径），下次导航回顶后**按记录翻到那一屏的前一屏**，
从那里接着往下扫——少翻一屏是必要的：回顶那一甩结束时的回弹会把紧接着的第一下
拖拽吃掉一截，实测正好差一屏。缓存只当提示：一路扫到底还没命中就回顶重扫一遍，
永远以模板匹配为准。

另外 `really_home()` 的收尾判定要**复用循环里的结果**：原来循环里判到主界面就
break，循环后再调一次 `really_home()`——两次之间隔着几百毫秒，页面正好在切页
动画里就会翻脸判 False，日志表现为「4 秒就 FAIL：回不到主界面」，而游戏其实
好端端在主界面（2026-11-18 RI 实测踩到）。现在循环结果存进 `home`，不成立时
等 1.2 秒再复核一次。

真机复验（dev 环境、灰bao 官服）：
`RI-1` → 第 13 屏命中 4.4 分 → 进图；`IW-1` → 第 8 屏命中 2.6 分 → 进图
（重裁模板前是翻到底 17.9 分 FAIL）；`CV-1`（靠底部）第 15 屏命中 1.1 分 →
进图，全程 2 分 36 秒。走缓存的第二次 `RI-1`：回顶 → 直接翻到第 12 屏 → 第 14 屏
命中 5.4 分，全程 1 分 26 秒；网格本来就停在目标卡上时「当前这一屏就找到了
（1.2 分）」，全程 48 秒。
