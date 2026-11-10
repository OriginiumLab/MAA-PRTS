# frontend —— 界面层

## 用途

纯静态的界面代码，由 `backend/main.py` 起的本地 HTTP 服务托管（`http://127.0.0.1:<port>/`），
在 pywebview 的 WebView2 窗口里渲染。没有构建步骤、没有 npm、没有框架，改完刷新即可生效。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | 页面骨架：左侧账号栏、中间主区（账号信息 + 模块菜单 + 模块面板）、右侧作业集 |
| `style.css` | 全部样式。深色玻璃拟态，颜色/圆角/字体集中在 `:root` 变量里 |
| `app.js` | 全部交互逻辑，按功能分段注释 |

## 实现要点

**状态集中在一个对象**

```js
const S = {
  settings, accounts, current, detail,   // 当前账号与其完整配置
  maa, activities,                        // MAA 状态、快捷入口数据
  basket, result, searching,             // 作业集、最近一次搜索结果
  paths, pack, setup, gamedata, appInfo, // 路径、资源包信息、首启标记、资源版本、原生桥信息
};
```

没有响应式框架，所有 UI 都是「改 `S` → 调 `renderXxx()` 重画对应区块」。
区块之间互不依赖，错开重画不会互相清空。

**与后端的唯一通道**

```js
api('/api/xxx', body)   // 统一包成 {ok, data|error}
```

返回 `ok:false` 时抛错，调用处 `catch` 后 toast 提示。长任务（同步账号、写入 MAA）
走「创建任务 → 轮询 `/api/task?id=&since=`」，日志逐行推进弹窗的 `.log-box`。

**与桌面窗口的桥**

`window.pywebview.api` 提供三个原生能力（浏览器里打开时自动降级）：

| 方法 | 用途 |
|---|---|
| `choose_folder(title, initial)` | 设置页/引导页的「浏览」按钮，调系统文件夹选择框 |
| `open_folder(which)` | 打开 `accounts` / `config` / `root` 目录 |
| `app_info()` | 版本、是否打包、数据目录，用于设置页底部信息 |

`boot()` 里用 `waitPywebview()` 轮询等桥就绪，最多 2 秒；拿不到就当普通网页跑。

**主要渲染函数**

| 函数 | 负责区块 |
|---|---|
| `renderAccounts()` | 左侧账号列表：只渲染侧栏顶部标签选中的那一组（明日方舟 / 终末地），头像配色 + 「终末地」小标签二次区分；拖拽排序只在这一组内生效，另一组的顺序原样保留 |
| `setSideGame()` | 侧栏顶部「明日方舟 / 终末地」分组标签（各带账号数）。切换时若当前账号不属于这一组，自动选中这组第一个；某组为空也保留标签，点了给空态说明 |
| `renderHero()` | 顶部账号信息卡 + 统计块（「缺卡」是可点按钮）；明日方舟账号显示「从 MAA 更新干员信息」，终末地只显示等级/区服/抽卡数 |
| `showMissing()` | 缺卡清单弹窗，按星级分组，只显示名字 |
| `importFromMaa()` | 「从 MAA 更新干员信息」（已有账号的手动更新入口）：确认弹窗（目录 + OperBoxData.json）→ `POST /api/account/import-maa` → 摘要弹窗并刷新当前账号；本地识别不含技能/专精/模组会明示 |
| `renderUpdate()` | 资源包更新提示条（顶部的绿色条） |
| `renderQuick()` | 快捷活动胶囊：活动名 + 关卡代号字母 + NEW 角标 |
| `toggleBasket()` | 窄屏抽屉开关（≤1180px），控制 `.basket.show`、遮罩与浮动按钮 |
| `renderResult()` | 单关卡卡片 / 活动关卡列表两套版式 |
| `renderBasket()` | 右侧作业集（含拖动排序） |
| `openAccountWizard(mode)` | 账号向导共用组件（首次引导 `onboard` / 添加账号 `add`）：第 1 页选 MAA 根目录，第 2 页二选一——森空岛 token（一行一个批量导入，成功后自动从 MAA 导入练度）或「直接从选定的 MAA 导入」（不用 token，调 `POST /api/account/create-from-maa` 建本地账号） |
| `openOnboarding()` / `openAddAccount()` | 上面的薄包装（引导多一个「稍后设置」，成功后把目录存为全局默认） |
| `openSettings()` | 设置弹窗 |
| `pollTask()` | 任务日志轮询（同步账号、写入 MAA、同步抽卡共用） |
| `setModule(mod)` | 模块菜单切换（作业获取 / 抽卡统计），同时决定右栏作业集要不要收起来 |
| `applyModuleAvailability()` | 按当前账号的游戏决定 tab 可见性：终末地账号隐藏「作业获取」并自动切到抽卡统计；从终末地切回明日方舟时自动回作业获取（手动点过的模块不会被抢） |
| `renderGacha()` / `paintGacha()` | 抽卡统计面板：5 张汇总卡、星级分布条、六星记录、按卡池、最近记录表 |
| `gachaSync()` | 「同步最新记录」：`POST /api/gacha/sync` → 轮询任务日志 → 重画面板 |

**模块菜单**

账号卡下面那行是模块菜单，介于「账号信息」和「搜索框」之间：

| tab | 状态 |
|---|---|
| 作业获取 | 原有整块（搜索 + 结果），终末地账号不显示 |
| 抽卡统计 | 读 `/api/gacha` 的本地缓存渲染，点「同步最新记录」才联网 |
| 配置修改 / 森空岛签到 | 占位（disabled + 「待做」角标），后续实现 |

切换只改 `hidden` 与 `.app.no-basket`，不重建 DOM——搜索词与结果在切回来时
还在。抽卡统计不需要右栏作业集，`.app.no-basket` 把第三列收掉。
另外支持 `?mod=gacha` 直接打开抽卡统计（做快捷方式用，与 `?q=TL-10` 同类）。

**快捷栏**

快捷栏只放活动，不再有 TR 之类的系列入口（要搜系列直接在搜索框输 `TR-`）。
每个胶囊是「活动名 + 关卡代号字母」，例如 `时间啊，向前！ TL`。
顺序直接用后端给的（进行中优先），前端不再按编号重排，否则一堆 NEW 会把当前活动挤出可视区：
字母由后端从关卡代号（TL-1…TL-10）里取，用户照着输 `TL-10` 就能搜。

排序规则：`S.gamedata.pending.newActs` 里的活动永远排最前并带 `NEW` 角标，
其余按 act 编号从新到旧，只显示前 12 个（多了横向滚动）。

**资源更新提示**

`/api/bootstrap` 带回 `gamedata`（当前资源包版本 + 是否有待提示的更新）。
有 `pending` 时页面顶部出现提示条，列出新增干员/关卡数和新增活动；
点「知道了」调 `/api/gamedata/ack` 清掉，不点就每次启动都提醒。

**作业集拖动排序**

`renderBasket()` 渲染时给每项加 `draggable="true"`，`bindBasketDrag()` 做三件事：

1. `dragstart` 记下被拖的元素，加 `.dragging` 半透明样式；
2. `dragover` 时按鼠标在目标项的上半/下半，把被拖元素 `insertBefore` 到目标前/后
   —— 直接挪 DOM，不重排数组，拖动过程才不会中断；
3. `dragend` 调 `syncBasketOrder()`，按 DOM 顺序重排 `S.basket` 并重画（刷新序号）。

`S.basket` 的顺序就是 `/api/maa/apply` 里 `items` 的顺序，也就是 MAA 多作业列表里的执行顺序。

**首次引导的判定**

`/api/bootstrap` 返回 `setup.needsToken`（没有账号）与 `setup.needsMaa`（找不到 MAA.exe）。
`boot()` 在「没有账号」或「没填 MAA 目录」时弹引导；用户点「稍后设置」后本次会话不再弹。
首次引导与「添加账号」是同一个向导组件（`openAccountWizard`），只差标题文案与
「稍后设置」按钮：第 1 页选 MAA 根目录，第 2 页二选一（森空岛 token / 直接从选定的
MAA 导入建号），两种入口行为完全一致。

向导共用同一段 token 获取说明（`TOKEN_GUIDE` 常量）：
引导用户自己去森空岛社区登录后从 `zonai.skland.com` 的请求头里取 token，
**不放任何示例 token**（示例容易被当成可直接用的值抄走）。

## 改样式

颜色、圆角、字体都在 `style.css` 顶部的 `:root`：

```css
--mint: #5eead4;   /* 主色（按钮、强调） */
--amber: #f5b955;  /* 警告 / 缺卡 */
--star: #ffcf6b;   /* 评分星 */
--panel: rgba(20, 24, 34, 0.72);
```

改完直接重启程序即可，静态资源是 `Cache-Control: no-store`，不会读到旧缓存。
