"""Offline tests for the code wrapper. Each wrapped file is actually run, with
flag_on replaced by a fake, to prove both paths still behave like the originals."""

import asyncio
import textwrap

import pytest
from fastapi.testclient import TestClient

from autopilot.transformer import SDK_IMPORT, TransformError, changed_functions, wrap

FLAG = "ap_checkout_v2"

BASE = textwrap.dedent('''\
    """Pricing logic for the demo shop."""

    PRICES = {"tea": 4.50, "coffee": 5.00, "mug": 12.00, "beans": 18.00}


    class UnknownItem(ValueError):
        pass


    def calculate_total(items):
        total = 0.0
        for item in items:
            if item["sku"] not in PRICES:
                raise UnknownItem(item["sku"])
            total += PRICES[item["sku"]] * item["qty"]
        return round(total, 2)


    def shipping(total):
        return 0 if total > 50 else 5
''')

HEAD = BASE.replace(
    "    return round(total, 2)\n",
    "    # New: 10% off orders of 30.00 or more.\n"
    "    if total >= 30:\n"
    "        total *= 0.9\n"
    "    return round(total, 2)\n",
)

assert HEAD != BASE

CART = [{"sku": "beans", "qty": 2}]  # 36.00 -> 32.40 with the discount


def load(source, flag_value):
    """Run the wrapped module with flag_on faked; returns its namespace and the flags asked."""
    asked = []
    namespace = {}
    exec(compile(source, "<wrapped>", "exec"), namespace)

    def fake_flag_on(name):
        asked.append(name)
        return flag_value
    namespace["flag_on"] = fake_flag_on
    return namespace, asked


# --- the real checkout shape ---------------------------------------------------

def test_wrap_checkout_runs_old_and_new_paths():
    out = wrap(BASE, HEAD, "calculate_total", FLAG)
    off, asked_off = load(out, False)
    on, asked_on = load(out, True)
    assert off["calculate_total"](CART) == 36.00
    assert on["calculate_total"](CART) == 32.40
    assert asked_off == [FLAG] and asked_on == [FLAG]
    # Errors still come from the original code on both paths.
    for ns in (off, on):
        with pytest.raises(ns["UnknownItem"]):
            ns["calculate_total"]([{"sku": "nope", "qty": 1}])


def test_wrap_checkout_output_shape():
    out = wrap(BASE, HEAD, "calculate_total", FLAG)
    assert "def _calculate_total__old(items):" in out
    assert "def _calculate_total__new(items):" in out
    assert "def calculate_total(items):" in out          # exact signature, not *args/**kwargs
    assert f'if flag_on("{FLAG}"):' in out
    assert "# New: 10% off orders of 30.00 or more." in out  # comments in the PR body survive
    assert out.count(SDK_IMPORT) == 1
    # Import goes after the docstring with a blank line, before the code.
    assert out.startswith('"""Pricing logic for the demo shop."""\n\n' + SDK_IMPORT + "\n")
    # Code outside the function is untouched.
    assert "def shipping(total):\n    return 0 if total > 50 else 5" in out
    compile(out, "<wrapped>", "exec")


def test_old_comes_from_base_and_new_from_pr():
    out = wrap(BASE, HEAD, "calculate_total", FLAG)
    old_part = out.split("def _calculate_total__old")[1].split("def _calculate_total__new")[0]
    new_part = out.split("def _calculate_total__new")[1].split("def calculate_total")[0]
    assert "total *= 0.9" not in old_part
    assert "total *= 0.9" in new_part


def test_changes_elsewhere_in_the_pr_are_kept():
    head = HEAD.replace('"mug": 12.00', '"mug": 13.00')
    out = wrap(BASE, head, "calculate_total", FLAG)
    assert '"mug": 13.00' in out


# --- signatures ----------------------------------------------------------------

def test_wrap_keeps_every_kind_of_parameter():
    base = textwrap.dedent('''\
        def f(a, /, b, c=2, *rest, key, opt=None, **extra) -> dict:
            return {"v": "old", "a": a, "b": b, "c": c, "rest": rest, "key": key, "opt": opt, "extra": extra}
    ''')
    head = base.replace('"v": "old"', '"v": "new"')
    out = wrap(base, head, "f", "ap_f")
    assert "def f(a, /, b, c=2, *rest, key, opt=None, **extra) -> dict:" in out
    for flag, version in ((False, "old"), (True, "new")):
        ns, _ = load(out, flag)
        got = ns["f"](1, 2, 3, 4, 5, key="k", z=9)
        assert got == {"v": version, "a": 1, "b": 2, "c": 3, "rest": (4, 5), "key": "k",
                       "opt": None, "extra": {"z": 9}}


def test_wrap_async_function():
    base = "async def fetch(x):\n    return x + 1\n"
    head = "async def fetch(x):\n    return x + 100\n"
    out = wrap(base, head, "fetch", "ap_fetch")
    assert "async def fetch(x):" in out and "return await _fetch__new(x)" in out
    off, _ = load(out, False)
    on, _ = load(out, True)
    assert asyncio.run(off["fetch"](1)) == 2
    assert asyncio.run(on["fetch"](1)) == 101


def test_wrap_multiline_def():
    base = "def f(\n    a,\n    b,\n):\n    return a + b\n"
    head = "def f(\n    a,\n    b,\n):\n    return a * b\n"
    out = wrap(base, head, "f", "ap_f")
    off, _ = load(out, False)
    on, _ = load(out, True)
    assert off["f"](2, 3) == 5 and on["f"](2, 3) == 6


# --- decorators / FastAPI --------------------------------------------------------

ROUTE_BASE = textwrap.dedent('''\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/price/{sku}")
    def price(sku: str, qty: int = 1):
        return {"sku": sku, "total": 10 * qty}
''')
ROUTE_HEAD = ROUTE_BASE.replace("10 * qty", "9 * qty")


def test_wrap_fastapi_route_registers_once_and_switches():
    out = wrap(ROUTE_BASE, ROUTE_HEAD, "price", "ap_price")
    # Decorator stays only on the dispatcher.
    assert out.count('@app.get("/price/{sku}")') == 1
    assert out.index('@app.get("/price/{sku}")') > out.index("def _price__new")
    # Import joins the import block, no blank line in between.
    assert out.startswith("from fastapi import FastAPI\n" + SDK_IMPORT + "\n")
    for flag, total in ((False, 30), (True, 27)):
        ns, _ = load(out, flag)
        routes = [r for r in ns["app"].routes if getattr(r, "path", "") == "/price/{sku}"]
        assert len(routes) == 1
        resp = TestClient(ns["app"]).get("/price/beans", params={"qty": 3})
        assert resp.status_code == 200
        assert resp.json() == {"sku": "beans", "total": total}
        # FastAPI still sees the real parameters (validation works).
        assert TestClient(ns["app"]).get("/price/beans", params={"qty": "x"}).status_code == 422


def test_wrap_with_multiple_decorators_and_comment():
    base = textwrap.dedent('''\
        import functools


        def twice(fn):
            @functools.wraps(fn)
            def inner(*a, **k):
                return fn(*a, **k) * 2
            return inner


        @twice
        # a comment between decorator and def
        @functools.lru_cache(maxsize=None)
        def f(x):
            return x
    ''')
    head = base.replace("def f(x):\n    return x\n", "def f(x):\n    return x + 1\n")
    out = wrap(base, head, "f", "ap_f")
    assert out.count("@twice") == 1 and out.count("@functools.lru_cache") == 1
    off, _ = load(out, False)
    on, _ = load(out, True)
    assert off["f"](5) == 10 and on["f"](5) == 12


# --- refusals --------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["checkout", "AP_checkout", "ap-checkout", "ap_", "", None, "ap_" + "x" * 61])
def test_refuses_bad_flag_names(flag):
    with pytest.raises(TransformError, match="flag name"):
        wrap(BASE, HEAD, "calculate_total", flag)


def test_refuses_new_file():
    with pytest.raises(TransformError, match="file is new"):
        wrap(None, HEAD, "calculate_total", FLAG)


def test_refuses_function_new_in_pr():
    head = HEAD + "\n\ndef brand_new():\n    return 1\n"
    with pytest.raises(TransformError, match="new in this PR"):
        wrap(BASE, head, "brand_new", FLAG)


def test_refuses_function_removed_in_pr():
    head = BASE.replace("def shipping(total):\n    return 0 if total > 50 else 5\n", "")
    with pytest.raises(TransformError, match="isn't defined in the PR"):
        wrap(BASE, head, "shipping", FLAG)


def test_refuses_unchanged_function():
    with pytest.raises(TransformError, match="didn't change"):
        wrap(BASE, HEAD, "shipping", FLAG)


def test_refuses_decorator_only_change():
    with pytest.raises(TransformError, match="only `price`'s decorators changed"):
        wrap(ROUTE_BASE, ROUTE_BASE.replace('"/price/{sku}"', '"/cost/{sku}"'), "price", "ap_price")


def test_refuses_parameter_change():
    head = HEAD.replace("def calculate_total(items):", "def calculate_total(items, coupon=None):")
    with pytest.raises(TransformError, match="parameters changed"):
        wrap(BASE, head, "calculate_total", FLAG)


def test_refuses_sync_async_change():
    with pytest.raises(TransformError, match="async def"):
        wrap("def f():\n    return 1\n", "async def f():\n    return 2\n", "f", "ap_f")


def test_refuses_async_generator():
    base = "async def f():\n    yield 1\n"
    head = "async def f():\n    yield 2\n"
    with pytest.raises(TransformError, match="async generator"):
        wrap(base, head, "f", "ap_f")


def test_sync_generator_is_fine():
    out = wrap("def f():\n    yield 1\n", "def f():\n    yield 2\n", "f", "ap_f")
    off, _ = load(out, False)
    on, _ = load(out, True)
    assert list(off["f"]()) == [1] and list(on["f"]()) == [2]


def test_refuses_method():
    base = "class Cart:\n    def total(self):\n        return 1\n"
    head = "class Cart:\n    def total(self):\n        return 2\n"
    with pytest.raises(TransformError, match="isn't a top-level function"):
        wrap(base, head, "total", "ap_total")


def test_refuses_already_wrapped():
    once = wrap(BASE, HEAD, "calculate_total", FLAG)
    again = once.replace("total *= 0.9", "total *= 0.8")
    with pytest.raises(TransformError, match="already wrapped"):
        wrap(once, again, "calculate_total", FLAG)


def test_refuses_duplicate_definitions():
    head = HEAD + "\n\ndef calculate_total(items):\n    return 0\n"
    with pytest.raises(TransformError, match="more than once"):
        wrap(BASE, head, "calculate_total", FLAG)


def test_refuses_invalid_python():
    with pytest.raises(TransformError, match="isn't valid Python"):
        wrap(BASE, "def calculate_total(items:\n", "calculate_total", FLAG)


def test_windows_line_endings_are_kept():
    base, head = BASE.replace("\n", "\r\n"), HEAD.replace("\n", "\r\n")
    out = wrap(base, head, "calculate_total", FLAG)
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    assert out.replace("\r\n", "\n") == wrap(BASE, HEAD, "calculate_total", FLAG)


# --- import placement --------------------------------------------------------------

def test_import_at_top_when_no_docstring_or_imports():
    out = wrap("def f():\n    return 1\n", "def f():\n    return 2\n", "f", "ap_f")
    assert out.startswith(SDK_IMPORT + "\n\n")


def test_import_after_future_and_other_imports():
    base = '"""Doc."""\nfrom __future__ import annotations\n\nimport os\n\n\ndef f():\n    return 1\n'
    head = base.replace("return 1", "return 2")
    out = wrap(base, head, "f", "ap_f")
    lines = out.split("\n")
    assert lines.index(SDK_IMPORT) == lines.index("import os") + 1
    compile(out, "<wrapped>", "exec")  # __future__ import must stay first


def test_import_not_duplicated_when_present():
    base = f"{SDK_IMPORT}\n\n\ndef f():\n    return 1\n"
    head = base.replace("return 1", "return 2")
    out = wrap(base, head, "f", "ap_f")
    assert out.count(SDK_IMPORT) == 1


# --- changed_functions -------------------------------------------------------------

def test_changed_functions_lists_only_real_changes():
    head = HEAD.replace("return 0 if total > 50 else 5", "return 0 if total > 40 else 5")
    head += "\n\ndef added():\n    return 1\n"
    assert changed_functions(BASE, head) == ["calculate_total", "shipping"]
    assert changed_functions(BASE, BASE) == []


def test_changed_functions_ignores_formatting_and_comments():
    head = BASE.replace("total = 0.0", "total = 0.0  # start at zero").replace(
        "    return 0 if total > 50 else 5", "    return (0 if total > 50 else 5)")
    assert changed_functions(BASE, head) == []