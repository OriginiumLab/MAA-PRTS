# pack-data

MAA-PRTS 的游戏资源数据分支，由 `.github/workflows/pack.yml` 自动生成，
**请勿手工编辑**。

| 文件 | 说明 |
| --- | --- |
| `resources/arknights.json` | 干员 / 技能 / 模组 / 关卡 / 区域 / 活动 |
| `resources/paradox.json` | 悖论模拟清单与解锁条件 |
| `resources/pack-manifest.json` | 版本、指纹、sha256、下载地址（客户端读它） |

只保留最新一份快照，每次同步都是单提交强制覆盖，不累积历史。
原始出处：`yuanyan3060/ArknightsGameResource`，源提交记在 manifest 的 `source.commit`。
