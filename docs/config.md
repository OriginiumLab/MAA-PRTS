# config —— 用户数据：设置、账号、token

## 用途

程序所有可写数据都在这个目录里：全局设置、资源版本记录，以及账号数据
（每个角色一份 JSON，token 密文就存在账号文件里）。第一次运行时目录是空的，
程序会按需自动创建。

## 目录结构

```
config/
├── settings.json                全局设置（MAA 路径、上次账号等）
├── gamedata.json                资源版本记录（自动生成）
├── <昵称> <服务器>.json         ★ 每个角色一份账号配置（练度 + 缺卡 + token 密文）
├── _index.json / _removed/      账号索引 / 删除备份（自动生成）
├── 作业站/                       导出「作业站本地导入」格式（API /api/export 生成，界面暂无入口）
├── subprof_cache.json           分支中文名缓存（边同步边学）
└── tokens.json                  旧版密文 token 库：迁移产物 + 只读兜底，不再写入
```

这些文件都可以随时删除：删掉账号 `*.json` 只是丢失缓存，重新同步就回来了
（token 密文也在里面，删掉后需要重新粘贴一次 token）。

## settings.json

```jsonc
{
  "maaDir": "D:\\2.game\\MAA-ALL\\MAA-ARK\\MAA-v6.20.1-win-x64",
  "maaDirs": ["D:\\2.game\\MAA-ALL\\MAA-ARK\\MAA-v6.20.1-win-x64"],
  "autoStartMaa": true,
  "autoUpdatePack": true,
  "autoCheckAppUpdate": true,
  "acceptPrerelease": false,
  "lastAccount": "小卡拉帕鲁灰#6408 官服.json"
}
```

| 字段 | 说明 |
|---|---|
| `maaDir` | 全局默认 MAA 根目录（含 `MAA.exe` 与 `config/gui.new.json`）。写入路径时会自动规范化斜杠 |
| `maaDirs` | 用过的 MAA 根目录历史（添加账号时「已存过」列表的数据源） |
| `autoStartMaa` | 写完作业后是否自动启动 MAA |
| `autoUpdatePack` | 启动后是否自动下载新的游戏资源包（新关卡 / 新干员 / 新活动），默认开。关掉不影响使用，只是要等发新版才有新数据 |
| `autoCheckAppUpdate` | 启动后是否检查程序新版本，默认开。6 小时最多一次，且**只提醒不自动装**——下载与重启都要用户自己点 |
| `acceptPrerelease` | 是否接收 `vX.Y.Z-beta.N` 预发布版，默认关。关掉时预发布版本不会提示 |
| `lastAccount` | 上次选中的账号文件，下次启动直接恢复 |

这个文件可以直接手改；改完要重启程序生效（程序启动时读一次并缓存）。
删掉它等于恢复默认：引导会重新出现，需要再填一次 MAA 目录。
手改坏了 JSON 也不会丢数据：启动时会把坏文件改名成
`settings.json.broken-<时间戳>` 保住原件，程序按默认设置继续运行。

## 账号文件（<昵称> <服务器>.json）

schemaVersion 2 快照（`backend/core/updater.py` 的 `build_snapshot`）加一段
`skd`。`maa` / `tags` / `note` / `skd` 是用户字段，重复同步不会被覆盖：

```jsonc
{
  "schemaVersion": 2,
  "account": { "uid": "…", "nickName": "…", "channelName": "官服", /* … */ },
  "maa":  { "dir": "D:\\…", "profile": "", "note": "" },  // 该账号绑定的 MAA 目录
  "skd":  { "alias": "大号",                               // 备注（添加时填的）
            "addedAt": "2026-11-05T07:40:00",              // 添加时间
            "data": "AQAAANCMnd8BFdERjHoAwE…" },           // base64(DPAPI(token))
  "tags": [], "note": "",
  "chars": [ /* 练度 */ ], "missing": [ /* 缺卡 */ ], "stats": { /* 统计 */ },
  "sync": { "syncedAt": "…", "source": "…" }
}
```

`maa.dir` 与 `skd` 同处一个账号文件：一个账号用哪个 MAA、拿哪个 token，
删文件即整体清除。从 MAA 导入干员识别结果（界面的「从 MAA 导入干员信息」）
也是改这份文件——只覆盖 `chars/stats/missing/sync`，其余原样保留。

### token 加密（skd 段）

token 是账号凭证，明文落盘不放心，所以**每条 token 单独**用 **Windows DPAPI**
（数据保护 API，`CurrentUser` 作用域）加密后，以 base64 密文存进账号文件的
`skd.data` 段（备注与时间可见，token 是密文）。加密的性质来自 Windows 本身，
程序自己不保存密钥：

- **只能在本机、当前 Windows 用户**下解开；拷到别的电脑、或同机换用户登录，
  都会解密失败——换电脑迁移时别指望拷文件，到新机器上重新粘贴一次 token
  就行（界面「添加账号」）；
- 一个 token 绑定多个角色时，`skd` 会出现在每个角色的账号文件里；
  「同步全部」按明文 token 去重，不会重复同步；
- 单条解不开时只跳过该条并在启动日志里点名警告（换电脑后常见），
  不影响其他账号，也不会改名/删除任何文件。

管理 token 不需要手改文件：界面「添加账号」粘贴 token 同步成功后即落盘
（同步失败不落盘，向导里还能重试）；同一 token 重新同步以最新的为准。

### 旧版 tokens.json

`config/tokens.json` 是旧布局的全局密文 token 库（格式与 `skd` 段同构的
多条目列表），现在只作为迁移产物与只读兜底存在——「同步全部」会把它一并
读进来，但程序不再写入它。**所有账号都重新同步成功后，这个文件可以手动
删掉**；删掉任何账号文件前先确认 token 还在别处有备份。

## gamedata.json

程序自动生成的「资源版本记录」，用来判断内置游戏资源包有没有更新：

```jsonc
{
  "version": "2026.11.05",          // 上次见到的资源包版本
  "fingerprint": "3af97ae0",        // 上次见到的内容指纹
  "counts": { "chars": 469, "stages": 3647, ... },
  "acts": ["act55side", "act54side", ...],   // 上次见过的活动，用来算「新活动」
  "seenAt": "2026-11-05T01:50:00",
  "pending": null                   // 有更新时是一段 {from,to,delta,newActs}，用户点「知道了」后清空
}
```

- 删掉它 = 下次启动当作首次运行，静默重新记录，**不会弹更新提示**。
- 想再看一次更新提示（测试用）：把 `version` 或 `fingerprint` 改成别的值，重启即可。

## 旧版迁移

历史上有两代旧目录，升级后首次启动会自动压平（幂等，失败不丢数据）：

1. 更旧版数据在项目根 `skland/`（明文 `tokens.txt`）→ 先搬进 `config/skland/` 中转；
2. 旧布局 `config/skland/`（`tokens.json` + `accounts/…`）→ 压平进 `config/` 本层，
   路径里的第一级 `accounts/` 剥掉：`accounts/A.json → config/A.json`、
   `accounts/作业站/… → config/作业站/`、`accounts/_removed/… → config/_removed/`、
   顶层 `tokens.json` / `subprof_cache.json` → `config/` 同名。

安全规则：目标已有同名文件就跳过（宁可不搬也不覆盖）、单个文件搬失败留在
原地下次再试、自底向上只删真正搬空的目录——`config/skland` 只要还有东西
就整体保留。token 密文原样搬运（不解密不重加密）；明文 `tokens.txt` 的加密
转换仍延后到读取时做，转换成功后原文件改名 `tokens.txt.bak` 留底，读不了的
改名 `tokens.txt.broken` 留证。若 `tokens.txt` 与 `tokens.json` 并存（txt
不迁移、文件保留），里面的 token 不参与同步，启动警告会点名提示处理。

## 隐私

- `skd` 段与 `tokens.json` 是密文，但**不要**把它当作可以随便分享的东西——
  在能解密的那台电脑上它就等于明文 token；
- 账号 JSON 里有游戏 uid、昵称、完整干员练度，上传前注意（界面暂无
  「导出」入口：想分享练度先自行删掉 `skd` 段，或用 API `/api/export`
  导出只含练度的干净数据）；
- `.gitignore` 已忽略整个 `config/`，不会误提交。
