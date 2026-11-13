# resources —— 内置游戏资源包

## 用途

程序需要一份「干员 → 名字/星级/职业」「关卡代号 → stage_id」的对照表，用来：

- 算缺卡：图鉴全集（440）减去已拥有
- 算六星总数、职业分布
- 把 `TL-10` 这样的代号解析成作业站认识的 `act55side_10`
- 把技能 id、模组 id 翻成人话

原始游戏表（`ArknightsGameResource/gamedata/excel`，9 个文件合计 64 MB）直接放进仓库太重。
`make_pack.py` 只挑用得到的字段，压成一个 **约 0.58 MB** 的 `arknights.json`，
让它跟着代码走，用户不必自己准备资源目录。

## 文件

| 文件 | 说明 |
|---|---|
| `arknights.json` | 内置资源包（已生成，直接提交） |
| `paradox.json` | 悖论模拟清单（316 条，约 38 KB，纯 json 不含图片） |
| `pack-manifest.json` | 给客户端比对的清单：版本 / 指纹 / `sha256` / 下载地址，由 `tools/sync_pack.py` 生成 |
| `make_pack.py` | 生成 `arknights.json`，CI 与本地共用 |
| `assets/app.ico` | 程序图标源码资产；构建时 `tools/build.py` 用 Pillow 另画一份到 `build/work/`，不回写本文件 |

`paradox.json` 由 `tools/build_paradox_data.py` 生成，字段是
`charId → {name, pname, stage, elite, level}`；它只用来算「哪些干员能开打悖论」，
判断依据是账号快照里的 `elite/level`，不涉及任何图像识别。

## 全自动更新

两条链路，平时都不用管：

```
上游 yuanyan3060/ArknightsGameResource（7.28 GB，只取 9 个 json）
        │  每天 12:00 / 16:00（北京时间）
        ▼
GitHub Actions  .github/workflows/pack.yml
        │  1. 取上游 main 的 commit sha
        │  2. 和上次生成的 sourceCommit 比，一样就结束
        │  3. 按 sha 定点下载 9 个 json（不 clone）
        │  4. 重新生成 arknights.json / paradox.json / pack-manifest.json
        │  5. 条数只增不减才放行
        │  6. 推到数据分支 pack-data（单提交 + 强推，不累积历史）
        ▼
用户端  启动约 2.5 秒后 /api/pack/status
        │  按 pack-data 分支读 pack-manifest.json，和本地包比 fingerprint
        ▼
        /api/pack/update → 校验 sha256 与条数 → 原子替换 resources/*.json
```

**为什么数据走 `pack-data` 而不是 `main`**：`main` 挂着规则集（强制走 PR、
需 1 个审批、只允许 squash），`github-actions[bot]` 不在绕过名单里，直推必被拒。
资源数据也不需要和代码抢同一条分支 —— 独立分支没有保护，客户端按固定分支名
拼 raw / jsDelivr 地址就行。`pack-data` 里只有三个 json + 一份 README，
每次同步都是一个孤立提交强推覆盖，所以仓库体积不会随同步次数增长。

`main` 里的 `resources/*.json` 只在发版时更新（打进 exe 作为离线基线），
两者会不同步 —— 没关系，客户端启动后会自动追到 `pack-data` 上的最新版。

**幂等约定**（改 `make_pack.py` 前必读，否则 CI 会天天提交、用户天天弹提示）：

| 字段 | 规则 |
|---|---|
| `version` | 取自上游 `version` 文件（游戏版本 `26-10-29-…` → `2026.10.29`），**不是生成日期** |
| `fingerprint` | 只由内容决定：干员/技能/模组/关卡/区域/活动。日期、`subProfessions` 都不参与 |
| `generatedAt` | CI 传上游提交时间；本地跑才用当前时间 |
| `subProfessions` | 落盘前按 key 排序（本地有缓存、CI 只有旧包，两边来源不同但结果必须一致） |

同一份上游数据，任何时候、任何机器生成，产物都逐字节相同 —— CI 才能用
`git diff` 判断「要不要提交」，客户端也才不会白下一遍。

## arknights.json 结构

```jsonc
{
  "schema": 1,
  "version": "2026.10.29",                    // 游戏版本日期，界面「资源版本」显示它
  "fingerprint": "28b2db93",                  // 纯内容指纹，判更新只看它
  "generatedAt": "2026-11-05T19:09:34Z",      // 上游提交时间
  "gameVersion": "26-10-29-15-36-11-33649006",// 上游 version 原文
  "sourceCommit": "6e5c0541299e...",          // 上游提交 sha，CI 用它判断要不要重算
  "counts": { "chars": 469, "skills": 1846, "equips": 924, "stages": 3647, "zones": 492, "activities": 325, "subProfessions": 73 },

  "chars": {
    "char_4100_caper": {
      "name": "跃跃", "rarity": 3,            // 0 基：3 = 四星
      "profession": "SNIPER",                // 英文枚举，界面再翻中文
      "subProfessionId": "loopshooter",
      "isNotObtainable": false               // true = 剧情 NPC，不计入图鉴
    }
  },
  "skills": { "skchr_xxx_2": "技能名" },
  "equips": { "uniequip_xxx": { "uniEquipName": "模组名", "typeIcon": "original|xxx" } },
  "stages": [{ "id": "act55side_10", "code": "TL-10", "name": "活着的重量",
               "zone": "act55side_zone1", "story": false }],
  "zones":  { "act55side_zone1": { "name": "残照终尽", "type": "ACTIVITY" } },
  // 活动正式名，界面显示用这个（zone 名只是地图分区名，不是活动名）
  "activities": { "act55side": { "name": "时间啊，向前！", "start": 1793487600, "end": 1795291199 } },
  "subProfessions": { "loopshooter": "回环射手" }
}
```

`version` / `fingerprint` 是 `backend/core/version.py` 判更新的依据：换包后
`version.check()` 拿它和 `config/gamedata.json` 记的上次值比对，不一致就写一条
`pending`，前端顶部弹「游戏资源已更新到 x，新增活动 …」。
活动代号字母（如 `TL`）不入包，运行时从关卡代号里现算（`resources._code_prefix()`）。

分支中文名（`subProfessions`）没有公开静态表，是从森空岛接口学来的种子（73 条），
运行时还会继续学，学到的写进 `config/subprof_cache.json`。

`paradox.json` 也带 `fingerprint`（`chars` 的内容哈希）和 `sourceCommit`，
客户端靠它判断悖论清单要不要一起换。

## 手动生成（不依赖 CI）

```bat
python resources\make_pack.py --excel "D:\ArknightsGameResource\gamedata\excel"
python tools\build_paradox_data.py --src "D:\ArknightsGameResource"
```

或者一条命令全做（含 manifest，和 CI 完全同一条路径）：

```bat
python tools\sync_pack.py --from-dir "E:\.github-ARK\ArknightsGameResource"
```

`sync_pack.py` 参数：

| 参数 | 说明 |
|---|---|
| `--check-only` | 只问上游有没有新提交，不下载 |
| `--force` | 上游提交没变也重新生成 |
| `--from-dir` | 用本地上游目录，不联网下载（离线/调试） |
| `--sha` / `--commit-time` | 指定上游提交与时间（补录、复现问题） |
| `--keep-work` | 保留下载的临时目录 |

`make_pack.py` 参数：

| 参数 | 说明 |
|---|---|
| `--out` | 输出路径，默认就是 `resources/arknights.json` |
| `--subprof` | 分支名缓存，默认自动找 `config/subprof_cache.json`（旧版 `config/skland/…` 与 `skland/…` 兜底） |
| `--game-version` | 上游版本串，默认自动读 `<excel>/../../version` |
| `--source-commit` / `--commit-time` | 写进包里的来源信息 |

跑完会打印各表条数和体积，并逐项和旧包对比（`+3` / `持平` / `-2 <- 变少`）。
条数只该增不减，有变少时退出码为 `2`（`sync_pack.py` 同样返回 2，CI 会失败不提交）。

`subProfessions`（分支中文名）不是游戏表里的字段，是运行时从森空岛学的。
生成时会**先继承旧包里已有的条目，再用运行时缓存覆盖**，所以缓存文件不存在也不会
把已有的 73 条清零。

当前基准（游戏版本 `26-10-29`）：`chars 469`（可获取 440）/ `stages 3647`（其中剧情关 208）/
`skills 1846` / `equips 924` / `zones 492` / `activities 325` / `subProfessions 73`，体积 0.58 MB。

## 用户端怎么拿到新包

1. **自动**（默认开）：启动 2.5 秒后比对 manifest，有新版就下载替换，弹一条
   「游戏资源已更新到 2026.10.29」，新活动在快捷栏打 NEW 角标并置顶；
2. **手动**：设置 → 游戏资源 → 「检查更新」；
3. **离线**：把 `arknights.json` / `paradox.json` 放进 exe 同目录的 `resources\`，优先级高于内置那份。

三种方式最终都落到 `<运行根>/resources/`，旧文件留 `.bak` 可回滚。
设置里的「启动后自动更新游戏资源」可以整体关掉（`autoUpdatePack`）。

## 注意

- 资源包里只有干员名、关卡名这类**基础名词**，不含图片、立绘、剧情文本。
- 关卡表里排除了没有 `code` 的内部关卡（剿灭、教程占位等），`stages` 3647 条足够覆盖
  主线、活动、悖论模拟、资源关。
- `stages` 里同时保留了 `TR-` 系列与塔模式重名关，解析时在 `resources.resolve()` 里排除。
- `story: true` 是剧情关（`stage_table` 的 `isStoryOnly`，当前 208 条）：只有剧情没有战斗，
  作业站永远不会有作业，`resources._stages()` 会把它们整体剔掉，不参与搜索与活动展开。
  旧包没有这个字段时靠代号/id 兜底（`xx-ST-n` / `10-1` / `EG-3` 三种形态），见 `_is_story()`。
- 上游仓库 7.28 GB（含全部立绘素材），**任何地方都不要 clone**；CI 只按 sha 下 9 个 json。
- GitHub 的定时任务在仓库连续 60 天没有提交时会被自动停用，到 Actions 页面点一次
  「Enable workflow」即可恢复。
