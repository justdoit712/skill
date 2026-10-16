# 特定品种 Skill 专区 (Special Skills)

本目录用于存放特定品类、垂类领域或定制化的 AI Agent Skill 元数据。

## 1. 核心设计原则

- **纯 JSON 存储**：只保存与主目录推荐区/候选区同构的 JSON 条目数据，**不下载、不镜像、不执行任何代码**。
- **按品类分文件**：每个特定品种独立为一个 JSON 文件（如 `ui-design-style.json`），文件名即品类。
- **搜索去重与隔离保护**：凡是在本目录下登记了 `skill_id` 的条目，流水线采集与定向搜索时自动跳过抓取与 LLM 评估，保障 0 额外 Token 消耗与数据隔离。

## 2. 单条 Entry 数据格式参考

每个 `<品种名>.json` 文件中的 `entries` 列表元素格式与 `data/catalog.json` 保持完全一致：

```json
{
  "skill_id": "owner/repo:path/to/SKILL.md",
  "name": "skill-name",
  "url": "https://github.com/owner/repo/blob/HEAD/path/to/SKILL.md",
  "repo_url": "https://github.com/owner/repo",
  "author": "owner",
  "summary_zh": "中文功能简述与设计风格说明。",
  "main_category": {
    "id": "design",
    "name": "产品与设计"
  },
  "tags": ["UI设计", "设计风格", "前端界面"],
  "status": "special",
  "source_type": "manual",
  "added_at": "2026-10-09"
}
```
