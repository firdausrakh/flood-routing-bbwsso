from __future__ import annotations

import gzip
import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon

from api.services import hec_routing
from scripts.preprocess_hms import convolved_duration_flow, modeled_area, muskingum_cunge_route, parse_basin


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _feature(geometry, **properties):
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def _install_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "local")
    hms = tmp_path / "data" / "hms"
    model = hms / "Oyo"
    scenario_dir = model / "scenarios"
    scenario_dir.mkdir(parents=True)

    # Pre-oriented upstream -> downstream. R1 runs west -> east, while S1 is a
    # Subbasin-named reach2d centerline draining toward R1.
    _write_json(model / "reaches.geojson", _fc([
        _feature({"type": "LineString", "coordinates": [[110.000, -7.000], [110.010, -7.000]]},
                 model_id="Oyo", route_id="Oyo:R1", element_id="R1", element_type="reach",
                 reach_id="R1", subbasin_id=None, downstream_reach_id=None, route_len_m=1100.0, reach_len_m=1100.0),
        _feature({"type": "LineString", "coordinates": [[110.000, -7.010], [110.010, -7.010]]},
                 model_id="Oyo", route_id="Oyo:S1", element_id="S1", element_type="subbasin",
                 reach_id=None, subbasin_id="S1", downstream_reach_id="R1", route_len_m=1100.0, reach_len_m=None),
    ]))
    _write_json(model / "subbasins.geojson", _fc([
        _feature({"type": "Polygon", "coordinates": [[[109.99, -7.02], [110.02, -7.02], [110.02, -6.99], [109.99, -6.99], [109.99, -7.02]]]},
                 model_id="Oyo", subbasin_id="S1"),
    ]))
    _write_json(model / "modeled_area.geojson", _fc([
        _feature({"type": "Polygon", "coordinates": [[[109.99, -7.02], [110.02, -7.02], [110.02, -6.99], [109.99, -6.99], [109.99, -7.02]]]}, model_id="Oyo"),
    ]))
    _write_json(model / "model_rivers.geojson", _fc([]))
    _write_json(model / "topology.json", {
        "schema_version": 1,
        "nodes": {
            "S1": {"type": "subbasin", "downstream": "J1", "upstream": []},
            "J1": {"type": "junction", "downstream": "R1", "upstream": ["S1"]},
            "R1": {"type": "reach", "downstream": "Sink-1", "upstream": ["J1"]},
            "Sink-1": {"type": "sink", "downstream": None, "upstream": ["R1"]},
        },
    })

    flow = {
        "schema_version": 3,
        "dataset_id": "T_0002",
        "return_period_years": 2,
        "parameter": "FLOW",
        "units": "M3/S",
        "interval": "5Minute",
        "times": ["0", "1", "2"],
        "reaches": {"R1": [1.0, 10.0, 5.0]},
        "reach_peaks": {"R1": 10.0},
        "reach_peak_indices": {"R1": 1},
        "reach_inflows": {"R1": [0.5, 8.0, 4.0]},
        "reach_inflow_peaks": {"R1": 8.0},
        "reach_inflow_peak_indices": {"R1": 1},
        "reach_inflow_sources": {"R1": ["R1/FLOW-COMBINE"]},
        "reach_inflow_modes": {"R1": "dss_reach_flow_combine"},
        "subbasins": {"S1": [0.0, 4.0, 8.0]},
        "subbasin_peaks": {"S1": 8.0},
        "subbasin_peak_indices": {"S1": 2},
        "duration_subbasins": {
            "12": {"S1": [0.0, 2.0, 4.0]},
            "24": {"S1": [0.0, 1.0, 2.0]},
        },
        "duration_reaches": {
            "12": {"R1": [0.5, 5.0, 2.5]},
            "24": {"R1": [0.25, 2.5, 1.25]},
        },
        "duration_reach_inflows": {
            "12": {"R1": [0.25, 4.0, 2.0]},
            "24": {"R1": [0.125, 2.0, 1.0]},
        },
        "junctions": {},
        "junction_peaks": {},
    }
    with gzip.open(scenario_dir / "T_0002.flow.json.gz", "wt", encoding="utf-8") as fp:
        json.dump(flow, fp)

    meta = {
        "schema_version": 2,
        "id": "Oyo",
        "name": "Oyo",
        "path": "Oyo",
        "reaches": "reaches.geojson",
        "routing_lines": "reaches.geojson",
        "subbasins": "subbasins.geojson",
        "modeled_area": "modeled_area.geojson",
        "model_rivers": "model_rivers.geojson",
        "topology": "topology.json",
        "default_scenario": "T_0002",
        "scenarios": [{"id": "T_0002", "return_period_years": 2, "label": "2 Tahun", "flow": "scenarios/T_0002.flow.json.gz", "ready": True}],
    }
    _write_json(model / "model.json", meta)
    _write_json(hms / "index.json", {"schema_version": 1, "models": [meta]})

    monkeypatch.setattr(hec_routing, "HMS_DIR", hms)
    monkeypatch.setattr(hec_routing, "INDEX_PATH", hms / "index.json")
    for fn in (hec_routing.registry, hec_routing._model_fc, hec_routing._context, hec_routing._flow):
        fn.cache_clear()
    return hms


def test_basin_parser_builds_explicit_topology(tmp_path):
    basin = tmp_path / "Oyo.basin"
    basin.write_text(
        "Basin: Oyo\n"
        "Subbasin: S1\n  Downstream: J1\nEnd:\n"
        "Subbasin: S2\n  Downstream: J1\nEnd:\n"
        "Junction: J1\n  Downstream: R1\nEnd:\n"
        "Reach: R1\n  Downstream: J2\n  From Canvas X: 100\n  From Canvas Y: 200\nEnd:\n"
        "Sink: J2\nEnd:\n",
        encoding="utf-8",
    )
    topo = parse_basin(basin)
    assert topo["counts"] == {"subbasin": 2, "junction": 1, "reach": 1, "sink": 1}
    assert topo["nodes"]["S1"]["downstream"] == "J1"
    assert topo["nodes"]["J1"]["upstream"] == ["S1", "S2"]
    assert topo["nodes"]["R1"]["upstream"] == ["J1"]
    assert topo["first_downstream_reach"]["S1"] == "R1"


def test_modeled_area_repairs_invalid_subbasin_geometry():
    bad = Polygon([(110.0, -7.0), (110.02, -7.02), (110.0, -7.02), (110.02, -7.0), (110.0, -7.0)])
    gdf = gpd.GeoDataFrame([{"model_id": "Oyo", "subbasin_id": "S1", "geometry": bad}], geometry="geometry", crs="EPSG:4326")
    area = modeled_area(gdf, "Oyo")
    assert len(area) == 1
    assert not area.geometry.iloc[0].is_empty
    assert area.geometry.iloc[0].is_valid


def test_duration_convolution_stretches_rainfall_and_preserves_depth():
    twelve_hours = convolved_duration_flow([2.0, 0.0], [1.0, 0.5], [0.0], 12, 3)
    twenty_four_hours = convolved_duration_flow([2.0, 0.0], [1.0, 0.5], [0.0], 24, 5)

    assert twelve_hours == [1.0, 1.5, 0.5]
    assert twenty_four_hours == [0.5, 0.75, 0.75, 0.75, 0.25]


def test_muskingum_cunge_preserves_steady_flow_without_calibration():
    routing = {
        "method": "Muskingum Cunge", "channel": "Trapezoid",
        "index_parameter_type": "Index Celerity", "index_celerity_mps": 1.52,
        "energy_slope": 0.01, "mannings_n": 0.05,
        "bottom_width_m": 10.0, "side_slope": 3.0,
    }
    routed = muskingum_cunge_route([25.0] * 12, routing, 1_000.0, 5)

    assert all(abs(value - 25.0) < 1e-9 for value in routed)


def test_runtime_exposes_reach_and_subbasin_routing_series(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    assert hec_routing.available_scenarios()[0]["label"] == "2 Tahun"
    payload = hec_routing.selected_reach_series(["Oyo:R1", "Oyo:S1"], "T_0002")
    assert payload["reaches"]["Oyo:R1"] == [1.0, 10.0, 5.0]
    assert payload["reaches"]["Oyo:S1"] == [0.0, 4.0, 8.0]
    assert payload["route_types"] == {"Oyo:R1": "reach", "Oyo:S1": "subbasin"}
    assert payload["reach_peak_indices"] == {"Oyo:R1": 1, "Oyo:S1": 2}


def test_snap_uses_reach2d_for_reach_and_subbasin_without_longest_flowpath(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    reach = hec_routing.snap_points([{"point_id": "P1", "lon": 110.005, "lat": -7.0001}], radius_m=300, scenario_id="T_0002")
    assert reach["point_count"] == 1
    assert reach["points"][0]["snap_type"] == "routing_line"
    assert reach["points"][0]["element_type"] == "reach"

    sub = hec_routing.snap_points([{"point_id": "P2", "lon": 110.005, "lat": -7.0099}], radius_m=300, scenario_id="T_0002")
    assert sub["point_count"] == 1
    assert sub["points"][0]["snap_type"] == "routing_line"
    assert sub["points"][0]["element_type"] == "subbasin"
    assert sub["points"][0]["series_type"] == "subbasin"
    assert sub["points"][0]["series_id"] == "S1"


def test_reach_snap_selects_inflow_upstream_and_outflow_downstream(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    upstream = hec_routing.observe_points([
        {"point_id": "P1", "label": "Hulu", "lon": 110.001, "lat": -7.00005},
    ], radius_m=300, scenario_id="T_0002")["points"][0]
    downstream = hec_routing.observe_points([
        {"point_id": "P2", "label": "Hilir", "lon": 110.009, "lat": -7.00005},
    ], radius_m=300, scenario_id="T_0002")["points"][0]

    assert upstream["series_type"] == "reach_inflow"
    assert upstream["series_role"] == "inflow"
    assert upstream["position_role"] == "hulu"
    assert upstream["series"] == [0.5, 8.0, 4.0]
    assert upstream["peak_q"] == 8.0
    assert upstream["series_derivation"] == "dss_reach_flow_combine"
    assert upstream["series_source_elements"] == ["R1/FLOW-COMBINE"]

    assert downstream["series_type"] == "reach_outflow"
    assert downstream["series_role"] == "outflow"
    assert downstream["position_role"] == "hilir"
    assert downstream["series"] == [1.0, 10.0, 5.0]
    assert downstream["peak_q"] == 10.0
    assert downstream["series_derivation"] == "dss_flow_outflow"


def test_subbasin_reach2d_route_uses_subbasin_flow(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    payload = hec_routing.observe_points([
        {"point_id": "P1", "label": "Hulu", "lon": 110.005, "lat": -7.0099},
    ], radius_m=300, scenario_id="T_0002")
    point = payload["points"][0]
    assert point["series"] == [0.0, 4.0, 8.0]
    assert point["peak_q"] == 8.0
    assert point["peak_index"] == 2


def test_subbasin_duration_uses_precomputed_unit_graph_convolution(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    payload = hec_routing.observe_points([
        {"point_id": "P1", "label": "Hulu", "lon": 110.005, "lat": -7.0099},
    ], radius_m=300, scenario_id="T_0002", duration_hours=12)
    point = payload["points"][0]

    assert point["series"][:3] == [0.0, 2.0, 4.0]
    assert point["peak_q"] == 4.0
    assert point["series_derivation"] == "dss_unit_graph_convolution"


def test_snap_returns_distinct_modeled_area_and_radius_errors(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    too_far = hec_routing.snap_points([{"point_id": "P1", "lon": 110.005, "lat": -7.005}], radius_m=100, scenario_id="T_0002")
    assert too_far["point_count"] == 0
    assert too_far["errors"][0]["code"] == "no_flowpath"
    assert too_far["errors"][0]["radius_m"] == 100

    outside = hec_routing.snap_points([{"point_id": "P2", "lon": 111.0, "lat": -8.0}], radius_m=300, scenario_id="T_0002")
    assert outside["point_count"] == 0
    assert outside["errors"][0]["code"] == "outside_modeled_area"


def test_sqlite_selector_uses_reach2d_names_for_reach_and_subbasin(monkeypatch):
    from shapely.geometry import LineString, Polygon
    from scripts import preprocess_hms

    topo = {
        "nodes": {
            "R1": {"type": "reach"},
            "S1": {"type": "subbasin"},
        }
    }
    layers = {
        "reach": gpd.GeoDataFrame(columns=["name", "geometry"], geometry="geometry", crs="EPSG:32749"),
        "reach2d": gpd.GeoDataFrame([
            {"name": "R1", "geometry": LineString([(400000, 9100000), (400100, 9099900)])},
            {"name": "R999", "geometry": LineString([(401000, 9100000), (401100, 9099900)])},
            {"name": "S1", "geometry": LineString([(402000, 9100000), (402100, 9099900)])},
        ], geometry="geometry", crs="EPSG:32749"),
        "subbasin2d": gpd.GeoDataFrame([
            {"name": "S1", "geometry": Polygon([(399900, 9100100), (400200, 9100100), (400200, 9099800), (399900, 9099800)])},
        ], geometry="geometry", crs="EPSG:32749"),
        "longest_flowpath": gpd.GeoDataFrame([
            {"subbasin": "S1", "geometry": LineString([(399950, 9100050), (400100, 9099900)])},
        ], geometry="geometry", crs="EPSG:32749"),
    }
    monkeypatch.setattr(preprocess_hms, "sqlite_spatial_layers", lambda _: list(layers))
    monkeypatch.setattr(preprocess_hms.gpd, "read_file", lambda _path, layer=None: layers[layer].copy())

    spatial = preprocess_hms.load_sqlite_spatial(Path("Oyo.sqlite"), topo)
    assert spatial["layers"] == {"routing_lines": "reach2d", "subbasins": "subbasin2d"}
    assert spatial["routing_lines"]["name"].tolist() == ["R1", "S1"]
    assert "longest_flowpath" not in spatial


def test_flood_routing_uses_existing_docked_panel_sync_function():
    js = (Path(__file__).resolve().parents[1] / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")
    assert "syncWindowLaunchers(" not in js
    assert "function syncDockedPanels()" in js


def test_preprocess_preserves_strahler_and_orients_routing_lines_upstream_to_downstream():
    from shapely.geometry import LineString
    from scripts.preprocess_hms import prep_routing_lines, stream_order_base_width

    topo = {
        "nodes": {
            "R1": {"type": "reach", "downstream": None, "from_canvas_x": None, "from_canvas_y": None},
            "S1": {"type": "subbasin", "downstream": "R1", "from_canvas_x": None, "from_canvas_y": None},
        },
        "first_downstream_reach": {"R1": None, "S1": "R1"},
    }
    routes = gpd.GeoDataFrame([
        {"name": "R1", "strmorder": 2, "upstream_x": 400000, "upstream_y": 9100000, "dnstream_x": 400100, "dnstream_y": 9099900,
         "geometry": LineString([(400100, 9099900), (400000, 9100000)])},
        {"name": "S1", "strmorder": 4, "upstream_x": 401000, "upstream_y": 9100000, "dnstream_x": 401100, "dnstream_y": 9099900,
         "geometry": LineString([(401100, 9099900), (401000, 9100000)])},
    ], geometry="geometry", crs="EPSG:32749")

    web, source = prep_routing_lines(routes, topo, "Oyo")
    assert web.crs.to_epsg() == 4326
    assert source["element_type"].tolist() == ["reach", "subbasin"]
    assert source["strmorder"].tolist() == [2, 4]
    assert source["base_width"].tolist() == [stream_order_base_width(2), stream_order_base_width(4)]
    assert list(source.iloc[0].geometry.coords)[0][:2] == (400000.0, 9100000.0)
    assert list(source.iloc[1].geometry.coords)[0][:2] == (401000.0, 9100000.0)


def test_flood_routing_restores_previous_five_classes_without_line_animation():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")
    html = (root / "templates" / "spatial.html").read_text(encoding="utf-8")
    css = (root / "static" / "css" / "spatial.css").read_text(encoding="utf-8")

    assert "0.20, 1.0, 0.50, 1.2, 0.85, 1.5" in js
    assert "feature-state', 'falling" in js
    assert "REACH_FALLING_LAYER" in js
    assert "REACH_MOTION_LAYER, false" in js
    assert "flowMotion" not in js
    assert "q-baseflow" in html
    assert "q-rising" in html
    assert "q-near-peak" in html
    assert "q-peak" in html
    assert "q-falling" in html
    assert "Debit awal / rendah" in html
    assert "Rising Limb" in html
    assert "Mendekati Puncak" in html
    assert "Peak Discharge / Puncak" in html
    assert "Falling Limb / Resesi" in html
    assert "Semua garis solid tanpa animasi" in html
    assert ".flood-legend-swatch.q-falling" in css

def test_observation_pair_is_order_independent_on_same_reach(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    payload = hec_routing.observe_points([
        {"point_id": "HILIR", "label": "Hilir dulu", "lon": 110.009, "lat": -7.00005},
        {"point_id": "HULU", "label": "Hulu belakangan", "lon": 110.001, "lat": -7.00005},
    ], radius_m=300, scenario_id="T_0002")
    assert len(payload["segments"]) == 1
    segment = payload["segments"][0]
    assert segment["from_point_id"] == "HULU"
    assert segment["to_point_id"] == "HILIR"
    assert segment["is_downstream_path"] is True
    assert segment["distance_m"] > 0


def test_topology_selection_uses_downstream_boundary_and_all_contributing_headwaters(monkeypatch):
    ctx = {
        "route_by_id": {
            "S1": {"route_id": "Oyo:S1", "length_m": 100.0},
            "S2": {"route_id": "Oyo:S2", "length_m": 110.0},
            "R1": {"route_id": "Oyo:R1", "length_m": 200.0},
            "R2": {"route_id": "Oyo:R2", "length_m": 300.0},
        },
        "next_route": {"S1": "R1", "S2": "R1", "R1": "R2"},
        "upstream_routes": {"R1": ["S1", "S2"], "R2": ["R1"]},
    }
    monkeypatch.setattr(hec_routing, "_context", lambda _model_id: ctx)
    s1 = {"model_id": "Oyo", "point_id": "P1", "element_id": "S1", "chainage_m": 50.0, "route_id": "Oyo:S1"}
    r2 = {"model_id": "Oyo", "point_id": "P2", "element_id": "R2", "chainage_m": 120.0, "chainage_fraction": 0.4, "route_id": "Oyo:R2"}
    selection = hec_routing._routing_selection([r2, s1])
    assert selection["downstream_point_id"] == "P2"
    assert selection["downstream_route_id"] == "Oyo:R2"
    assert selection["downstream_chainage_fraction"] == 0.4
    assert set(selection["route_ids"]) == {"Oyo:S1", "Oyo:S2", "Oyo:R1", "Oyo:R2"}


def test_separate_headwater_branches_before_confluence_are_not_directly_traceable(monkeypatch):
    ctx = {
        "route_by_id": {
            "S1": {"route_id": "Oyo:S1", "length_m": 100.0},
            "S2": {"route_id": "Oyo:S2", "length_m": 100.0},
            "R1": {"route_id": "Oyo:R1", "length_m": 200.0},
        },
        "next_route": {"S1": "R1", "S2": "R1"},
        "upstream_routes": {"R1": ["S1", "S2"]},
    }
    monkeypatch.setattr(hec_routing, "_context", lambda _model_id: ctx)
    a = {"model_id": "Oyo", "point_id": "A", "element_id": "S1", "chainage_m": 40.0}
    b = {"model_id": "Oyo", "point_id": "B", "element_id": "S2", "chainage_m": 40.0}
    relation = hec_routing._direct_pair(a, b)
    assert relation["connected"] is False
    assert relation["reason"] == "separate_upstream_branches"
    selection = hec_routing._routing_selection([a, b])
    assert selection["available"] is False
    assert selection["reason"] == "separate_upstream_branches"


def test_frontend_has_idle_summary_wide_flow_hover_and_delineasi_style_add_modal():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")
    html = (root / "templates" / "spatial.html").read_text(encoding="utf-8")
    spatial = (root / "static" / "js" / "spatial.js").read_text(encoding="utf-8")
    assert "REACH_HIT_LAYER" in js and "line-width': ['max', 36" in js
    assert "Debit saat waktu t" in js and "Waktu t" in js
    assert "FLOW-COMBINE · Inflow" not in js and "FLOW · Outflow" not in js
    assert "inspectIdleLocation" in js and "floodIdleHydrograph" in js
    assert 'id="floodAddPointModal"' in html and '>Tambahkan</button>' in html
    assert "Seri hidrograf" not in js
    assert "bukan jalur hilir langsung" not in js
    assert "setFloodOfficialRiversTemporarilyHidden" in spatial
    assert "esri-dark-gray" in spatial and "lastLightBasemap" in spatial and "darkBasemapChanged" in spatial
    assert "modeledRiverUrl" in spatial and "tier: String(tier || 'full')" in spatial


def test_no_runtime_or_preprocess_dependency_on_longest_flowpath():
    root = Path(__file__).resolve().parents[1]
    service = (root / "api" / "services" / "hec_routing.py").read_text(encoding="utf-8")
    preprocess = (root / "scripts" / "preprocess_hms.py").read_text(encoding="utf-8")
    assert "_nearest_flowpath" not in service
    assert "flow_by_sub" not in service
    assert 'spatial["longest_flowpath"]' not in preprocess
    assert 'out/"longest_flowpath.geojson"' not in preprocess


def test_dss_contract_uses_reach_flow_outflow_and_reach_flow_combine_inflow():
    root = Path(__file__).resolve().parents[1]
    preprocess = (root / "scripts" / "preprocess_hms.py").read_text(encoding="utf-8")
    service = (root / "api" / "services" / "hec_routing.py").read_text(encoding="utf-8")
    assert '/FLOW-COMBINE//' in preprocess
    assert '_reach_upstream_inflow_candidates' not in preprocess
    assert 'dss_reach_flow_combine' in preprocess
    assert 'derived_upstream_sum' not in preprocess
    assert 'fallback_reach_outflow' not in preprocess
    assert 'missing_dss_flow_combine' in service
    assert 'dss_flow_outflow' in service


def test_extract_dss_reads_flow_combine_from_same_reach_for_reach_inflow(tmp_path, monkeypatch):
    from scripts import preprocess_hms

    class FakeTs:
        def __init__(self, values):
            self.values = values
            self.times = ["0", "1", "2"]

    calls = []
    series = {
        "//R1/FLOW//5Minute/RUN:T=0002/": [1.0, 10.0, 5.0],
        "//R1/FLOW-COMBINE//5Minute/RUN:T=0002/": [0.5, 8.0, 4.0],
        "//S1/FLOW//5Minute/RUN:T=0002/": [0.0, 4.0, 8.0],
        "//J1/FLOW//5Minute/RUN:T=0002/": [0.25, 6.0, 3.0],
        "//S1/FLOW-UNIT GRAPH/TS-PATTERN/5Minute/RUN:T=0002/": [1.0, 0.5],
        "//S1/PRECIP-EXCESS//5Minute/RUN:T=0002/": [2.0, 0.0, 0.0],
        "//S1/FLOW-BASE//5Minute/RUN:T=0002/": [0.0, 0.0, 0.0],
    }

    class FakeFile:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read_ts(self, pathname, **_kwargs):
            calls.append(pathname)
            if pathname not in series:
                raise KeyError(pathname)
            return FakeTs(series[pathname])

    def fake_open(_path, mode="r"):
        return FakeFile()

    def fake_meta(_path, parameter="FLOW"):
        if parameter == "FLOW-COMBINE":
            return {"parameter": parameter, "interval": "5Minute", "version": "RUN:T=0002", "elements": ["R1"], "available": True}
        return {"parameter": parameter, "interval": "5Minute", "version": "RUN:T=0002", "elements": ["J1", "R1", "S1"], "available": True}

    monkeypatch.setattr(preprocess_hms, "pydss_open", lambda: fake_open)
    monkeypatch.setattr(preprocess_hms, "dss_meta", fake_meta)
    topo = {
        "nodes": {
            "R1": {
                "type": "reach", "upstream": ["J1"], "length_m": 456.0,
                "routing": {
                    "energy_slope": 0.01, "mannings_n": 0.05,
                    "bottom_width_m": 10.0, "side_slope": 3.0,
                    "index_celerity_mps": 1.52,
                },
            },
            "S1": {"type": "subbasin", "upstream": []},
            "J1": {"type": "junction", "upstream": ["S1"]},
        }
    }

    payload = preprocess_hms.extract_dss(tmp_path / "T_0002.dss", topo)
    assert payload["reaches"]["R1"] == [1.0, 10.0, 5.0]
    assert payload["reach_inflows"]["R1"] == [0.5, 8.0, 4.0]
    assert payload["reach_inflow_modes"]["R1"] == "dss_reach_flow_combine"
    assert payload["reach_inflow_sources"]["R1"] == ["R1/FLOW-COMBINE"]
    assert "//R1/FLOW-COMBINE//5Minute/RUN:T=0002/" in calls
    # Junction FLOW remains a separate series; Reach inflow is read directly
    # from the same Reach FLOW-COMBINE record.
    assert payload["junctions"]["J1"] == [0.25, 6.0, 3.0]


def test_v221_frontend_state_rules_and_pinned_chart_readouts():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")
    spatial = (root / "static" / "js" / "spatial.js").read_text(encoding="utf-8")
    html = (root / "templates" / "spatial.html").read_text(encoding="utf-8")
    css = (root / "static" / "css" / "spatial.css").read_text(encoding="utf-8")

    assert "const hideForRouting = state.routingVisualizationVisible && state.selectedReachIds.length > 0" in js
    assert "hideForComparison" not in js
    assert "['max', 36" in js
    assert "scheduleReachHoverClear(120)" in js
    assert "text: 'Jam ke-'" in js
    assert "Jam ke-${escapeHtml" in js
    assert "tooltip: { enabled: false }" in js
    assert 'id="floodChartHoverReadout"' in html
    assert 'id="floodChartBackdrop"' in html
    assert 'maxlength="25"' in html
    assert "Math.round(distance)" in js
    assert "fractionDigitsForMagnitude" in js
    assert "'line-opacity': 0" in js
    assert "chartPortalPlaceholder" in js
    assert "Maksimal 25 karakter" in js
    assert "width:min(568px" in css
    assert ".flood-right-open .flood-bottom-bar" in css
    assert "fetchRiverAsset(next)" in spatial
    assert "$('autoRiverZoom')?.checked === false ? 'full'" in spatial


def test_modeled_river_tiers_generalize_only_clipped_model_network(tmp_path, monkeypatch):
    _install_runtime(tmp_path, monkeypatch)
    model_rivers = tmp_path / "data" / "hms" / "Oyo" / "model_rivers.geojson"
    _write_json(model_rivers, _fc([
        _feature({"type": "LineString", "coordinates": [[110.000, -7.000], [110.010, -7.000]]}, model_id="Oyo", river_name="Kali Main", river_order=1),
        _feature({"type": "LineString", "coordinates": [[110.000, -7.005], [110.010, -7.005]]}, model_id="Oyo", river_name="Trib2", river_order=2),
        _feature({"type": "LineString", "coordinates": [[110.000, -7.010], [110.010, -7.010]]}, model_id="Oyo", river_name="Trib3", river_order=3),
        _feature({"type": "LineString", "coordinates": [[110.000, -7.015], [110.010, -7.015]]}, model_id="Oyo", river_name="Trib4", river_order=4),
    ]))
    hec_routing._model_fc.cache_clear()
    hec_routing._modeled_rivers_geojson_cached.cache_clear()

    full = hec_routing.modeled_rivers_geojson("T_0002", "full")
    low_zoom = hec_routing.modeled_rivers_geojson("T_0002", "z6-8")
    assert len(full["features"]) == 4
    assert full["features"][0]["properties"]["river_name"] == "Main"
    assert full["features"][0]["properties"]["river_label"] == "K. Main"
    assert {int(f["properties"]["river_order"]) for f in low_zoom["features"]} == {1, 2}
    # No external/national feature can appear: output model ids all come from
    # the already-clipped model_rivers file installed above.
    assert {f["properties"]["model_id"] for f in low_zoom["features"]} == {"Oyo"}


def test_v220_basemap_state_tracks_light_restore_and_dark_manual_override():
    spatial = (Path(__file__).resolve().parents[1] / "static" / "js" / "spatial.js").read_text(encoding="utf-8")
    assert "lastLightBasemap = currentBasemap" in spatial
    assert "darkBasemapChanged = false" in spatial
    assert "currentBasemap = 'esri-dark-gray'" in spatial
    assert "currentBasemap = darkBasemapChanged ? currentBasemap : lastLightBasemap" in spatial
    assert "if (dark) darkBasemapChanged = true" in spatial
    assert "if (darkBasemapChanged) lastLightBasemap = currentBasemap" in spatial


def test_v221_folder_rename_safe_launchers_and_r2_contract():
    root = Path(__file__).resolve().parents[1]
    run_bat = (root / "run.bat").read_text(encoding="utf-8")
    pre_bat = (root / "preprocess_hms.bat").read_text(encoding="utf-8")
    r2_bat = (root / "preprocess_hms_r2.bat").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    backend = (root / "api" / "services" / "hms_backend.py").read_text(encoding="utf-8")
    reference = (root / "api" / "services" / "reference_backend.py").read_text(encoding="utf-8")

    for text in (run_bat, pre_bat, r2_bat):
        assert "%~dp0" in text
        assert "flood-routing-work" not in text.lower()
    assert "activate.bat" not in run_bat.lower()
    assert "activate.bat" not in pre_bat.lower()
    assert "R2_RUNTIME_BUCKET=flood-routing" in env_example
    assert "R2_REFERENCE_BUCKET=dta-map-assets" in env_example
    assert "R2_DOWNLOAD_WORKERS=4" in env_example
    assert "R2_HMS_MANIFEST_KEY=" in env_example and "R2_REFRESH_CACHE=0" in env_example
    assert "ensure_hms_object" in backend and "R2_HMS_PREFIX" in backend
    assert "tcp_keepalive=True" in backend and "_object_lock" in backend
    assert 'R2_REFERENCE_BUCKET", "dta-map-assets"' in reference
    assert "official_basins.geojson" in reference and "official_rivers.geojson" in reference
    assert "_download_many" in reference
    assert 'call "%ROOT%preprocess_hms.bat"' not in r2_bat
    assert r"scripts\upload_hms_r2.py" in r2_bat
    assert r"data\hms\index.json" in r2_bat


def test_v221_hover_reuses_popup_and_add_point_follows_map():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")
    css = (root / "static" / "css" / "spatial.css").read_text(encoding="utf-8")
    assert "Reuse one popup while crossing reaches" in js
    assert "map.on('mousemove', e =>" in js
    assert "map.on('move', positionAddPointDialog)" in js
    assert "nameInput.maxLength = MAX_NAME_LENGTH" in js
    assert ".flood-add-point-modal::after{display:none" in css
    assert "Jam ke-${escapeHtml" in js


def test_hms_r2_backend_lazy_download_uses_flood_routing_bucket(tmp_path, monkeypatch):
    from api.services import hms_backend

    calls = []

    class FakeClient:
        def download_file(self, bucket, key, target):
            calls.append((bucket, key))
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"schema_version":1,"models":[]}', encoding="utf-8")

    monkeypatch.setenv("DATA_BACKEND", "r2")
    monkeypatch.setenv("FLOOD_HMS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("R2_RUNTIME_BUCKET", "flood-routing")
    monkeypatch.setenv("R2_HMS_PREFIX", "")
    monkeypatch.setattr(hms_backend, "_client", lambda: FakeClient())
    hms_backend._runtime_manifest.cache_clear()

    path = hms_backend.ensure_hms_object("index.json")
    assert path.exists()
    assert calls == [("flood-routing", "index.json")]
    # A second lookup is a local cache hit and must not download again.
    assert hms_backend.ensure_hms_object("index.json") == path
    assert len(calls) == 1


def test_hydrograph_xlsx_uses_one_sheet_per_control_point_and_en_dash_filename():
    import io
    import zipfile

    from api.services.hydrograph_export import build_hydrograph_xlsx, hydrograph_filename

    payload = {
        "interval": "5Minute",
        "points": [
            {"point_id": "A", "label": "Kali Progo – Kranggan", "snapped_lat": -7.339961, "snapped_lon": 110.209919, "series": [1, 2, 3]},
            {"point_id": "B", "label": "Kali Progo – Srumbung", "snapped_lat": -7.1, "snapped_lon": 110.1, "series": [4, 5, 6]},
        ],
    }
    blob = build_hydrograph_xlsx(
        payload,
        return_period_years=25,
        scenario_label="25 Tahun",
        sheet_names=["Kranggan", "Srumbung"],
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        sheet1 = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        sheet2 = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        assert 'name="Kranggan"' in workbook
        assert 'name="Srumbung"' in workbook
        assert "Debit Banjir Kala Ulang 25 Tahun Kali Progo – Kranggan" in sheet1
        assert "Kali Progo – Kranggan" in sheet1
        assert "Kali Progo – Srumbung" in sheet2
        assert "-7.339961, 110.209919" in sheet1
        assert '<c r="A2" s="4"><v>0.0</v></c>' in sheet1
        assert '<c r="A3" s="4"><v>0.003472222222222222</v></c>' in sheet1
        assert "mergeCells" not in sheet1
        assert 'numFmtId="20"' in archive.read("xl/styles.xml").decode("utf-8")
    assert hydrograph_filename(
        ["Kali Progo – Kranggan", "Kali Progo – Srumbung"],
        return_period_years=25,
    ) == "Debit Banjir Kala Ulang 25 Tahun Kali Progo – Kranggan dkk.xlsx"
    assert hydrograph_filename(
        ["Kali Progo – Kranggan"],
        return_period_years=25,
    ) == "Debit Banjir Kala Ulang 25 Tahun Kali Progo – Kranggan.xlsx"
