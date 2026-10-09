# 常用命令

> **环境要求**：Windows PowerShell | Python 解释器：`.\.venv\Scripts\python.exe`

---

## 1. 本地运行与预览 (Run & Preview)

```powershell
# 一键本地静态网页预览（自动选择空闲端口并打开浏览器）
.\scripts\preview.ps1

# 原生 Python 静态服务预览
.\.venv\Scripts\python.exe -m http.server 8000 --bind 127.0.0.1 --directory public

# 本地运行环境与配置预检（不联网、不调用模型、不写数据）
.\.venv\Scripts\python.exe tools/run_local.py --check

# 启动本地采集与评估任务
.\.venv\Scripts\python.exe tools/run_local.py
# 或使用 PowerShell 包装脚本：
.\scripts\run-local.ps1

# 离线同步配置与页面索引（无需模型凭据与网络）
.\.venv\Scripts\python.exe tools/run_local.py --sync-config

# 离线恢复已完成评估与页面（不调用模型）
.\.venv\Scripts\python.exe tools/run_local.py --recover-catalog
```

---

## 2. 模型管理与切换 (Model Management)

> ⚠️ 模型切换仅由开发者在终端手动执行。

```powershell
# 查看当前生效模型
.\.venv\Scripts\python.exe tools/switch_model.py --show

# 列出全部可用模型配置
.\.venv\Scripts\python.exe tools/switch_model.py --list

# 切换到指定模型或别名
.\.venv\Scripts\python.exe tools/switch_model.py <模型标识或别名>
# 示例：.\.venv\Scripts\python.exe tools/switch_model.py qwen3.7-flash-2026-07-15

# 测试指定模型连通性
.\.venv\Scripts\python.exe tools/test_model_call.py --model <模型名称>
```

---

## 3. 定向技能查找 (Find Skills)

```powershell
# 交互式查找（或使用默认配置）
.\.venv\Scripts\python.exe tools/find_skill.py

# 命令行指定需求与短名单上限
.\.venv\Scripts\python.exe tools/find_skill.py "需求关键词" --limit 5

# 恢复上次中断的查找任务
.\.venv\Scripts\python.exe tools/find_skill.py --resume

# 重建指定历史运行报告并更新前端快照
.\.venv\Scripts\python.exe tools/find_skill.py --rebuild-report "data/local/find-skills/<运行目录>" --publish-snapshot
```

---

## 4. 候选池与治理工具 (Governance & Pool)

```powershell
# 候选池离线比对（dry-run 预检）
.\.venv\Scripts\python.exe tools/manage_pool.py reconcile --dry-run

# 候选池离线比对并应用修正
.\.venv\Scripts\python.exe tools/manage_pool.py reconcile --apply

# 查看当前已收录条目列表
.\.venv\Scripts\python.exe tools/manage_owned.py --list

# 离线标签与规则清洗（不调用 AI）
.\.venv\Scripts\python.exe tools/filter_existing_catalog.py report
```
