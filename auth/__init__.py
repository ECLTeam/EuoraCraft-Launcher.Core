"""为原始 Microsoft 认证模块提供稳定的导出路径。"""

import sys

from ..Core import MicrosoftAuth as microsoft
from ..Core.MicrosoftAuth import MicrosoftAuthManager

# 复用原始模块对象，确保测试或插件替换 MicrosoftAuth 时作用于真实实现。
sys.modules[f"{__name__}.microsoft"] = microsoft

__all__ = ["MicrosoftAuthManager", "microsoft"]
