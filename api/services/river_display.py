from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import geopandas as gpd
import pandas as pd

RIVER_SIMPLIFY_CRS = "EPSG:32749"


@dataclass(frozen=True)
class RiverDisplayTier:
    key: str
    filename: str
    min_zoom: float
    max_zoom: float | None
    tolerance_m: float
    allowed_orders: tuple[int, ...] | None


RIVER_DISPLAY_TIERS: tuple[RiverDisplayTier, ...] = (
    # Six display levels. Orders 1 + 2 intentionally enter together at z6.5.
    RiverDisplayTier(
        key="z6-8",
        filename="official_rivers_z6_8.geojson",
        min_zoom=6.5,
        max_zoom=8.5,
        tolerance_m=300.0,
        allowed_orders=(1, 2),
    ),
    RiverDisplayTier(
        key="z8-10",
        filename="official_rivers_z8_10.geojson",
        min_zoom=8.5,
        max_zoom=10.5,
        tolerance_m=150.0,
        allowed_orders=(1, 2),
    ),
    RiverDisplayTier(
        key="z10-11",
        filename="official_rivers_z10_11.geojson",
        min_zoom=10.5,
        max_zoom=11.5,
        tolerance_m=75.0,
        allowed_orders=(1, 2, 3),
    ),
    RiverDisplayTier(
        key="z11-12",
        filename="official_rivers_z11_12.geojson",
        min_zoom=11.5,
        max_zoom=12.5,
        tolerance_m=35.0,
        allowed_orders=(1, 2, 3),
    ),
    RiverDisplayTier(
        key="z12-14",
        filename="official_rivers_z12_14.geojson",
        min_zoom=12.5,
        max_zoom=14.0,
        tolerance_m=12.0,
        allowed_orders=None,
    ),
    RiverDisplayTier(
        key="full",
        filename="official_rivers.geojson",
        min_zoom=14.0,
        max_zoom=None,
        tolerance_m=0.0,
        allowed_orders=None,
    ),
)

RIVER_DISPLAY_TIER_BY_KEY = {tier.key: tier for tier in RIVER_DISPLAY_TIERS}
RIVER_DISPLAY_TIER_BY_FILENAME = {tier.filename: tier for tier in RIVER_DISPLAY_TIERS}


def _numeric_order(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def build_river_display_gdf(
    rivers: gpd.GeoDataFrame,
    tier: RiverDisplayTier,
    *,
    web_crs: str = "EPSG:4326",
    simplify_crs: str = RIVER_SIMPLIFY_CRS,
) -> gpd.GeoDataFrame:
    """Build a display-only river GeoDataFrame for one map zoom tier.

    The source dataset is never modified. Simplification is performed in a metric
    projected CRS and only affects the public map asset, never hydrologic/runtime
    analysis data.
    """
    frame = rivers.copy()
    if frame.crs is None:
        raise RuntimeError("CRS jaringan sungai tidak terdefinisi; generalisasi display dibatalkan.")

    order_col = "river_order_int" if "river_order_int" in frame.columns else "river_order"
    if tier.allowed_orders is not None:
        if order_col not in frame.columns:
            raise RuntimeError("Kolom river_order/river_order_int tidak tersedia untuk membuat tier sungai.")
        allowed = set(int(v) for v in tier.allowed_orders)
        values = _numeric_order(frame[order_col])
        frame = frame.loc[values.isin(allowed)].copy()

    if tier.tolerance_m > 0 and not frame.empty:
        metric = frame.to_crs(simplify_crs)
        metric.geometry = metric.geometry.simplify(float(tier.tolerance_m), preserve_topology=True)
        metric = metric.loc[metric.geometry.notna() & ~metric.geometry.is_empty].copy()
        frame = metric.to_crs(web_crs)
    else:
        frame = frame.to_crs(web_crs)

    return frame


def river_display_asset_keys() -> Iterable[str]:
    return (tier.key for tier in RIVER_DISPLAY_TIERS)
