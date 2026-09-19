#!/usr/bin/env python3
"""
技能验证工具入口脚本

用法:
  python skill_validator.py validate                     # 验证所有技能
  python skill_validator.py validate skills/agent-team   # 验证单个技能
  python skill_validator.py read-properties skills/agent-team
  python skill_validator.py to-prompt skills/agent-team
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_validator"))

from skill_validator.cli import main

if __name__ == "__main__":
    main()
