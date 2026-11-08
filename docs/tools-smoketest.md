# tools/smoketest —— 冒烟测试

## 用法

```bat
.venv\Scripts\python.exe tools\smoketest\smoke_test.py
```

跑完打印每项 PASS/FAIL，全部通过退出码 0。不碰真实用户数据：
B 组把 `core.settings` 的数据路径指到临时目录，C 组整个项目拷到临时目录再启动。

## 检查项

| 组 | 内容 |
|---|---|
| A | 三个 `.bat` 是 UTF-8 无 BOM + CRLF（错编码会让 `call :标签` 错位）；全部 `.py` 可编译 |
| B | DPAPI 加解密往返、密文篡改必报错；token 密文随账号文件 `skd` 段（跨文件去重、备注保留、不进 API 视图、坏条目逐个跳过点名警告）；旧 `tokens.txt` 三种写法自动迁移；旧布局 `config/skland` 压平迁移（同名跳过、搬空才删、幂等）；MAA 干员识别 JSON（新 JObject / 旧裸数组）解析成练度结构；作业筛选（v1 默认练度、MAA 导入只比等级、剧情关不进搜索）；主线难度变体（简单/标准/磨难/险地、`#s` 独立成行、下一关推进）；资源包幂等（`version` 取游戏版本、指纹只看内容）与换包校验（sha256 / 条数不许变少）；程序自更新（`2.10 > 2.9`、预发布小于同号正式、脏 tag 不触发；安装包 sha256/体积/缺 exe 全拒、zip-slip 拦截；安装脚本只碰 `MAA-PRTS.exe` 与 `_internal`，绝不出现 `config/`、`debug/`、`resources/`） |
| C | 临时副本里 `--no-window` 拉起服务，探活 `/api/ping`、`/api/bootstrap`、`/api/gamedata`；bootstrap 报告的账号数据路径在 `config/` 本层且 `config/skland` 不再被创建；日志落在 `debug/app.log`；结束后副本根目录除 `config/`、`debug/` 外零新增 |

## 什么时候跑

- 改了 `vault.py` / `accounts.py` / `settings.py` / `main.py` 之后
- 改了打包、启动脚本或 `.gitignore` 之后（A 组和 C 组的目录零污染检查）
- 发版前过一遍

依赖只用到程序自身的运行环境（B/C 不会联网调森空岛接口），用 `.venv` 的
Python 跑即可；C 组会真实调用 Windows DPAPI，换电脑/换用户跑 B 组是
「加密绑定本机」的活体验证。
