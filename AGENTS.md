# 项目智能体交互与运行规范 (Agent Guidelines)

本项目为开源 Skill 评估与目录聚合系统。所有在此仓库工作的 AI 智能体（Agent）必须严格遵守以下交互与操作规则：

## 1. 命令行交互与执行边界（核心规则）

- **切换模型指令规范**：当用户提出“切换模型”或指定切换到某个模型时（例如“切换到 qwen3.7-flash-2026-07-15”），**助手必须仅给出命令行供用户在终端自行执行，严禁私自调用工具执行切换命令，严禁私自发起网络请求或执行脚本去测试模型**。其它要求（如需要由助手代为执行或测试）用户会显式说明。
- **显式授权原则**：未经用户明确要求（如“请执行”、“帮我切换”、“去测试一下”），不可代为触发对环境有副作用的命令或外部网络请求。默认交付物为精准的命令行代码块与必要的使用说明。

## 2. 模型管理与切换常用命令

- 切换到指定模型：
  ```powershell
  .\.venv\Scripts\python.exe tools/switch_model.py <模型标识或别名>
  ```
  例如切换到 `qwen3.7-flash-2026-07-15`：
  ```powershell
  .\.venv\Scripts\python.exe tools/switch_model.py qwen3.7-flash-2026-07-15
  ```
- 查看当前生效模型：
  ```powershell
  .\.venv\Scripts\python.exe tools/switch_model.py --show
  ```
- 列出全部可用模型配置：
  ```powershell
  .\.venv\Scripts\python.exe tools/switch_model.py --list
  ```

## 3. 本地运行与开发环境规范

- **运行环境**：本项目仅在 Windows PowerShell 环境下本地运行与开发，不部署亦不在 Linux 中运行。
- **Python 解释器**：优先使用项目内的虚拟环境解释器 `.\.venv\Scripts\python.exe`。
- **本地任务入口**：`.\.venv\Scripts\python.exe tools/run_local.py`
- **前端测试**：`npm test`
- **单元测试**：`.\.venv\Scripts\python.exe -m unittest discover -s tests -q`
