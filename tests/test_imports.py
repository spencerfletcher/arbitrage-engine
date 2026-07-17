"""Every module under bot/ must import. The suite's cheapest real guard.

WHY THIS EXISTS: nothing else imports bot/main.py, and a green suite said nothing about
module-level breakage anywhere. Two edits on 2026-07-15 shipped code that passed all ~750 tests and
crashed on import:
  - a 4-vs-8 space indent in main.py's banner  -> IndentationError
  - a config edit that deleted LOG_LEVEL       -> AttributeError at import, from a DIFFERENT module

`compileall` would catch the first and NOT the second: deleting LOG_LEVEL is valid Python that only
fails when another module reads it. Only an actual import catches that class.

Import-only, no instantiation — clients/feeds do their work in __init__/connect, not at module
scope. 43 modules in ~0.7s.
"""
import importlib
import pkgutil

import pytest

import bot

_MODULES = sorted(m.name for m in pkgutil.walk_packages(bot.__path__, prefix="bot."))


def test_the_walk_found_the_package():
    """Guard the guard: if walk_packages returns nothing, every test below vacuously passes."""
    assert len(_MODULES) > 30, f"expected the whole package, walked only {len(_MODULES)}"
    assert "bot.main" in _MODULES, "bot.main must be covered — it is the module nothing else imports"


@pytest.mark.parametrize("name", _MODULES)
def test_module_imports(name):
    importlib.import_module(name)
