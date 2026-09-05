(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const config = window.FLOOD_CONFIG || {};
  const MAP_ASSETS_BASE = String(config.mapAssetsBase || '').replace(/\/$/, '');
  const MAP_ASSETS_VERSION = String(config.mapAssetsVersion || '');
  const RIVER_ZOOM = { 1: 6.5, 2: 6.5, 3: 10.5, other: 12.5 };
  const RIVER_KEYS = ['1', '2', '3', 'other'];
  const STATE_KEY = 'penelusuranBanjirMapUiV2';

  function readState() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY) || '{}') || {}; } catch (_) { return {}; }
  }
  function writeState(partial = {}) {
    const state = { ...readState(), ...partial };
    try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch (_) {}
    return state;
  }

  const restored = readState();
  let sidebarCollapsed = Boolean(restored.sidebarCollapsed);
  let currentBasemap = String(restored.currentBasemap || 'world-topo');
  let lastLightBasemap = String(restored.lightBasemap || (currentBasemap === 'esri-dark-gray' ? 'world-topo' : currentBasemap));
  let darkBasemapChanged = false;
  let floodOfficialRiversTemporarilyHidden = false;
  let riverAssetKey = '';
  let riverAssetPendingKey = '';
  let riverLoadSerial = 0;
  const riverAssetCache = new Map();
  let lastRiverLabelFilterSignature = '';
  let homeBounds = null;
  let measureActive = false;
  let measurePoints = [];
  window.FLOOD_MEASURE_ACTIVE = false;
  let searchPopup = null;
  let toastTimer = null;
  let basinAssetFallbackActivated = false;

  const BASEMAP_DEFS = {
    'world-topo': { tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}'], attribution: 'Tiles © Esri', maxzoom: 19 },
    'esri-satellite': { tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], attribution: 'Tiles © Esri', maxzoom: 19 },
    osm: { tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], attribution: '© OpenStreetMap contributors', maxzoom: 19 },
    'google-maps': { tiles: ['https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}'], attribution: '© Google', maxzoom: 20 },
    'google-satellite': { tiles: ['https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}'], attribution: '© Google', maxzoom: 20 },
    rbi: { tiles: ['https://geoservices.big.go.id/rbi/rest/services/BASEMAP/Rupabumi_Indonesia/MapServer/tile/{z}/{y}/{x}'], attribution: '© Badan Informasi Geospasial', maxzoom: 23 },
    'esri-dark-gray': { tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}'], attribution: 'Dark Gray Canvas © Esri', maxzoom: 16 },
    'esri-light-gray': { tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}'], attribution: 'Light Gray Canvas © Esri', maxzoom: 16 },
    opentopomap: { tiles: ['https://a.tile.opentopomap.org/{z}/{x}/{y}.png'], attribution: '© OpenStreetMap contributors, SRTM | © OpenTopoMap', maxzoom: 17 },
  };
  if (currentBasemap !== 'no-basemap' && !BASEMAP_DEFS[currentBasemap]) currentBasemap = 'world-topo';
  if (lastLightBasemap !== 'no-basemap' && !BASEMAP_DEFS[lastLightBasemap]) lastLightBasemap = 'world-topo';
  let lastThemeDark = document.documentElement.getAttribute('data-theme') === 'dark';
  if (lastThemeDark) currentBasemap = 'esri-dark-gray';

  function buildMapStyle() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    const sources = {};
    const layers = [{ id: 'basemap-background', type: 'background', paint: { 'background-color': dark ? '#151d2b' : '#eef1f4' } }];
    for (const [key, def] of Object.entries(BASEMAP_DEFS)) {
      sources[`basemap-source-${key}`] = { type: 'raster', tiles: def.tiles, tileSize: 256, maxzoom: def.maxzoom, attribution: def.attribution };
      layers.push({ id: `basemap-layer-${key}`, type: 'raster', source: `basemap-source-${key}`, layout: { visibility: key === currentBasemap ? 'visible' : 'none' }, paint: { 'raster-fade-duration': 120 } });
    }
    return { version: 8, sources, layers };
  }

  class ExistingControl {
    constructor(id) { this.id = id; this.el = null; }
    onAdd() { this.el = $(this.id); this.el?.classList.remove('hidden'); return this.el; }
    onRemove() { if (this.el?.parentNode) this.el.parentNode.removeChild(this.el); }
  }

  const camera = restored.camera || {};
  try { maplibregl.prewarm?.(); } catch (_) {}
  const map = new maplibregl.Map({
    container: 'map',
    center: Array.isArray(camera.center) ? camera.center : [110.1, -7.55],
    zoom: Number.isFinite(Number(camera.zoom)) ? Number(camera.zoom) : 7,
    bearing: 0,
    pitch: 0,
    minPitch: 0,
    maxPitch: 0,
    pitchWithRotate: false,
    touchPitch: false,
    style: buildMapStyle(),
    locale: {
      'NavigationControl.ZoomIn': 'Perbesar',
      'NavigationControl.ZoomOut': 'Perkecil',
      'NavigationControl.ResetBearing': 'Atur ulang arah',
      'FullscreenControl.Enter': 'Layar penuh',
      'FullscreenControl.Exit': 'Keluar layar penuh',
    },
  });
  window.FLOOD_MAP = map;

  map.on('error', event => {
    if (basinAssetFallbackActivated || !MAP_ASSETS_BASE) return;
    const message = String(event?.error?.message || '');
    const isBasinAssetError = event?.sourceId === 'official-basins'
      || message.includes('official_basins.geojson')
      || message.includes(MAP_ASSETS_BASE);
    if (!isBasinAssetError) return;
    const source = map.getSource('official-basins');
    if (!source?.setData) return;
    basinAssetFallbackActivated = true;
    source.setData('/api/map-assets/official-basins?proxy=1');
  });

  map.addControl(new ExistingControl('mapSearchForm'), 'top-left');
  map.addControl(new ExistingControl('mapToolbarControl'), 'bottom-right');
  map.addControl(new ExistingControl('coordReadout'), 'bottom-left');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 140, unit: 'metric' }), 'bottom-left');

  function assetFilename(key) {
    return {
      'official-basins': 'official_basins.geojson',
      'official-rivers-z6-8': 'official_rivers_z6_8.geojson',
      'official-rivers-z8-10': 'official_rivers_z8_10.geojson',
      'official-rivers-z10-11': 'official_rivers_z10_11.geojson',
      'official-rivers-z11-12': 'official_rivers_z11_12.geojson',
      'official-rivers-z12-14': 'official_rivers_z12_14.geojson',
      'official-rivers': 'official_rivers.geojson',
    }[key] || '';
  }

  function assetUrl(key) {
    if (MAP_ASSETS_BASE) {
      const file = assetFilename(key);
      const suffix = MAP_ASSETS_VERSION ? `?v=${encodeURIComponent(MAP_ASSETS_VERSION)}` : '';
      return `${MAP_ASSETS_BASE}/${file}${suffix}`;
    }
    return `/api/map-assets/${encodeURIComponent(key)}`;
  }

  function riverAssetForZoom(zoom = map.getZoom()) {
    if (zoom < 8.5) return 'z6-8';
    if (zoom < 10.5) return 'z8-10';
    if (zoom < 11.5) return 'z10-11';
    if (zoom < 12.5) return 'z11-12';
    if (zoom < 14) return 'z12-14';
    return 'full';
  }

  function modeledRiverUrl(tier = 'full') {
    const qs = new URLSearchParams({ tier: String(tier || 'full') });
    return `/api/hec-routing/modeled-rivers?${qs.toString()}`;
  }

  function riverOrderExpression() {
    return ['to-number', ['coalesce', ['get', 'river_order_int'], ['get', 'river_order']], 0];
  }

  function riverFilter(key) {
    const order = riverOrderExpression();
    return key === 'other'
      ? ['all', ['!=', order, 1], ['!=', order, 2], ['!=', order, 3]]
      : ['==', order, Number(key)];
  }

  function riverMapLabelExpression() {
    return ['case',
      ['!=', ['coalesce', ['get', 'river_name'], ''], ''],
      ['concat', 'K. ', ['to-string', ['get', 'river_name']]],
      ['coalesce', ['get', 'river_label'], ''],
    ];
  }

  function riverLabelSizeExpression() {
    const order = riverOrderExpression();
    const sizeAt = zoom => ['match', order, 1, zoom === 7 ? 11 : 14, 2, zoom === 7 ? 10 : 12.5, 3, zoom === 7 ? 9 : 11.5, zoom === 7 ? 8.5 : 10];
    return ['interpolate', ['linear'], ['zoom'], 7, sizeAt(7), 15, sizeAt(15)];
  }

  function riverLabelSortKeyExpression() {
    return ['match', riverOrderExpression(), 1, 10, 2, 20, 3, 30, 40];
  }

  function enabledRiverOrdersForCurrentZoom() {
    const auto = $('autoRiverZoom')?.checked !== false;
    const zoom = map?.getZoom?.() ?? 0;
    return RIVER_KEYS.filter(key => {
      const enabled = document.querySelector(`.river-order-toggle[data-order="${key}"]`)?.checked !== false;
      return enabled && (!auto || zoom >= RIVER_ZOOM[key]);
    });
  }

  function riverLabelFilter() {
    const allowed = enabledRiverOrdersForCurrentZoom();
    if (!allowed.length) return ['==', 1, 0];
    const filters = allowed.map(riverFilter);
    return filters.length === 1 ? filters[0] : ['any', ...filters];
  }

  function updateRiverLabelFilter({ force = false } = {}) {
    if (!map?.getLayer?.('official-river-label')) return;
    const signature = enabledRiverOrdersForCurrentZoom().join(',');
    if (!force && signature === lastRiverLabelFilterSignature) return;
    lastRiverLabelFilterSignature = signature;
    map.setFilter('official-river-label', riverLabelFilter());
  }

  function setLayerVisible(id, visible) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
  }

  function addReferenceLayers() {
    // Basin boundaries are display-only. A modest GeoJSON tiling tolerance
    // keeps pan/zoom responsive while preserving the boundary at WebGIS scale.
    if (!map.getSource('official-basins')) map.addSource('official-basins', {
      type: 'geojson', data: assetUrl('official-basins'), tolerance: 1.2, buffer: 64, maxzoom: 10,
    });
    if (!map.getLayer('official-basin-fill')) map.addLayer({ id: 'official-basin-fill', type: 'fill', source: 'official-basins', paint: { 'fill-color': '#9b7300', 'fill-opacity': 0.015 } });
    if (!map.getLayer('official-basin-line')) map.addLayer({ id: 'official-basin-line', type: 'line', source: 'official-basins', paint: { 'line-color': '#9b7300', 'line-width': 2, 'line-opacity': 0.9 } });

    if (!map.getSource('official-basin-labels')) map.addSource('official-basin-labels', {
      type: 'geojson', data: '/api/basin-labels', tolerance: 0.5, buffer: 32, maxzoom: 10,
    });
    if (!map.getLayer('official-basin-label')) map.addLayer({
      id: 'official-basin-label', type: 'symbol', source: 'official-basin-labels', minzoom: 6,
      layout: { 'text-field': ['concat', 'DAS ', ['get', 'basin_name']], 'text-size': ['interpolate', ['linear'], ['zoom'], 6, 10, 11, 13], 'text-letter-spacing': 0.05, 'text-allow-overlap': false },
      paint: { 'text-color': '#7a5c00', 'text-halo-color': 'rgba(255,255,255,.9)', 'text-halo-width': 1.5 },
    });

    // Rendering follows Delineasi DTA exactly; only the source geometry differs:
    // this endpoint returns the network clipped to the HEC-HMS modeled area.
    if (!map.getSource('official-rivers')) map.addSource('official-rivers', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
      tolerance: 0,
      maxzoom: 24,
      buffer: 128,
      lineMetrics: true,
    });
    for (const key of RIVER_KEYS) {
      if (!map.getLayer(`official-river-${key}`)) map.addLayer({
        id: `official-river-${key}`,
        type: 'line',
        source: 'official-rivers',
        filter: riverFilter(key),
        minzoom: RIVER_ZOOM[key],
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': '#0083d7', 'line-width': Math.max(0.55, 2 * ({ 1: 1, 2: 0.70, 3: 0.48, other: 0.34 }[key])), 'line-opacity': 1 },
      });
    }
    if (!map.getLayer('official-river-label')) map.addLayer({
      id: 'official-river-label', type: 'symbol', source: 'official-rivers', filter: riverLabelFilter(), minzoom: RIVER_ZOOM[1],
      layout: {
        'symbol-placement': 'line', 'symbol-spacing': 220, 'symbol-sort-key': riverLabelSortKeyExpression(), 'symbol-z-order': 'source',
        'text-field': riverMapLabelExpression(), 'text-size': riverLabelSizeExpression(),
        'text-rotation-alignment': 'map', 'text-pitch-alignment': 'map', 'text-keep-upright': true, 'text-max-angle': 70,
        'text-offset': [0, -0.55], 'text-padding': 1, 'text-allow-overlap': false, 'text-ignore-placement': false,
      },
      paint: { 'text-color': '#0083d7', 'text-halo-color': 'rgba(255,255,255,.97)', 'text-halo-width': 1.45 },
    });

    if (!map.getSource('esri-hillshade')) map.addSource('esri-hillshade', { type: 'raster', tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, maxzoom: 16, attribution: 'Hillshade © Esri' });
    if (!map.getLayer('esri-hillshade-layer')) map.addLayer({ id: 'esri-hillshade-layer', type: 'raster', source: 'esri-hillshade', layout: { visibility: 'none' }, paint: { 'raster-opacity': 1 } }, 'official-basin-fill');

    if (!map.getSource('measure-source')) map.addSource('measure-source', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    if (!map.getLayer('measure-line')) map.addLayer({ id: 'measure-line', type: 'line', source: 'measure-source', filter: ['==', ['geometry-type'], 'LineString'], paint: { 'line-color': '#223468', 'line-width': 2.5, 'line-dasharray': [2, 1.5] } });
    if (!map.getLayer('measure-points')) map.addLayer({ id: 'measure-points', type: 'circle', source: 'measure-source', filter: ['==', ['geometry-type'], 'Point'], paint: { 'circle-radius': 5, 'circle-color': '#223468', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });

    try { map.moveLayer('official-basin-label'); } catch (_) {}
    applyLayerControls();
    updateRiverAsset(true);
  }

  function riverCacheKey(tier) {
    return tier;
  }

  function fetchRiverAsset(tier) {
    const key = riverCacheKey(tier);
    let task = riverAssetCache.get(key);
    if (!task) {
      task = fetch(modeledRiverUrl(tier), { cache: 'force-cache' })
        .then(response => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .catch(error => { riverAssetCache.delete(key); throw error; });
      riverAssetCache.set(key, task);
    }
    return task;
  }

  function prefetchRiverTiers(currentTier) {
    if ($('autoRiverZoom')?.checked === false) return;
    const tiers = ['z6-8', 'z8-10', 'z10-11', 'z11-12', 'z12-14'];
    const index = tiers.indexOf(currentTier);
    for (const tier of [tiers[index - 1], tiers[index + 1]]) {
      if (tier) fetchRiverAsset(tier).catch(() => {});
    }
  }

  function updateRiverAsset(force = false) {
    const next = $('autoRiverZoom')?.checked === false ? 'full' : riverAssetForZoom();
    if (!force && (next === riverAssetKey || next === riverAssetPendingKey)) return;
    const serial = ++riverLoadSerial;
    riverAssetPendingKey = next;
    fetchRiverAsset(next).then(data => {
      if (serial !== riverLoadSerial) return;
      map.getSource('official-rivers')?.setData?.(data);
      riverAssetKey = next;
      riverAssetPendingKey = '';
      lastRiverLabelFilterSignature = '';
      updateRiverLabelFilter({ force: true });
      // Preload only adjacent tiers. It makes common zoom-in/out interactions
      // immediate without downloading the whole hierarchy at once.
      const schedule = window.requestIdleCallback || (callback => setTimeout(callback, 180));
      schedule(() => prefetchRiverTiers(next));
    }).catch(() => {
      if (serial === riverLoadSerial) riverAssetPendingKey = '';
    });
  }

  function applyLayerControls() {
    const basinOn = $('showBasins')?.checked !== false;
    const basinLabelOn = $('showBasinLabels')?.checked !== false;
    setLayerVisible('official-basin-fill', basinOn);
    setLayerVisible('official-basin-line', basinOn);
    setLayerVisible('official-basin-label', basinOn && basinLabelOn);

    const riverLinesOn = $('showRivers')?.checked !== false && !floodOfficialRiversTemporarilyHidden;
    const riverLabelsOn = ($('showRiverLabels')?.checked !== false);
    const auto = $('autoRiverZoom')?.checked !== false;
    for (const key of RIVER_KEYS) {
      const enabled = document.querySelector(`.river-order-toggle[data-order="${key}"]`)?.checked !== false;
      const layerId = `official-river-${key}`;
      if (map.getLayer(layerId)) {
        map.setLayerZoomRange(layerId, auto ? RIVER_ZOOM[key] : 0, 24);
        setLayerVisible(layerId, riverLinesOn && enabled);
      }
    }
    if (map.getLayer('official-river-label')) map.setLayerZoomRange('official-river-label', auto ? RIVER_ZOOM[1] : 0, 24);
    setLayerVisible('official-river-label', riverLabelsOn);
    updateRiverLabelFilter({ force: true });

    setLayerVisible('esri-hillshade-layer', Boolean($('showHillshade')?.checked));
    const hillOpacity = Math.max(0, Math.min(1, Number($('hillshadeOpacity')?.value || 100) / 100));
    if (map.getLayer('esri-hillshade-layer')) map.setPaintProperty('esri-hillshade-layer', 'raster-opacity', hillOpacity);
    if ($('hillshadeOpacityValue')) $('hillshadeOpacityValue').textContent = `${Math.round(hillOpacity * 100)}%`;

    const lineWidth = Number($('lineWidth')?.value || 2);
    if ($('lineWidthValue')) $('lineWidthValue').textContent = `${lineWidth.toFixed(1)} px`;
    if (map.getLayer('official-basin-line')) map.setPaintProperty('official-basin-line', 'line-width', Math.max(0.8, lineWidth));
    const riverWidthFactors = { 1: 1, 2: 0.70, 3: 0.48, other: 0.34 };
    for (const key of RIVER_KEYS) {
      const id = `official-river-${key}`;
      if (map.getLayer(id)) map.setPaintProperty(id, 'line-width', Math.max(0.55, lineWidth * riverWidthFactors[key]));
    }
    try { window.setFloodRoutingLineWidthScale?.(lineWidth / 2); } catch (_) {}
    const basinColor = $('basinColor')?.value || '#9b7300';
    if (map.getLayer('official-basin-line')) map.setPaintProperty('official-basin-line', 'line-color', basinColor);
    if (map.getLayer('official-basin-fill')) map.setPaintProperty('official-basin-fill', 'fill-color', basinColor);
    if (map.getLayer('official-basin-label')) map.setPaintProperty('official-basin-label', 'text-color', basinColor);
    const riverColor = $('riverColor')?.value || '#0083d7';
    for (const id of ['official-river-1', 'official-river-2', 'official-river-3', 'official-river-other']) if (map.getLayer(id)) map.setPaintProperty(id, 'line-color', riverColor);
    if (map.getLayer('official-river-label')) map.setPaintProperty('official-river-label', 'text-color', riverColor);
  }

  window.setFloodOfficialRiversTemporarilyHidden = hidden => {
    floodOfficialRiversTemporarilyHidden = Boolean(hidden);
    applyLayerControls();
  };

  function applyBasemap() {
    for (const key of Object.keys(BASEMAP_DEFS)) setLayerVisible(`basemap-layer-${key}`, key === currentBasemap);
    if (map.getLayer('basemap-background')) map.setPaintProperty('basemap-background', 'background-color', document.documentElement.getAttribute('data-theme') === 'dark' ? '#151d2b' : '#eef1f4');
    document.querySelectorAll('.basemap-card').forEach(card => {
      const active = card.dataset.basemap === currentBasemap;
      card.classList.toggle('active', active);
      card.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    $('noBasemapBtn')?.classList.toggle('active', currentBasemap === 'no-basemap');
    writeState({ currentBasemap, lightBasemap: lastLightBasemap });
  }

  function selectBasemap(key, { fromTheme = false } = {}) {
    currentBasemap = String(key || 'world-topo');
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (!fromTheme) {
      if (dark) darkBasemapChanged = true;
      else lastLightBasemap = currentBasemap;
    }
    applyBasemap();
  }

  function setSidebarCollapsed(value) {
    sidebarCollapsed = Boolean(value);
    $('sidebar')?.classList.toggle('collapsed', sidebarCollapsed);
    $('spatialWorkspace')?.classList.toggle('sidebar-is-collapsed', sidebarCollapsed);
    const button = $('sidebarSearchToggle');
    if (button) {
      button.innerHTML = `<i data-lucide="${sidebarCollapsed ? 'panel-left-open' : 'panel-left-close'}"></i>`;
      button.title = sidebarCollapsed ? 'Tampilkan panel' : 'Sembunyikan panel';
    }
    writeState({ sidebarCollapsed });
    setTimeout(() => map.resize(), 240);
    window.lucide?.createIcons?.();
  }
  window.setFloodSidebarCollapsed = value => setSidebarCollapsed(value);

  function openModal(el) { el?.classList.remove('hidden'); }
  function closeModal(el) { el?.classList.add('hidden'); }
  function showToast(text, duration = 3500) {
    const toast = $('appToast');
    if (!toast) return;
    clearTimeout(toastTimer);
    $('appToastText').textContent = String(text || '');
    toast.classList.remove('hidden');
    toastTimer = setTimeout(() => toast.classList.add('hidden'), duration);
  }

  function updateThemeIcon() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    const icon = $('themeIcon');
    if (icon) icon.setAttribute('data-lucide', dark ? 'sun' : 'moon');
    window.lucide?.createIcons?.();
  }
  function toggleTheme() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (dark) document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', 'dark');
    try { localStorage.setItem('theme', dark ? 'light' : 'dark'); } catch (_) {}
    updateThemeIcon();
    applyBasemap();
  }

  // common.js owns the theme button. Observe its theme change so the map and
  // local icon stay synchronized without double-toggling the same click.
  const themeObserver = new MutationObserver(() => {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (dark !== lastThemeDark) {
      if (dark) {
        // Entering dark mode always starts from Dark Gray, but remember the
        // active light basemap. A manual basemap change while dark becomes the
        // new choice when the user returns to light mode.
        lastLightBasemap = currentBasemap;
        darkBasemapChanged = false;
        currentBasemap = 'esri-dark-gray';
      } else {
        currentBasemap = darkBasemapChanged ? currentBasemap : lastLightBasemap;
        if (darkBasemapChanged) lastLightBasemap = currentBasemap;
        darkBasemapChanged = false;
      }
      lastThemeDark = dark;
    }
    updateThemeIcon();
    applyBasemap();
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  function haversineMeters(a, b) {
    const R = 6371008.8;
    const rad = x => x * Math.PI / 180;
    const p1 = rad(a[1]), p2 = rad(b[1]);
    const dp = rad(b[1] - a[1]), dl = rad(b[0] - a[0]);
    const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * R * Math.atan2(Math.sqrt(h), Math.sqrt(Math.max(0, 1 - h)));
  }
  function formatDistance(m) {
    if (!Number.isFinite(m)) return '—';
    return m >= 1000 ? `${(m / 1000).toLocaleString('id-ID', { maximumFractionDigits: 2 })} km` : `${Math.round(m).toLocaleString('id-ID')} m`;
  }
  function refreshMeasure() {
    const features = [];
    measurePoints.forEach(coord => features.push({ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: coord } }));
    if (measurePoints.length >= 2) features.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: measurePoints } });
    map.getSource('measure-source')?.setData?.({ type: 'FeatureCollection', features });
    let total = 0;
    for (let i = 1; i < measurePoints.length; i++) total += haversineMeters(measurePoints[i - 1], measurePoints[i]);
    if ($('measureText')) $('measureText').textContent = measurePoints.length < 2 ? 'Klik titik berikutnya pada peta.' : `Jarak sementara: ${formatDistance(total)}`;
  }
  function clearMeasure() {
    measurePoints = [];
    refreshMeasure();
    if ($('measureText')) $('measureText').textContent = 'Klik titik pertama pada peta.';
  }
  function toggleMeasure() {
    measureActive = !measureActive;
    window.FLOOD_MEASURE_ACTIVE = measureActive;
    $('measureBtn')?.classList.toggle('active', measureActive);
    $('measurePanel')?.classList.toggle('hidden', !measureActive);
    if (!measureActive) clearMeasure();
    window.lucide?.createIcons?.();
  }

  async function runSearch(query) {
    const host = $('searchResults');
    if (!host) return;
    host.classList.remove('hidden');
    host.innerHTML = '<div class="search-loading">Mencari…</div>';
    try {
      const response = await fetch(`/api/geocode?q=${encodeURIComponent(query)}`, { cache: 'no-store' });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
      const results = payload?.results || [];
      if (!results.length) { host.innerHTML = '<div class="search-empty">Lokasi tidak ditemukan.</div>'; return; }
      host.innerHTML = results.map((item, index) => `<button type="button" class="search-result" data-index="${index}"><strong>${escapeHtml(item.name || item.display_name || 'Lokasi')}</strong><span>${escapeHtml(item.display_name || item.category || '')}</span></button>`).join('');
      host.querySelectorAll('.search-result').forEach(button => button.addEventListener('click', () => {
        const item = results[Number(button.dataset.index)];
        if (!item) return;
        host.classList.add('hidden');
        map.flyTo({ center: [Number(item.lon), Number(item.lat)], zoom: 14, essential: true });
        if (searchPopup) searchPopup.remove();
        searchPopup = new maplibregl.Popup({ offset: 12 }).setLngLat([Number(item.lon), Number(item.lat)]).setHTML(`<div class="location-preview-card"><strong>${escapeHtml(item.name || 'Lokasi')}</strong><span>${escapeHtml(item.display_name || '')}</span></div>`).addTo(map);
      }));
    } catch (err) {
      host.innerHTML = `<div class="search-empty">${escapeHtml(err?.message || String(err))}</div>`;
    }
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
  }

  function usageNoticeSeenThisBrowserSession() {
    return document.cookie.split(';').some(part => part.trim().startsWith('floodUsageNoticeSeen='));
  }

  function markUsageNoticeSeenThisBrowserSession() {
    // Session cookie: shared by tabs and refreshes, but expires when the browser session ends.
    document.cookie = 'floodUsageNoticeSeen=1; Path=/; SameSite=Lax';
  }

  function bindUi() {
    $('sidebarSearchToggle')?.addEventListener('click', () => setSidebarCollapsed(!sidebarCollapsed));
    $('zoomInBtn')?.addEventListener('click', () => map.zoomIn({ duration: 250 }));
    $('zoomOutBtn')?.addEventListener('click', () => map.zoomOut({ duration: 250 }));
    $('resetNorthBtn')?.addEventListener('click', () => map.easeTo({ bearing: 0, pitch: 0, duration: 300 }));
    $('homeBtn')?.addEventListener('click', () => {
      if (homeBounds) map.fitBounds(homeBounds, { padding: 55, duration: 500 });
      else map.flyTo({ center: [110.1, -7.55], zoom: 7, duration: 500 });
    });
    $('fullscreenBtn')?.addEventListener('click', () => {
      const el = $('mapWrap');
      if (!document.fullscreenElement) el?.requestFullscreen?.().catch(() => {});
      else document.exitFullscreen?.().catch(() => {});
    });
    $('basemapBtn')?.addEventListener('click', () => openModal($('basemapModal')));
    $('closeBasemapModal')?.addEventListener('click', () => closeModal($('basemapModal')));
    $('noBasemapBtn')?.addEventListener('click', () => { selectBasemap('no-basemap'); closeModal($('basemapModal')); });
    document.querySelectorAll('.basemap-card').forEach(card => card.addEventListener('click', () => { selectBasemap(card.dataset.basemap); closeModal($('basemapModal')); }));

    $('layerBtn')?.addEventListener('click', () => $('layerPanel')?.classList.toggle('hidden'));
    $('closeLayerPanel')?.addEventListener('click', () => $('layerPanel')?.classList.add('hidden'));
    for (const id of ['showHillshade', 'showBasins', 'showBasinLabels', 'showRivers', 'showRiverLabels']) $(id)?.addEventListener('change', applyLayerControls);
    $('autoRiverZoom')?.addEventListener('change', () => { applyLayerControls(); updateRiverAsset(true); });
    for (const el of document.querySelectorAll('.river-order-toggle')) el.addEventListener('change', applyLayerControls);
    for (const id of ['hillshadeOpacity', 'lineWidth', 'basinColor', 'riverColor']) $(id)?.addEventListener('input', applyLayerControls);
    $('resetColorsBtn')?.addEventListener('click', () => {
      if ($('basinColor')) $('basinColor').value = '#9b7300';
      if ($('riverColor')) $('riverColor').value = '#0083d7';
      if ($('lineWidth')) $('lineWidth').value = '2';
      applyLayerControls();
    });
    document.querySelectorAll('.layer-eye-toggle').forEach(button => button.addEventListener('click', event => {
      event.preventDefault(); event.stopPropagation();
      const target = button.dataset.layerGroup === 'basins' ? $('showBasins') : button.dataset.layerGroup === 'rivers' ? $('showRivers') : null;
      if (!target) return;
      target.checked = !target.checked;
      applyLayerControls();
      button.innerHTML = `<i data-lucide="${target.checked ? 'eye' : 'eye-off'}"></i>`;
      window.lucide?.createIcons?.();
    }));

    $('measureBtn')?.addEventListener('click', toggleMeasure);
    $('clearMeasureBtn')?.addEventListener('click', clearMeasure);

    $('mapSearchForm')?.addEventListener('submit', event => {
      event.preventDefault();
      const q = String($('searchInput')?.value || '').trim();
      if (q.length >= 2) runSearch(q);
    });
    $('searchInput')?.addEventListener('input', () => { if (!$('searchInput')?.value.trim()) $('searchResults')?.classList.add('hidden'); });

    $('usageInfoBtn')?.addEventListener('click', () => openModal($('usageNoticeModal')));
    $('acceptUsageNotice')?.addEventListener('click', () => { markUsageNoticeSeenThisBrowserSession(); closeModal($('usageNoticeModal')); });
    $('definitionSidebarBtn')?.addEventListener('click', () => openModal($('definitionModal')));
    $('methodologySidebarBtn')?.addEventListener('click', () => openModal($('methodologyModal')));
    $('closeDefinitionModal')?.addEventListener('click', () => closeModal($('definitionModal')));
    $('closeMethodologyModal')?.addEventListener('click', () => closeModal($('methodologyModal')));
    $('basinSourceBtn')?.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); openModal($('basinSourceModal')); });
    $('closeBasinSourceModal')?.addEventListener('click', () => closeModal($('basinSourceModal')));

    document.querySelectorAll('.modal-backdrop').forEach(modal => modal.addEventListener('pointerdown', event => { if (event.target === modal) closeModal(modal); }));
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      document.querySelectorAll('.modal-backdrop:not(.hidden)').forEach(closeModal);
      $('layerPanel')?.classList.add('hidden');
      if (measureActive) toggleMeasure();
    });
    document.querySelectorAll('.info-tooltip').forEach(button => {
      const field = button.closest('.settings-field');
      const text = field?.dataset?.help || '';
      if (text) button.title = text;
      button.addEventListener('click', event => { event.preventDefault(); if (text) showToast(text, 5000); });
    });
    if (!usageNoticeSeenThisBrowserSession()) { markUsageNoticeSeenThisBrowserSession(); openModal($('usageNoticeModal')); }
  }

  let coordReadoutRaf = null;
  let pendingCoord = null;
  map.on('mousemove', event => {
    pendingCoord = [event.lngLat.lat, event.lngLat.lng];
    if (coordReadoutRaf) return;
    coordReadoutRaf = requestAnimationFrame(() => {
      coordReadoutRaf = null;
      const readout = $('coordReadout');
      if (readout && pendingCoord) readout.textContent = `${pendingCoord[0].toFixed(6)}, ${pendingCoord[1].toFixed(6)}`;
    });
  });
  map.on('click', event => {
    try {
      if (event.originalEvent?.__floodObservationPointHandled) return;
      if (typeof window.handleFloodRoutingMapClick === 'function' && window.handleFloodRoutingMapClick(event)) return;
    } catch (_) {}
    if (measureActive) {
      measurePoints.push([event.lngLat.lng, event.lngLat.lat]);
      refreshMeasure();
    }
  });
  map.on('zoomend', () => {
    if ($('autoRiverZoom')?.checked !== false) updateRiverAsset();
  });
  map.on('zoom', () => updateRiverLabelFilter());
  map.on('moveend', () => {
    const center = map.getCenter();
    writeState({ camera: { center: [center.lng, center.lat], zoom: map.getZoom() } });
  });

  map.once('load', async () => {
    addReferenceLayers();
    bindUi();
    setSidebarCollapsed(sidebarCollapsed);
    updateThemeIcon();
    applyBasemap();
    try {
      const response = await fetch('/api/info', { cache: 'no-store' });
      const payload = await response.json();
      if (response.ok && Array.isArray(payload?.bounds_wgs84) && payload.bounds_wgs84.length === 4) {
        const [minLon, minLat, maxLon, maxLat] = payload.bounds_wgs84.map(Number);
        if ([minLon, minLat, maxLon, maxLat].every(Number.isFinite)) homeBounds = [[minLon, minLat], [maxLon, maxLat]];
      }
    } catch (_) {}
    window.lucide?.createIcons?.();
  });
})();
