"""social-hub：自研社媒自动发布系统（双通道 API/CDP）。"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:  # 单一版本源 = pyproject.toml；源码直跑（未安装）时回退
    __version__ = _pkg_version("social-hub")
except PackageNotFoundError:
    __version__ = "0.1.1"
