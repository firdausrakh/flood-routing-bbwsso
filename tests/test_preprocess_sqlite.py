from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from scripts import preprocess_hms


def _write_demo_basin(path: Path) -> None:
    path.write_text(
        "Basin: Demo\n"
        "Subbasin: S1\n  Downstream: J1\nEnd:\n"
        "Junction: J1\n  Downstream: R1\nEnd:\n"
        "Reach: R1\n  Downstream: OUT\n  From Canvas X: 0\n  From Canvas Y: 0\nEnd:\n"
        "Sink: OUT\nEnd:\n",
        encoding="utf-8",
    )


def _write_spatial_sqlite(path: Path, *, include_flowpath: bool = True) -> None:
    subbasins = gpd.GeoDataFrame(
        [{"ElementName": "S1", "geometry": Polygon([(-100, -100), (1100, -100), (1100, 1100), (-100, 1100), (-100, -100)])}],
        crs="EPSG:3857",
    )
    reaches = gpd.GeoDataFrame(
        [{"ElementName": "R1", "geometry": LineString([(0, 0), (1000, 0)])}],
        crs="EPSG:3857",
    )
    flowpaths = gpd.GeoDataFrame(
        [{"ElementName": "S1", "geometry": LineString([(500, 900), (200, 200), (0, 0)])}],
        crs="EPSG:3857",
    )
    layers = [("Watershed_Polygons", subbasins), ("Routing_Lines", reaches)]
    if include_flowpath:
        layers.append(("Computed_Longest_Flow_Path", flowpaths))
    for index, (layer, frame) in enumerate(layers):
        frame.to_file(path, layer=layer, driver="SQLite", append=index > 0)


def test_process_reads_only_basin_sqlite_and_return_period_dss_contract(tmp_path, monkeypatch):
    source = tmp_path / "data" / "source" / "Demo"
    source.mkdir(parents=True)
    _write_demo_basin(source / "Demo.basin")
    _write_spatial_sqlite(source / "Demo.sqlite")

    # --skip-dss is intentionally used here so the spatial/source contract can
    # be tested without requiring pydsstools in this unit test.
    hms_root = tmp_path / "data" / "hms"
    monkeypatch.setattr(preprocess_hms, "HMS_ROOT", hms_root)
    meta = preprocess_hms.process(source, skip_dss=True)

    assert meta["counts"]["reaches"] == 1
    assert meta["counts"]["subbasins"] == 1
    assert meta["counts"]["longest_flowpaths"] == 1
    manifest = json.loads((hms_root / "Demo" / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["basin"] == "Demo.basin"
    assert manifest["sqlite"] == "Demo.sqlite"
    assert manifest["sqlite_layers"]["reaches"]
    assert manifest["sqlite_layers"]["subbasins"]
    assert manifest["sqlite_layers"]["longest_flowpath"]
    assert "reaches" not in manifest or not str(manifest.get("reaches", "")).endswith(".shp")


def test_process_allows_sqlite_without_longest_flowpath(tmp_path, monkeypatch):
    source = tmp_path / "data" / "source" / "Demo"
    source.mkdir(parents=True)
    _write_demo_basin(source / "Demo.basin")
    _write_spatial_sqlite(source / "Demo.sqlite", include_flowpath=False)

    hms_root = tmp_path / "data" / "hms"
    monkeypatch.setattr(preprocess_hms, "HMS_ROOT", hms_root)
    meta = preprocess_hms.process(source, skip_dss=True)

    assert meta["counts"]["longest_flowpaths"] == 0
    manifest = json.loads((hms_root / "Demo" / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["sqlite_layers"]["longest_flowpath"] is None
    assert any("snapping fallback hanya memakai reach" in item for item in manifest["warnings"])


def test_source_contract_rejects_legacy_gis_files(tmp_path):
    source = tmp_path / "Demo"
    source.mkdir()
    _write_demo_basin(source / "Demo.basin")
    (source / "Demo.sqlite").write_bytes(b"")
    (source / "legacy_reaches.shp").write_bytes(b"")

    with pytest.raises(ValueError, match="hanya boleh berisi"):
        preprocess_hms.validate_source_folder(source, skip_dss=True)


def test_source_contract_requires_four_digit_return_period_name(tmp_path):
    source = tmp_path / "Demo"
    source.mkdir()
    _write_demo_basin(source / "Demo.basin")
    (source / "Demo.sqlite").write_bytes(b"")
    (source / "T_2.dss").write_bytes(b"")

    with pytest.raises(ValueError, match="T_xxxx.dss"):
        preprocess_hms.validate_source_folder(source)
