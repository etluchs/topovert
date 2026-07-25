Prebuilt Garmin maps for **Switzerland**, so you don't need to install GDAL, Java
or run the toolchain yourself. Download an `.img`, drop it on your device, done.

## Which file?

| File | Contents | Size | Best for |
|---|---|---|---|
| `topovert-switzerland-contours.img` | Elevation contour lines (20 m, every 5th a bold index line) | small | **Devices** (Fenix, Edge, …) |
| `topovert-switzerland-contours-hillshade.img` | The same contours **plus** an embedded country-wide DEM for shaded relief | large | **Desktop** (QMapShack / BaseCamp) |

Both are built from the open **Copernicus GLO-30** (~30 m) DEM and are
**transparent overlays**: they draw on top of whatever base map you already have,
rather than replacing it.

## ⚠️ Before you load the hillshade map on a device

The hillshade file embeds a whole-country DEM and is split into many map tiles.
A comparable whole-Switzerland map with an embedded DEM **crashed a Garmin Edge
1040 badly enough to need a full device reset** (see issue `topovert-qg3`; the
cause is not yet isolated). We have not cleared this file for on-device use.

- **Check it on the desktop first** (QMapShack or BaseCamp), which is safe.
- For a device, prefer `topovert-switzerland-contours.img` — no embedded DEM,
  far smaller, and the configuration we consider low-risk.
- Loading the hillshade map onto a device is at your own risk.

## Install

Copy the `.img` into the `Garmin/` folder of your device's storage, then restart
it. The map appears under the device's map/activity settings, where it can be
enabled or disabled. To remove it, delete the file.

## What's *not* in these maps

No roads, paths, buildings, land cover or POIs. Those come from **swissTLM3D**,
a large manual download from swisstopo that CI can't fetch, so these prebuilt
files are DEM-derived only (contours ± relief). To build a map *with* vector
features, run topovert yourself and pass `--tlm` — see the README.

## Verify your download

Each `.img` ships with a `.sha256` file:

```bash
sha256sum -c topovert-switzerland-contours.img.sha256
```
