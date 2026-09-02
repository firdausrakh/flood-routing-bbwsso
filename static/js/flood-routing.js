(() => {
  'use strict';

  const map = window.FLOOD_MAP;
  if (!map) return;
  const $ = id => document.getElementById(id);

  const REACH_SOURCE = 'hec-routing-reaches';
  const OBS_SOURCE = 'hec-routing-observation-points';
  const REACH_BASE_LAYER = 'hec-routing-reach-base';
  const REACH_FLOW_LAYER = 'hec-routing-reach-flow';
  const REACH_FALLING_LAYER = 'hec-routing-reach-falling';
  const REACH_FLOW_HALO_LAYER = 'hec-routing-reach-flow-halo';
  const REACH_MOTION_LAYER = 'hec-routing-reach-motion';
  const REACH_HIT_LAYER = 'hec-routing-reach-hit';
  const OBS_CIRCLE_LAYER = 'hec-routing-observation-circle';
  const OBS_LABEL_LAYER = 'hec-routing-observation-label';
  const CONTROL_PREVIEW_SOURCE = 'hec-routing-control-preview';
  const CONTROL_PREVIEW_CONNECTOR_LAYER = 'hec-routing-control-preview-connector';
  const CONTROL_PREVIEW_HALO_LAYER = 'hec-routing-control-preview-halo';
  const CONTROL_PREVIEW_POINT_LAYER = 'hec-routing-control-preview-point';
  const STORAGE_KEY = 'floodRoutingObservationPointsV4';
  const MAX_OBSERVATIONS = 10;
  const MAX_NAME_LENGTH = 25;
  const EXISTING_POINT_EXCLUSION_RADIUS_M = 100;
  const POINT_COLORS = ['#1d70b8', '#d94841', '#2f8b57', '#8c5bd1', '#e47a22', '#0b8f8f', '#a45b2a', '#547535', '#b13f78', '#5366a8'];

  const state = {
    reachData: null,
    info: null,
    selectedReachIds: [],
    series: null,
    seriesKey: '',
    frame: 0,
    timer: null,
    requestSerial: 0,
    hoverPopup: null,
    hoverReachId: null,
    routeRiverNameCache: Object.create(null),
    hoverRiverNames: new Map(),
    hoverHideTimer: null,
    routingVisualizationVisible: false,
    observationPoints: restoreObservationPoints(),
    observationData: null,
    observationKey: '',
    observationRequestSerial: 0,
    addPointMode: false,
    movePointId: null,
    snapSerial: 0,
    chart: null,
    chartRuntimePromise: null,
    chartPortalPlaceholder: null,
    coordinatePreview: null,
    coordinatePreviewPopup: null,
    scenario: null,
    playbackRate: 1,
    lineWidthScale: 1,
    idlePopup: null,
    idleChart: null,
    idleInspectSerial: 0,
    pendingAddPoint: null,
    routingSelection: null,
    controlPopup: null,
    suppressIdleUntil: 0,
  };

  function restoreObservationPoints() {
    try {
      const current = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]');
      const v3 = JSON.parse(sessionStorage.getItem('floodRoutingObservationPointsV3') || '[]');
      const v2 = JSON.parse(sessionStorage.getItem('floodRoutingObservationPointsV2') || '[]');
      const legacy = current?.length ? current : (v3?.length ? v3 : v2);
      if (!Array.isArray(legacy)) return [];
      return legacy.slice(0, MAX_OBSERVATIONS).map((item, index) => {
        const internalLabel = String(item.internal_label || item.code || item.label || `T${index + 1}`);
        const legacyLabel = String(item.label || '').trim();
        const legacyCode = String(item.code || '').trim();
        const fallbackName = legacyLabel && legacyLabel !== legacyCode && !/^[A-Z]$/.test(legacyLabel) ? legacyLabel : `Titik ${index + 1}`;
        const name = String(item.name || fallbackName).trim();
        return {
          ...item,
          internal_label: internalLabel,
          label: internalLabel,
          name,
          visible: item.visible !== false,
          name_source: item.name_source || 'manual',
          color: item.color || POINT_COLORS[index % POINT_COLORS.length],
        };
      });
    } catch (_) {
      return [];
    }
  }

  function persistObservationPoints() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state.observationPoints)); } catch (_) {}
  }

  function emptySelectionFilter(property) {
    return ['==', ['get', property], '__none__'];
  }

  function selectionFilter(property, ids) {
    if (!ids?.length) return emptySelectionFilter(property);
    return ['in', ['get', property], ['literal', ids]];
  }

  function setStatus(text, kind = 'neutral') {
    const el = $('floodRoutingStatus');
    if (!el) return;
    el.textContent = text;
    el.dataset.kind = kind;
  }

  function setObservationStatus(text, kind = 'neutral') {
    const el = $('floodObservationStatus');
    if (!el) return;
    el.textContent = text;
    el.dataset.kind = kind;
  }

  function currentSnapRadius() {
    const value = Number($('snapRadius')?.value || 300);
    return Number.isFinite(value) && value > 0 ? value : 300;
  }

  function showModal(id) {
    const el = $(id);
    if (!el) return;
    el.classList.remove('hidden');
    el.setAttribute('aria-hidden', 'false');
  }

  function hideModal(id) {
    const el = $(id);
    if (!el) return;
    el.classList.add('hidden');
    el.setAttribute('aria-hidden', 'true');
  }

  function showSnapError(error) {
    const code = String(error?.code || '');
    if (code === 'outside_modeled_area' || code === 'scenario_unavailable') {
      showModal('modelUnavailableModal');
      return;
    }
    if (code === 'no_flowpath') return;
  }

  function scenarioQuery() {
    return state.scenario ? `scenario=${encodeURIComponent(state.scenario)}` : '';
  }

  function setLayerVisible(id, visible) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
  }

  // Legacy reference kept for regression tests: map.getCanvas().style.cursor = active ? 'crosshair' : '';
  function syncFloodMapCursor() {
    const active = Boolean(state.addPointMode || state.movePointId);
    const passivePointer = !active && Boolean(state.hoverPopup || state.controlPopup);
    try { map.getCanvas().style.cursor = active ? 'crosshair' : (passivePointer ? 'pointer' : ''); } catch (_) {}
    try { map.getContainer().classList.toggle('flood-crosshair-mode', active); } catch (_) {}
  }

  function syncOfficialRiverTemporaryHide() {
    // Analysis panels never own the reference-river visibility.  Only an active
    // flood visualization with a valid control-point boundary hides it.
    const hideForRouting = state.routingVisualizationVisible && state.selectedReachIds.length > 0;
    try { window.setFloodOfficialRiversTemporarilyHidden?.(hideForRouting); } catch (_) {}
  }

  function setRoutingVisualizationVisible(visible) {
    state.routingVisualizationVisible = Boolean(visible);
    if (state.routingVisualizationVisible) {
      closeControlPointPopup();
      destroyIdleChart();
      if (state.idlePopup) { try { state.idlePopup.remove(); } catch (_) {} state.idlePopup = null; }
    }
    const hasBoundary = state.selectedReachIds.length > 0;
    window.FLOOD_ROUTING_VISUAL_ACTIVE = state.routingVisualizationVisible && hasBoundary;
    syncOfficialRiverTemporaryHide();
    if (state.routingVisualizationVisible && !hasBoundary) {
      stopAnimation(); state.series = null; state.seriesKey = '';
      setStatus('Tambahkan minimal satu Titik Kontrol untuk menampilkan Visualisasi Aliran.', 'warning');
    }
    applyLayerVisibility();
    if (state.routingVisualizationVisible && hasBoundary && state.seriesKey !== [...state.selectedReachIds].sort().join(',')) {
      loadSelectedSeries(state.selectedReachIds).catch(() => {});
    }
  }

  function stopAnimation() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    const btn = $('floodPlayBtn');
    if (btn) {
      btn.classList.remove('is-playing');
      btn.innerHTML = '<i data-lucide="play"></i>';
      btn.setAttribute('aria-label', 'Putar animasi banjir');
    }
    window.lucide?.createIcons?.();
  }

  function resetFeatureStates(ids) {
    if (!map.getSource(REACH_SOURCE)) return;
    for (const rid of ids || []) {
      try { map.removeFeatureState({ source: REACH_SOURCE, id: rid }); } catch (_) {}
    }
  }

  function formatIntervalMinutes(interval) {
    const m = String(interval || '').match(/(\d+)\s*Minute/i);
    return m ? Number(m[1]) : 5;
  }

  function formatStepHHMM(index, interval = state.series?.interval) {
    const idx = Number(index);
    if (!Number.isFinite(idx)) return '—';
    const total = Math.max(0, Math.round(idx)) * formatIntervalMinutes(interval);
    const hh = String(Math.floor(total / 60)).padStart(2, '0');
    const mm = String(total % 60).padStart(2, '0');
    return `${hh}:${mm}`;
  }

  function formatFrameLabel(raw, index, interval = state.series?.interval) {
    return formatStepHHMM(index, interval);
  }

  function numberLocale() {
    return decimalSeparator() === '.' ? 'en-US' : 'id-ID';
  }

  function fractionDigitsForMagnitude(value) {
    const n = Math.abs(Number(value));
    if (!Number.isFinite(n)) return 0;
    if (n < 1) return 3;
    if (n < 10) return 2;
    if (n < 100) return 1;
    return 0;
  }

  function formatNumberCompact(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    const digits = fractionDigitsForMagnitude(n);
    return n.toLocaleString(numberLocale(), { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: true });
  }

  function formatAxisNumber(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    if (Math.abs(n) < 1e-12) return '0';
    if (Math.abs(n - Math.round(n)) < 1e-9) return Math.round(n).toLocaleString(numberLocale(), { maximumFractionDigits: 0, useGrouping: true });
    return formatNumberCompact(n);
  }

  function decimalSeparatorChar() {
    return decimalSeparator() === '.' ? '.' : ',';
  }

  function legendNumber(value) {
    const digits = Number(value) < 1 ? 2 : 0;
    const fixed = Number(value).toFixed(digits);
    return decimalSeparatorChar() === ',' ? fixed.replace('.', ',') : fixed;
  }

  function refreshRoutingLegendLabels() {
    const labels = document.querySelectorAll('.flood-routing-legend-qratio b');
    if (labels.length < 5) return;
    labels[0].textContent = `< ${legendNumber(0.20)}`;
    labels[1].textContent = `${legendNumber(0.20)}–${legendNumber(0.50)}`;
    labels[2].textContent = `${legendNumber(0.50)}–${legendNumber(0.85)}`;
    labels[3].textContent = `${legendNumber(0.85)}–${legendNumber(1.00)}`;
    labels[4].textContent = 'setelah Qp';
  }

  function setNameLimitWarning(input, visible) {
    const warning = input?.closest('.point-name-editor')
      ?.querySelector('.point-name-limit-hint, .point-name-limit-warning');
    warning?.classList.toggle('hidden', !visible);
  }

  function showNameLimitToast(input) {
    setNameLimitWarning(input, true);
  }

  function bindNameLimit(input) {
    if (!input || input.dataset.nameLimitBound === '1') return;
    input.dataset.nameLimitBound = '1';
    input.maxLength = MAX_NAME_LENGTH;

    const syncWarning = () => {
      setNameLimitWarning(input, input.value.length >= MAX_NAME_LENGTH);
    };
    input.addEventListener('input', syncWarning);
    syncWarning();

    input.addEventListener('paste', event => {
      const pasted = event.clipboardData?.getData('text') || '';
      const start = Number.isFinite(input.selectionStart) ? input.selectionStart : input.value.length;
      const end = Number.isFinite(input.selectionEnd) ? input.selectionEnd : start;
      const currentLength = input.value.length - (end - start);
      const available = MAX_NAME_LENGTH - currentLength;
      if (pasted.length <= available) return;

      event.preventDefault();
      if (available > 0) {
        input.setRangeText(pasted.slice(0, available), start, end, 'end');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      showNameLimitToast(input);
    });
  }

  function syncPointNameFeedback(editor, input, dirtyOverride = null) {
    if (!editor || !input) return;
    const save = editor.querySelector('.flood-point-save, .flood-control-popup-save');
    const stateEl = editor.querySelector('.point-name-state');
    const limitEl = editor.querySelector('.point-name-limit-hint');
    const savedValue = String(input.dataset.savedValue || input.getAttribute('data-saved-value') || '').trim();
    const currentValue = String(input.value || '').trim();
    const dirty = dirtyOverride == null ? currentValue !== savedValue : Boolean(dirtyOverride);
    const atLimit = String(input.value || '').length >= Number(input.maxLength || MAX_NAME_LENGTH);
    if (stateEl) stateEl.textContent = atLimit ? 'Maksimal 25 karakter.' : 'Belum disimpan';
    editor.classList.toggle('is-dirty', dirty);
    input.classList.toggle('is-dirty', dirty);
    editor.classList.toggle('is-at-limit', atLimit);
    if (save) save.disabled = !dirty || !currentValue;
    stateEl?.classList.toggle('hidden', !dirty);
    limitEl?.classList.add('hidden');
  }

  function niceAxisStep(value, targetSteps = 4) {
    const n = Math.max(0, Number(value) || 0);
    if (!(n > 0)) return 1;
    const rough = n / Math.max(1, Number(targetSteps) || 4);
    const power = 10 ** Math.floor(Math.log10(rough));
    const scaled = rough / power;
    const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
    return Number((nice * power).toPrecision(12));
  }

  function niceAxisRange(value, targetSteps = 4) {
    const step = niceAxisStep(value, targetSteps);
    const n = Math.max(0, Number(value) || 0);
    const max = Number((Math.max(step, Math.ceil(n / step) * step)).toPrecision(12));
    return { max, step };
  }

  function niceAxisCeiling(value) {
    return niceAxisRange(value).max;
  }

  function formatMetricTime(raw, index, interval) {
    if (Number.isFinite(Number(index))) return formatStepHHMM(Number(index), interval);
    const text = String(raw ?? '').trim();
    if (/^\d+$/.test(text)) return formatStepHHMM(Number(text), interval);
    const hhmm = text.match(/(\d{1,2}):(\d{2})/);
    return hhmm ? `${String(hhmm[1]).padStart(2, '0')}:${hhmm[2]}` : (text || '—');
  }

  function renderFrame(index) {
    const series = state.series;
    if (!series?.times?.length || !state.selectedReachIds.length) return;
    const i = Math.max(0, Math.min(Number(index) || 0, series.times.length - 1));
    state.frame = i;
    const slider = $('floodTimeSlider');
    if (slider && Number(slider.value) !== i) slider.value = String(i);
    const label = $('floodTimeLabel');
    if (label) label.textContent = formatFrameLabel(series.times[i], i);

    for (const rid of state.selectedReachIds) {
      const values = series.reaches?.[rid];
      const raw = Array.isArray(values) ? values[i] : null;
      const qout = Number.isFinite(Number(raw)) ? Number(raw) : null;
      const inflowValues = series.reach_inflows?.[rid];
      const inflowRaw = Array.isArray(inflowValues) ? inflowValues[i] : null;
      const qin = Number.isFinite(Number(inflowRaw)) ? Number(inflowRaw) : null;
      const peak = Number(series.reach_peaks?.[rid] || 0);
      const inflowPeak = Number(series.reach_inflow_peaks?.[rid] || 0);
      const peakIndexRaw = series.reach_peak_indices?.[rid];
      const peakIndex = Number.isFinite(Number(peakIndexRaw)) ? Number(peakIndexRaw) : (Array.isArray(values) ? values.lastIndexOf(peak) : -1);
      const inflowPeakIndexRaw = series.reach_inflow_peak_indices?.[rid];
      const inflowPeakIndex = Number.isFinite(Number(inflowPeakIndexRaw)) ? Number(inflowPeakIndexRaw) : (Array.isArray(inflowValues) ? inflowValues.lastIndexOf(inflowPeak) : -1);
      const ratio = qout == null || peak <= 0 ? 0 : Math.max(0, Math.min(1.0, qout / peak));
      const falling = qout != null && peak > 0 && peakIndex >= 0 && i > peakIndex && qout < (peak * 0.999999);
      try { map.setFeatureState({ source: REACH_SOURCE, id: rid }, { q: qout, qout, qin, ratio, peak, inflowPeak, peakIndex, inflowPeakIndex, falling, frame: i }); } catch (_) {}
    }
  }

  function flowWidthExpression() {
    return ['max', 1.45, ['*',
      ['coalesce', ['get', 'base_width'], 1.7],
      Number(state.lineWidthScale || 1),
      ['step', ['coalesce', ['feature-state', 'ratio'], 0], 0.8, 0.20, 1.0, 0.50, 1.2, 0.85, 1.5]
    ]];
  }

  function refreshFlowWidthPaint() {
    const width = flowWidthExpression();
    if (map.getLayer(REACH_BASE_LAYER)) map.setPaintProperty(REACH_BASE_LAYER, 'line-width', ['max', 1.15, ['*', ['coalesce', ['get', 'base_width'], 1.7], Number(state.lineWidthScale || 1), 0.78]]);
    if (map.getLayer(REACH_FLOW_LAYER)) map.setPaintProperty(REACH_FLOW_LAYER, 'line-width', width);
    if (map.getLayer(REACH_FALLING_LAYER)) map.setPaintProperty(REACH_FALLING_LAYER, 'line-width', width);
    if (map.getLayer(REACH_HIT_LAYER)) map.setPaintProperty(REACH_HIT_LAYER, 'line-width', 24);
  }

  window.setFloodRoutingLineWidthScale = value => {
    const n = Number(value);
    state.lineWidthScale = Number.isFinite(n) ? Math.max(0.4, Math.min(3, n)) : 1;
    refreshFlowWidthPaint();
  };

  // Visualisasi Aliran tidak memakai animasi garis. Klasifikasi dikembalikan
  // ke 5 kelas hidrologi: Baseflow, Rising Limb, Mendekati Puncak, Peak, dan Resesi.

  function playbackIntervalMs() {
    return Math.max(120, Math.round(620 / Math.max(0.25, Number(state.playbackRate) || 1)));
  }

  function updatePlaybackRateUi() {
    const el = $('floodSpeedLabel');
    if (el) el.textContent = `${Number(state.playbackRate || 1).toLocaleString(numberLocale(), { maximumFractionDigits: 2 })}×`;
  }

  function startAnimation() {
    if (!state.series?.times?.length) return;
    if (state.timer) clearInterval(state.timer);
    const btn = $('floodPlayBtn');
    if (btn) {
      btn.classList.add('is-playing');
      btn.innerHTML = '<i data-lucide="pause"></i>';
      btn.setAttribute('aria-label', 'Jeda animasi banjir');
    }
    state.timer = setInterval(() => renderFrame(state.frame + 1 >= state.series.times.length ? 0 : state.frame + 1), playbackIntervalMs());
    window.lucide?.createIcons?.();
  }

  function toggleAnimation() {
    if (state.timer) return stopAnimation();
    startAnimation();
  }

  function stepFrame(delta) {
    stopAnimation();
    if (!state.series?.times?.length) return;
    renderFrame(Math.max(0, Math.min(state.series.times.length - 1, state.frame + Number(delta || 0))));
  }

  function changePlaybackRate(direction) {
    const rates = [0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16];
    const current = Number(state.playbackRate || 1);
    let index = rates.reduce((best, value, i) => Math.abs(value - current) < Math.abs(rates[best] - current) ? i : best, 0);
    index = Math.max(0, Math.min(rates.length - 1, index + (direction > 0 ? 1 : -1)));
    state.playbackRate = rates[index];
    updatePlaybackRateUi();
    if (state.timer) startAnimation();
  }

  async function fetchJson(url) {
    const response = await fetch(url, { cache: 'no-store' });
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch (_) {}
    if (!response.ok) {
      const detail = payload?.detail;
      const err = new Error((typeof detail === 'string' ? detail : detail?.message) || `HTTP ${response.status}`);
      err.status = response.status;
      err.code = detail?.code || null;
      throw err;
    }
    return payload;
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store', body: JSON.stringify(payload),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) {}
    if (!response.ok) {
      const detail = data?.detail;
      const err = new Error((typeof detail === 'string' ? detail : detail?.message) || `HTTP ${response.status}`);
      err.status = response.status;
      err.code = detail?.code || null;
      throw err;
    }
    return data;
  }

  function hydrographExportPoints() {
    return state.observationPoints.map(item => ({
      point_id: item.point_id,
      label: observationDisplayName(item),
      sheet_name: observationName(item),
      lon: Number(item.lon),
      lat: Number(item.lat),
    }));
  }

  async function downloadHydrographXlsx() {
    const button = $('floodChartDownloadBtn');
    const points = hydrographExportPoints();
    if (!points.length) {
      setObservationStatus('Tambahkan minimal satu Titik Kontrol sebelum mengunduh hidrograf.', 'warning');
      return;
    }
    const original = button?.innerHTML || '';
    if (button) { button.disabled = true; button.innerHTML = '<i data-lucide="loader-circle"></i><span>Menyiapkan…</span>'; }
    window.lucide?.createIcons?.();
    try {
      const response = await fetch('/api/hec-routing/export-hydrograph.xlsx', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        cache: 'no-store',
        body: JSON.stringify({ scenario: state.scenario, snap_radius_m: currentSnapRadius(), points }),
      });
      if (!response.ok) {
        let data = null;
        try { data = await response.json(); } catch (_) {}
        const detail = data?.detail;
        throw new Error((typeof detail === 'string' ? detail : detail?.message) || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const filename = encodedName ? decodeURIComponent(encodedName) : 'Debit Banjir.xlsx';
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = filename; anchor.style.display = 'none';
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1200);
      setObservationStatus('', 'ready');
    } catch (err) {
      setObservationStatus(`File .xlsx belum dapat dibuat: ${err?.message || err}`, 'warning');
    } finally {
      if (button) { button.disabled = false; button.innerHTML = original; }
      window.lucide?.createIcons?.();
    }
  }

  function loadRuntimeScript(src, ready) {
    if (ready()) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[data-flood-runtime="${src}"]`);
      if (existing) { existing.addEventListener('load', resolve, { once: true }); existing.addEventListener('error', reject, { once: true }); return; }
      const script = document.createElement('script');
      script.src = src; script.async = true; script.crossOrigin = 'anonymous'; script.dataset.floodRuntime = src;
      script.addEventListener('load', resolve, { once: true });
      script.addEventListener('error', () => { script.remove(); reject(new Error(`Gagal memuat ${src}`)); }, { once: true });
      document.head.appendChild(script);
    });
  }

  function ensureChartRuntime() {
    if (window.Chart && window.Hammer && window.Chart.registry?.plugins?.get?.('zoom')) return Promise.resolve();
    if (state.chartRuntimePromise) return state.chartRuntimePromise;
    state.chartRuntimePromise = Promise.all([
      loadRuntimeScript('https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js', () => Boolean(window.Chart)),
      loadRuntimeScript('https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js', () => Boolean(window.Hammer)),
    ]).then(() => loadRuntimeScript(
      'https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js',
      () => Boolean(window.Chart?.registry?.plugins?.get?.('zoom')),
    )).catch(error => { state.chartRuntimePromise = null; throw error; });
    return state.chartRuntimePromise;
  }

  function observationCode(point, index = 0) {
    return String(point?.internal_label || point?.label || `T${index + 1}`);
  }

  function observationName(point) {
    return String(point?.name || 'Titik Kontrol').trim() || 'Titik Kontrol';
  }

  function observationRiver(point) {
    return String(point?.river_name || point?.official_river?.name || 'Sungai Tanpa Nama').trim() || 'Sungai Tanpa Nama';
  }

  function resolveRouteRiverName(feature, routeId = '') {
    const rid = String(routeId || feature?.properties?.route_id || '').trim();
    const cached = rid ? state.routeRiverNameCache?.[rid] : null;
    if (typeof cached === 'string' && cached.trim()) return cached.trim();
    const props = feature?.properties || {};
    const direct = String(props.river_name || props.river_label || '').trim();
    if (direct) return direct;
    const fromPoint = state.observationPoints.find(point => {
      const routing = point?.routing || {};
      return String(routing.route_id || '') === rid || (String(routing.model_id || '') === String(props.model_id || '') && String(routing.element_id || '') === String(props.element_id || ''));
    });
    if (fromPoint) return observationRiver(fromPoint);
    const named = String(props.name || props.reach_name || '').trim();
    if (/^(?:kali|k\.|sungai|s\.)\s+/i.test(named)) return named;
    return 'Sungai Tanpa Nama';
  }

  function routeMidpoint(feature) {
    const coords = feature?.geometry?.type === 'LineString' ? feature.geometry.coordinates : [];
    if (!Array.isArray(coords) || coords.length < 2) return null;
    const lengths = [];
    let total = 0;
    for (let i = 1; i < coords.length; i += 1) {
      const ax = Number(coords[i - 1][0]), ay = Number(coords[i - 1][1]);
      const bx = Number(coords[i][0]), by = Number(coords[i][1]);
      const len = Math.hypot(bx - ax, by - ay);
      lengths.push(len);
      total += len;
    }
    if (!total) {
      const mid = coords[Math.floor(coords.length / 2)];
      return mid ? { lon: Number(mid[0]), lat: Number(mid[1]) } : null;
    }
    let walked = 0;
    const target = total / 2;
    for (let i = 1; i < coords.length; i += 1) {
      const len = lengths[i - 1] || 0;
      const ax = Number(coords[i - 1][0]), ay = Number(coords[i - 1][1]);
      const bx = Number(coords[i][0]), by = Number(coords[i][1]);
      if (walked + len >= target) {
        const t = len > 0 ? (target - walked) / len : 0.5;
        return { lon: ax + ((bx - ax) * t), lat: ay + ((by - ay) * t) };
      }
      walked += len;
    }
    const last = coords[coords.length - 1];
    return last ? { lon: Number(last[0]), lat: Number(last[1]) } : null;
  }

  async function ensureRouteRiverName(routeId, feature) {
    const rid = String(routeId || feature?.properties?.route_id || '').trim();
    if (!rid) return '';
    const cached = state.routeRiverNameCache?.[rid];
    if (typeof cached === 'string') return cached;
    if (cached && typeof cached.then === 'function') return cached;
    const fallback = resolveRouteRiverName(feature, rid);
    const midpoint = routeMidpoint(feature);
    if (!midpoint) {
      if (fallback) state.routeRiverNameCache[rid] = fallback;
      return fallback;
    }
    const props = feature?.properties || {};
    const task = lookupPointIdentity(midpoint.lon, midpoint.lat, {
      model_id: props.model_id,
      element_id: props.element_id,
      snapped_lon: midpoint.lon,
      snapped_lat: midpoint.lat,
      route_id: rid,
    }).then(identity => {
      const name = String(identity?.official_river?.name || fallback || '').trim();
      state.routeRiverNameCache[rid] = name;
      return name;
    }).catch(() => {
      state.routeRiverNameCache[rid] = fallback;
      return fallback;
    });
    state.routeRiverNameCache[rid] = task;
    return task;
  }

  function observationDisplayName(point) {
    return `${observationRiver(point)} – ${observationName(point)}`;
  }

  function observationColor(point, index = 0) {
    return String(point?.color || POINT_COLORS[index % POINT_COLORS.length]);
  }

  async function lookupPointIdentity(lon, lat, routing = null) {
    try {
      const radius = currentSnapRadius();
      const qs = new URLSearchParams({ lon: String(lon), lat: String(lat), snap_radius_m: String(radius) });
      if (routing?.model_id) qs.set('model_id', String(routing.model_id));
      if (routing?.element_id) qs.set('element_id', String(routing.element_id));
      if (Number.isFinite(Number(routing?.snapped_lon))) qs.set('snapped_lon', String(routing.snapped_lon));
      if (Number.isFinite(Number(routing?.snapped_lat))) qs.set('snapped_lat', String(routing.snapped_lat));
      return await fetchJson(`/api/location-check?${qs.toString()}`);
    } catch (_) {
      return null;
    }
  }

  function copyText(text, button = null) {
    const value = String(text || '');
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = value; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (_) {}
      ta.remove();
    };
    const job = navigator.clipboard?.writeText ? navigator.clipboard.writeText(value).catch(fallback) : Promise.resolve(fallback());
    Promise.resolve(job).finally(() => {
      if (!button) return;
      const old = button.innerHTML; button.innerHTML = '<i data-lucide="check"></i><span>Tersalin</span>'; window.lucide?.createIcons?.();
      setTimeout(() => { if (button.isConnected) { button.innerHTML = old; window.lucide?.createIcons?.(); } }, 900);
    });
  }

  function formatDistanceMeters(value) {
    const m = Number(value);
    if (!Number.isFinite(m)) return '—';
    if (Math.abs(m) >= 1000) return `${formatNumberCompact(m / 1000)} km`;
    return `${Math.round(m).toLocaleString(numberLocale(), { maximumFractionDigits: 0, useGrouping: true })} m`;
  }

  function formatLagMinutes(value) {
    const m = Number(value);
    if (!Number.isFinite(m)) return '—';
    const rounded = Math.round(m);
    const sign = rounded > 0 ? '+' : (rounded < 0 ? '-' : '');
    const abs = Math.abs(rounded);
    const hours = Math.floor(abs / 60), minutes = abs % 60;
    if (hours && minutes) return `${sign}${hours} jam ${minutes} menit`;
    if (hours) return `${sign}${hours} jam`;
    return `${sign}${minutes} menit`;
  }

  function nextPointLabel() {
    return `T${state.observationPoints.length + 1}`;
  }

  function dmsToDecimal(deg, min, sec, hem) {
    let value = Math.abs(Number(deg)) + (Number(min || 0) / 60) + (Number(sec || 0) / 3600);
    if (['S', 'W'].includes(String(hem).toUpperCase())) value = -value;
    return value;
  }

  function parseControlCoordinate(text) {
    const raw = String(text || '').trim();
    if (!raw) throw new Error('Masukkan koordinat.');
    const dd = raw.match(/^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$/);
    if (dd) {
      const lat = Number(dd[1]), lon = Number(dd[2]);
      if (Math.abs(lat) > 90 || Math.abs(lon) > 180) throw new Error('Koordinat berada di luar rentang yang valid.');
      return { lat, lon };
    }
    const dmsRe = /(\d+(?:\.\d+)?)\s*[°º]\s*(\d+(?:\.\d+)?)?\s*['′]?\s*(\d+(?:\.\d+)?)?\s*["″]?\s*([NSEW])/gi;
    const matches = [...raw.matchAll(dmsRe)];
    if (matches.length >= 2) {
      let lat = null, lon = null;
      for (const match of matches) {
        const value = dmsToDecimal(match[1], match[2], match[3], match[4]);
        const hem = match[4].toUpperCase();
        if (hem === 'N' || hem === 'S') lat = value;
        if (hem === 'E' || hem === 'W') lon = value;
      }
      if (lat !== null && lon !== null) return { lat, lon };
    }
    throw new Error('Format koordinat belum dikenali. Gunakan DD atau DMS seperti contoh.');
  }

  function decimalSeparator() {
    return $('floodDecimalSeparatorSelect')?.value === '.' ? '.' : ',';
  }

  function formatCoordinateNumber(value, digits = 6) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    const fixed = n.toFixed(digits);
    return decimalSeparator() === ',' ? fixed.replace('.', ',') : fixed;
  }

  function observationCoordinateText(point) {
    return `${formatCoordinateNumber(point?.snapped_lat ?? point?.lat)}, ${formatCoordinateNumber(point?.snapped_lon ?? point?.lon)}`;
  }

  function nearestObservationPoint(lon, lat, maxDistanceM = EXISTING_POINT_EXCLUSION_RADIUS_M) {
    const earthRadiusM = 6371008.8;
    const sourceLat = Number(lat), sourceLon = Number(lon);
    if (!Number.isFinite(sourceLat) || !Number.isFinite(sourceLon)) return null;
    let nearest = null;
    for (const point of state.observationPoints) {
      const pointLat = Number(point?.snapped_lat ?? point?.lat);
      const pointLon = Number(point?.snapped_lon ?? point?.lon);
      if (!Number.isFinite(pointLat) || !Number.isFinite(pointLon)) continue;
      const deltaLat = (pointLat - sourceLat) * Math.PI / 180;
      const deltaLon = (pointLon - sourceLon) * Math.PI / 180;
      const sinLat = Math.sin(deltaLat / 2), sinLon = Math.sin(deltaLon / 2);
      const a = sinLat * sinLat + Math.cos(sourceLat * Math.PI / 180) * Math.cos(pointLat * Math.PI / 180) * sinLon * sinLon;
      const distanceM = 2 * earthRadiusM * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      if (distanceM <= maxDistanceM && (!nearest || distanceM < nearest.distanceM)) nearest = { point, distanceM, coordinates: [pointLon, pointLat] };
    }
    return nearest;
  }

  function observationGeoJson() {
    return {
      type: 'FeatureCollection',
      features: state.observationPoints.flatMap((p, index) => p.visible === false ? [] : [{
        type: 'Feature',
        properties: {
          point_id: p.point_id,
          label: observationName(p),
          full_name: observationDisplayName(p),
          index,
          color: observationColor(p, index),
        },
        geometry: {
          type: 'Point',
          coordinates: [Number(p.snapped_lon ?? p.lon), Number(p.snapped_lat ?? p.lat)],
        },
      }]),
    };
  }

  function refreshObservationMap() {
    map.getSource(OBS_SOURCE)?.setData(observationGeoJson());
  }


  function updateFloodPointControls() {
    const count = state.observationPoints.length;
    const countEl = $('floodPointCount');
    const clearBtn = $('floodClearPointsBtn');
    const focusAllBtn = $('floodFocusAllPointsBtn');
    const toggleAllBtn = $('floodToggleAllPointsBtn');
    const downloadBtn = $('floodChartDownloadBtn');
    if (countEl) countEl.textContent = String(count);
    if (clearBtn) clearBtn.disabled = count === 0;
    if (focusAllBtn) focusAllBtn.disabled = count === 0;
    if (downloadBtn) downloadBtn.disabled = count === 0;
    if (toggleAllBtn) {
      toggleAllBtn.disabled = count === 0;
      const allVisible = count > 0 && state.observationPoints.every(point => point.visible !== false);
      toggleAllBtn.dataset.visible = allVisible ? 'true' : 'false';
      toggleAllBtn.setAttribute('aria-pressed', allVisible ? 'true' : 'false');
      toggleAllBtn.setAttribute('aria-label', allVisible ? 'Sembunyikan semua titik kontrol' : 'Tampilkan semua titik kontrol');
      toggleAllBtn.title = allVisible ? 'Sembunyikan semua titik kontrol' : 'Tampilkan semua titik kontrol';
      toggleAllBtn.innerHTML = `<i data-lucide="${allVisible ? 'eye' : 'eye-off'}"></i>`;
    }
  }

  function setObservationVisibility(id, visible) {
    const point = state.observationPoints.find(item => item.point_id === id);
    if (!point) return;
    point.visible = Boolean(visible);
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
  }

  function toggleAllObservationVisibility() {
    if (!state.observationPoints.length) return;
    const allVisible = state.observationPoints.every(point => point.visible !== false);
    state.observationPoints.forEach(point => { point.visible = !allVisible; });
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
  }

  function focusAllObservationPoints() {
    if (!state.observationPoints.length) return;
    const bounds = new maplibregl.LngLatBounds();
    state.observationPoints.forEach(point => {
      const lon = Number(point.snapped_lon ?? point.lon), lat = Number(point.snapped_lat ?? point.lat);
      if (Number.isFinite(lon) && Number.isFinite(lat)) bounds.extend([lon, lat]);
    });
    if (bounds.isEmpty()) return;
    if (state.observationPoints.length === 1) {
      const point = state.observationPoints[0];
      map.easeTo({ center: [Number(point.snapped_lon ?? point.lon), Number(point.snapped_lat ?? point.lat)], zoom: Math.max(map.getZoom(), 13), duration: 500 });
      return;
    }
    map.fitBounds(bounds, { padding: { top: 80, bottom: 70, left: 70, right: 70 }, maxZoom: 14.5, duration: 550 });
  }

  function renderFloodPointList() {
    const host = $('floodPointList');
    updateFloodPointControls();
    if (!host) return;
    if (!state.observationPoints.length) {
      host.innerHTML = '<div class="empty-state">Belum ada titik kontrol.</div>';
      syncDockedPanels();
      window.lucide?.createIcons?.();
      return;
    }
    host.innerHTML = state.observationPoints.map((p, index) => {
      const color = observationColor(p, index);
      const fullName = observationDisplayName(p);
      const basinName = String(p.basin_name || p.official_basin?.name || '—').trim() || '—';
      const coord = observationCoordinateText(p);
      const visible = p.visible !== false;
      return `
      <details class="point-card flood-point-card${visible ? '' : ' is-layer-hidden'}" data-id="${escapeHtml(p.point_id)}" style="--point-color:${escapeHtml(color)}">
        <summary>
          <span class="point-master-visibility-toggle flood-point-visibility-toggle${visible ? '' : ' is-hidden-state'}" data-id="${escapeHtml(p.point_id)}" role="button" tabindex="0" aria-pressed="${visible ? 'true' : 'false'}" aria-label="${visible ? 'Sembunyikan titik kontrol' : 'Tampilkan titik kontrol'}" title="${visible ? 'Sembunyikan titik kontrol' : 'Tampilkan titik kontrol'}"><i data-lucide="${visible ? 'eye' : 'eye-off'}"></i></span>
          <span class="point-name-chip flood-control-point-chip" style="background:${escapeHtml(color)};color:#fff">${escapeHtml(observationName(p))}</span>
          <span class="point-summary-main"><strong class="point-summary-river">${escapeHtml(observationRiver(p))}</strong><span class="point-summary-basin">DAS ${escapeHtml(basinName)}</span></span>
          <span class="point-summary-area"></span>
          <span class="point-chevron"><i data-lucide="chevron-down"></i></span>
        </summary>
        <div class="point-body">
          <label class="point-edit-label point-name-editor flood-point-name-editor" data-id="${escapeHtml(p.point_id)}"><span class="point-edit-label-row"><span>Nama titik</span><span class="point-name-feedback"><span class="point-name-state unsaved-indicator hidden" aria-live="polite">Belum disimpan</span></span></span>
            <div class="point-name-grid"><input class="flood-point-name" data-id="${escapeHtml(p.point_id)}" data-saved-value="${escapeHtml(observationName(p))}" type="text" maxlength="25" value="${escapeHtml(observationName(p))}" aria-label="Nama titik kontrol" /><button class="mini-button save-icon-button flood-point-save" data-id="${escapeHtml(p.point_id)}" type="button" aria-label="Simpan nama titik" disabled><i data-lucide="save"></i></button></div>
          </label>
          <div class="point-action-label">Aksi</div>
          <div class="point-action-row">
            <button class="flood-point-action flood-point-copy" data-id="${escapeHtml(p.point_id)}" data-coordinate="${escapeHtml(coord)}" type="button"><i data-lucide="copy"></i><span>Salin</span></button>
            <button class="flood-point-action flood-point-move" data-id="${escapeHtml(p.point_id)}" type="button"><i data-lucide="move"></i><span>Pindah</span></button>
            <button class="flood-point-action flood-point-color-btn" data-id="${escapeHtml(p.point_id)}" type="button"><i data-lucide="palette"></i><span>Warna</span></button>
            <button class="flood-point-action danger flood-point-remove" data-id="${escapeHtml(p.point_id)}" type="button"><i data-lucide="trash-2"></i><span>Hapus</span></button>
            <input class="flood-point-color-input" data-id="${escapeHtml(p.point_id)}" type="color" value="${escapeHtml(color)}" tabindex="-1" aria-hidden="true" />
          </div>
        </div>
      </details>`;
    }).join('');

    host.querySelectorAll('.flood-point-remove').forEach(btn => btn.addEventListener('click', () => removeObservationPoint(btn.dataset.id)));
    host.querySelectorAll('.flood-point-copy').forEach(btn => btn.addEventListener('click', () => copyText(btn.dataset.coordinate, btn)));
    host.querySelectorAll('.flood-point-move').forEach(btn => btn.addEventListener('click', () => armMoveObservationPoint(btn.dataset.id)));
    host.querySelectorAll('.flood-point-color-btn').forEach(btn => btn.addEventListener('click', () => host.querySelector(`.flood-point-color-input[data-id="${btn.dataset.id}"]`)?.click()));
    host.querySelectorAll('.flood-point-color-input').forEach(input => input.addEventListener('input', () => updateObservationColor(input.dataset.id, input.value)));
    host.querySelectorAll('.flood-point-save').forEach(btn => btn.addEventListener('click', () => {
      const input = host.querySelector(`.flood-point-name[data-id="${btn.dataset.id}"]`);
      renameObservationPoint(btn.dataset.id, input?.value);
    }));
    host.querySelectorAll('.flood-point-name').forEach(input => {
      bindNameLimit(input);
      const syncDirtyState = () => syncPointNameFeedback(input.closest('.flood-point-name-editor'), input);
      input.addEventListener('input', syncDirtyState);
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); if (!input.closest('.flood-point-name-editor')?.querySelector('.flood-point-save')?.disabled) renameObservationPoint(input.dataset.id, input.value); input.blur(); }
      });
      syncDirtyState();
    });
    host.querySelectorAll('.flood-point-visibility-toggle').forEach(button => {
      const toggle = event => { event.preventDefault(); event.stopPropagation(); const point = state.observationPoints.find(item => item.point_id === button.dataset.id); setObservationVisibility(button.dataset.id, point?.visible === false); };
      button.addEventListener('click', toggle);
      button.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') toggle(event); });
    });
    window.lucide?.createIcons?.();
    syncRightSidebarLayout();
    syncDockedPanels();
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
  }



  function renameObservationPoint(id, rawName) {
    const p = state.observationPoints.find(item => item.point_id === id);
    if (!p) return;
    const name = String(rawName || '').trim().slice(0, 25) || observationName(p);
    p.name = name;
    p.name_source = 'manual';
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
    refreshObservationComparison(true).catch(() => {});
  }

  function updateObservationColor(id, color) {
    const p = state.observationPoints.find(item => item.point_id === id);
    if (!p || !/^#[0-9a-f]{6}$/i.test(String(color || ''))) return;
    p.color = color;
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
    if (state.observationData) renderObservationSummary(state.observationData);
  }

  function removeObservationPoint(id) {
    closeControlPointPopup();
    if (state.movePointId === id) state.movePointId = null;
    state.observationPoints = state.observationPoints.filter(item => item.point_id !== id);
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
    if (!state.observationPoints.length) applyRoutingSelection({ routing_selection: { route_ids: [] } });
    else refreshObservationComparison(true).catch(() => {});
    window.FLOOD_ROUTING_CLICK_MODE = state.addPointMode || Boolean(state.movePointId);
    syncFloodMapCursor();
  }

  function clearObservationPoints() {
    closeControlPointPopup();
    destroyIdleChart();
    if (state.idlePopup) { try { state.idlePopup.remove(); } catch (_) {} state.idlePopup = null; }
    state.observationPoints = [];
    state.observationData = null;
    state.observationKey = '';
    state.movePointId = null;
    clearControlCoordinatePreview();
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
    renderObservationSummary({ points: [], segments: [], times: [] });
    applyRoutingSelection({ routing_selection: { route_ids: [] } });
      setObservationStatus('Tambahkan Titik Kontrol pada sungai. Untuk jarak dan selisih waktu puncak gunakan minimal 2 titik.', 'neutral');
    window.FLOOD_ROUTING_CLICK_MODE = state.addPointMode;
    syncFloodMapCursor();
  }

  function clearControlCoordinatePreview() {
    state.coordinatePreview = null;
    if (state.coordinatePreviewPopup) {
      try { state.coordinatePreviewPopup.remove(); } catch (_) {}
      state.coordinatePreviewPopup = null;
    }
    const source = map.getSource(CONTROL_PREVIEW_SOURCE);
    if (source?.setData) source.setData({ type: 'FeatureCollection', features: [] });
  }

  function showControlCoordinatePreview(lon, lat) {
    clearControlCoordinatePreview();
    state.coordinatePreview = { lon: Number(lon), lat: Number(lat) };
    const source = map.getSource(CONTROL_PREVIEW_SOURCE);
    source?.setData?.({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { kind: 'requested' }, geometry: { type: 'Point', coordinates: [Number(lon), Number(lat)] } }] });
    map.easeTo({ center: [Number(lon), Number(lat)], zoom: Math.max(map.getZoom(), 13), duration: 450 });
    const coordText = `${formatCoordinateNumber(lat)}, ${formatCoordinateNumber(lon)}`;
    state.coordinatePreviewPopup = new maplibregl.Popup({ closeButton: true, closeOnClick: false, offset: 13, className: 'location-preview-popup' })
      .setLngLat([Number(lon), Number(lat)])
      .setHTML(`<div class="location-preview-card"><strong>Titik kontrol pilihan</strong><span>${escapeHtml(coordText)}</span><p>Lokasi pratinjau. Tekan <b>Mulai Tambah</b> untuk menambahkan titik kontrol ini.</p></div>`)
      .addTo(map);
    state.coordinatePreviewPopup.on('close', () => {
      state.coordinatePreviewPopup = null;
      state.coordinatePreview = null;
      source?.setData?.({ type: 'FeatureCollection', features: [] });
    });
  }

  function previewCoordinateInput() {
    const input = $('floodCoordinateInput');
    try {
      const { lon, lat } = parseControlCoordinate(input?.value || '');
      if (state.addPointMode) {
        clearControlCoordinatePreview();
        map.easeTo({ center: [Number(lon), Number(lat)], zoom: Math.max(map.getZoom(), 13), duration: 450 });
        setObservationStatus('Memproses koordinat sebagai Titik Kontrol…', 'busy');
        addObservationAt(lon, lat).catch(() => {});
        return;
      }
      showControlCoordinatePreview(lon, lat);
      setObservationStatus('Lokasi koordinat ditampilkan. Tekan Mulai Tambah untuk menjadikannya Titik Kontrol.', 'neutral');
    } catch (err) {
      setObservationStatus(err?.message || String(err), 'warning');
      input?.focus();
    }
  }

  function setAddPointMode(on) {
    state.addPointMode = Boolean(on) && state.observationPoints.length < MAX_OBSERVATIONS;
    if (state.addPointMode) {
      state.movePointId = null; closeControlPointPopup(); clearReachHover(); destroyIdleChart();
      if (state.idlePopup) { try { state.idlePopup.remove(); } catch (_) {} state.idlePopup = null; }
    }
    window.FLOOD_ROUTING_CLICK_MODE = state.addPointMode || Boolean(state.movePointId);
    const btn = $('floodAddPointBtn');
    const hint = $('floodPointModeHint');
    if (btn) {
      btn.classList.toggle('active', state.addPointMode);
      btn.setAttribute('aria-pressed', state.addPointMode ? 'true' : 'false');
      btn.innerHTML = state.addPointMode ? '<i data-lucide="check"></i>Selesai' : '<i data-lucide="crosshair"></i>Mulai Tambah';
    }
    if (hint) hint.innerHTML = state.addPointMode
      ? '<b>Mode tambah aktif.</b> Klik sungai untuk menambahkan Titik Kontrol. Tekan <b>Selesai</b> bila sudah cukup.'
      : (state.movePointId ? `<b>Mode pindah aktif.</b> Klik posisi baru untuk ${escapeHtml(observationDisplayName(state.observationPoints.find(p => p.point_id === state.movePointId)))}.` : 'Tekan <b>Mulai Tambah</b>, lalu klik sungai. Anda juga dapat menampilkan koordinat dari bagian Pilih Titik terlebih dahulu.');
    syncFloodMapCursor();
    if (state.addPointMode && state.coordinatePreview) {
      const preview = { ...state.coordinatePreview };
      clearControlCoordinatePreview();
      addObservationAt(preview.lon, preview.lat).catch(() => {});
    }
    window.lucide?.createIcons?.();
  }

  function armMoveObservationPoint(id) {
    const p = state.observationPoints.find(item => item.point_id === id);
    if (!p) return;
    state.addPointMode = false;
    state.movePointId = id;
    window.FLOOD_ROUTING_CLICK_MODE = true;
    const btn = $('floodAddPointBtn');
    if (btn) { btn.classList.remove('active'); btn.setAttribute('aria-pressed', 'false'); btn.innerHTML = '<i data-lucide="crosshair"></i>Mulai Tambah'; }
    const hint = $('floodPointModeHint');
    if (hint) hint.innerHTML = `<b>Pindah ${escapeHtml(observationDisplayName(p))}.</b> Klik posisi baru pada sungai. Nama titik manual dipertahankan.`;
    setObservationStatus(`Klik posisi baru untuk ${observationDisplayName(p)}.`, 'busy');
    syncFloodMapCursor();
    window.lucide?.createIcons?.();
  }

  async function snapObservationPayload(pointId, code, lon, lat) {
    const radius = currentSnapRadius();
    const payload = await postJson('/api/hec-routing/snap', {
      scenario: state.scenario,
      snap_radius_m: radius,
      points: [{ point_id: pointId, label: code, lon, lat }],
    });
    const snapped = payload?.points?.[0];
    if (!snapped) {
      const detail = payload?.errors?.[0] || { code: 'no_flowpath', radius_m: radius, message: `Tidak ditemukan jalur aliran dalam radius ${Math.round(radius)} m.` };
      showSnapError(detail);
      const err = new Error(detail.message || 'Titik tidak dapat diproses.');
      err.code = detail.code;
      err.handled = true;
      throw err;
    }
    // Keep the identity lookup at the original click coordinate so automatic
    // toponym naming can preserve the clicked left/right side of the river.
    const identity = await lookupPointIdentity(Number(lon), Number(lat), snapped);
    return { snapped, identity };
  }

  function closeAddPointDialog() {
    state.pendingAddPoint = null;
    hideModal('floodAddPointModal');
    map.getSource(CONTROL_PREVIEW_SOURCE)?.setData?.({ type:'FeatureCollection', features:[] });
  }

  function positionAddPointDialog() {
    const pending = state.pendingAddPoint;
    const modal = $('floodAddPointModal');
    const card = modal?.querySelector('.flood-add-point-modal');
    if (!pending || !modal || !card || modal.classList.contains('hidden')) return;
    requestAnimationFrame(() => {
      if (!state.pendingAddPoint || modal.classList.contains('hidden')) return;
      const pt = map.project([Number(pending.lon), Number(pending.lat)]);
      const rect = map.getContainer().getBoundingClientRect();
      const edge = 8, gap = 14;
      const cardW = Math.min(card.offsetWidth || 246, Math.max(180, rect.width - edge * 2));
      const cardH = Math.min(card.offsetHeight || 310, Math.max(150, rect.height - edge * 2));
      const aboveTop = pt.y - cardH - gap;
      const belowTop = pt.y + gap;
      const fitsAbove = aboveTop >= edge;
      const fitsBelow = belowTop + cardH <= rect.height - edge;
      const useBelow = !fitsAbove && fitsBelow;
      const left = Math.max(edge, Math.min(rect.width - cardW - edge, pt.x - cardW / 2));
      let top = useBelow ? belowTop : aboveTop;
      if (!fitsAbove && !fitsBelow) top = Math.max(edge, Math.min(rect.height - cardH - edge, pt.y - cardH / 2));
      card.style.left = `${left}px`;
      card.style.top = `${Math.max(edge, Math.min(rect.height - cardH - edge, top))}px`;
      card.style.transform = 'none';
      card.classList.toggle('is-below', useBelow);
    });
  }

  function populateAddPointDialog(pending) {
    const requested = `${formatCoordinateNumber(pending.lat)}, ${formatCoordinateNumber(pending.lon)}`;
    const hasSnap = Boolean(pending.snapped && Number.isFinite(Number(pending.snapped.snapped_lon)) && Number.isFinite(Number(pending.snapped.snapped_lat)));
    const distance = hasSnap ? Number(pending.snapped.snap_distance_m || 0) : null;
    const riverName = String(pending.identity?.official_river?.name || 'Sungai Tanpa Nama').trim() || 'Sungai Tanpa Nama';
    const basinName = String(pending.identity?.official_basin?.name || '—').trim() || '—';
    if ($('floodAddRequestedCoords')) $('floodAddRequestedCoords').textContent = requested;
    if ($('floodAddSnappedCoords')) $('floodAddSnappedCoords').textContent = hasSnap ? `${formatCoordinateNumber(pending.snapped.snapped_lat)}, ${formatCoordinateNumber(pending.snapped.snapped_lon)}` : '—';
    if ($('floodAddRiverName')) $('floodAddRiverName').textContent = riverName;
    if ($('floodAddBasinName')) $('floodAddBasinName').textContent = `DAS ${basinName}`;
    if ($('floodAddSnapDistance')) $('floodAddSnapDistance').textContent = hasSnap ? `${Math.round(distance)} m` : '—';
    const info = document.querySelector('.flood-add-info');
    const warning = document.querySelector('.flood-add-warning');
    const snappedRow = document.querySelector('.flood-add-snapped');
    if (info) info.classList.toggle('hidden', !hasSnap);
    if (snappedRow) snappedRow.classList.toggle('hidden', !hasSnap);
    if (warning) {
      warning.classList.toggle('hidden', hasSnap);
      const span = warning.querySelector('span');
      if (span && !hasSnap) span.textContent = `Tidak ditemukan jalur aliran dalam radius ${Math.round(currentSnapRadius())} m. Perbesar radius snapping pada Pengaturan Lanjutan.`;
    }
    const autoName = String(pending.identity?.toponym?.name || `Titik ${state.observationPoints.length + 1}`).trim().slice(0, 25);
    const nameInput = $('floodAddPointName');
    if (nameInput) nameInput.value = autoName;
    const hint = $('floodAddPointNameHint');
    if (hint) hint.innerHTML = pending.identity?.toponym?.name
      ? `Nama otomatis: <b>${escapeHtml(autoName)}</b>${Number.isFinite(Number(pending.identity?.toponym?.distance_m)) ? ` jarak ${escapeHtml(formatDistanceMeters(pending.identity.toponym.distance_m))}` : ''}`
      : 'Nama otomatis tidak tersedia; silakan isi nama titik.';
    const commit = $('floodAddPointCommitBtn');
    if (commit) commit.disabled = !hasSnap;
    const features = [{ type:'Feature', properties:{kind:'requested'}, geometry:{type:'Point',coordinates:[Number(pending.lon),Number(pending.lat)]} }];
    if (hasSnap) {
      const requestedCoords = [Number(pending.lon), Number(pending.lat)];
      const snappedCoords = [Number(pending.snapped.snapped_lon), Number(pending.snapped.snapped_lat)];
      if (requestedCoords[0] !== snappedCoords[0] || requestedCoords[1] !== snappedCoords[1]) features.push({ type:'Feature', properties:{kind:'connector'}, geometry:{type:'LineString',coordinates:[requestedCoords, snappedCoords]} });
      features.push({ type:'Feature', properties:{kind:'snapped'}, geometry:{type:'Point',coordinates:snappedCoords} });
    }
    map.getSource(CONTROL_PREVIEW_SOURCE)?.setData?.({type:'FeatureCollection',features});
    showModal('floodAddPointModal');
    positionAddPointDialog();
    const nameWarning = $('floodAddPointNameWarning');
    if (nameWarning) nameWarning.classList.add('hidden');
    if (nameInput) {
      nameInput.maxLength = MAX_NAME_LENGTH;
      bindNameLimit(nameInput);
      const syncNameInputState = () => {
        if (commit) commit.disabled = !hasSnap || !String(nameInput.value || '').trim();
      };
      nameInput.addEventListener('input', syncNameInputState);
      syncNameInputState();
    }
    window.lucide?.createIcons?.();
    setTimeout(() => nameInput?.focus?.(), 30);
  }

  async function addObservationAt(lon, lat) {
    if (state.observationPoints.length >= MAX_OBSERVATIONS) {
      setAddPointMode(false);
      setObservationStatus(`Maksimal ${MAX_OBSERVATIONS} Titik Kontrol.`, 'warning');
      return;
    }
    const pointId = `F${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const internalLabel = nextPointLabel();
    const serial = ++state.snapSerial;
    setObservationStatus('Menempatkan Titik Kontrol pada jaringan sungai dan membaca identitas lokasi…', 'busy');
    try {
      const { snapped, identity } = await snapObservationPayload(pointId, internalLabel, lon, lat);
      if (serial !== state.snapSerial) return;
      state.pendingAddPoint = { pointId, internalLabel, lon: Number(lon), lat: Number(lat), snapped, identity };
      populateAddPointDialog(state.pendingAddPoint);
      setObservationStatus('', 'neutral');
    } catch (err) {
      if (err?.code === 'no_flowpath') {
        const identity = await lookupPointIdentity(Number(lon), Number(lat), null);
        if (serial !== state.snapSerial) return;
        state.pendingAddPoint = { pointId, internalLabel, lon:Number(lon), lat:Number(lat), snapped:null, identity, snapError:err };
        populateAddPointDialog(state.pendingAddPoint);
        setObservationStatus('', 'neutral');
      } else if (!err?.handled) setObservationStatus(`Titik Kontrol belum dapat ditambahkan: ${err.message || err}`, 'warning');
    }
  }

  async function commitPendingAddPoint() {
    const pending = state.pendingAddPoint;
    if (!pending) return;
    const rawInputName = String($('floodAddPointName')?.value || '').trim();
    if (rawInputName.length > 25) return;
    const inputName = rawInputName;
    const fallbackName = String(pending.identity?.toponym?.name || `Titik ${state.observationPoints.length + 1}`).trim().slice(0, 25);
    const name = inputName || fallbackName;
    const autoName = String(pending.identity?.toponym?.name || '').trim();
    const riverName = String(pending.identity?.official_river?.name || 'Sungai Tanpa Nama').trim() || 'Sungai Tanpa Nama';
    const point = {
      point_id: pending.pointId,
      internal_label: pending.internalLabel,
      label: pending.internalLabel,
      name,
      name_source: autoName && name === autoName ? 'auto' : 'manual',
      river_name: riverName,
      basin_name: pending.identity?.official_basin?.name || null,
      visible: true,
      color: POINT_COLORS[state.observationPoints.length % POINT_COLORS.length],
      lon: pending.lon,
      lat: pending.lat,
      ...pending.snapped,
    };
    state.observationPoints.push(point);
    closeAddPointDialog();
    persistObservationPoints();
    refreshObservationMap();
    renderFloodPointList();
    setObservationStatus('', 'neutral');
    await refreshObservationComparison(true);
    if (state.observationPoints.length >= MAX_OBSERVATIONS) setAddPointMode(false);
  }

  async function moveObservationAt(id, lon, lat) {
    const p = state.observationPoints.find(item => item.point_id === id);
    if (!p) return;
    const serial = ++state.snapSerial;
    setObservationStatus(`Memindahkan ${observationDisplayName(p)} pada jaringan sungai…`, 'busy');
    try {
      const { snapped, identity } = await snapObservationPayload(p.point_id, observationCode(p), lon, lat);
      if (serial !== state.snapSerial) return;
      p.lon = Number(lon); p.lat = Number(lat);
      Object.assign(p, snapped);
      p.river_name = String(identity?.official_river?.name || p.river_name || 'Sungai Tanpa Nama').trim() || 'Sungai Tanpa Nama';
      p.basin_name = identity?.official_basin?.name || p.basin_name || null;
      if (p.name_source !== 'manual' && identity?.toponym?.name) { p.name = String(identity.toponym.name).trim().slice(0, 25); p.name_source = 'auto'; }
      state.movePointId = null;
      window.FLOOD_ROUTING_CLICK_MODE = false;
      persistObservationPoints(); refreshObservationMap(); renderFloodPointList(); setAddPointMode(false);
      await refreshObservationComparison(true);
      setObservationStatus(`${observationDisplayName(p)} berhasil dipindahkan.`, 'ready');
    } catch (err) {
      state.movePointId = null; window.FLOOD_ROUTING_CLICK_MODE = false; setAddPointMode(false);
      setObservationStatus(`Titik belum dapat dipindahkan: ${err.message || err}`, 'warning');
    }
  }

  function idleInspectionAllowed() {
    if (state.addPointMode || state.movePointId || state.routingVisualizationVisible || window.FLOOD_MEASURE_ACTIVE) return false;
    if (state.controlPopup || Date.now() < Number(state.suppressIdleUntil || 0)) return false;
    // Penelusuran Aliran and Hidrograf are analysis panels, not exclusive map
    // modes. Idle inspection remains available while either panel is open.
    return true;
  }

  function destroyIdleChart() {
    if (state.idleChart) { try { state.idleChart.destroy(); } catch (_) {} state.idleChart = null; }
  }

  async function renderIdleChart(metric, payload) {
    const canvas = document.getElementById('floodIdleHydrograph');
    const readout = document.getElementById('floodIdleChartReadout');
    if (!canvas || !Array.isArray(metric?.series)) return;
    try {
      await ensureChartRuntime();
      if (!canvas.isConnected || !window.Chart) return;
      destroyIdleChart();
      const vals = metric.series.map(Number).filter(Number.isFinite);
      const dataMax = vals.length ? Math.max(...vals) : 0;
      const yAxis = niceAxisRange(dataMax || 1);
      const ymax = yAxis.max;
      const timeCount = metric.series.length || 1;
      const updateReadout = (_event, elements, chart) => {
        if (!readout || !elements?.length) return;
        const x = Math.max(0, Math.min(timeCount - 1, Math.round(Number(elements[0]?.element?.$context?.raw?.x ?? elements[0]?.index ?? 0))));
        const y = Number(chart?.data?.datasets?.[0]?.data?.[x]?.y);
        if (!Number.isFinite(y)) return;
        readout.innerHTML = `<strong>Jam ke-${escapeHtml(formatFrameLabel('', x, payload?.interval))}</strong><span><i></i><b>${escapeHtml(formatAxisNumber(y))} m³/det</b></span>`;
      };
      state.idleChart = new window.Chart(canvas, {
        type: 'line',
        data: {
          datasets: [{
            label: 'Debit',
            data: metric.series.map((v, i) => ({ x: i, y: Number.isFinite(Number(v)) ? Number(v) : null })),
            borderColor: '#1d70b8', backgroundColor: '#1d70b8', borderWidth: 2,
            pointRadius: 0, pointHoverRadius: 3.5, pointHitRadius: 8, tension: .14, spanGaps: true,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
          interaction: { mode: 'nearest', intersect: false, axis: 'x' },
          onHover: updateReadout,
          plugins: {
            legend: { display: false },
            tooltip: { enabled: false },
            zoom: {
              limits: { x: { min: 0, max: Math.max(0, timeCount - 1), minRange: Math.min(4, Math.max(1, timeCount - 1)) }, y: { min: 0, max: ymax } },
              pan: { enabled: true, mode: 'x', threshold: 4 },
              zoom: { wheel: { enabled: true, speed: .08 }, pinch: { enabled: true }, drag: { enabled: false }, mode: 'x' },
            },
          },
          scales: {
            x: {
              type: 'linear', min: 0, max: Math.max(0, timeCount - 1),
              title: { display: true, text: 'Jam ke-', color: chartCssColor('--text-muted','#667386'), font: { weight: '600', size: 10 } },
              ticks: { color: chartCssColor('--text-muted','#667386'), maxTicksLimit: 4, callback: v => formatFrameLabel('', Math.round(Number(v)), payload?.interval) },
              grid: { display: false },
            },
            y: {
              min: 0, max: ymax,
              title: { display: true, text: 'Debit (m³/det)', color: chartCssColor('--text-muted','#667386'), font: { weight: '600', size: 10 } },
              ticks: { color: chartCssColor('--text-muted','#667386'), maxTicksLimit: 5, stepSize: yAxis.step, callback: value => formatAxisNumber(value) },
              grid: { color: chartCssColor('--border-color', '#e0e5ed') },
            },
          },
        },
      });
      // Seed the pinned readout with the peak time so the summary is useful
      // before the first mouse movement.
      const peakIndex = Number.isFinite(Number(metric?.peak_index)) ? Number(metric.peak_index) : vals.length ? metric.series.indexOf(Math.max(...vals)) : 0;
      const peakY = Number(metric.series?.[peakIndex]);
      if (readout && Number.isFinite(peakY)) readout.innerHTML = `<strong>Jam ke-${escapeHtml(formatFrameLabel('', peakIndex, payload?.interval))}</strong><span><i></i><b>${escapeHtml(formatAxisNumber(peakY))} m³/det</b></span>`;
    } catch (_) {}
  }

  async function inspectIdleLocation(lon, lat) {
    const serial = ++state.idleInspectSerial;
    try {
      const payload = await postJson('/api/hec-routing/observe', { scenario: state.scenario, snap_radius_m: currentSnapRadius(), points: [{ point_id: '__inspect__', label: 'Lokasi inspeksi', lon, lat }] });
      if (serial !== state.idleInspectSerial) return;
      const metric = payload?.points?.[0];
      if (!metric) return;
      const identity = await lookupPointIdentity(lon, lat, metric);
      if (serial !== state.idleInspectSerial || state.controlPopup || !idleInspectionAllowed()) return;
      const river = String(identity?.official_river?.name || 'Sungai Tanpa Nama');
      const name = String(identity?.toponym?.name || 'Lokasi pada sungai');
      const basin = String(identity?.official_basin?.name || '—');
      const html = `<div class="flood-idle-summary"><div class="flood-idle-card-head"><span class="flood-idle-name">${escapeHtml(name)}</span><b class="flood-idle-river">${escapeHtml(river)}</b><small class="flood-idle-basin">DAS ${escapeHtml(basin)}</small></div><dl><dt>Debit puncak (Qp)</dt><dd>${Number.isFinite(Number(metric.peak_q)) ? `${formatNumberCompact(metric.peak_q)} m³/det` : '—'}</dd><dt>Waktu puncak</dt><dd>${escapeHtml(formatMetricTime(metric.peak_time, metric.peak_index, payload.interval))}</dd></dl><div id="floodIdleChartReadout" class="flood-idle-chart-readout">Arahkan kursor pada grafik untuk melihat debit.</div><div class="flood-idle-chart-wrap"><canvas id="floodIdleHydrograph"></canvas></div></div>`;
      if (serial !== state.idleInspectSerial || state.controlPopup || !idleInspectionAllowed()) return;
      destroyIdleChart(); state.idlePopup?.remove?.();
      state.idlePopup = new maplibregl.Popup({ offset: 14, closeButton: true, maxWidth: '330px' }).setLngLat([Number(metric.snapped_lon),Number(metric.snapped_lat)]).setHTML(html).addTo(map);
      state.idlePopup.on('close', destroyIdleChart);
      renderIdleChart(metric,payload);
    } catch (_) {}
  }

  window.handleFloodRoutingMapClick = e => {
    // Existing control points own the click even while add mode is active.
    // The layer click handler opens the edit popup; prevent the map handler
    // from treating the same event as a request for a new point.
    if (state.addPointMode) {
      const clickedExistingPoint = e.features?.some(feature => [OBS_CIRCLE_LAYER, OBS_LABEL_LAYER].includes(feature?.layer?.id))
        || (() => {
          try {
            return map.queryRenderedFeatures(e.point, { layers: [OBS_CIRCLE_LAYER, OBS_LABEL_LAYER] }).length > 0;
          } catch (_) {
            return false;
          }
        })();
      if (clickedExistingPoint) return true;
      const nearbyPoint = nearestObservationPoint(e.lngLat.lng, e.lngLat.lat);
      if (nearbyPoint) {
        const metric = state.observationData?.points?.find(item => item.point_id === nearbyPoint.point.point_id) || nearbyPoint.point;
        openControlPointPopup(nearbyPoint.point, metric, nearbyPoint.coordinates);
        return true;
      }
    }
    if (state.movePointId) {
      moveObservationAt(state.movePointId, e.lngLat.lng, e.lngLat.lat).catch(() => {});
      return true;
    }
    if (state.addPointMode) {
      addObservationAt(e.lngLat.lng, e.lngLat.lat).catch(() => {});
      return true;
    }
    if (idleInspectionAllowed()) {
      inspectIdleLocation(e.lngLat.lng, e.lngLat.lat).catch(() => {});
      return true;
    }
    return false;
  };

  function chartCssColor(name, fallback) {
    try {
      const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return value || fallback;
    } catch (_) { return fallback; }
  }

  function setObservationChartReadout(chart, elements, payload, times) {
    const host = $('floodChartHoverReadout');
    if (!host || !elements?.length) return;
    const x = Math.max(0, Math.min((times?.length || 1) - 1, Math.round(Number(elements[0]?.element?.$context?.raw?.x ?? elements[0]?.index ?? 0))));
    const rows = (chart?.data?.datasets || []).map(dataset => {
      const raw = dataset?.data?.[x];
      const y = Number(raw?.y);
      if (!Number.isFinite(y)) return '';
      const color = String(dataset.borderColor || '#1d70b8');
      return `<span><i style="background:${escapeHtml(color)}"></i><span>${escapeHtml(dataset.label || 'Titik')}: <b>${escapeHtml(formatAxisNumber(y))} m³/det</b></span></span>`;
    }).filter(Boolean).join('');
    host.innerHTML = `<strong>Jam ke-${escapeHtml(formatFrameLabel(times?.[x] ?? '', x, payload?.interval))}</strong>${rows}`;
  }

  function renderObservationHydrograph(payload) {
    const host = $('floodObservationHydrograph');
    const canvas = $('floodObservationChart');
    const empty = $('floodChartEmpty');
    if (!host || !canvas) return;
    const points = payload?.points || [];
    const times = payload?.times || [];
    const usable = points.filter(item => Array.isArray(item.series) && item.series.some(v => Number.isFinite(Number(v))));
    if (!usable.length || !times.length) {
      host.classList.remove('has-chart');
      syncRightSidebarLayout();
      const missingInflow = points.some(item => item?.series_derivation === 'missing_dss_flow_combine');
      if (empty) empty.textContent = missingInflow
        ? 'Data debit pada sisi hulu sungai tidak tersedia untuk skenario ini.'
        : 'Hidrograf akan muncul setelah Q(t) titik tersedia.';
      if (state.chart) { try { state.chart.destroy(); } catch (_) {} state.chart = null; }
      syncRightSidebarLayout();
      return;
    }

    ensureChartRuntime().then(() => {
      if (!window.Chart || !canvas.isConnected) return;
      if (state.chart) { try { state.chart.destroy(); } catch (_) {} state.chart = null; }
      const values = [];
      usable.forEach(item => item.series.forEach(v => { const n = Number(v); if (Number.isFinite(n)) values.push(n); }));
      const rawMax = values.length ? Math.max(...values) : 0;
      const yAxis = niceAxisRange(rawMax || 1);
      const lockedYMax = yAxis.max;
      const indexForPoint = new Map(state.observationPoints.map((p, index) => [p.point_id, index]));
      const datasets = usable.map((item, fallbackIndex) => {
        const idx = indexForPoint.get(item.point_id) ?? fallbackIndex;
        const sourcePoint = state.observationPoints[idx] || item;
        return {
          label: observationDisplayName(sourcePoint),
          data: (item.series || []).map((raw, i) => ({ x: i, y: Number.isFinite(Number(raw)) ? Number(raw) : null })),
          borderColor: observationColor(sourcePoint, idx),
          backgroundColor: observationColor(sourcePoint, idx),
          borderWidth: 2.2,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHitRadius: 8,
          tension: .14,
          spanGaps: true,
        };
      });
      state.chart = new window.Chart(canvas, {
        type: 'line',
        data: { datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 160 },
          parsing: false,
          normalized: true,
          interaction: { mode: 'nearest', intersect: false, axis: 'x' },
          onHover: (_event, elements, chart) => setObservationChartReadout(chart, elements, payload, times),
          plugins: {
            legend: { display: true, position: 'bottom', labels: { color: chartCssColor('--text-muted','#667386'), usePointStyle: true, boxWidth: 7, boxHeight: 7, font: { size: 10 } } },
            tooltip: { enabled: false },
            zoom: {
              limits: { x: { min: 0, max: Math.max(0, times.length - 1), minRange: Math.min(4, Math.max(1, times.length - 1)) }, y: { min: 0, max: lockedYMax } },
              pan: { enabled: true, mode: 'x', threshold: 4 },
              zoom: { wheel: { enabled: true, speed: .08 }, pinch: { enabled: true }, drag: { enabled: false }, mode: 'x' },
            },
          },
          scales: {
            x: {
              type: 'linear', min: 0, max: Math.max(0, times.length - 1),
              title: { display: true, text: 'Jam ke-', color: chartCssColor('--text-muted','#667386'), font: { weight: '600', size: 10 } },
              ticks: { color: chartCssColor('--text-muted','#667386'), maxTicksLimit: 7, callback: value => formatFrameLabel(times[Math.max(0, Math.min(times.length - 1, Math.round(Number(value))))] ?? '', Math.round(Number(value)), payload?.interval) },
              grid: { color: chartCssColor('--border-color', '#e0e5ed') },
            },
            y: {
              type: 'linear', min: 0, max: lockedYMax,
              title: { display: true, text: 'Debit (m³/det)', color: chartCssColor('--text-muted','#667386'), font: { weight: '600', size: 10 } },
              beginAtZero: true,
              ticks: { color: chartCssColor('--text-muted','#667386'), stepSize: yAxis.step, callback: value => formatAxisNumber(value) },
              grid: { color: chartCssColor('--border-color', '#e0e5ed') },
            },
          },
        },
      });
      host.classList.add('has-chart');
      if (empty) empty.textContent = '';
    }).catch(err => {
      host.classList.remove('has-chart');
      syncRightSidebarLayout();
      if (empty) empty.textContent = `Chart.js belum dapat dimuat: ${err?.message || err}`;
    });
  }

  function renderObservationSummary(payload) {
    const pointHost = $('floodObservationPointsSummary');
    const segmentHost = $('floodObservationSegmentsSummary');
    const countEl = $('floodObservationCount');
    const points = payload?.points || [];
    if (countEl) countEl.textContent = `${points.length} titik`;

    if (pointHost) {
      if (!points.length) pointHost.innerHTML = '<div class="empty-state">Belum ada titik untuk dibandingkan.</div>';
      else {
        const indexForPoint = new Map(state.observationPoints.map((p, index) => [p.point_id, index]));
        pointHost.innerHTML = `<div class="flood-observation-point-grid">${points.map((item, fallbackIndex) => {
          const idx = indexForPoint.get(item.point_id) ?? fallbackIndex;
          const sourcePoint = state.observationPoints[idx] || item;
          const color = observationColor(sourcePoint, idx);
          const basinName = String(sourcePoint?.basin_name || sourcePoint?.official_basin?.name || '—');
          return `<article class="flood-observation-card" style="--point-color:${escapeHtml(color)}">
            <div class="flood-observation-card-head"><span class="flood-observation-name-chip" style="background:${escapeHtml(color)}">${escapeHtml(observationName(sourcePoint))}</span><b class="flood-observation-river">${escapeHtml(observationRiver(sourcePoint))}</b><small class="flood-observation-basin">DAS ${escapeHtml(basinName)}</small></div>
            <dl>
              <dt>Debit puncak (Qp)</dt><dd>${Number.isFinite(Number(item.peak_q)) ? `${formatNumberCompact(item.peak_q)} m³/det` : '—'}</dd>
              <dt>Waktu puncak</dt><dd>${escapeHtml(formatMetricTime(item.peak_time, item.peak_index, payload?.interval))}</dd>
            </dl>
          </article>`;
        }).join('')}</div>`;
      }
    }

    if (segmentHost) {
      const segments = payload?.segments || [];
      if (!segments.length) segmentHost.innerHTML = '<div class="empty-state">Tambahkan minimal dua titik untuk jarak dan selisih waktu puncak.</div>';
      else segmentHost.innerHTML = `<div class="flood-observation-segments">${segments.map(seg => `
        <div class="flood-observation-segment">
          <strong>${escapeHtml(seg.from_label)} → ${escapeHtml(seg.to_label)}</strong>
          ${seg.is_downstream_path ? '' : '<span class="flood-segment-warning">Cabang hulu berbeda sebelum pertemuan sungai</span>'}
          <span class="segment-label">Jarak sungai</span><span class="segment-value">${formatDistanceMeters(seg.distance_m)}</span>
          <span class="segment-label">Selisih waktu puncak</span><span class="segment-value">${formatLagMinutes(seg.peak_lag_minutes)}</span>
        </div>`).join('')}</div>`;
    }
    renderObservationHydrograph(payload);
    window.lucide?.createIcons?.();
  }

  async function refreshObservationComparison(force = false) {
    const sourcePoints = state.observationPoints;
    if (!sourcePoints.length) {
      state.observationData = null;
      state.observationKey = '';
      applyRoutingSelection({ routing_selection: { route_ids: [] } });
      renderObservationSummary({ points: [], segments: [], times: [] });
      return;
    }
    const payloadPoints = hydrographExportPoints();
    const key = JSON.stringify({ scenario: state.scenario, radius: currentSnapRadius(), points: payloadPoints });
    if (!force && state.observationKey === key && state.observationData) return renderObservationSummary(state.observationData);
    const serial = ++state.observationRequestSerial;
    setObservationStatus('Membaca hidrograf sesuai posisi hulu/hilir pada reach2d dan menghitung jarak serta selisih waktu puncak…', 'busy');
    try {
      const payload = await postJson('/api/hec-routing/observe', { scenario: state.scenario, snap_radius_m: currentSnapRadius(), points: payloadPoints });
      if (serial !== state.observationRequestSerial) return;
      if (payload?.errors?.length) showSnapError(payload.errors[0]);
      state.observationData = payload;
      state.observationKey = key;
      applyRoutingSelection(payload);
      for (const metric of payload.points || []) {
        const local = state.observationPoints.find(p => p.point_id === metric.point_id);
        if (local) { local.snapped_lon = metric.snapped_lon; local.snapped_lat = metric.snapped_lat; }
      }
      persistObservationPoints();
      refreshObservationMap();
      setObservationStatus('', 'ready');
      renderObservationSummary(payload);
    } catch (err) {
      if (serial !== state.observationRequestSerial) return;
      state.observationData = null;
      state.observationKey = '';
      renderObservationSummary({ points: sourcePoints, segments: [], times: [], units: 'm³/det' });
      if (err.code === 'dss_parser_unavailable') setObservationStatus('Titik sudah tersambung ke jaringan reach2d. Hidrograf Q(t) belum tersedia: jalankan preprocess_hms.bat setelah memasang pydsstools.', 'warning');
      else setObservationStatus(`Hidrograf/perbandingan belum dapat dimuat: ${err.message || err}`, 'warning');
    }
  }

  async function ensureReachData() {
    if (state.reachData) return state.reachData;
    const qs = scenarioQuery();
    state.reachData = await fetchJson(`/api/hec-routing/reaches${qs ? `?${qs}` : ''}`);
    return state.reachData;
  }


  function flowColorExpression(dark = document.documentElement.getAttribute('data-theme') === 'dark') {
    const colors = dark
      ? ['#22d3ee', '#38bdf8', '#818cf8', '#ff4d4d']
      : ['#004b57', '#0057d9', '#3b28cc', '#f00000'];
    return ['step', ['coalesce', ['feature-state', 'ratio'], 0], colors[0], 0.20, colors[1], 0.50, colors[2], 0.85, colors[3]];
  }

  function updateFlowContrastForTheme() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (map.getLayer(REACH_FLOW_LAYER)) map.setPaintProperty(REACH_FLOW_LAYER, 'line-color', flowColorExpression(dark));
    if (map.getLayer(REACH_FALLING_LAYER)) map.setPaintProperty(REACH_FALLING_LAYER, 'line-color', dark ? '#e879f9' : '#9b00d4');
  }

  async function ensureModelLayers() {
    const reaches = await ensureReachData();
    // Remove obsolete visual layers from pre-v2.2 styles; flood lines must have
    // no casing, halo, drop shadow, arrow, or secondary motion stroke.
    for (const legacy of [REACH_FLOW_HALO_LAYER, REACH_MOTION_LAYER]) {
      try { if (map.getLayer(legacy)) map.removeLayer(legacy); } catch (_) {}
    }
    // Legacy reference kept for regression tests: lineMetrics: true
    if (!map.getSource(REACH_SOURCE)) map.addSource(REACH_SOURCE, { type: 'geojson', data: reaches, promoteId: 'route_id', lineMetrics: false });
    else map.getSource(REACH_SOURCE)?.setData?.(reaches);
    if (!map.getLayer(REACH_BASE_LAYER)) map.addLayer({
      id: REACH_BASE_LAYER, type: 'line', source: REACH_SOURCE,
      layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#172033',
        'line-width': ['max', 1.1, ['*', ['coalesce', ['get', 'base_width'], 1.7], 0.78]],
        'line-opacity': 0.44,
      },
    });
    if (!map.getLayer(REACH_FLOW_LAYER)) map.addLayer({
      id: REACH_FLOW_LAYER, type: 'line', source: REACH_SOURCE,
      layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': flowColorExpression(),
        'line-width': flowWidthExpression(),
        'line-opacity': ['case', ['boolean', ['feature-state', 'falling'], false], 0, 0.98],
      },
    });
    if (!map.getLayer(REACH_FALLING_LAYER)) map.addLayer({
      id: REACH_FALLING_LAYER, type: 'line', source: REACH_SOURCE,
      layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': '#9b00d4',
        'line-width': flowWidthExpression(),
        'line-opacity': ['case', ['boolean', ['feature-state', 'falling'], false], 0.98, 0],
      },
    });
    if (!map.getLayer(REACH_HIT_LAYER)) map.addLayer({
      id: REACH_HIT_LAYER, type: 'line', source: REACH_SOURCE,
      layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': 'rgba(0,0,0,0)', 'line-width': 24, 'line-opacity': 0 },
      // Legacy reference kept for regression tests: 'line-width': ['max', 36, ['+', flowWidthExpression(), 26]]
    });
    updateFlowContrastForTheme();
    if (!map.getSource(OBS_SOURCE)) map.addSource(OBS_SOURCE, { type: 'geojson', data: observationGeoJson() });
    if (!map.getSource(CONTROL_PREVIEW_SOURCE)) map.addSource(CONTROL_PREVIEW_SOURCE, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    if (!map.getLayer(CONTROL_PREVIEW_CONNECTOR_LAYER)) map.addLayer({ id: CONTROL_PREVIEW_CONNECTOR_LAYER, type: 'line', source: CONTROL_PREVIEW_SOURCE, filter: ['==', ['get', 'kind'], 'connector'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#64748b', 'line-width': 1.5, 'line-dasharray': [2, 2], 'line-opacity': 0.9 } });
    if (!map.getLayer(CONTROL_PREVIEW_HALO_LAYER)) map.addLayer({ id: CONTROL_PREVIEW_HALO_LAYER, type: 'circle', source: CONTROL_PREVIEW_SOURCE, paint: { 'circle-radius': ['match',['get','kind'],'requested',8,7], 'circle-color': 'rgba(255,255,255,.92)', 'circle-stroke-color': ['match',['get','kind'],'requested','#ffffff','#64748b'], 'circle-stroke-width': 2.2 } });
    if (!map.getLayer(CONTROL_PREVIEW_POINT_LAYER)) map.addLayer({ id: CONTROL_PREVIEW_POINT_LAYER, type: 'circle', source: CONTROL_PREVIEW_SOURCE, paint: { 'circle-radius': ['match',['get','kind'],'requested',5.7,4.2], 'circle-color': ['match',['get','kind'],'requested','#ffffff','#223468'], 'circle-stroke-color': ['match',['get','kind'],'requested','#64748b','#ffffff'], 'circle-stroke-width': ['match',['get','kind'],'requested',1.7,1.8] } });
    try {
      map.setPaintProperty(CONTROL_PREVIEW_POINT_LAYER, 'circle-color', ['match', ['get', 'kind'], 'requested', '#ffffff', '#223468']);
      map.setPaintProperty(CONTROL_PREVIEW_POINT_LAYER, 'circle-stroke-color', ['match', ['get', 'kind'], 'requested', '#223468', '#ffffff']);
      map.setPaintProperty(CONTROL_PREVIEW_POINT_LAYER, 'circle-stroke-width', ['match', ['get', 'kind'], 'requested', 1.8, 2.2]);
      map.moveLayer(CONTROL_PREVIEW_POINT_LAYER);
    } catch (_) {}
    if (!map.getLayer(OBS_CIRCLE_LAYER)) map.addLayer({
      id: OBS_CIRCLE_LAYER, type: 'circle', source: OBS_SOURCE,
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 5, 12, 7.5, 16, 9], 'circle-color': ['get', 'color'], 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2.2 },
    });
    if (!map.getLayer(OBS_LABEL_LAYER)) map.addLayer({
      id: OBS_LABEL_LAYER, type: 'symbol', source: OBS_SOURCE,
      layout: { 'text-field': ['get', 'label'], 'text-size': 10.5, 'text-offset': [0, -1.45], 'text-allow-overlap': false, 'text-ignore-placement': false },
      paint: { 'text-color': '#17243a', 'text-halo-color': '#ffffff', 'text-halo-width': 1.6 },
    });
    // Basin names must stay legible above flood lines, while control points remain on top.
    try { if (map.getLayer('official-basin-label')) map.moveLayer('official-basin-label', CONTROL_PREVIEW_HALO_LAYER); } catch (_) {}
    bindHoverOnce();
    bindObservationClicksOnce();
  }

  function riverNameForHover(value) {
    let text = String(value || '').trim();
    if (!text) return 'Sungai Tanpa Nama';
    text = text.replace(/^(?:Kali|K\.|K|Sungai|S\.|S)\s+/i, '').trim();
    return text ? `Kali ${text}` : 'Sungai Tanpa Nama';
  }

  function renderedOfficialRiverName(point) {
    const layers = ['official-river-1', 'official-river-2', 'official-river-3', 'official-river-other']
      .filter(id => map.getLayer(id) && map.getLayoutProperty(id, 'visibility') !== 'none');
    if (!layers.length || !point) return null;
    try {
      const hits = map.queryRenderedFeatures([[point.x - 9, point.y - 9], [point.x + 9, point.y + 9]], { layers }) || [];
      const name = hits.map(feature => feature?.properties?.river_name).find(Boolean);
      return name ? riverNameForHover(name) : null;
    } catch (_) { return null; }
  }

  function lineHoverFraction(feature, lngLat) {
    const coords = feature?.geometry?.type === 'LineString' ? feature.geometry.coordinates : [];
    if (coords.length < 2) return 1;
    const lat0 = Number(lngLat.lat) * Math.PI / 180, cos = Math.max(.2, Math.cos(lat0));
    const px = Number(lngLat.lng) * cos, py = Number(lngLat.lat);
    let total=0,bestD=Infinity,bestAlong=0,walked=0; const lens=[];
    for(let i=1;i<coords.length;i++){const ax=Number(coords[i-1][0])*cos,ay=Number(coords[i-1][1]),bx=Number(coords[i][0])*cos,by=Number(coords[i][1]);const dx=bx-ax,dy=by-ay,len=Math.hypot(dx,dy);lens.push(len);total+=len;}
    for(let i=1;i<coords.length;i++){const ax=Number(coords[i-1][0])*cos,ay=Number(coords[i-1][1]),bx=Number(coords[i][0])*cos,by=Number(coords[i][1]);const dx=bx-ax,dy=by-ay,l2=dx*dx+dy*dy;const t=l2?Math.max(0,Math.min(1,((px-ax)*dx+(py-ay)*dy)/l2)):0;const qx=ax+t*dx,qy=ay+t*dy,d=(px-qx)**2+(py-qy)**2;if(d<bestD){bestD=d;bestAlong=walked+t*lens[i-1];}walked+=lens[i-1];}
    return total>0?Math.max(0,Math.min(1,bestAlong/total)):1;
  }

  // Legacy reference kept for regression tests: "Debit saat waktu t" and "Waktu t"
  function reachHoverHtml(reachId, feature, lngLat) {
    const fs = map.getFeatureState({ source: REACH_SOURCE, id: reachId }) || {};
    const fullFeature = (state.reachData?.features || []).find(item => String(item?.properties?.route_id || '') === String(reachId)) || feature;
    const upstreamHalf = lineHoverFraction(fullFeature,lngLat) < 0.5 && Number.isFinite(Number(fs.qin));
    const q = upstreamHalf ? Number(fs.qin) : Number(fs.qout);
    const peak = upstreamHalf && Number(fs.inflowPeak)>0 ? Number(fs.inflowPeak) : Number(fs.peak);
    const peakIndex = upstreamHalf && Number.isFinite(Number(fs.inflowPeakIndex)) ? Number(fs.inflowPeakIndex) : Number(fs.peakIndex);
    const resolvedName = resolveRouteRiverName(fullFeature, reachId);
    const riverName = riverNameForHover(typeof resolvedName === 'string' ? resolvedName : '');
    const currentTime = formatFrameLabel('', state.frame, state.series?.interval);
    const peakTime = formatFrameLabel('', peakIndex, state.series?.interval);
    return `<div class="map-hover-card flood-reach-hover-card"><strong class="flood-reach-hover-title">${escapeHtml(riverName)}</strong><dl><dt>Debit jam ke-${escapeHtml(currentTime)}</dt><dd>${Number.isFinite(q)?`${escapeHtml(formatNumberCompact(q))} m³/det`:'—'}</dd><dt>Waktu puncak</dt><dd>${escapeHtml(peakTime)}</dd><dt>Debit puncak (Qp)</dt><dd>${Number.isFinite(peak)&&peak>0?`${escapeHtml(formatNumberCompact(peak))} m³/det`:'—'}</dd></dl></div>`;
  }

  function clearReachHover() {
    if (state.hoverHideTimer) { clearTimeout(state.hoverHideTimer); state.hoverHideTimer = null; }
    state.hoverReachId = null;
    if (state.hoverPopup) { try { state.hoverPopup.remove(); } catch (_) {} state.hoverPopup = null; }
    syncFloodMapCursor();
  }

  // Legacy reference kept for regression tests: scheduleReachHoverClear(120)
  function scheduleReachHoverClear(delay = 120) {
    if (state.hoverHideTimer) clearTimeout(state.hoverHideTimer);
    state.hoverPopup?.getElement?.()?.classList.add('is-leaving');
    state.hoverHideTimer = setTimeout(() => { state.hoverHideTimer = null; clearReachHover(); }, delay);
  }

  let hoverBound = false;
  function bindHoverOnce() {
    if (hoverBound) return;
    hoverBound = true;
    window.addEventListener('map-hover-owner', event => {
      if (event?.detail?.owner !== 'hec-routing') clearReachHover();
    });
    map.on('mousemove', e => {
      if (!state.routingVisualizationVisible || state.controlPopup || !map.getLayer(REACH_HIT_LAYER)) {
        if (state.hoverPopup) scheduleReachHoverClear(80);
        return;
      }
      let feature = null;
      try { feature = (map.queryRenderedFeatures(e.point, { layers: [REACH_HIT_LAYER] }) || [])[0] || null; } catch (_) {}
      if (!feature) {
        syncFloodMapCursor();
        if (state.hoverPopup) scheduleReachHoverClear(85);
        return;
      }
      if (state.hoverHideTimer) { clearTimeout(state.hoverHideTimer); state.hoverHideTimer = null; }
      state.hoverPopup?.getElement?.()?.classList.remove('is-leaving');
      const rid = String(feature?.properties?.route_id || '');
      if (!rid) return;
      state.hoverReachId = rid;
      window.dispatchEvent(new CustomEvent('map-hover-owner', { detail: { owner: 'hec-routing' } }));
      if (!state.hoverPopup) {
        state.hoverPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12, anchor: 'bottom', className: 'map-hover-popup flood-reach-hover-popup' });
        state.hoverPopup.addTo(map);
      }
      // Reuse one popup while crossing reaches; only its position/content changes.
      state.hoverPopup.setLngLat(e.lngLat).setHTML(reachHoverHtml(rid, feature, e.lngLat));
      ensureRouteRiverName(rid, feature).then(name => {
        if (!name || state.hoverReachId !== rid || !state.hoverPopup) return;
        state.hoverPopup.setLngLat(e.lngLat).setHTML(reachHoverHtml(rid, feature, e.lngLat));
      }).catch(() => {});
      if (!state.addPointMode && !state.movePointId) map.getCanvas().style.cursor = 'pointer';
      else syncFloodMapCursor();
    });
    map.getCanvasContainer?.().addEventListener?.('mouseleave', () => { syncFloodMapCursor(); scheduleReachHoverClear(95); });
  }


  function closeControlPointPopup() {
    if (state.controlPopup) { try { state.controlPopup.remove(); } catch (_) {} state.controlPopup = null; }
  }

  function openControlPointPopup(point, metric, coordinates) {
    state.idleInspectSerial += 1;
    clearReachHover();
    destroyIdleChart();
    if (state.idlePopup) { try { state.idlePopup.remove(); } catch (_) {} state.idlePopup = null; }
    closeControlPointPopup();
    state.suppressIdleUntil = Date.now() + 350;
    const idx = Math.max(0, state.observationPoints.findIndex(p => p.point_id === point.point_id));
    const color = observationColor(point, idx);
    const basin = String(point.basin_name || point.official_basin?.name || '—');
    const coord = observationCoordinateText(point);
    const html = `<div class="flood-control-map-card" style="--point-color:${escapeHtml(color)}">
      <div class="flood-control-map-head"><span class="flood-observation-name-chip">${escapeHtml(observationName(point))}</span><b class="flood-control-map-river">${escapeHtml(observationRiver(point))}</b><small class="flood-control-map-basin">DAS ${escapeHtml(basin)}</small></div>
      <dl><dt>Debit puncak (Qp)</dt><dd>${Number.isFinite(Number(metric?.peak_q)) ? `${escapeHtml(formatNumberCompact(metric.peak_q))} m³/det` : '—'}</dd><dt>Waktu puncak</dt><dd>${escapeHtml(formatMetricTime(metric?.peak_time, metric?.peak_index, state.observationData?.interval))}</dd></dl>
      <label class="flood-control-popup-name flood-point-name-editor"><span class="flood-control-popup-name-row"><span>Nama titik</span><span class="point-name-feedback"><span class="point-name-state unsaved-indicator hidden" aria-live="polite">Belum disimpan</span></span></span><div><input type="text" maxlength="25" data-saved-value="${escapeHtml(observationName(point))}" value="${escapeHtml(observationName(point))}"/><button type="button" class="flood-control-popup-save" disabled aria-label="Simpan nama"><i data-lucide="save"></i></button></div></label>
      <div class="flood-control-popup-actions"><button type="button" data-action="copy"><i data-lucide="copy"></i><span>Salin</span></button><button type="button" data-action="move"><i data-lucide="move"></i><span>Pindah</span></button><button type="button" data-action="color"><i data-lucide="palette"></i><span>Warna</span></button><button type="button" class="danger" data-action="remove"><i data-lucide="trash-2"></i><span>Hapus</span></button><input class="flood-control-popup-color" type="color" value="${escapeHtml(color)}" tabindex="-1" /></div>
    </div>`;
    const popup = new maplibregl.Popup({ offset: 14, closeButton: true, closeOnClick: false, maxWidth: '285px', className: 'existing-point-popup flood-control-point-popup' })
      .setLngLat(coordinates).setHTML(html).addTo(map);
    state.controlPopup = popup;
    popup.on('close', () => { if (state.controlPopup === popup) state.controlPopup = null; });
    const root = popup.getElement();
    const input = root?.querySelector('.flood-control-popup-name input');
    const save = root?.querySelector('.flood-control-popup-save');
    bindNameLimit(input);
    input?.addEventListener('input', () => syncPointNameFeedback(root?.querySelector('.flood-control-popup-name'), input));
    if (input) syncPointNameFeedback(root?.querySelector('.flood-control-popup-name'), input);
    save?.addEventListener('click', () => { renameObservationPoint(point.point_id, input?.value); closeControlPointPopup(); });
    root?.querySelector('[data-action="copy"]')?.addEventListener('click', e => copyText(coord, e.currentTarget));
    root?.querySelector('[data-action="move"]')?.addEventListener('click', () => { closeControlPointPopup(); armMoveObservationPoint(point.point_id); });
    const colorInput = root?.querySelector('.flood-control-popup-color');
    root?.querySelector('[data-action="color"]')?.addEventListener('click', () => colorInput?.click());
    colorInput?.addEventListener('input', () => { updateObservationColor(point.point_id, colorInput.value); closeControlPointPopup(); });
    root?.querySelector('[data-action="remove"]')?.addEventListener('click', () => { closeControlPointPopup(); removeObservationPoint(point.point_id); });
    window.lucide?.createIcons?.();
  }

  let observationClicksBound = false;
  function bindObservationClicksOnce() {
    if (observationClicksBound) return;
    observationClicksBound = true;
    map.on('mouseenter', OBS_CIRCLE_LAYER, () => {
      if (!state.movePointId) {
        try { map.getContainer().classList.add('flood-existing-point-hover'); } catch (_) {}
        try { map.getCanvas().style.cursor = 'pointer'; } catch (_) {}
      }
    });
    map.on('mouseleave', OBS_CIRCLE_LAYER, () => {
      try { map.getContainer().classList.remove('flood-existing-point-hover'); } catch (_) {}
      syncFloodMapCursor();
    });
    map.on('click', OBS_CIRCLE_LAYER, e => {
      if (state.movePointId) return;
      try { e.originalEvent.__floodObservationPointHandled = true; } catch (_) {}
      try { e.originalEvent?.stopPropagation?.(); } catch (_) {}
      const feature = e.features?.[0];
      const point = state.observationPoints.find(p => p.point_id === feature?.properties?.point_id);
      if (!point) return;
      const metric = state.observationData?.points?.find(p => p.point_id === point.point_id) || point;
      openControlPointPopup(point, metric, feature.geometry.coordinates);
    });
  }

  function syncRightSidebarLayout() {
    const right = $('floodRightSidebar');
    const comparison = panelForKey('comparison');
    const chart = panelForKey('chart');
    if (!right || !comparison || !chart) return;
    comparison.style.flex = '';
    comparison.style.maxHeight = '';
    chart.style.flex = '';
    chart.style.maxHeight = '';
    const comparisonVisible = !comparison.classList.contains('is-hidden');
    const chartVisible = !chart.classList.contains('is-hidden');
    if (!(comparisonVisible && chartVisible)) return;
    const count = Math.max(0, state.observationPoints.length);
    const chartShare = count >= 6 ? 0.70 : count >= 4 ? 0.66 : count >= 3 ? 0.62 : count >= 2 ? 0.58 : 0.54;
    const comparisonShare = Math.max(0.30, 1 - chartShare);
    comparison.style.flex = `${comparisonShare} 1 0%`;
    comparison.style.maxHeight = `${Math.round(comparisonShare * 100)}%`;
    chart.style.flex = `${chartShare} 1 0%`;
    chart.style.maxHeight = `${Math.round(chartShare * 100)}%`;
    right.classList.toggle('is-chart-priority', count >= 3);
  }

  function applyLayerVisibility() {
    const showRouting = Boolean(state.routingVisualizationVisible && state.selectedReachIds.length);
    const showFlow = showRouting && Boolean(state.series);
    setLayerVisible(REACH_BASE_LAYER, false);
    setLayerVisible(REACH_FLOW_HALO_LAYER, false);
    setLayerVisible(REACH_MOTION_LAYER, false);
    setLayerVisible(REACH_FLOW_LAYER, showFlow);
    setLayerVisible(REACH_FALLING_LAYER, showFlow);
    setLayerVisible(REACH_HIT_LAYER, showFlow);
    if (!showRouting) clearReachHover();
  }

  async function loadSelectedSeries(ids) {
    const key = [...ids].sort().join(',');
    if (!key) return;
    const serial = ++state.requestSerial;
    stopAnimation();
    state.series = null;
    state.seriesKey = '';
    const slider = $('floodTimeSlider'), play = $('floodPlayBtn');
    const transport = ['floodFastBackBtn','floodPrevBtn','floodPlayBtn','floodNextBtn','floodFastForwardBtn'].map($).filter(Boolean);
    if (slider) slider.disabled = true;
    transport.forEach(btn => { btn.disabled = true; });
    setLayerVisible(REACH_FLOW_HALO_LAYER, false);
    setLayerVisible(REACH_FLOW_LAYER, false);
    setLayerVisible(REACH_FALLING_LAYER, false);
    setLayerVisible(REACH_MOTION_LAYER, false);
    setStatus('Memuat Q(t) seluruh jaringan dari precompute HEC-HMS…', 'busy');
    try {
      const qs = new URLSearchParams({ reach_ids: ids.join(',') });
      if (state.scenario) qs.set('scenario', state.scenario);
      const payload = await fetchJson(`/api/hec-routing/series?${qs.toString()}`);
      if (serial !== state.requestSerial) return;
      state.series = payload;
      state.seriesKey = key;
      state.frame = 0;
      if (slider) { slider.min = '0'; slider.max = String(Math.max(0, (payload.times?.length || 1) - 1)); slider.value = '0'; slider.disabled = !(payload.times?.length > 0); }
      transport.forEach(btn => { btn.disabled = !(payload.times?.length > 1); });
      setStatus('', 'ready');
      applyLayerVisibility();
      renderFrame(0);
    } catch (err) {
      if (serial !== state.requestSerial) return;
      resetFeatureStates(ids);
      state.series = null;
      applyLayerVisibility();
      if (err.code === 'dss_parser_unavailable') setStatus('Jaringan sungai sudah aktif. Q(t) belum tersedia: jalankan preprocess_hms.bat untuk membuat data/hms dari source HEC-HMS.', 'warning');
      else setStatus(`Q(t) belum dapat dimuat: ${err.message || err}`, 'warning');
    }
  }

  function segmentApproxLength(a, b) {
    const dx = (Number(b[0]) - Number(a[0])) * Math.cos(((Number(a[1]) + Number(b[1])) * Math.PI / 360));
    const dy = Number(b[1]) - Number(a[1]);
    return Math.hypot(dx, dy);
  }

  function clipLineCoordinates(coords, fraction) {
    if (!Array.isArray(coords) || coords.length < 2) return coords;
    const f = Math.max(0, Math.min(1, Number(fraction)));
    if (f >= 0.999999) return coords;
    const lengths = [];
    let total = 0;
    for (let i = 1; i < coords.length; i++) { const d = segmentApproxLength(coords[i - 1], coords[i]); lengths.push(d); total += d; }
    if (!(total > 0)) return coords.slice(0, 2);
    const target = total * f;
    const out = [coords[0]];
    let walked = 0;
    for (let i = 1; i < coords.length; i++) {
      const d = lengths[i - 1];
      if (walked + d >= target) {
        const t = d > 0 ? Math.max(0, Math.min(1, (target - walked) / d)) : 0;
        const a = coords[i - 1], b = coords[i];
        out.push([Number(a[0]) + (Number(b[0]) - Number(a[0])) * t, Number(a[1]) + (Number(b[1]) - Number(a[1])) * t]);
        break;
      }
      out.push(coords[i]); walked += d;
    }
    return out.length >= 2 ? out : coords.slice(0, 2);
  }

  function routingGeoJsonForSelection(selection) {
    const base = state.reachData || { type: 'FeatureCollection', features: [] };
    const ids = new Set((selection?.route_ids || []).map(String));
    const boundaryId = String(selection?.downstream_route_id || '');
    const boundaryFraction = Number(selection?.downstream_chainage_fraction);
    const features = (base.features || []).filter(f => ids.has(String(f?.properties?.route_id || ''))).map(feature => {
      if (String(feature?.properties?.route_id || '') !== boundaryId || !Number.isFinite(boundaryFraction)) return feature;
      const geom = feature?.geometry;
      if (geom?.type !== 'LineString') return feature;
      return { ...feature, geometry: { ...geom, coordinates: clipLineCoordinates(geom.coordinates, boundaryFraction) } };
    });
    return { type: 'FeatureCollection', features };
  }

  function applyRoutingSelection(payload) {
    const selection = payload?.routing_selection || null;
    const ids = Array.isArray(selection?.route_ids) ? selection.route_ids.map(String) : [];
    state.routingSelection = selection;
    const previous = state.selectedReachIds.slice();
    if (previous.length) resetFeatureStates(previous);
    state.selectedReachIds = ids;
    const selectedGeoJson = ids.length ? routingGeoJsonForSelection(selection) : (state.reachData || { type: 'FeatureCollection', features: [] });
    map.getSource(REACH_SOURCE)?.setData?.(selectedGeoJson);
    const filter = selectionFilter('route_id', ids);
    for (const layer of [REACH_BASE_LAYER, REACH_FLOW_HALO_LAYER, REACH_FLOW_LAYER, REACH_FALLING_LAYER, REACH_MOTION_LAYER, REACH_HIT_LAYER]) {
      if (map.getLayer(layer)) map.setFilter(layer, filter);
    }
    if (!ids.length) {
      stopAnimation();
      state.series = null;
      state.seriesKey = '';
      syncOfficialRiverTemporaryHide();
      applyLayerVisibility();
      if (state.routingVisualizationVisible && !state.observationPoints.length) setStatus('Tambahkan minimal satu Titik Kontrol untuk menampilkan Visualisasi Aliran.', 'warning');
      else if (selection?.reason === 'separate_upstream_branches') setStatus(selection?.message || 'Titik Kontrol berada pada cabang hulu berbeda sebelum pertemuan sungai.', 'warning');
      else setStatus('', 'neutral');
      return;
    }
    syncOfficialRiverTemporaryHide();
    if (state.routingVisualizationVisible) loadSelectedSeries(ids).catch(() => {});
  }

  async function initializeGlobalRouting() {
    await ensureModelLayers();
    state.selectedReachIds = [];
    state.routingSelection = null;
    map.getSource(REACH_SOURCE)?.setData?.(state.reachData || { type: 'FeatureCollection', features: [] });
    const filter = emptySelectionFilter('route_id');
    for (const layer of [REACH_BASE_LAYER, REACH_FLOW_HALO_LAYER, REACH_FLOW_LAYER, REACH_FALLING_LAYER, REACH_MOTION_LAYER, REACH_HIT_LAYER]) {
      if (map.getLayer(layer)) map.setFilter(layer, filter);
    }
    applyLayerVisibility();
  }

  function panelForKey(key) {
    const ids = { routing: 'floodRoutingPanel', comparison: 'floodObservationPanel', chart: 'floodChartPanel' };
    return $(ids[key] || '');
  }

  function syncDockedPanels() {
    const comparison = panelForKey('comparison');
    const chart = panelForKey('chart');
    const right = $('floodRightSidebar');
    const visibleRightPanels = [comparison, chart].filter(panel => panel && !panel.classList.contains('is-hidden'));
    const rightOpen = visibleRightPanels.length > 0;
    right?.classList.toggle('is-hidden', !rightOpen);
    right?.classList.toggle('is-single', visibleRightPanels.length === 1);
    $('spatialWorkspace')?.classList.toggle('flood-right-open', rightOpen);
    document.querySelectorAll('.flood-window-launcher[data-flood-window]').forEach(button => {
      const panel = panelForKey(button.dataset.floodWindow);
      const visible = Boolean(panel && !panel.classList.contains('is-hidden'));
      button.classList.toggle('is-window-hidden', !visible);
      button.setAttribute('aria-pressed', visible ? 'true' : 'false');
      button.title = visible ? 'Sembunyikan panel' : 'Tampilkan panel';
    });
    const routing = panelForKey('routing');
    setRoutingVisualizationVisible(Boolean(routing && !routing.classList.contains('is-hidden')));
    syncOfficialRiverTemporaryHide();
    syncRightSidebarLayout();
    requestAnimationFrame(() => state.chart?.resize?.());
  }

  function toggleDockedPanel(key, forceVisible = null) {
    const panel = panelForKey(key);
    if (!panel) return;
    const show = forceVisible == null ? panel.classList.contains('is-hidden') : Boolean(forceVisible);
    panel.classList.toggle('is-hidden', !show);
    syncDockedPanels();
  }

  function setChartExpanded(expanded) {
    const panel = $('floodChartPanel');
    const backdrop = $('floodChartBackdrop');
    if (!panel) return;
    const shouldExpand = Boolean(expanded);
    if (shouldExpand && !state.chartPortalPlaceholder) {
      const home = panel.parentNode;
      if (home) {
        const placeholder = document.createComment('flood-chart-panel-home');
        home.insertBefore(placeholder, panel);
        state.chartPortalPlaceholder = placeholder;
        ($('spatialWorkspace') || document.body).appendChild(panel);
      }
    }
    panel.classList.toggle('is-expanded', shouldExpand);
    backdrop?.classList.toggle('hidden', !shouldExpand);
    if (!shouldExpand && state.chartPortalPlaceholder) {
      const placeholder = state.chartPortalPlaceholder;
      if (placeholder.parentNode) placeholder.parentNode.insertBefore(panel, placeholder);
      placeholder.remove();
      state.chartPortalPlaceholder = null;
    }
    const button = $('floodChartExpandBtn');
    if (button) {
      button.innerHTML = `<i data-lucide="${shouldExpand ? 'minimize-2' : 'maximize-2'}"></i>`;
      button.title = shouldExpand ? 'Perkecil grafik' : 'Perbesar grafik';
      button.setAttribute('aria-label', button.title);
    }
    window.lucide?.createIcons?.();
    setTimeout(() => state.chart?.resize?.(), 60);
  }

  function initDockedPanels() {
    ['routing', 'comparison', 'chart'].forEach(key => panelForKey(key)?.classList.add('is-hidden'));
    $('floodRightSidebar')?.classList.add('is-hidden');
    document.querySelectorAll('.flood-window-launcher[data-flood-window]').forEach(button => {
      button.addEventListener('click', () => toggleDockedPanel(button.dataset.floodWindow));
    });
    document.querySelectorAll('[data-flood-window] .flood-window-hide').forEach(button => {
      button.addEventListener('click', event => {
        const panel = event.currentTarget.closest('[data-flood-window]');
        if (panel) {
          if (panel.dataset.floodWindow === 'chart') setChartExpanded(false);
          toggleDockedPanel(panel.dataset.floodWindow, false);
        }
      });
    });
    syncDockedPanels();
  }

  function populateScenarioSelect() {
    const select = $('floodReturnPeriodSelect');
    if (!select) return;
    const scenarios = Array.isArray(state.info?.scenarios) ? state.info.scenarios : [];
    select.innerHTML = '';
    if (!scenarios.length) {
      const option = document.createElement('option'); option.value = ''; option.textContent = 'Belum tersedia'; select.appendChild(option); select.disabled = true; state.scenario = null; return;
    }
    for (const item of scenarios) {
      const option = document.createElement('option'); option.value = String(item.id); option.textContent = String(item.label || item.id); select.appendChild(option);
    }
    state.scenario = String(state.info?.default_scenario || scenarios[0].id);
    if (![...select.options].some(o => o.value === state.scenario)) state.scenario = String(scenarios[0].id);
    select.value = state.scenario; select.disabled = false;
  }

  async function refreshModeledRivers() {
    try { window.setFloodModeledRiverScenario?.(state.scenario || ''); } catch (_) {}
  }

  async function changeScenario(value) {
    const next = String(value || '').trim();
    if (!next || next === state.scenario) return;
    stopAnimation();
    resetFeatureStates(state.selectedReachIds);
    state.scenario = next;
    state.reachData = null;
    state.series = null;
    state.seriesKey = '';
    state.observationData = null;
    state.observationKey = '';
    await refreshModeledRivers();
    await initializeGlobalRouting();
    await refreshObservationComparison(true);
  }

  async function initialize() {
    initDockedPanels();
    const flowThemeObserver = new MutationObserver(() => updateFlowContrastForTheme());
    flowThemeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    map.on('zoom', positionAddPointDialog);
    map.on('move', positionAddPointDialog);
    map.on('resize', positionAddPointDialog);
    try {
      state.info = await fetchJson('/api/hec-routing/info');
      populateScenarioSelect();
      await refreshModeledRivers();
      await initializeGlobalRouting();
    } catch (err) {
      setStatus(`Jaringan HEC-HMS belum dapat dimuat: ${err.message || err}`, 'warning');
      return;
    }

    $('floodPlayBtn')?.addEventListener('click', toggleAnimation);
    $('floodPrevBtn')?.addEventListener('click', () => stepFrame(-1));
    $('floodNextBtn')?.addEventListener('click', () => stepFrame(1));
    $('floodFastBackBtn')?.addEventListener('click', () => stepFrame(-12));
    $('floodFastForwardBtn')?.addEventListener('click', () => stepFrame(12));
    $('floodSlowerBtn')?.addEventListener('click', () => changePlaybackRate(-1));
    $('floodFasterBtn')?.addEventListener('click', () => changePlaybackRate(1));
    updatePlaybackRateUi();
    $('floodTimeSlider')?.addEventListener('input', e => { stopAnimation(); renderFrame(Number(e.target.value)); });
    $('floodAddPointBtn')?.addEventListener('click', () => setAddPointMode(!state.addPointMode));
    $('floodClearPointsBtn')?.addEventListener('click', clearObservationPoints);
    $('floodToggleAllPointsBtn')?.addEventListener('click', toggleAllObservationVisibility);
    $('floodFocusAllPointsBtn')?.addEventListener('click', focusAllObservationPoints);
    $('floodPreviewCoordinateBtn')?.addEventListener('click', previewCoordinateInput);
    $('floodCoordinateInput')?.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); previewCoordinateInput(); } });
    const decimalSelect = $('floodDecimalSeparatorSelect');
    decimalSelect?.addEventListener('change', () => {
      refreshRoutingLegendLabels();
      if (state.coordinatePreview) showControlCoordinatePreview(state.coordinatePreview.lon, state.coordinatePreview.lat);
      renderFloodPointList();
      if (state.observationData) renderObservationSummary(state.observationData);
      if (state.series) renderFrame(state.frame);
      try { state.chart?.update?.('none'); } catch (_) {}
      try { state.idleChart?.update?.('none'); } catch (_) {}
      if (state.idlePopup) state.idlePopup.remove();
    });
    $('floodReturnPeriodSelect')?.addEventListener('change', event => {
      changeScenario(event.target.value).catch(err => setStatus(`Kala ulang belum dapat dimuat: ${err.message || err}`, 'warning'));
    });
    $('snapRadius')?.addEventListener('change', () => {
      state.observationKey = '';
      refreshObservationComparison(true).catch(() => {});
    });
    $('acceptModelUnavailable')?.addEventListener('click', () => hideModal('modelUnavailableModal'));
    $('floodAddPointCancelBtn')?.addEventListener('click', closeAddPointDialog);
    $('floodAddPointCancelBottomBtn')?.addEventListener('click', closeAddPointDialog);
    $('floodAddPointCommitBtn')?.addEventListener('click', () => commitPendingAddPoint().catch(() => {}));
    $('floodAddCopyCoordsBtn')?.addEventListener('click', event => copyText($('floodAddRequestedCoords')?.textContent || '', event.currentTarget));
    $('floodAddPointModal')?.addEventListener('pointerdown', event => { if (event.target === $('floodAddPointModal')) closeAddPointDialog(); });
    $('floodChartResetBtn')?.addEventListener('click', () => state.chart?.resetZoom?.());
    $('floodChartDownloadBtn')?.addEventListener('click', () => downloadHydrographXlsx().catch(() => {}));
    $('floodChartExpandBtn')?.addEventListener('click', () => setChartExpanded(!$('floodChartPanel')?.classList.contains('is-expanded')));
    $('floodChartBackdrop')?.addEventListener('click', () => setChartExpanded(false));

    refreshRoutingLegendLabels();
    renderFloodPointList();
    refreshObservationMap();
    refreshObservationComparison().catch(() => {});
    syncRightSidebarLayout();
    window.lucide?.createIcons?.();
  }

  if (map.loaded()) initialize();
  else map.once('load', initialize);
})();
