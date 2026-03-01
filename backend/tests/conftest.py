"""テスト用共通設定"""

import sys
from pathlib import Path

# backend/ ディレクトリをsys.pathに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
