"""Tests for the swissTLM3D STAC auto-download (no network, no 2.9 GB pull).

The STAC queries and the fetch are monkeypatched, so these exercise asset
selection, release resolution, caching/skip logic, extraction and the error
paths without touching data.geo.admin.ch.
"""

import zipfile

import pytest

from topovert import TopovertError, tlm_download


def _item(rid, *, gdb=True):
    assets = {
        f"{rid}_2056_5728.shp.zip": {"href": f"https://x/{rid}.shp.zip"},
        f"{rid}_2056_5728.xtf.zip": {"href": f"https://x/{rid}.xtf.zip"},
    }
    if gdb:
        assets[f"{rid}_2056_5728.gdb.zip"] = {"href": f"https://x/{rid}.gdb.zip"}
    return {"id": rid, "assets": assets}


def test_asset_href_prefers_the_filegdb_zip():
    """ogr2ogr reads a .gdb; the shapefile/INTERLIS assets must not be picked."""
    href = tlm_download._asset_href(_item("swisstlm3d_2025-03"))
    assert href == "https://x/swisstlm3d_2025-03.gdb.zip"


def test_asset_href_none_when_no_filegdb_published():
    assert tlm_download._asset_href(_item("swisstlm3d_2025-03", gdb=False)) is None


def test_list_releases_follows_stac_pagination(monkeypatch):
    pages = {
        "start": {
            "features": [_item("swisstlm3d_2024-03")],
            "links": [{"rel": "next", "href": "page2"}],
        },
        "page2": {
            "features": [_item("swisstlm3d_2025-03"), _item("nope", gdb=False)],
            "links": [],
        },
    }
    calls = []

    def fake_get(url):
        calls.append(url)
        return pages["page2" if url == "page2" else "start"]

    monkeypatch.setattr(tlm_download, "_get_json", fake_get)
    got = tlm_download.list_releases()
    assert [r for r, _ in got] == ["swisstlm3d_2024-03", "swisstlm3d_2025-03"]
    assert len(calls) == 2  # followed the `next` link
    # the item without a FileGDB asset is dropped
    assert all("nope" not in r for r, _ in got)


def test_list_releases_raises_when_no_filegdb_anywhere(monkeypatch):
    monkeypatch.setattr(
        tlm_download, "_get_json",
        lambda url: {"features": [_item("x", gdb=False)], "links": []},
    )
    with pytest.raises(TopovertError, match="no swissTLM3D FileGDB"):
        tlm_download.list_releases()


@pytest.fixture
def releases(monkeypatch):
    rels = [
        ("swisstlm3d_2024-03", "https://x/a.gdb.zip"),
        ("swisstlm3d_2025-03", "https://x/b.gdb.zip"),
        ("swisstlm3d_2026-02-24", "https://x/c.gdb.zip"),
    ]
    monkeypatch.setattr(tlm_download, "list_releases", lambda: rels)
    return rels


def test_resolve_release_latest_is_the_newest_listed(releases):
    assert tlm_download.resolve_release("latest")[0] == "swisstlm3d_2026-02-24"
    assert tlm_download.resolve_release("")[0] == "swisstlm3d_2026-02-24"


def test_resolve_release_exact_and_substring(releases):
    assert tlm_download.resolve_release("swisstlm3d_2025-03")[0] == "swisstlm3d_2025-03"
    assert tlm_download.resolve_release("2025-03")[0] == "swisstlm3d_2025-03"


def test_resolve_release_unknown_lists_what_is_available(releases):
    with pytest.raises(TopovertError, match="unknown swissTLM3D release"):
        tlm_download.resolve_release("1999-01")


def test_resolve_release_ambiguous_substring_is_an_error(releases):
    """A prefix matching several releases must not silently pick one."""
    with pytest.raises(TopovertError, match="ambiguous"):
        tlm_download.resolve_release("swisstlm3d_20")


def _make_zip(path, *, gdb_name="SWISSTLM3D.gdb"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{gdb_name}/gdb", "x")
        zf.writestr(f"{gdb_name}/a00000001.gdbtable", "x")


def test_download_release_extracts_and_returns_the_gdb(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tlm_download, "resolve_release",
        lambda r="latest": ("swisstlm3d_2025-03", "https://x/b.gdb.zip"),
    )
    monkeypatch.setattr(
        tlm_download, "_fetch", lambda url, dst: _make_zip(dst)
    )
    gdb = tlm_download.download_release("latest", tmp_path)
    assert gdb.is_dir() and gdb.name == "SWISSTLM3D.gdb"
    # extracted under the release id, and no .part staging left behind
    assert gdb.parent.name == "swisstlm3d_2025-03"
    assert not list(tmp_path.glob("*.part"))


def test_download_release_reuses_the_extracted_cache(tmp_path, monkeypatch):
    """A second call must not re-download the multi-GB archive."""
    monkeypatch.setattr(
        tlm_download, "resolve_release",
        lambda r="latest": ("swisstlm3d_2025-03", "https://x/b.gdb.zip"),
    )
    fetches = []

    def counting_fetch(url, dst):
        fetches.append(url)
        _make_zip(dst)

    monkeypatch.setattr(tlm_download, "_fetch", counting_fetch)
    first = tlm_download.download_release("latest", tmp_path)
    second = tlm_download.download_release("latest", tmp_path)
    assert first == second
    assert len(fetches) == 1  # cached on the second call


def test_download_release_can_drop_the_archive_to_save_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tlm_download, "resolve_release",
        lambda r="latest": ("swisstlm3d_2025-03", "https://x/b.gdb.zip"),
    )
    monkeypatch.setattr(tlm_download, "_fetch", lambda url, dst: _make_zip(dst))
    tlm_download.download_release("latest", tmp_path, keep_archive=False)
    assert not list(tmp_path.glob("*.gdb.zip"))


def test_corrupt_archive_is_removed_so_a_rerun_can_recover(tmp_path, monkeypatch):
    """A truncated cached download must not wedge every later run."""
    monkeypatch.setattr(
        tlm_download, "resolve_release",
        lambda r="latest": ("swisstlm3d_2025-03", "https://x/b.gdb.zip"),
    )
    monkeypatch.setattr(
        tlm_download, "_fetch",
        lambda url, dst: dst.write_bytes(b"not a zip"),
    )
    with pytest.raises(TopovertError, match="could not extract"):
        tlm_download.download_release("latest", tmp_path)
    assert not list(tmp_path.glob("*.gdb.zip"))  # poisoned archive cleared
    assert not list(tmp_path.glob("*.part"))     # staging cleaned up


def test_find_gdb_errors_when_the_archive_has_none(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(TopovertError, match="no .gdb directory"):
        tlm_download._find_gdb(tmp_path / "empty")
