"""pytest 公共配置：让测试直接 import src/server 下的模块（无需安装包）。"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_ROOT, "src", "server")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
