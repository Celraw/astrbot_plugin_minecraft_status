"""端到端验证 zip 安装包能被 AstrBot 真正加载。

这个脚本模拟 AstrBot 的「从文件安装插件」流程中与安装包相关的部分：

    1. 记录归档里的全部条目
    2. 用 AstrBot 的 ``_extract_plugin_archive`` 解压并展平目录结构
       （这一步会处理 GitHub 下载包常见的 ``仓库名-sha`` 外层目录）
    3. 用 AstrBot 的元数据校验函数校验解压结果
    4. 确认插件目录名与 metadata.name 一致（importlib 加载的前提）
    5. 真实执行 ``importlib`` 导入 main.py，验证插件类能被加载

用法（建议在 AstrBot 的 Python 环境下运行）：
    PYTHONPATH=<AstrBot源码> python tests/verify_zip_install.py [zip路径]
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZIP = PROJECT_ROOT / "astrbot_plugin_minecraft_status.zip"

# 让脚本能导入项目根目录下的 package.py。
sys.path.insert(0, str(PROJECT_ROOT))

# 避免 AstrBot 把运行时数据写进项目目录。
if not os.environ.get("ASTRBOT_ROOT"):
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="astrbot-zip-verify-")

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一条校验结果。"""
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" -> {detail}" if detail else ""))
        FAILURES.append(label)


def main() -> int:
    """执行安装包校验。"""
    zip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ZIP
    print("=" * 72)
    print("插件 zip 安装包校验")
    print("=" * 72)
    print(f"安装包：{zip_path}")

    if not zip_path.exists():
        print("找不到安装包，请先运行 python package.py")
        return 1

    try:
        from astrbot.core.star.updater import _PluginUpdater

        has_astrbot = True
    except ImportError:
        has_astrbot = False
        print("（未检测到 AstrBot，仅做通用 zip 校验）")

    # ------------------------------------------------------------------
    # 1. 归档结构
    # ------------------------------------------------------------------
    print("\n1. 归档结构")
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        bad = archive.testzip()

    check(bad is None, "zip 完整性校验通过（无损坏条目）", str(bad))
    check(
        all(n.startswith("astrbot_plugin_minecraft_status/") for n in names),
        "所有条目都在单一顶层目录下",
    )
    check(
        "astrbot_plugin_minecraft_status/metadata.yaml" in names,
        "包含 metadata.yaml（位于顶层）",
    )
    check(
        "astrbot_plugin_minecraft_status/main.py" in names,
        "包含 main.py",
    )
    check(
        "astrbot_plugin_minecraft_status/_conf_schema.json" in names,
        "包含 _conf_schema.json",
    )
    check(
        not any(n.endswith(".pyc") or "__pycache__" in n for n in names),
        "不包含 __pycache__ / .pyc 缓存文件",
    )
    check(
        not any(n.startswith("/") or ".." in n for n in names),
        "没有绝对路径或路径穿越条目",
    )

    # ------------------------------------------------------------------
    # 2. 用 AstrBot 的官方逻辑解压展平
    # ------------------------------------------------------------------
    # 注意：_extract_plugin_archive 是「安装」流程的一部分，它会把插件内容
    # 展平到 target_dir 根目录，随后**删除传入的 zip**。因此这里必须先复制到
    # 临时目录再调用，绝不能把真正的交付物交给它。
    print("\n2. AstrBot 官方解压逻辑")
    if has_astrbot:
        with tempfile.TemporaryDirectory() as tmp:
            staged_zip = Path(tmp) / zip_path.name
            shutil.copy2(zip_path, staged_zip)
            target = Path(tmp) / "plugin"
            try:
                _PluginUpdater()._extract_plugin_archive(str(staged_zip), str(target))
                check(True, "_extract_plugin_archive 安装流程解压成功")
            except Exception as exc:
                check(False, "_extract_plugin_archive 安装流程解压成功", str(exc))
                target = None

            if target is not None and target.exists():
                # 展平后，插件内容应直接位于 target 根目录。
                flattened = (target / "metadata.yaml").is_file() and (
                    target / "main.py"
                ).is_file()
                check(flattened, "目录结构已展平（metadata.yaml 与 main.py 在根目录）")
                check(
                    (target / "templates" / "status.html").is_file(),
                    "templates/status.html 被完整保留",
                )
                check(
                    not (target / "astrbot_plugin_minecraft_status").exists(),
                    "顶层目录已被展平（不再嵌套一层）",
                )
                importlib_error = _try_import(target)
                check(
                    importlib_error is None,
                    "展平后的插件 main.py 可被 importlib 正常导入",
                    importlib_error or "",
                )
    else:
        check(True, "跳过 AstrBot 官方解压逻辑（环境未安装 AstrBot）")

    # ------------------------------------------------------------------
    # 3. 通用解压 + 装载必备文件
    # ------------------------------------------------------------------
    print("\n3. 解压后装载必备文件")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)
        plugin_root = Path(tmp) / "astrbot_plugin_minecraft_status"

        required = [
            "metadata.yaml",
            "main.py",
            "_conf_schema.json",
            "requirements.txt",
            "mc_protocol.py",
            "store.py",
            "commands.py",
            "formatter.py",
            "__init__.py",
            "templates/status.html",
        ]
        missing = [r for r in required if not (plugin_root / r).is_file()]
        check(not missing, f"全部 {len(required)} 个文件解压后存在", str(missing))

        # _conf_schema.json 必须是合法 JSON。
        import json

        try:
            schema = json.loads(
                (plugin_root / "_conf_schema.json").read_text(encoding="utf-8"),
            )
            check(isinstance(schema, dict), "配置 schema 是合法 JSON 对象")
            check(
                "output_mode" in schema,
                "配置包含 output_mode（文字/图片切换）",
            )
        except Exception as exc:
            check(False, "配置 schema 是合法 JSON 对象", str(exc))

        # metadata.yaml 必需字段。
        from package import read_metadata_name

        name = read_metadata_name(plugin_root / "metadata.yaml")
        check(
            name == "astrbot_plugin_minecraft_status",
            "metadata.name 与目录名一致（importlib 加载前提）",
            str(name),
        )

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    # 回归保护：AstrBot 的 _extract_plugin_archive 会删除传入的 zip，
    # 因此校验流程结束后交付物必须仍然存在。
    print("\n4. 交付物完整性")
    check(zip_path.exists(), "校验结束后安装包仍存在（未被校验流程删除）")
    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as archive:
            check(
                len(archive.namelist()) == len(names),
                "安装包内容未被修改",
                f"{len(archive.namelist())} != {len(names)}",
            )

    print()
    print("=" * 72)
    if FAILURES:
        print(f"校验未通过：{len(FAILURES)}/{CHECKS} 项失败")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{CHECKS}/{CHECKS} 项校验成功")
    return 0


def _try_import(plugin_root: Path) -> str | None:
    """尝试用 importlib 导入插件，返回错误信息或 None。"""
    sys.path.insert(0, str(plugin_root.parent))
    try:
        spec = importlib.util.spec_from_file_location(
            "astrbot_plugin_minecraft_status.main",
            plugin_root / "main.py",
            submodule_search_locations=[str(plugin_root)],
        )
        if spec is None or spec.loader is None:
            return "无法创建模块 spec"
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "MinecraftStatusPlugin"):
            return "模块内未找到 MinecraftStatusPlugin 类"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    finally:
        if str(plugin_root.parent) in sys.path:
            sys.path.remove(str(plugin_root.parent))


if __name__ == "__main__":
    raise SystemExit(main())
