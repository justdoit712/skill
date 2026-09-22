"""定向查找 Skill：根据用户具体需求在 GitHub 上搜索、读取并评估匹配的 Skill。

用法示例：
    python tools/find_skill.py "生成高质量 Prompt"
    python tools/find_skill.py "提示词优化" --limit 3 --max-evaluations 10
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.skill_finder import main


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main(root=ROOT))
