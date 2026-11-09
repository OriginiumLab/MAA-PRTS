# 自动更新

程序有两条**互不相干**的更新线，别搞混：

| | 资源包 | 程序本体 |
|---|---|---|
| 更新什么 | 关卡 / 干员 / 活动数据（`arknights.json`、`paradox.json`） | exe、`_internal/`、`version.json` |
| 体积 | 约 0.6 MB | 约 13 MB |
| 谁触发 | 启动 2.5 秒后自动，静默换掉 | 启动 4 秒后只**检查**，装不装用户定 |
| 要不要重启 | 不用（换完立即生效） | 必须重启 |
| 代码 | `core/packsync.py` | `core/appupdate.py` |
| 节奏 | 上游游戏数据一变就有 | 我们发 Release 才有 |

资源包那条线（上游同步 + 客户端拉取）见 [resources.md](resources.md)，本文只讲程序本体。

## 用户看到什么

1. 有新版本时，左下角**齿轮右上角出现一个小红点**，鼠标悬停提示「新版本 vX.Y.Z 可用」；
2. 打开设置，「程序版本」那一行变成 `MAA-PRTS 2.3.0 → v2.4.0`，按钮从「检查更新」变成「查看新版本」；
3. 点开是更新弹窗：版本对比、发布时间、安装包大小、Release 里的完整更新说明；
4. 点「下载并安装」→ 下载 + 校验 → 按钮变成「**重启并安装**」；
5. 点「重启并安装」→ 窗口自己关掉 → 重开时已经是新版本。

**不会静默安装。** 下载和重启都要用户点两次，13 MB 流量和一次重启不能替用户决定。

## 检查：尽量不花 API 配额

```
HEAD https://github.com/OriginiumLab/MAA-PRTS/releases/latest
      ↓ 读 302 的 Location，拿到 v2.4.0（不消耗 GitHub API 配额）
  版本没变 → 结束
      ↓ 版本更新了
GET  https://api.github.com/repos/OriginiumLab/MAA-PRTS/releases/latest
      ↓ changelog + assets[].digest(sha256)
  API 不可用 → 按命名规则拼下载地址，照样能装（没有 sha256，只校验结构）
```

- **6 小时节流**：成功检查后 6 小时内不再自动查（`config/appupdate.json` 的 `checkedTs`）。
  失败**不写**这个时间戳，所以断网不会把下一次检查推到 6 小时后。
- **手动检查不受节流**：设置页「检查更新」走 `?force=1`。
- **版本比较按数字**：`v2.10.0 > v2.9.0`；`v2.4.0-beta.1 < v2.4.0`；
  解析不了的 tag（`latest`、`2.4`）一律当作没有更新，绝不触发。
- **预发布**：默认不提示。设置里开「接收预发布版」才会看到 `vX.Y.Z-beta.N`。

### GitHub API 不通时（国内常见）

`api.github.com` 被拦/超时是常态，但 `github.com` 的 HEAD 往往还能通。这时**不能**
把用户卡在「知道有新版本、按钮却是灰的」——那是最难受的状态。所以做了三层兜底：

1. **地址按命名规则拼**：`release.yml` 产出的资产名固定是
   `MAA-PRTS-<tag>-win-<arch>.zip`，不需要 API 就能构造
   `https://github.com/<repo>/releases/download/<tag>/<name>`，再套上两个反代；
2. **校验降级但不裸奔**：没有 `digest` 时仍校验「是合法 zip + 里面有 `MAA-PRTS.exe`」，
   弹窗里明确写「没拿到官方 sha256」，成功提示也降级成黄色；
3. **失败不占 6 小时**：API 失败时 `checkedTs` 只往后推 10 分钟，而不是 6 小时，
   网络恢复后很快就能拿到完整信息和校验值。

前端也配合改了：打包版的「下载并安装」**不再因为 `asset` 为空而禁用**，点下去先强制
重查一次（`?force=1`），拿到地址就直接下载。源码模式仍然禁用（不能自替换）。

## 安装：退出后才替换

Windows 上运行中的 exe 不能覆盖，`_internal/` 里的 DLL 也不能删，所以替换必须等进程退出：

```
下载 13 MB → 校验（字节数 + sha256 + zip 内有 MAA-PRTS.exe）
     ↓
解压到 <运行根>/update/stage/（拒绝绝对路径和 ..）
     ↓
写 <运行根>/update/install.ps1，写 update/pending.json
     ↓                          ← 用户点「重启并安装」
无窗口拉起 install.ps1（脱离父进程），主进程退出
     ↓
PowerShell：按 PID 等旧进程退出（最多 60 秒）→ 备份 exe 到 update/backup/
     → robocopy /MIR 镜像 _internal → robocopy 覆盖根级文件（exe、version.json）
     → 校验版本号 → 启动程序 → 清 stage/zip → 自删
```

**为什么是 `.ps1` 而不是 `.cmd`**：cmd 版用 `tasklist | find` 判断进程是否还在，
那条管道在 Windows 上会死锁——`tasklist` 退了，但 cmd 不关管道写端，`find` 永远
等不到 EOF。2026-11-08 真机上就是这么挂的：点「重启并安装」弹出一个黑窗口，
标题停在 `find /i "MAA-PRTS.exe"`，然后一直不动，安装卡在第一步。
PowerShell 按 PID 等进程退出，没有管道；配合 `CREATE_NO_WINDOW` 拉起，
**用户全程看不到任何窗口**。

`install.ps1` 换的是**程序本体**：`MAA-PRTS.exe`、`_internal/`，以及根级文件
（`version.json`、README 等）；**用户数据目录一个都不碰**。

```
MAA-PRTS/                    ← 程序本体整个换掉
├─ MAA-PRTS.exe              ← 覆盖
├─ version.json              ← 必须一起换：版本号读它，漏了会「装完还是旧版本」
├─ _internal/                ← 镜像（旧 DLL 会被清掉，这正是我们要的）
├─ config/                   ← 不碰：账号、设置、token 密文
├─ debug/                    ← 不碰：日志
├─ resources/                ← 不碰：可能是用户自己换的资源包
└─ update/                   ← 安装包、stage、backup、install.log，跑完自清
```

根级复制用 `robocopy /E` 而不是 `/MIR`：`/E` 只覆盖、不删除，所以 `config/`
`debug/` `resources/` 就算没进 `/XD` 排除也不会被删掉；`/XD` 是把它们整个排除，
连覆盖都不做。`_internal` 单独用 `/MIR`，是为了清掉新版本已经不再使用的旧 DLL。

脚本每一步都写 `update/install.log`（`=== 开始安装 ===` 到 `=== 安装结束 ===`），
真机出问题直接看这个文件，不用靠猜。

冒烟测试 B22 会把生成的脚本读出来，断言 `config`、`debug`、`resources`
这三个词**只出现在 `/XD` 排除列表里**，`version.json` 必须在替换清单里，
且脚本里**不许出现 `tasklist` / `find`**——防止以后有人图省事退回那条会死锁的管道。

### 失败怎么办

- 替换失败（`robocopy` 返回 ≥ 8、压缩包不完整、版本号没换成功）：脚本先把
  `update/backup/MAA-PRTS.exe` 还原，写 `update/install-error.txt`（附原因），
  然后**把旧版本重新拉起来**——不会出现「点了安装，程序没了」；
- 成功了但版本没变：`update/pending.json` 不会被清掉，新版本启动时日志记一条
  `[警告] 上次更新到 X 没生效`，前端第一次启动会弹一次提示。

排查时直接看 `debug/app.log` 里 `程序已更新：2.3.0 → 2.4.0` 或那条警告，
再看 `update/install.log`。

## 发版注意

1. **Release 的 tag 必须是 `vX.Y.Z`**：`release.yml` 从 PR 标题 `Release vX.Y.Z` 取版本号，
   tag 也用它。解析不了的 tag 用户端一律忽略。
2. **别删旧 Release 的 asset**：检查用的是 `/releases/latest`，只认最新那条；
   但如果最新是预发布，`latest` 会返回上一个正式版——这是 GitHub 的行为，符合预期。
3. **Windows 的包必须叫 `MAA-PRTS-<tag>-win-<arch>.zip`**：命名契约三处一致——
   release.yml 打包步骤、`appupdate.ASSET_NAME`（兜底下载按它拼地址）、本文件；
   `_pick_asset()` 按架构挑，挑不到才退到第一个 zip。
4. **不需要额外配置**：`digest` 是 GitHub 自己给 asset 算的 sha256，发版流程一个字都不用改。

## 排查表

| 现象 | 原因 |
|---|---|
| 一直没红点 | 设置里关了「启动后检查程序新版本」；或 6 小时内已查过；或最新 Release 是预发布而没开开关 |
| 提示有新版本但「下载并安装」是灰的 | 只拿到了版本号，API 被墙/限流，拿不到下载地址与 sha256。只能「前往下载页」 |
| 点「重启并安装」弹出黑窗口，然后一直不动 | **≤ v2.4.0 的已知缺陷**：cmd 版 `tasklist \| find` 死锁，安装卡在第一步。v2.4.1 起改成 `install.ps1`。旧版本请手动覆盖安装一次 |
| 点完「重启并安装」窗口关了但版本没变 | 看 `update/install.log` 与 `install-error.txt`；多半是杀软拦了脚本或 `robocopy` |
| 安装失败后程序自己又起来了，版本没变 | 这是有意设计：失败会回滚 exe 并重新拉起旧版本，不会让你「程序没了」 |
| 下载很慢或超时 | GitHub 直链慢，会自动退到 `gh-proxy` / `ghfast`；都失败就是网络问题，用下载页手动下 |
| 源码模式点不了安装 | 正常。源码运行不替换文件，要测请用打包版 |

## 以后接国内源

`appupdate.py` 里的下载地址是列表形式（`DOWNLOAD_TEMPLATES`），检查逻辑也只依赖
「一个能返回版本号 + 下载地址的源」。要接 MirrorChyan（Mirror酱）时：

1. 加一个 `MirrorChyanSource`，命中就用它的 URL 替换 `asset.urls`；
2. 检查顺序改成「先 Mirror酱（不带 CDK 也能查版本），超时再 GitHub」；
3. 前端不用动——它只认 `status()` 返回的那几个字段。

前提是先拿到 RID 和上传 token，并且给 13 MB 的 zip 单独开一个 RID（资源包没必要走 Mirror酱）。
