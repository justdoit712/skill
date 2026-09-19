#!/usr/bin/env python3
"""Generate llms-full.txt for AI Agent 技能商店 (skill store).
Combines:
- SKILL_SOURCES.json (auto-synced upstream skills)
- README.md local skills sections (curated 61 local skills)
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES_JSON = REPO / "SKILL_SOURCES.json"
README = REPO / "README.md"
OUT = REPO / "public" / "llms-full.txt"

# 1. Load SKILL_SOURCES.json
with open(SOURCES_JSON, "r", encoding="utf-8") as f:
    sources_doc = json.load(f)
sources = sources_doc.get("sources", {})
last_updated = sources_doc.get("last_updated", "")
total_count = sources_doc.get("total_count", len(sources))

# Group sources by team / org (first path segment of "owner/repo")
team_buckets = {}
for repo_path, meta in sources.items():
    owner = repo_path.split("/", 1)[0]
    team_buckets.setdefault(owner, []).append((repo_path, meta))

# 2. Read README.md to extract official-team summary and local-skills sections
readme_text = README.read_text(encoding="utf-8")

# Extract official team summary block (## 📚 官方技能来源 ...)
official_block_match = re.search(
    r"## 📚 官方技能来源.*?(?=\n## )", readme_text, re.S
)
official_block = official_block_match.group(0).strip() if official_block_match else ""

# Extract local skills block (## 💾 本地技能库 ... up to next top-level ##)
local_block_match = re.search(
    r"## 💾 本地技能库.*?(?=\n## 🚀)", readme_text, re.S
)
local_block = local_block_match.group(0).strip() if local_block_match else ""

# 3. Compose llms-full.txt
lines = []
add = lines.append

add("# llms-full.txt - AI Agent 技能商店 全量精简版")
add("")
add("> 本文件遵循 llms.txt 标准 (https://llmstxt.org)")
add("> 提供站点全量技能列表的精简文本版，方便 AI 引擎一次性吸收。")
add("")
add("## 站点信息")
add("")
add("- 名称: AI Agent 技能商店 (Skill Store)")
add("- 仓库: https://github.com/anbeime/skill")
add("- 主页: https://skill.miyucaicai.cn")
add("- 兄弟项目: https://solar.miyucaicai.cn")
add(f"- 上游技能源最近更新: {last_updated}")
add(f"- 自动爬取技能数: {total_count}")
add("- 本地精选技能数: 61")
add("- 技能总数（README 公布口径）: 243")
add("")
add("## 数据来源")
add("")
add("- 上游 awesome 仓库: https://github.com/VoltAgent/awesome-agent-skills （每 24 小时同步）")
add("- 本地仓库 README: https://github.com/anbeime/skill/blob/main/README.md")
add("- 完整源 JSON: https://github.com/anbeime/skill/blob/main/SKILL_SOURCES.json")
add("")

# ----- Official team summary -----
add("## 官方技能来源（顶级团队）")
add("")
if official_block:
    # strip the heading line, keep body
    body_lines = official_block.split("\n", 1)[1].strip()
    add(body_lines)
else:
    add("(see README)")
add("")

# ----- Auto-synced upstream skills -----
add("## 自动同步技能列表（按贡献者/组织聚合）")
add("")
add(f"以下 {total_count} 个技能由每日爬虫从 awesome-agent-skills 自动同步。")
add("格式：`owner/repo  ←  upstream_source  ←  discovered_at`")
add("")

for owner in sorted(team_buckets.keys(), key=lambda x: (-len(team_buckets[x]), x.lower())):
    items = team_buckets[owner]
    add(f"### {owner} ({len(items)})")
    add("")
    for repo_path, meta in sorted(items):
        upstream = meta.get("upstream_source", "")
        discovered = meta.get("discovered_at", "")[:10]
        url = f"https://github.com/{repo_path}"
        add(f"- [{repo_path}]({url})  ←  {upstream}  ←  {discovered}")
    add("")

# ----- Local curated skills -----
add("## 本地精选技能（61 个，中文垂直领域）")
add("")
if local_block:
    # remove the "## 💾 本地技能库（61个）" heading line; demote subheadings (### -> ####) to nest under H2
    body = local_block.split("\n", 1)[1]
    body = re.sub(r"^### ", "#### ", body, flags=re.M)
    add(body.strip())
else:
    add("(see README local skills section)")
add("")

# ----- Use & contribution -----
add("## 使用条款")
add("")
add("- 允许 AI 模型抓取、分析、引用本文件及衍生数据")
add("- 引用请标注来源: https://github.com/anbeime/skill")
add("- 收录的技能本身遵循各自上游开源许可")
add("")
add("## 贡献 & 联系")
add("")
add("- 提交技能: https://github.com/anbeime/skill/issues/new?template=submit-skill.yml")
add("- GitHub Issues: https://github.com/anbeime/skill/issues")
add("- 邮箱: anbeime@coze.email")
add("")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote {OUT} ({OUT.stat().st_size} bytes, {len(lines)} lines)")
