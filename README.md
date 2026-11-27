# pack-data

MAA-PRTS 的公共资源数据分支，**请勿手工编辑**。

| 目录 / 文件 | 说明 | 谁在维护 |
| --- | --- | --- |
| `resources/arknights.json` | 干员 / 技能 / 模组 / 关卡 / 区域 / 活动 | `pack.yml`（每天 12:00 / 16:00） |
| `resources/paradox.json` | 悖论模拟清单与解锁条件 | `pack.yml` |
| `resources/pack-manifest.json` | 版本、指纹、sha256、下载地址（客户端读它） | `pack.yml` |
| `resources/avatars/` | 干员头像 96px WebP + `avatar-index.json` | `avatars.yml`（每周一） |
| `themes/` | 第三方主题包 + `manifest.json` | 人工发布 |

每次同步都是单提交强制覆盖，不累积历史；但只会覆盖自己那一块，
其余目录原样继承。原始出处：`yuanyan3060/ArknightsGameResource`，
源提交记在 manifest 的 `source.commit`。
