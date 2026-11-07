# tools/setup —— 一键构建的环境引导

## 用途

`prepare-python.bat` 是 `启动.bat` / `打包.bat` 共享的 Python 环境引导脚本，
不要单独运行。它做三件事：

1. 选解释器：优先项目 `.venv`，其次 `py -3` / `python` / `python3`，
   再扫官方安装器和 Python Install Manager 的常见安装位置，要求 3.10+；
2. 没有合适解释器就用系统 Python 创建 `.venv`；顺带确保 pip 可用；
3. 成功后向调用方回传 `PYTHON`（解释器路径）、`PYTHON_IS_VENV`、`PIP_USER_ARG`
   （直接用系统 Python 时为 `--user`，避免无管理员权限写不进去），失败时打印
   具体原因并以退出码 1 结束。

## 约定

- 脚本含 UTF-8 中文，开头自带 `chcp 65001`，调用方不必先切代码页。
- 脚本内不用 `setlocal`，否则上面的变量传不回调用方。
