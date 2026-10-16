"""把插件打包成可分发的 zip（用于 GitHub Release）。

仓库本身采用 AstrBot 约定的「平铺」结构（``metadata.yaml`` 在仓库根目录），
这样 AstrBot 用 Git 地址安装时可以直接识别。

打出的 zip 则会把这些文件放进一个顶层目录 ``astrbot_plugin_minecraft_status/``，
符合 AstrBot「从文件安装」的预期（它会自动展平顶层目录）::

    astrbot_plugin_minecraft_status/
    ├── main.py
    ├── mc_protocol.py
    ├── store.py
    ├── commands.py
    ├── formatter.py
    ├── renderer_local.py
    ├── metadata.yaml
    ├── _conf_schema.json
    ├── requirements.txt
    ├── README.md
    ├── LICENSE
    └── templates/status.html

打完包后会调用 AstrBot 自己的归档校验逻辑
（``_PluginUpdater.validate_plugin_archive``）验证产物。

用法:
    python package.py
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PLUGIN_NAME = "astrbot_plugin_minecraft_status"
OUTPUT_ZIP = PROJECT_ROOT / f"{PLUGIN_NAME}.zip"

# 插件本体文件（仓库根目录下的这些文件属于插件）。
PLUGIN_FILES = (
    "__init__.py",
    "_conf_schema.json",
    "commands.py",
    "formatter.py",
    "main.py",
    "mc_protocol.py",
    "metadata.yaml",
    "renderer_local.py",
    "requirements.txt",
    "store.py",
)
PLUGIN_DIRS = ("templates",)

# 随包附带、但不是插件本体的文件。
EXTRA_FILES = ("README.md", "LICENSE")

# 绝不进入发布包的内容。
EXCLUDE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    ".DS_Store",
    "Thumbs.db",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".log"}


def _should_skip(path: Path) -> bool:
    """判断某个路径是否应被排除。"""
    if path.name in EXCLUDE_NAMES:
        return True
    return path.suffix.lower() in EXCLUDE_SUFFIXES


def collect_payload() -> list[tuple[Path, str]]:
    """收集需要打包的文件。

    Returns:
        [(源文件路径, 归档内相对路径), ...]，按归档路径排序以保证可复现。
    """
    entries: list[tuple[Path, str]] = []

    for name in PLUGIN_FILES:
        path = PROJECT_ROOT / name
        if path.is_file():
            entries.append((path, name))

    for dirname in PLUGIN_DIRS:
        directory = PROJECT_ROOT / dirname
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or _should_skip(path):
                continue
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            entries.append((path, relative))

    for name in EXTRA_FILES:
        path = PROJECT_ROOT / name
        if path.is_file():
            entries.append((path, name))

    return sorted(entries, key=lambda item: item[1])


def build_zip(destination: Path) -> list[str]:
    """创建 zip，返回归档内的路径列表。"""
    entries = collect_payload()
    written: list[str] = []

    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path, relative in entries:
            arcname = f"{PLUGIN_NAME}/{relative}"
            info = zipfile.ZipInfo(arcname)
            # 固定时间戳，保证相同内容产生完全一致的 zip。
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
            written.append(arcname)

    return written


def read_metadata_name(metadata_path: Path) -> str | None:
    """从 metadata.yaml 读取 name 字段（无 PyYAML 时退化为逐行解析）。"""
    text = metadata_path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        for line in text.splitlines():
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip("'\"")
        return None

    metadata = yaml.safe_load(text)
    if isinstance(metadata, dict):
        return metadata.get("name")
    return None


def verify_extraction(zip_path: Path) -> None:
    """解压到临时目录并确认插件必需文件齐全。"""
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)

        plugin_root = Path(tmp) / PLUGIN_NAME
        required = [
            "metadata.yaml",
            "main.py",
            "_conf_schema.json",
            "mc_protocol.py",
            "store.py",
            "commands.py",
            "formatter.py",
            "renderer_local.py",
            "templates/status.html",
        ]
        missing = [name for name in required if not (plugin_root / name).exists()]
        if missing:
            raise SystemExit(f"解压后缺少必需文件：{missing}")

        name = read_metadata_name(plugin_root / "metadata.yaml")
        if name != PLUGIN_NAME:
            raise SystemExit(
                f"metadata.name({name!r}) 与目录名({PLUGIN_NAME!r}) 不一致",
            )

        print(f"  解压校验通过：{PLUGIN_NAME}/ 下必需文件齐全")
        print(f"  元数据 name 与目录名一致：{name}")


def verify_with_astrbot(zip_path: Path) -> None:
    """用 AstrBot 的归档校验逻辑验证产物（不可用时跳过）。"""
    try:
        from astrbot.core.star.updater import _PluginUpdater
    except ImportError:
        print("  （未检测到 AstrBot 环境，跳过官方归档校验）")
        return

    inspection = _PluginUpdater.inspect_plugin_archive(str(zip_path))
    metadata = inspection["metadata"]
    print(f"  AstrBot 归档校验通过：入口 {inspection['metadata_entry']}")
    print(f"    name: {metadata.get('name')}")
    print(f"    version: {metadata.get('version')}")


def main() -> int:
    """执行打包。"""
    if not (PROJECT_ROOT / "metadata.yaml").is_file():
        print(f"找不到 metadata.yaml，确认在仓库根目录运行：{PROJECT_ROOT}")
        return 1

    if OUTPUT_ZIP.exists():
        OUTPUT_ZIP.unlink()

    written = build_zip(OUTPUT_ZIP)
    size_kb = OUTPUT_ZIP.stat().st_size / 1024

    print(f"已生成：{OUTPUT_ZIP.name}（{size_kb:.1f} KB，{len(written)} 个文件）")
    for name in written:
        print(f"  {name}")

    print("\n校验：")
    verify_extraction(OUTPUT_ZIP)
    verify_with_astrbot(OUTPUT_ZIP)

    print("\n打包完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
