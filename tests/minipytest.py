"""一个极简的 pytest 兼容层。

当前环境无法访问 PyPI，安装不了 pytest，因此这里实现测试用到的
那一小部分 API（fixture / mark.asyncio / mark.parametrize / raises /
importorskip / MonkeyPatch），以便测试脚本可以在零依赖的情况下真实运行。

只覆盖本项目测试实际使用的功能，不是 pytest 的完整替代品。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import sys
import types
from collections.abc import Callable, Generator


class Skipped(Exception):
    """测试被跳过。"""


class Failed(Exception):
    """测试失败（断言之外的情况）。"""


# ----------------------------------------------------------------------
# mark / fixture
# ----------------------------------------------------------------------
class _Mark:
    """``pytest.mark.xxx`` 的简易实现。

    既可以直接当装饰器用（``@pytest.mark.x``），也可以带参数调用
    （``@pytest.mark.parametrize(...)``）。
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args, **kwargs):
        """带参数使用时转发给对应的实现；不带参数时作为装饰器。"""
        if self.name == "parametrize":
            return parametrize(*args, **kwargs)

        if len(args) == 1 and not kwargs and callable(args[0]):
            return self._apply(args[0])

        raise TypeError(f"不支持 mark.{self.name} 的调用方式：{args!r} {kwargs!r}")

    def _apply(self, func: Callable) -> Callable:
        """把标记记录到函数上。"""
        marks = getattr(func, "_pytest_marks", [])
        marks.append(self.name)
        func._pytest_marks = marks
        return func


class _MarkGenerator:
    """``pytest.mark``，按属性访问动态生成标记。"""

    def __getattr__(self, name: str) -> _Mark:
        return _Mark(name)


def parametrize(argnames, argvalues):
    """实现 ``@pytest.mark.parametrize``。

    Args:
        argnames: 参数名，字符串（逗号分隔）或名称序列。
        argvalues: 参数值列表。

    Returns:
        装饰器。
    """
    if isinstance(argnames, str):
        names = [n.strip() for n in argnames.split(",") if n.strip()]
    else:
        names = list(argnames)

    def decorator(func: Callable) -> Callable:
        cases = []
        for value in argvalues:
            if len(names) == 1:
                cases.append({names[0]: value})
            else:
                if not isinstance(value, (tuple, list)):
                    raise TypeError(
                        f"参数化 {func.__name__} 的多参数用例必须是元组：{value!r}",
                    )
                cases.append(dict(zip(names, value, strict=False)))
        pending = getattr(func, "_pytest_parametrize", [])
        pending.append((names, cases, getattr(func, "_pytest_id", None)))
        func._pytest_parametrize = pending
        return func

    return decorator


def fixture(func: Callable | None = None, **kwargs):
    """实现 ``@pytest.fixture``（仅支持函数级作用域）。"""
    def wrap(target: Callable) -> Callable:
        target._pytest_fixture = True
        return target

    if func is not None:
        return wrap(func)
    return wrap


class _RaisesContext:
    """``pytest.raises`` 的上下文管理器实现。"""

    def __init__(self, expected: type[BaseException] | tuple) -> None:
        self.expected = expected
        self.value: BaseException | None = None

    def __enter__(self) -> "_RaisesContext":
        return self

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        if exc_type is None:
            names = (
                self.expected.__name__
                if isinstance(self.expected, type)
                else ", ".join(e.__name__ for e in self.expected)
            )
            raise AssertionError(f"期望抛出 {names}，但没有抛出任何异常。")
        if issubclass(exc_type, self.expected):
            self.value = exc_value
            return True
        return False


def raises(expected: type[BaseException] | tuple) -> _RaisesContext:
    """实现 ``pytest.raises``。"""
    return _RaisesContext(expected)


def importorskip(name: str, *args, **kwargs) -> types.ModuleType:
    """实现 ``pytest.importorskip``：模块不存在时跳过整个文件。"""
    try:
        return __import__(name, fromlist=["_"])
    except ImportError as exc:
        raise Skipped(f"跳过：无法导入 {name}（{exc}）") from exc


# ----------------------------------------------------------------------
# MonkeyPatch
# ----------------------------------------------------------------------
class MonkeyPatch:
    """``monkeypatch`` fixture 的最小实现。"""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def setattr(self, target, name, value, raising: bool = True) -> None:
        """替换对象属性，并在测试结束后还原。"""
        had = hasattr(target, name)
        original = getattr(target, name, None)

        def undo() -> None:
            if had:
                setattr(target, name, original)
            else:
                with contextlib.suppress(AttributeError):
                    delattr(target, name)

        self._undo.append(undo)
        setattr(target, name, value)

    def delattr(self, target, name, raising: bool = True) -> None:
        """删除对象属性，并在测试结束后还原。"""
        original = getattr(target, name)
        setattr(target, name, original)

        def undo() -> None:
            setattr(target, name, original)

        self._undo.append(undo)
        delattr(target, name)

    def undo(self) -> None:
        """还原全部改动。"""
        for action in reversed(self._undo):
            with contextlib.suppress(Exception):
                action()
        self._undo.clear()


# ----------------------------------------------------------------------
# 模块级对象，供注入 sys.modules 使用
# ----------------------------------------------------------------------
mark = _MarkGenerator()
__all__ = [
    "Failed",
    "MonkeyPatch",
    "Skipped",
    "fixture",
    "importorskip",
    "mark",
    "parametrize",
    "raises",
]


def install() -> types.ModuleType:
    """把本模块注册为 ``pytest``，使测试文件可以直接 import pytest。"""
    module = types.ModuleType("pytest")
    module.fixture = fixture
    module.mark = mark
    module.parametrize = parametrize
    module.raises = raises
    module.importorskip = importorskip
    module.MonkeyPatch = MonkeyPatch
    module.Skipped = Skipped
    module.Failed = Failed
    module.__all__ = __all__
    sys.modules["pytest"] = module
    return module


_ID_SAFE = re.compile(r"[^0-9A-Za-z_\-.:]+")


def safe_id(text: str) -> str:
    """把用例参数转成可读的测试 ID。"""
    return _ID_SAFE.sub("_", str(text))[:80]
