# P6: 前端内联 JS 原生 ES 模块化拆分

> 历史记录：文中旧评估/实施方案的路径和行号保留用于追溯，原文件可在 Git 提交 `f2fc5bc` 的 `docs/` 中查看；当前约束见 [架构约定](../架构约定.md)。

> 对应方案与计划：`docs/src分层与轻量化评估.md` §7、`docs/refactor/06-P5-清理收尾与架构守卫回归.md` §5 以及 P6 实施计划。

---

## 1. 重构背景与目标

此前 `public/index.html` 在单一文件中包含了长达 1,050+ 行的内联 `<script>` 脚本，涵盖了：
1. 日期计算与文本格式化；
2. 收藏（picks）、排除（exclusions）、冷冻（snoozed）状态变换与 LocalStorage 读写；
3. 目录卡片（推荐区、候选区、收藏区）与待复核（pending_review）折叠盒 DOM 渲染；
4. 弹窗控制器（同步配置弹窗双 Tab 切换、剪贴板复制、JSON 文件下载、GitHub 直跳编辑页；冷冻条目弹窗及恢复操作）；
5. 定向技能查找报告（find-report）多状态提示条、评估准则拆解、证据引文与备选差距渲染；
6. 顶层数据拉取与事件委托。

这种混合不仅导致 HTML 膨胀，而且状态逻辑无法单独进行自动化单元测试。

### 核心目标与红线
- **0 构建链、0 打包器、0 npm 生产依赖**：严格保持静态站点的超轻量特性，使用浏览器原生 `<script type="module" src="js/app.js"></script>`，与 GitHub Pages 及本地 `scripts/preview.ps1`（基于 `python -m http.server`）100% 兼容。
- **职责清晰、单一职责**：将单体内联代码拆分为 6 个原生 ES 模块。
- **纯状态可独立测试**：提取出纯函数状态机，通过 Node.js 内置原生测试框架（`node --test`）进行 100% 隔离验证。
- **无破坏性改动**：保持全部 CSS 选择器、DOM 结构与属性名称不变，293 个 Python 自动化测试 100% 持续通过。

---

## 2. 模块拆分与职责划分

所有模块均置于 `public/js/` 目录下：

```
public/
├── index.html                 # 瘦身为 159 行，只包含语义化 HTML，通过 <script type="module" src="js/app.js"> 加载
├── styles.css
└── js/
    ├── utils.js               # 工具模块：Shanghai 日期、冷冻期计算、XSS 转义、标签映射
    ├── catalog-state.js       # 纯状态机：收藏/屏蔽/冷冻状态管理、LocalStorage 存取、JSON 生成
    ├── catalog-view.js        # 视图渲染：检索筛选匹配、复核折叠盒、目录卡片列表、冷冻条目行
    ├── find-view.js           # 定向查找视图：概况卡片、评判准则、代码证据引文、备选差距、停止状态条
    ├── modals.js              # 弹窗交互：未同步浮条、同步配置弹窗、冷冻条目查看与恢复弹窗
    └── app.js                 # 主控制器：DOM 节点管理、数据加载引导、事件委托与协同更新
```

### 各模块详细职责

1. **`public/js/utils.js`**：
   - `SKILL_TYPE_LABELS`：技能形态枚举标签字典。
   - `shanghaiTodayStr()`：获取 Asia/Shanghai 时区的 `YYYY-MM-DD` 当日日期。
   - `computeExpiresAt(snoozedAtStr, days)`：计算冷冻到期日（默认为 150 天后）。
   - `isSnoozeActive(item, today)`：判断条目是否正处于活跃冷冻期内（`snoozed_at <= today < expires_at`，到期当天自动恢复显示）。
   - `escapeHtml(s)`：防 XSS 安全转义。
   - `text(val)`、`day(val)`：字符串规范化工具。

2. **`public/js/catalog-state.js`（纯逻辑状态机）**：
   - 完全不依赖 DOM，所有逻辑均为纯函数或接收状态引用的无害变换。
   - 互斥状态约束：
     - 收藏技能时，自动移除排除（黑名单）与冷冻状态。
     - 屏蔽技能时，自动移除收藏与冷冻状态。
     - 冷冻技能时，自动移除收藏与排除状态。
   - `partitionEntries()`：根据基线分类与实时干预状态，纯函数计算划分为 `activeRecommended`、`activeCandidates`、`activeManual`。
   - `generateOverridesJson()` 与 `generateSnoozedJson()`：生成符合仓库格式规范的标准 JSON。
   - `saveStorage()` / `loadStorage()` / `clearStorage()`：支持传入 mockStorage 实现脱离浏览器的隔离测试。

3. **`public/js/catalog-view.js`**：
   - `matches(entry, queryState)`：支持关键词、主要分类与来源类型的多维组合筛选。
   - `reviewBlock(entry)`：格式化渲染上游内容变更后原评估版本的比对详情。
   - `renderCatalogList()`：动态构建目录卡片，为推荐区、候选区、收藏区分别适配专属操作按钮。
   - `renderSnoozedList()`：渲染冷冻弹窗中的条目列表，显示冷冻剩余天数与“恢复显示”按钮。

4. **`public/js/find-view.js`**：
   - 独立承担定向技能查找报告的富文本投影渲染。
   - 完整支持所有停机状态提示条：`usage_unknown`（未知用量熔断）、`token_limit`（Token 预算耗尽）、`evaluation_limit`（评估上限到达）、`interrupted`（用户主动中断保留局部结果）、`model_failures`（连续模型故障）与异常中止。
   - 适配扁平/嵌套双模式候选与评估数据契约（`item.candidate || item`, `item.evaluation || item`）。
   - 渲染逐项代码证据引文（定位行号与双引号原文）以及备选差距分析。

5. **`public/js/modals.js`**：
   - `updateSyncBar()`：计算待同步到仓库的收藏、屏蔽、冷冻总项数并更新顶部提示条。
   - `initSyncModal()`：驱动同步弹窗在 `overrides.json` 与 `snoozed.json` 双 Tab 之间切换，提供一键复制、本地文件下载和直跳 GitHub 在线编辑页功能。
   - `initSnoozedModal()`：管理冷冻条目弹窗开启、关闭与条目取消冷冻事件。

6. **`public/js/app.js`（主入口）**：
   - 导入以上各模块。
   - 统筹数据拉取（`fetch("data/catalog.json")` 与 `fetch("data/find-report.json")`）。
   - 统一委托 DOM 事件：搜索框 input、分类/来源 select、主导航 Tab 切换、卡片收藏/屏蔽/暂不看按钮等。

---

## 3. 测试与验证策略

### 3.1 前端原生单元测试（Node.js Built-in Runner）

新建 `tests/frontend/catalog-state.test.mjs`，包含 7 个独立测试套件：
- `utils: computeExpiresAt & isSnoozeActive`：验证 150 天周期计算与到期当天自动恢复。
- `catalog-state: togglePick adds and removes pick`：验证点击收藏与取消收藏。
- `catalog-state: blockSkill adds exclusion and clears pick and snooze`：验证屏蔽及互斥清理。
- `catalog-state: snoozeSkill adds snooze and clears pick and exclusion`：验证冷冻及互斥清理。
- `catalog-state: partitionEntries partitions entries correctly`：验证条目纯函数分区计算。
- `catalog-state: generateOverridesJson and generateSnoozedJson`：验证导出的 JSON 结构合规性。
- `catalog-state: mock storage save, load, and clear`：验证 LocalStorage 读写与清空。

运行命令：
```powershell
node --test tests/frontend/catalog-state.test.mjs
```
**结果：7/7 单元测试全部通过（耗时 < 100ms）。**

### 3.2 Python 集成测试适配

- `tests/test_public.py`：`IndexPageTest.setUpClass` 同步读取 `public/index.html` 及 `public/js/*.js` 内容，验证 `fetch("data/catalog.json")` 等发布断言。
- `tests/test_finder_projection.py`：`TestT13FrontendFindViewRendering.setUpClass` 联合检索 HTML 与 JS 模块，验证全部 5 种停机状态条与卡片数据模型契约断言。

运行命令：
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
**结果：293/293 个 Python 单元与集成测试持续 100% 通过。**

### 3.3 离线维护与静态预览命令验证

- `tools/run_local.py --check`：预检通过。
- `tools/run_local.py --sync-config`：主索引与页面数据同步成功。
- `node -c public/js/*.js`：ES 语法静态检查 0 错误。

---

## 4. 总结与后续

至此，原 `public/index.html` 的 1,050+ 行单体内联脚本已完全转换为 6 个符合现代浏览器规范的原生 ES 模块。前端既获得了模块化与单元测试能力，又没有增加任何 npm 打包或构建依赖，完美兼顾了工程可维护性与极致的轻量化。
