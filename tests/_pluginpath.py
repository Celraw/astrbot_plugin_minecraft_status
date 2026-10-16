"""让测试能以包的形式导入插件本体。

仓库采用 AstrBot 社区约定的「平铺」结构：``metadata.yaml`` / ``main.py`` 等
直接位于仓库根目录，以便 AstrBot 用 Git 地址安装时能识别插件。

但这样仓库根目录本身就是一个包，测试无法用 ``import astrbot_plugin_minecraft_status``
直接导入。本模块把仓库根目录注册为一个名为 ``astrbot_plugin_minecraft_status``
的包，使各测试文件可以用与线上一致的方式导入：

    import _pluginpath  # noqa: F401
    from astrbot_plugin_minecraft_status import mc_protocol

这样既保留了 AstrBot 要求的平铺结构，也保留了清晰的包内相对导入。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
"""仓库根目录，同时也是插件目录。"""

PACKAGE_NAME = "astrbot_plugin_minecraft_status"
"""插件包名（与 metadata.yaml 中的 name 一致）。"""


def ensure_plugin_package() -> Path:
    """把仓库根目录注册为插件包，并返回该目录。

    重复调用是安全的（已注册时直接返回）。
    """
    if PACKAGE_NAME not in sys.modules:
        init_file = PLUGIN_DIR / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME,
            init_file,
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        if spec is None or spec.loader is None:  # pragma: no cover - 仅在文件缺失时
            raise RuntimeError(f"无法为插件目录创建模块 spec：{PLUGIN_DIR}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = module
        spec.loader.exec_module(module)

    return PLUGIN_DIR


ensure_plugin_package()
