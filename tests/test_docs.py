"""Guard against doc drift: README / cli.py help must stay true to the code.

These assert a few concrete invariants that have actually drifted before (CLI
flags renamed without a README update; swissTLM3D called a ``.gpkg`` when it is
a ``.gdb``; the Python pin getting out of step). They need no GDAL/Java and run
in the normal ``uv run pytest`` gate, so CI fails fast on doc rot.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import tomllib

from topovert import cli

_ROOT = Path(__file__).resolve().parents[1]
_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_CLI_SRC = (_ROOT / "src" / "topovert" / "cli.py").read_text(encoding="utf-8")


def _cli_option_strings() -> set[str]:
    """Every long/short flag the `topovert` parser accepts (top level + build)."""
    parser = cli._build_parser()
    flags: set[str] = set()
    stack: list[argparse.ArgumentParser] = [parser]
    while stack:
        p = stack.pop()
        for action in p._actions:
            flags.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    return flags


def test_readme_flags_all_exist_in_cli():
    """Any --flag mentioned in the README must be a real CLI option."""
    known = _cli_option_strings()
    referenced = set(re.findall(r"--[a-zA-Z][\w-]+", _README))
    unknown = referenced - known
    assert not unknown, (
        f"README references CLI flags that don't exist: {sorted(unknown)}. "
        "Update the README (or cli.py) so they match."
    )


def test_readme_python_pin_matches_pyproject():
    """The Python version named in the README must match requires-python."""
    meta = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pin = meta["project"]["requires-python"]  # e.g. ">=3.11"
    version = re.search(r"\d+\.\d+", pin).group(0)
    assert version in _README, (
        f"pyproject requires-python is {pin!r} but the README doesn't mention "
        f"{version}; keep the Requirements section in sync."
    )


def test_every_subcommand_is_documented_in_readme():
    """Each CLI subcommand (build/render-contours/legend/…) must appear in the README."""
    parser = cli._build_parser()
    subactions = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    names = {name for a in subactions for name in a.choices}
    assert names, "no subcommands found"
    missing = {n for n in names if f"topovert {n}" not in _README}
    assert not missing, (
        f"README doesn't document these subcommands: {sorted(missing)}. "
        "Add a usage line for them."
    )


def test_tlm_input_is_a_geodatabase_not_geopackage():
    """swissTLM3D is a .gdb GeoDatabase; the .gpkg label was a long-lived bug."""
    for name, text in (("README.md", _README), ("cli.py", _CLI_SRC)):
        assert "GeoPackage" not in text and ".gpkg" not in text, (
            f"{name} calls the swissTLM3D input a GeoPackage/.gpkg; it is a "
            "GeoDatabase (.gdb)."
        )
