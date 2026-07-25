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


_RELEASE_WF = _ROOT / ".github" / "workflows" / "release-maps.yml"
_RELEASE_NOTES = _ROOT / ".github" / "release-notes-maps.md"


def test_release_asset_names_match_the_docs():
    """The .img names the release workflow builds must be the ones we advertise.

    Renaming an asset without updating the README/release notes would point every
    download link at a file that doesn't exist.
    """
    wf = _RELEASE_WF.read_text(encoding="utf-8")
    notes = _RELEASE_NOTES.read_text(encoding="utf-8")
    slugs = set(re.findall(r"slug:\s*([\w-]+)", wf))
    assert slugs, "no matrix slugs found in the release workflow"
    for slug in slugs:
        asset = f"topovert-switzerland-{slug}.img"
        assert asset in _README, f"README doesn't mention the {asset} download"
        assert asset in notes, f"release notes don't mention {asset}"


def test_release_workflow_is_not_triggered_by_ordinary_pushes():
    """A whole-country build is hours long: it must be tag/dispatch only.

    A `branches:` push trigger here would kick off a multi-hour country-scale
    build (and a release upload) on every commit.
    """
    wf = _RELEASE_WF.read_text(encoding="utf-8")
    triggers = wf.split("jobs:", 1)[0]
    assert "workflow_dispatch:" in triggers
    assert "branches:" not in triggers, (
        "release-maps.yml must not run on branch pushes; keep it tag/dispatch only"
    )


def test_hillshade_download_carries_the_device_warning():
    """topovert-qg3: the whole-CH DEM map is not cleared for on-device use.

    Until that bug is resolved, both the README and the release notes must warn
    people before they load the hillshade artifact onto a device.
    """
    for name, text in (
        ("README.md", _README),
        ("release-notes-maps.md", _RELEASE_NOTES.read_text(encoding="utf-8")),
    ):
        assert "topovert-qg3" in text, f"{name} lost the qg3 reference"
        # whitespace-tolerant: the warning wraps across lines in both files
        assert re.search(r"(?i)edge\s+1040", text), f"{name} lost the crash warning"


def test_tlm_input_is_a_geodatabase_not_geopackage():
    """swissTLM3D is a .gdb GeoDatabase; the .gpkg label was a long-lived bug."""
    for name, text in (("README.md", _README), ("cli.py", _CLI_SRC)):
        assert "GeoPackage" not in text and ".gpkg" not in text, (
            f"{name} calls the swissTLM3D input a GeoPackage/.gpkg; it is a "
            "GeoDatabase (.gdb)."
        )
