"""Auto-download the swissTLM3D vector dataset from the swisstopo STAC API.

This is the vector half of the area auto-download (the DEM half is
:mod:`topovert.dem_download`; full per-area *tiled* STAC selection remains
``topovert-y8s``). It removes the last manual step from a full build: swissTLM3D
is published as open data under a plain, unauthenticated HTTPS URL, one
whole-country **FileGDB zip per release**, discoverable through the STAC
collection ``ch.swisstopo.swisstlm3d``.

Two things to know before using it:

* **It is a big download.** The current release is ~2.9 GB compressed and
  expands to roughly 8-10 GB of ``.gdb``. Both the zip and the extracted
  GeoDatabase are cached under ``<cache>/swisstlm3d/`` and reused across runs.
* **It is national, not per-area.** swisstopo publishes swissTLM3D as one file
  covering all of Switzerland; there is no per-tile asset to pick from. Clipping
  to a smaller extent happens downstream (``topovert-05p``), not here.

The release defaults to the newest one the STAC collection lists. Pin a specific
release (e.g. ``swisstlm3d_2025-03``) for a reproducible build.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import TopovertError
from .jars import cache_dir

log = logging.getLogger(__name__)

# swisstopo's STAC API. Override for a mirror / air-gapped setup, mirroring the
# TOPOVERT_COPERNICUS_URL escape hatch in dem_download.
STAC_COLLECTION_URL = os.environ.get(
    "TOPOVERT_TLM_STAC_URL",
    "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swisstlm3d",
)

# The FileGDB zip is the asset we want: ogr2ogr reads a .gdb directly (the
# shapefile zip loses the layer/attribute structure vector.py relies on, and the
# INTERLIS .xtf needs a different driver).
_ASSET_SUFFIX = ".gdb.zip"

_RETRIES = 4
_BACKOFF_BASE_S = 2
_STAC_PAGE_LIMIT = 100


def _get_json(url: str) -> dict:
    """GET ``url`` and parse JSON, retrying transient failures."""
    last = ""
    for attempt in range(1, _RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
            if attempt < _RETRIES:
                delay = _BACKOFF_BASE_S * 2 ** (attempt - 1)
                log.warning(
                    "STAC request %s failed (%s); retrying in %ds [%d/%d]",
                    url, last, delay, attempt, _RETRIES,
                )
                time.sleep(delay)
    raise TopovertError(
        f"failed to query the swisstopo STAC API at {url}: {last}\n"
        "Set TOPOVERT_TLM_STAC_URL to a reachable mirror if needed."
    )


def _asset_href(feature: dict) -> str | None:
    """The ``.gdb.zip`` download URL of a STAC item, if it publishes one."""
    for name, asset in (feature.get("assets") or {}).items():
        if name.endswith(_ASSET_SUFFIX):
            href = asset.get("href")
            if href:
                return href
    return None


def list_releases() -> list[tuple[str, str]]:
    """Every swissTLM3D release as ``(release_id, gdb_zip_url)``, oldest first.

    Walks the STAC collection's paginated ``items`` feed and keeps the items that
    actually publish a FileGDB asset.
    """
    url = f"{STAC_COLLECTION_URL}/items?limit={_STAC_PAGE_LIMIT}"
    out: list[tuple[str, str]] = []
    seen_pages = 0
    while url:
        doc = _get_json(url)
        for feat in doc.get("features", []):
            href = _asset_href(feat)
            if href and feat.get("id"):
                out.append((feat["id"], href))
        url = next(
            (l.get("href") for l in doc.get("links", []) if l.get("rel") == "next"),
            None,
        )
        seen_pages += 1
        if seen_pages > 50:  # pagination guard; the collection has a handful
            break
    if not out:
        raise TopovertError(
            "the swisstopo STAC collection listed no swissTLM3D FileGDB asset; "
            "the dataset layout may have changed"
        )
    return out


def resolve_release(release: str = "latest") -> tuple[str, str]:
    """Resolve ``release`` to ``(release_id, gdb_zip_url)``.

    ``"latest"`` picks the newest release the collection lists; anything else is
    matched against the release ids (exact, or as a substring so ``2025-03``
    finds ``swisstlm3d_2025-03``).
    """
    releases = list_releases()
    if release.strip().lower() in ("", "latest"):
        return releases[-1]

    key = release.strip()
    for rid, href in releases:
        if rid == key:
            return (rid, href)
    matches = [(rid, href) for rid, href in releases if key in rid]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(rid for rid, _ in releases)
    if not matches:
        raise TopovertError(
            f"unknown swissTLM3D release {release!r}; available: {known}"
        )
    raise TopovertError(
        f"ambiguous swissTLM3D release {release!r}; matches: "
        + ", ".join(rid for rid, _ in matches)
    )


def _fetch(url: str, dst: Path) -> None:
    """Download ``url`` to ``dst`` atomically, retrying transient failures.

    The partial download lands in a ``.part`` sidecar and is renamed into place
    only on a complete transfer, so an interrupted multi-GB pull never leaves a
    truncated archive that a later run would treat as cached.
    """
    tmp = dst.with_suffix(dst.suffix + ".part")
    last = ""
    for attempt in range(1, _RETRIES + 1):
        try:
            urllib.request.urlretrieve(url, tmp)
        except urllib.error.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            last = f"HTTP {exc.code}"
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            last = str(exc)
        else:
            tmp.replace(dst)
            return
        if attempt < _RETRIES:
            delay = _BACKOFF_BASE_S * 2 ** (attempt - 1)
            log.warning(
                "download of %s failed (%s); retrying in %ds [%d/%d]",
                url, last, delay, attempt, _RETRIES,
            )
            time.sleep(delay)
    raise TopovertError(f"failed to download {url} after {_RETRIES} attempts: {last}")


def _find_gdb(root: Path) -> Path:
    """The ``.gdb`` directory inside an extracted swissTLM3D release."""
    if root.is_dir() and root.suffix.lower() == ".gdb":
        return root
    candidates = sorted(p for p in root.rglob("*.gdb") if p.is_dir())
    if not candidates:
        raise TopovertError(
            f"no .gdb directory found in the extracted swissTLM3D release at {root}"
        )
    if len(candidates) > 1:
        log.warning(
            "%d .gdb directories in %s; using %s", len(candidates), root, candidates[0]
        )
    return candidates[0]


def download_release(
    release: str = "latest",
    dst_dir: Path | None = None,
    *,
    keep_archive: bool = True,
) -> Path:
    """Download + extract a swissTLM3D release; return the ``.gdb`` path.

    The result is what ``--tlm`` expects, so the pipeline can treat it exactly
    like a local GeoDatabase. Both the zip and the extracted GeoDatabase are
    cached under ``dst_dir`` (default ``<cache>/swisstlm3d``) and reused, since
    this is a ~2.9 GB download that expands to several times that.

    ``keep_archive=False`` deletes the zip once extracted — worth it in CI, where
    disk is tighter than bandwidth.
    """
    rid, url = resolve_release(release)
    dst = dst_dir or (cache_dir() / "swisstlm3d")
    dst.mkdir(parents=True, exist_ok=True)

    extracted = dst / rid
    if extracted.is_dir():
        gdb = _find_gdb(extracted)
        log.info("swissTLM3D %s already cached at %s", rid, gdb)
        return gdb

    archive = dst / f"{rid}{_ASSET_SUFFIX}"
    if archive.exists() and archive.stat().st_size > 0:
        log.info("using cached swissTLM3D archive %s", archive)
    else:
        log.info("downloading swissTLM3D %s (~2.9 GB) from %s ...", rid, url)
        _fetch(url, archive)

    # Extract to a .part directory and rename, so an interrupted unzip is never
    # mistaken for a complete cached release on the next run.
    staging = dst / f"{rid}.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    log.info("extracting %s ...", archive.name)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        # A corrupt cached archive should not wedge every later run.
        archive.unlink(missing_ok=True)
        raise TopovertError(
            f"could not extract the swissTLM3D archive for {rid}: {exc}. "
            "The cached download was removed; re-run to fetch it again."
        ) from exc
    staging.replace(extracted)

    if not keep_archive:
        archive.unlink(missing_ok=True)

    gdb = _find_gdb(extracted)
    log.info("swissTLM3D %s ready at %s", rid, gdb)
    return gdb
