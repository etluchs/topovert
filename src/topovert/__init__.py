"""topovert — convert Swisstopo data into Garmin .IMG maps.

v1 covers a single vertical slice: a local directory of swissALTI3D GeoTIFF
elevation tiles (EPSG:2056) -> hill-shaded Garmin ``.IMG`` via GDAL + mkgmap.
"""

__version__ = "0.1.0"


class TopovertError(Exception):
    """Base class for expected, user-facing errors.

    The CLI catches these and prints the message without a traceback; any other
    exception is a bug and is allowed to propagate.
    """
