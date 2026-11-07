# build —— 构建产物

## 用途

仓库里唯一的构建输出目录，由 `打包.bat` / `python tools/build.py` 生成：

```
build/
└── MAA-PRTS/           ★ 发布包本体：Release 压缩包解压后的内容
    ├── MAA-PRTS.exe        主程序
    └── _internal/          运行时（Python 解释器、pywebview、前端页面、资源包）
```

构建过程中的中间产物（spec、分析缓存、全新包 `work/dist/`）放在 `build/work/`，
打包成功后自动删除；只有失败时才保留供排查。

`MAA-PRTS/` 约 30 MB，不需要装 Python，也不需要 WebView2 之外的其他运行时
（Win10/11 自带）。发版时把 `build/MAA-PRTS` 压缩成 `MAA-PRTS-v<版本>-win64.zip`
传 Release 即可，包内容与用户下载解压得到的一致。

## 生成

双击根目录 `打包.bat`（或 `python tools/build.py`）。每次都全量构建出新包，再以
**同名文件覆盖**的方式合并进 `build/MAA-PRTS/`：目录里已有的其它内容（如原地
运行产生的 `config/`、`debug/`）一律不动；新包里已不存在的旧文件也不清理
（打包结束会提示残留数量，介意就手动删掉整个目录重新打包）。

## 数据放在哪

**无论在仓库里直接运行 exe，还是把 `build/MAA-PRTS` 整个拷到别处**：数据一律写在
exe 同级的 `config/`（账号 json、token 密文、设置都在这一层），日志在 exe 同级的
`debug/`，即插即用。程序不向上找项目根，也不会往系统目录或用户目录写东西。

注意：exe 一旦运行过，`build/MAA-PRTS/` 里就会多出 `config/`、`debug/`（含加密
token）。重新打包不会动它们，**压缩发版前请先删掉这两个目录**再压缩。

## 注意

- 本目录内容全部由脚本生成（整个目录不入库），不要手改；下次打包会被覆盖。
- `.gitignore` 忽略 `build/*`；产物不进仓库，发版走 Release 附件。
- 改了 `frontend/` 或 `resources/` 之后必须重新打包，exe 用的是包内的副本。
