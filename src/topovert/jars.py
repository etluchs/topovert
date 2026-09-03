"""Locate Java and obtain a cached mkgmap.jar (auto-download on first use).

mkgmap is distributed as a versioned zip (``mkgmap-rXXXX.zip``) containing
``mkgmap.jar``. We download a pinned release once into a per-user cache and reuse
it thereafter. The expected SHA-256 SHOULD be pinned in ``MKGMAP_SHA256``; if it
is ``None`` we log the computed digest (so it can be pinned) and proceed without
hard verification. The download URL can be overridden with ``TOPOVERT_MKGMAP_URL``
for air-gapped/mirror setups.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import TopovertError

log = logging.getLogger(__name__)

# Pinned mkgmap/splitter releases. mkgmap.org.uk prunes old revisions, so when
# bumping either you MUST update both the URL and the SHA-256 (download the zip
# and run `sha256sum`). The SHA is enforced; a mismatch aborts the download.
MKGMAP_URL = os.environ.get(
    "TOPOVERT_MKGMAP_URL", "https://www.mkgmap.org.uk/download/mkgmap-r4924.zip"
)
MKGMAP_SHA256: str | None = (
    "b2170799b61a95d4fc258e8e4fb4e21396809e0390789178f94c77109f8e0d84"
)

# splitter tiles huge OSM extents into mkgmap-sized pieces (needed for large
# swissTLM3D areas). Same `splitter-rXXXX/{splitter.jar,lib/}` zip layout.
SPLITTER_URL = os.environ.get(
    "TOPOVERT_SPLITTER_URL", "https://www.mkgmap.org.uk/download/splitter-r654.zip"
)
SPLITTER_SHA256: str | None = (
    "87b0ca0e827bef556341f66ec8dd7f48eea2a479d1ff3659caa5d6f0e9a9b68c"
)

# --- Wahoo (mapsforge) toolchain --------------------------------------------
# osmosis runs the mapsforge map-writer plugin that produces the .map tiles.
# Pinned to 0.48.3 deliberately: 0.49.x is compiled for **Java 17** bytecode,
# which would raise this project's Java floor (>= 1.8, set by mkgmap) for Wahoo
# builds only. 0.48.3 is Java 8 and lays its jars out under `lib/default/`.
OSMOSIS_VERSION = "0.48.3"
OSMOSIS_URL = os.environ.get(
    "TOPOVERT_OSMOSIS_URL",
    f"https://github.com/openstreetmap/osmosis/releases/download/{OSMOSIS_VERSION}/"
    f"osmosis-{OSMOSIS_VERSION}.zip",
)
OSMOSIS_SHA256: str | None = (
    "b4e9776e99f5446191cf572df8d34f177d0a0d53e27d3cf306f9f923595731a6"
)
# The map-writer is a plain "jar with dependencies" from Maven Central (not a
# zip). Bumping it means updating both the URL and the SHA-256, and checking the
# new jar is still Java 8 bytecode (`javap -v`, major version 52).
MAPWRITER_VERSION = "0.21.0"
MAPWRITER_URL = os.environ.get(
    "TOPOVERT_MAPWRITER_URL",
    "https://repo1.maven.org/maven2/org/mapsforge/mapsforge-map-writer/"
    f"{MAPWRITER_VERSION}/mapsforge-map-writer-{MAPWRITER_VERSION}-jar-with-dependencies.jar",
)
MAPWRITER_SHA256: str | None = (
    "8e23b591fd7c61ca2fec5500c3b2406c5aba5cca2148a9e7fb59dabe0b46d687"
)


def cache_dir() -> Path:
    """Per-user cache directory for downloaded tools."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "topovert"


def find_java() -> str:
    """Path to a working ``java`` (>= 1.8), preferring ``$JAVA_HOME``."""
    candidates: list[str] = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(str(Path(java_home) / "bin" / "java"))
    found = shutil.which("java")
    if found:
        candidates.append(found)

    for java in candidates:
        try:
            proc = subprocess.run(
                [java, "-version"], capture_output=True, text=True
            )
        except (OSError, ValueError):
            continue
        if proc.returncode == 0:
            log.debug("using java: %s (%s)", java, proc.stderr.splitlines()[:1])
            return java

    raise TopovertError(
        "no working Java runtime found. mkgmap needs a JRE >= 1.8. Install one "
        "(e.g. `apt install default-jre`) or set JAVA_HOME."
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _common_top_dir(names: list[str]) -> str | None:
    """The single top-level directory all entries sit under, if there is one.

    mkgmap/splitter zips nest everything under ``<tool>-rXXXX/``; the osmosis
    zip has ``bin/``, ``lib/`` at the root. Stripping a *shared* top directory
    normalises both to the same on-disk layout.
    """
    tops = {n.split("/", 1)[0] for n in names if n.strip("/")}
    if len(tops) != 1:
        return None
    top = tops.pop()
    return top if all(n == top or n.startswith(top + "/") for n in names) else None


def _extract_distribution(zip_path: Path, dst_dir: Path) -> None:
    """Extract the whole distribution into ``dst_dir``.

    The *whole* thing, not just the wanted jar: mkgmap.jar / splitter.jar have
    ``Class-Path: lib/...`` in their manifest, so the bundled dependency jars
    (osmpbf, protobuf, fastutil, ...) must sit beside them, and osmosis finds its
    tasks by scanning every jar in ``lib/default/``.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        top = _common_top_dir(names)
        for member in names:
            if member.endswith("/"):
                continue
            rel = member[len(top) + 1:] if top else member
            if not rel:
                continue
            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _download_verified(name: str, url: str, sha256: str | None, dst: Path) -> None:
    """Download ``url`` to ``dst``, enforcing ``sha256`` when it is pinned."""
    log.info("downloading %s from %s ...", name, url)
    try:
        urllib.request.urlretrieve(url, dst)
    except OSError as exc:
        raise TopovertError(
            f"failed to download {name} from {url}: {exc}\n"
            f"Set the override env var to a reachable mirror if needed."
        ) from exc

    digest = _sha256(dst)
    if sha256 is None:
        log.warning(
            "%s SHA-256 is not pinned; downloaded digest is %s "
            "(pin it in jars.py to enforce integrity)",
            name, digest,
        )
    elif digest != sha256:
        raise TopovertError(
            f"{name} download digest mismatch:\n  expected {sha256}\n"
            f"  got      {digest}"
        )


def _ensure_dist(name: str, url: str, sha256: str | None, marker: str) -> Path:
    """Return the cached distribution directory, downloading ``url`` if needed.

    ``marker`` is a path inside it whose presence means the cache is populated.
    """
    dist_dir = cache_dir() / name
    if (dist_dir / marker).exists():
        log.debug("using cached %s: %s", name, dist_dir)
        return dist_dir

    dist_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{name}.zip"
        _download_verified(name, url, sha256, zip_path)
        _extract_distribution(zip_path, dist_dir)

    if not (dist_dir / marker).exists():
        raise TopovertError(f"{marker} not found inside the {name} download ({url})")
    log.info("cached %s at %s", name, dist_dir)
    return dist_dir


def _ensure_jar(name: str, url: str, sha256: str | None, jar_filename: str) -> Path:
    """Return a path to ``jar_filename`` inside the cached distribution."""
    return _ensure_dist(name, url, sha256, jar_filename) / jar_filename


def _ensure_bare_jar(name: str, url: str, sha256: str | None, filename: str) -> Path:
    """Return a path to a cached single ``.jar`` download (no enclosing zip)."""
    jar = cache_dir() / name / filename
    if jar.exists():
        log.debug("using cached %s: %s", filename, jar)
        return jar
    jar.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_jar = Path(tmp) / filename
        _download_verified(name, url, sha256, tmp_jar)
        shutil.move(str(tmp_jar), jar)
    log.info("cached %s at %s", name, jar)
    return jar


def ensure_mkgmap() -> Path:
    """Return a path to mkgmap.jar, downloading + caching it if necessary."""
    return _ensure_jar("mkgmap", MKGMAP_URL, MKGMAP_SHA256, "mkgmap.jar")


def ensure_splitter() -> Path:
    """Return a path to splitter.jar, downloading + caching it if necessary."""
    return _ensure_jar("splitter", SPLITTER_URL, SPLITTER_SHA256, "splitter.jar")


def ensure_osmosis() -> Path:
    """Return the osmosis distribution directory, downloading + caching it.

    The directory, not a jar: :func:`topovert.wahoo.classpath` needs every jar in
    its ``lib/default/`` (that is also how the map-writer plugin gets found).
    """
    return _ensure_dist(
        "osmosis", OSMOSIS_URL, OSMOSIS_SHA256,
        f"lib/default/osmosis-core-{OSMOSIS_VERSION}.jar",
    )


def ensure_mapwriter() -> Path:
    """Return the mapsforge map-writer plugin jar, downloading + caching it."""
    return _ensure_bare_jar(
        "mapsforge-map-writer", MAPWRITER_URL, MAPWRITER_SHA256,
        f"mapsforge-map-writer-{MAPWRITER_VERSION}-jar-with-dependencies.jar",
    )
