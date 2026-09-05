from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely.geometry import Point, mapping
from shapely.ops import transform
from shapely.strtree import STRtree

from api.services.hec_routing import (
    DssParserUnavailable,
    DssReadError,
    observe_points as hec_observe_points,
    reaches_geojson as hec_reaches_geojson,
    modeled_rivers_geojson as hec_modeled_rivers_geojson,
    modeled_area_geojson as hec_modeled_area_geojson,
    routing_info as hec_routing_info,
    selected_reach_series,
    snap_points as hec_snap_points,
    routing_flow_side as hec_routing_flow_side,
    prewarm_display_objects as hec_prewarm_display_objects,
)
from api.services.hydrograph_export import (
    build_hydrograph_xlsx,
    hydrograph_filename,
    xlsx_mime_type,
)
from api.services.river_display import RIVER_DISPLAY_TIER_BY_FILENAME, build_river_display_gdf
from api.services.reference_backend import (
    ensure_toponym_db_path,
    get_reference_backend_metrics,
    load_reference_bundle,
)

API_DIR = Path(__file__).resolve().parent
ROOT_DIR = API_DIR.parent
STATIC_DIR = ROOT_DIR / "static"
TEMPLATES_DIR = ROOT_DIR / "templates"

CRS_WEB = "EPSG:4326"
CRS_AREA = "ESRI:54034"
APP_VERSION = "1.0.2.0"
DEFAULT_RIVER_SEARCH_RADIUS_M = 300.0
TOPONYM_NAMING_RADIUS_M = 5_000.0
TOPONYM_SETTLEMENT_PRIORITY = {
    "Permukiman Lainnya": 0,
    "Ibukota Desa": 0,
    "Ibukota Kecamatan": 0,
    "Desa": 1,
    "Kecamatan": 2,
    "Kota": 3,
    "Ibukota Kabupaten": 4,
}
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "bbwsso-flood-routing/2.0 (river flood routing application)",
)


class HecObservationPoint(BaseModel):
    point_id: str | None = None
    label: str | None = None
    sheet_name: str | None = None
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)


class HecObservationRequest(BaseModel):
    points: list[HecObservationPoint] = Field(default_factory=list, max_length=10)
    scenario: str | None = None
    duration_hours: Literal[6, 12, 24] = 12
    snap_radius_m: float = Field(DEFAULT_RIVER_SEARCH_RADIUS_M, gt=0, le=20_000)
    # The add-point interaction needs both the routing snap and its display
    # identity.  Keeping this opt-in avoids changing the response used by
    # callers that only need routing geometry.
    include_identity: bool = False


class HecSeriesRequest(BaseModel):
    reach_ids: list[str] = Field(default_factory=list, max_length=1000)
    scenario: str | None = None
    duration_hours: Literal[6, 12, 24] = 12


# Reference layers are used only for cartography and automatic naming.
REFERENCE_DATA = load_reference_bundle(ROOT_DIR)
DATA_BACKEND = REFERENCE_DATA.backend
REFERENCE_METADATA = REFERENCE_DATA.metadata or {}
official_basins = REFERENCE_DATA.official_basins.copy()
official_rivers = REFERENCE_DATA.official_rivers.copy()
MAP_ASSETS_PUBLIC_BASE = REFERENCE_DATA.map_assets_public_base
MAP_ASSETS_VERSION = REFERENCE_DATA.map_assets_version


def _prewarm_toponym_object() -> None:
    """Fetch the optional R2 toponym index before the first map click needs it."""
    try:
        ensure_toponym_db_path(REFERENCE_DATA)
    except Exception:
        # Automatic naming is optional; a transient R2 failure must not stop startup.
        pass


if (
    DATA_BACKEND == "r2"
    and os.getenv("FLOOD_PREWARM_TOPONYM", "1").strip().lower() not in {"0", "false", "no"}
):
    threading.Thread(target=_prewarm_toponym_object, name="r2-toponym-prewarm", daemon=True).start()


if official_basins.empty or official_basins.crs is None:
    raise RuntimeError("Layer Batas DAS resmi tidak tersedia atau tidak memiliki CRS.")
if official_rivers.empty or official_rivers.crs is None:
    raise RuntimeError("Layer jaringan sungai resmi tidak tersedia atau tidak memiliki CRS.")
if official_rivers.crs != official_basins.crs:
    official_rivers = official_rivers.to_crs(official_basins.crs)

REFERENCE_CRS = official_basins.crs
to_reference = Transformer.from_crs(CRS_WEB, REFERENCE_CRS, always_xy=True)
to_web = Transformer.from_crs(REFERENCE_CRS, CRS_WEB, always_xy=True)

official_basin_geometries = list(official_basins.geometry.values)
official_basin_tree = STRtree(official_basin_geometries)
official_river_geometries = list(official_rivers.geometry.values)
official_river_tree = STRtree(official_river_geometries)
try:
    official_area_km2 = (
        official_basins.to_crs(CRS_AREA).geometry.area.to_numpy(dtype="float64", copy=False) / 1_000_000.0
    )
except Exception:
    official_area_km2 = [None] * len(official_basins)

_web_bounds = official_basins.to_crs(CRS_WEB).total_bounds.tolist()
_minlon, _minlat, _maxlon, _maxlat = [float(v) for v in _web_bounds]


def _largest_polygon_component(geom):
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type == "MultiPolygon":
        parts = [part for part in geom.geoms if not part.is_empty]
        return max(parts, key=lambda item: item.area) if parts else geom
    return geom


@lru_cache(maxsize=1)
def _build_basin_label_fc() -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    for _, row in official_basins.iterrows():
        geom = _largest_polygon_component(row.geometry)
        if geom is None or geom.is_empty:
            continue
        pt = transform(to_web.transform, geom.representative_point())
        features.append({
            "type": "Feature",
            "properties": {"basin_name": str(row.get("basin_name") or "")},
            "geometry": mapping(pt),
        })
    return {"type": "FeatureCollection", "features": features}


def official_basin_at_point(point_projected: Point) -> dict[str, Any] | None:
    idxs = official_basin_tree.query(point_projected, predicate="within")
    if len(idxs) == 0:
        idxs = official_basin_tree.query(point_projected, predicate="intersects")
    if len(idxs) == 0:
        return None
    pos = int(idxs[0])
    row = official_basins.iloc[pos]
    area = official_area_km2[pos] if pos < len(official_area_km2) else None
    return {
        "code": str(row.get("basin_code") or "") or None,
        "name": str(row.get("basin_name") or "") or None,
        "area_km2": round(float(area), 3) if area is not None and math.isfinite(float(area)) else None,
    }


def _river_base_name(name: str | None) -> str | None:
    text = str(name or "").strip()
    if not text:
        return None
    for prefix in ("Kali ", "K. ", "K ", "Sungai ", "S. ", "S "):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break
    return text or None


def _river_label(name: str | None) -> str | None:
    base = _river_base_name(name)
    return f"Kali {base}" if base else None


def _river_map_label(name: str | None) -> str | None:
    base = _river_base_name(name)
    return f"K. {base}" if base else None


def _nearest_official_river_match(point_projected: Point, max_distance_m: float = 1000.0):
    try:
        idxs, distances = official_river_tree.query_nearest(
            point_projected,
            max_distance=float(max_distance_m),
            return_distance=True,
            all_matches=True,
        )
    except Exception:
        return None
    if len(idxs) == 0:
        return None
    best = min(range(len(idxs)), key=lambda i: float(distances[i]))
    pos = int(idxs[best])
    return official_rivers.iloc[pos], float(distances[best])


def nearest_official_river(point_projected: Point, max_distance_m: float = 1000.0) -> dict[str, Any] | None:
    match = _nearest_official_river_match(point_projected, max_distance_m)
    if match is None:
        return None
    row, distance = match
    label = _river_label(row.get("river_name"))
    if not label:
        return None
    order = row.get("river_order")
    try:
        order_value = int(order) if order is not None and math.isfinite(float(order)) else None
    except Exception:
        order_value = None
    return {
        "name": label,
        "order": order_value,
        "basin": str(row.get("basin_name") or "") or None,
        "distance_m": round(float(distance), 1),
    }

def _normalize_search_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip()


_TOPONYM_CONNECTIONS = threading.local()


def _toponym_connection() -> sqlite3.Connection | None:
    try:
        path = ensure_toponym_db_path(REFERENCE_DATA)
    except Exception:
        return None
    if not path.exists():
        return None
    cached = getattr(_TOPONYM_CONNECTIONS, "connection", None)
    cached_path = getattr(_TOPONYM_CONNECTIONS, "path", None)
    if cached is not None and cached_path == str(path):
        return cached
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    _TOPONYM_CONNECTIONS.connection = conn
    _TOPONYM_CONNECTIONS.path = str(path)
    return conn


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * radius * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1.0 - h)))


@lru_cache(maxsize=4096)
def _nearby_settlement_candidates_cached(lon_key: float, lat_key: float) -> tuple[dict[str, Any], ...]:
    lon, lat = float(lon_key), float(lat_key)
    conn = _toponym_connection()
    if conn is None:
        return ()
    radius_m = TOPONYM_NAMING_RADIUS_M
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / max(10_000.0, 111_320.0 * math.cos(math.radians(lat)))
    rows = conn.execute(
        """
        SELECT t.id,t.name,t.category,t.lon,t.lat
        FROM toponim_rtree r
        JOIN toponim t ON t.id=r.id
        WHERE r.min_lon BETWEEN ? AND ?
          AND r.min_lat BETWEEN ? AND ?
        """,
        (lon - lon_delta, lon + lon_delta, lat - lat_delta, lat + lat_delta),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        category = str(row["category"] or "").strip()
        if category not in TOPONYM_SETTLEMENT_PRIORITY:
            continue
        distance = _haversine_m(lon, lat, float(row["lon"]), float(row["lat"]))
        if distance > radius_m:
            continue
        candidates.append({
            "id": int(row["id"]),
            "name": str(row["name"]),
            "category": category,
            "distance_m": float(distance),
            "lon": float(row["lon"]),
            "lat": float(row["lat"]),
            "priority": int(TOPONYM_SETTLEMENT_PRIORITY[category]),
        })
    return tuple(candidates)


def _closest_line_part(geometry, point_projected: Point):
    if geometry is None or getattr(geometry, "is_empty", True):
        return None
    parts = list(getattr(geometry, "geoms", [])) or [geometry]
    linear = [g for g in parts if getattr(g, "geom_type", "") in {"LineString", "LinearRing"} and not g.is_empty]
    if not linear:
        return None
    return min(linear, key=lambda g: float(g.distance(point_projected)))


def _river_side(line, snapped_point: Point, target_point: Point) -> int:
    """Side of target relative to local line direction: +1 left, -1 right."""
    if line is None or line.length <= 0:
        return 0
    offset = float(snapped_point.distance(target_point))
    if offset < 12.0:
        return 0
    station = float(line.project(snapped_point))
    delta = min(90.0, max(20.0, float(line.length) * 0.03))
    a_station = max(0.0, station - delta)
    b_station = min(float(line.length), station + delta)
    if b_station - a_station < 5.0:
        return 0
    a = line.interpolate(a_station); b = line.interpolate(b_station)
    dx, dy = float(b.x - a.x), float(b.y - a.y)
    vx, vy = float(target_point.x - snapped_point.x), float(target_point.y - snapped_point.y)
    tangent_len = math.hypot(dx, dy)
    if tangent_len <= 1e-9:
        return 0
    cross = dx * vy - dy * vx
    sin_angle = abs(cross) / max(1e-9, tangent_len * max(offset, 1e-9))
    if sin_angle < 0.08:
        return 0
    return 1 if cross > 0 else -1


def nearest_settlement_toponym(
    lon: float,
    lat: float,
    *,
    model_id: str | None = None,
    element_id: str | None = None,
    snapped_lon: float | None = None,
    snapped_lat: float | None = None,
) -> dict[str, Any] | None:
    """Pick a nearby settlement, preferring the clicked bank of reach2d."""
    candidates = [dict(item) for item in _nearby_settlement_candidates_cached(round(float(lon), 5), round(float(lat), 5))]
    if not candidates:
        return None
    requested_side = 0
    same_side_used = False
    if model_id and element_id and snapped_lon is not None and snapped_lat is not None:
        try:
            requested_side = hec_routing_flow_side(model_id, element_id, snapped_lon, snapped_lat, lon, lat)
        except Exception:
            requested_side = 0
        if requested_side:
            same_side = []
            for candidate in candidates:
                try:
                    side = hec_routing_flow_side(
                        model_id, element_id, snapped_lon, snapped_lat,
                        float(candidate["lon"]), float(candidate["lat"]),
                    )
                except Exception:
                    side = 0
                candidate["river_side"] = side
                if side == requested_side:
                    same_side.append(candidate)
            if same_side:
                candidates = same_side
                same_side_used = True
    candidates.sort(key=lambda item: (item["priority"], item["distance_m"], item["name"].casefold()))
    chosen = candidates[0]
    return {
        "name": chosen["name"], "category": chosen["category"],
        "distance_m": round(float(chosen["distance_m"]), 1),
        "lon": float(chosen["lon"]), "lat": float(chosen["lat"]),
        "source": "toponim", "same_river_side_preferred": bool(same_side_used),
        "requested_river_side": "left" if requested_side > 0 else "right" if requested_side < 0 else None,
    }


_geocode_lock = threading.Lock()
_last_geocode_request = 0.0


@lru_cache(maxsize=128)
def _nominatim_search_cached(query_text: str) -> tuple[dict[str, Any], ...]:
    global _last_geocode_request
    with _geocode_lock:
        elapsed = time.monotonic() - _last_geocode_request
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        params = {
            "q": query_text,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
            "countrycodes": "id",
            "viewbox": f"{_minlon},{_maxlat},{_maxlon},{_minlat}",
            "bounded": 1,
        }
        req = urllib.request.Request(
            NOMINATIM_URL + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept-Language": "id,en;q=0.8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Nominatim tidak dapat diakses: {exc}") from exc
        finally:
            _last_geocode_request = time.monotonic()

    clean: list[dict[str, Any]] = []
    for item in payload:
        try:
            lon = float(item["lon"])
            lat = float(item["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        display_name = str(item.get("display_name", ""))
        clean.append({
            "display_name": display_name,
            "name": str(item.get("name") or (display_name.split(",")[0] if display_name else query_text)),
            "lon": lon,
            "lat": lat,
            "type": str(item.get("type", "")),
            "category": str(item.get("category", item.get("class", ""))) or "OpenStreetMap",
            "source": "osm",
            "source_label": "OpenStreetMap",
        })
    return tuple(clean)


app = FastAPI(title="Penelusuran Banjir API", version=APP_VERSION)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif request.url.path == "/":
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="spatial.html",
        context={
            "map_assets_public_base": MAP_ASSETS_PUBLIC_BASE or "",
            "map_assets_version": MAP_ASSETS_VERSION or APP_VERSION,
        },
    )


@app.get("/api/info")
def info():
    return {
        "app_version": APP_VERSION,
        "mode": "routing_only",
        "data_backend": DATA_BACKEND,
        "reference_metadata": REFERENCE_METADATA,
        "official_rivers": int(len(official_rivers)),
        "official_basins": int(len(official_basins)),
        "bounds_wgs84": [float(v) for v in _web_bounds],
        "max_control_points": 10,
        "default_river_search_radius_m": DEFAULT_RIVER_SEARCH_RADIUS_M,
        "reference_layers_display_only": True,
        "hec_routing": hec_routing_info(),
        "reference_backend_metrics": get_reference_backend_metrics() if DATA_BACKEND == "r2" else None,
    }


def _location_identity(
    *,
    lon: float,
    lat: float,
    snap_radius_m: float,
    model_id: str | None = None,
    element_id: str | None = None,
    snapped_lon: float | None = None,
    snapped_lat: float | None = None,
) -> dict[str, Any]:
    """Display/name context; identity follows snap, name preserves clicked bank."""
    ref_lon = float(snapped_lon) if snapped_lon is not None else lon
    ref_lat = float(snapped_lat) if snapped_lat is not None else lat
    x, y = to_reference.transform(ref_lon, ref_lat)
    pt = Point(x, y)
    return {
        "official_basin": official_basin_at_point(pt),
        "official_river": nearest_official_river(pt, max_distance_m=snap_radius_m),
        "toponym": nearest_settlement_toponym(
            lon, lat, model_id=model_id, element_id=element_id,
            snapped_lon=snapped_lon, snapped_lat=snapped_lat,
        ),
        "reference_layers_display_only": True,
    }


@app.get("/api/location-check")
def location_check(
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    snap_radius_m: float = Query(DEFAULT_RIVER_SEARCH_RADIUS_M, gt=0, le=20_000),
    model_id: str | None = Query(default=None),
    element_id: str | None = Query(default=None),
    snapped_lon: float | None = Query(default=None, ge=-180, le=180),
    snapped_lat: float | None = Query(default=None, ge=-90, le=90),
):
    return _location_identity(
        lon=float(lon), lat=float(lat), snap_radius_m=float(snap_radius_m),
        model_id=model_id, element_id=element_id,
        snapped_lon=snapped_lon, snapped_lat=snapped_lat,
    )


@app.get("/api/map-assets/{asset_key}")
def map_asset(asset_key: str, proxy: bool = Query(default=False)):
    assets = {
        "official-basins": "official_basins.geojson",
        "official-rivers-z6-8": "official_rivers_z6_8.geojson",
        "official-rivers-z8-10": "official_rivers_z8_10.geojson",
        "official-rivers-z10-11": "official_rivers_z10_11.geojson",
        "official-rivers-z11-12": "official_rivers_z11_12.geojson",
        "official-rivers-z12-14": "official_rivers_z12_14.geojson",
        "official-rivers": "official_rivers.geojson",
    }
    filename = assets.get(asset_key)
    if not filename:
        raise HTTPException(status_code=404, detail="Map asset tidak ditemukan.")

    if MAP_ASSETS_PUBLIC_BASE and not proxy:
        suffix = f"?v={urllib.parse.quote(MAP_ASSETS_VERSION)}" if MAP_ASSETS_VERSION else ""
        return RedirectResponse(
            f"{MAP_ASSETS_PUBLIC_BASE}/{filename}{suffix}",
            status_code=307,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    local_path = STATIC_DIR / "data" / filename
    if not local_path.exists():
        cache_dir = Path(tempfile.gettempdir()) / "flood-routing-map-assets"
        cache_dir.mkdir(parents=True, exist_ok=True)
        local_path = cache_dir / filename
        if asset_key == "official-basins":
            frame = official_basins.copy().to_crs(CRS_WEB)
        else:
            frame = official_rivers.copy()
            name_col = "river_name" if "river_name" in frame.columns else ("NAMOBJ" if "NAMOBJ" in frame.columns else None)
            if name_col:
                frame["river_label"] = frame[name_col].map(_river_map_label)
            tier = RIVER_DISPLAY_TIER_BY_FILENAME.get(filename)
            frame = build_river_display_gdf(frame, tier) if tier is not None else frame.to_crs(CRS_WEB)
        local_path.write_text(frame.to_json(drop_id=True), encoding="utf-8")
    return FileResponse(local_path, media_type="application/geo+json", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/basin-labels")
def basin_labels():
    return _build_basin_label_fc()


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=2, max_length=160)):
    text = " ".join(q.strip().split())
    if len(text) < 2:
        raise HTTPException(status_code=400, detail="Kata pencarian terlalu pendek.")
    try:
        osm_results = list(_nominatim_search_cached(text))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"query": text, "results": osm_results[:5], "sources": {"openstreetmap": len(osm_results)}}


@app.get("/api/hec-routing/info")
def hec_routing_metadata():
    return hec_routing_info()


@app.get("/api/hec-routing/reaches")
def hec_routing_reaches(response: Response, scenario: str | None = Query(default=None)):
    response.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400, stale-while-revalidate=604800"
    return hec_reaches_geojson(scenario)


@app.get("/api/hec-routing/modeled-rivers")
def hec_routing_modeled_rivers(
    response: Response,
    scenario: str | None = Query(default=None),
    tier: str | None = Query(default=None),
):
    # Display tiers are immutable precomputed geometry.  Let the browser and
    # Vercel edge retain each tier instead of invoking the R2-backed runtime on
    # every zoom threshold crossing.
    response.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400, stale-while-revalidate=604800"
    return hec_modeled_rivers_geojson(scenario, tier)


@app.get("/api/hec-routing/modeled-area")
def hec_routing_modeled_area(scenario: str | None = Query(default=None)):
    return hec_modeled_area_geojson(scenario)


@app.get("/api/hec-routing/series")
def hec_routing_series(
    reach_ids: str | None = Query(default=None),
    scenario: str | None = Query(default=None),
    duration_hours: Literal[6, 12, 24] = Query(default=12),
):
    selected = [item.strip() for item in (reach_ids or "").split(",") if item.strip()]
    try:
        return selected_reach_series(selected or None, scenario, duration_hours)
    except DssParserUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "dss_parser_unavailable", "message": str(exc)}) from exc
    except DssReadError as exc:
        raise HTTPException(status_code=500, detail={"code": "dss_read_error", "message": str(exc)}) from exc


@app.post("/api/hec-routing/series")
def hec_routing_series_post(payload: HecSeriesRequest):
    try:
        return selected_reach_series(payload.reach_ids or None, payload.scenario, payload.duration_hours)
    except DssParserUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "dss_parser_unavailable", "message": str(exc)}) from exc
    except DssReadError as exc:
        raise HTTPException(status_code=500, detail={"code": "dss_read_error", "message": str(exc)}) from exc


@app.post("/api/hec-routing/snap")
def hec_routing_snap(payload: HecObservationRequest):
    result = hec_snap_points(
        [item.model_dump() for item in payload.points],
        radius_m=payload.snap_radius_m,
        scenario_id=payload.scenario,
    )
    if not payload.include_identity:
        return result

    # A separate /api/location-check round trip was previously made after every
    # successful snap.  On Vercel that extra cross-region request is noticeable
    # even after the worker is warm.  Enrich the already-computed snap response
    # in the same request instead.
    requested_by_id = {str(item.point_id): item for item in payload.points if item.point_id}
    for item in result.get("points", []):
        requested = requested_by_id.get(str(item.get("point_id")))
        if requested is None:
            continue
        item["identity"] = _location_identity(
            lon=float(requested.lon),
            lat=float(requested.lat),
            snap_radius_m=float(payload.snap_radius_m),
            model_id=str(item.get("model_id") or "") or None,
            element_id=str(item.get("element_id") or "") or None,
            snapped_lon=float(item["snapped_lon"]),
            snapped_lat=float(item["snapped_lat"]),
        )
    return result


@app.post("/api/hec-routing/export-hydrograph.xlsx")
def hec_routing_export_hydrograph_xlsx(payload: HecObservationRequest):
    if not payload.points:
        raise HTTPException(status_code=400, detail={"code": "no_control_points", "message": "Tambahkan minimal satu Titik Kontrol sebelum mengunduh hidrograf."})
    try:
        result = hec_observe_points(
            [item.model_dump() for item in payload.points],
            radius_m=payload.snap_radius_m,
            scenario_id=payload.scenario,
            duration_hours=payload.duration_hours,
        )
    except DssParserUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "dss_parser_unavailable", "message": str(exc)}) from exc
    except DssReadError as exc:
        raise HTTPException(status_code=500, detail={"code": "dss_read_error", "message": str(exc)}) from exc

    scenario_meta = next((item for item in hec_routing_info().get("scenarios", []) if str(item.get("id")) == str(result.get("scenario"))), {})
    labels = [str(item.get("label") or item.get("point_id") or "Titik Kontrol") for item in result.get("points", [])]
    sheet_name_by_id = {str(item.point_id): str(item.sheet_name or "").strip() for item in payload.points if item.point_id}
    sheet_names = [sheet_name_by_id.get(str(item.get("point_id")), "") for item in result.get("points", [])]
    workbook = build_hydrograph_xlsx(
        result,
        return_period_years=scenario_meta.get("return_period_years"),
        scenario_label=scenario_meta.get("label") or result.get("scenario"),
        duration_hours=payload.duration_hours,
        sheet_names=sheet_names,
    )
    filename = hydrograph_filename(
        labels,
        return_period_years=scenario_meta.get("return_period_years"),
        scenario_label=scenario_meta.get("label") or result.get("scenario"),
        duration_hours=payload.duration_hours,
    )
    encoded = urllib.parse.quote(filename, safe="")
    return Response(
        content=workbook,
        media_type=xlsx_mime_type(),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/hec-routing/observe")
def hec_routing_observe(payload: HecObservationRequest):
    try:
        return hec_observe_points(
            [item.model_dump() for item in payload.points],
            radius_m=payload.snap_radius_m,
            scenario_id=payload.scenario,
            duration_hours=payload.duration_hours,
        )
    except DssParserUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "dss_parser_unavailable", "message": str(exc)}) from exc
    except DssReadError as exc:
        raise HTTPException(status_code=500, detail={"code": "dss_read_error", "message": str(exc)}) from exc


@app.get("/api/health")
def health():
    # The shell starts this request in the background.  On R2 deployments,
    # overlap display/topology downloads now so opening the routing controls or
    # placing the first point does not serialize those transfers.
    hec_prewarm_display_objects()
    return {
        "status": "ok",
        "mode": "routing_only",
        "data_backend": DATA_BACKEND,
        "hec_routing": hec_routing_info(),
    }
