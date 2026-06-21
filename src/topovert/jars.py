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

# Pinned mkgmap release. mkgmap.org.uk prunes old revisions, so when bumping this
# you MUST update both the URL and the SHA-256 (download the zip and run
# `sha256sum`). The SHA is enforced; a mismatch aborts the download.
MKGMAP_URL = os.environ.get(
    "TOPOVERT_MKGMAP_URL", "https://www.mkgmap.org.uk/download/mkgmap-r4924.zip"
)
MKGMAP_SHA256: str | None = (
    "b2170799b61a95d4fc258e8e4fb4e21396809e0390789178f94c77109f8e0d84"
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


def _extract_distribution(zip_path: Path, dst_dir: Path) -> Path:
    """Extract the whole mkgmap distribution into ``dst_dir``, returning the jar.

    mkgmap.jar's manifest has ``Class-Path: lib/...`` relative to the jar, so the
    bundled dependency jars (osmpbf, protobuf, fastutil) must sit alongside it.
    The zip nests everything under a top ``mkgmap-rXXXX/`` directory, which we
    strip so the layout becomes ``dst_dir/mkgmap.jar`` + ``dst_dir/lib/*.jar``.
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
            if rel == "mkgmap.jar":
                jar_path = target
    if jar_path is None:
        raise TopovertError(f"mkgmap.jar not found inside {MKGMAP_URL}")
    return jar_path


def ensure_mkgmap() -> Path:
    """Return a path to mkgmap.jar, downloading + caching it if necessary."""
    dist_dir = cache_dir() / "mkgmap"
    jar = dist_dir / "mkgmap.jar"
    if jar.exists():
        log.debug("using cached mkgmap.jar: %s", jar)
        return jar

    dist_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading mkgmap from %s ...", MKGMAP_URL)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "mkgmap.zip"
        try:
            urllib.request.urlretrieve(MKGMAP_URL, zip_path)
        except OSError as exc:
            raise TopovertError(
                f"failed to download mkgmap from {MKGMAP_URL}: {exc}\n"
                "Set TOPOVERT_MKGMAP_URL to a reachable mirror if needed."
            ) from exc

        digest = _sha256(zip_path)
        if MKGMAP_SHA256 is None:
            log.warning(
                "mkgmap SHA-256 is not pinned; downloaded digest is %s "
                "(pin it in jars.MKGMAP_SHA256 to enforce integrity)",
                digest,
            )
        elif digest != MKGMAP_SHA256:
            raise TopovertError(
                f"mkgmap download digest mismatch:\n  expected {MKGMAP_SHA256}\n"
                f"  got      {digest}"
            )

        jar = _extract_distribution(zip_path, dist_dir)

    log.info("cached mkgmap at %s", jar)
    return jar
