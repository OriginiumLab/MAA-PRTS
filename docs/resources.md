# resources —— 内置游戏资源包

## 用途

程序需要一份「干员 → 名字/星级/职业」「关卡代号 → stage_id」的对照表，用来：

- 算缺卡：图鉴全集（440）减去已拥有
- 算六星总数、职业分布
- 把 `TL-10` 这样的代号解析成作业站认识的 `act55side_10`
- 把技能 id、模组 id 翻成人话

原始游戏表（`ArknightsGameResource/gamedata/excel`）有 50 MB，直接放进仓库太重。
`make_pack.py` 只挑用得到的字段，压成一个 **约 0.5 MB** 的 `arknights.json`，
让它跟着代码走，用户不必自己准备资源目录。

## 文件

| 文件 | 说明 |
|---|---|
| `arknights.json` | 内置资源包（已生成，直接提交） |
| `make_pack.py` | 生成脚本，只在游戏大版本更新后跑一次 |
| `paradox.json` | 悖论模拟清单（316 条，约 38 KB，纯 json 不含图片） |
| `assets/app.ico` | 程序图标（`tools/build.py` 会用 Pillow 重新画） |

`paradox.json` 由 `tools/build_paradox_data.py` 生成，字段是
`charId → {name, pname, stage, elite, level}`；它只用来算「哪些干员能开打悖论」，
判断依据是账号快照里的 `elite/level`，不涉及任何图像识别。

## arknights.json 结构

```jsonc
{
  "schema": 1,
  "version": "2026.11.05",                    // 资源版本（生成日期），前端和更新提示用它
  "fingerprint": "3af97ae0",                  // 内容指纹，同一天重新生成也能区分
  "generatedAt": "2026-11-05T01:20:00+08:00",
  "counts": { "chars": 469, "skills": 1846, "equips": 924, "stages": 3647, "zones": 492, "activities": 325 },

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

`version` / `fingerprint` 是 `backend/core/version.py` 判更新的依据：
换了包但版本号没变、指纹变了，也会被认成更新（打印「资源已更新」提示条）。
活动代号字母（如 `TL`）不入包，运行时从关卡代号里现算（`resources._code_prefix()`）。

分支中文名（`subProfessions`）没有公开静态表，是从森空岛接口学来的种子（73 条），
运行时还会继续学，学到的写进 `config/subprof_cache.json`。

## 更新方法

游戏出新干员 / 新关卡后，**双击 `E:\project\MAA-PRTS-版本更新\更新资源数据.bat`** 即可
（自动找 Python、备份旧包、跑下面的命令并检查条数）。
完整步骤、检查项和常见问题见 `MAA-PRTS-版本更新\游戏资源包更新.md`。

手动跑：

```bat
python resources\make_pack.py --excel "D:\ArknightsGameResource\gamedata\excel"
```

可选参数：

| 参数 | 说明 |
|---|---|
| `--out` | 输出路径，默认就是 `resources/arknights.json` |
| `--subprof` | 分支名缓存，默认自动找 `config/subprof_cache.json`（旧版 `config/skland/…` 与 `skland/…` 兜底） |

跑完会打印各表条数和体积，并逐项和旧包对比（`+3` / `持平` / `-2 <- 变少`）。
条数只该增不减，有变少时退出码为 `2`，多半是资源库没 `git pull`。

`subProfessions`（分支中文名）不是游戏表里的字段，是运行时从森空岛学的。
生成时会**先继承旧包里已有的条目，再用运行时缓存覆盖**，所以缓存文件不存在也不会
把已有的 73 条清零。

当前基准（2026-11-07）：`chars 469`（可获取 440）/ `stages 3647`（其中剧情关 208）/
`skills 1846` / `equips 924` / `zones 492` / `activities 325` / `subProfessions 73`，体积 0.57 MB。

把新的 `arknights.json` 提交，下次启动缺卡统计就会用新数据。

**只换资源、不重装程序**：程序优先读项目目录下的 `resources/arknights.json`，
把新包直接覆盖进去（或让用户下个新包丢到 `resources/`）即可；
启动时 `version.py` 发现 `version`/`fingerprint` 变了，会在界面顶部提示
「游戏资源已更新到 xxxx，新增活动 时间啊，向前！ TL …」。
快捷栏顺序由后端定：进行中的活动排最前，其次按开始时间倒序；NEW 只是角标，不参与重排。

`--excel` 指向 `ArknightsGameResource` 的 `gamedata/excel` 目录，该仓库可以用
`git clone https://github.com/Kengxxiao/ArknightsGameResource` 获取，只用来生成包，运行时不需要它。

## 注意

- 资源包里只有干员名、关卡名这类**基础名词**，不含图片、立绘、剧情文本。
- 关卡表里排除了没有 `code` 的内部关卡（剿灭、教程占位等），`stages` 3647 条足够覆盖
  主线、活动、悖论模拟、资源关。
- `stages` 里同时保留了 `TR-` 系列与塔模式重名关，解析时在 `resources.resolve()` 里排除。
- `story: true` 是剧情关（`stage_table` 的 `isStoryOnly`，当前 208 条）：只有剧情没有战斗，
  作业站永远不会有作业，`resources._stages()` 会把它们整体剔掉，不参与搜索与活动展开。
  旧包没有这个字段时靠代号/id 兜底（`xx-ST-n` / `10-1` / `EG-3` 三种形态），见 `_is_story()`。
