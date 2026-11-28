# 在 MAA-PRTS 中使用 ChatGPT 套餐

ChatGPT 接入使用 OpenAI 为本地开源应用提供的[动态客户端注册和授权](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)，模型请求发往 [Responses API](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)。

## 第一次使用

1. 解压整个 Windows x64 构建包，保留 `MAA-PRTS.exe` 和旁边的 `_internal` 文件夹，再启动程序。已有配置可以在关闭旧版后将其同级 `config` 文件夹复制过来。Windows 加密的凭据只能由同一台电脑上的同一 Windows 用户解开。
2. 打开「设置 → 助手设置」，把连接方式切换为「ChatGPT 套餐」，点击 **Continue with ChatGPT**。
3. 在系统浏览器中登录，选择符合资格的账号和工作区，并允许此应用使用 ChatGPT 套餐额度。
4. 返回程序，选择 OpenAI 为此连接提供的模型。进入「助手」发送一条消息，例如「列出当前活动关卡」。
5. 在「管理用量」查看共享额度和应用限额。只想用已有套餐额度时，在 ChatGPT 用量设置里关闭该应用使用额外 credits 的权限（如果设置里有此选项）。

原有 OpenAI 兼容 API Key 方式仍可在同一页面选择。套餐请求失败时助手会显示原因，由用户选择何时重试或切换方式。

## 范围和限制

- 符合资格的 Plus 账号可以授权使用现有套餐额度。模型可用性和限额由 OpenAI 按账号、工作区及其政策决定。Plus 的相关额度与同样使用套餐的工具共享。常规 API Key 调用遵循相应 API 服务的计费方式。参见 [OpenAI 计划用量说明](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)。
- 助手向 OpenAI 发送当前对话上下文和相关游戏工具结果，例如关卡、账号别名或作业摘要。它通过官方授权获得套餐调用权限，使用新的应用内会话，不导入 ChatGPT 历史或记忆。对话在切换 ChatGPT 连接或模型时重新开始。
- 令牌在本机通过 Windows DPAPI 加密存入 `config/chatgpt.dpapi`。退出登录会清除该连接的本机令牌并尝试撤销远端会话。也可在 ChatGPT 设置断开应用连接。旧版 DeepSeek 等供应商设置和密钥可继续使用。
- 助手使用上游现有的游戏工具白名单和执行确认设置。套餐请求由助手对话、用户确认或手动工具测试触发。原项目定时任务无需调用模型。
- 本功能使用 OpenAI 的[预览接口](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)，未来可能调整。首次真实授权和可用模型以本机实际结果为准。

## 跟进上游

本地改动集中在 `backend/core/chatgpt_auth.py`、`backend/core/chatgpt_responses.py` 和 `frontend/chatgpt-settings.js`。主要合并点是助手调用入口、助手配置页面和打包依赖。日常关卡资源仍可用原来的更新路径。

在改版仓库内，从补丁分支同步上游源码并重新打包。若上游也修改了上述入口，先解决冲突，再运行两个测试。

```powershell
git switch chatgpt-sign-in
git fetch origin
git rebase origin/main
.\.venv\Scripts\python.exe tools/smoketest/test_chatgpt.py
.\.venv\Scripts\python.exe tools/smoketest/smoke_test.py
.\build.bat
```

也可把补丁通过 `git am --3way` 应用到新的上游副本。若应用补丁时发生冲突，解决后用 `git am --continue`。
本地构建默认为 `dev`，程序不会提示发行版更新。上游发行包包含此功能之前，
请通过同步源码并重新构建来更新改版。安装上游发行包会替换程序文件。
更换构建前关闭正在运行的程序，并保留原配置目录。

打包流程会运行新生成的 EXE，以临时 RSA 密钥验证登录使用的 JWKS 验签逻辑，
并检查错误密钥签名会被拒绝。此检查无需登录或联网，通过后才会生成发布目录。
