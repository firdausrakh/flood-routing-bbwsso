"""Runtime reader for preprocessed HEC-HMS flood-routing data.

Raw HMS exports are stored in ``data/source/<model>``. They are converted once
by ``scripts/preprocess_hms.py`` into ``data/hms/<model>``. Runtime API requests
read *only* ``data/hms``.

Snap inside modeled coverage uses the nearest HMS ``reach2d`` routing centerline
within the threshold.  The precomputed line layer can represent both Reach and
Subbasin elements through its ``element_type``/``element_id`` properties.

For Reach elements the snapped chainage determines which hydrograph is used:
- upstream half of the reach -> inflow from FLOW-COMBINE on the same Reach;
- downstream half -> Reach OUTFLOW from FLOW on the Reach itself.
Subbasin centerlines use the Subbasin FLOW/outflow series.
"""
from __future__ import annotations

from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
import gzip
import json
import math
import os

import geopandas as gpd
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from api.services.river_display import RIVER_DISPLAY_TIER_BY_KEY, build_river_display_gdf
from api.services.hms_backend import backend_name as hms_backend_name, backend_metrics as hms_backend_metrics, ensure_hms_object, hms_root


class DssParserUnavailable(RuntimeError):
    pass


class DssReadError(RuntimeError):
    pass


ROOT_DIR = Path(__file__).resolve().parents[2]
HMS_DIR = hms_root()
INDEX_PATH = HMS_DIR / "index.json"


def _materialize(path: Path) -> Path:
    """Materialize a path relative to the storage-neutral HMS root."""
    if hms_backend_name() == "local":
        return path
    try:
        rel = path.relative_to(HMS_DIR)
    except ValueError:
        return path
    return ensure_hms_object(rel)
DEFAULT_SNAP_RADIUS_M = 300.0


def _json(path: Path) -> dict[str, Any]:
    try:
        path = _materialize(path)
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _empty_fc() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


@lru_cache(maxsize=1)
def registry() -> dict[str, Any]:
    payload = _json(INDEX_PATH)
    if isinstance(payload.get("models"), list):
        return payload
    models = []
    if HMS_DIR.exists():
        for d in sorted(p for p in HMS_DIR.iterdir() if p.is_dir()):
            meta = _json(d / "model.json")
            if meta:
                meta.setdefault("id", d.name)
                meta.setdefault("path", d.name)
                models.append(meta)
    return {"schema_version": 1, "models": models}


def _models() -> list[dict[str, Any]]:
    return [x for x in registry().get("models", []) if isinstance(x, dict) and x.get("id")]


def prewarm_display_objects() -> None:
    """Materialize only the small GeoJSON/topology needed by the map and snap.

    This runs behind the existing background health warm-up.  It deliberately
    excludes scenario flow files, which can be much larger and are only needed
    after a user actually adds a control point.
    """
    if hms_backend_name() != "r2":
        return
    refs: set[Path] = set()
    for model in _models():
        root = _model_dir(model)
        for key, fallback in (
            ("routing_lines", str(model.get("reaches") or "reaches.geojson")),
            ("modeled_area", "modeled_area.geojson"),
            ("model_rivers", "model_rivers.geojson"),
            ("topology", "topology.json"),
        ):
            refs.add((root / str(model.get(key) or fallback)).relative_to(HMS_DIR))
    if not refs:
        return
    workers = max(1, min(int(os.getenv("R2_DOWNLOAD_WORKERS", "4")), len(refs)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hms-display") as pool:
        pending = [pool.submit(ensure_hms_object, ref) for ref in refs]
        for future in as_completed(pending):
            future.result()


def _model(model_id: str) -> dict[str, Any] | None:
    return next((x for x in _models() if str(x.get("id")) == str(model_id)), None)


def _model_dir(model: dict[str, Any]) -> Path:
    return HMS_DIR / str(model.get("path") or model["id"])


def _scenario(model: dict[str, Any], scenario_id: str | None) -> dict[str, Any] | None:
    items = [s for s in model.get("scenarios", []) if isinstance(s, dict)]
    if scenario_id:
        found = next((s for s in items if str(s.get("id")) == str(scenario_id)), None)
        if found:
            return found
        return None
    default = str(model.get("default_scenario") or "")
    return next((s for s in items if str(s.get("id")) == default), None) or (items[0] if items else None)


def available_scenarios() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for model in _models():
        for s in model.get("scenarios", []) or []:
            if not isinstance(s, dict) or not s.get("id") or s.get("ready") is False:
                continue
            sid = str(s["id"])
            rec = merged.setdefault(sid, {
                "id": sid,
                "return_period_years": s.get("return_period_years"),
                "label": s.get("label") or sid,
                "model_ids": [],
            })
            rec["model_ids"].append(str(model["id"]))
    return sorted(merged.values(), key=lambda x: (x.get("return_period_years") is None, x.get("return_period_years") or 0, x["id"]))


def default_scenario_id() -> str | None:
    scenarios = available_scenarios()
    return str(scenarios[0]["id"]) if scenarios else None


def _fc(path: Path) -> dict[str, Any]:
    obj = _json(path)
    return obj if obj.get("type") == "FeatureCollection" else _empty_fc()


@lru_cache(maxsize=64)
def _model_fc(model_id: str, key: str, fallback: str) -> dict[str, Any]:
    model = _model(model_id)
    if not model:
        return _empty_fc()
    return _fc(_model_dir(model) / str(model.get(key) or fallback))


@lru_cache(maxsize=8)
def reaches_geojson(scenario_id: str | None = None) -> dict[str, Any]:
    """Return HMS reach2d routing centerlines (Reach + Subbasin elements)."""
    features = []
    for model in _models():
        if scenario_id and not _scenario(model, scenario_id):
            continue
        mid = str(model["id"])
        for feature in _model_fc(mid, "routing_lines", str(model.get("reaches") or "reaches.geojson")).get("features", []):
            f = dict(feature)
            props = dict(f.get("properties") or {})
            element_id = str(props.get("element_id") or props.get("reach_id") or props.get("subbasin_id") or props.get("name") or "")
            element_type = str(props.get("element_type") or ("subbasin" if props.get("subbasin_id") else "reach"))
            props.update(model_id=mid, element_id=element_id, element_type=element_type, route_id=str(props.get("route_id") or f"{mid}:{element_id}"))
            if element_type == "reach": props.setdefault("reach_id", element_id)
            if element_type == "subbasin": props.setdefault("subbasin_id", element_id)
            f["properties"] = props
            f["id"] = props["route_id"]
            features.append(f)
    return {"type": "FeatureCollection", "features": features}

@lru_cache(maxsize=48)
def _modeled_rivers_geojson_cached(scenario_id: str | None, tier_key: str) -> dict[str, Any]:
    """Return rivers clipped to the HMS modeled area, optionally generalized.

    ``model_rivers.geojson`` is already clipped during preprocessing.  Runtime
    display tiers therefore simplify/filter *that clipped network*, never the
    national reference layer, so rivers outside the modeled coverage cannot
    leak back into the map at lower zoom levels.
    """
    features: list[dict[str, Any]] = []
    for model in _models():
        if scenario_id and not _scenario(model, scenario_id):
            continue
        mid = str(model["id"])
        for feature in _model_fc(mid, "model_rivers", "model_rivers.geojson").get("features", []):
            f = dict(feature)
            props = dict(f.get("properties") or {})
            props.setdefault("model_id", mid)
            f["properties"] = props
            features.append(f)

    fc = {"type": "FeatureCollection", "features": features}
    tier = RIVER_DISPLAY_TIER_BY_KEY.get(str(tier_key or "full").replace("official-rivers-", "").replace("z6_8", "z6-8"))
    if not tier or tier.key == "full" or not features:
        return fc
    try:
        frame = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        display = build_river_display_gdf(frame, tier, web_crs="EPSG:4326")
        return json.loads(display.to_json(drop_id=True))
    except Exception:
        # Display generalization is cosmetic. Never make the routing UI fail if
        # a legacy asset misses an expected order field or has odd geometry.
        return fc


def modeled_rivers_geojson(scenario_id: str | None = None, tier: str | None = None) -> dict[str, Any]:
    key = str(tier or "full").strip().lower() or "full"
    return _modeled_rivers_geojson_cached(scenario_id, key)


def modeled_area_geojson(scenario_id: str | None = None) -> dict[str, Any]:
    features = []
    for model in _models():
        if scenario_id and not _scenario(model, scenario_id):
            continue
        features.extend(_model_fc(str(model["id"]), "modeled_area", "modeled_area.geojson").get("features", []))
    return {"type": "FeatureCollection", "features": features}


@lru_cache(maxsize=64)
def _flow(model_id: str, scenario_id: str) -> dict[str, Any]:
    model = _model(model_id)
    if not model:
        raise DssReadError(f"Model {model_id} tidak tersedia.")
    scenario = _scenario(model, scenario_id)
    if not scenario:
        raise DssReadError(f"Kala ulang {scenario_id} tidak tersedia untuk {model_id}.")
    path = _model_dir(model) / str(scenario.get("flow") or f"scenarios/{scenario_id}.flow.json.gz")
    try:
        path = _materialize(path)
    except Exception as exc:
        raise DssReadError(str(exc)) from exc
    if not path.exists():
        raise DssReadError(f"Hasil preprocessing belum tersedia: {path}")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fp:
            payload = json.load(fp)
    except Exception as exc:
        raise DssReadError(f"Gagal membaca {path.name}: {exc}") from exc
    return payload




def _width_classes(values: list[Any], peak: float) -> list[int]:
    """Map Q/Qp to ten deterministic thickness classes (0..9)."""
    p = float(peak or 0.0)
    if p <= 0:
        return [0 for _ in values]
    out: list[int] = []
    for raw in values:
        try:
            q = max(0.0, float(raw))
        except (TypeError, ValueError):
            q = 0.0
        ratio = min(1.0, q / p)
        out.append(min(9, int(ratio * 10.0)))
    return out

def all_reach_series(
    scenario_id: str | None = None,
    reach_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Return Q(t) for every routing centerline keyed by route_id.

    ``reaches`` remains the display/outflow series (Reach FLOW + Subbasin FLOW).
    Reach ``FLOW-COMBINE`` is exposed separately through ``reach_inflows`` so
    hover/inspection UI can show inflow and outflow together without changing
    the Q/Qp styling contract.
    """
    sid = scenario_id or default_scenario_id()
    if not sid:
        raise DssReadError("Belum ada hasil preprocessing kala ulang pada data/hms.")
    times: list[Any] = []
    routes: dict[str, list[Any]] = {}
    peaks: dict[str, float] = {}
    peak_indices: dict[str, int | None] = {}
    route_types: dict[str, str] = {}
    width_classes: dict[str, list[int]] = {}
    inflows: dict[str, list[Any]] = {}
    inflow_peaks: dict[str, float] = {}
    inflow_peak_indices: dict[str, int | None] = {}
    interval, units = "5Minute", "M3/S"
    for model in _models():
        if not _scenario(model, sid):
            continue
        mid = str(model["id"])
        try:
            payload = _flow(mid, sid)
        except DssReadError:
            continue
        if len(payload.get("times") or []) > len(times):
            times = list(payload.get("times") or [])
            interval = str(payload.get("interval") or interval)
            units = str(payload.get("units") or units)
        for kind, series_key, peak_key, index_key in (
            ("reach", "reaches", "reach_peaks", "reach_peak_indices"),
            ("subbasin", "subbasins", "subbasin_peaks", "subbasin_peak_indices"),
        ):
            for eid, vals in (payload.get(series_key) or {}).items():
                key = f"{mid}:{eid}"
                if reach_ids is not None and key not in reach_ids:
                    continue
                routes[key] = vals
                peaks[key] = float((payload.get(peak_key) or {}).get(eid) or 0.0)
                peak_indices[key] = (payload.get(index_key) or {}).get(eid)
                route_types[key] = kind
                width_classes[key] = _width_classes(list(vals or []), peaks[key])
        for eid, vals in (payload.get("reach_inflows") or {}).items():
            key = f"{mid}:{eid}"
            if reach_ids is not None and key not in reach_ids:
                continue
            inflows[key] = vals
            inflow_peaks[key] = float((payload.get("reach_inflow_peaks") or {}).get(eid) or 0.0)
            inflow_peak_indices[key] = (payload.get("reach_inflow_peak_indices") or {}).get(eid)
    if not routes:
        raise DssReadError(f"Hasil routing {sid} belum tersedia.")
    return {
        "dataset_id": sid, "scenario": sid, "parameter": "FLOW", "inflow_parameter": "FLOW-COMBINE",
        "units": units, "interval": interval, "times": times,
        "reaches": routes, "reach_peaks": peaks, "reach_peak_indices": peak_indices,
        "reach_width_classes": width_classes,
        "width_class_count": 10,
        "width_class_breaks_q_qp": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        "reach_inflows": inflows, "reach_inflow_peaks": inflow_peaks,
        "reach_inflow_peak_indices": inflow_peak_indices,
        "route_types": route_types, "source": "precomputed_hms",
    }


def selected_reach_series(reach_ids: list[str] | None = None, scenario_id: str | None = None) -> dict[str, Any]:
    if not reach_ids:
        return all_reach_series(scenario_id)
    wanted = {str(x).strip() for x in reach_ids if str(x).strip()}
    payload = all_reach_series(scenario_id, wanted)
    # The map derives its visual state from Q/Qp and does not consume the
    # parallel integer arrays. Keep them only on the legacy all-series response.
    payload.pop("reach_width_classes", None)
    payload.pop("width_class_count", None)
    payload.pop("width_class_breaks_q_qp", None)
    payload["selected_reach_count"] = len(payload["reaches"])
    return payload

def catalog() -> dict[str, Any]:
    return {"mapping_complete": bool(_models()), "reach_count": len(reaches_geojson().get("features", [])), "route_count": len(reaches_geojson().get("features", [])),
            "primary_parameter": "FLOW", "primary_interval": "5Minute", "dataset_id": default_scenario_id(),
            "scenarios": available_scenarios()}


def routing_info() -> dict[str, Any]:
    scenarios = available_scenarios()
    return {"mode": "precomputed_hms", "model_count": len(_models()),
            "models": [{"id": str(x["id"]), "name": x.get("name") or x["id"]} for x in _models()],
            "reach_count": len(reaches_geojson().get("features", [])), "route_count": len(reaches_geojson().get("features", [])), "scenarios": scenarios,
            "default_scenario": default_scenario_id(), "series_ready": bool(scenarios),
            "raw_source_read_at_runtime": False, "spatial_role": "reach2d_reach_and_subbasin_centerlines",
            "data_backend": hms_backend_name(), "storage_metrics": hms_backend_metrics()}


@lru_cache(maxsize=32)
def _context(model_id: str) -> dict[str, Any]:
    model = _model(model_id)
    if not model:
        return {}
    root = _model_dir(model)
    route_path = root / str(model.get("routing_lines") or model.get("reaches") or "reaches.geojson")
    topology_path = root / str(model.get("topology") or "topology.json")
    try:
        route_path = _materialize(route_path)
        topology_path = _materialize(topology_path)
    except Exception:
        pass
    try:
        topology = json.loads(topology_path.read_text(encoding="utf-8")) if topology_path.exists() else {}
    except Exception:
        topology = {}
    nodes = topology.get("nodes") or {}
    area_fc = _model_fc(model_id, "modeled_area", "modeled_area.geojson")
    area_geoms = [shape(x["geometry"]) for x in area_fc.get("features", []) if x.get("geometry")]
    area = unary_union(area_geoms) if area_geoms else None
    routes = gpd.read_file(route_path) if route_path.exists() else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    crs = (routes.estimate_utm_crs() if not routes.empty else None) or "EPSG:3857"
    routes_pr = routes.to_crs(crs) if not routes.empty else routes
    route_by_id: dict[str, dict[str, Any]] = {}
    reach_by_id: dict[str, dict[str, Any]] = {}
    for ll, pr in zip(routes.itertuples(index=False), routes_pr.itertuples(index=False)):
        element_id = str(getattr(ll, "element_id", None) or getattr(ll, "reach_id", None) or getattr(ll, "subbasin_id", None) or getattr(ll, "name", ""))
        if not element_id:
            continue
        element_type = str(getattr(ll, "element_type", None) or ("subbasin" if getattr(ll, "subbasin_id", None) else "reach"))
        item = {
            "element_id": element_id,
            "element_type": element_type,
            "route_id": str(getattr(ll, "route_id", None) or f"{model_id}:{element_id}"),
            "ll": ll.geometry, "pr": pr.geometry,
            "length_m": float(getattr(ll, "route_len_m", None) or getattr(ll, "reach_len_m", None) or pr.geometry.length or 0),
            "downstream_element": str(getattr(ll, "downstream_element", None) or ""),
            "downstream_reach_id": str(getattr(ll, "downstream_reach_id", None) or ""),
        }
        route_by_id[element_id] = item
        if element_type == "reach":
            reach_by_id[element_id] = {**item, "reach_id": element_id}

    def next_routing_element(element_id: str) -> str | None:
        # .basin is the topology authority. Follow junction/source/sink nodes
        # until another reach2d element is encountered.
        if nodes:
            current = str((nodes.get(element_id) or {}).get("downstream") or "")
            seen = {element_id}
            while current and current not in seen:
                if current in route_by_id:
                    return current
                seen.add(current)
                current = str((nodes.get(current) or {}).get("downstream") or "")
            return None
        # Compatibility fallback for older precompute/test fixtures.
        fallback = str((route_by_id.get(element_id) or {}).get("downstream_reach_id") or "")
        return fallback if fallback in route_by_id and fallback != element_id else None

    next_route: dict[str, str] = {}
    upstream_routes: dict[str, list[str]] = {}
    for element_id in route_by_id:
        nxt = next_routing_element(element_id)
        if not nxt:
            continue
        next_route[element_id] = nxt
        upstream_routes.setdefault(nxt, []).append(element_id)
    return {
        "model": model, "area": area, "crs": crs, "topology": topology, "nodes": nodes,
        "route_by_id": route_by_id, "reach_by_id": reach_by_id,
        "next_route": next_route, "upstream_routes": upstream_routes,
    }

def _project_point(lon: float, lat: float, crs: Any) -> Point:
    return gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(crs).iloc[0]


def routing_flow_side(model_id: str, element_id: str, snapped_lon: float, snapped_lat: float, target_lon: float, target_lat: float) -> int:
    """Side of target relative to pre-oriented reach2d: +1 left, -1 right."""
    ctx = _context(str(model_id))
    item = (ctx.get("route_by_id") or {}).get(str(element_id))
    if not item:
        return 0
    line = item.get("pr")
    if line is None or line.is_empty or float(line.length or 0) <= 0:
        return 0
    snapped = _project_point(float(snapped_lon), float(snapped_lat), ctx["crs"])
    target = _project_point(float(target_lon), float(target_lat), ctx["crs"])
    offset = float(snapped.distance(target))
    if offset < 8.0:
        return 0
    station = float(line.project(snapped))
    delta = min(100.0, max(20.0, float(line.length) * 0.03))
    a_station, b_station = max(0.0, station - delta), min(float(line.length), station + delta)
    if b_station - a_station < 5.0:
        return 0
    a, b = line.interpolate(a_station), line.interpolate(b_station)
    dx, dy = float(b.x - a.x), float(b.y - a.y)
    vx, vy = float(target.x - snapped.x), float(target.y - snapped.y)
    tangent_len = math.hypot(dx, dy)
    if tangent_len <= 1e-9:
        return 0
    cross = dx * vy - dy * vx
    sin_angle = abs(cross) / max(1e-9, tangent_len * max(offset, 1e-9))
    if sin_angle < 0.08:
        return 0
    return 1 if cross > 0 else -1


def _contains(ctx: dict[str, Any], lon: float, lat: float) -> bool:
    area = ctx.get("area")
    return bool(area is not None and not area.is_empty and area.covers(Point(lon, lat)))


def _nearest_route(ctx: dict[str, Any], lon: float, lat: float, radius: float) -> dict[str, Any] | None:
    p = _project_point(lon, lat, ctx["crs"])
    best = None
    for item in ctx["route_by_id"].values():
        d = float(p.distance(item["pr"]))
        if d <= radius and (best is None or d < best[0]): best = (d, item)
    if not best: return None
    dist, item = best
    geom_len=float(item["pr"].length or 0.0)
    chain = float(item["pr"].project(p)); frac = chain / geom_len if geom_len else 0.0
    frac=max(0.0,min(1.0,frac))
    snap = item["ll"].interpolate(frac, normalized=True)
    element_type=item["element_type"]
    common={
        "model_id": str(ctx["model"]["id"]), "snap_type": "routing_line",
        "element_type": element_type, "element_id": item["element_id"], "route_id": item["route_id"],
        "chainage_m": chain, "route_length_m": item["length_m"], "chainage_fraction": frac,
        "remaining_route_m": max(0.0,item["length_m"]-chain), "snap_distance_m": dist,
        "snapped_lon": float(snap.x), "snapped_lat": float(snap.y),
    }
    if element_type == "reach":
        # Geometry is pre-oriented upstream -> downstream. Choose the endpoint
        # hydrograph closest to the clicked location. The upstream-half inflow
        # is precomputed from FLOW-COMBINE on this same Reach;
        # the downstream-half outflow is FLOW on the Reach itself.
        role="inflow" if frac < 0.5 else "outflow"
        return {**common, "reach_id": item["element_id"], "series_type": f"reach_{role}",
                "series_role": role, "series_id": item["element_id"],
                "position_role": "hulu" if role=="inflow" else "hilir"}
    return {**common, "subbasin_id": item["element_id"], "series_type": "subbasin",
            "series_role": "outflow", "series_id": item["element_id"],
            "position_role": "alur_subbasin", "downstream_reach_id": item["downstream_reach_id"]}

def snap_one(lon: float, lat: float, radius_m: float = DEFAULT_SNAP_RADIUS_M, scenario_id: str | None = None):
    containing = []
    for model in _models():
        ctx = _context(str(model["id"]))
        if ctx and _contains(ctx, lon, lat): containing.append(ctx)
    if not containing:
        return None, {"code": "outside_modeled_area", "message": "Data Pemodelan Banjir Kala Ulang Belum Tersedia"}
    containing = [c for c in containing if _scenario(c["model"], scenario_id)]
    if not containing:
        return None, {"code": "scenario_unavailable", "message": "Data Pemodelan Banjir Kala Ulang Belum Tersedia"}
    radius = max(1.0, float(radius_m or DEFAULT_SNAP_RADIUS_M))
    candidates = [x for x in (_nearest_route(c, lon, lat, radius) for c in containing) if x]
    if candidates: return min(candidates, key=lambda x: x["snap_distance_m"]), None
    return None, {"code": "no_flowpath", "radius_m": radius,
                  "message": f"Tidak ditemukan jalur aliran reach2d dalam radius {int(round(radius))} m. Pindah titik atau perbesar radius snapping pada Pengaturan Lanjutan."}

def snap_points(point_specs: list[dict[str, Any]], radius_m: float = DEFAULT_SNAP_RADIUS_M, scenario_id: str | None = None) -> dict[str, Any]:
    points, errors = [], []
    for i, spec in enumerate(point_specs or [], 1):
        try: lon, lat = float(spec.get("lon")), float(spec.get("lat"))
        except Exception: continue
        point_id = spec.get("point_id") or f"P{i}"; label = str(spec.get("label") or spec.get("name") or point_id)
        obs, err = snap_one(lon, lat, radius_m, scenario_id)
        if err: errors.append({"point_id": point_id, "input_lon": lon, "input_lat": lat, **err})
        else: points.append({"point_id": point_id, "label": label, "input_lon": lon, "input_lat": lat, **obs})
    return {"points": points, "errors": errors, "point_count": len(points), "scenario": scenario_id or default_scenario_id()}


def _interval_minutes(interval: Any) -> int:
    d = "".join(ch for ch in str(interval or "5Minute") if ch.isdigit()); return int(d or 5)


def _metrics(values: list[Any], times: list[Any], interval: Any) -> dict[str, Any]:
    clean = []
    for v in values:
        try: n = float(v)
        except Exception: n = math.nan
        clean.append(n if math.isfinite(n) else None)
    valid = [v for v in clean if v is not None]
    if not valid: return {"peak_q": None, "peak_index": None, "peak_time": None, "peak_minutes": None}
    peak = max(valid); idx = next((i for i, v in enumerate(clean) if v == peak), None)
    label = str(times[idx]).replace("T", " ").replace("Z", "") if idx is not None and idx < len(times) else None
    return {"peak_q": peak, "peak_index": idx, "peak_time": label, "peak_minutes": idx * _interval_minutes(interval) if idx is not None else None}


def _series(obs: dict[str, Any], sid: str) -> tuple[list[Any], dict[str, Any]]:
    payload = _flow(str(obs["model_id"]), sid)
    kind=str(obs.get("series_type") or "")
    if kind == "reach_inflow": group=payload.get("reach_inflows")
    elif kind == "subbasin": group=payload.get("subbasins")
    else: group=payload.get("reaches")
    return list((group or {}).get(str(obs.get("series_id")), [])), payload

def _route_distance(ctx: dict[str, Any], a_id: str, a_chain: float, b_id: str, b_chain: float) -> float | None:
    """Distance following the directed reach2d/.basin topology."""
    routes = ctx.get("route_by_id") or {}
    if a_id not in routes or b_id not in routes:
        return None
    a_len = max(0.0, float(routes[a_id].get("length_m") or 0.0))
    b_len = max(0.0, float(routes[b_id].get("length_m") or 0.0))
    a_chain = max(0.0, min(a_len, float(a_chain or 0.0)))
    b_chain = max(0.0, min(b_len, float(b_chain or 0.0)))
    if a_id == b_id:
        delta = b_chain - a_chain
        return delta if delta >= -1e-6 else None
    distance = max(0.0, a_len - a_chain)
    current = (ctx.get("next_route") or {}).get(a_id)
    seen = {a_id}
    while current and current not in seen and current in routes:
        seen.add(current)
        if current == b_id:
            return distance + b_chain
        distance += max(0.0, float(routes[current].get("length_m") or 0.0))
        current = (ctx.get("next_route") or {}).get(current)
    return None


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """Directed distance from ``a`` downstream to ``b`` independent of click order.

    Distinct upstream branches before their confluence are intentionally not
    considered a direct path to one another: neither route can reach the other.
    """
    if a.get("model_id") != b.get("model_id"):
        return None
    ctx = _context(str(a["model_id"]))
    return _route_distance(
        ctx,
        str(a.get("element_id") or ""), float(a.get("chainage_m") or 0.0),
        str(b.get("element_id") or ""), float(b.get("chainage_m") or 0.0),
    )


def _direct_pair(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Resolve a pair independent of insertion order and return upstream -> downstream."""
    ab = _distance(a, b)
    if ab is not None:
        upstream, downstream, distance = a, b, ab
    else:
        ba = _distance(b, a)
        if ba is None:
            return {
                "connected": False,
                "reason": "separate_upstream_branches",
                "upstream": a,
                "downstream": b,
                "distance_m": None,
            }
        upstream, downstream, distance = b, a, ba
    lag = None
    if upstream.get("peak_minutes") is not None and downstream.get("peak_minutes") is not None:
        lag = int(downstream["peak_minutes"] - upstream["peak_minutes"])
    return {
        "connected": True,
        "reason": None,
        "upstream": upstream,
        "downstream": downstream,
        "distance_m": float(distance),
        "peak_lag_minutes": lag,
    }


def _upstream_route_ids(ctx: dict[str, Any], boundary: dict[str, Any]) -> list[str]:
    """All reach2d routes contributing upstream of the downstream control point."""
    routes = ctx.get("route_by_id") or {}
    element_id = str(boundary.get("element_id") or "")
    if element_id not in routes:
        return []
    selected: set[str] = {element_id}
    stack = [element_id]
    reverse = ctx.get("upstream_routes") or {}
    while stack:
        current = stack.pop()
        for upstream in reverse.get(current, []):
            if upstream in selected or upstream not in routes:
                continue
            selected.add(upstream)
            stack.append(upstream)
    return sorted(str(routes[eid]["route_id"]) for eid in selected)


def _routing_selection(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {"available": False, "reason": "no_control_points", "route_ids": []}
    models = {str(p.get("model_id") or "") for p in points}
    if len(models) != 1:
        return {"available": False, "reason": "different_models", "route_ids": []}
    candidates = []
    for candidate in points:
        distances = []
        for other in points:
            if other is candidate:
                distances.append(0.0)
                continue
            d = _distance(other, candidate)
            if d is None:
                break
            distances.append(float(d))
        else:
            candidates.append((sum(distances), candidate))
    if not candidates:
        return {
            "available": False,
            "reason": "separate_upstream_branches",
            "message": "Titik Kontrol berada pada cabang hulu berbeda sebelum pertemuan sungai; tidak ada jalur aliran langsung di antara titik tersebut.",
            "route_ids": [],
        }
    _, boundary = max(candidates, key=lambda item: item[0])
    ctx = _context(str(boundary["model_id"]))
    route_ids = _upstream_route_ids(ctx, boundary)
    return {
        "available": bool(route_ids),
        "reason": None if route_ids else "topology_unavailable",
        "downstream_point_id": boundary.get("point_id"),
        "downstream_element_id": boundary.get("element_id"),
        "downstream_route_id": boundary.get("route_id"),
        "downstream_chainage_fraction": boundary.get("chainage_fraction"),
        "route_ids": route_ids,
    }

def observe_points(point_specs: list[dict[str, Any]], radius_m: float = DEFAULT_SNAP_RADIUS_M, scenario_id: str | None = None) -> dict[str, Any]:
    sid = scenario_id or default_scenario_id()
    if not sid:
        raise DssReadError("Belum ada kala ulang hasil preprocessing.")
    snapped = snap_points(point_specs, radius_m, sid); points = snapped["points"]
    times: list[Any] = []; interval = "5Minute"; units = "M3/S"
    for item in points:
        try: vals, payload = _series(item, sid)
        except DssReadError: vals, payload = [], {}
        item["series"] = vals
        if item.get("series_type") == "reach_inflow":
            item["series_derivation"] = (payload.get("reach_inflow_modes") or {}).get(str(item.get("series_id"))) or "missing_dss_flow_combine"
            item["series_source_elements"] = list((payload.get("reach_inflow_sources") or {}).get(str(item.get("series_id"))) or [])
            item["dss_parameter"] = "FLOW-COMBINE"
        elif item.get("series_type") == "reach_outflow":
            item["series_derivation"] = "dss_flow_outflow"
            item["dss_parameter"] = "FLOW"
        elif item.get("series_type") == "subbasin":
            item["series_derivation"] = "dss_subbasin_flow"
            item["dss_parameter"] = "FLOW"
        if len(payload.get("times") or []) > len(times):
            times = list(payload.get("times") or []); interval = str(payload.get("interval") or interval); units = str(payload.get("units") or units)
        item.update(_metrics(vals, list(payload.get("times") or times), payload.get("interval") or interval))

    # Build links from each selected point to its nearest selected downstream
    # neighbour. This makes the result independent of click order and naturally
    # handles a tree of selected controls. Separate sibling headwater branches
    # have no direct segment between them.
    segments = []
    for upstream in points:
        downstream_candidates = []
        for downstream in points:
            if downstream is upstream:
                continue
            distance = _distance(upstream, downstream)
            if distance is not None:
                downstream_candidates.append((float(distance), downstream))
        if not downstream_candidates:
            continue
        distance, downstream = min(downstream_candidates, key=lambda item: item[0])
        lag = None
        if upstream.get("peak_minutes") is not None and downstream.get("peak_minutes") is not None:
            lag = int(downstream["peak_minutes"] - upstream["peak_minutes"])
        segments.append({
            "from_point_id": upstream.get("point_id"), "from_label": upstream.get("label"),
            "to_point_id": downstream.get("point_id"), "to_label": downstream.get("label"),
            "distance_m": distance, "is_downstream_path": True, "reason": None,
            "peak_lag_minutes": lag, "input_order_reversed": False,
        })
    if len(points) == 2 and not segments:
        relation = _direct_pair(points[0], points[1])
        upstream, downstream = relation["upstream"], relation["downstream"]
        segments.append({
            "from_point_id": upstream.get("point_id"), "from_label": upstream.get("label"),
            "to_point_id": downstream.get("point_id"), "to_label": downstream.get("label"),
            "distance_m": relation.get("distance_m"),
            "is_downstream_path": bool(relation.get("connected")), "reason": relation.get("reason"),
            "peak_lag_minutes": relation.get("peak_lag_minutes"), "input_order_reversed": False,
        })
    routing_selection = _routing_selection(points)
    return {
        "dataset_id": sid, "scenario": sid, "units": units, "interval": interval, "times": times,
        "points": points, "errors": snapped.get("errors", []), "segments": segments,
        "routing_selection": routing_selection,
        "point_count": len(points), "source": "precomputed_hms",
    }
