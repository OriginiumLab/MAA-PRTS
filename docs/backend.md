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
| `core/envcheck.py` | 开跑前的环境闸：模拟器开着没有、adb 通不通。路径优先从 MAA 的 `gui.new.json` 抄（`Config` 类型 / `AdbPath` / `Extras.<类型>.EmulatorPath` / `InstanceIndex`），再退到 `settings.emulatorPath` 和 adb 反推。没启动就替用户启动（`settings.emulatorAutoStart`，默认开），起来才放行。**不进 `SKILLS`**，所以 `/api/skill/list` 和助手工具表里都看不到它——它是每个任务的前置闸，`skills.run_skill` 起任务时自动调一次，正常时零日志 |
| `core/tasks.py` | 内存任务表：状态、日志、结果，供前端轮询。`owners` 记任务属于哪些账号（右栏按账号分栏用），`prefix` 让多开子进程的每行日志自动带 `[昵称]`；`_public()` 只吐可序列化字段——文件句柄绝不能出接口（2026-11-22「右边日志都消失了」就是它漏出去导致 `/api/task` 500） |
| `core/progress.py` | 森空岛 `player/info` → 关卡进度：主线推到哪（含剧情关落点）、后续关卡、活动 zone 已打/总数、剿灭最高击杀、日常周常。只做 zone 级余量判断，不拿计数切关卡列表（两边口径含剧情关/突袭关，会错位） |
| `core/asstlog.py` | MAA 日志观测：`debug/asst.log` 的回调 JSON + `debug/gui.log` 的 `Start/Completed Task Chain` → 结构化事件流；增量读、轮转识别、状态推导（idle/running/stalled/failed/done）、`failures()` 还原「跑过哪几关、哪几关没跑完」 |
| `core/maalog.py` | 直连跑机时的 MAA 日志桥：`maalog.Tap` 从 `asst.log` **末尾**开始增量读（`Tail.seek_end()`，不重放历史），只把 `[ERR]/[WRN]` 里能解释问题的行翻成人话（认不出关卡名 / 干员不存在 / 参数不合法），噪行丢弃、同句 20 秒去重。回调 JSON 行不读——那部分由 `maacore` 回调负责，避免同一件事写两遍 |
| `core/watch.py` | 自愈层：后台线程盯 `asstlog` 的状态，判停滞就关 MAA → 从当前关续写作业列表 → 重启。同一关连续 3 次救不回来就停手报给用户 |
| `core/acctname.py` | 切号名（MAA `AccountName`）的归一化与校验，**唯一一份规则**。MAA 是拿它和登录界面那行 OCR 做子串匹配，所以 `5505` / `136` / `23****45` / `157****3635` / `181****5505` 都合法；用户给全号、带横线、全角数字、星号数量不对都归一成 `前3****后4`，只给「前 3 + 后 4」（`1573635` / `157 3635`）也收。官服只拦「填了昵称」（拿昵称切号会死循环几十次然后失败），B 服不拦 |
| `core/acctbind.py` | 「这个账号跑 StartUp 写哪个切号名」的唯一解析入口：账号绑了配置文件（`maa.profile`）且没手改过 → 读那份档案 StartUp 的 `AccountName`（`maa.profile_account_name()`，**只读**）；否则用账号存的 `maa.accountName`；都没有才返回空让调用方去问用户。手改过的值标 `accountNameFrom=manual`，不会被档案里的旧号码盖回去 |
| `core/multiacct.py` | 多账号编排：MAA「一个配置档案 = 一个账号」，按各账号练度各挑作业，串行切换执行。档案一律新建 `PRTS-` 前缀的那份，用户自己的档案一个字段都不动。切号名统一走 `acctname.check()`；`maaProfile` 是档案名不是切号名，别混用 |
| `core/maaplan.py` | 把「用户想怎么挂」翻成可执行的东西，两条路线：① `build_queue`/`ensure_profile` 写 GUI 的 `TaskQueue` + `RunDirectly`（能看界面，但**不能抄作业**：MAA 的 `InitializeItems` 不认 CopilotTask，会抛 NotImplementedException 让整个任务列表空白）；② `core_tasks`/`run_core` 生成 MaaCore 的任务序列直接驱动（能抄作业、能跑 Custom 小活动、自己按开始）。`run_core` 跑抄作业固定四段：**登录（StartUp）→ `AsstStop` 把 MAA 停住 → 用 `stagenav` 点到关卡地图 → 再 append Copilot + `AsstStart`**。停这一步不能省：不停的话 MaaCore 还在按自己的逻辑截屏/滑动，和我们的 adb 点击抢界面。导航失败且这一关有卡片模板（老故事集）时**直接报错**，不把作业交给 MAA——它只会横向滑主线，白跑 30 秒再失败（2026-11-17 SV 实测：停住后 24 秒内 MAA 就在地图上 OCR 到 SV-1、点开始行动、自动编队开打） |
| `core/maacore.py` | MaaCore 直连：`AsstCreateEx` → `AsstLoadResource` → `AsstConnect` → `AsstAppendTask` → `AsstStart`，回调翻成事件流。三个坑写在文件头：`AsstCreateEx` 的 restype 必须是 `c_void_p`（否则 64 位句柄截断成 32 位，每次 append 都 access violation）、Copilot 的 `filename` 必须绝对路径、`copilot_list` 的键是 `filename` 不是 `file_name`（写错会让进程 C++ 异常崩溃）；还有第四个**静默**坑——子任务回调码是 20000 段不是 70000 段（见下一节） |
| `core/pages.py` | 认「现在停在哪一页」+ 回主界面。主页 = 左上角「设置齿轮 + 邮件」两个图标定点比对（实测 0.0 分；活动地图/设置页/基建都不命中）；基建「是否确认离开罗德岛基建？」= 底部按钮条上左「✕」右「✓」两个白圆钮同时定点命中，认出就点右半「确认」的中心（这个弹窗按 ESC / 返回都退不出去）。**「是否确认退出游戏？」按钮条长得一样**，靠「背后是模糊的主界面（亮）」区分，认出点左边「✕」取消并直接当到家——点错右边就是把游戏关掉。`to_home()` 每轮先看一帧再决定按返回 / ESC / 点确认。 |
| `core/stagenav.py` | 把界面带到活动关的关卡列表。**MAA 自己做不到**：`TL-Open`/`TLChapterToTL` 那套模板图在当前活动 UI 上全匹配失败，Copilot 的 `FullStageNavigation` 又只会横向滑动切章节（这版游戏改成点底部 EPISODE 了）。这条路是真机一步步点出来的：底部「终端」tab → 主界面活动大图 → 主题页区域名 → 活动地图区域列表第 N 行。两个坑：① 主界面点大图是**记忆位置**的，所以流程按 `probe()` 判出的页面状态收敛，不是死按顺序点；② `AsstAsyncClick` 一次连接只能点中第一下，所以点击/截图都走 adb（`Touch`）。坐标 1280×720 口径，可用 `config/nav.json` 覆盖 |
| `core/stagenav.py`（老故事集） | 当期活动之外的第二条路：**曲谱 → 乐章收录 → 网格找卡片 → 进入活动 →（EX / S / MO 关再点右下角那排分区图标里的对应格子）**。老活动 MAA 资源里压根没有导航配置（`Fight` 直接拒收），只能从「曲谱」这个统一索引里进。网格里卡片位置跟滚动位置绑定、新活动还会把顺序挤动，所以**按模板图找**（`navtpl/<代号>.png`，46 张随包发布，覆盖全部有战斗关的常驻活动；代号是 2026-11-17 真机把 48 张卡逐个导航进图、读落图关卡前缀核出来的，易错对照见 `stagenav.EVENTS` 注释）。分发双闸：模板存在**或**代号在 `EVENTS` 表里都走这条路；`EVENTS` 里的活动导航失败会**直接报错停止**，绝不交给 MAA——它只会横向滑主线，白跑 30 秒再失败。匹配是「1/4 降采样粗扫 + 全分辨率两轮精修 + 全程只记最像那一屏」，粗扫明显不像（>32 分）就跳过精修：真卡 0~7 分、无关卡 21 分起，**阈值 12**（暗色模板在相邻暗卡上能拿 14~19 分，18 挡不住）。五个坑：① 主界面有两套视图，不带底部 tab 的那套要先点中央「终端」大卡（`at_home()` 用底部功能格白像素判，主界面 0.45 / 其它 ≤0.07；带白色 EX 徽章的活动地图上白像素也会超 0.2，所以回主界面的判据是 `at_home AND not in_story_page AND not in_story_dialog AND not on_login_page` 四条件——后两个是 2026-11-17 补的：明亮沙地 CG 回放连 `at_home` 都能骗过，登录页浅灰底 + 白 logo 也能骗过）；② **扫描步长别贪快**：整张表（主线 + 各 Story Line + 未归组的新活动）11 屏，`_SWIPE_STEP` 360 配 1.4 秒等待刚好一张不漏；560 那版实测会漏屏——玛莉娅·临光（MN）正好落在两张截图之间，翻完 34 屏报「没找到」，用 360 重扫第 5 屏 1.0 分命中；③ **`in_story_page()` 必须两条同时成立**（底部左 tab 白 <0.05 且左上灰度 <90）：只看灰度会把主界面判成曲谱页（皮肤立绘压在左边，灰度 98.5 卡在旧阈值 100 下面），于是登录后一步没走就开始翻屏，报「翻了几十屏没找到」；④ **滑动起点必须在最左边缘 x=20 的空白处**，压到卡片上会被判成点击、直接跳进那个活动的详情页，之后分数一动不动（`goto_story_activity` 里躺过一次）；⑤ 退回上一层**先按返回键、再按 ESC**（有些详情页对 `keyevent 4` 完全没反应，ESC 一下就退）；⑥ **剧情页判定换了三版才站住**：白像素占比会把主界面右上角的货币数字（合成玉/342）当成「跳过▶」，于是「剧情排水」在主界面按返回键弹「退出游戏」又点中确认——**游戏被自己关掉**；灰度模板又把 TG 的亮天空烤进了模板，暗背景剧情全失配（而且模板匹配锚点一度把「中心」当「左上角」，全体错位）；最终用**字形掩码 XOR**（两边 >235 二值化后比对差异占比，剧情页 0.00~0.14 / 非剧情 ≥1.0，阈值 0.55），背景无关且天然排除登录页；⑦ **登录页判定也得用模板**：「开始唤醒」按钮（navtpl/_wakeup.png，820,726 起 280×90）匹配认页，整屏均值判据对登录页失效（底部深色横带把均值拉到 ~150，过不了 >165）；⑧ **重启流程**（`restart_app`）：先 `pidof` 查游戏进程（不在就直接拉起，别在安卓桌面上按返回键空转），`am start` 后 30 秒内要看到进程、没看到再拉（最多 3 次），轮询 600 秒、每轮先 `game_alive`（游戏半路崩了要重新拉起——实测强杀后重启会走到 START 页又崩），轮询里**先认拦截页再判主界面**（顺序反了会在登录页误报回主界面）；START 页点**黄色菱形质心**（真实中心 (0.474w,0.949h)，按亮度判会把主界面亮皮肤/公告弹窗误判成 START 页、在主界面上连点几十下）；⑨ **`run_core` 的顺序铁律**：登录（StartUp）没跑完绝不能开始导航——两者并发时导航点在启动页甚至桌面上（nav-fail 现场图是一张桌面截图），作业也永远没排队；队列头是 StartUp 就只排它、等完、`eng.stop()`、导航、再排剩下的；作业列表给了但队列没勾「抄作业」时自动补一条 Copilot；B 服账号要把 `com.hypergryph.arknights.bilibili` 包名传给 Touch，否则「游戏进程不在」的检测永远为真。每一步点完都用 `_moved()`（截图差值 >3.0）验证页面真的切了——「OK」不等于「到对了」，RS/DL/BY 三个模板都曾经匹配到相邻错误卡上还报成功。模板本身也可能过期：卡片改成 214×204 的斜切框以后，早期按「卡片+周边背景」裁的 400×258 图会失配，正确裁法是取卡片中心 200×165、避开左上进度徽章和 NEW! 角标；新活动上线后按 `docs/config.md` 的裁法补一张即可 |
| `core/scatter.py` | 散点关卡（S 关）：活动地图上浮着的是 `OS-S-1`~`OS-S-5` 五个按钮，不在关卡列表里，MaaCore 的 `ClickStageName` 认不出。这里用定点模板认格子 → 点「开始行动」/「突袭模式→开始突袭」→ 等编队页，再交给 `maaplan.run_scatter` 用单作业模式（只给 `filename`、不给 `copilot_list`）一关一趟打。格子的 `roi` 把命中位置也锁死（模板带雪山背景会认到隔壁）；编队页判据用右上「快捷编队」而不是右下那条有滑入动画的「开始行动」 |
| `core/agent.py` | 内置助手：10 个工具的 agent loop、OpenAI 兼容流式调用、写操作确认闸门；apiKey 走 DPAPI 单独存 `config/agent.key` |
| `core/mirror.py` | 国内更新源（Mirror 酱）：版本查询、错误码翻译、CDK 的 DPAPI 存取（`config/mirror.key`）。见 `docs/update.md` |
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
| POST | `/api/account/maa` | 把 `maa.profile` 等用户字段写回账号 JSON（同步不覆盖的那几个）。`dir` 会归一化并并入「用过的 MAA」历史；`profile` 是 MAA 的配置档案名（只记名字，**不读写档案内容**）；`accountName`（切号名）统一走 `acctname.check()`——`5505` / `181 5505` / `157****3635` 都收成规范写法，官服填昵称当场 400，**留空合法**（还没填 / 想清掉）。`accountNameFrom`：`manual`=手填（以用户为准）/ `profile`=跟着配置文件走；不传时按 `manual` |
| POST | `/api/account/token` | 设置 → 账号管理用：返回 `{has, broken, token, mask}`。`token` 是明文（只在本地窗口里展示，不落日志、不进 bootstrap），`mask` 只留尾 4 位；`broken=true` 表示密文本机解不开（换电脑 / 换 Windows 用户），与「没存过」分开 |
| POST | `/api/account/token/check` | 拿 grant 接口验一次 token 还能不能用（最长约 12 秒）。只有明确失效才回 `ok:false`，网络问题走异常——界面据此决定要不要解锁「修改」 |
| POST | `/api/account/token/set` | 换 token：先 `Skland(token)` 登录 + 取绑定，确认 token 绑的 uid 就是这个账号，才把密文写回 `skd` 段（练度 / 进度 / MAA 绑定一个字段都不动）。uid 对不上直接报错，防止粘错号把凭证静默换掉 |
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
| GET | `/api/task?id=&since=` | 后台任务状态与增量日志（`since` 是日志游标），带 `owners`（属于哪些账号） |
| GET | `/api/tasks?file=&limit=` | 某个账号相关的任务（新的在前，含完整日志），助手右栏切账号时回填用；任务 `owners` 为空 = 全局，所有 tab 可见 |
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
2026-11-18 增加第七个 `train`（培养干员）；2026-11-19 加 `sweep` / `tool`；
2026-11-22 加 `claim`（领活动奖励），并新增 `run_batch()` 把任意几项串成一条龙
（见「多 MAA / 多模拟器 + 一条龙」）。

| skill | 能力 | 实现依托 |
|---|---|---|
| `wakeup` | 唤醒游戏到主界面；带 `accountName`（手机尾号等切号名）时走 MAA StartUp 换号 | `stagenav.restart_app` / `maaplan.run_core` |
| `navigate` | 从主界面导航到任意活动（老活动走曲谱、当期走区域列表、EX 也能到） | `stagenav.goto_stage_list` |
| `fight` | 自动战斗：编队 / 借助战 / 吃理智药 / 开打，一次一组关 | `maaplan.run_core`（Copilot 参数 formation/support/sanityPotion） |
| `skip_story` | 过剧情关（MN-ST-1 这类 ST 关）：MAA fight 单关当「点关探针」，剧情播放页出现后 `stagenav._skip_story_replay` 连续跳幕 | adb 跳幕 |
| `signin` | 森空岛签到（单号 / 全部） | `signin.run` |
| `weekly` | 周目编排：ST 关自动过剧情、连续战斗关合并成 fight 批次、EX 收尾；**任何一步失败立即停**并在日志写明卡在哪关 | 本文件 `_cycle_rows` + `_cycle_sort_key` |
| `train` | 培养干员：精英化 / 技能基础等级 / 专精，可一次排多个；材料不足**暂停**并落盘剩下的计划 | `maaplan.run_core` 的 `OperProgress` 任务 |
| `sweep` | 扫图：普通 → EX 普通 → EX 突袭 → S 关，段间自动重新导航 | 本文件 `_sweep_segments` |
| `tool` | MAA 牛杂小工具（小游戏 / 活动商店） | `maatools` + `eventnav.run_tool` |
| `claim` | 领活动奖励：进奖励页点「一键领取」（纯 adb，不连 MaaCore） | `eventnav.claim_reward` |

**加一个新 skill 只要三步**（2026-11-22 重构，`REGISTRY` + `@skill` 装饰器）：

```python
@skill("fishing", "钓鱼：自动打完今天的鱼", aliases=("钓鱼",))
def skill_fishing(ctx):          # 第一个参数叫 ctx → _call_skill 传 Ctx
    t = ctx.touch()              # 这个号的 adb 连接（档案/多开地址都已生效）
    ctx.log("开始钓鱼")
    return {"ok": True}
```

1. 函数第一个参数写 `ctx`（`Ctx` 里有 `file` / `maa_dir` / `conn` / `client` /
   `touch()` / `spec()` / `log` / `tid`），不用再抄「解析账号 → 查 MAA 目录 →
   拼连接 → 建 Touch」那四行；
2. `@skill(名字, 一句话描述, needs_game=?, aliases=?)` 会把它登记进 `REGISTRY`，
   `SKILLS` / `_NEEDS_GAME` / `ALIASES` / `/api/skill/list` 全部自动派生，
   **不用改第二张表**；
3. 老写法 `(params, log, tid=None)` 继续支持（`_call_skill` 看第一个参数名决定
   怎么调），现有 skill 可以一个一个慢慢迁。

页面状态判断和「回主界面」不在这个文件，见 `pages.py`（下一节）。

排序规则（`_cycle_sort_key`）：`ST-N → 主线 1..N → EX`，正好是 MN 这类周目的
推图顺序；关卡清单来自 `resources._stages(True)`（含 story=True 的 208 条剧情关，
resolve 的默认视图查不到它们）。

助手侧（`agent.py`）注册了对应工具：`skill_wakeup / skill_navigate /
skill_signin / skill_claim` 免确认直接跑；`skill_fight / skill_skip_story /
skill_weekly / skill_train / skill_sweep / skill_tool / run_steps` 走 `_guard`
确认卡（确认标记 `_confirmed` 放进 pending args，重入直接跑不再二次确认）。
MAA 事件日志通过 tid 透传进同一条任务日志，右栏日志栏与助手都能看到卡在哪。

## 用户实测三连修（2026-11-18 晚）

用户跑完真机测报了三个 bug，根因都在 MAA 日志（asst.log）里看得清清楚楚：

1. **「MAA 根本没跑开始唤醒」——切号名永远为空**。`skills._account_name_tail`
   读的是 `snap['accountName'] / ['nickName']`，可快照顶层**根本没有这两个
   字段**（昵称在 `account.nickName`、切号名在 `maa.accountName`）——切号名
   永远拿不到，StartUp 不带 `account_name`：游戏已在主界面时这条链 2 秒就
   「完成」，什么都没做，用户看到的就是「没唤醒就开始导航/培养」。
   **昵称绝不能顶**：官服切号认手机号，拿昵称 MAA 在账号列表里翻不到。
2. **agent 模式切号 = 临时配置，只读用户的 MAA 档案**（2026-11-18 定口径，
   2026-11-22 放宽一处）。agent 跑 MAA 从不**写**用户的 `gui.new.json` 档案：
   所谓的「配置」就是 StartUp **任务参数**——只配「开始唤醒 + 该账号的切号
   名」，MaaCore 的 `StartUpTask` 拿到非空 `account_name` 就自己换号
   （核对过 6.20.1 源码：`set_params` 只认 `account_name` / `client_type` /
   `start_game_enabled` 三个键，**没有** `account_switch_enabled`，给了也
   被忽略）。切号名的来源按顺序（`acctbind.resolve`）：① 账号绑的配置文件
   里 StartUp 的 `AccountName`（用户 2026-11-22 要求：他在 MAA 里已经配过
   一次，不该再抄一遍，**这一处破例读档案，但只读一个字段**）；② 账号存的
   `maa.accountName`（手填的 / 对话里报的）；③ 都没有就**问用户**。
   中间版（2026-11-18）曾完全禁读档案，因为「两个游戏账号对 MAA 里五个
   尾号档案，档案名和 AccountName 说不清谁是谁」——现在不是猜：绑哪份档案
   由用户在设置里点名，程序照他点的读，手改过的值（`manual`）优先。
2. **「培养干员没进去就报错」——`Engine.wait` 启动竞态**。asst.log 现场：
   StartUp 完成 → append OperProgress → `start` → **同一毫秒 `~Assistant`
   析构**，任务「完成」但培养一步没跑。原因：AsstStart 异步生效，刚 start
   完的头几百毫秒 `AsstRunning` 还是 False，而 `_done` 刚被 `start()` clear，
   `wait()` 的 `if not self.running(): return self._done.is_set()` 把「还没
   开始跑」当成「已经跑完」，`_wait_run` 两层立刻返回，run_core 收尾 close
   把刚排队的任务当场杀掉。修复：`wait()` 只在**见过 running=True** 之后才
   认「running 变 False = 停了」；没见过时给 15 秒启动宽限。
   **同一处还有第二个窗口**（2026-11-18 22:35 用户实测）：唤醒链一完成
   （`AllTasksCompleted` 早于工作线程收尾，`AsstRunning` 仍为 true），run_core
   第二段 append 培养再 `AsstStart` → **直接返回 false**。第一次修的是「false
   就等 running 变 false 再重试」，方向反了：MaaCore 被挡时并不会停，它继续
   消费队列，我们刚 append 的那条几十毫秒后就开跑（asst.log：
   `00:25:09.674 Start | block` → `00:25:09.776 TaskChainStart`）。于是
   `running` 一直 true → 等满 15 秒 → 判成失败 → 用户看到「X RuntimeError:
   AsstStart 返回失败」，可任务其实在跑（2026-11-19 三次培养全中）。
   **现在的判据**：`AsstStart` 返回 false 后盯 `_chain_starts`（收到
   `TaskChainStart` 的次数）——新链开始了就直接算成功；`running` 变 false
   才是「真没接」，这时才重试一次；`running` 一直是 true 也当成功（队列在被
   消费），交给 `_wait_run` 兜底重排一次，宁可多等一秒也不误判。
3. **「设置里配过手机尾号，助手每次还问」**——`list_accounts` 没带切号名。
   模型看不到账号绑定的 `maa.accountName`，只能按提示再问一遍。修复：
   工具输出补 `accountName`，`hint` 和系统提示都写明「非空的直接沿用，别
   再问」。真正开跑时 `skills._account_name_tail` 本来就优先读账号绑定。
4. **「只推荐普通难度，说突袭打不了」——作业变体永远落 normal**。突袭关
   （含 IW-EX 这类有 `#f#` 突袭 id 的活动关）在作业站拆普通/突袭两行，同一份
   通用位（difficulty=3）作业两行都有。`stageplan._lookup` 与
   `skills._prepare_ops` 挑行时 band 相同先到先得、行序普通在前（且
   `_prepare_ops` 干脆只取第一行），于是 variant 永远是 normal，写进 MAA
   一律 `IsRaid=false`——用户挂 IW-EX 打的全是普通难度，作业明明叫
   「IW-EX-1突袭五人」。修复：同样能打时**优先突袭行**（可打性仍优先于
   变体：突袭行全打不了、普通行能打时照旧选普通）。

## 每关只打真正要打的那一档（2026-11-19 03:13 定稿）

前两版都错了，用户第三次报才拿到决定性证据。asst.log：

```
11-17 17:42  MN-EX-1_98495.json is_raid=false   ← 手动导的作业集
             MN-EX-1_98495.json is_raid=true
11-19 01:42  SV-EX-1_41623.json is_raid=true
             01:42:21 to_be_recognized: ["RaidConfirm","ChangeToRaidDifficulty"]
             01:42:22 切突袭完成 → BattleStartPre           ← 单条突袭其实能打！
11-19 02:02  SV-EX-1 false / SV-EX-1 true 交替 14 条     ← 我们排的两条
             → 用户在模拟器里看到的就是「又从头挂了一遍普通 EX-1」
```

**MaaCore 拿到 `is_raid=true` 会自己点 `ChangeToRaidDifficulty` 切难度**，不需要
前面垫一条普通。上一版「普通 → 突袭」两条，等于每关白烧一次理智，用户看到
「说好只排突袭，结果还是挂普通」就是这个。

现在的口径（`skills._prepare_ops`）：

- 默认 `raid='auto'`：**只排普通**（2026-11-23 改）。原先 auto = 「有突袭作业
  就只排突袭」，出事经过：用户说「帮我挂 OS-S 关 1 到 5 普通」，助手没传 `raid`，
  5 关全排成突袭，第二关还因为突袭作业要「圣聆初雪」编队失败卡了 20 分钟。
  而且突袭本来要先通关普通才解锁，首次打某关排突袭大概率点不进去。
  **要突袭必须明说**：`raid='raid'` 或逐关 `@raid`。
- `raid='raid'`：只打突袭（用户明说要突袭时）。
- `raid='both'`：普通 → 突袭两条（普通还没通关、要一次打通时用）。
- `raid=False` / `'normal'`：只排普通（刷材料）。

> **难度只照用户原话**：用户说「普通」→ 必须 `raid='normal'`；助手不许以
> 「复刻活动」「应该打突袭」为由替他升级。`skill_fight` 排完单会单独打一行
> 「难度：普通 OS-S-1、OS-S-2 …」，是给用户对照的锚点。
- **逐关指定**：`stages` 支持 `SV-EX-1@raid` / `SV-EX-1@both` / `SV-EX-1@normal`，
  也支持 `{'stage': 'SV-EX-1', 'raid': 'both'}`（见 `_parse_stages`）。这是为了
  让助手能按计划执行（「EX-1~5 只打突袭、EX-6/7 普通+突袭」），以前只有全局
  开关，助手答应了也做不到。
- `maaplan.prepare_copilot` **不再给突袭条目自动补普通**（旧兜底已删）。
- `copilot.entry_variants`：选了哪一行就写哪一行，不再被作业自身的难度位标志
  改回普通。
- **磨难 / 险地不是突袭**（用户明确）：主线 10-14 章磨难（`tough_`）和 15+
  章险地（`#s`）是独立关卡 id、各自一条，不展开、不套突袭规则。

## 逐关战报 + 失败换作业重挂 + 分段扫图（2026-11-19）

> **2026-11-22 修订：①「成功几关」这一项已经删掉。** MaaCore 在 Copilot 任务里
> **不发 `StageDrops`**（本机 asst.log 里 255 条 `SubTaskExtraInfo` 全是
> `CopilotAction` / `BattleFormation` / `CopilotListLoadTaskFileSuccess` /
> `ExceededLimit`，`StageDrops` 0 条），所以 `_mark_clear()` 永远不触发、
> `ok_list` 恒为空，界面就一直是「一关都没挂」。按用户口径「判断不出来就别统计」，
> 现在 `_mark_clear()` 是个空实现（保留函数名 + docstring 说明原因），
> `run_core` 不再返回 `stages_ok`，改为 `stageReport: 'trail'` +
> `stage_trail`（`CopilotListLoadTaskFileSuccess` 记的「跑过哪些关」）。
> 下面 ① 的原文留着当历史，别照着它去「修」成功数。
> 失败那一半仍然有效：`_tally()` 只在有 `TaskChainError` 时记失败。

用户提了四条，一次做完：

1. 作业翻车要能看见**是哪一关**翻的
2. 挂完一关就显示「一关战斗成功」（**已废，见上**）
3. 某关失败就换作业集里**别的作业**重挂这一关，已成功的关不重排
4. skill 联动：挂完普通 → 导航到突袭 → 挂突袭 → 导航到 S → 挂 S

**① 逐关结果（`run_core` 的 `state['results']`）**

- ~~`StageDrops` 到达 = 这关打完了 → `_mark_clear()` 记成功~~（Copilot 不发，
  见上面的修订）
- `chain_failed` 且链是 Copilot → `_mark_fail()` 打
  `✘ SV-EX-1 翻车：原因`。`maacore._translate` 的 chain_failed 事件为此补了
  `why` 字段（以前只有一句「失败：自动战斗」）。
- `CopilotListLoadTaskFileSuccess` → `state['trail']` 追加关卡名，收尾打
  「跑过的关：…」。「打过哪些关」用它，别用掉落。

**② 换作业重挂（`_retry_failed`）**

> **2026-11-22 二次修订：改成「只换翻车那一关」。** 用户报「我刚刚确实是 2 星，
> 但是 agent 并没有帮我换作业重新挂」，要求「回到对应的关卡界面，更换作业集
> （只改那关，其他不变，帮我重新挂），然后任务继续」。查下来有**两个**独立原因：
>
> 1. **翻车根本没被识别**：`_mark_fail` 要 `state['stage']` 才知道哪一关翻车，
>    而这个值只由 `CopilotListLoadTaskFileSuccess` 设置——那个回调码当时写成
>    70003（MaaCore 4.x），6.x 是 20003，事件被丢，`stage` 永远空，于是
>    `_mark_fail` 直接 return、`results` 为空、重挂条件不成立。同一次的总根因，
>    见「回放真实回调流查『日志还是没有』」那一节。
> 2. **重挂的触发条件太窄**：`if state.get('results') and spec.get('retry')`。
>    `_prepare_ops` 只在「某关有 >1 份候选」时才给 `retry`，所以每关只有一份作业
>    的批次，翻车后什么都不做。

现在的行为：

- **触发**：只要 `state['results']` 里有 `ok=False` 且这批有 `copilotList`，
  就进 `_retry_failed`——不再要求 `spec['retry']` 存在。
- **只动翻车那一关**：候选池取该关「下一份没试过」的；**翻车关之后还没打的关
  沿用原来那份**（旧实现会顺手把它们也换成下一份，白冒风险）。已打完的关
  （`start_idx` 之前）一关都不重打。
- **没别的作业就同一份再试一次**：漏怪是偶发的，2 星重打一次经常就三星。
  以前「候选都试过」直接收工、把后面几关一起丢下。同一份只补试一次
  （`state['same_retry']`），试过还不行就把这关记进 `state['skip_retry']`
  跳过、继续打后面的关。
- **不值得重挂的原因直接跳过**：理智不够 / 关卡没解锁 / 作业与关卡对不上，
  重挂也是同样结果，别烧时间。`state['pause']`（材料不足）直接不重挂。
- **每轮按最新结果继续**：每轮结束重看 `_tally(state['results'])[1]`，哪关还失败
  才继续；都过了立刻收手。旧实现每轮都从同一个起点重排，第二轮会把打过的关
  再打一遍（白烧理智）。
- **「上一关过了」要有记录**：MaaCore 在 Copilot 里不发 `StageDrops`，唯一可靠
  的成功信号是「换到下一个作业」。所以 `CopilotListLoadTaskFileSuccess` 里看到
  `prev != code` 时，除了打 `✓ X 完成`，还往 `results` 写一条 `ok=True`——
  不写的话 `_tally` 只看到失败，重挂成功一轮后还会再挂一次。
- **翻车原因不再说「MAA 没给原因」**：MaaCore 的 `TaskChainError` 只有
  `taskchain`/`taskid`，`details` 是空的。拿最近一条 `SubTaskError` 推：
  `Copilot@WaitUntilEndOfAction` → 「结算不是三星（漏怪 / 作业没打过这关）」。
  用户看到这句才知道「为什么要换作业」，而不是以为程序坏了。

其余机制不变：

- `skills._select_ops` 每关挑 `depth=3` 份候选（按「可直接抄 → 借一人 →
  练度不足」排），`_prepare_ops` 返回 `(copilotList, retry)`，
  `retry = {'pool': {'SV-EX-1@raid': [41623, 41624, 46114]}, 'maxRounds': 3}`。
- 同一个 Engine 直接 `append` 新的 Copilot 任务再 `start`——不重新 open /
  connect / 导航，省掉每轮十几秒。`tried` 记着试过哪些，#102→#202→#102 这种
  来回打转不会发生。
- **翻车判定只认 MaaCore 回调**（2026-11-22 用户明确要求：「失败识别应该以
  maa 的失败通知为准，而不是自己识别有没有失败」）：`chain_failed`
  （`TaskChainError`）或子任务失败（`Copilot@WaitUntilEndOfAction` = 结算不是
  三星）→ `_mark_fail` 记一条 `results[ok=False]` → `_retry_failed` 才动手。
  截图只用来**看界面在哪**，绝不用来猜「是不是失败了」。
- **重挂前先把界面弄回活动关卡界面**（`_leave_result`，是 `_retry_failed` 里的
  **硬前置**——返回 False 就这一轮不排，绝不把作业丢在结算页上白跑）：
  用户 2026-11-22 实测「**失败后点屏幕任何位置都会立刻回到关卡界面**，在那个
  界面重新运行作业集就能换作业，不需要重新唤醒登录，也不需要写导航」。
  所以流程只有「点一下 → 看现在是不是活动关卡地图」：
  1. 点 MAA 自己的 `ClickCorner`（`_RESULT_TAP = (1135, 45)`，1280 口径）。
     任务定义写明「点屏幕右边不会点到掉落物品，也不会关掉关卡选择」，所以
     结算页上能推进，已经在地图上点了也不误触（真机验过：在地图上点完界面
     不动）。
  2. `stagenav.probe_bytes() == 'map'`（底部 tab 暗）就算回到了活动关卡界面。
     **第一帧不判**：结算页底部也是暗的，probe 会把它当成地图，于是「还没点
     就放行」，作业排在结算页上白跑一整轮。点过一次之后结算页必然消失，probe
     才有意义。
  3. 点三次还不是地图（掉到主页 / 基建 / 主题页）→ `pages.to_home()` + 完整
     导航（`_navigate`）兜底。
- **不再有「结算页识别」**：先按两颗亮星裁模板（`result_star1/2.png`）被用户
  否掉——三星 / 一星 / 直接失败都停在同一个结算页，只有星亮几颗不同；后来
  改成「行动结束」标题 mask 也被否掉（仍是自识别）。现在 `pages.py` 只认
  主页 + 基建确认框 + 退出游戏确认框（后者点 ✕ 取消，不是失败识别），
  翻车界面靠「先点后判」。
- 收尾判失败只从 `eng.chains[mark:]` 看，否则第一轮的失败会把重试成功盖掉。
  `mark is None` 表示压根没重试（**0 是合法分界**，别拿它当「没重试」）。

**②a 跨分区必须分段导航（2026-11-22 晚）**

用户实测：「挂完 OS-10 后，没有切换我们的导航到 EX 的关卡界面，就直接挂
OS-EX-1 了，导致 MAA 一直找不到 OS-EX-1」。

根因：`run_core` 原来只在开工前导航**第一关**，然后把整个 `copilotList`
当成一条 Copilot 任务一次性交给 MaaCore。MaaCore 在多关之间只会
`ClickStageName` 在**当前这一页**上 OCR 找关卡名——它不会点活动地图上的分区
节点（圣巡之旅 / 变革之路 / 喀兰之心）。所以基础区最后一关打完后，界面还停在
基础区列表，EX 关根本不在这一页上，它会一直找，整轮卡死在那儿。

现在 `_run_tail` 把作业按「活动前缀 + 分区」切段（`segments_of`），段与段
之间：

1. `_leave_result` 点掉上一段末关的结算页（顺序不能反，见上一条）；
2. `_navigate` 到下一段首关所在的分区（多花 10~15 秒，用户 2026-11-22 明确
   接受这个代价）；
3. 再把这一段 `append` + `start`。

几条硬规矩：

- **只合并连续的同类，绝不重排**：顺序排成 OS-1 → OS-EX-1 → OS-2 就老实切
  三段来回跑。重排等于替用户改打关顺序，出了问题全是没法解释的疑问。
- **分区口径和 `eventnav.run` 完全一致**：`st`（剧情关）/`f`（额外关）都归
  基础区（`_zone_of` 里的白名单），否则会切出「没标定过分区节点」的段，
  导航必然失败。
- **段间导航失败不许 raise**：前面几关已经打完了，raise 会把整轮记成失败。
  打一行「警告：没导航到 X，这一段的作业可能全找不到关卡」照样排下去。
- **首段能不能复用已有导航**看 `can_reuse_nav`：只有「刚导航完、中间没跑
  别的任务」才复用；抄作业前面排了收菜/招募，界面早就不在关卡列表上了，
  必须重新导航。
- **直跑模式（`noNav`）同样分段**：`skill_fight` 默认 `wakeup=false` 就是这
  条路，`run_core` 原来在 `noNav` 下把整个队列一次性 `append + start`，根本
  走不到分段。现在 `noNav` 也交给 `_run_tail`，只是首段 `can_reuse_nav=True`
  ——「别重复唤醒/导航」的意思是别从头再走一遍，不是「打不到也不许点分区」。
- `_retry_failed` 的重挂同样分段——翻车在 OS-10、重挂往后接 OS-EX-1，
  是同一个坑。

**②b 关卡名单核对（`skills._tidy_stage_list`，2026-11-19）**

用户说「SV-EX-1 ~ SV-EX-6」，助手实际排的是 `SV-EX-2 ~ SV-EX-7`——跳掉
EX-1 之后又往后补了一关凑数，白烧一整轮理智。预检会把前置关补进计划，所以
计划本来就可能比用户点名的多；这里按白名单收口：

- 允许 = **用户点名的关** ∪ `stageplan.expand(asked, with_prereq=True,
  fill_base=True)`（游戏内前置关）；
- 名单外的关一律剔掉并打日志；剔空了直接报错；
- 用户点名却不在计划里的关也点名（那是预检判定打不了，上面已有原因）。

**③ 分段扫图（`skill_sweep`）**

- `_sweep_segments` 把活动拆成「普通关 / EX 普通 / EX 突袭 / S 关」。老活动
  没有 S 关就只有三段；`tiers` 可以只挑一段，`raid` 控制 EX（both/raid/normal）。
- 每段开始前用 `skill_navigate` **重新导航**：EX 和 S 是活动地图里不同的
  分区，MAA 的 Copilot 只认当前分区上的关卡名（见 `_run_fight_batch` 的注释）。
  2026-11-22 起 `run_core` 自己也会按分区切段（见 ②a），这条只剩「sweep 想
  按 tiers 分组算作业」的意义。
- 段内 `_select_ops(strict=True)`：普通段不拿突袭作业顶，反之亦然。
- 每段一次 `run_core`（带 retry 池），段与段之间靠导航衔接。

## 多 MAA / 多模拟器 + 一条龙（`plan_groups` / `run_batch`，2026-11-22）

用户的原话：「每个账号都有自己的 maa 路径和配置文件，agent 要能读多个配置文件下的
adb 路径；不同就多开并行；同一台就切号 → 普通 → 突袭 → S → 领奖 → 商店 → 下一个号」。

**① 每个账号自己的 MAA 目录 / 档案**

账号 json 的 `maa` 段现在有两个字段（都放在 `account['maa']` 里，别往别处加）：

| 字段 | 含义 | 空的时候 |
|---|---|---|
| `dir` | 这个号归哪份 MAA 目录管 | 回落 `settings.maaDir` |
| `profile` | 用哪份配置档案（`moren` / `136`…） | 回落 MAA `gui.new.json` 的 `Current` |
| `adbAddress` | 这个号钉在哪台模拟器（`emulator-5556`） | 回落档案里的 `Address` |

解析只有一处：`acctbind.dir_of(file)` / `acctbind.profile_of(file)` /
`acctbind.addr_of(file)`。`skills._maa_dir_of(file)` / `_conn_of(file, d)` 都走它，
所以唤醒、导航、战斗、培养、扫图、牛杂**每一项**都会连到「这个号自己那台模拟器」
上，不再一律用全局那台。`envcheck.ensure(..., maa_dir=, profile=)` 也跟着改，
判断模拟器开没开、要不要替用户启动，看的都是这个号的那份配置。

**①b 地址钉死 + 客户端类型（2026-11-23 两个「不听命令」的根因）**

用户报「明明指定 5554 挂活动，却会影响 5556 那台」和「启动账号不看 b 服还是官服」，
查下来是两处独立的问题，都出在连接这一段：

1. **地址兜底会偷偷换台**。`envcheck.adb_now` / `stagenav.Touch.ready` 原来都是
   「档案地址连不上就挑 `adb devices` 里第一台在线的」。档案里写着过期的 5554、
   实际开着 5556 时，任务就自己跑去连 5556——在用户看来就是「串台了」。
   现在按**地址来源**决定能不能改主意（`skills.device_of`）：

   | 来源 | 那台不在线时 |
   |---|---|
   | 聊天里点名 / 账号 `adbAddress` | **绝不自愈**，直接停下并列在线设备 |
   | MAA 档案 `Address` | 只有一台在线才顶上（地址过期很常见，无歧义） |
   | 都没写 | 一台在线用它；多台在线停下要用户指定 |

   `skills.run_skill` 和 `_run_group` 起跑前都会先过这一层，把解析结果用
   `_set_addr` 钉到线程上，`run_core` / `run_scatter` / `run_custom` 全按它连。
2. **客户端类型没落到任务参数上**。这块踩了两次，一次比一次深，记清楚：

   | 参数 | 认的是什么 | 写错的症状 |
   |---|---|---|
   | `AsstConnect(adb, address, config)` 第 4 个 | **模拟器连接配置名**（`General` / `LDPlayer` / `MuMuEmulator12`，就是 `resource/config.json` 里 `connection[].configName`） | 传 `Official` / `Bilibili` → `ConnectionInfo why=ConfigNotFound` + `ERR config  Official not found`，MAA 退回 `General` 才连上（能用，但每次先报一次错，且丢了模拟器专属的 adb 调优） |
   | 任务参数 `StartUp.client_type` | **客户端类型**（`Official` / `Bilibili` / `YoStarJP`…） | 传成 `Official` → `am start -n com.hypergryph.arknights/...`，b 服账号去启动**官服包**、在官服账号列表里找 b 服昵称，表现就是「认成官服了」 |

   两个参数长得像，**混一次就是一轮空跑**。现在的口径：

   - `maaplan._conn_for(spec)`：`config` 只取档案的 `ConnectSettings.Config`
     （空则 `General`），客户端类型放在 `conn['client']` 里只用于人话日志
     （`连接 emulator-5556（b服）`）
   - `maaplan.resolve_client(spec)`：客户端类型的**唯一判定入口**。优先级
     `spec['clientType']`（`Bilibili` 或 `bilibili服` 都吃）> `spec['channel']`
     > `spec['accountFile']` 里的 `account.channelName` > `Official`
   - `maaplan.core_tasks` 用 `resolve_client` 填 `StartUp.client_type`

   光有判定入口还不够——2026-11-23 用户第二次报「还是把我 b 服账号认成官服的」，
   asst.log 实证 `append_task StartUp {"client_type": "Official"}`。根因是
   `skill_fight` 建 spec 时只从 `skills._profile_kw()` 里取了 `profile`，同一份
   字典里的 `clientType` 被丢掉了，`core_tasks` 拿不到只能兜底。现在 spec 统一
   用 `skills._spec_kw(file)` 展开（`profile` + `clientType` + `adbAddress` +
   `accountFile`），并且 `resolve_client` 最后那层能拿 `accountFile` 回查渠道，
   **以后新加一条跑机入口漏传也不会再按官服跑**。

设置界面对应「账号 → 模拟器」下拉（`/api/account/devices` 返回 `devices` 在线设备
+ `instances` 本机实例，没开的也列，否则那台关着时用户没得选），助手侧对应
`bind_emulator` 工具——两边写的是同一个 `maa.adbAddress`，不靠谁记得传参。

**② 分组：同模拟器串行，不同模拟器并行**

```python
# maaplan.plan_groups(rows, spec_base)
key = (os.path.normcase(os.path.abspath(d or '.')), str(conn.get('address') or ''))
```

- `d` 是这个账号的 MAA 目录，`address` 是那份档案里 adb 指向的模拟器地址
  （雷电 `emulator-5554` / MuMu `127.0.0.1:16384` 之类）；
- 同一个键 = 同一份 MAA + 同一台模拟器 → **组内串行**（两条 adb 同时戳同一台
  会表现成「点了没反应」）；
- 不同键 → 每组一个**独立进程并行**，这就是「多开」。

`run_plan` 只在单组时保持原来的同步行为，多组才开线程（每组再起子进程），
日志按组汇总。

**②b 多开必须「一进程一台模拟器」（2026-11-22 真机定位，这是「另一台不动」的
最后一层根因）**

分组、档案、地址全对了，第二台还是不动。真机对着两份 `debug/asst.log` 才看清：

```
[ERR] CreateOverlappablePipe failed, err 231
[WRN] Call ` D:\...\adb.exe devices ` failed
[INF] ConnectionInfo ... "address":"emulator-5556",
      "why":"Cannot connect: address appears to be serial number but device not found"
```

`err 231` = Windows「所有管道实例都忙」。MaaCore 调 adb 时用命名管道收输出，
**同一个进程里两个 MaaCore 实例撞同一个管道名**，后到的那次直接失败，于是
MaaCore 认为设备不存在。跟档案、地址、adb 路径都无关——5556 单独跑（只起一个
Engine）一切正常，两台同时跑就只有 5554 能动。

所以多开只能一个进程一台模拟器：

- `backend/core/multiproc.py`：写 payload → `MAA-PRTS --skill-worker <payload>`
  （源码模式跑 `python backend/main.py --skill-worker ...`）→ tail 子进程的
  日志文件转发进任务日志 → 读回 `{results, report}`；
- `backend/core/skillworker.py`：子进程入口，`kind` 分 `skill`（run_batch 一组）、
  `plan`（run_plan 一组）和 `one`（run_skill 单跑，见 ②e），日志落到父进程
  指定的 `debug/tasks/<tid>-g<i>.log`，任务结束删 payload / 结果，日志留着排障；
- `skills.run_batch` / `maaplan.run_plan` 在 `len(groups) > 1` 时全部走子进程；
  单组仍就地跑（不起进程，省 1~2 秒，日志即时），**除非本进程已经有一路在跑**
  （见 ②e）。

冒烟 B52 里 `multiproc.run_group` 被换成「就地执行」——否则测试里的假 skill
不会出现在子进程里，会真的去连模拟器。

**②e 一路一个进程：后开的那路必须让位（2026-11-23）**

用户报「我多开另一个号，他把我前面的停了」。两份日志对起来看：5556 那组
12:59:50 开跑，13:01:37 另一个号**在同一个进程里**开了第二个 MaaCore，13 秒后
5556 的 StartUp 链就自己「结束」了——`asst.log` 里只有 `Stop | block` 和
`~Assistant`，没有 `TaskChainError`，上层看到的是 `TaskChainStopped`，于是就写成
「已停止（你按了停止）」，用户当然以为是新任务把旧的停了。

根因不在「停止」，在**同进程第二个实例**：`AsstSetUserDir` / `AsstLoadResource` /
`AsstCreateEx` 动的是 MaaCore 的**进程级全局资源**，第二个实例一 open，
第一个正在跑的链就被打断。err 231 那条（②b）只是它的另一种表现。

落地成三件事：

- `maacore.claim_inproc()` / `release_inproc()`：本进程「跑机名额」只有一个。
  `skills.run_skill`、`skills.run_batch`（单组）、`maaplan.run_plan`（单台）开跑
  前抢，抢不到就把这一路交给独立进程（`kind='one'` / `'skill'` / `'plan'`），
  抢到的用 `try/finally` 还回去；
- `maacore.Engine.open()` 兜底：本进程已经有活着的实例就直接抛
  `EngineError`（「多开必须一路一个独立进程」），宁可这一路明着失败，也不许
  把已经在跑的那一路拖死；
- `Engine.stopped_by_user`：只有 `stop_live()`（点名停）会置位。超时收尾自己
  调的 `eng.stop()` 不再冒充「你按了停止」——`run_core` 的 `timed_out` /
  `paused='手动停止'` 都改看这个标记。顺带把「等了 %d 分钟」改成**实际耗时**
  （以前打的是时间预算，跑两分钟也能写「等了 60 分钟」）。

客户端这边也收紧了 `/api/run/stop`：既没 `file` 也没 `all=true` 直接回错误，
不再默认全停——右栏那个按钮没选中账号时就是这种情况。

**②c 官服 / b 服是两个包（2026-11-22）**

b 服账号的渠道在 `snapshot['account']['channelName']` 里，值是 **`bilibili服`**
（不是 `bilibili`）。以前两处都读错：

- `skills._client_of()` 读的是**顶层** `channelName`（永远空）→ 一律当官服；
- `stagenav.game_package()` 只认 `('bilibili','b','b服')`，`bilibili服` 两边
  都不等 → 返回官服包名。

结果 b 服那台模拟器上 MAA 去启动/查找**官服**客户端，登录失败后进入
「game-restart loop」，表现还是「另一台没动静」。现在：

- `_client_of` 先读 `account` 段，再退回顶层；
- `game_package` 按「含 bili / b服」判，b 服走
  `com.hypergryph.arknights.bilibili`；
- `_profile_kw()` 顺带带 `clientType`（`maaplan.client_type_of('bilibili服')`
  → `Bilibili`），所有 `run_core` 的 StartUp 都按账号自己的渠道跑。

**②d 右栏日志按账号分栏（2026-11-22）**

用户两条反馈一起处理：「右边日志都消失了」+「每个账号的日志只在自己的
tab 里」。

- **日志消失的根因在序列化**：给任务加日志落盘时把打开的文件句柄塞进了
  任务 dict，`api_task` 直接 `json.dumps(tasks.get(...))` →
  `TypeError: Object of type TextIOWrapper is not JSON serializable` → 接口
  500 → 前端每 700ms 收一次失败，右栏只剩一行任务名。现在内部字段一律
  下划线开头（`_fh` / `_prefix`），`_public()` 白名单只吐 9 个可序列化字段。
  所有创建点都补了 `owners`：`run_batch` / `run_skill` / `run_plan` /
  `multiacct` / `/api/multi/run`，单账号任务只挂在那个号名下。
- **分栏靠「任务 owners + 行前缀」两级**：`tasks.for_owner(file)` 先挑出这个号
  相关的任务（含 `owners` 为空的全局任务），前端 `chatSideSwitch()` 切号时
  拉 `/api/tasks?file=` 回填；`paintSideLog()` 再逐行按 `sideOwnerOf()` 过滤
  ——`[昵称] xxx` 归那个号、`[组 xxx]` 和没有前缀的汇总行是公共行、
  匹配不上的前缀跟着「发起这个任务的号」走。单账号任务整块跳过别的 tab，
  连前端合成的「✓ 完成」也不会留下一个孤零零的任务标题。
- **多开两组的日志会被 tail 到同一个任务里交错**，所以子进程写日志时就要
  带前缀。`tasks.create(prefix='[昵称]')` → `tasks.log()` 里的 `_decorate()`
  统一补：无前缀行补上、`[组 emulator-5556]` 改写成 `[昵称]`、已有
  `[昵称]` / `[MAA]` 原样。挂在 `tasks` 这一层而不是包装 worker 的 `log`
  函数——`maaplan.run_core` 是**直接** `tasks.log_factory(tid)` 写「MaaCore
  已加载 / 模拟器已连接」的，包装拦不到。一组多号时不硬塞（每行本来就有
  各自的前缀）。真机回来两组日志每行都带 `[昵称]`，官服 tab 看不到 B 服。

**③ `skills.run_batch(accounts, steps)`：一条龙**

不写死流程，`steps` 是字符串列表，每项「skill 名 + 空格参数」：

```python
['wakeup 5505', 'navigate OS', "fight OS-EX-1~7", 'claim', 'tool 活动商店']
```

- `_norm_steps()` 把每项拆成 `(name, params)`：`fight` 后面那串当关卡/关键词，
  `tool` 后面当工具名，`wait 60` 当等待；别名（`领奖` / `商店` / `导航` /
  小游戏名）都认。
- 每个账号把整串 steps 依次跑完；一个账号挂了不影响别的账号（各自 try/except），
  最后汇总 `{ok, done: [...], failed: [...]}`。
- 日志前缀 `[昵称]`（昵称取 `snap['account']['nickName']`，不是文件名——
  第一版写错了，日志里全是 `.json`）。
- 外层由 `tasks.create('批量任务（N 个账号 / M 台模拟器）')` 挂成一条任务，
  前端右侧进度栏只有一个进度条。多组时每组一个**独立进程**（见 ②b），
  父进程那条线程只负责 tail 日志、收结果，包了 try/except，崩了往日志写
  「这一组出错」——组线程静默死掉，用户看到的就是「另一台没动静」。
- 每个任务的日志**同时落盘** `debug/tasks/<tid>.log`（`tasks.py`）：以前只在
  内存里，窗口一关现场就没了，用户报「只有一台在跑」时连哪组没启动都查不出。

**③b 地址要真的传进 MaaCore（2026-11-22 修，这是「第二台不动」的根因）**

分组对了不等于连得对：`run_core` / `run_custom` 原来只用
`_connection(maaDir, spec['profile'])`，而各 skill 的 spec **没写 profile**，
于是永远拿 MAA 的 `Current`。用户八份档案全指向 `emulator-5554`，b 服那份在
另一台 MAA 目录里（`...-win-x64-1`，`Current` 指向 5556）——只有 b 服那组碰巧
连对，别的全挤到同一台。

现在：

- 每个 skill 的 spec 都带 `'profile': acctbind.profile_of(file)`（`_profile_kw`）
- `run_core` / `run_custom` 认 `spec['adbAddress']`，覆盖档案里的地址
- `run_batch(..., emulators={账号: 'emulator-5556'})` 用线程局部 `_ADDR` 把地址
  钉在组上，`_conn_of` / `_addr_kw` 都读它
- `stagenav.Touch(strict=...)`：指定过地址的号，adb 自检连不上就**报错**，
  不再「自动换第一台在线设备」——换了等于两组同时戳一台，另一台一动不动
- `envcheck.ensure(address=...)`：多开时按点名那台的**实例序号**启动它（不在线
  就拉起这一台，只等这一台上线），绝不换成档案里那台；见「开跑前的环境闸 →
  多开：启动哪一台由地址说了算」

**④ 领奖 skill（`skill_claim`）**

`eventnav.claim_reward(touch, prefix)` 早就有（进奖励页点「一键领取」），但只能
手动点。现在包成 `SKILLS['claim']`：

```python
skill_claim({'account': ..., 'stage': 'OS', 'wakeup': False})
```

纯 adb 操作（不连 MaaCore），所以它不在 `_WRITE_HANDLERS` 里——和 `wakeup` /
`navigate` 一样免确认。奖励页的「一键领取」按钮已领完也还在，点了没反应不算失败。

## 暂停要真的停住（2026-11-24）

用户按「停止」后，日志已经写了「已停止（你按了停止），不再往下排」，可扫图
第 2/3 段还是把 EX 的作业一份份排了上去。不是 `AsstStop` 没生效——那一条链
确实停了——而是**上层的循环不知道用户按了停**：

- `skills.skill_sweep`：base → EX → S 是一段一段调 `run_core` 的，段与段之间
  只看时间预算，不看「用户是否叫停」；
- `skills._run_group`（多账号排队）：一个号跑完接着下一个号；
- `skills.skill_weekly`：一批战斗打完接着下一批；
- `maaplan._retry_failed`：翻车后无条件换作业重挂。

`run_core` 返回值里其实已经有 `paused == '手动停止'`（来自
`Engine.stopped_by_user`），只是上层没人看。

修法：**用户按停止时先按账号记一笔**（`maacore.request_stop`，`stop_live`
的第一条语句），引擎照常叫停；然后上面四处循环在段/步开头查
`maacore.stop_requested(file)`，命中就收手。

三个必须的细节：

1. **按账号判**（`owner_match`）：停甲号不许把乙号一起停——用户口径是
   「停这一路，别的账号没动」。全停只有 `stop_all_live` 会给 `'*'`。
2. **开工前清标记**（`maacore.clear_stop`）：`run_skill` / `run_batch` 一开跑
   就清这一路的旧标记，否则上次停了之后这一路永远开不起来。冒烟夹具
   `_reset_config` 也要清，不然 B61 停过的标记会串到 B52/B53。
3. **`stop_live` 先记账再叫停**：用户按的时机常常正好在「段与段之间」，
   那会儿一个活引擎都没有（循环一个都停不到），只靠 AsstStop 的话下一段
   照排。

顺带修的两句日志：`run_core` 手动停止时不再打「跑了 X 分 Y 秒还没结束」
（那是超时的话术）；翻车重挂入口先看 `eng.stopped`，用户停了就不换作业。

冒烟 B66 钉住：标记按账号隔离 / 全停 / `stop_live` 记账 / 多账号排队时停一个
号不波及下一个号（甲号 1 步即停、乙号 3 步跑满）。

## 页面识别与回主界面（`pages.py`，2026-11-22）

用户报「判断不出游戏停在哪一页」：以前只有 `stagenav.at_home()` 一套整屏
统计特征（底部白占比），认得主页，认不出设置页 / 基建 / 退出确认框，
「回主界面」只能盲按返回键，撞上基建的「是否确认离开罗德岛基建？」就卡死。

- **主页判据换成图标定点比对**：左上角「设置齿轮 + 邮件」两个模板
  （`backend/core/navtpl/pages/home_gear.png` / `home_mail.png`，1920 口径从
  真机原地裁 56×56，中心 (48,32) / (188,32)）。两个**同时**命中才算主页：
  设置页左上角也有齿轮（只是更靠右），只要一个会误判。实测主页 0.0 分、
  活动地图齿轮 27.6（阈值 14）。`at_home()` 优先走它，模板缺失才退回老特征。
- **基建确认框**：底部横贯整屏的按钮条，左黑「✕」（取消）/ 右暗红「✓」
  （确认）。判据同样是**定点比对**：`backend/core/navtpl/pages/confirm_cancel.png`
  / `confirm_ok.png`（1920 口径从真机原地裁 72×72，中心相对坐标 0.346/0.687
  与 0.658/0.688），两个圆钮**同时**命中才算，返回右半「确认」的中心
  （1920×1080 实测 (1264,743)，可以直接 tap）。
  为什么不用「按行数红色像素」（2026-11-22 曾这么写）：这条暗红按钮带一层
  斜纹贴图，红像素是散点——同一行红占比只有 35%~60%、最长连续红段仅 30px，
  于是关卡列表 / 主页的红色角标也能凑够数，真机日志里冒出过一次假弹窗。
  换模板后主页 0.0、其余页面 85 分以上。打包漏了模板才退回
  `_bar_bytes()`：按「左半很暗 + 右半偏红」的色彩签名找按钮条（`_BAR_*`）。
- **`to_home(touch, log)`**：每轮先看一帧 → 主页就停；认到**退出确认**就点左
  边「✕」并直接当到家（见下条）；认到基建确认框就点「✓」（返回键对这个弹窗
  无效）；剧情页跳幕；否则前 3 轮返回键、再 3 轮 ESC、最后点左上「<」。
  `eventnav._to_home` 已改成直接调它。
- **「是否确认退出游戏？」必须点 ✕，不能当基建框点 ✓**（2026-11-23 用户报障：
  「S 关入口没认出来 → 一路按 ESC → 主界面也没认出来 → 停在退出确认 →
  继续按 ESC，游戏直接退出了」）。两种弹窗**按钮条一模一样**（左 ✕ 右 ✓），
  但点 ✓ 就是替用户退出游戏。区分靠**背后亮不亮**：退出确认只在主界面弹，
  背后是模糊掉的主界面，按钮条上方（0.10~0.26 屏高）平均亮度 125~155；基建
  确认背后是基地，是暗的。判据 `exit_confirm_bytes()` = 按钮条命中 **且**
  该段亮度 ≥ `_EXIT_LUM`(100)，认到返回左半「✕」的坐标（0.3464, 0.6870）。
  阈值故意压低：把基建框误认成退出框只会少退一层（✕ 是无害的取消），反过来
  就是把游戏关掉。`to_home` 点完 ✕ **直接 return True**（这个框只在主界面弹，
  取消完必然还在家），不再补返回/ESC 把它弹回来；`stagenav` 那条回主页的老
  循环（曲谱导航前）也加了同一道闸。


## 当期活动导航（`eventnav.py`，2026-11-19 / 2026-11-22 改）

新活动的入口 UI 和老的「曲谱 / 主界面活动大图」都不一样了，用户逐屏标注了
步骤，这一段按他的步骤点：

    任意界面 --底部第1个tab「终端」--> 主界面
    主界面   --固定位置点「终端」大卡---> 终端页
    终端页   --左下横幅（当期活动）-----> 活动主页（约 5 秒过场动画）
    终端页   --ATTENTION 区活动卡片-----> 活动地图（已结束的旧活动走这条）
    活动主页 --节点（圣巡之旅）--------> 普通关卡界面（基础区）
    活动主页 --节点（变革之路）--------> EX 关卡界面
    活动主页 --节点（喀兰之心）--------> S 关卡界面

到关卡列表就交给 Copilot 的 `ClickStageName` 自己 OCR 找关卡名。

**分区是地图上的节点，必须直点**（2026-11-22 修）：`run()` 现在只点目标那一个
节点（`OS-1` → 圣巡之旅、`OS-EX-1` → 变革之路、`OS-S-1` → 喀兰之心）。以前是
「先点基础区进关卡列表、再点右下角区域按钮切 EX/S」——那是**上一代 UI** 的
做法，在新地图上第一步就离开地图了，EX/S 的模板根本不在关卡列表那一页，
`_find` 认不到就按标定坐标兜底点，那一戳可能正好点在关卡卡片上。
`goto_stage_list` 也**不再退回老路径**：ENTRIES 里的活动，eventnav 失败就
直接返回「当期活动导航没到位：…」，因为老 UI（主界面大图 → 主题页 → 区域
列表）对新活动根本点不到，退化过去只会白点几下再报个更难懂的错。

**两种入口并存**（2026-11-22 用户实测）：正在开的活动在终端页**左下横幅**上
（`<雪山降临1101>活动已开放`）；活动结束之后横幅会被下一个活动占走，旧活动
退到 ATTENTION 区第二格卡片、显示「作战已结束」，点卡片照样能进地图——但
地图上的关卡分区已经关了，只剩商店（见下面的 `closed`）。

**为什么不吃老路径**：老路径是「主界面大图 → 主题页 → 右下区域列表」，
新活动的地图是散点式区域节点、分区靠地图上的节点图标切，老坐标
（`themeZone` / `zoneRows`）全对不上。`goto_stage_list` 里 eventnav
**排在曲谱和老路径前面**，认不了才退回去。

**起点可能是任何一页，别盲点「终端」大卡**（2026-11-22 修）：连跑多关时，
每关导航前游戏停在**上一关的关卡列表**（或结算页）上。`terminalCard` 这个
固定坐标是按主界面量的（1280 口径 913,173），在关卡列表上正好压在关卡卡片
上——一戳就打开关卡详情，接着点底部「终端」tab 又戳到列表底部，连跑几个号
就会莫名多进一次关卡。现在的收敛过程分两段：

- `_near_map()`：认得到基础区节点就说明**已经在活动地图上**，直接用（连跑
  多关时省掉回主界面那一路，实测 22.8 秒降到 7.8 秒）。认不到但 `probe` 判成
  `map`（关卡列表）就先按一下返回——这一下反正是回主界面的必经步骤，之后
  可能停在活动主题页（不是地图，节点位置低一截、模板认不出）。
- `_to_terminal()`：`at_home()` 判出「在主界面系」才点终端大卡；否则交给
  `_to_home()` 一层层退（它会跳剧情、退化到 ESC、最后点左上角「<」）。
  **返回键绝不能无条件按**：在主界面上会弹「退出游戏」确认框。

**数据在 `ENTRIES`，一条活动一项，老的别删**（活动会复刻，UI 不变、模板还能
用）。两项长这样（OS 是复刻活动，TL 已结束只剩商店）：

```python
'TL': {
    'name': '时间啊，向前！',
    'until': '2026-11-28',          # 关卡结束、商店还开着
    'closed': True,                 # 关卡分区和小游戏入口都没了
    'entry': {'tpl': 'TL_card.png', 'roi': [640, 283, 247, 153],
              'tap': [753, 371], 'wait': 5.0},   # 点 ATTENTION 区那张卡片
    'zones': {},                    # 关卡导航关闭
    'tools': {'shop': {'tpl': 'TL_shop.png', 'label': '炮火所掩埋的', ...}},
},
'OS': {
    'name': '雪山降临1101',
    'until': '2026-12-02',
    'reprint': True,                # 复刻：前端标「复刻」、助手会问突袭
    # 左下横幅：roi 是 1280 口径，×1.5 就是真机上的 (150,804)~400×44。
    # 这条**走定点比对**（_entry_here），不走整屏搜索，见下面的说明
    'entry': {'tpl': 'OS_entry.png', 'roi': [100, 536, 267, 31],
              'tap': [233, 551], 'wait': 5.0},
    'zones': {
        'base': {'tpl': 'OS_z1.png', 'label': '圣巡之旅',
                 'roi': [869, 513, 113, 167], 'tap': [925, 596]},
        'ex':   {'tpl': 'OS_z2.png', 'label': '变革之路',
                 'roi': [992, 481, 134, 167], 'tap': [1059, 565]},
        's':    {'tpl': 'OS_z3.png', 'label': '喀兰之心',
                 'roi': [1120, 410, 130, 157], 'tap': [1185, 488]},
    },
    # 同一屏上的入口节点：小游戏 / 活动商店 / 活动奖励（点进去都不是关卡列表）
    'tools': {
        'minigame': {'tpl': 'OS_mini.png', 'label': '喀兰贸易技术研发部',
                     'roi': [1102, 122, 136, 179], 'tap': [1170, 211]},
        'shop':     {'tpl': 'OS_shop.png', 'label': '观测站后勤处',
                     'roi': [113, 400, 120, 67], 'tap': [172, 460]},
        'reward':   {'tpl': 'OS_reward.png', 'label': '神谕谜题',
                     'roi': [288, 507, 123, 122], 'tap': [349, 568]},
    },
    # 奖励页进去还要点「一键领取」，见 claim_reward()
    'claim': {'tpl': 'OS_claim.png', 'label': '一键领取', ...},
}
```

- `roi` 是 1280 口径 `[x,y,w,h]`：**只在这个框里认模板**（同名按钮在别的位
  置也出现过，框外命中当没认到）。OS 的六个入口绕成一圈、图标长得像，ROI
  必须各自限死，否则会认到隔壁去。
- `tap` 是识别不到时的兜底坐标；`offset` 是「认出来的中心不是该点的地方」
  时的偏移（老 TL 的活动提示条就这么用：认的是下半截黄钻石行，要点上面那行字）。
- `until` 过期**只提示不失效**（日志会写「标定于 X，活动改过版的话要重新标
  定」）。
- `closed`：活动结束后关卡分区没了（TL）。`run()` 会在导航**之前**就返回
  「已经结束了，关卡入口关了——只能进活动商店」，别白跑一趟地图再报错。
- `reprint`：复刻活动。前端快捷栏挂「复刻」标记并钉在悖论模拟后面；助手据此
  在用户要刷 EX / S 时先问「要不要打突袭、去年打过吗」（见 `docs/agent.md`）。
- `active()` 把 `closed` 的活动排到后面，所以「最新活动」落到正在开的那期
  （OS），不会给已结束的 TL。

**模板**放 `backend/core/navtpl/events/`（用户配置目录
`config/navtpl/events/` 同名文件优先，升级不覆盖），**1920 口径**、红框往里缩
6~12px 别把红线抠进去，`find_card_bytes` 会按屏幕宽度缩放。加一条新活动的
流程：用户给带红框的截图 + 步骤 → **用 `adb exec-out screencap -p` 抓真机
1920×1080 截图、按红框原位裁** → 往 `ENTRIES` 加一项 →
冒烟 B44 / B51 会检查模板在不在、ROI 有没有互相抢。

**活动办完 UI 还会再改一次，模板要跟着重抠**（2026-11-25，TL 商店导航失效）：
活动结束当天，终端页的卡片被盖上一条「作战已结束」灰条，活动地图的商店节点
多了红色时钟和「941」剩余时间横幅。老 `TL_card.png` 在定点位置比出 **46.8 分**
（阈值 16）→ `_to_terminal` 认为「终端页上没有这个活动」，`_to_map` 直接失败，
用户看到的就是「TL 商店导航失效」；商店节点也掉到 14.1 分（阈值 12），只剩
兜底坐标盲点。重抠时把握两点：

- **模板整块裁插画**（370×230，连灰条一起认）。灰条本身就是「这个活动结束了」
  的标记，而且它出现以后插画就不再变；左边图标列里的「3 天」倒计时会天天变，
  千万别裁进去。
- **`roi` 必须跟着模板一起改**。这次真正的坑是 `roi` 还停在旧模板的位置
  （(620,300) 而内容实际在 (960,425)），定点比对只找 ±6 像素，差几十像素必然
  认不到。冒烟 B71 现在会检查「模板尺寸 ≈ roi×1.5」「挪 20 像素必须认不到」，
  换模板忘改 roi 会当场被抓。

重抠后真机复验：`run_tool('shop', prefix='TL')` 12 秒到「炮火所掩埋的」，
entry 3.0 分、节点 2.7 分（游戏里的字是「炮火」，以前按用户截图误记成「熄火」，
label 已改正）。

**模板必须从真机截图原位裁**（2026-11-22 血的教训）：模板只能用
`adb exec-out screencap -p` 抓的 **1920×1080** 图，在图上按红框原位裁。以前 OS 这套是拿用户的窗口截图（1176×700，还含 28px 标题栏）去掉标题栏再
×1.633 放大出来的——**在真机上一张都认不出**（entry 22 分、base 20 分、
ex 16 分、s 12.5 分、reward 17.5 分、claim 21~23 分，全都压着阈值 12 过不去），
表现就是「还是用老导航 / 一个节点都点不到」。窗口截图经过一次缩放再放大，
字形和 UI 边缘的亚像素对不上，模板匹配对这点差异极其敏感。重裁后同一批
模板全是 0~8 分。

**活动提示条走「定点比对」，别整屏搜**（2026-11-22）：`entry` 那条横幅
（`<雪山降临1101>活动已开放`）位置是钉死的，`_entry_here()` 按 `roi` 的左上角
±6 像素逐像素比一次（步长必须是 1，隔一格就差十来分：805 对 10 分、804 对
13 分、806 对 21 分）。**不能用 `find_card_bytes` 整屏搜**：它是一行 400×44
的长条字，缩到 1/4 粗扫后笔画几乎没了，粗扫会锁到背景渐变的谷底上，精修只在
冠军附近几十像素内找，回不来——实测同一帧定点比 10 分，整屏搜报 22 分、
坐标跑到下面别的行上，日志就变成「点了六次终端大卡都看不到提示条」白等
二十多秒。阈值 `_ENTRY_MAX = 16`（真命中 0~11 分，别的页面上同一片区域
59 分以上）。

**`find_card_bytes` 粗扫留前 3 名**（2026-11-22）：同样是上面那个「细长低对比
模板在 1/4 尺度上失真」的问题，现在粗扫排序后取相互隔开「半个模板」的前 3 个
候选各自精修，命中 `_EARLY_STOP` 就提前收工。真命中本来就 0~6 分，早停，不多
花钱；只有「看着有点像但位置不对」的帧才多算两份。


**「最新活动」怎么落到代号**：`eventnav.match_name` 认活动名 / 代号 /
「最新活动、当期活动、新活动…」，`skills.norm_stage` 和助手工具
`list_activities`（返回里的 `current`）都走它，所以助手不用猜活动代号。

**`tools` 是同一屏上的其他入口**：小游戏、活动商店、活动奖励。它们和分区节点
长在同一张地图上，但点进去不是关卡列表，所以单独列一格。
`eventnav.run_tool(touch, 'minigame'|'shop'|'reward')` 只负责把界面点到门口
（`_to_map` 到活动地图 → 点节点），点完的画面校验是软的：商店/小游戏页面偏
静态，`_moved` 硬判会误报，真正认页面的是后面的 MaaCore 任务。
活动奖励页要自己领：`eventnav.claim_reward(touch, 'OS')` 进奖励页后点「一键
领取」（`claim` 那格模板），已经领完时按钮还在，点了没反应也不报错。
2026-11-22 起这层包成了 skill：`skills.skill_claim` / 助手工具 `skill_claim`
（见上面「④ 领奖 skill」）。

## 主线导航（`mainnav.py`，2026-11-24）

用户逐屏标注的那条路，前半段和活动卡（`stagenav.goto_story_activity`）**完全
一样**，后半段分岔：

    任意界面 --底部第1个tab「终端」--> 主界面
    主界面   --「终端」大卡---------> 带底部 tab 的那一页
    底部 tab --第 2 个（曲谱）-------> 曲谱：左边 Story Line 时间轴
    曲谱     --右上「乐章收录」------> 网格视图
    网格     --顶部「默认进度」------> 卡片上不带进度徽章
    网格     --滚到能看见目标行------> 点 EPxx 卡片
    章节详情 --右下「前往章节」------> 该章关卡列表

到关卡列表就不用管了：Copilot 的 `ClickStageName` 会 OCR 认出 `15-3` 点进去。
所以**前半段抽成了 `stagenav.reach_home` + `stagenav.to_score_grid` 两个模块级
函数**，两处共用一份——各写一遍的话，改一处忘另一处就会出现「活动能进、主线
进不去」。

### 为什么活动卡要整页翻、主线不用

主线 18 张卡（EP00~EP17）固定排在网格最顶上「主题曲 / 为了明日」区：5 列、
行距 315、卡片 265×265（都是 1920 口径）。滚到顶后第 0 行左上角就是
`(100, 261)`，其余按 `col = ep % 5`、`row = ep // 5` 算得出来。活动卡会被新
活动插进来挤动，所以只能整页翻模板找。

### 三个必须踩对的细节

1. **行位置靠「行间暗带」找，不靠卡片白边框**。EP15~EP17 那三张卡的顶部根本
   不是白的（离解复合血红 / 反常光谱白底黑手印 / 相变临界墨绿），按边框亮度
   找一行都认不出来；而卡片外面那条约 50px 的空隙永远是纯暗的（灰度 15~30）。
   做法：把 x=100~1624 压成一列求逐行均值，连续 15~250 行低于 45 就是一条
   暗带，暗带的下一个像素就是卡片行起点（`_rows_of`）。上限给到 250 是因为
   滚到顶时第一行上方是标题区、暗带 189px，卡 90 会正好漏掉**唯一确定的那一行**。
2. **认卡用「按格子定点比对」，不用 `find_card_bytes` 整屏粗扫**。后者是
   1/4 尺度 + 三个缩放档，对 265×265 的大模板会锁错位置：2026-11-24 同一帧里
   EP02 自匹配报了 26 分（EP00/EP01 都是 0.0），根因是 1/4 尺度下前三个候选里
   没有它的真位置。限定在已知格子 ±18 内做全分辨率比对，真卡 0.0~4.9 分，
   最像的别的卡 22.6 分（EP00 / EP01 都是「黑暗时代」），阈值 12。
3. **滚动步长 240（1280 口径）= 360 像素**，小于行距 315 的一屏行高。一次滑
   400（=600px）那种会带惯性甩过头——2026-11-24 调试时 5 屏就滑到「别传」区，
   界面已经离开主题曲区很远。

### 模板

`backend/core/navtpl/mainstory/EP00.png ~ EP17.png`，都是从 1920×1080 真机
截图里按卡片白边框**原样裁的整卡**（265×265，含右下角 EP 编号）；用户配置目录
`config/navtpl/mainstory/` 同名文件优先，升级不覆盖。

**别和根目录的 `EP.png` 混**：那是「出苍白海」**活动**的卡（EP 前缀活动），
和主题曲第 16 章完全两回事。

### 「前往章节」

点开卡片是章节详情页（剧情简介 + 底部 EP 切换），右下角「前往章节」才进关卡
列表。按钮位置固定（1920 口径中心约 (1590,934) → 1280 口径 `(1060,623)`，
`stagenav.points()` 里的 `mainChapterEnter`，可在 `config/nav.json` 覆盖）。
**不做模板识别**：那块白色渐变条大小稳定，但底下的章节立绘每章都不一样，做
模板要 18 份。点完校验「画面变了」，没变就往左挪 40 再点一次（按钮宽约 300px）。

### 顺手修掉的一个大坑：主页图标标定过期

调试主线导航时发现 `pages.is_home_bytes` 对**真主界面永远返回 False**：
2026-11-22 那次标定把「齿轮 + 警告三角」当成「齿轮 + 邮件」裁了，坐标
`(48,32)/(188,32)` 也比真实图标左上偏了约 39px。齿轮还能 7.2 分勉强命中，
邮件 23.7 分认不出来 → `at_home` 恒为假 → 所有「先回主界面」的导航退化成
一路按 ESC，最后弹出「是否确认退出游戏？」（再按一次游戏就没了）。

2026-11-24 按真机重标：这台机器左上角是「齿轮 / 警告! / 邮件 / 日历」四个
图标，中心 x = **71 / 179 / 291 / 408**（y 都是 80）。模板重裁（56×56 原位），
坐标写进 `pages._HOME_ICONS`。这条以后每次游戏大版本更新都要复验一次——
判据失效不会报错，只会让导航变慢、变危险。

冒烟 B67 钉住：代号解析（`15-3`/`0-11`/突袭 `H15-1` 算主线，`GT-1`/`MN-1-7`
不算）、18 张模板齐全且尺寸一致、合成帧上格子定位准（第 17 章 → (862,708)）、
隔壁章不放行、`goto_stage_list` 把主线分给 mainnav 而活动代号照旧走老路径、
主页图标按标定贴得住且挪回旧坐标就认不出。

## 主线作战环境（难度，`mainnav.set_mode`，2026-11-24）

主线第 9~14 章在关卡列表左下角多一条「作战环境」，三档：

| 章 | 能选 | 备注 |
|---|---|---|
| 9 | 剧情体验环境 / 标准实战环境 | 标准这档有突袭关（H9-x） |
| 10~14 | 剧情体验环境 / 标准实战环境 / 磨难险地环境 | 磨难那档是**另一批关卡 id** |
| 0~8、15~17 | —— | 只有普通 / 突袭，没有这条栏 |

界面上是三件事叠在一起：

1. **收起态**：左下角一行字显示当前那一档（`标准实战环境`，磨难那档是红字）。
   点它 → 底部展开 tab 栏，三档等距排在 1920 口径的 x=177 / 533 / 860
   （只有两档时也是这个位置，磨难那个位置空着）。
2. **展开态**：点目标 tab 直接切，切完 tab 栏自动收起 —— 第 10~14 章都这样。
   展开时左边缘会多一个 **(i) 圆钮**，这是判断「tab 栏开没开」唯一的稳判据。
3. **第 9 章切剧情**会再弹一个全屏「请选择作战环境」：左剧情、右标准，各带
   一个「进入作战」（当前那档显示「当前选择」且灰着）。要再点一下左边那个
   按钮才真的切过去。第一次切过之后就不再弹了。

所以 `set_mode` 的状态机是：

    认当前那档（三张模板定点比对）
      ├─ 已经是目标档 → 直接返回
      ├─ 是弹窗 → 点弹窗里目标档的「进入作战」
      ├─ 认得出（收起态）→ 点按钮展开 → 点目标 tab
      └─ 认不出 + (i) 在（展开态）→ 直接点目标 tab
         认不出 + (i) 也不在 → 报错（不在这章的关卡列表上）

**不能拿按钮当「收起 / 展开」开关**：实测在「标准」展开时点那个位置，结果是
**选中第一个 tab（剧情）**——一按就切错档。所以必须先判 (i) 在不在。

### 三个模板阈值（都是真机 1920 口径原位裁的）

| 模板 | 位置 | 实测分数 |
|---|---|---|
| `mode_story/normal/hard.png` | `(178,944)-(382,988)` | 同一帧 0.0，相邻档 16~22，没这条栏 50~90 |
| `pop_title.png` | `(812,136)-(1108,188)` | 弹窗 0.0，普通列表 ≥48 |
| `tab_info.png` | `(42,978)-(92,1022)` | 展开 0~3，收起 26~80 |

阈值：认档 `≤30` 且和第二像的差 `≥8`；弹窗 `≤20`；展开 `≤12`。

### 挂机时怎么用

`skill_fight` 的 `difficulty`（`story` / `normal` / `hard`）→ `spec['storyMode']`
→ `maaplan._navigate(..., mode=)`：**导航到那一章的关卡列表之后**切档，切不过去
直接报错（`导航没到位（到第 N 章了，但作战环境没切过去…）`），不会闷头打错档。
直跑（noNav）没有导航那一步，`skill_fight` 就地切一次。

作业那一侧也按档挑（`skills._select_ops(diff=...)`）：作业站把三档当成同一
代号的三行（`easy_12-01` / `main_12-01` / `tough_12-01`）。以前只挑「第一行
非突袭」，点了「标准」却拿磨难那份作业去抄（12-2 第一位就是 tough）。现在只认
这一档；这一档一份作业都没有时，**优先拿更难的那档顶上**（磨难作业能打标准，
反过来一定翻车），日志里点名用的是哪一档。

### 「导航 + 切档」必须一次调用（2026-11-25）

`skill_navigate` 也有 `difficulty`：导航到位后自己调 `mainnav.set_mode`。以前
只有 `skill_fight` / `skill_difficulty` 能切档，助手被要求「导航到 13 章、切磨难」
时会拆成两个调用发出去，十次有八次漏掉第二步——用户看到的就是「导航了，档没切」。

真机实测（同一天）：`goto_stage_list` 54 秒 + `set_mode` 9 秒，一次到位。

## 同一个账号串行：后发的等前面的（`skills._wait_idle`，2026-11-25）

**现场**：用户说「导航到 13 章 + 切磨难」，助手把 `skill_wakeup` 和
`skill_difficulty` 同时发出去（日志时间差 1 秒）。切难度那一路抢不到本进程的
跑机名额（`claim_inproc`）→ 换独立进程 → 一进场就查游戏进程，那时 MAA 才刚开始
登录，`pidof` 自然是空的 → 抛「游戏没在跑」，导航一次都没做。同一时段 MAA 的
StartUp 连着三次「回到主界面」都没认出来，最后重试到上限——两路同时在点同一台
模拟器，界面被点乱了。

**做法**：`run_skill` 的 job 在抢跑机名额之前先过 `_wait_idle(tid, [owner])`：
同一个账号还有**比自己早开工**的任务在跑，就排队等（日志写「这个账号还有一路
在跑（唤醒），等它跑完再动界面」），等到再往下走；上限 30 分钟，超了打一行日志
接着跑（前面那路可能卡死了）。

- **只等比自己早开工的**：互相等会死锁（A 等 B、B 等 A，两边都不动）。
  「谁更早」比的是 `tasks.create` 发的**创建序号**，不是 `startedAt`：Windows 上
  `time.time()` 粒度约 15.6ms，连着建的两个任务会拿到完全相同的时间戳，光比时间
  会把兄弟任务判成「不比我早」→ 该等的没等（2026-11-25 CI 上 B69 的
  `AssertionError: []` 就是这个，本机偶尔能过、CI 必挂）。
- **不同账号不等**：多开本来就是并行，两台模拟器互不相干。
- 等的时候用户按了停止（`maacore.stop_requested`，pause 分支才有这个接口）就
  直接退出，别接着跑。

同一批还有一个兜底：`skills._ensure_game(t, log, tid, file)`——查不到游戏进程就
**直接叫唤醒**（`skill_wakeup`：有切号名走 MAA 开始唤醒，没有就重启客户端），
不是干等。用户 2026-11-25 截图报的就是这个：adb 已经就绪，日志只剩一排
「adb 返回 1：」+「游戏还没起来，等一会儿（最多 90 秒）…」，MAA 的自动唤醒压根
没动。现在的顺序是 查一次 → 不在就唤醒 → 唤醒回来还没起才给 90 秒宽限 → 真不行
才报错；唤醒抛异常但客户端其实起来了也照样放行（不能因为唤醒报错把导航一起拖死）。

轮询统一走 `game_alive(quiet=True)` / `_run(..., quiet=True)`：`pidof` 查不到进程
本来就返回码 1，那是**正常情况**，每三秒往日志里刷一行「adb 返回 1：」只会看着像
故障。`quiet` 只影响日志，返回值不变。

## 多账号：每个号的第一件事是「开始唤醒」切号（2026-11-25）

用户第三次报「助手又没加切号自动唤醒」，所以这条不再只靠提示词：
`run_batch` 在账号数 > 1 时，把 `('wakeup', {})` 插到步骤表**最前面**
（`skills._with_auto_wake`），每个号开跑前都走一遍（重写前会判断 `_wakes_itself`，
避免重复唤醒）。

为什么必须程序兜底：多账号是「一个号跑完原地接着跑下一个号」，界面停在哪、现在
登的是谁，从上一个号身上完全推不出来；而且 `skill_navigate` 的 `_ensure_game`
**只看进程在不在**，游戏在跑但登的是别人的号时它不会唤醒（这正是「导航没问题、
切号没做」的现场）。

| 情况 | 补不补 wakeup |
|---|---|
| 多账号，第一步是 `tool` / `claim`（不带 wakeup）/ `navigate` / `fight`（不带 wakeup） | 补 |
| 第一步是 `wakeup` / `sweep` / `weekly` / `train`（默认 `wakeup=true`）/ `fight` 带 `wakeup` / `claim` 带 `wakeup` | 不补（自己会唤醒） |
| 单账号 | 不补（2026-11-17 用户吐槽过单号被强行唤醒） |
| 用户说「不用切号」→ `switchAccount=false` | 不补 |

`_with_auto_wake` 是纯函数（返回新的步骤表，不改原列表），所以冒烟可以直接钉。
日志在任务开头写「多账号：每个号开跑前先「开始唤醒」切号」。

## 散点关卡（S 关，`scatter.py`，2026-11-23）

当期活动（雪山降临1101 / OS）的 S 关**不在关卡列表里**：活动地图右下角点
「喀兰之心」进去是一张满屏雪山图，五个 `OS-S-x` 按钮浮在上面，左上竖条写着
「积雪清理系统」。MaaCore 的 Copilot 只在**关卡列表**里整屏 OCR 找关卡名
（`Copilot@ClickStageName`），这种界面它一个都认不出——用户 2026-11-23 的原话
是「MAA 无法识别 S 关卡」。

所以这条路完全绕开 Copilot 的导航：

| 步 | 谁做 | 判据 / 动作 |
|---|---|---|
| 1 | `eventnav.run` | 主页 → 终端 → 活动地图 → 点「喀兰之心」，到 S 关界面 |
| 2 | `scatter.cells_of` | 五个定点模板各认一次，**认到才算解锁**（没解锁的格子游戏不画） |
| 3 | `scatter.open_stage` | 点格子 → 等详情面板 → 点「开始行动」（普通）或「突袭模式 → 开始突袭」（突袭）→ 等编队页 |
| 4 | `maaplan.run_scatter` | 一关排**一个** Copilot 任务（单作业模式），等 TaskChainCompleted |
| 5 | `scatter.back_to_cells` | 点屏幕回 S 关界面（认到格子且没有详情面板就停手） |

**单作业模式怎么写**（`maaplan._single_params`）：Copilot 参数只给
`filename`、**不给** `copilot_list`。MaaCore 里那句话是
`CopilotTask set_params failed, stage_name or filename not found`——filename
单独就成立。给了 `copilot_list` 它会走 MultiCopilot，每关都要 ClickStageName
导航，而散点关卡在它的 OCR 里根本不存在。只给 filename 它就不导航，界面在哪
从哪开始——正好我们从编队页交给它。

**单作业模式不判三星**：asst.log 实证，`BattleProcessTask` 完成 →
`TaskChainCompleted`，中间没有 `StageDrops` / `EndOfAction` / `WaitUntilEndOfAction`。
也就是说它打完就结束，界面停在结算页，**成败它不报**。所以：
- 「点一下回关卡界面」归 `back_to_cells`（三星和失败都要点，失败页还多一层
  「任务失败」，实测要点两下）；
- 日志里如实写「打完了（单作业模式不判三星）」，**不要**编造逐关成功数；
- 别再用「截图看几颗星」补这一课——用户 2026-11-22 明确否了自识别结算页。

**模板与 ROI**（`navtpl/scatter/`，1920 口径）：

| 名字 | 内容 | 备注 |
|---|---|---|
| `OSs_1.png` ~ `OSs_5.png` | 五个格子（含编号） | 每个带 `roi`（1920 口径 `[x,y,w,h]`）：模板里带雪山背景，整屏搜会认到隔壁格子上（实测 S2 的模板在 S5 位置 6.9 分，比阈值 12 还低） |
| `OSs_start.png` | 详情页「开始行动」（蓝） | 本体 0 分，其他页 ≥19.5 分 |
| `OSs_raid.png` | 「突袭模式」红条 | 切到突袭后这一格就变成「普通难度」了，所以它同时是「当前是普通难度」的判据 |
| `OSs_start_raid.png` | 「开始突袭」红钮 | 和「开始行动」同位置换字，靠灰度就能分开 |
| `OSs_quickform.png` | 编队页右上「快捷编队」 | **别用右下那条竖的「开始行动」当判据**：它有滑入动画、亮度跟主题变，同一位置重截一帧能从 0 分掉到 999 |

**格子只认一次**：详情面板一展开，整屏蒙一层暗色，所有模板分数从 0 涨到
11 分左右，这时重新识别会把「有」看成「没有」（实测 S4 被判成没解锁）。所以
`run_scatter` 开头认一次、把坐标存下来，之后 `open_stage(..., pos=坐标)`。

**加一期新的散点活动**：
1. 真机 1920×1080 截图，按上面列表裁模板进 `navtpl/scatter/`；
2. `LAYOUTS` 里加一条（`zone` 填 eventnav 的分区键，`cells` 填编号→模板+ROI）；
3. 每张模板先自检（同帧 `find_card_bytes` 应 0~2 分、别的页面 >18 分）；
4. 冒烟 B58 会自动带上（模板缺失会直接报）。

### 一关怎么算结束（2026-11-23 补齐，三个坑一起踩过）

单作业模式下 MaaCore 的收尾回调**不是只有 Completed/Error 两种**，这三种都要认：

| 回调 | 真实含义 | 处理 |
|---|---|---|
| `TaskChainCompleted` | 打完了 | 记成功、`back_to_cells`、下一关 |
| `TaskChainError` | 任务链报错（结算不是三星等） | 记失败，接着打下一关 |
| `SubTaskError(why=OperatorMissing)` + `TaskChainStopped` | **编队缺干员**（作业要的干员账号里没有） | 记失败并说出缺谁，接着打下一关 |
| `TaskChainStopped`（无前置错误） | 次数打满 / 理智不足 / 用户按停止 | 正常收尾 |
| `wait()` 返回 False 且 `eng.stopped` | **用户点了「停止挂机」** | 记「已停止」，不记失败，直接收尾 |

- **`TaskChainStopped` 不能一律当成功**：编队缺干员时 MaaCore 只发
  `SubTaskError` + `Stopped`，不发 `Error`。旧代码把 Stopped 记成 `ok=True`，
  于是 `run_scatter` 既不报失败、`wait()` 又返回 False，最后打成一句
  「这一关超时（1200 秒）」——用户 2026-11-23 就被这句骗了（实际是缺「圣聆初雪」，
  而且他中途还手动按了停止）。现在 `maacore` 把 `SubTaskError` 的 `why` 记在
  引擎上，下一条 `TaskChainStopped` 带 `why` 就走 `chain_failed`。
- **`wait()` 返回 False ≠ 超时**：它有三种来源（真跑满 / 任务被停 / 根本没跑起来）。
  现在日志报**实际等了多久**，别再说「超时 1200 秒」这个上限值。
- **用户按停止要单独说**：`Engine.stop()` 置 `self.stopped`，`run_scatter` /
  `run_core` 因此在 `wait()` 返回 False 时能分清「你按了停止」和「MAA 卡死」。
  手动停止：不记 `stages_failed`、`timed_out=False`、`paused='手动停止'`，
  日志说「已停止（你按了停止），剩下的关不打了」。

**已知限制**：理智不足时「开始行动」点不动（弹理智不足框），`open_stage` 会
返回「点开始后没进到编队界面」并按关跳过；用户要自己补理智或勾吃药。

### 停止：必须能按账号点名（2026-11-23）

用户原话：「停止的话不能指定停止，会一起停其他任务」。他让停 b 服那个号，
结果两台模拟器一起断，官服那轮正在打的关白烧。以前只有 `stop_all_live()`
一个全停入口，现在按账号筛：

| 停什么 | 在哪 | 怎么筛 |
|---|---|---|
| 本进程里的 MaaCore 实例 | `maacore.stop_live(owners)` | `Engine.owner` 是账号文件名，比较走 `maacore.owner_match`（忽略 `.json`、认昵称） |
| 多开时的独立子进程 | `multiproc.stop_groups(owners)` | `_RUNNING` 注册表按这一组的 `files` 匹配，命中就 `proc.kill()` |
| MAA 本体（跑 GUI 任务队列那条老路） | `maa.close(dir)` | 只在**全停**时做 |

```python
skills.stop_accounts(['示例昵称#0384 bilibili服.json'])   # → {'engines':1,'processes':0,'stopped':1}
skills.stop_accounts()                                    # 全停
skills.match_accounts('b服那个')                           # 名字 → 文件名，对上多个直接报错
```

- `skills.match_accounts`：完全相等的优先（文件名 / 昵称 / 别名），子串命中
  **只允许唯一**。守住这条是因为停止是破坏性操作——用户说「停灰」，把三个
  「灰」开头的号全停了比多问一句亏得多。
- 子进程被 kill 的那一组，`multiproc.run_group` 返回的第三个值是 `True`，
  调用方按「你手动停了这一组」记账（`stopped=True`），不记「子进程异常」。
- `Engine.stop()` 会置 `self.stopped`，已停过的实例不会再被重复计数，
  所以工具返回的 `engines` 就是「这次实际停了几路」。
- 入口三处共用同一套：agent 的 `stop_run(account=...)`、
  `/api/run/stop`（body `{file}` 或 `{all:true}`）、界面右栏「停止」按钮
  （只传当前账号）。返回里带 `accounts / engines / processes`，**说话照它说**，
  别把「停了一个号」讲成「都停了」。

## MAA 小工具（牛杂，`maatools.py`，2026-11-19）

MAA GUI 的「小工具 → 牛杂」里那些按钮**不在一键长草队列里**，但 MaaCore
认：它们都是 `Custom` 任务。对着 asst.log 试出来的两种参数：

```python
# 战时新闻委员会（TL 的活动小游戏）
{"task_names": ["MiniGame@WartimePressCommittee@Begin"]}

# 喀兰贸易技术研发部（OS 的活动小游戏，2026-11-22 加）
{"task_names": ["MiniGame@OS@Begin"]}

# 活动商店
{"task_names": ["SS@Store@Begin"],
 "params": {"event_shop": {"blacklist": ["数据增补条", "数据增补", "龙门币", "家具零件"]}}}
```

> 任务名是从 MaaCore 二进制里 grep 出来的（`MiniGame@OS@Begin` /
> `MiniGame@WartimePressCommittee@Begin` 都在），不是猜的。以后再加小游戏，
> 先 `grep -a 'MiniGame@' MaaCore.dll` 找名字，再进 `maatools.TOOLS`。

黑名单的三个坑（都实测过，别改）：

| 写法 | 结果 |
|------|------|
| 挂在 `params.event_shop.blacklist`，非空字符串数组 | ✅ `Parsed and registered EventShop task: SS@Store@Begin` |
| 挂到顶层 / 塞进 params 字符串 | ❌ 静默忽略，**把无限池一起买光** |
| 空数组 `[]` | ❌ 不注册插件，同样买无限池 |
| 元素是数字 | ❌ `set_params failed, event shop blacklist item is not string` |

所以 `maatools.build_params('event_shop', [])` 直接抛错，宁可失败也不买无限池。

**执行**：`maaplan.run_custom(spec)` —— 不复用 `run_core`。那条链是给抄作业
写的（强制补 StartUp、关卡导航、排 Copilot、失败重挂），牛杂一项都用不上。
`run_custom` 只做「关 GUI → 连接 → append Custom → start → 等 → 收尾」，顺带
把 asst.log 里活动商店那几句话翻成人话（跳过黑名单商品 / 选中商品 / 到无限池
收工 / 代币不够）。

**卡教程**：小游戏第一次进要过一段教程，MAA 认不出教程对话，会安静地重试同一
个节点。`run_custom` 每 10 秒看一次「多久没新事件」，超过 120 秒就把
`spec['tutorial']` 那句话打进日志 —— 用户才知道要手动过一遍教程再挂。

**入口**：`skill_tool({'name': 'wartime_press'|'os_rd'|'event_shop',
'account': ..., 'blacklist': [...]})`，助手工具同名。导航和 MAA 都跑在同一个
后台任务里，`tid` 透传给 `run_custom`，日志落在同一条任务上。

**`prefix` 与「小游戏」这个泛称**（2026-11-22 加）：每一项可以带
`'prefix': 'OS'` 表示「它只在 OS 的活动地图上」。于是有两个用法：

- `name='喀兰贸易技术研发部'` / `'os小游戏'` → 直接选中 `os_rd`，导航用
  `ent['prefix']` 去 OS 的地图，不会跑到当期活动上瞎找；
- `name='小游戏'`（`_GENERIC` 里的泛称）→ 按 `prefix` 在那个活动下挑；不传
  `prefix` 而小游戏不止一个时**报错并列出候选**（「「小游戏」里有好几个小游戏，
  要哪一个：…」），不做默认猜测。

`describe()` 也把 `prefix` 带出去，前端 / 助手能看出这一项属于哪期活动。


## 培养干员 skill（`train`，2026-11-18）

MaaCore v6.20+ 有个 `OperProgress` 任务（GUI 里叫「干员培养」），能力是
「按计划把干员升到目标练度」：精英化、技能基础等级、技能 1/2/3 专精，还会
自己跳加工站/制造站补材料（材料够的话）。`skills.skill_train` 就是它的封装：

```python
{"operators": [{"name": "泥岩", "elite": 2, "skillLevel": 7, "mastery": [0, 3, 0]}],
 "account": "xxx 官服.json", "accountName": "5505",
 "switchAccount": True, "wakeup": True}
```

- **专精是绝对等级**（`[0,3,0]` = 二技能专三），不是「再升三级」；`mastery`
  也吃 `{"2":3}` / `"2:3"` / `"技能2专精3"` / `"二技能专三"`（`_norm_mastery`）。
- **先跟账号快照对一遍**（`_train_plans`）：已经精二 / 技能已到 / 技能已专到
  的项直接剔掉，一条不剩就压根不跑 MAA；名字在快照里找不到（写错字 / 新干员
  / 没同步）当场报错，别让 MAA 翻完列表再说「干员不存在」。
- **材料不足 = 暂停，不是失败**：`OperProgressDetail` 的
  `result_detail=ResourceInsufficient` 一到，`_on_train_detail` 置
  `state['pause']` 并另起线程 `AsstStop`（不能在回调线程里同步 stop，会等
  工作线程收尾）。`run_core` 收尾时不再刷一串 `[失败]`，只报「已暂停：XX 材料不足」。
  **`ChipNotCraftable` / `FormulaLocked` 同样算「该去补东西」**（2026-11-19
  用户报「卡在升级等级的界面报错」）：MaaCore 6.20.1 在 E0→E1 缺普通芯片时
  直接 `return ChipNotCraftable`，不走加工站合成（上游 issue #18612，MAA
  自己的 GUI 手动跑也一样，只有芯片齐备时才成功）。日志写清「缺芯片 / 怎么补」
  并暂停；暂停后同一干员的连环 `PrerequisiteNotMet` 静默，不刷屏。
- **暂停/失败要把界面拉回来**：培养中止时 MaaCore 停在干员档案的精英化页，
  用户切回模拟器以为卡死。`run_core` 收尾等 `running` 变 false（最多 5 秒）
  后调 `AsstBackToHome`，日志补一句「界面已退回主界面」。
- **剩下的计划落盘** `config/train-plan.json`（`_remain_plans` 用 MAA 报回来的
  成功项相减），用户补完材料说「继续培养」→ `skill_train(resume=true)` 接着做，
  不用重报练度；全部做完就把文件删掉。
- `wakeup=false`（用户说游戏已经开着）时不排 StartUp（`spec['noStartUp']`，
  core_tasks 不再强制补）；`OperProgress` 自己会走 `OperBoxBegin` 从任意页面
  进干员列表。`switchAccount=false` 时 StartUp 照跑，但**不发** `account_name`
  （MaaCore 里给了这个参数就切号，没有单独的开关可关）。

## config/ 里的全局 json 不是账号（2026-11-19）

用户报「**突然无法自动战斗**」——唤醒、战斗、培养三个 skill 同时失败，日志
清一色 `AttributeError: 'str' object has no attribute 'get'`。

根因不在 skill，在 `accounts.list_accounts`：培养暂停时 `skills._save_train_plan`
把 `config/train-plan.json` 落盘，里面 `"account": "abc示例#7986 官服.json"` 是
**文件名字符串**；账号扫描把这个文件当账号读，`(snap.get('account') or {}).get('uid')`
撞上字符串 → 整个账号列表抛异常。所有 skill 开跑第一句都是 `_account_file('')`
→ 全部一起挂。这也解释了「突然」：文件是**上一次培养暂停时才生成**的，
之前目录里没有它。

修法两层，缺一不可：

1. `updater.NON_ACCOUNT_JSON` 补 `train-plan.json` / `navcache.json` /
   `appupdate.json`——我们自己写在 config/ 根目录的全局文件都登记进去；
2. `list_accounts` / `scan_accounts` / `find_by_uid` / `char_path` /
   `merge_preserved` 里的 `account` 段一律 `isinstance(..., dict)` 才算账号，
   `stats` / `maa` / `skd` 也走 `_dict()`。**`or {}` 挡不住字符串**，这是
   这个 bug 的全部原因；以后新增全局文件忘了登记也不会再连锁崩。

B40 用「train-plan.json + account 为字符串/数组/缺 game 的文件」钉死。

## MAA 日志补全（2026-11-18）

用户报「自动战斗没有日志，右边不知道 MAA 在干什么」。原因有两层，都补了：

1. **回调侧**（`maaplan._on_core_event`）：以前只写「换作业 / 掉落 / 理智 /
   打满」，一场战斗 3 分钟面板上只有一行「开始执行」。现在补上
   `CopilotAction`（部署 / 开技能 / 二倍速 / 技能就绪自动开）、`BattleFormation`
   （编队名单）、`SubTaskStart` 的关键阶段（找关卡 / 开始行动 / 战斗中 / 结算）、
   `ExceededLimit`（重试到上限）和 `OperProgressDetail/Summary`（培养结果）。
   开技能 15 秒内只说一次、同一个干员只报一次部署，避免刷屏。

   `SubTaskStart` 的 `subtask` 字段只是插件类名（`ProcessTask` /
   `BattleProcessTask`），真正能给人看的阶段在 **`first[0]`**（`maacore._translate`
   取它），形如 `Copilot@WaitUntilEndOfAction` / `BattleStartAll`。注意名字有的
   本来就带 `<TaskChain>@` 前缀、有的不带（后者会被补上前缀），所以
   `_subtask_label()` 按裸名查表，两种都能命中。这张表**必须按
   `<MAA>/debug/asst.log` 里实际出现的 `first[0]` 统计来写**，不能照源码猜——
   第一版猜成 `Copilot@BattleStartPreFlag`，跟真实日志全对不上，战斗过程依旧
   一片空白。按真实日志校准后关键阶段覆盖率 93.8%，剩下 6% 是
   `BattleProcessTask` / `StartGameTask` 这类插件类名，静默跳过才不刷屏。
   `CopilotAction` 里白名单外的动作（`Output` / `SkillUsage`）同样不写。
2. **日志侧**（`maalog.py`）：MAA 有些话只写 `debug/asst.log`
   （`confirm stage name failed … stage name: MN-EX-6`、`unknown oper name`），
   不走回调。`maalog.Tap` 用 `asstlog.Tail` 从**当前文件末尾**开始增量读
   （`Tail.seek_end()`，不重放几十 MB 历史），只翻 `[ERR]/[WRN]` 里的有效行，
   噪音（`roi is empty` / `Unknown task` / 缺 pipeline）一律丢，同一句 20 秒内
   只说一次，输出成 `[MAA] …` 进同一条任务日志。
3. **心跳**（`_wait_run`）：90 秒没有任何新日志就补一句「…还在跑：MN-EX-6
   （已 3 分 20 秒）」。以前直接 `eng.wait(timeout=…)`，卡住和正常跑在面板上
   长得一模一样。

### 回放真实回调流查「日志还是没有」（2026-11-22 晚）

用户第二次报「战斗日志还是有问题，没有 MAA 那种提示」。这次日志里连**一行**
逐条步骤都没有，只有「开始任务 / 任务失败」。查法（这套值得记住）：

1. 先看落盘的任务日志 `debug/tasks/<tid>.log`，确认不是前端没渲染——它确实
   一个阶段行都没写。
2. 再看 MAA 自己的 `debug/asst.log`：同一时间段 `append_callback | SubTaskStart`
   和 `SubTaskExtraInfo` 一大把，说明 MaaCore **发了**，是我们没接。
3. 写探针跑一趟最便宜的 `Depot`（仓库识别），在 `Engine._translate` 上打补丁
   打印**原始 msg 码**：拿到的是 `10001,10002,20001,20002,20003`。

**根因**：`MSG_SUBTASK_*` 一直按 MaaCore 4.x 抄成 70000~70004，而 5.x/6.x
的 `AsstMsg` 早就挪到 **20000~20004**。任务链级（10001~10004）两边一样，所以
「开始任务 / 任务失败」一直正常、唯独逐条步骤被 `_translate` 全部挡掉——
表现就是「一场仗打完日志一个字都没有」，而且没有任何报错。

- 常量改成 20000 段；另留 `_SUBTASK_OLD` 映射，`_translate` 开头先归一化，
  老核心（4.x）的 70000 段也认，换核心不会又哑一次。
- `_fn` 里 `_translate` 原来是裸调：翻译层一抛异常，整条事件连日志都不留痕。
  现在包 try/except，出错落一条 `日志翻译出错（msg=…）` 的 error 事件——
  这次的坑就是「静默」放大的。
- 补 `MSG_SUBTASK_ERROR`（20000）：MaaCore 只给 pipeline 名不给原因，
  `Copilot@WaitUntilEndOfAction` 翻成「作战结束了，但结算不是三星（漏怪 /
  作业没打过这关）」。以前这种情况只有一句空白的「任务失败：自动战斗」。
- 验证不用真打：`debug/replay_log.py` 把历史 asst.log 里的 `append_callback`
  行按时间窗回放给 `_translate` + `_on_core_event`，看面板会打出什么。
  拿 20:51 那次真机翻车（OS-6 打 2 星）回放，输出和 MAA 自己 gui.log 的形状
  一致：`换作业：OS-6` → `开始编队` → `[芬, 斑点, …]` → `选择干员: 芬` →
  `已开始行动` → `当前步骤: 切换倍速 / 部署 芬 …` → `作战结束了，但结算不是三星`。

顺带两条降噪（同一轮用户要求）：

- **练度修正不再显示**：`copilot._fix_oper` 补精英下限照样补，但不再往 notes
  里塞「芬 精英要求 None → 1」（一套作业集 5~10 行，纯噪音）。技能/星级
  不合法、丢弃动作这些真问题仍会写。
- `to_group Group not found in combat data: 雪崩触发装置` 这条 ERR 是无害的
  （作业把装置写成干员组），`maalog` 翻成「装置/召唤物占位，MAA 认不出会跳过，
  不影响打」，免得用户以为程序坏了。


4. **按 MAA 面板的样式补全（2026-11-22）**：对照 MAA 自己的日志面板，之前缺
   三类——`BattleFormationSelected`（`details.selected`）没接，所以没有
   `选择干员: 芬`；`CopilotAction` 的 `SwitchSpeed` / `SkillDaemon` /
   `UseSkill` / `Retreat` 只报了个词，没有 MAA 那种 `当前步骤: …` 前缀；
   `BattleFormation` 打成一整句「编队：…」，MAA 是单独一行 `[芬, 斑点, 黑角]`。
   现在全部对齐，`StartAction` 单独一行 `已开始行动`。

   **每关成功**：MaaCore 6.20.1 在 Copilot 任务里不发 `StageDrops`，但
   MultiCopilot 是**顺序执行**的——收到下一个 `CopilotListLoadTaskFileSuccess`
   就说明上一关已经打完（翻车会走 `TaskChainError`，不会换关）。所以换作业时
   先 `✓ <上一关> 完成`，再把 `state['stage']` 换成新关，并**清掉
   `state['nodes']`**：阶段去重是按 label + 8 秒窗口做的，不清的话新一关的
   「已开始行动」会被上一关的那条吞掉。单关作业没有换关信号，以最终的
   `完成任务：自动战斗` 为准。


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

真机验证（dev 环境、abc示例 官服、游戏停在主界面）：
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

真机复验（dev 环境、abc示例 官服）：
`RI-1` → 第 13 屏命中 4.4 分 → 进图；`IW-1` → 第 8 屏命中 2.6 分 → 进图
（重裁模板前是翻到底 17.9 分 FAIL）；`CV-1`（靠底部）第 15 屏命中 1.1 分 →
进图，全程 2 分 36 秒。走缓存的第二次 `RI-1`：回顶 → 直接翻到第 12 屏 → 第 14 屏
命中 5.4 分，全程 1 分 26 秒；网格本来就停在目标卡上时「当前这一屏就找到了
（1.2 分）」，全程 48 秒。


## 活动内的「分区图标」（EX / S / MO 导航，2026-11-18 凌晨）

用户实测后指出：活动地图右下角**不是一个 EX 开关，是一排分区图标**（2~4 个：
基础关 / EX / S / MO），点哪个切哪个，EX 也不总在右边——DM 的「破败前线」、
BB、MT 的「谒见神祇」、PV 的「彩车游行」都在中间。老实现只有一个点
`exSwitch=[1199,620]`，只有「2 个图标、EX 在最右」的活动能蒙对；S / MO 更是
压根没实现（`is_extra()` 只认 EX，`BB-S-1` / `MT-S-2` / `DM-MO-1` 会被当成
普通关，进基础区后 MAA 找不到关卡，表现是「导航成功但打不了」）。

现在的分工：

| 事情 | 谁负责 | 依据 |
|---|---|---|
| 这是什么分区 | `zone_kind()` | 代号后缀：`-EX-` / `-HX-` / `-S-` / `-MO-` / `-ST-` / `-F`（HX 归一成 ex） |
| 它在第几格 | `zone_rows_of()` | 资源库 `zoneId` 排序（`act17d5_zone2` → 1），**和界面左右顺序一致**；个别活动用 `zoneRowByCode` 硬指 |
| 格子在哪 | `zone_icons_of()` | `DEFAULT_POINTS['zoneIcons']`，逐活动标定，`config/nav.json` 可覆盖 |
| 单 EX 按钮在哪 | `ex_point()` | `exSwitchByEvent`，只适用于「基础区 + 一个 EX 按钮」的活动 |
| 点击与校验 | `goto_subzone()` | 先点第 1 格归位 → 点目标格 → 画面变化 + 再点一次稳定性 |

要点：

1. **先归位再切**。游戏会记住上次停在哪个分区，只点目标格的话「本来就在」
   和「点空了」都表现为画面不变，判不清；先点第 1 格就变成确定性动作。
2. **坐标不是硬信**。`_snap_icon()` 在点击前用当前画面重新找图标列：图标是
   花纹密集的徽记，逐列算灰度标准差会出现峰，背景是平滑插画没有峰。只接受
   ±3% 屏宽（≈58px / 1920）内的微调——离线拿 10 个活动的截图试过，全局找峰
   最远会偏 108px（那是隔壁图标），宁可不校正。
3. **没标定的活动不许瞎点**：EX 退回老单点（历史行为，日志会写明），S / MO
   直接报错让补坐标。已标定 35 个（第一批 DM/WD/BB/GO/GA/HE/MT/BI/RS/PV/MN，
   第二批 NL/HS/OR/CW/DV/MB，第三批 SV/BP/EP/TW/LE/ZT/FC/EA/OF/RI/DH/IC/SL/
   AS/AT/CB/CV/AD），CW 只标前 2 格（S 分区是另一套 UI，MAA 跑不了，故意留空）。
4. **`OF-EX1` 这类写法以前进不了导航**：形态闸 `_STAGE_CODE_RE` 要求 EX 与
   数字之间有横杠，火蓝之心（OF）/ 喧闹法则（CB）写成 `OF-EX1`，在形态闸就被
   判「不是关卡代号」。正则改成 `(?:-[A-Za-z]+)?-?\d+`，两种写法都放行。
5. `skill_weekly` 的每个战斗批次开打前会先 `skill_navigate` 一次（`nav` 参数
   可关）：老活动 MAA 没有寻路，Copilot 只有在「已经站在正确分区的地图上」时
   OCR 才找得到关卡名，不导航的话 EX / S 批次必报找不到关卡。
6. **不是所有活动都是方格栏**。WR / WB / IW / GT 这类老活动的右下角是「左边一个
   EX 按钮 + 右边一块活动名/副标题」，没有整齐的格子，按分区下标点会点到副标题。
   它们的坐标记在 `exSwitchByEvent`，`goto_subzone` 走单点分支：点一下 → 比对
   画面变化，没变就报「EX 按钮没反应」，不假装成功（也**不做归位**，界面上没有
   基础格可归）。
7. **`GT-HX-*` 不是新分区**。GT-EX-1~3 与 GT-HX-1~3 在资源库里都是
   `act13d2_zone2`，共用一个「EX」图标，所以 `zone_kind()` / `stageplan.kind_of()`
   把 HX 归一成 ex，分区下标、排序、预检前置展开都按 EX 走。
8. **第三批（老故事集剩下的 18 个活动）踩到的坑**：
   - **资源库分组要按 `zoneId` 前缀，不能按 `stageId`**。火蓝之心（OF）的
     stageId 是 `a003_ex01` 这种老式 id，和 `act11d7_zone3` 对不上，老代码
     `sid.startswith('act')` 直接滑到兜底分支，`OF-EX1` 被算成第 2 格
     （实际是第 3 格），点了切到 ② 页、MAA 找不到关卡。现在按 zoneId 前缀
     分组；主线/磨难关（stageId `main_*` / `hard_*`，zoneId 却是
     `act2mainss_zone1`）要排除掉，否则 `15-3`、`H15-1` 会被判成「活动第 1 区」。
     改完做了全量对比：3032 个代号里只有 OF / IC-P 这两族的结果变了。
   - **`OF-F1~4` 是第二个「地区切换」页**，`zone_kind()` 新增 `f`；不认的话
     `zone_rows_of` 走兜底返回 None，导航会当成「不是活动关」直接不切页。
   - **`SV` 复刻过**：资源库里原版 `act10d5_zone1` 和复刻 `act11sre_zone1`
     都收着，同一活动排出 3 个 zone，`SV-EX-1` 被算成第 3 格（界面只有两格）。
     这类「界面格数和资源库分区数对不上」的，用 `zoneRowByCode` 按代号前缀
     硬指（`SV-EX` → 1、`IC-P` → 0）。前缀按整段匹配，`IC-P` 不会误吞 `IC-PX`。
   - **「基础区」也是一格，必须标**：`goto_subzone` 每次先点第 1 格归位。
     SV / RI / EA / BP / LE / FC / AD 的基础区是「左边一块活动横牌 + 右边 EX
     按钮」，横牌本身就当一格点（MB 的「超狱计划」先例）。
   - 标定这 18 个活动时，红框检测改用**纯红竖边（每列 ≥70 个红像素）**找左右
     边界，再用 `_snap_icon` 离线过一遍：44 个新点一个都没被改点。

标定方法（新增活动时，2026-11-18 用这套标了三批共 35 个）：进活动地图截全屏（带窗口
标题栏也行）→ 用户在图上框出目标分区图标 → 程序按「纯红竖边」找框的左右边界
（每列红色像素数 ≥70 才算，能滤掉背景红），框中心 ÷1.5 就是 1280 口径的 x；
**相邻图标的 y 统一取 608**（图标行高度一致，徽记高约 110px，608 对应 1920 的
950，落在 900~1000 都能点中）。同活动内图标等距，两个锚点就能定整排。
`tools/zone_calib.py` 的列标准差找峰在插画复杂的图上会混进地图关卡标签，只适合
当交叉验证。写完跑 `python tools/smoketest/smoke_test.py`，B35 会校验图标数量、
坐标范围、左右间距和「进地图后必须调 goto_subzone」。

## 打关预检：这号能打哪几关（2026-11-18）

用户实测：助手把 BB-S-1~4 直接排进队列，账号练度不够，MAA 跑 8 分钟
「0/1 个账号正常结束」，第一关就过不去，后面三关根本没解锁——用户只看到
失败，不知道卡在哪、也不知道该补什么。之前 `_prepare_ops` 是「搜到作业就
排第一份」，完全不看这号能不能抄。

现在分成「预检」和「执行」两段，实现在 `backend/core/stageplan.py`：

| 环节 | 做法 | 依据 |
|---|---|---|
| 顺序 | 同活动按 `resources.flow_key` 排（基础 → EX → S → MO） | 资源库自家流程表，不另造 |
| 前置 | **默认不补**（`autoPrereq` 默认 `false`）；补的时候只补同分区前面的关，且**只打普通** | `BB-S-3` → 不传参数就只排 S-3；传 `autoPrereq=true` 才加 S-1、S-2 |
| 练度 | 取作业站的作业，用 `copilot.req_met` 比对账号干员 | 四档：ready / borrow / train / blocked |
| 连锁 | 某关 train / blocked，同活动后面的关一律「前置未通」 | 前一关没过，后面在游戏里是灰的 |
| 收尾 | `run_core` 回报 `failed_stage` / `stage_trail` | 从 `CopilotListLoadTaskFileSuccess` 的文件名反查代号 |

### 四档结论

- `ready` 干员齐、练度够 → 直接抄
- `borrow` 差一人，可借助战顶上 → 也排（`support_unit_usage=1`）
- `train` 有干员但练度不够 → **不排**，日志写「谁 需 精二60 专三，你有 精一70」
- `blocked` 缺干员且借不到 / 这关没作业 → **不排**

只有前两档进 `copilotList`；后两档连带把后面同活动的关标「前置未通」。
`skill_fight` 拿到的 `playable` 就是真正要排的关，`blocked` 原样返回给调用方
（助手会在确认卡和回话里说清楚）。

**全都被判「缺干员」时不拒绝**（2026-11-19）：作业站最新的作业常常用当期新
干员，这号一比对就全缺人，但缺人可以借助战（`support_unit_usage=1`），用户
手动跑 MAA 明明是能打的。所以 `blocked` 全是「缺」且不含「练度」时，改成
警告 + 照排（`预检说这几关缺干员…照排试试`）；「练度不足 / 这关没作业」仍然
一关都不排、直接报错。

### 几个刻意的选择

1. **默认不补前置（2026-11-19 改）**：`expand(['BB-S-3'])` 默认仍是链，但
   `skill_fight` 传的 `with_prereq` 默认 `False`——**用户点名的关就是全部**。
   起因是他只点名 S-3、S-4，程序把 S-1、S-2 也从头打了一遍（还按突袭打）。
   不补时 `notes` 里写「S-4 前面还有 S-1、S-2、S-3，没打过的话是锁着的」，
   助手把这句转述给用户，他同意再传 `autoPrereq=true`。
   补进来的关一律 `prereq=True`，`_select_ops` 见到它 **只排普通作业**
   （打通就解锁，打突袭白烧一倍理智）。
   主线关不展开：`chain('1-7')` 只返回自己（`zoneType=ACTIVITY` 才认活动，
   老活动 `a003_01` 这种 stageId 不带 act 前缀，按前缀判会漏）。
2. **剧情关不挡后面的战斗关**：`BB-ST-1` 没看过，`BB-S-1` 照样能打，
   所以 ST 不进连锁判定，只提示「交给 skill_skip_story」。
3. **作业查询有 10 分钟缓存**：预检查一次、下载作业再查一次就是双倍请求；
   缓存键用**账号文件名**（不是 `id(snap)`，每次 review 都新建 dict，用 id
   永远打不中）。
4. **查不到 ≠ 打不了**：作业站抽风（`_lookup` 抛异常）时这关标 `unknown`
   照排，只写一句原因；账号快照读不到就只判「有没有作业」，不判练度。
   `skills.skill_fight` 外面还有一层 try/except，预检整个挂掉也照原计划开打。

### 真开打之后

`maaplan.run_core(spec)` 多返回三个字段：`failed_stage`（卡在哪关）、
`failed_why`（MAA 原文）、`stage_trail`（按顺序打过的关）。`skill_fight` 用
`stageplan.explain_failure()` 把 `why` 翻成人话（未解锁 / 打不过 / 理智不够 /
找不到关卡），并写明「后面 X、Y 没打」。`skill_weekly` 的每个战斗批次现在
遇到 `blocked` 或 `failed` 会**立刻停**并带上原因抛给上层——周目是按解锁顺序
走的，这一关过不去，后面的 EX / S 排了也是白跑。

### 兜底超时

`skill_fight` 给 `run_core` 传默认两小时超时（`timeout` 参数可覆盖）。
用户截图里出现过「等了 480 分钟还没跑完」——MAA 卡在编队/加载界面时不会
自己停，而 `run_core` 不传超时就是 8 小时兜底。现在到点 `AsstStop`，返回值
带 `timed_out=true`，日志写「跑满 120 分钟还没结束，已停下；最后一关是 X」。

### 已知边界

- `mode=fight`（纯刷关）没有作业可比，判不了练度：预检只给顺序，全部标
  `unknown`，靠运行时失败即停。
- S / MO 分区在游戏里还需要先通关该活动全部 EX 关，这一点资源库没有逐关
  解锁关系，只能在预检提示里写明；没通的话到那一步会报「找不到关卡」。
- 本地没有关卡进度数据，判断不了「哪几关已经通关」。以前靠自动补前置兜底，
  结果是每次把前置关重打一遍；现在默认不补，解锁交给用户在
  「要不要一起补」里决定（`autoPrereq=true`）。
- 资源包（`resources/arknights.json`）落后于游戏时，当期活动新开的 EX / S
  分区不在表里——靠 `resources.infer_stage()` 按代号规则推（见下一节），
  推出来的关**没有流程序**：`chain()` 只返回自己，前置提示走 `prereq_codes()`。
- 周目（`skill_weekly`）现在按**分区断批**：基础 / EX / S / MO 各自一次
  navigate + 一次 Copilot。以前只在 ST 关处断批，活动没有 ST 时整个活动挤成
  一批，MAA 站在基础关地图上找 EX 关，必然「找不到」。

## 资源包缺分区 + 突袭不降级（2026-11-19）

用户实测：「还是不会选突袭，说的和实际给我选择的关不一样」。三处连环：

**① 资源包没有当期活动的 EX / S 关。** 包是 2026.10.29 的，TL 活动只有
`TL-1~TL-10`，`TL-S-1~S-4` 不在表里。于是 `resources.resolve('TL-S-4')`
返回 `unknown`，`copilot.search` 走到最后那条兜底：

```python
pool = _pull({'document': keyword})       # 全文搜
sid = pool[0]['content']['stage_name']    # 拿第一条的 stage_name 顶
```

全文搜「TL-S-4」的第一条是 `act55side_s02` 的作业——**挂 S-4，抄的是 S-2
的打法**，突袭行自然也拿不到（作业站按 stageId 分普通/突袭两行）。

现在改成 `resources.infer_stage(code)`：

| 输入 | 推断 | 依据 |
|---|---|---|
| `TL-S-4` | `act55side_s04` | 同活动基础关 `TL-1` 的 stageId 是 `act55side_01`，前缀 `act55side` |
| `TL-EX-3` | `act55side_ex03` | 同上，分区代号小写 + 两位序号 |
| `TL-MO-1` | `act55side_mo01` | 同上 |
| `TL-1` / `胡言乱语` | `None` | 不是「活动-分区-序号」格式，不瞎推 |

- 推断行标 `inferred=True`、`raid=True`（EX/HX/MO/S 都有突袭难度，F 除外），
  zone 字段全部继承基础关。
- `resolve()` 在「代号精确」「stageId 精确」之后、前缀展开之前插这一步；
  `copilot.search` 的兜底改成「推不出来就如实说搜不到」——**宁可搜不到，
  也不能挂错关**。
- 推断出来的关**不进 `stage_index`**（没有流程序，进了会把 `chain()` 算乱）。
  `stageplan.row_of()` 单点兜底 infer，`chain()` 对 `flow` 缺失（序号 1e9）
  的关直接返回自己；前置提示另走 `prereq_codes()`（纯代号规则）。

**② 突袭作业判「缺干员」时偷偷降级成普通。** `_select_ops` 以前要求
`raid_ok`（`mode in ready/borrow/unknown`）才排突袭，否则打印一行小字就改排
普通——用户要突袭，看到的是普通。现在只要**这关有突袭作业**就照排
（`_note` 会如实告警「缺 X，照排有可能打不过」），只有「作业站这关压根没有
突袭作业」才退回普通。

**③ 名单核对靠 `expand()`，资源包缺分区时收不拢。** `_tidy_stage_list`
现在先用代号规则自己算白名单（同前缀、同分区、序号 ≤ 点名最大序号），
再并上 `expand()` 的链——资源包没有 S 关也能把多余的 `TL-S-5` 剔掉。

## 开跑前的环境闸（`envcheck.py`，2026-11-18）

用户要求：每次跑任务前先确认模拟器在不在，**路径优先从 MAA 的配置里读**；
检测本身不要显示出来，只有真没启动、真替他启动了才说话。

### 为什么放在 `run_skill` 而不是各 skill 里

所有 skill 最终都要连模拟器，而「模拟器没开」的反面反馈是最难看懂的那种：
MaaCore 只会报 `adb 连不上`，用户第一反应是程序坏了。放一处前置闸，
`wakeup / navigate / fight / skip_story / weekly` 全覆盖；`signin` 走森空岛
网页，用 `skills._NEEDS_GAME` 排除掉——不然用户只想签个到，却被要求开模拟器。

### 路径从哪来

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `settings.emulatorPath` | 用户手动指定，最高优先（MAA 里没配 / 想指另一个实例时用） |
| 2 | MAA `config/gui.new.json` | 当前配置的 `Gui.ConnectSettings`：`Config`（LDPlayer / MuMuEmulator12 / BlueStacks / Nox / MEmu）、`AdbPath`、`Address`、`Extras.<Config>.EmulatorPath`、`InstanceIndex` |
| 3 | adb 路径反推 | `AdbPath` 是 `<模拟器目录>/adb.exe` 时，上一级就是安装目录 |
| 4 | 常见安装位置 | 只在提示里用，不拿它启动 |

### 判断「开着没有」

先 `adb devices`：目标地址是 `device` 就算通；地址对不上但**有别的在线设备**
也放行（`emulator-5554` 和 `127.0.0.1:5555` 是同一台机器的两个名字，
`stagenav.Touch.ready` 也是这么自愈的）。

不通再看进程。雷电多一层 `ldconsole list2`：那一行第 5 个字段是「安卓是否
已启动」，能精确到实例——只看进程名的话，用户多开时实例 1 开着、MAA 配的是
实例 0，会误判「已在启动中」，白等 45 秒还连不上。

### 启动

按模拟器类型挑启动器，前面的不存在就换后面：

| 类型 | 启动器 | 参数 |
|---|---|---|
| LDPlayer | `ldconsole.exe` | `launch --index <实例>` |
| MuMuEmulator12 | `shell/MuMuManager.exe` | `control -v <实例> launch` |
| BlueStacks | `HD-Player.exe` | — |
| Nox | `Nox.exe` | 首个实例空参；多开 `-clone:Nox_<实例>` |
| MEmu | `MEmuConsole.exe` | `-v <实例>` |

只负责「点着火」，不等它开完机：之后轮询 `adb devices`，最多 90 秒
（`wait` 参数可覆盖），期间每 15 秒往日志写一句进度。真机实测雷电 14
从冷启动到 `emulator-5554 device` 是 **20.7 秒**。

### 多开：启动哪一台由**地址**说了算（2026-11-23）

用户报「只能帮我启动 5554 的模拟器，启动不了 5556 的」。两个洞叠在一起：

1. 实例号取自 MAA 档案的 `InstanceIndex`，而多开用户那八份档案几乎全写 0
   → 不管要哪台，`launch --index 0` 永远拉起第一个实例；
2. `ensure(address=...)` 只查不启：目标不在线就回一句「先把它开起来再重跑」，
   等于把「第二个实例没开」当成用户填错了地址。

现在 `read_config(..., address=...)` 会把序号按地址反查出来（`indexFrom='地址'`，
**压过**档案）：

```
ldconsole adb --index N --command get-serialno
  → emulator-5554            （实例开着：回序列号）
  → error: device 'emulator-5558' not found   （没开：错误行里也带序列号）
```

拿 `list2` 的每个序号跑一遍，`序列号 == 地址`（或 `地址端口 == 序列号端口` 或
`== 序列号端口 + 1`，`emulator-5556` 与 `127.0.0.1:5557` 是同一台）就是那台。
序号在本机 `list2` 里**不存在**时返回 `(None, '')`——宁可不启动，也不去拉
一台不存在的实例。MuMu 之类没有反查手段的返回 `None`，走老口径「只检查、
不代启动」。

启动后 `wait_ready(..., wants=[地址, 序列号])` **只等这几台**：以前是「有任意
一台在线就算通」，多开时第一台一响就往下跑，最后连到错的那台。

配合 `skills.device_of` 的改动：钉死/点名的地址不在线时**不换台**，原样返回
那个地址（交给 `ensure` 去启动它），只有档案地址（没钉过）才有「单台在线就
顶上」的老兜底。

### 日志口径（用户要的「不显示出来」）

| 情况 | 日志 | 返回值 |
|---|---|---|
| adb 已通 | **一行都不写** | `(True, 'adb 正常（…）')` |
| 进程在、adb 还没通 | `已经在跑了，等 adb 就绪…` | 等到通 → True；超时 → False |
| 没启动、自动启动开 | `…没启动，正在帮你启动…` + `启动命令已发出…` + `已启动，adb 就绪` | True |
| 没启动、自动启动关 | 无 | False，提示自己开 |
| 起不来 / 目录不存在 / 类型不认识 | 无（只在返回值里） | False，写明原因 |

返回 False 时 `run_skill` 直接把任务置为 error（`环境没准备好：…`），不往下
走到「翻了几十屏没找到卡片」那种更误导人的报错。读不到 MAA 配置（还没引导）
时放行——宁可放过，不能因为检测本身有问题把所有任务堵死。

### 已知边界

- 只查模拟器，不查游戏进程。游戏要谁拉起是 `wakeup` / MAA `StartUp` 的事。
- `describe()` 是只读诊断入口（类型 / 目录 / 实例 / adb 现状），不启动任何东西。
- 沙箱里 `Popen` 出去的模拟器会随命令会话结束被回收（实测），所以「启动后
  保持运行」只能在常驻进程（dev 服务 / exe）里验。

## 设置页改版后的两个「能动账号文件」的入口（2026-11-18）

设置弹窗从「一列到底」改成左 tab + 右内容（账号 / MAA / 版本 / 助手 /
数据与日志 / 黑名单）。界面本身在 `docs/frontend.md`，这里只记后端口径——
新增的两个入口都会直接改账号 json，错一点就是数据事故。

### token：默认只读，失效才解锁

| 情况 | 界面 | 依据 |
|---|---|---|
| 有 token、`check` 通过 | `修改` 置灰，只能看 | 凭证没坏，允许改等于给「粘错号」开门 |
| 有 token、`check` 明确返回失效 | 解锁，改成「修改」 | 唯一真正需要换 token 的场景 |
| 没存过（MAA 导入建号） | 解锁，文案是「添加」 | 不是「失效」，别用失效话术误导 |
| `broken`（密文本机解不开） | 解锁，文案是「重填」 | 换电脑/换用户后必然发生，重新粘一次就好 |

`/api/account/token/check` 用 `sklogin.verify_token()`（`grant` 换码），
网络问题一律抛异常而不是回 `ok:false`——**一断网就把所有账号解锁**是最糟
的默认值，用户会在没坏的时候重粘一遍 token。

写回走 `/api/account/token/set`，中间多一道 uid 核对：`Skland(token)` 登录
→ `bindings()` 取 uid（终末地再补 `gacha.endfield_roles()` 的 uid / roleId）
→ 和账号文件里的 uid 比。对不上就拒绝，并提示用「添加账号」——否则粘错一个
别的号的 token，下次同步会静默多出一个账号、原账号还是失效状态，非常难查。

`accounts.set_token()` 只重写 `skd` 段（`alias` 保留、`addedAt` 刷新），
练度 / 进度 / `maa` 绑定全部原样：用户是来换凭证的，不是来重新同步的。

### 切号名：`api_account_maa` 里统一归一

`accountName` 现在可以直接在设置 → MAA 路径里按账号填。规则不在这儿重写，
一律 `acctname.check(nm, channel)`：能吃 `5505` / `136` / `23****45` /
`157****3635` / 全角与带横线的写法；官服只拦「填了昵称」（MAA 拿昵称去
账号列表里子串匹配，死找几十次后超时，整轮挂机废掉）。**空串放行**——
「还没填」和「填错了」是两件事，用 `check()` 原文会把清空输入框也拦下。

同一个接口还收 `profile`（MAA 的配置档案名）和 `accountNameFrom`
（`manual` / `profile`）。设置页里选了配置文件，前端会把该档案 StartUp 的
`AccountName` 带进输入框、来源标 `profile`；用户一改就标 `manual`，之后
`acctbind.resolve` 以他填的为准。**档案本身只读**，`gui.new.json` 一个字段
都不写。

## 卫戍协议速刷（`garrison.py` / `garrisondl.py` / `resources/garrison/`，2026-11-20）

**核心不在仓库里**。真正干活的 `service.mjs` 是第三方插件
（`ai-desktop.arknights-medal-farm`，GPL-3.0-or-later，约 114MB，带 MaaFramework
原生模块），不进 git、不进安装包。首次打开卫戍协议页时 `garrisondl.py` 从小黑盒
扩展记录（`%APPDATA%\HeyboxDesktop\...\records\*.catalog.json`）拿 CDN 直链，
拿不到就用锚定过 sha256 的 v1.1.4 回退直链，逐文件校验后落到
`<运行根>/garrison/medal-farm-v<版本>/`。

**一个账号一个宿主进程**：`host.mjs` 用 Node 把核心包成 HTTP 服务（随机端口 +
`x-garrison-token` 握手），Python 只做进程编排和转发。`--parent-pid` 看门狗保证
MAA-PRTS 崩了宿主自己退出，不会变成孤儿继续点游戏。设备与截图方式从账号绑定
MAA 的 `gui.new.json` 里 `ConnectSettings` 抄（MuMu→`mumu`+Extras 快速截图、
雷电→`ldplayer`、PC 客户端→`pc`）。

### 核心文本补丁（`resources/garrison/patches.json`）

核心是混淆过的单文件，页面识别遇到游戏新弹窗就会卡住。改它（而不是在 MAA-PRTS
侧另挂一个 adb 看门狗去抢截图和点击）是因为**点哪儿仍由核心自己按 OCR 文本算**，
分辨率、UI 缩放、主题全都天然自适应；看门狗得把坐标写死，还会跟核心抢输入。

`apply_core_patches(core)` 在每次 spawn 宿主前跑一遍：

| 情形 | 行为 |
|------|------|
| 规则里的 `replace` 已在文件里 | 记 `skipped`，不重复替换（幂等） |
| `find` 能找到 | 替换一次，首次改动前把原文件备份成 `service.mjs.orig` |
| `find` 找不到（核心升级换了写法） | 记 `miss` + 日志，**一个字节都不动** |

**2026-11-20 实证**：点「准备就绪」后游戏弹出「确认准备就绪吗？当前仍处于临时
整备区的干员和装备，将在进入战斗后自动销毁（召唤物仍会在下个休整期出现）」，
`garrison-<账号>.log` 里是 `[t] r9 prep.ready 115998ms` 紧跟
`[trace] prep -> unknown`，然后空转 90 秒报「画面持续无法识别」。原因是核心把
`confirm-ready` 页判定成「`确认准备` **且** `剩余资金|未使用的|指令`」，这个弹窗
没有后三个词，落到 `unknown`。补丁把条件放宽为再认 `临时整备区|召唤物|自动销毁`，
就走进核心自带的 confirm 分支：`Ma()` 找不到「本局不再提示」返回 null（跳过勾选），
`wt(state, /^确认$/)` 点 OCR 到的「确认」，然后 `Z(1200)` 继续战斗流程。

核心升级（v1.1.5+）后若 `find` 脱靶，补丁只会在 app.log 里留一行 miss，核心照常
跑、但这个弹窗问题会复发 —— 那时更新 `patches.json` 里的 `find` 就行，逻辑不用改。


### 核心目录别写死版本号（2026-11-20 实证）

`garrison.core_dir()` 原来的候选里写死 `garrison/medal-farm-v1.1.4/core`，而
`garrisondl` 是按本机小黑盒记录装**上游最新版**（那次装的是 v1.1.5）。两边一错位
就成了死循环：下载器 `_installed_pkg_dir()` 扫到 v1.1.5 → `start()` 立刻置
`stage=done`；`core_dir()` 只认 1.1.4 → 返回空 → `needsDownload` 恒为 true；
前端在 `stage === 'done'` 分支里又调 `start()`，每 1.4 秒一轮，永远停在
「核心就绪，正在启动…」，重启也没用（下次 `start()` 照样 immediately done）。

现在分两层收口：

| 层 | 行为 |
|----|------|
| `core_dir()` | 先扫 `garrison/medal-farm-v*/core` 与 `garrison-standalone/medal-farm-v*/core`，按 `garrisondl.ver_key` 取**版本最高**的一份（数字比较，`1.1.10 > 1.1.9`）；扫不到才退回 `garrison/core` 这类无版本号的固定位置 |
| `status()` | 若 `needsDownload` 却拿到 `download.stage == 'done'`，直接把 stage 改写成 `error` 并说明「核心包 vX 已下载，但没找到 core/service.mjs」 |
| 前端 | `garHandleMissing()` 只在 `idle` 自动触发下载；`done` 最多再催两次，之后停在错误态给重试按钮（点击重试会重置计数），不会无限轮询 |

**推论**：以后核心升到 v1.2.0、v2.0.0 都不用改代码；只有把包挪到非
`medal-farm-v*` 的目录结构时才需要动 `core_dir()`。

## 定时任务（`scheduler.py`，2026-11-22）

设置里的「定时任务」页：到点启动外部程序，支持链式启动。只做「把用户的程序
按时拉起来」这一件事——挂机脚本、模拟器启动器、别的游戏的自动化工具、cmd
批处理都能原样填进来（程序 + 参数照抄）。

| 概念 | 实现 |
|---|---|
| 触发 | `mode=time`：`weekdays`（1=周一…7=周日）+ `time`（HH:MM）；`mode=chain`：接在上一条后面 |
| 动作 | 启动一个程序（exe / bat / cmd）。bat/cmd 走 `cmd /d /c "<整条命令行>"`，路径带空格也不会拆错 |
| 预设 | `preset`：`custom` / `self` / `maa` / 其它游戏，只决定「先试哪几个 exe 名」和默认参数 |
| 冲突 | 同一时刻只跑一个：`conflict=stop` 停掉在跑的，`skip` 跳过本次 |
| 链式失败 | `onChainFail=continue` 继续下一个，`stop` 停掉整条链 |

### 程序预设（2026-11-23）

预设解决「别让用户自己去找 exe 在哪」，但不写死路径——各家装在哪差别太大，
猜错比不猜更烦人。预设只给 `exes`（按文件名去常见位置找）和 `args`（参数栏
留空时的兜底）：找到就回填，找不到让用户点「浏览」。

| id | 名称 | 怎么找 |
|---|---|---|
| `custom` | 自定义程序 | 不猜，必须自己填 |
| `self` | MAA-PRTS 本体 | 打包＝本进程 exe；源码＝同一个 python + `backend/main.py` |
| `maa` | MAA 官方客户端 | 设置里的 `maaDir` → `maaDirs` 历史 → 正在跑的 MAA 进程 → 扫盘 |
| `bettergi` / `onedragon` / `okww` / `maaend` / `m9a` / `mfaa` | 原神 / 绝区零 / 鸣潮 / 终末地 / 重返未来 / MFA | 按 `exes` 名扫盘（限深 2 层、限时 1.5 秒、跳过系统目录） |

- **预设只是「猜」，不是「覆盖」**：`resolve()` 只补空的 `program` / `args` /
  `cwd`，用户填了的一律不动；任务里 `program` 空着也能存，跑的时候按预设再
  找一次（换机器、挪目录都不用改任务）。
- **想加自己的预设**：`config/schedule-presets.json` 写
  `[{"id": "xx", "name": "显示名", "exes": ["xx.exe"], "args": "--foo"}]`，
  同 id 覆盖内置那条，不用改代码。
- **探测结果缓存 5 分钟**（`_scan_cache`）：界面每次打开对话框不必重扫全盘；
  要强制重找用 `detect(id, refresh=True)` 或 `/api/schedule/detect?id=xx&refresh=1`。

几个踩过的点：

- **只认当前分钟，不补跑**。程序关着时错过的计划点直接跳过——开机瞬间把前几天
  的任务一起补跑，比漏一次更糟。
- **链式只排下一个**。每条任务跑完只找它后面最近的一条 chain 任务；一次性把
  整条链全排进队列的话，同一个任务会被前后两条各排一次（实测跑了两遍）。
- **停用的 chain 跳过但不截断**：A → B（停用）→ C 会跑 A、C。
- **杀进程要杀树**。bat 是 cmd 的子进程，只 terminate cmd 会留下孤儿；Windows
  上统一 `taskkill /F /T /PID`。
- **参数按 `CommandLineToArgvW` 拆**（跟 CreateProcess 同一套规则）。`shlex`
  在 Windows 上会把引号留在参数里，`--x "a b"` 会变成 `'"a b"'`。
- **数据/日志**：`config/schedule.json`（原子写）；`debug/schedule.log` +
  内存环形缓冲。设置页 5 秒轮一次 `/api/schedule/status`，跑完的提示靠
  `events`（带 seq，同一个事件不会弹两遍）。

接口：`/api/schedule/status`（`?since=<seq>` 拿增量事件）、`/api/schedule/save`
（整表）、`/api/schedule/run`、`/api/schedule/stop`、`/api/schedule/presets`
（预设表）、`/api/schedule/detect`（选预设时回填 program/args/cwd）。以后让
agent 帮用户写定时，直接调这些就行，不需要另开一套。

线程在 `main()` 里起（`scheduler.start()`：tick + worker 两个守护线程），
起不来只记一行警告，不影响主流程。

## 开机自启（`autostart.py`，2026-11-25）

需求原话：「加一个开机自动的选项，默认不勾选，用户勾选后才生效，一定不要默认勾选，
我极度厌恶没有在我同意情况下默认开机自启的软件」。所以这里只有三条铁律：

1. **默认 `False`**，`settings.py` 里那行注释写着「永远不许改成 True」。
2. **源码模式一律不碰注册表**：`supported()` 要求 `sys.frozen`。开发时启动一次就把
   打包版那条 `Run` 项删掉的话，用户会以为自启坏了；`sync()` 在源码模式返回
   `skipped`，界面照实说「当前是源码运行，打包后才行」。
3. **只在我们自己的值上动手**：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
   下名为 `MAA-PRTS` 的一项，读写删都只碰它，不动别人的项。

接口：

| 方法 | 作用 |
|---|---|
| `status()` | 只读：`{supported, enabled, command, reason}` |
| `enable()` / `disable()` | 只有用户点那个开关时才调 |
| `sync(enabled)` | 启动时对账：设置说开、注册表没有 → 补上；反之 → 删掉 |

`main()` 在 `settings.ensure_dirs()` 之后调一次 `sync()`。之所以每次启动都对账：
用户可能手动删过注册表项（或用别的清理软件删了），只信 `settings.json` 会出现
「开关是开的，实际不生效」这种没人能理解的错位。

前端只认 `POST /api/autostart {enabled: true|false}`，失败要把复选框回滚——
写不进去（组策略/杀软拦截）时不能让界面显示成已开。

## 界面主题与主题包（`themes.py`，2026-11-25）

需求：「默认黑色，新增加白色主题和卫戍协议主题；除了黑白，其他主题资源放在
pack-data 分支，用户点击下载才能用，防止 exe 体积越来越离谱」。

### 文件放在哪

| 位置 | 内容 |
|---|---|
| `frontend/style.css` + `frontend/themes.css` | 内置 dark / light，随 exe 走 |
| `config/themes/<id>/` | 第三方主题解压到这里（点过「下载」才有） |
| `pack-data:themes/manifest.json` | 远端目录：id / 名称 / 体积 / sha256 / 缩略图 |
| `pack-data:themes/<id>.zip` | 主题包：`theme.json` + `theme.css` + 字体/图标 |

启动时**不会**去读远端，只有 `/api/themes` 被调（打开设置页）才拉一次清单，
8 秒超时、失败只写 `remoteError`，页面照常用黑白两套。

### theme.json 契约

```json
{
  "id": "closure", "name": "卫戍协议 · 可露希尔", "version": "1.0.0",
  "desc": "…", "css": "theme.css", "preview": "preview.jpg",
  "pet": {"video": "pet/idle.webm", "actions": {"talk": "…"}, "every": 26, "float": true},
  "extra": [{"file": "pet/idle.webm", "urls": ["https://raw…", "https://gh-proxy…"]}],
  "license": "…"
}
```

### `extra`：**我们不代发的素材**

可露希尔桌宠那四个 888×748 VP9-Alpha webm 来自上游 `dsh-closure-pet`，它的
NOTICE 明确写了「不在 MIT 授权范围、仅限个人本机娱乐」。所以主题包（zip）里
**只放我们有权再分发的部分**（配色、像素图标、字体），角色动画写进 `extra`，
由客户端首次安装时从原作者仓库直连下载：

- 下到了 → 完整主题（含桌宠）；
- 下不到 → 只记 `extraFailed`，降级成纯配色主题，**不报错、不回滚**。
  为一个可选动画把刚装好的主题删掉是更差的结果。

`LICENSE` 与 `NOTICE` 也一并写进 `theme.json.license` 和界面提示里，别让它变成
「我们都在包里」的错觉。

### 安装流程（`install(tid)`）

1. 拉 manifest（`fetch_manifest`，三镜像 raw → gh-proxy → jsDelivr，`?t=` 破缓存）。
2. 按 `bytes` 与 `sha256` **校验一遍**再解压——镜像吐半截字节的历史上有过。
3. 解到 `<id>.tmp`（`_safe_extract` 拒绝对路径和 `..`），确认 `theme.json` 和
   `meta['css']` 存在，再原子改名成正式目录。下歪了绝不覆盖已经能用的那份。
4. `_download_extra()` 拉 `extra`（已存在的跳过，`.part` 中转再 `os.replace`）。

上限：单包 60 MB、单素材 80 MB。manifest 被写坏时不至于拉爆磁盘。

### 输出与接口

| 接口 | 作用 |
|---|---|
| `GET /api/themes` | `{themes(内置+已装), remote(可下载), installed, remoteError}` |
| `GET /api/themes?local=1` | 只读本机、不联网：`remote` 恒空、`partial: true`。设置页先用它把卡片画出来，再拉全量补「可下载」（2026-11-25：远端慢时第三方主题会像丢了） |
| `POST /api/theme/install` `{id}` | 装 / 重装，返回 `extra` 与 `extraFailed` |
| `POST /api/theme/remove` `{id}` | 删掉目录（内置主题拒绝） |
| `GET /themes/<id>/<rel>` | 主题包里的文件（`file_of()` 防目录穿越） |

`catalog()` 里两个刻意的不对称：

- **已装**主题的 `preview` 是本地路径 `/themes/<id>/preview.jpg`；
- **未装**主题的 `preview` 指向数据分支（优先 gh-proxy：raw 国内基本连不上，
  jsDelivr 对分支路径会 301）。拉不到就让前端露出主题色块，别裂图。

`usable(tid)` 是「这个 id 现在能不能用」的唯一判断（内置 → True；第三方 → 有
`theme.json` 且 `css` 文件存在）。设置里存的 `theme` 指向一个被删掉的包时，
前端 `applyTheme` 会自动退回 `dark`，不会整页无样式。

### 有一件事故意没做

没有「主题商店」这种联网列表页。manifest 就是全部，一个主题一个 zip，
`/api/themes` 一次给完。等真有五六个主题再考虑分类/搜索。

## 国内更新源（`mirror.py`，2026-11-25）

程序本体更新现在有两条链路，海外源是 `appupdate.py`（GitHub），国内源是
`mirror.py`（Mirror 酱）。整合点在 `appupdate.status()`，机制细节写在
`docs/update.md` 的「国内源」一节，这里只列接口和边界。

| 接口 | 作用 |
|---|---|
| `GET /api/mirror/status` | `{hasCdk, masked, rid, source, updateSource, cdkState, cdkExpiredAt, mirrorError}` |
| `GET/POST /api/mirror/cdk` | GET 只读；POST `{cdk}` 保存，空串 = 清除。**永远不回明文** |
| `POST /api/mirror/probe` | `{cdk?}` 用给定的（或已存的）CDK 查一次；不落盘，返回 `ok/code/hint/cdkState` |

三条边界，改代码前先看：

1. **`mirror.py` 不用 `net.open`。** 那个入口是给 GitHub 链路走代理用的；
   mirrorchyan.com 是国内 CDN，跟森空岛、作业站一样直连更快。
2. **`cdkState` 由后端翻译，前端不认数字。** 映射表在 `mirror.CDK_STATES`
   （7001~7005），非 CDK 错误统一 `error`。这样错误码表变了只改后端。
3. **`asset` 分源校验。** mirror 查到新版本时 `asset` 长这样：

   ```json
   {"name": "...", "size": 27967657, "sha256": "...", "origin": "mirror",
    "urls": ["<带时效>"],
    "github": {"urls": ["<稳定直链 x3>"], "size": 0, "sha256": ""},
    "updateType": "full"}
   ```

   `urls` 只在本次查询的内存结果里活着（`_persistable()` 落盘前剥掉），
   `download()` 发现「源是 mirror 但没 urls」会强制重查一次。
   `_download_plan()` 给每个地址配自己那套校验值——两个源的包不是同一份字节，
   混用哈希会稳定拒装。

CDK 的存法和内置助手的 `agent.key` 完全一致：`vault.protect()` → 
`config/mirror.key`，`settings.json` 里一个字都不留，所以用户贴配置也不会漏。
