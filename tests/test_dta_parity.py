from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DtaParityTests(unittest.TestCase):
    def test_official_rivers_remain_the_tiered_hec_model_clip(self):
        source = (ROOT / "static" / "js" / "spatial.js").read_text(encoding="utf-8")

        self.assertIn("function modeledRiverUrl", source)
        self.assertIn("/api/hec-routing/modeled-rivers", source)
        self.assertIn("const riverAssetCache = new Map()", source)
        self.assertIn("schedule(() => prefetchRiverTiers(next))", source)
        self.assertNotIn("data: mapAssetUrl(currentRiverAssetKey)", source)
        self.assertIn("const RIVER_ZOOM = { 1: 6.5, 2: 6.5, 3: 10.5, other: 12.5 }", source)
        self.assertIn("tolerance: 0", source)
        self.assertIn("maxzoom: 24", source)
        self.assertIn("buffer: 128", source)
        self.assertIn("lineMetrics: true", source)
        self.assertIn("'symbol-spacing': 220", source)
        self.assertIn("'symbol-sort-key': riverLabelSortKeyExpression()", source)
        self.assertIn("'text-max-angle': 70", source)

    def test_point_add_uses_dta_click_and_cancellation_contract(self):
        source = (ROOT / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")

        self.assertIn("const MAP_POINT_CLICK_DEBOUNCE_MS = 180", source)
        self.assertIn("state.snapAbortController.abort()", source)
        self.assertIn("scheduleObservationAt(e.lngLat.lng, e.lngLat.lat)", source)
        self.assertIn("map.on('dblclick'", source)
        self.assertIn("include_identity: true", source)

    def test_snap_preview_matches_dta_requested_snapped_and_connector_style(self):
        source = (ROOT / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")

        self.assertIn("'line-color': '#596779'", source)
        self.assertIn("'line-width': 1.6", source)
        self.assertIn("'line-dasharray': [2, 2]", source)
        self.assertIn("filter: ['==', ['get', 'kind'], 'requested']", source)
        self.assertIn("'circle-radius': 6, 'circle-color': '#ffffff'", source)
        self.assertIn("filter: ['==', ['get', 'kind'], 'snapped']", source)
        self.assertIn("'circle-radius': 7, 'circle-color': '#223468'", source)
        self.assertIn("distance > 0.25", source)
        self.assertIn("floodAddSnapDistance", source)
        self.assertIn("Math.max(150, Math.min(500, Number(radius || 300) * 0.65))", source)
        self.assertIn("distance > snapWarningThreshold()", source)

    def test_unavailable_model_clears_the_white_requested_point(self):
        source = (ROOT / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")

        unavailable_branch = source[source.index("function showSnapError"):source.index("function setLayerVisible")]
        self.assertIn("clearControlPreviewFeatures()", unavailable_branch)
        self.assertIn("showModal('modelUnavailableModal')", unavailable_branch)
        self.assertIn("setObservationStatus('', 'neutral')", unavailable_branch)

    def test_preview_layers_exist_before_the_first_point_request(self):
        source = (ROOT / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")

        initialize = source[source.index("async function initialize()"):]
        self.assertLess(initialize.index("ensureControlPointPreviewLayers()"), initialize.index("fetchJson('/api/hec-routing/info')"))
        schedule = source[source.index("function scheduleObservationAt"):source.index("async function snapObservationPayload")]
        self.assertLess(schedule.index("ensureControlPointPreviewLayers()"), schedule.index("getSource(CONTROL_PREVIEW_SOURCE)"))


if __name__ == "__main__":
    unittest.main()
