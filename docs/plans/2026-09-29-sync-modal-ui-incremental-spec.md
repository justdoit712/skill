# 前端同步弹窗视觉修复与增量展示实施方案

> **版本**：v1.1.0  
> **日期**：2026-09-29  
> **状态**：待实施；本次审阅只修订文档，不代表界面已修改。  
> **范围**：同步弹窗的代码预览、待同步差异展示、复制/下载交互，以及必要的状态一致性与回归测试。

## 1. 现状与问题定位

### 1.1 代码框样式

当前 `public/index.html` 使用：

```html
<pre><code id="json-preview" class="json-preview"></code></pre>
```

`public/styles.css` 将背景、内边距、高度和滚动样式赋给 `.json-preview`，但此类挂在默认行内的 `code` 上，没有显式设为块级。该结构会使多行背景和盒模型表现异常，是需要修正的具体代码问题。

原文所述“横切打码”是待浏览器复现的视觉现象，本次未获得截图并复现渲染，不能把全部视觉表现都认定为已验证的单一根因。实施时通过实际弹窗检查字体、行高、背景和滚动，而不是仅凭 CSS 文本判定修复成功。

### 1.2 默认全量预览与数据语义

`modals.js` 的收藏/屏蔽、冷冻页签分别调用 `generateOverridesJson`、`generateSnoozedJson`，总是展示基线与本地修改合并后的完整快照，难以核对少量变更。

现有 `staged*`、`removed*` 会经 LocalStorage 保存并在页面加载时恢复。因此展示对象应称为**待同步变更**，定义为“相对于当前页面加载基线的本地净变化”，不能称为“刚才的点击”“本次会话”或完整操作历史。

此外，`staged*` 不一定都是新增：同一 ID 可能在基线已存在，只是原因、日期等字段被修改；恢复旧缓存后也可能已经与新基线一致。直接 `Object.values(staged*)` 不等于正确增量。

### 1.3 现有第三个页签必须保留

同步弹窗已有 `owned`（已收录）页签，使用 `generateOwnedPatch` 输出带前置条件的 `owned-patch.json`，由本地工具合并。它不同于收藏/屏蔽、冷冻两个配置快照，不能套用新的通用增量格式或跳转 GitHub 覆盖逻辑。

## 2. 目标与明确边界

1. 修复代码框盒模型，长 JSON 只在代码区内滚动，不撑破弹窗。
2. 收藏/屏蔽与冷冻页签默认展示待同步净变化，可切换至完整配置快照。
3. 新增的差异 JSON **仅用于核对**，不是配置文件，也不是可被现有工具应用的补丁。本次不实现新补丁导入协议。
4. 复制与下载的内容、按钮文案、文件名和当前预览保持一致，不暗中替换为另一份内容。
5. 保留已收录变更包协议、LocalStorage 格式和现有清空确认行为。
6. 不自动提交 GitHub、不把复制或下载成功视为已经同步、不自动清空暂存修改。

## 3. 视觉与可访问性

### 3.1 HTML 结构

保留 `json-preview` ID，改为块级 `pre`；JSON 始终用 `textContent` 写入，不把技能 ID、reason 等数据插入 `innerHTML`。

```html
<div class="json-preview-container">
  <div class="json-preview-toolbar">
    <span id="json-view-mode-label">待同步变更（仅供核对）</span>
    <div id="json-view-toggles" role="group" aria-label="预览内容">
      <button type="button" id="btn-view-incremental"
              aria-pressed="true" aria-controls="json-preview">待同步变更</button>
      <button type="button" id="btn-view-full"
              aria-pressed="false" aria-controls="json-preview">完整配置</button>
    </div>
  </div>
  <pre id="json-preview" class="json-preview" tabindex="0"
       aria-label="待同步变更 JSON 预览"></pre>
</div>
```

这是结构示意，需与现有页签、说明、状态提示和底部动作区一起实现。视图切换是按钮组，不增加一套未实现键盘语义的 ARIA tabs。现有文件页签要补齐匹配的 tab/tabpanel 语义与键盘行为，或统一改成语义明确的按钮组，不能保留只有 `tablist` 的不完整状态。

### 3.2 CSS 要求

用 `#sync-modal` 限定新增样式，修改现有规则，避免到处叠加 `!important`。无需语法高亮库，也不承诺未实现的彩色 token 高亮。

```css
#sync-modal .json-preview-container {
  min-width: 0;
  margin-bottom: 12px;
  border: 1px solid #1e293b;
  border-radius: 8px;
  overflow: hidden;
  background: #0f172a;
}

#sync-modal .json-preview-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  color: #cbd5e1;
  background: #1e293b;
}

#sync-modal pre.json-preview {
  display: block;
  width: 100%;
  box-sizing: border-box;
  margin: 0;
  padding: 12px 14px;
  border: 0;
  border-radius: 0;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.5;
  color: #f8fafc;
  background: #0f172a;
  max-height: 240px;
  overflow: auto;
  white-space: pre;
  tab-size: 2;
}
```

补充按钮选中、悬停、禁用和 `:focus-visible` 状态，不能只靠颜色区分选中状态。窄屏工具条及底部按钮可换行，弹窗正文可滚动；代码区长行不带动页面横向溢出。同步弹窗支持 Escape 关闭、打开后聚焦、Tab 焦点限制于弹窗、关闭后还原触发点。状态提示使用 `role="status"` / `aria-live="polite"`，整块 JSON 不设为 live region。

## 4. 净变化生成与一致性

### 4.1 统一有效快照

先抽取无 DOM、无存储写入的纯函数，按现有规则计算收藏、排除、冷冻的有效集合。完整 JSON 生成器与差异生成器使用同一有效快照，禁止各自维护一套互相矛盾的合并算法。

以 `skill_id` 为键，对每个集合比较基线与有效快照：

- 只在结果中存在：`added`，保留完整条目。
- 只在基线中存在：`removed`，包含 `skill_id` 及原条目 `before`，便于核对。
- 两边存在且实际导出字段不同：`updated`，包含 `skill_id`、`before`、`after`。
- 两边一致：不展示。

比较采用对象键顺序无关的深比较，忽略用于展示的临时字段，不仅比较 ID。保持条目内部有意义的数组顺序；结果列表按 `skill_id` 稳定排序，纯函数不调用当前时间、不修改输入对象或 Set。

对新增后取消的同一条目显示零变化；基线条目取消后恢复，如果导出元数据确实变化则显示更新，不能声称净变化一定为零。若恢复时应保持旧元数据，需在相应状态转换中明确修复并测试，不能靠差异预览掩盖。

过期冷冻不因为当前日期变化就伪造“用户删除”。差异必须比较实际导出集合，不使用只返回当前生效冷冻项的 `getEffectiveSnoozedList` 作为导出基线。

### 4.2 互斥及关联修改

收藏、屏蔽、冷冻应保持互斥，跨集合转换可能同时影响两个文件。例：收藏一个基线冷冻项，需要在 `overrides.json` 新增收藏，在 `snoozed.json` 删除冷冻。

现有 `togglePick` 对基线屏蔽项调用 `removedExclusions.delete(sid)`，没有正确移除基线屏蔽记录；不能在新预览中继续把这种冲突当作合法完整配置。实施时修复相关状态转换并补充回归。恢复的旧本地缓存若存在无法确定意图的跨集合冲突，展示冲突提示并阻止导出配置，不能任意选择一方或静默丢弃数据。

关联修改需在页签计数和说明中可见，例如“此操作还需同步冷冻配置”。不承诺两个文件可以在 GitHub 在线编辑中原子保存，用户必须完成所有受影响文件的同步。

### 4.3 差异展示格式

建议新纯函数先返回结构化对象，再由序列化函数输出 JSON。原文 `generateIncrementalOverridesJson` / `generateIncrementalSnoozedJson` 可作为序列化入口，但字段语义必须改为只读差异摘要：

```json
{
  "preview_version": "1.0.0",
  "kind": "change_preview",
  "target_file": "config/governance/overrides.json",
  "notice": "仅供核对，不能覆盖配置文件，也不能直接导入",
  "summary": {
    "changed_records": 1,
    "affected_skills": 1
  },
  "changes": {
    "manual_picks": {
      "added": [
        {
          "skill_id": "author/tool:SKILL.md",
          "reason": "用户收藏",
          "added_at": "2026-09-29",
          "from": "recommended"
        }
      ],
      "updated": [],
      "removed": []
    },
    "manual_exclusions": {
      "added": [],
      "updated": [],
      "removed": []
    }
  }
}
```

冷冻预览使用 `changes.snoozed`，同样包含三类差异。保留空数组，零变化时显示“此文件没有待同步变更”，仍可查看完整快照。

`changed_records` 统计集合级变化记录，`affected_skills` 统计去重技能数。一次冷冻转收藏可能是一项技能、两条配置变更，不称为“两次点击”。顶部浮条、文件页签及弹窗摘要使用相同净差异计数；已收录继续使用自己的协议计数，并在文案中区分。

## 5. 弹窗状态与动作契约

### 5.1 页签与视图

- `currentModalTab` 保留 `overrides` / `snoozed` / `owned`。
- 每次打开弹窗重置普通配置页签为 `incremental`，优先选择有净变化的页签；延续“只有已收录变化则打开 owned”的行为，混合变化时各页签都可见。
- 同一次打开期间，普通配置页签之间保留当前视图偏好；进入 owned 时隐藏双视图按钮，返回时恢复普通配置视图。
- owned 始终显示原 `generateOwnedPatch` 的结果，不提供虚构的完整 `owned-skills.json` 导出。
- 视图切换更新标题、说明、`aria-pressed`、预览标签、下载文件名和按钮文案，清除过时的复制状态。重新打开或修改数据时重新生成快照。

### 5.2 各按钮的唯一行为

| 页签/视图 | 仅复制 | 下载文件 | 原“去 GitHub”按钮 |
| --- | --- | --- | --- |
| overrides / 待同步变更 | 复制当前差异摘要 | `overrides-changes-preview.json` | 显示“查看完整配置以同步”，点击仅切换完整视图，不复制、不打开网页 |
| snoozed / 待同步变更 | 复制当前差异摘要 | `snoozed-changes-preview.json` | 同上 |
| overrides / 完整配置 | 复制当前完整快照 | `overrides.json` | “复制完整配置并打开 GitHub 编辑页” |
| snoozed / 完整配置 | 复制当前完整快照 | `snoozed.json` | 同上 |
| owned | 复制原始变更包 | `owned-patch.json` | 保持“复制本地合并命令”，不跳转 GitHub |

这次视图转换是明确的预览步骤，不另加确认弹窗。禁止“增量模式自动智能复制全量，或者也可复制增量”的二义实现。

owned 合并命令采用项目 Windows 解释器：

```powershell
.\.venv\Scripts\python.exe tools/manage_owned.py --apply-changes owned-patch.json
```

### 5.3 剪贴板、下载与 GitHub 失败处理

- 点击动作时捕获不可变的文本、页签、视图、文件名快照；异步回调不能读取已经切换后的页面状态。
- `clipboard.writeText` 成功后才显示复制成功；不存在、拒绝或失败时提供选中文本手动复制的说明，不能吞异常后继续显示成功。
- 复制进行中禁用对应动作或用操作序号防重复；页签/视图改变后旧回调不能覆盖新视图的状态提示。
- 完整配置按钮在复制成功后尝试打开固定的相应仓库文件编辑 URL；若浏览器阻止弹窗，显示可点击的同一编辑链接。不要为规避拦截而在复制失败时也直接打开编辑页。链接使用适当的 `noopener` / `noreferrer`。
- 下载内容必须等于点击时预览文本，文件名不能将只读摘要伪装为配置。释放 Blob URL 的时机要兼容下载启动，下载提示不宣称文件已经在仓库保存。
- 仅复制、下载、打开网页、切换视图都不得更新基线、删除 LocalStorage 或标记已同步。清空仍是明确的“放弃本地修改”操作。

## 6. 完整配置的有效范围

页面通过 `public/js/app.js` 获取 `data/catalog.json`，`populateBaseline` 从中构造基线；它不是直接读取 GitHub 编辑页当前文件。现有生成器也重建部分固定顶层字段，不保证保留仓库最新的全部元数据。

因此完整视图应说明：“基于当前页面数据与本地修改生成；保存前核对仓库最新内容，其他人的更新和未包含字段不会自动合并。”有数据时间戳时展示该时间，没有则明确未知，不编造版本。

本次保留现有完整快照导出能力，不能宣称是已与最新仓库合并的文件，不能把“复制成功”写成“提交后立即生效”。GitHub 保存、流水线刷新数据、Pages 发布与浏览器刷新是后续步骤。

本次不引入 GitHub API 写入、配置补丁合并器或版本冲突自动解决功能。如果将来需要可直接应用的增量补丁，需另行定义基线版本、前置条件、幂等规则、校验器和导入工具。

## 7. 实施文件范围

| 文件 | 改造内容 |
| --- | --- |
| `public/index.html` | 块级预览、视图按钮组、说明和状态提示 |
| `public/styles.css` | 同步弹窗局部样式、窄屏与焦点样式，替换原有错误规则 |
| `public/js/app.js` | 注册新增 DOM 元素并传入 `initSyncModal`；与浮条刷新保持一致 |
| `public/js/catalog-state.js` | 抽取有效快照与净差异纯函数、统一计数、修复必要互斥转换 |
| `public/js/modals.js` | 三页签的视图状态、预览与动作矩阵、异步复制失败处理、焦点管理 |
| `public/js/owned-state.js` | 原则上保持协议不变；作为回归核对依赖 |
| `tests/frontend/catalog-state.test.mjs` | 净差异、撤销、元数据更新、持久化恢复、互斥与一致性测试 |
| 新增 `tests/frontend/modals.test.mjs` | 用 DOM/浏览器 API 替身测试页签、动作、文件名、剪贴板和失败分支 |

现有测试采用 Node 原生测试运行器；不为文档预先指定新的浏览器依赖。纯状态测试不能替代实际浏览器视觉验收。

## 8. 验收计划

### 8.1 状态逻辑

1. 大量基线中只新增一项收藏：净差异只含该项，完整快照保留原集合。
2. 删除基线收藏、取消屏蔽、解除冷冻：显示明确删除记录及原值。
3. 修改同 ID 的 reason/日期/期限：显示更新，不误标新增。
4. 新增后取消回到基线：净差异为零，浮条计数一致；基线取消后恢复按实际元数据判断。
5. 从 LocalStorage 恢复的旧变更仍显示为待同步；已经与当前基线一致的项不重复计数，不擅自清缓存。
6. 屏蔽转收藏、冷冻转收藏、收藏转冷冻的最终集合互斥，两个文件的关联修改齐全。
7. 冷冻到期不会被差异生成器自动当作用户删除。
8. 生成器输入不变；对象键顺序不影响比较，输出可重复；冲突不会导出为合法完整配置。

### 8.2 控制器与导出

1. 默认增量、切换完整、关闭再打开复位；仅 owned 变化、混合变化、零变化均正常。
2. 复制/下载内容与当前预览逐字一致，增量文件名有 `changes-preview` 标识。
3. 增量视图的同步按钮只切完整视图；完整视图复制成功后才尝试打开对应文件编辑页。
4. 覆盖剪贴板不存在、Promise 拒绝、弹窗拦截、复制中换页签、重复点击，不能出现虚假的成功提示。
5. owned 原协议及前置条件不变，下载仍为 `owned-patch.json`，合并命令正确，不出现完整配置视图。
6. 所有预览/导出动作不清空暂存；明确放弃后的浮条与弹窗刷新一致。
7. 含 `<script>`、HTML 字符、引号、中文及长 ID 的条目只作为文本显示。

### 8.3 视觉与键盘

在实际浏览器中使用空变更、单条变更、大量配置和长行数据检查：

- 代码框背景连续，字符清晰，行高和内边距正常，无切碎或重叠。
- 桌面、约 375px 窄屏及 200% 缩放下不出现页面级横向溢出，操作区可到达。
- 代码框滚动、文件页签、两种视图及 owned 页签均正常。
- 焦点清晰，键盘可完成切换、复制及关闭；弹窗打开/关闭焦点行为正确。

实施后先运行相关纯逻辑/控制器测试，再运行项目约定的前端回归：

```powershell
node --test tests/frontend/catalog-state.test.mjs tests/frontend/modals.test.mjs
npm test
```

`modals.test.mjs` 为计划新增文件，当前未创建时不能把以上首条命令当成已完成验证。记录实际测试数与浏览器验收结果，不预写“固定 8 项断言全部通过”。离线替身测试不写真实仓库、不触发真实下载或 GitHub 提交。

## 9. v1.1.0 审阅修订摘要

- 将“本次会话增量”改为相对页面基线的待同步净变化，包含新增、更新、删除与撤销。
- 将未经实现的 `incremental_patch` 改为明确只读的 `change_preview`，区分摘要和配置文件名。
- 明确同步按钮先展示完整配置，不暗中复制另一视图内容；补齐剪贴板与弹窗失败处理。
- 纳入已收录第三页签和 `app.js` DOM 绑定，保留既有变更包协议。
- 补充状态互斥问题、跨文件关联修改、统一计数及基线陈旧限制。
- 收敛 CSS 修复，补充浏览器、键盘和控制器验收，去除未经验证的视觉根因与测试数量承诺。
