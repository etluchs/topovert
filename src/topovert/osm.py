"""Synthesize the minimal OSM file mkgmap needs alongside ``--dem``.

mkgmap embeds the DEM as a subfile of a *map*, so it still needs some OSM input
to build that map. For the v1 hillshading-only slice we have no vector features
yet, so we emit a tiny valid ``.osm``: the data bounds, a closed way around them
(tagged with an innocuous area type), and a single named locality POI at the
centre so the produced map is unambiguously non-empty.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE = """\
<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='topovert'>
  <bounds minlat='{min_lat:.7f}' minlon='{min_lon:.7f}' maxlat='{max_lat:.7f}' maxlon='{max_lon:.7f}'/>
  <node id='-1' lat='{min_lat:.7f}' lon='{min_lon:.7f}'/>
  <node id='-2' lat='{min_lat:.7f}' lon='{max_lon:.7f}'/>
  <node id='-3' lat='{max_lat:.7f}' lon='{max_lon:.7f}'/>
  <node id='-4' lat='{max_lat:.7f}' lon='{min_lon:.7f}'/>
  <node id='-5' lat='{ctr_lat:.7f}' lon='{ctr_lon:.7f}'>
    <tag k='place' v='locality'/>
    <tag k='name' v='{name}'/>
  </node>
  <way id='-1'>
    <nd ref='-1'/>
    <nd ref='-2'/>
    <nd ref='-3'/>
    <nd ref='-4'/>
    <nd ref='-1'/>
    <tag k='natural' v='heath'/>
  </way>
</osm>
"""


def write_bounds_osm(
    bounds: tuple[float, float, float, float], dst: Path, name: str = "topovert"
) -> Path:
    """Write the minimal bounds ``.osm`` for ``bounds`` (min_lon,min_lat,max_lon,max_lat)."""
    min_lon, min_lat, max_lon, max_lat = bounds
    dst.write_text(
        _TEMPLATE.format(
            min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
            ctr_lon=(min_lon + max_lon) / 2, ctr_lat=(min_lat + max_lat) / 2,
            name=name,
        ),
        encoding="utf-8",
    )
    return dst
