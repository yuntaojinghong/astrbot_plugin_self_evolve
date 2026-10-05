"""给 pytest 用的入口检查。

本仓库真正的测试是**脚本**（tests/test_logic.py、tests/test_settings.py），
用 `python tests/xxx.py` 跑——它们自己往 sys.modules 里塞假的 astrbot 模块，
并用 print 输出分段结果，见 pytest.ini 里的说明。

但一个仓库如果 `pytest` 跑起来什么都不做，也很容易被误认为"没有测试"。
所以这里放一个真正的 pytest 用例：它不重复脚本里的断言，只负责**看住那些脚本**——
文件还在不在、语法有没有坏、关键用例有没有被误删。

这样两件事都成立：
  · `pytest` 有东西可跑，且通过
  · 脚本测试被删掉/写坏时，pytest 会报出来
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

#: 必须存在的脚本测试
REQUIRED_SCRIPTS = ["test_logic.py", "test_settings.py"]


@pytest.mark.parametrize("name", REQUIRED_SCRIPTS)
def test_script_exists(name: str) -> None:
    path = TESTS / name
    assert path.is_file(), f"{path} 不见了"
    assert path.stat().st_size > 1000, f"{path} 小得可疑，可能被清空了"


@pytest.mark.parametrize("name", REQUIRED_SCRIPTS)
def test_script_compiles(name: str) -> None:
    """语法必须正确。语法坏掉时 CI 里的 `python tests/xxx.py` 会直接报错，
    但那条信息离"哪个文件坏了"比较远，这里先给一个明确的。"""
    src = (TESTS / name).read_text(encoding="utf-8")
    ast.parse(src, filename=str(TESTS / name))


@pytest.mark.parametrize("name", REQUIRED_SCRIPTS)
def test_script_has_main_guard(name: str) -> None:
    """脚本要有执行入口。

    test_settings.py 曾经把断言全写在模块顶层：`pytest tests` 会在 collect
    阶段就执行它们，报出来的却是 "ERROR collecting"；而且它和 test_logic.py
    都改 sys.modules，同进程跑会互相覆盖对方的桩，报出与真实原因无关的错。
    收进 main() 之后就没这个问题。
    """
    src = (TESTS / name).read_text(encoding="utf-8")
    assert "__main__" in src, f"{name} 缺少 __main__ 入口，没法直接跑"


def test_settings_has_pytest_entry() -> None:
    """test_settings.py 要有一个能被 pytest 调用的函数。"""
    tree = ast.parse((TESTS / "test_settings.py").read_text(encoding="utf-8"))
    funcs = [n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert "main" in funcs, "test_settings.py 里找不到 main()"
    assert any(f.startswith("test_") for f in funcs), (
        "test_settings.py 没有 test_ 开头的函数，pytest 不会执行它")


def test_logic_assertion_count() -> None:
    """test_logic.py 的检查项数量不该突然变少——防止有人误删大段用例。

    注意它**不是**用 assert，而是自带一个 `check(name, cond)` 累加器
    （跑完打印「结果: 220 通过, 0 失败」），所以这里数的是 check( 的调用。
    """
    src = (TESTS / "test_logic.py").read_text(encoding="utf-8")
    n = src.count("check(")
    assert n >= 150, (
        f"test_logic.py 只剩 {n} 处 check( 调用，像是被删了内容"
        f"（该文件用自带的 check() 累加器，不是 assert）")

