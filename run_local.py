"""在 PyCharm 中右键 Run 即可；参数在 config/local-run.json 中修改。"""

from pathlib import Path
import sys

from src.local_run import main


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main(root=Path(__file__).resolve().parent))
