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


def _extract_distribution(zip_path: Path, dst_dir: Path, jar_filename: str) -> Path:
    """Extract the whole distribution into ``dst_dir``, returning the wanted jar.

    mkgmap.jar / splitter.jar have ``Class-Path: lib/...`` in their manifest, so
    the bundled dependency jars (osmpbf, protobuf, fastutil, ...) must sit beside
    them. The zip nests everything under a top ``<tool>-rXXXX/`` directory, which
    we strip so the layout becomes ``dst_dir/<jar>`` + ``dst_dir/lib/*.jar``.
    """
    jar_path: Path | None = None
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            rel = member.split("/", 1)[1] if "/" in member else member
            if not rel:
                continue
            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            if rel == jar_filename:
                jar_path = target
    if jar_path is None:
        raise TopovertError(f"{jar_filename} not found inside {zip_path}")
    return jar_path


def _ensure_jar(name: str, url: str, sha256: str | None, jar_filename: str) -> Path:
    """Return a path to ``jar_filename``, downloading + caching ``url`` if needed."""
    dist_dir = cache_dir() / name
    jar = dist_dir / jar_filename
    if jar.exists():
        log.debug("using cached %s: %s", jar_filename, jar)
        return jar

    dist_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s from %s ...", name, url)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{name}.zip"
        try:
            urllib.request.urlretrieve(url, zip_path)
        except OSError as exc:
            raise TopovertError(
                f"failed to download {name} from {url}: {exc}\n"
                f"Set the override env var to a reachable mirror if needed."
            ) from exc

        digest = _sha256(zip_path)
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

        jar = _extract_distribution(zip_path, dist_dir, jar_filename)

    log.info("cached %s at %s", name, jar)
    return jar


def ensure_mkgmap() -> Path:
    """Return a path to mkgmap.jar, downloading + caching it if necessary."""
    return _ensure_jar("mkgmap", MKGMAP_URL, MKGMAP_SHA256, "mkgmap.jar")


def ensure_splitter() -> Path:
    """Return a path to splitter.jar, downloading + caching it if necessary."""
    return _ensure_jar("splitter", SPLITTER_URL, SPLITTER_SHA256, "splitter.jar")
