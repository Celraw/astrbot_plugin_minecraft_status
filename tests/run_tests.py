"""零依赖测试运行器。

当前环境无法访问 PyPI（装不了 pytest），因此这里用一个自带的极简
pytest 兼容层 + 本运行器来真实执行 tests/ 下的全部测试。

用法:
    python tests/run_tests.py               # 运行全部测试
    python tests/run_tests.py test_store    # 只运行匹配的测试文件
    python tests/run_tests.py -v            # 显示每个用例的结果

退出码: 0 表示全部通过，1 表示存在失败或错误。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

# AstrBot 默认把数据目录放在「当前工作目录」下的 data/。
# 直接 import astrbot 会在项目里生成 data/ 之类的运行时目录，
# 因此这里显式把 ASTRBOT_ROOT 指向一个临时目录，避免污染工作区。
if not os.environ.get("ASTRBOT_ROOT"):
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="astrbot-test-root-")

sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import minipytest  # noqa: E402

minipytest.install()

# 仓库采用 AstrBot 约定的平铺结构，这里把仓库根目录注册为插件包，
# 使测试能按 ``astrbot_plugin_minecraft_status.xxx`` 正常导入。
import _pluginpath  # noqa: E402,F401

import pytest  # noqa: E402  （已被上面的 install 替换为兼容层）


class Outcome:
    """单个用例的执行结果。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "passed"
        self.message = ""


def _is_fixture(obj: object) -> bool:
    """判断对象是否是 fixture 函数。"""
    return callable(obj) and getattr(obj, "_pytest_fixture", False)


async def _resolve_fixture(func, fixtures: dict, cache: dict):
    """解析并执行一个 fixture 函数。"""
    if func.__name__ in cache:
        return cache[func.__name__]

    kwargs = {}
    for param_name in inspect.signature(func).parameters:
        if param_name == "monkeypatch":
            kwargs[param_name] = pytest.MonkeyPatch()
            continue
        provider = fixtures.get(param_name)
        if provider is None:
            raise RuntimeError(
                f"fixture `{func.__name__}` 需要未定义的 fixture `{param_name}`",
            )
        kwargs[param_name] = await _resolve_fixture(provider, fixtures, cache)

    value = func(**kwargs)
    if inspect.isawaitable(value):
        value = await value
    cache[func.__name__] = value
    return value


async def _call_with_fixtures(func, fixtures: dict) -> None:
    """按签名注入 fixture 后调用测试函数。"""
    cache: dict = {}
    kwargs = {}
    params = inspect.signature(func).parameters
    temp_dirs: list[tempfile.TemporaryDirectory] = []
    for param_name in params:
        if param_name == "monkeypatch":
            kwargs[param_name] = pytest.MonkeyPatch()
            continue
        if param_name == "tmp_path":
            # 内置 fixture：提供一个临时目录（Path 对象）。
            holder = tempfile.TemporaryDirectory(prefix="pytest-tmp-")
            temp_dirs.append(holder)
            kwargs[param_name] = Path(holder.name)
            continue
        provider = fixtures.get(param_name)
        if provider is None:
            raise RuntimeError(
                f"测试 `{func.__name__}` 需要未定义的 fixture `{param_name}`",
            )
        kwargs[param_name] = await _resolve_fixture(provider, fixtures, cache)

    try:
        result = func(**kwargs)
        if inspect.isawaitable(result):
            await result
    finally:
        for value in kwargs.values():
            if isinstance(value, pytest.MonkeyPatch):
                value.undo()
        for holder in temp_dirs:
            holder.cleanup()


def _expand_params(func, module) -> list[tuple[str, dict]]:
    """展开 parametrize 装饰器，生成 (用例名, 参数) 列表。"""
    pending = getattr(func, "_pytest_parametrize", None)
    if not pending:
        return [(func.__name__, {})]

    cases: list[tuple[str, dict]] = [("", {})]
    for names, values, _ in pending:
        expanded: list[tuple[str, dict]] = []
        for prefix_id, prefix_kwargs in cases:
            for case in values:
                case_id = ",".join(minipytest.safe_id(case[n]) for n in names)
                merged = dict(prefix_kwargs)
                merged.update(case)
                new_id = f"{prefix_id}[{case_id}]" if prefix_id else f"[{case_id}]"
                expanded.append((new_id, merged))
        cases = expanded
    return [(func.__name__ + suffix, kwargs) for suffix, kwargs in cases]


async def run_module(module, verbose: bool, filter_text: str | None) -> list[Outcome]:
    """执行单个测试模块，返回全部用例结果。"""
    outcomes: list[Outcome] = []
    fixtures = {
        name: obj
        for name, obj in vars(module).items()
        if _is_fixture(obj)
    }

    for name, obj in list(vars(module).items()):
        if not name.startswith("test_") or not callable(obj):
            continue

        for case_name, extra_kwargs in _expand_params(obj, module):
            full_name = f"{module.__name__}::{case_name}"
            if filter_text and filter_text.lower() not in full_name.lower():
                continue

            outcome = Outcome(full_name)
            try:
                if extra_kwargs:
                    async def _run(fn=obj, kw=extra_kwargs):
                        cache: dict = {}
                        call_kwargs = {}
                        temp_dirs: list[tempfile.TemporaryDirectory] = []
                        for param_name in inspect.signature(fn).parameters:
                            if param_name in kw:
                                call_kwargs[param_name] = kw[param_name]
                                continue
                            if param_name == "monkeypatch":
                                call_kwargs[param_name] = pytest.MonkeyPatch()
                                continue
                            if param_name == "tmp_path":
                                holder = tempfile.TemporaryDirectory(
                                    prefix="pytest-tmp-",
                                )
                                temp_dirs.append(holder)
                                call_kwargs[param_name] = Path(holder.name)
                                continue
                            provider = fixtures.get(param_name)
                            if provider is None:
                                raise RuntimeError(
                                    f"测试 `{fn.__name__}` 需要未定义的 fixture "
                                    f"`{param_name}`",
                                )
                            call_kwargs[param_name] = await _resolve_fixture(
                                provider, fixtures, cache,
                            )
                        try:
                            result = fn(**call_kwargs)
                            if inspect.isawaitable(result):
                                await result
                        finally:
                            for value in call_kwargs.values():
                                if isinstance(value, pytest.MonkeyPatch):
                                    value.undo()
                            for holder in temp_dirs:
                                holder.cleanup()

                    await _run()
                else:
                    await _call_with_fixtures(obj, fixtures)
            except pytest.Skipped as exc:
                outcome.status = "skipped"
                outcome.message = str(exc)
            except AssertionError as exc:
                outcome.status = "failed"
                outcome.message = str(exc) or "断言失败"
            except Exception as exc:  # noqa: BLE001
                outcome.status = "error"
                outcome.message = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

            outcomes.append(outcome)
            if verbose:
                icon = {"passed": "PASS", "failed": "FAIL", "error": "ERROR", "skipped": "SKIP"}[
                    outcome.status
                ]
                print(f"  [{icon}] {full_name}")
                if outcome.message:
                    print(f"         {outcome.message.splitlines()[0]}")

    return outcomes


async def main_async(args: argparse.Namespace) -> int:
    """运行全部匹配的测试模块。"""
    pattern = f"*{args.filter}*.py" if args.filter else "test_*.py"
    files = sorted(p for p in TESTS_DIR.glob(pattern) if p.name != "minipytest.py")
    if not files:
        print(f"没有找到匹配的测试文件：{pattern}")
        return 1

    all_outcomes: list[Outcome] = []
    for path in files:
        if args.verbose:
            print(f"\n{path.name}")
        try:
            source = path.read_text(encoding="utf-8")
            module = type(sys)(path.stem)
            module.__file__ = str(path)
            module.__name__ = path.stem
            sys.modules[path.stem] = module
            exec(compile(source, str(path), "exec"), module.__dict__)
        except pytest.Skipped as exc:
            print(f"SKIP {path.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR 导入 {path.name} 失败: {exc}")
            traceback.print_exc()
            all_outcomes.append(_error_outcome(path.name, exc))
            continue

        all_outcomes.extend(await run_module(module, args.verbose, args.filter))

    passed = [o for o in all_outcomes if o.status == "passed"]
    failed = [o for o in all_outcomes if o.status == "failed"]
    errors = [o for o in all_outcomes if o.status == "error"]
    skipped = [o for o in all_outcomes if o.status == "skipped"]

    print()
    print("=" * 72)
    for outcome in failed + errors:
        label = "FAIL" if outcome.status == "failed" else "ERROR"
        print(f"[{label}] {outcome.name}")
        for line in outcome.message.splitlines():
            print(f"    {line}")
        print()
    print("=" * 72)
    print(
        f"结果：{len(passed)} 通过, {len(failed)} 失败, "
        f"{len(errors)} 错误, {len(skipped)} 跳过（共 {len(all_outcomes)}）",
    )
    return 0 if not failed and not errors else 1


def _error_outcome(name: str, exc: Exception) -> Outcome:
    """构造一个模块导入失败的占位结果。"""
    outcome = Outcome(f"{name}::<import>")
    outcome.status = "error"
    outcome.message = f"{type(exc).__name__}: {exc}"
    return outcome


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行测试（零依赖）")
    parser.add_argument("filter", nargs="?", default=None, help="测试文件名关键字")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示每个用例")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
