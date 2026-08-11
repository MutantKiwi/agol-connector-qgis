def _log_get():
    """Lazy logger getter — avoids import-time failures."""
    try:
        from .logger import log
        return log
    except Exception:
        class _NullLog:
            def debug(self, *a, **k): pass
            def info(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def error(self, *a, **k): pass
            def success(self, *a, **k): pass
        return _NullLog()

class _LogProxy:
    """Module-level proxy that lazily resolves the real logger."""
    def __getattr__(self, name):
        return getattr(_log_get(), name)

_log = _LogProxy()

"""
agol_client.py — ArcGIS Online REST client (no Esri libraries)
==============================================================
Endpoints used:
  Auth        /sharing/rest/generateToken  /sharing/rest/oauth2/token
  Content     /sharing/rest/content/users/{user}[/{folder}]
  Folders     /sharing/rest/content/users/{user}/createFolder
  Search      /sharing/rest/search
  Features    /FeatureServer/{n}/query?f=geojson
  Map/WMS     /MapServer/WMSServer
  Image/WMS   /ImageServer/WMSServer
  Image dl    /ImageServer/exportImage
  Tile XYZ    {tileUrl}/tile/{z}/{y}/{x}
  Upload      /sharing/rest/content/users/{user}[/{folder}]/addItem
  Publish     /sharing/rest/content/users/{user}[/{folder}]/publish
  Share       /sharing/rest/content/users/{user}/shareItems
"""

import json
import os
import uuid
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

AGOL_ROOT = "https://www.arcgis.com"

def _token_expiry_mins() -> int:
    """Read token expiry from plugin settings."""
    try:
        from qgis.core import QgsSettings
        return int(QgsSettings().value(
            "AGOL/settings/token_expiry_mins", 60
        ))
    except Exception:
        return 60


# AGOL item types and how to consume them in QGIS
SERVICE_TYPES = {
    "Feature Service":     "feature",
    "Map Service":         "map",
    "Image Service":       "image",
    "Tile Layer":          "tile",
    "Vector Tile Service": "vtile",
    "Web Map":             "webmap",
}


class AGOLAuthError(Exception):
    pass


class AGOLRequestError(Exception):
    pass

class AGOLTokenExpiredError(AGOLRequestError):
    """Raised when AGOL returns error code 498 or 499 (token expired/invalid)."""
    pass


class AGOLClient:

    def __init__(self, portal_url: str = AGOL_ROOT):
        self.portal_url = portal_url.rstrip("/")
        self.sharing = f"{self.portal_url}/sharing/rest"
        self.token: Optional[str] = None
        self.username: Optional[str] = None
        self.token_expires: int = 0

    # ------------------------------------------------------------------ #
    #  Authentication                                                      #
    # ------------------------------------------------------------------ #

    def login_token(self, username: str, password: str,
                    referer: str = AGOL_ROOT) -> None:
        """
        Authenticate with username + password.
        Tries referer-based token first, then client=requestip fallback.
        Federated/SSO org accounts cannot use generateToken and must
        use OAuth2 -- a clear message is raised explaining this.
        """
        last_error = ""
        for params in [
            {"username": username, "password": password,
             "referer": referer,
             "expiration": _token_expiry_mins(), "f": "json"},
            {"username": username, "password": password,
             "client": "requestip",
             "expiration": _token_expiry_mins(), "f": "json"},
        ]:
            try:
                data = self._post(
                    f"{self.sharing}/generateToken",
                    params,
                    authenticated=False,
                )
                if "token" in data:
                    self.token = data["token"]
                    self.token_expires = data.get("expires", 0)
                    self.username = username
                    return
                last_error = data.get("error", {}).get("message", "Unknown")
            except Exception as exc:
                last_error = str(exc)

        msg = last_error or "Authentication failed"
        if "Unable to generate token" in msg or not self.token:
            msg = (
                "Unable to generate a token for account '" + username + "'.\n\n"
                "This usually means the account is federated (SSO/SAML) and "
                "cannot log in with username + password.\n\n"
                "Fix: switch to the OAuth2 tab and sign in with your "
                "organisation's Client ID, or contact your AGOL administrator."
            )
        raise AGOLAuthError(msg)


    def login_oauth(self, client_id: str,
                    redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob") -> str:
        qs = urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code",
            "redirect_uri": redirect_uri, "expiration": 20160,
        })
        return f"{self.sharing}/oauth2/authorize?{qs}"

    def exchange_oauth_code(self, client_id: str, code: str,
                             redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob") -> None:
        data = self._post(f"{self.sharing}/oauth2/token", {
            "client_id": client_id, "grant_type": "authorization_code",
            "code": code, "redirect_uri": redirect_uri,
        }, authenticated=False)
        if "error" in data:
            raise AGOLAuthError(data["error"].get("message", "OAuth exchange failed"))
        self.token = data["access_token"]
        me = self._get(f"{self.sharing}/community/self")
        self.username = me.get("username")

    # ------------------------------------------------------------------ #
    #  Folders                                                             #
    # ------------------------------------------------------------------ #

    def get_user_folders(self) -> list[dict]:
        self._require_auth()
        return self._get(
            f"{self.sharing}/content/users/{self.username}"
        ).get("folders", [])

    def delete_item(self, item_id: str) -> None:
        """Permanently delete an item from AGOL. Raises on failure."""
        self._require_auth()
        data = self._post(
            f"{self.sharing}/content/users/{self.username}/items/{item_id}/delete",
            {},
        )
        if data.get("success") is False or "error" in data:
            msg = data.get("error", {}).get("message", "Delete failed")
            raise AGOLRequestError(msg)

    def get_user_groups(self) -> list[dict]:
        """Return groups the authenticated user belongs to."""
        self._require_auth()
        me = self._get(f"{self.sharing}/community/self")
        return me.get("groups", [])

    def create_group(self, title: str, description: str = "",
                     access: str = "private") -> dict:
        """Create a new group. Returns the group dict including 'id'."""
        self._require_auth()
        data = self._post(f"{self.sharing}/community/createGroup", {
            "title":       title,
            "description": description,
            "access":      access,
            "f":           "json",
        })
        if "error" in data:
            raise AGOLRequestError(
                data["error"].get("message", "Group creation failed")
            )
        return data.get("group", data)

    def add_item_to_group(self, item_id: str, group_id: str) -> None:
        """Share an item with a group."""
        self._require_auth()
        self._post(
            f"{self.sharing}/content/users/{self.username}/shareItems",
            {"items": item_id, "groups": group_id},
        )

    def set_item_access(self, item_id: str, access: str,
                         group_ids: str = "") -> None:
        _log.info("Setting item access", item_id=item_id, access=access)
        """
        Set the access level of an item.
        access: "private" | "org" | "public"
        group_ids: optional comma-separated group IDs to also share with
        """
        self._require_auth()
        params = {
            "items":    item_id,
            "everyone": "true" if access == "public" else "false",
            "org":      "true" if access in ("public", "org") else "false",
        }
        if group_ids:
            params["groups"] = group_ids
        _log.info("Calling shareItems", item_id=item_id, access=access,
                  everyone=params["everyone"], org=params["org"])
        resp = self._post(
            f"{self.sharing}/content/users/{self.username}/shareItems",
            params,
        )
        _log.info("shareItems response", resp=resp)
        if resp.get("error"):
            raise AGOLRequestError(
                f"Sharing failed: {resp['error'].get('message', resp)}"
            )

    def get_web_map_layers(self, item_id: str) -> list[dict]:
        """
        Return the operational layers defined in a Web Map item.
        Each dict has: title, url, layerType, id, visibility, opacity.
        """
        self._require_auth()
        # Fetch the item data JSON (the web map definition)
        data = self._get(
            f"{self.sharing}/content/items/{item_id}/data"
        )
        layers = []
        for op in data.get("operationalLayers", []):
            layers.append({
                "title":     op.get("title", op.get("id", "Layer")),
                "url":       op.get("url", ""),
                "layerType": op.get("layerType", ""),
                "id":        op.get("id", ""),
                "visibility":op.get("visibility", True),
                "opacity":   op.get("opacity", 1.0),
            })
        return layers

    def get_folder_items(self, folder_id: str = "") -> list[dict]:
        """Return ALL service types in a folder (not just Feature Services)."""
        self._require_auth()
        if folder_id:
            url = f"{self.sharing}/content/users/{self.username}/{folder_id}"
        else:
            url = f"{self.sharing}/content/users/{self.username}"
        items = self._get(url, {"num": 100}).get("items", [])
        return [i for i in items if i.get("type") in SERVICE_TYPES]

    def create_folder(self, name: str) -> dict:
        self._require_auth()
        resp = self._post(
            f"{self.sharing}/content/users/{self.username}/createFolder",
            {"title": name},
        )
        if not resp.get("success") and "folder" not in resp:
            raise AGOLRequestError(f"createFolder failed: {resp}")
        return resp.get("folder", resp)

    # ------------------------------------------------------------------ #
    #  Permissions                                                         #
    # ------------------------------------------------------------------ #

    def share_item(self, item_id: str, everyone: bool = False,
                   org: bool = False, groups: str = "") -> dict:
        self._require_auth()
        payload = {
            "items": item_id,
            "everyone": "true" if everyone else "false",
            "org": "true" if org else "false",
        }
        if groups:
            payload["groups"] = groups
        return self._post(
            f"{self.sharing}/content/users/{self.username}/shareItems", payload
        )

    # ------------------------------------------------------------------ #
    #  Search — all service types                                          #
    # ------------------------------------------------------------------ #

    def search_feature_services(self, query: str = "", owner: str = "",
                                  max_results: int = 50) -> list[dict]:
        return self.search_services(
            query, owner, max_results, service_type="Feature Service"
        )

    def search_services(self, query: str = "", owner: str = "",
                         max_results: int = 200,
                         service_type: str = "") -> list[dict]:
        """
        Search AGOL for any service type.
        Paginates automatically — AGOL returns max 100 per page, so we
        loop with the `start` parameter until we have max_results or run out.

        service_type: "Feature Service" | "Map Service" | "Image Service" |
                      "Tile Layer" | "" (all supported types)
        """
        if service_type:
            q_parts = [f'type:"{service_type}"']
        else:
            type_clause = " OR ".join(f'type:"{t}"' for t in SERVICE_TYPES)
            q_parts = [f"({type_clause})"]
        if query:
            q_parts.append(query)
        if owner:
            q_parts.append(f"owner:{owner}")

        q_str   = " AND ".join(q_parts)
        results = []
        start   = 1
        page    = min(100, max_results)   # AGOL hard limit is 100 per request

        while len(results) < max_results:
            data = self._get(f"{self.sharing}/search", {
                "q":     q_str,
                "num":   page,
                "start": start,
            })
            batch = data.get("results", [])
            results.extend(batch)
            next_start = data.get("nextStart", -1)
            if next_start == -1 or not batch:
                break   # no more pages
            start = next_start

        return results[:max_results]

    def get_service_layers(self, service_url: str) -> list[dict]:
        data = self._get(service_url.rstrip("/"), {"f": "json"})
        return data.get("layers", [])

    # ------------------------------------------------------------------ #
    #  QGIS connection strings for each service type                      #
    # ------------------------------------------------------------------ #

    def get_service_capabilities(self, service_url: str) -> dict:
        """
        Fetch the service JSON and return what's actually available:
          wms    — WMSServer extension enabled
          wcs    — WCSServer extension enabled  
          export — exportImage / Export capability
          tile   — has a tile cache (tileInfo present)
          raw    — full service JSON
        """
        try:
            raw = self._get(service_url.rstrip("/"), {"f": "json"})
        except Exception:
            raw = {}
        exts = raw.get("supportedExtensions", "") or ""
        caps = raw.get("capabilities", "") or ""
        return {
            "wms":    "WMSServer" in exts,
            "wcs":    "WCSServer" in exts,
            "export": any(k in caps for k in ("Image", "Catalog", "Export")),
            "tile":   bool(raw.get("tileInfo")),
            "raw":    raw,
        }


    def get_wms_url(self, service_url: str) -> str:
        """
        Build a QGIS WMS data source URI.
        Only call this after confirming get_service_capabilities()["wms"] is True.
        Token is embedded in the endpoint URL; layer name comes from GetCapabilities.
        Values are NOT percent-encoded — QGIS encodes them internally.
        """
        wms_endpoint = f"{service_url.rstrip('/')}/WMSServer"
        if self.token:
            wms_endpoint += f"?token={self.token}"
        layer_name = self._wms_first_layer(wms_endpoint) or "0"
        return (
            f"crs=EPSG:4326"
            f"&format=image/png"
            f"&layers={layer_name}"
            f"&styles="
            f"&url={wms_endpoint}"
            f"&version=1.3.0"
        )

    def _wms_first_layer(self, wms_endpoint: str) -> str:
        """Fetch WMS GetCapabilities and return the first named layer, or ""."""
        import xml.etree.ElementTree as ET
        sep = "&" if "?" in wms_endpoint else "?"
        caps_url = urllib.parse.quote(
            f"{wms_endpoint}{sep}SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0",
            safe=":/?#[]@!$&'()*+,;=%",
        )
        try:
            req = urllib.request.Request(
                caps_url, headers={"User-Agent": "QGIS-AGOL-Connector/0.1"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                root = ET.fromstring(r.read())
            ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
            prefix = f"{{{ns}}}" if ns else ""
            for layer_el in root.iter(f"{prefix}Layer"):
                name_el = layer_el.find(f"{prefix}Name")
                if name_el is not None and name_el.text:
                    return name_el.text.strip()
        except Exception:
            pass
        return ""

    def get_wcs_url(self, service_url: str) -> str:
        """Build a QGIS WCS data source URI for an AGOL Image Service."""
        endpoint = f"{service_url.rstrip('/')}/WCSServer"
        if self.token:
            endpoint += f"?token={self.token}"
        return f"url={endpoint}&version=1.1.1"

    def get_xyz_url(self, service_url: str) -> str:
        """
        Build a QGIS XYZ tile data source URI for an AGOL Map Service.

        The token is passed as a custom HTTP header (X-Esri-Authorization)
        rather than a query parameter — this avoids URL-encoding issues and
        is more reliable across QGIS versions.

        Falls back to query-param token if the header approach isn't supported.
        """
        import urllib.parse
        base = service_url.rstrip("/")
        tile_template = f"{base}/tile/{{z}}/{{y}}/{{x}}"

        if self.token:
            # Percent-encode the full tile URL (including token as query param)
            # so QGIS's XYZ provider handles it correctly
            tile_with_token = f"{tile_template}?token={urllib.parse.quote(self.token, safe='')}"
            # Use http-header to pass token — avoids encoding issues
            # Format: type=xyz&url=...&http-header:X-Esri-Authorization=Bearer TOKEN
            encoded_url = urllib.parse.quote(tile_template, safe=":/?={}")
            return (
                f"type=xyz"
                f"&url={encoded_url}"
                f"&zmin=0&zmax=19"
                f"&http-header:X-Esri-Authorization=Bearer {self.token}"
            )
        return f"type=xyz&url={urllib.parse.quote(tile_template, safe=':/?={{}}'  )}&zmin=0&zmax=19"

    def get_wmts_url(self, service_url: str) -> str:
        """QGIS WMTS data source URI. Token embedded in capabilities URL."""
        wmts_endpoint = (
            f"{service_url.rstrip('/')}/WMTS/1.0.0/WMTSCapabilities.xml"
        )
        if self.token:
            wmts_endpoint += f"?token={self.token}"
        return (
            f"crs=EPSG:3857"
            f"&format=image/png"
            f"&layers=0"
            f"&styles="
            f"&tileMatrixSet=default028mm"
            f"&url={wmts_endpoint}"
        )


    # ------------------------------------------------------------------ #
    #  Feature layer query                                                 #
    # ------------------------------------------------------------------ #

    def query_layer(self, layer_url: str, where: str = "1=1",
                    out_fields: str = "*", max_record_count: int = 2000,
                    out_sr: int = 4326,
                    progress_callback=None) -> dict:
        """
        Query a feature layer, paginating automatically.

        Strategy A — objectIds paging (preferred, most reliable):
          Fetch all matching OIDs in one request, then retrieve features
          in batches keyed by OID.  Reliable even when services don't set
          exceededTransferLimit and when records change mid-query.

        Strategy B — resultOffset paging (fallback):
          Used when the OID fetch fails.  Continues while either
          exceededTransferLimit is True OR a full page was returned
          (handles services that never set exceededTransferLimit).

        progress_callback(fetched: int, total: int): optional UI callback.
        """
        base_url  = layer_url.rstrip("/")
        query_url = f"{base_url}/query"

        # ── Read server's own maxRecordCount ────────────────────────────
        page_size = max_record_count   # default
        try:
            svc_info     = self._get(base_url, {"f": "json"})
            server_limit = int(svc_info.get("maxRecordCount") or 0)
            if server_limit > 0:
                page_size = min(max_record_count, server_limit)
        except Exception:
            pass

        # ── Strategy A: objectIds batching ──────────────────────────────
        try:
            oid_resp = self._get(query_url, {
                "where":         where,
                "returnIdsOnly": "true",
                "f":             "json",
            })
            # If the response contains an error dict, fall through
            if "error" in oid_resp:
                raise AGOLRequestError(oid_resp["error"].get("message", "OID error"))

            all_oids = oid_resp.get("objectIds") or []
            if all_oids:
                total        = len(all_oids)
                all_features = []
                for i in range(0, total, page_size):
                    batch    = all_oids[i: i + page_size]
                    oids_csv = ",".join(str(o) for o in batch)
                    chunk = self._get(query_url, {
                        "objectIds": oids_csv,
                        "outFields": out_fields,
                        "outSR":     out_sr,
                        "f":         "geojson",
                    })
                    if "error" in chunk:
                        raise AGOLRequestError(
                            chunk["error"].get("message", "Batch error")
                        )
                    all_features.extend(chunk.get("features", []))
                    if progress_callback:
                        progress_callback(min(i + page_size, total), total)
                return {"type": "FeatureCollection", "features": all_features}
            # Empty OID list = no features matching where clause
            return {"type": "FeatureCollection", "features": []}

        except Exception:
            # OID fetch not supported — fall through to Strategy B
            pass

        # ── Strategy B: resultOffset paging ─────────────────────────────
        # Stop when:
        #   a) no features returned (server exhausted), OR
        #   b) fewer features than page_size AND exceededTransferLimit is False
        #      (partial page = last page, even if server never sets the flag).
        # Do NOT stop just because exceededTransferLimit is False — many
        # AGOL services omit it even when there are more pages.
        all_features = []
        offset = 0
        while True:
            chunk = self._get(query_url, {
                "where":             where,
                "outFields":         out_fields,
                "outSR":             out_sr,
                "resultOffset":      offset,
                "resultRecordCount": page_size,
                "f":                 "geojson",
            })
            features = chunk.get("features", [])
            n = len(features)
            all_features.extend(features)
            if progress_callback:
                progress_callback(len(all_features), len(all_features))

            exceeded    = chunk.get("exceededTransferLimit", False)
            got_full    = (n == page_size)

            # No features → definitely done
            if n == 0:
                break
            # Partial page AND server says not exceeded → last page
            if not got_full and not exceeded:
                break
            # Full page or exceeded → there may be more
            offset += n
        return {"type": "FeatureCollection", "features": all_features}

    # ------------------------------------------------------------------ #
    #  Image service export (rendered extent → GeoTIFF)                  #
    # NOTE: this gives a rendered image, not raw raster data.             #
    # ------------------------------------------------------------------ #

    def export_image_extent(self, image_service_url: str,
                             bbox: tuple[float, float, float, float],
                             width: int = 2048, height: int = 2048,
                             out_path: str = "") -> str:
        """
        Download a rendered GeoTIFF for the given bounding box from an ImageServer.
        bbox: (xmin, ymin, xmax, ymax) in WGS84
        Returns the path of the saved file.
        NOTE: This is a rendered export, not the raw source raster.
        """
        xmin, ymin, xmax, ymax = bbox
        url = f"{image_service_url.rstrip('/')}/exportImage"
        params = {
            "bbox":       f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR":     "4326",
            "size":       f"{width},{height}",
            "imageSR":    "4326",
            "format":     "tiff",
            "pixelType":  "UNKNOWN",
            "f":          "image",
        }
        if self.token:
            params["token"] = self.token
        qs = urllib.parse.urlencode(params)
        full_url = f"{url}?{qs}"

        if not out_path:
            import tempfile
            fd, out_path = tempfile.mkstemp(suffix=".tif")
            os.close(fd)

        req = urllib.request.Request(
            full_url, headers={"User-Agent": "QGIS-AGOL-Connector/0.1"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(out_path, "wb") as f:
                f.write(resp.read())
        return out_path

    # ------------------------------------------------------------------ #
    #  Raster upload → hosted Image Service                               #
    # ------------------------------------------------------------------ #

    def upload_raster(self, title: str, file_path: str,
                      description: str = "", tags: str = "qgis,raster",
                      folder_id: str = "") -> dict:
        """
        Upload a raster file (GeoTIFF, IMG, etc.) to AGOL and publish it as
        a hosted Image Service or Tile Layer.

        Steps:
          1. Multipart POST → addItem (type = "Image Service" item)
          2. Publish → hosted Image Service
          Returns the service info dict.
        """
        self._require_auth()
        base = f"{self.sharing}/content/users/{self.username}"
        add_url = f"{base}/{folder_id}/addItem" if folder_id else f"{base}/addItem"
        pub_url = f"{base}/{folder_id}/publish" if folder_id else f"{base}/publish"

        ext = os.path.splitext(file_path)[1].lower()
        mime = "image/tiff" if ext in (".tif", ".tiff") else "application/octet-stream"

        fields = {
            "title":       title,
            "type":        "Image Service",
            "description": description,
            "tags":        tags,
            "overwrite":   "false",
            "f":           "json",
        }
        if self.token:
            fields["token"] = self.token

        item_resp = self._post_multipart(add_url, fields, "file", file_path, mime)
        if not item_resp.get("success"):
            raise AGOLRequestError(f"addItem (raster) failed: {item_resp}")
        item_id = item_resp["id"]

        pub_resp = self._post(pub_url, {
            "itemid":          item_id,
            "filetype":        "imageService",
            "publishParameters": json.dumps({"name": title}),
        })
        services = pub_resp.get("services", [])
        if not services:
            raise AGOLRequestError(f"Raster publish failed: {pub_resp}")
        result = services[0]
        result["_item_id"] = item_id
        return result

    # ------------------------------------------------------------------ #
    #  Vector upload → hosted Feature Service                             #
    # ------------------------------------------------------------------ #

    def upload_geojson_file(self, title: str, file_path: str,
                              description: str = "",
                              tags: str = "qgis,upload",
                              folder_id: str = "",
                              summary: str = "",
                              terms: str = "",
                              credits: str = "") -> dict:
        """
        Upload a GeoJSON file directly to AGOL.
        Switches to Shapefile ZIP automatically if >= 50 MB.
        """
        self._require_auth()

        file_size = os.path.getsize(file_path)
        _log.info("Uploading file", path=file_path,
                  size_mb=f"{file_size/1024/1024:.1f} MB", title=title)

        base    = f"{self.sharing}/content/users/{self.username}"
        add_url = f"{base}/{folder_id}/addItem" if folder_id else f"{base}/addItem"
        pub_url = f"{base}/{folder_id}/publish"  if folder_id else f"{base}/publish"

        extra = {}
        if summary: extra["snippet"]           = summary
        if terms:   extra["licenseInfo"]        = terms
        if credits: extra["accessInformation"]  = credits

        if file_size >= self._GEOJSON_SIZE_LIMIT:
            _log.info("File > 50 MB — converting to Shapefile ZIP")
            import json as _json
            with open(file_path, encoding="utf-8") as f:
                geojson = _json.load(f)
            return self._upload_geojson_as_shapefile_zip(
                title, geojson, description, tags, folder_id, add_url, pub_url,
                extra=extra,
            )

        # Multipart upload of GeoJSON file
        fields = {"title": title, "type": "GeoJson",
                  "description": description, "tags": tags,
                  "overwrite": "false", "f": "json", **extra}
        item_resp = self._post_multipart(
            add_url, fields,
            file_field="file",
            file_path=file_path,
            file_mime="application/json",
        )
        self._check_add_item_resp(item_resp, title)
        item_id = item_resp["id"]
        _log.info("addItem succeeded", item_id=item_id)

        pub_resp = self._post(pub_url, {
            "itemid":            item_id,
            "filetype":          "geojson",
            "publishParameters": json.dumps({"name": title}),
        })
        _log.debug("publish response", resp=pub_resp)
        return self._check_publish_resp(pub_resp, item_id)

    # ── GeoJSON size threshold above which we switch to Shapefile ZIP ──────
    _GEOJSON_SIZE_LIMIT = 50 * 1024 * 1024   # 50 MB

    def upload_geojson_as_service(self, title: str, geojson: dict,
                                   description: str = "",
                                   tags: str = "qgis,upload",
                                   folder_id: str = "") -> dict:
        """
        Upload a GeoJSON dict as a hosted Feature Service.

        Strategy:
          • GeoJSON < 50 MB  → multipart GeoJSON upload (fastest)
          • GeoJSON ≥ 50 MB  → Shapefile ZIP upload (smaller binary format)

        Both routes end with a /publish call that creates a hosted Feature Service.
        """
        self._require_auth()
        import tempfile as _tmp

        base    = f"{self.sharing}/content/users/{self.username}"
        add_url = f"{base}/{folder_id}/addItem" if folder_id else f"{base}/addItem"
        pub_url = f"{base}/{folder_id}/publish"  if folder_id else f"{base}/publish"

        geojson_str = json.dumps(geojson, separators=(",", ":"))  # compact
        size_bytes  = len(geojson_str.encode("utf-8"))
        _log.info("Upload started", title=title,
                  size_mb=f"{size_bytes/1024/1024:.1f} MB",
                  folder=folder_id or "home")

        if size_bytes >= self._GEOJSON_SIZE_LIMIT:
            # Large layer → Shapefile ZIP (handled by upload_layer_as_shapefile_zip)
            return self._upload_geojson_as_shapefile_zip(
                title, geojson, description, tags, folder_id,
                add_url, pub_url,
            )

        # ── Multipart GeoJSON upload ──────────────────────────────────
        with _tmp.NamedTemporaryFile(
            mode="w", suffix=".geojson",
            delete=False, encoding="utf-8"
        ) as tf:
            tf.write(geojson_str)
            tmp_path = tf.name

        try:
            item_resp = self._post_multipart(
                add_url,
                {"title": title, "type": "GeoJson",
                 "description": description, "tags": tags,
                 "overwrite": "false", "f": "json"},
                file_field="file",
                file_path=tmp_path,
                file_mime="application/json",
            )
        finally:
            try: os.unlink(tmp_path)
            except Exception: pass

        self._check_add_item_resp(item_resp, title)
        item_id = item_resp["id"]

        pub_resp = self._post(pub_url, {
            "itemid":            item_id,
            "filetype":          "geojson",
            "publishParameters": json.dumps({"name": title}),
        })
        return self._check_publish_resp(pub_resp, item_id)

    def _upload_geojson_as_shapefile_zip(self, title: str, geojson: dict,
                                          description: str, tags: str,
                                          folder_id: str,
                                          add_url: str, pub_url: str,
                                          extra: dict | None = None) -> dict:
        """Upload large layers as a Shapefile ZIP — more compact than GeoJSON."""
        import tempfile as _tmp, zipfile as _zf, subprocess as _sp, shutil as _sh

        tmpdir = _tmp.mkdtemp()
        try:
            # Write GeoJSON to temp file, convert to Shapefile with ogr2ogr
            gj_path  = os.path.join(tmpdir, "layer.geojson")
            shp_path = os.path.join(tmpdir, f"{title}.shp")
            zip_path = os.path.join(tmpdir, f"{title}.zip")

            with open(gj_path, "w", encoding="utf-8") as f:
                json.dump(geojson, f, separators=(",", ":"))

            # Use QGIS vector writer instead of ogr2ogr (always available)
            try:
                from qgis.core import QgsVectorLayer, QgsVectorFileWriter
                layer = QgsVectorLayer(gj_path, "tmp", "ogr")
                if not layer.isValid():
                    raise AGOLRequestError("Could not open GeoJSON for Shapefile conversion")
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName = "ESRI Shapefile"
                err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                    layer, shp_path, layer.transformContext(), opts
                )
                if err != QgsVectorFileWriter.WriterError.NoError:
                    raise AGOLRequestError(f"Shapefile conversion failed: {msg}")
            except ImportError:
                raise AGOLRequestError(
                    "Layer too large for GeoJSON upload and QGIS vector writer "
                    "unavailable for Shapefile conversion."
                )

            # Zip all Shapefile component files
            with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
                for ext in ("shp", "dbf", "shx", "prj", "cpg"):
                    part = shp_path.replace(".shp", f".{ext}")
                    if os.path.exists(part):
                        zf.write(part, os.path.basename(part))

            item_resp = self._post_multipart(
                add_url,
                {"title": title, "type": "Shapefile",
                 "description": description, "tags": tags,
                 "overwrite": "false", "f": "json", **(extra or {})},
                file_field="file",
                file_path=zip_path,
                file_mime="application/zip",
            )
        finally:
            try: _sh.rmtree(tmpdir)
            except Exception: pass

        self._check_add_item_resp(item_resp, title)
        item_id = item_resp["id"]
        _log.info("Shapefile addItem succeeded", item_id=item_id)

        pub_resp = self._post(pub_url, {
            "itemid":            item_id,
            "filetype":          "shapefile",
            "publishParameters": json.dumps({"name": title}),
        })
        _log.debug("Shapefile publish response", resp=pub_resp)
        return self._check_publish_resp(pub_resp, item_id)

    def _check_add_item_resp(self, resp: dict, title: str):
        """Raise a clear error if addItem failed."""
        _log.debug("addItem response", title=title, success=resp.get('success') if resp else None)
        if not resp:
            raise AGOLRequestError(
                "addItem returned no response.\n"
                "Possible causes: token expired, network timeout, or the "
                "request body was too large."
            )
        if "error" in resp:
            err     = resp["error"]
            code    = err.get("code", "")
            msg     = err.get("message", str(err))
            details = "; ".join(err.get("details") or [])
            raise AGOLRequestError(
                f"addItem error {code}: {msg}"
                + (f"\n{details}" if details else "")
            )
        if not resp.get("success"):
            raise AGOLRequestError(
                f"addItem failed: {resp}\n\n"
                "Ensure your account has 'Create content' privileges."
            )

    def _check_publish_resp(self, resp: dict, item_id: str) -> dict:
        """Raise a clear error if publish failed, else return result."""
        if "error" in resp:
            raise AGOLRequestError(
                f"Publish error: {resp['error'].get('message', resp)}"
            )
        services = resp.get("services", [])
        if not services:
            raise AGOLRequestError(
                f"Publish returned no services: {resp}\n\n"
                "The item was uploaded but could not be published as a Feature Service."
            )
        result = services[0]
        result["_item_id"] = item_id
        return result

    def append_features(self, layer_url: str, geojson_features: list[dict]) -> dict:
        esri = [self._geojson_feature_to_esri(f) for f in geojson_features]
        return self._post(f"{layer_url.rstrip('/')}/addFeatures", {
            "features": json.dumps(esri), "rollbackOnFailure": "true",
        })

    # ------------------------------------------------------------------ #
    #  HTTP helpers                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_token_error(data: dict) -> None:
        """Raise AGOLTokenExpiredError if AGOL returned a token error in JSON."""
        err = data.get("error", {})
        if isinstance(err, dict):
            code = err.get("code", 0)
            if code in (498, 499):
                raise AGOLTokenExpiredError(
                    err.get("message", "Token expired or invalid")
                )

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        p = dict(params or {})
        if self.token:
            p["token"] = self.token
        p.setdefault("f", "json")
        url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
        req = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(p)}",
            headers={"User-Agent": "QGIS-AGOL-Connector/0.1"},
        )
        try:
            from qgis.core import QgsSettings
            _to = int(QgsSettings().value('AGOL/settings/timeout', 30))
            with urllib.request.urlopen(req, timeout=_to) as r:
                data = json.loads(r.read().decode())
            self._check_token_error(data)
            return data
        except AGOLTokenExpiredError:
            raise
        except urllib.error.HTTPError as e:
            raise AGOLRequestError(f"HTTP {e.code} GET {url}: {e.reason}")

    def _post(self, url: str, data: dict, authenticated: bool = True) -> dict:
        d = dict(data)
        if authenticated and self.token:
            d["token"] = self.token
        d.setdefault("f", "json")
        url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(d).encode(),
            headers={
                "User-Agent":   "QGIS-AGOL-Connector/0.1",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            from qgis.core import QgsSettings
            _to = int(QgsSettings().value('AGOL/settings/timeout', 60))
            with urllib.request.urlopen(req, timeout=_to) as r:
                data = json.loads(r.read().decode())
            self._check_token_error(data)
            return data
        except AGOLTokenExpiredError:
            raise
        except urllib.error.HTTPError as e:
            raise AGOLRequestError(f"HTTP {e.code} POST {url}: {e.reason}")

    def _post_multipart(self, url: str, fields: dict,
                         file_field: str, file_path: str,
                         file_mime: str) -> dict:
        """Multipart form POST for file uploads — no third-party libs needed."""
        boundary = uuid.uuid4().hex
        ctype = f"multipart/form-data; boundary={boundary}"

        body = b""
        for key, value in fields.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as fh:
            file_data = fh.read()
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {file_mime}\r\n\r\n"
        ).encode() + file_data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        # Token must be percent-encoded when placed in the URL query string
        # — raw tokens contain +, ., _ chars that need encoding
        url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
        if self.token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={urllib.parse.quote(self.token, safe='')}&f=json"

        # Use a custom opener that does NOT follow redirects —
        # urllib follows 302 with a GET which loses the POST body and
        # returns empty. AGOL's addItem should never redirect on success.
        class _NoRedirect(urllib.request.HTTPErrorProcessor):
            def http_response(self, req, resp):
                return resp
            https_response = http_response

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent":   "QGIS-AGOL-Connector/0.1",
            "Content-Type": ctype,
        })
        try:
            with opener.open(req, timeout=300) as r:
                status = r.getcode()
                raw    = r.read().decode("utf-8", errors="replace")

            # Follow only 200 — anything else is an error
            if status in (301, 302, 303, 307, 308):
                raise AGOLRequestError(
                    f"Server redirected (HTTP {status}) the upload request. "
                    "This usually means the addItem URL is wrong or the "
                    "account's content endpoint has moved."
                )

            # Guard against HTML error pages
            if raw.lstrip().startswith("<"):
                raise AGOLRequestError(
                    "Server returned an HTML page instead of JSON.\n"
                    "The token may have expired — please sign out and sign in again."
                )

            if not raw.strip():
                raise AGOLRequestError(
                    f"Server returned an empty response (HTTP {status}).\n"
                    "Possible causes:\n"
                    "  • Token expired or invalid\n"
                    "  • Organisation does not allow hosted content creation\n"
                    "  • File size exceeds your account's storage quota"
                )
            _log.error("Empty response from addItem", url=url, status=status)

            _log.debug("Multipart POST response", status=status, size=len(raw))
            data = json.loads(raw)
            self._check_token_error(data)
            return data

        except AGOLTokenExpiredError:
            raise
        except AGOLRequestError:
            raise
        except json.JSONDecodeError as e:
            _log.error("Invalid JSON from server", raw=repr(raw[:200]))
            try:
                from .error_tracking import report_error
                report_error(e, {"url": url[:200], "status": status})
            except Exception: pass
            raise AGOLRequestError(
                f"Invalid JSON from server: {e}\n"
                f"Response ({len(raw)} chars): {repr(raw[:300])}"
            )
        except urllib.error.HTTPError as e:
            body_txt = ""
            try: body_txt = e.read().decode("utf-8", errors="replace")[:300]
            except Exception: pass
            raise AGOLRequestError(
                f"HTTP {e.code} during upload: {e.reason}\n{body_txt}"
            )
        except urllib.error.URLError as e:
            raise AGOLRequestError(f"Network error during upload: {e.reason}")

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    def _require_auth(self):
        if not self.username:
            raise AGOLAuthError("Not authenticated")

    @staticmethod
    def _geojson_feature_to_esri(feature: dict) -> dict:
        geom  = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        gt    = geom.get("type", "")
        c     = geom.get("coordinates", [])
        eg: dict = {}
        if gt == "Point":
            eg = {"x": c[0], "y": c[1]}
            if len(c) > 2:
                eg["z"] = c[2]
        elif gt == "LineString":
            eg = {"paths": [c]}
        elif gt == "MultiLineString":
            eg = {"paths": c}
        elif gt == "Polygon":
            eg = {"rings": c}
        elif gt == "MultiPolygon":
            eg = {"rings": [r for poly in c for r in poly]}
        eg["spatialReference"] = {"wkid": 4326}
        return {"geometry": eg, "attributes": props}
