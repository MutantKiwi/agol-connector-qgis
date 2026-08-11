# AGOL Connector — QGIS Plugin

Connect QGIS to **ArcGIS Online** via the public REST API. No Esri libraries required.

![QGIS](https://img.shields.io/badge/QGIS-3.16%2B-brightgreen)
![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Licence](https://img.shields.io/badge/licence-GPLv3-orange)

---
## Background
The premise behind this plugin is to allow QGIS users to work with ArcGIS Online (AGOL). Many environments use both Esri products and Open Source software, or QGIS on an Apple computer, so this provides some form of connectivity. It is a work in progress.

## Features

### Browse & Load
- **ArcGIS Living Atlas** — search the full AGOL catalogue with type filters, access indicators and tag columns
- **Connection tabs** — one tab per signed-in connection, lazy-loaded folder tree with search
- **Feature Services** — paginated GeoJSON download with progress bar; point / line / polygon icons
- **Map Services** — XYZ tile loading with WMS fallback
- **Image Services** — capability detection (WMS / WCS / export); export rendered GeoTIFF for current map extent
- **Tile Layers & Vector Tiles** — XYZ URI direct load
- **Web Maps** — expand to list operational layers; load each individually

### Upload
- **Vector layers** — export to GeoJSON (or Shapefile ZIP for layers > 50 MB), upload as hosted Feature Service
- **Metadata fields** — Title, Summary, Description, Terms of Use, Acknowledgements, Tags
- **Sharing** — Private / Org / Public radio buttons; optional group sharing
- **Destination** — choose existing folder or create new; create new groups inline

### Data Source Manager
- Full **ArcGIS REST Server–style** tab in the QGIS Data Source Manager
- Connection dropdown, Connect / New / Edit / Remove / Refresh / Load / Save buttons
- Search bar with type filter
- **Web service link** (clickable) and **Coordinate Reference System** panels
- Add / Add with Filter / Close / Help button row
- Access column (Public / Org / Private) with padlock icon on restricted items

### Browse Services panel (dock)
- Top separator line, custom title bar
- **ArcGIS Living Atlas** tab — search only
- **Per-connection tabs** — folder tree with inline search, lazy expansion
- Editable URL field — correct wrong service URLs before loading
- Clickable item page link (`portal/home/item.html?id=…`)
- Detail panel with WMS ✔/✘, WCS ✔/✘, Export ✔/✘ for Image Services

### Authentication
- Username / password via `generateToken`
- OAuth2 / SSO collapsible section
- Encrypted storage via **QGIS Authentication Manager**
- Token expiry detection — silent re-auth, auto-retry
- Auto sign-in on startup (configurable)

### Other
- **Processing algorithm** — "Upload layer to ArcGIS Online" in Processing Toolbox; supports batch and Graphical Modeler
- **Layer → Export → Save to AGOL…** — right-click context menu integration
- **Layer Properties → Information** — populated from AGOL metadata (CRS, extent, links, keywords)
- **Progress bar** in QGIS message bar during feature downloads
- **Log file** — `<QGIS profile>/logs/agol_connector.log` with rotating 5 MB backup
- **Error tracking** — Bugsink (Sentry-compatible) for unhandled exceptions

---

## Installation

1. Download `agol_connector.zip` from [Releases](https://github.com/MutantKiwi/agol-connector-qgis/releases)
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**
3. Select the downloaded zip and click **Install Plugin**

---

## Quick Start

### Sign in
1. Open **AGOL Connector → Connections…**
2. Click **New** and enter your portal URL (default: `https://www.arcgis.com`)
3. Click **Connect** and enter your username and password

### Browse and load a layer
1. Click **AGOL Connector → Browse Services** to open the dock panel
2. Switch to your connection tab
3. Expand a folder and expand a service to see its layers
4. Double-click a layer or select it and click **Add to map**

### Upload a layer
1. Select a vector layer in the Layers panel
2. Right-click → **Export → Save to AGOL…**
3. Choose connection, fill in metadata, select sharing level, click **Upload**

### Search ArcGIS Living Atlas
1. In the Browse Services panel, switch to the **ArcGIS Living Atlas** tab
2. Type a keyword and press **Search**
3. Use the type filter dropdown to narrow results

---

## Settings

**AGOL Connector → Settings…**

| Setting | Default | Description |
|---|---|---|
| Sign in automatically | On | Re-authenticate saved connections on QGIS startup |
| Token expiry | 60 min | How long tokens stay valid (max 2 weeks) |
| Max features per layer | 10,000 | Feature download cap |
| Page size | 2,000 | Features per paginated request |
| Search result limit | 100 | Max search results (paginates in batches of 100) |
| Request timeout | 30 s | HTTP timeout per API call |
| Preferred output CRS | — | Force feature downloads to a specific CRS |
| Default tags | qgis,upload | Pre-filled in the upload dialog |

---

## Known Limitations

| Feature | Status |
|---|---|
| Map Services (tile CDN auth) | XYZ tiles work for public services; org-restricted `tiles.arcgis.com` services may not load — use WMS fallback |
| Image Services export | Export image for current extent works; WCS/WMTS depend on service configuration |
| Raster upload | Not yet implemented |
| Feature editing (applyEdits) | Not yet implemented |
| Load/Save connections file | UI present; XML import/export not yet implemented |

---

## Architecture

- **No Esri libraries** — all communication via `urllib` against the public AGOL REST API
- **PyQt5 / PyQt6 compatible** — enum paths resolved at import time via `compat.py`
- **QGIS 3.16+** compatible; tested on QGIS 3.44
- Token auth: `generateToken` (username/password) or OAuth2
- Pagination: objectIds batch strategy (Strategy A) with resultOffset fallback (Strategy B)
- Upload: multipart POST → addItem → publish; auto-switches to Shapefile ZIP for layers > 50 MB

---

## Licence

GNU General Public License v3.0 — see [LICENSE](LICENSE)

## Author

**mutant.kiwi** — [hello@mutant.kiwi](mailto:hello@mutant.kiwi)

## Bug Reports

[github.com/MutantKiwi/agol-connector-qgis/issues](https://github.com/MutantKiwi/agol-connector-qgis/issues)
