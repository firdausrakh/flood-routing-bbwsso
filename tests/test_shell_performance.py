from __future__ import annotations

import asyncio
from pathlib import Path
import unittest

from api.app import app


async def _asgi_get(path: str, *, accept_encoding: str | None = None):
    request_sent = False
    messages: list[dict] = []

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    headers = []
    if accept_encoding:
        headers.append((b"accept-encoding", accept_encoding.encode("ascii")))
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("test", 123),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], response_headers, body


class ShellPerformanceTests(unittest.TestCase):
    def test_static_assets_are_long_lived_and_compressed(self):
        status, headers, _ = asyncio.run(_asgi_get("/static/css/spatial.css", accept_encoding="gzip"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertIn("accept-encoding", headers.get("vary", "").lower())

    def test_reach_geometry_is_edge_cacheable(self):
        status, headers, body = asyncio.run(_asgi_get("/api/hec-routing/reaches", accept_encoding="gzip"))
        self.assertEqual(status, 200)
        self.assertIn("s-maxage=86400", headers["cache-control"])
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertGreater(len(body), 1000)

    def test_html_revalidates_and_loads_routing_only_scripts(self):
        status, headers, body = asyncio.run(_asgi_get("/"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-cache")
        html = body.decode("utf-8")
        self.assertIn("addEventListener('load'", html)
        self.assertIn("fetch('/api/health'", html)
        self.assertIn('/static/js/spatial.js?v=2.2.3-performance', html)
        self.assertIn('/static/js/flood-routing.js?v=2.2.3-performance', html)
        self.assertNotIn('/static/js/hss.js', html)
        self.assertNotIn('id="appHeader"', html)


    def test_frontend_uses_docked_layout_and_keeps_model_internals_hidden(self):
        root = Path(__file__).parents[1]
        template = (root / "templates" / "spatial.html").read_text(encoding="utf-8")
        js = (root / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8")
        css = (root / "static" / "css" / "spatial.css").read_text(encoding="utf-8")
        self.assertIn('id="floodRoutingPanel" class="flood-bottom-bar is-hidden"', template)
        self.assertIn('id="floodRightSidebar" class="flood-right-sidebar is-hidden"', template)
        self.assertIn('id="floodReturnPeriodSelect"', template)
        self.assertLess(template.index('id="floodReturnPeriodSelect"'), template.index('id="floodDecimalSeparatorSelect"'))
        self.assertIn('id="themeToggleBtn"', template)
        self.assertNotIn('flood-window-resize-handle', template)
        self.assertNotIn('flood-window-drag', template)
        self.assertNotIn('Subbasins HEC-HMS', template)
        self.assertNotIn('Jaringan Routing HEC-HMS', template)
        self.assertIn('Data Pemodelan Banjir Kala Ulang Belum Tersedia', template)
        self.assertIn('Penelusuran Banjir Tidak Dapat Diproses', template)
        self.assertIn('const MAX_OBSERVATIONS = 10;', js)
        self.assertIn("map.getCanvas().style.cursor = active ? 'crosshair' : '';", js)
        self.assertIn('include_identity: true', js)
        self.assertIn('riverAssetCache = new Map()', (root / "static" / "js" / "spatial.js").read_text(encoding="utf-8"))
        self.assertIn("state.reachData = await fetchJson('/api/hec-routing/reaches', 'force-cache')", js)
        self.assertIn("postJson('/api/hec-routing/series'", js)
        self.assertIn("window.matchMedia('(max-width: 1100px)')", js)
        self.assertNotIn("qs.set('scenario', modeledRiverScenario)", (root / "static" / "js" / "spatial.js").read_text(encoding="utf-8"))
        self.assertIn('.spatial-page .flood-window-resize-handle,', css)
        self.assertIn('display:none!important', css)

    def test_performance_assets_have_no_merge_conflict_markers(self):
        root = Path(__file__).parents[1]
        for relative in (
            "pnpm-lock.yaml",
            "static/js/vendor/analytics.mjs",
            "static/js/vendor/speed-insights.mjs",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("<<<<<<<", source)
            self.assertNotIn("=======", source)
            self.assertNotIn(">>>>>>>", source)

    def test_frontend_has_no_removed_analysis_actions(self):
        root = Path(__file__).parents[1]
        source = "\n".join([
            (root / "templates" / "spatial.html").read_text(encoding="utf-8"),
            (root / "static" / "js" / "spatial.js").read_text(encoding="utf-8"),
            (root / "static" / "js" / "flood-routing.js").read_text(encoding="utf-8"),
        ])
        for legacy in ("/api/hss", "/api/characteristics", "/api/delineate", "boundaryMatch"):
            self.assertNotIn(legacy, source)


if __name__ == "__main__":
    unittest.main()
