# MAA-PRTS · 明日方舟作业控制台

用森空岛 token 读取账号练度，自动比对 [prts.plus](https://prts.plus/) 作业站的干员要求，
告诉你哪份作业能直接抄、哪份差一个人、哪份练度不够；挑好后一键写进 MAA 的多作业列表。

整个程序**不依赖外部脚本和游戏资源目录**，token、账号配置、日志全部保存在本项目的
`config/` 里（token 经 Windows DPAPI 加密，仅本机可解），随时可以整个文件夹打包带走。

## 下载

**[⬇ MAA-PRTS 最新版 · Windows x64（约 13 MB）](https://github.com/HuiParu/MAA-PRTS/releases/latest)**

解压到任意可写目录（不要放 `C:\Program Files`），双击 `MAA-PRTS.exe` 即可。
`_internal` 文件夹必须和 exe 放在一起，别只把 exe 拖出来。

```
MAA-PRTS\
  MAA-PRTS.exe
  _internal\        ← 必须一起保留
  README.md         ← token 获取步骤 + 常见问题
```

首次启动会引导两步：选择 MAA 根目录（含 `MAA.exe` 的那一层）→ 粘贴森空岛 token。
不想下载成品也可以按下面的「快速开始」从源码跑。

---

## 功能

| 功能 | 说明 |
|---|---|
| 账号同步 | 粘贴森空岛 token，自动拉取干员、等级、精英化、专精、模组，生成账号配置 JSON |
| 多账号 | 一个 token 绑定多个角色时分别生成；左侧列表一点即切 |
| 账号分组 | 左侧顶部「明日方舟 / 终末地」两栏切换，各带账号数，只显示当前这一栏；切到终末地那一栏会自动选中该组第一个账号 |
| 终末地账号 | 同一个 token 绑了《明日方舟：终末地》角色时，自动生成卡片并归到终末地那一栏；选中它只提供抽卡统计（MAA 只跑方舟，作业获取整体隐藏） |
| 抽卡统计 | 「抽卡统计」模块：总抽数、六星数/出率、平均出六星、距上次六星，星级分布、六星记录（含第几抽）、按卡池汇总、最近记录；记录只增不减地存在本地，没网也能看 |
| 练度统计 | 干员数 / 精二 / 满级 / 六星 / 专三 / 模组 / 潜能 / 信赖 |
| 缺卡清单 | 点顶部「缺卡」，按星级列出还没有的干员名字 |
| 作业匹配 | 输入关卡代号（`TL-10`）、活动（`act55side`）、系列（`TR-`）或中文名，逐份比对干员与练度 |
| 快捷活动 | 顶部胶囊列出近期活动（进行中的排最前），带关卡代号字母（`时间啊，向前！ TL`），点一下直接搜 |
| 作业集 | 单关挑一份，或活动每关自动挑最佳；支持复制 `maa://` 代码 |
| 拖动排序 | 作业集里的作业可以拖动换顺序，顺序就是写进 MAA 的执行顺序 |
| 写入 MAA | 关 MAA → 备份配置（只留最近 2 份）→ 下载作业 → 写 `TaskList` → 重启 MAA，全程日志可见 |
| 悖论模拟 | 「快捷」行的 `悖论模拟` 入口：按账号练度算出哪些干员能开打，勾选后自动取作业加进作业集；导入过的会记住，下次默认不勾 |
| 窄屏适配 | 窗口较窄时作业集收成右侧抽屉，右下角浮动按钮开关，竖屏笔记本也能用 |
| 资源版本 | 资源包带版本号和指纹，更新后首次启动会提示新增干员/关卡与活动 |
| 完全离线 | 关卡表、干员表内置在 `resources/arknights.json`，只有搜作业和同步账号需要联网 |

---

## 快速开始

### 方式一：用 Release 里的压缩包（普通用户）

从上面的「下载」拿最新版压缩包（文件名形如 `MAA-PRTS-vx.y.z-win-x64.zip`），解压后双击 `MAA-PRTS.exe` 即可，不用装 Python。

### 方式二：自己打包（需要 Python 3.10+）

双击 `build.bat` 即可：首次运行会自动创建项目专用环境 `.venv` 并安装依赖，然后调用 PyInstaller 打包。

产物在 `build\MAA-PRTS\`：整个文件夹就是发布包，压缩成 zip 即为 Release 附件。

### 方式三：跑源码（需要 Python 3.10+）

双击 `run.bat`：优先启动编译版，没有编译版时自动以源码模式运行，首次会创建 `.venv` 并安装依赖。

调试模式（带 devtools）：双击 `debug.bat`。

自动初始化失败时，也可以手动准备环境：

```bat
py -3 -m venv .venv
.venv\Scripts\pip install pywebview pythonnet pyinstaller pillow
```

---

## 首次启动会做什么

第一次打开会弹一个两步引导：

1. **MAA 根目录** —— 选包含 `MAA.exe` 的文件夹，点「检测」确认能找到
   `MAA.exe` 与 `config/gui.new.json`。
2. **森空岛 Token** —— 粘贴 token（可加备注）。点击「怎么拿 token」有获取步骤：
   浏览器登录森空岛社区 → F12 → Network → 找发往 `zonai.skland.com` 的请求头里的 `token`。
   同步成功后 token 会加密记进各账号文件的 `skd` 段（Windows DPAPI，仅本机可解），
   以后点左上角的刷新按钮即可一键同步全部账号。

两步都过了点「保存并同步账号」，窗口会实时打印同步日志，完成后自动切到该账号。
如果暂时没有 MAA，可以先点「稍后设置」——搜作业、复制代码都不受影响，只有「写入 MAA」需要它。

---

## 目录结构

```
MAA-PRTS-1/
├── frontend/            页面（HTML/CSS/JS），由后端本地服务托管
├── backend/             服务端：窗口、HTTP 接口、匹配算法、MAA 读写
├── resources/           内置游戏资源包（干员表/关卡表）与打包脚本
├── docs/                各目录的说明文档（backend.md、config.md 等）
├── config/              ★ 用户数据：settings.json、账号练度 *.json（token 密文随账号文件）
├── debug/               运行日志 app.log（可随手删）
├── build/               构建产物（MAA-PRTS/ 发布包；中间文件用后即删）
├── tools/               setup/（启动打包的环境引导）、build.py（打包脚本）、smoketest/（冒烟测试）
├── run.bat / debug.bat / build.bat / "build and run.bat"
└── README.md
```

各目录的用途与实现细节集中写在 `docs/` 下（如 `backend.md`、`config.md`、`tools-smoketest.md`）。

---

## 数据都存在哪

| 内容 | 位置 | 说明 |
|---|---|---|
| 账号配置 | `config/<昵称> <服务器>.json` | 干员练度 + 缺卡清单 |
| token 密文 | `config/<昵称> <服务器>.json` 的 `skd` 段 | Windows DPAPI 加密，仅本机当前 Windows 用户可解；旧版 `config/tokens.json` 兜底 |
| 分支名缓存 | `config/subprof_cache.json` | 分支中文名边用边学 |
| 应用设置 | `config/settings.json` | MAA 路径、默认配置名、上次选中的账号 |
| 资源版本记录 | `config/gamedata.json` | 上次见到的资源包版本，用来判断要不要提示更新 |
| 运行日志 | `debug/app.log` | 超过 2 MB 自动轮转，随时可删 |

- 上传 GitHub 时这些都会由 `.gitignore` 排除，仓库里只有代码和资源包。
- token 密文绑定本机与 Windows 用户：换电脑时把账号 JSON 拷过去，练度数据
  还能看，但里面的 token **解不开**，到新机器上重新粘贴一次 token 即可。
- 例外只有一个：MAA 根目录本身在你指定的位置，程序只按你填的路径去读写它的 `config/`。
- 例外只有一个：MAA 根目录本身在你指定的位置，程序只按你填的路径去读写它的 `config/`。

---

## 打包

```bat
python tools\build.py
```

产物：`build\MAA-PRTS\MAA-PRTS.exe`（onedir，约 30 MB）。

打包只带 `frontend/` 与 `resources/` 两个只读目录。运行时如果 exe 还在项目里，
数据目录会自动定位到项目根；如果把 `build/MAA-PRTS` 单独拷走，数据就写在 exe 旁边。

---

## 更新游戏资源包

**现在不用手动跑了。** GitHub Actions（`.github/workflows/pack.yml`）每天北京时间
12:00 / 16:00 检查上游资源库，有新数据就自动重新生成，推到独立的数据分支 `pack-data`
（`main` 有规则集，bot 推不进去）；用户那边启动 2.5 秒后自动拉新包，0.6 MB，
静默换掉不用重启。详见 `docs/resources.md`。

需要手工兜底时（排查、或上游没同步）：

```bat
python resources\make_pack.py --excel "D:\ArknightsGameResource\gamedata\excel"
```

生成的新包带 `version`（上游游戏版本日期，如 `2026.10.29`）和 `fingerprint`
（只看内容的指纹）。同一份数据无论跑多少次，产物逐字节一致——这是自动化的前提。

换了资源包之后第一次启动，界面顶部会提示：

```
游戏资源已更新到 2026.10.29（原 2026.10.01）
+1 干员 · +7 关卡 · 新活动 时间啊，向前！ TL、月行水上 SR …     [知道了]
```

同时新活动会被顶到快捷栏最前面并标上 `NEW`。提示条点「知道了」才消失，
不点就每次启动都提醒一次。详见 `docs\resources.md`、`docs\backend.md`。

---

## 更新程序本体

程序启动 4 秒后查一次 GitHub Releases（6 小时节流），有新版本只在设置齿轮上点个红点，
**不自动安装**。用户在弹窗里点「下载并安装」→「重启并安装」，
退出后由 `update/install.ps1` 替换 `MAA-PRTS.exe` 与 `_internal/`，
`config/`、`debug/`、`resources/` 一律不碰。详见 `docs/update.md`。

---

## 更新悖论模拟清单

悖论清单在 `resources\paradox.json`（哪个干员有悖论、关卡代号、解锁条件）。
它和资源包**在同一次同步里一起生成**（同一个上游提交、同一个 manifest），平时不用管。
手工重跑时：

```bat
python tools\build_paradox_data.py --src "E:\.github-ARK\ArknightsGameResource"
```

只需要资源库里的两个 json，不用任何图片。校验点与排查见 `docs/resources.md`。

---

## 常见问题

**搜索很慢 / 转圈很久**
活动关卡多时需要逐关去作业站拉数据，十几秒属于正常。单关卡一般 2～4 秒。

**某关卡搜出来是别的关卡的作业**
作业站的 `levelKeyword` 对部分关卡不生效，程序会逐条校验作业的 `stage_name`，
不匹配时改用关卡代号重搜。

**点「写入 MAA」后列表为空**
确认 MAA 根目录填对（存在 `config/gui.new.json`）、写入的配置名存在；
MAA 运行时会回写配置，所以必须先关闭它——这一步程序会自动做。

**token 失效**
森空岛 token 有有效期，失效后同步会打印 401。重新抓一个 token，
用界面「添加账号」粘一个新的即可（同一账号以最新 token 为准）。

---

## 说明

本项目只读取森空岛公开接口和 prts.plus 的作业数据，不上传任何账号信息。
作业版权归各作业作者所有，使用时请遵守 MAA 与作业站的相关约定。

以 [MIT License](LICENSE) 开源。仓库里不含任何 token、账号数据与编译产物，
`config/` 下只有说明文档，首次运行会自行生成数据文件。
