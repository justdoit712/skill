# 常用命令

> **环境要求**：Windows PowerShell | Python 解释器：`.\.venv\Scripts\python.exe`

---

## 1. 本地运行与预览 (Run & Preview)

```powershell
# 一键本地静态网页预览（自动选择空闲端口并打开浏览器）
.\scripts\preview.ps1

# 启动本地采集与评估任务
.\.venv\Scripts\python.exe tools/run_local.py
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

