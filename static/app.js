const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

// ---------- static demo copy (overwritten by live/backend data where noted) ----------
let alerts = [];
let insights = [
  ['🚘', 'High probability of congestion', '78% chance of heavy traffic at ITO in the next 30 minutes.'],
  ['≋', 'Increased flood risk', 'Low-lying areas may be affected after three hours of rain.'],
  ['💡', 'Optimize signal timing', 'Five intersections could reduce congestion by 18%.'],
];
const views = {
  overview: ['City overview', 'Operational summary across mobility, environment, safety and infrastructure.', 'Overview'],
  traffic: ['Traffic operations', 'Forecast below is produced by a blended tree ensemble trained on real historical traffic data.', 'Traffic'],
  weather: ['Weather intelligence', 'Live weather card (top-left of the map) is pulled from Open-Meteo in real time.', 'Weather'],
  air: ['Air quality monitor', 'AQI shown is a live reading for the selected city.', 'Air Quality'],
  flood: ['Flood risk analysis', 'Rain-derived screening risk uses live rainfall; enable the Flood Risk layer to see the exposure overlay on the map.', 'Flood Risk'],
  incidents: ['Incident command', 'One active road accident needs rerouting; emergency units are within response target.', 'Incidents'],
  transport: ['Public transport', 'Demo feed — connect a transit provider for live vehicle positions.', 'Public Transport'],
  energy: ['Energy & utilities', 'Demo feed — connect a utility provider for live load data.', 'Energy & Utilities'],
  simulation: ['Scenario simulation', 'Adjust rainfall, road block and traffic controls below, then run the scenario against the backend.', 'Simulation'],
  assistant: ['AI assistant', 'The assistant is monitoring traffic, flood, and air-quality risk for the selected city.', 'AI Assistant'],
  reports: ['Reports', 'Snapshot of today\u2019s key indicators.', 'Reports'],
};
const profiles = ['Naman', 'Aarav', 'Meera'];
let activeProfile = 0;

// ---------- state ----------
let map, tileLayer, floodHeat;
let trafficHighlight;
const layerGroups = { traffic: null, incidents: null, flood: null, ai: null };
let overview = null;
let modelInfo = null;
let currentCity = 'New Delhi';
let latestWeather = { rainMm: 0, tempC: 27, clouds: 40, weatherMain: 'Clouds', aqi: 0 };
let latestSignals = null;
let currentUser = null;
const sparkBuffers = { traffic: [], aqi: [], flood: [], power: [], transport: [] };
const MAX_SPARK = 24;

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), 2800);
}

function initAmbientScene() {
  const target = $('#vanta-bg');
  if (!target || !window.VANTA?.GLOBE || !window.THREE) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  try {
    window.cityTwinVanta = window.VANTA.GLOBE({
      el: target,
      mouseControls: true,
      touchControls: true,
      gyroControls: false,
      minHeight: 200,
      minWidth: 200,
      scale: 1,
      scaleMobile: 0.78,
      color: 0x3fd7ff,
      color2: 0xffb347,
      backgroundColor: 0x06111e,
      backgroundAlpha: 1,
      size: 1.05,
    });
  } catch (err) {
    console.warn('Ambient 3D background unavailable.', err);
  }
}

// ---------- Leaflet map ----------
function initMap() {
  try {
    map = L.map('map', { zoomControl: false, attributionControl: true }).setView([28.6139, 77.2090], 12);
    tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map);
    layerGroups.traffic = L.layerGroup().addTo(map);
    layerGroups.incidents = L.layerGroup().addTo(map);
    layerGroups.flood = L.layerGroup().addTo(map);
    layerGroups.ai = L.layerGroup().addTo(map);
    map.on('click', event => highlightTrafficAt(event.latlng));
    tileLayer.once('load', () => $('#map-fallback').classList.add('hidden'));
  } catch (err) {
    console.error('Map failed to initialize (Leaflet CDN unreachable?)', err);
    $('#map-detail').textContent = 'Map library failed to load \u2014 check your internet connection and reload.';
  }
}

function badgeIcon(name, label, status, isAI) {
  const html = `<div class="marker-wrap">
      <div class="map-dot status-${status}"></div>
      <div class="map-badge status-${status} ${isAI ? 'ai-suggestion' : ''}"><b>${isAI ? '\u2726 AI Suggestion' : name}</b><small>${label}</small></div>
    </div>`;
  return L.divIcon({ html, className: '', iconSize: [0, 0], iconAnchor: [0, 0] });
}

function renderAssetMarkers(assets, aiSuggestions) {
  if (!map) return;
  layerGroups.traffic.clearLayers();
  layerGroups.incidents.clearLayers();
  layerGroups.flood.clearLayers();
  layerGroups.ai.clearLayers();
  (assets || []).forEach(asset => {
    const icon = badgeIcon(asset.name, asset.label, asset.status, false);
    const marker = L.marker([asset.lat, asset.lng], { icon }).bindPopup(`<b>${asset.name}</b><br>${asset.label}`);
    const group = asset.type === 'incident' ? layerGroups.incidents : asset.type === 'flood' ? layerGroups.flood : layerGroups.traffic;
    marker.addTo(group);
  });
  (aiSuggestions || []).forEach(s => {
    const icon = badgeIcon('AI Suggestion', s.text, 'normal', true);
    L.marker([s.lat, s.lng], { icon }).bindPopup(`<b>AI Suggestion</b><br>${s.text}`).addTo(layerGroups.ai);
  });
  buildFloodHeat(assets);
}

function buildFloodHeat(assets) {
  // Stable illustrative exposure overlay: flood-tagged assets receive the
  // strongest weight, without random movement between renders.
  if (!map) return;
  if (floodHeat) map.removeLayer(floodHeat);
  const points = [];
  const offsets = [[-0.004, -0.006], [0.003, 0.005], [-0.002, 0.004], [0.005, -0.003], [-0.006, 0.002], [0.001, -0.005]];
  (assets || []).forEach(a => {
    const rainBoost = Math.min(0.35, latestWeather.rainMm / 100);
    const weight = Math.min(1, (a.type === 'flood' ? 0.8 : a.status === 'high' ? 0.45 : 0.2) + rainBoost);
    offsets.forEach(([latOffset, lngOffset]) => {
      points.push([a.lat + latOffset, a.lng + lngOffset, weight]);
    });
  });
  floodHeat = L.heatLayer(points, { radius: 35, blur: 25, maxZoom: 14, gradient: { 0.2: '#19c8ef', 0.5: '#ffe22f', 0.8: '#ff8b31', 1: '#e74343' } });
  if ($('#flood-layer-toggle').checked) floodHeat.addTo(map);
}

function setCityMarker(place) {
  if (!map) return;
  layerGroups.traffic.clearLayers();
  layerGroups.incidents.clearLayers();
  layerGroups.flood.clearLayers();
  layerGroups.ai.clearLayers();
  if (floodHeat) map.removeLayer(floodHeat);
  const icon = badgeIcon(place.name, `${Math.round(latestWeather.tempC)}\u00b0C \u00b7 AQI live`, 'normal', false);
  L.marker([place.latitude, place.longitude], { icon }).bindPopup(`<b>${place.name}</b>`).addTo(layerGroups.traffic);
  map.setView([place.latitude, place.longitude], 11);
}

function nearestAsset(latlng) {
  return (overview?.assets || []).reduce((nearest, asset) => {
    const distance = Math.hypot((asset.lat - latlng.lat) * 111, (asset.lng - latlng.lng) * 96);
    return !nearest || distance < nearest.distance ? { asset, distance } : nearest;
  }, null)?.asset;
}

async function highlightTrafficAt(latlng) {
  const asset = nearestAsset(latlng);
  const location = asset?.name || currentCity;
  const params = new URLSearchParams({
    location,
    rain_mm: latestWeather.rainMm,
    temp_c: latestWeather.tempC,
    clouds: latestWeather.clouds,
    weather: latestWeather.weatherMain,
    aqi: latestWeather.aqi,
  });
  try {
    const data = await fetch(`/api/predictions/traffic?${params}`).then(r => r.json());
    const congestion = data.signals.traffic.currentCongestion;
    trafficHighlight?.remove();
    trafficHighlight = L.circleMarker(latlng, {
      radius: 13,
      color: congestion >= 65 ? '#ff5c67' : congestion >= 40 ? '#ffbb34' : '#26d78c',
      fillColor: congestion >= 65 ? '#ff5c67' : congestion >= 40 ? '#ffbb34' : '#26d78c',
      fillOpacity: 0.3,
      weight: 3,
    }).bindPopup(`<b>Traffic highlight</b><br>${location}<br>Now: ${congestion}% congestion<br>Peak next 3h: ${data.signals.traffic.peakCongestion}%<br><small>Source: trained traffic ensemble + live weather</small>`).addTo(layerGroups.traffic);
    trafficHighlight.openPopup();
    $('#map-title').textContent = `Traffic highlight · ${location}`;
    $('#map-detail').textContent = `${congestion}% now · ${data.signals.traffic.peakCongestion}% peak forecast`;
    toast(`Traffic loaded for ${location}`);
  } catch (err) {
    toast('Traffic highlight unavailable right now.');
  }
}

// ---------- data loading ----------
async function loadOverview() {
  const data = await fetch('/api/overview').then(r => r.json());
  overview = data;
  renderAssetMarkers(data.assets, data.aiSuggestions);
  setRisk($('.tabs button.selected')?.dataset.risk || 'flood');
  alerts = data.alerts.map(a => [a.title, `${a.location} \u00b7 ${a.age}`, a.severity, a.age]);
  renderAlerts();
  const hb = data.healthBreakdown || {};
  $('#health-score').textContent = data.health;
  $('#environment-score').textContent = hb.environment ?? '\u2014';
  $('#mobility-score').textContent = hb.mobility ?? '\u2014';
  $('#safety-score').textContent = hb.safety ?? '\u2014';
  $('#infrastructure-score').textContent = hb.infrastructure ?? '\u2014';
  $('#overall-score').textContent = hb.overall ?? data.health;
  $('#power').textContent = `${data.metrics.powerGW} GW`;
  $('#power-detail').textContent = '72% of capacity';
  $('#transport').textContent = data.metrics.transport;
}

async function loadModelInfo() {
  modelInfo = await fetch('/api/model/info').then(r => r.json());
}

async function loadTrafficForecast(location = $('#forecast-location').value) {
  const params = new URLSearchParams({
    location,
    rain_mm: latestWeather.rainMm,
    temp_c: latestWeather.tempC,
    clouds: latestWeather.clouds,
    weather: latestWeather.weatherMain,
    aqi: latestWeather.aqi,
  });
  try {
    const data = await fetch(`/api/predictions/traffic?${params}`).then(r => r.json());
    drawChart(data.points);
    applyLiveSignals(data.signals);
    if (modelInfo) {
      $('#forecast-caption').textContent = `${modelInfo.model_version || 'Traffic ensemble'} \u00b7 trained on ${modelInfo.rows_after_cleaning.toLocaleString()} real hourly records (R\u00b2 ${modelInfo.test_r2}) \u00b7 live weather-conditioned forecast for ${location}.`;
    }
    const peak = Math.max(...data.points.map(p => p.congestion));
    $('#traffic').textContent = peak >= 65 ? 'Heavy' : peak >= 40 ? 'Moderate' : 'Light';
  } catch (err) {
    $('#forecast-caption').textContent = 'Forecast temporarily unavailable.';
  }
}

function applyLiveSignals(signals) {
  if (!signals) return;
  latestSignals = signals;
  const { power, transport } = signals;
  $('#power').textContent = `${power.demandGW} GW`;
  $('#power-detail').textContent = 'model-est. demand';
  $('#transport').textContent = transport.status;
  $('#transport-detail').textContent = `${transport.onTimePercent}% model-est. on time`;
  $('#traffic-detail').textContent = `ML now ${signals.traffic.currentCongestion}% · peak ${signals.traffic.peakCongestion}%`;
  $('#flood-risk-card').textContent = signals.flood.risk;
  $('#flood-detail').textContent = `${signals.flood.score}/100 rainfall screening`;
  pushSpark('power', power.demandGW);
  pushSpark('transport', transport.onTimePercent);
  setRisk($('.tabs button.selected')?.dataset.risk || 'flood');
}

// ---------- traffic forecast chart ----------
function drawChart(points) {
  const c = $('#chart'), ctx = c.getContext('2d'), w = c.width, h = c.height;
  const values = points.map(p => p.congestion);
  const pad = { left: 31, right: 10, top: 12, bottom: 25 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const xAt = i => pad.left + i * plotW / (values.length - 1);
  const yAt = value => pad.top + (100 - value) * plotH / 100;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#071523';
  ctx.fillRect(pad.left, pad.top, plotW, plotH);
  [[0, 35, '#19c8ef0c'], [35, 65, '#ffbb3410'], [65, 100, '#ff5c6710']].forEach(([low, high, color]) => {
    ctx.fillStyle = color;
    ctx.fillRect(pad.left, yAt(high), plotW, yAt(low) - yAt(high));
  });
  ctx.font = '9px Inter, Arial, sans-serif';
  ctx.textAlign = 'right';
  [0, 25, 50, 75, 100].forEach(value => {
    const y = yAt(value);
    ctx.strokeStyle = '#2a4b6077';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
    ctx.fillStyle = '#8199ad';
    ctx.fillText(`${value}`, pad.left - 6, y + 3);
  });
  ctx.textAlign = 'center';
  [0, 4, 8, 12].forEach(index => {
    if (index >= values.length) return;
    ctx.fillStyle = '#8199ad';
    ctx.fillText(index === 0 ? 'now' : `+${points[index].minute}m`, xAt(index), h - 8);
  });
  const gradient = ctx.createLinearGradient(0, pad.top, 0, yAt(0));
  gradient.addColorStop(0, 'rgba(255,83,95,.55)');
  gradient.addColorStop(1, 'rgba(255,83,95,0)');
  const xy = i => [xAt(i), yAt(values[i])];
  ctx.beginPath();
  values.forEach((value, index) => { const [x, y] = xy(index); index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.lineTo(xAt(values.length - 1), yAt(0)); ctx.lineTo(xAt(0), yAt(0)); ctx.closePath();
  ctx.fillStyle = gradient; ctx.fill();
  ctx.save();
  ctx.shadowColor = '#ff5b67aa'; ctx.shadowBlur = 8;
  ctx.beginPath();
  values.forEach((value, index) => { const [x, y] = xy(index); index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = '#ff5b67'; ctx.lineWidth = 2.4; ctx.stroke();
  ctx.restore();
  values.forEach((value, index) => {
    const [x, y] = xy(index);
    ctx.fillStyle = value >= 65 ? '#ff5c67' : value >= 35 ? '#ffbb34' : '#26d78c';
    ctx.beginPath(); ctx.arc(x, y, index === 0 ? 3.5 : 2, 0, Math.PI * 2); ctx.fill();
  });
  const peak = Math.max(...values), peakIndex = values.indexOf(peak), [peakX, peakY] = xy(peakIndex);
  ctx.setLineDash([4, 4]); ctx.strokeStyle = '#c9e8ff';
  ctx.beginPath(); ctx.moveTo(peakX, pad.top); ctx.lineTo(peakX, yAt(0)); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = '#e7f1fa'; ctx.textAlign = 'left';
  ctx.fillText(`peak ${peak}%`, Math.min(peakX + 6, w - 55), Math.max(peakY - 7, pad.top + 9));
}

// ---------- sparklines ----------
function pushSpark(key, value) {
  const buf = sparkBuffers[key];
  buf.push(value);
  if (buf.length > MAX_SPARK) buf.shift();
  drawSpark(key);
}

function drawSpark(key) {
  const canvas = $(`#spark-${key}`);
  if (!canvas) return;
  const buf = sparkBuffers[key];
  if (buf.length < 2) return;
  const ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  const min = Math.min(...buf), max = Math.max(...buf), span = (max - min) || 1;
  ctx.clearRect(0, 0, w, h);
  ctx.beginPath();
  buf.forEach((v, i) => {
    const x = i * w / (MAX_SPARK - 1);
    const y = h - 4 - ((v - min) / span) * (h - 8);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = '#4fd8ff';
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

// ---------- risk heatmap card (tabs) ----------
function setRisk(type) {
  const titles = { flood: 'Flood exposure', traffic: 'Traffic congestion', air: 'Air pollution concentration', incident: 'Incident density' };
  $('#heat-caption').textContent = titles[type];
  const heatmap = $('#heatmap');
  heatmap.innerHTML = '';
  const assets = overview?.assets || [];
  const values = assets.map(asset => {
    if (type === 'flood') return asset.type === 'flood' ? 90 : asset.status === 'high' ? 55 : 25;
    if (type === 'traffic') return asset.status === 'high' ? 88 : asset.status === 'moderate' ? 58 : 22;
    if (type === 'incident') return asset.type === 'incident' ? 95 : 16;
    return Math.min(100, (latestWeather.aqi || 70) * (asset.type === 'incident' ? 1.15 : 0.9));
  });
  const nodes = assets.length ? assets : [{ name: 'Waiting for live assets' }];
  nodes.forEach((asset, index) => {
    const node = document.createElement('i');
    const value = values[index] ?? 20;
    node.className = `heat-node ${value >= 65 ? 'high' : value >= 35 ? 'moderate' : 'low'}`;
    node.style.left = `${14 + (index * 67) % 76}%`;
    node.style.top = `${18 + (index * 43) % 64}%`;
    node.style.setProperty('--heat-value', `${Math.max(0.35, value / 100)}`);
    node.title = `${asset.name}: ${Math.round(value)}/100`;
    heatmap.appendChild(node);
  });
  $$('.tabs button').forEach(b => b.classList.toggle('selected', b.dataset.risk === type));
}

function renderAlerts(limit = 3) {
  $('#alerts').innerHTML = alerts.slice(0, limit).map(([title, place, severity, age]) =>
    `<div class="alert"><strong>${severity === 'high' ? '\u26a0' : '\u25cf'} ${title}</strong><small>${place}</small><span class="badge ${severity}">${severity}</span></div>`
  ).join('');
}
function renderInsights(limit = 3) {
  $('#insights').innerHTML = insights.slice(0, limit).map(([icon, title, text]) =>
    `<p class="insight">${icon} <b>${title}</b><br>${text}</p>`
  ).join('');
}

function changeView(view) {
  const [title, detail, note] = views[view];
  $('#map-title').textContent = title;
  $('#map-detail').textContent = detail;
  $('#mode-note').textContent = note;
  $('main').dataset.view = view;
  $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  if (view === 'assistant') {
    $('#assistant-modal').classList.add('open');
    $('#assistant-question').focus();
    return;
  }
  const reportsOpen = view === 'reports';
  $('#map').style.visibility = reportsOpen ? 'hidden' : 'visible';
  if (reportsOpen) renderReports();
  let panel = $('.reports-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'reports-panel';
    $('.map-card').appendChild(panel);
  }
  panel.classList.toggle('open', reportsOpen);
  if (view === 'traffic') loadTrafficForecast();
  if (view === 'air') setRisk('air');
  if (view === 'flood') setRisk('flood');
  if (view === 'incidents') setRisk('incident');
}

function renderReports() {
  const panel = $('.reports-panel') || (() => { const p = document.createElement('div'); p.className = 'reports-panel'; $('.map-card').appendChild(p); return p; })();
  const m = overview ? overview.metrics : {};
  panel.innerHTML = `<h2>Daily Operations Report \u2014 ${currentCity}</h2>
    <p>Generated ${new Date().toLocaleString()}</p>
    <table>
      <tr><th>Indicator</th><th>Value</th></tr>
      <tr><td>Traffic status</td><td>${$('#traffic').textContent}</td></tr>
      <tr><td>Air quality (AQI)</td><td>${$('#aqi').textContent} \u2014 ${$('#aqi-label').textContent}</td></tr>
      <tr><td>Flood risk</td><td>${$('#flood-risk-card').textContent}</td></tr>
      <tr><td>Power demand</td><td>${$('#power').textContent}</td></tr>
      <tr><td>Public transport</td><td>${$('#transport').textContent}</td></tr>
      <tr><td>City Health Index</td><td>${$('#health-score').textContent}</td></tr>
    </table>`;
}

// ---------- what-if simulation ----------
async function runSimulation() {
  const rainfall_mm = Number($('#rain').value);
  const traffic_increase = Number($('#traffic-increase').value);
  const blocked_road = $('#road').value;
  try {
    const res = await fetch('/api/simulations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rainfall_mm,
        traffic_increase,
        blocked_road,
        location: $('#forecast-location').value,
        current_rain_mm: latestWeather.rainMm,
        temp_c: latestWeather.tempC,
        aqi: latestWeather.aqi,
      }),
    }).then(r => r.json());
    const { floodRisk, trafficRisk, emergencyEtaDeltaMinutes, affectedPeople, floodScore, trafficScore } = res.result;
    $('#flood-risk').textContent = floodRisk;
    $('#traffic-risk').textContent = trafficRisk;
    $('#eta').textContent = `+${emergencyEtaDeltaMinutes} min`;
    $('#affected').textContent = `~${affectedPeople.toLocaleString()}`;
    $('#simulation-method').textContent = `${res.result.model} · baseline ${res.result.baselineCongestion}% · road penalty ${res.result.blockedRoadPenalty}%`;
    setRisk(floodScore > trafficScore ? 'flood' : 'traffic');
    toast(`Scenario complete: emergency ETA increases by ${emergencyEtaDeltaMinutes} minutes`);
  } catch (err) {
    toast('Simulation request failed \u2014 is the backend running?');
  }
}

async function profileAction(action) {
  $('#profile-menu').classList.remove('open');
  if (action === 'profile') toast(`Signed in as ${currentUser?.displayName || currentUser?.username || 'Operator'}`);
  if (action === 'new') {
    activeProfile = (activeProfile + 1) % profiles.length;
    $('#profile').innerHTML = `<span class="avatar">${profiles[activeProfile].slice(0, 2).toUpperCase()}</span> ${profiles[activeProfile]}\u2304`;
    toast(`New account selected: ${profiles[activeProfile]}`);
  }
  if (action === 'signout') {
    await fetch('/api/auth/logout', { method: 'POST' });
    currentUser = null;
    window.location.reload();
  }
}

function showUser(user) {
  currentUser = user;
  const name = user.displayName || user.username;
  $('#profile').innerHTML = `<span class="avatar">${name.slice(0, 2).toUpperCase()}</span> ${name}⌄`;
  $('#auth-screen').classList.remove('open');
}

function startDashboard(user) {
  showUser(user);
  initAmbientScene();
  initMap();
  renderAlerts();
  renderInsights();
  setRisk('flood');
  loadOverview().then(loadModelInfo).then(() => loadTrafficForecast());
  loadCity('New Delhi');
  connectTelemetry();
}

async function checkAuth() {
  try {
    const response = await fetch('/api/auth/me');
    if (response.ok) {
      const data = await response.json();
      startDashboard(data.user);
    }
  } catch (err) {
    $('#login-error').textContent = 'Cannot reach the authentication service.';
  }
}

function weatherIcon(code) { if ([0].includes(code)) return '\u2600\ufe0f'; if ([1, 2].includes(code)) return '\ud83c\udf24\ufe0f'; if ([3, 45, 48].includes(code)) return '\u2601\ufe0f'; if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return '\ud83c\udf27\ufe0f'; if ([71, 73, 75, 77, 85, 86].includes(code)) return '\u2744\ufe0f'; return '\u26c8\ufe0f'; }
function weatherText(code) { if (code === 0) return 'Clear sky'; if ([1, 2].includes(code)) return 'Partly cloudy'; if (code === 3) return 'Overcast'; if ([45, 48].includes(code)) return 'Fog'; if (code >= 51 && code <= 67) return 'Rain'; if (code >= 71 && code <= 86) return 'Snow showers'; return 'Thunderstorm'; }
function weatherMainFor(code) { if (code >= 51 && code <= 67 || (code >= 80 && code <= 82)) return 'Rain'; if (code >= 71 && code <= 86) return 'Snow'; if (code === 0) return 'Clear'; if ([45, 48].includes(code)) return 'Fog'; if (code >= 95) return 'Thunderstorm'; return 'Clouds'; }
function aqiText(aqi) { if (aqi <= 50) return 'Good'; if (aqi <= 100) return 'Moderate'; if (aqi <= 150) return 'Unhealthy for sensitive groups'; if (aqi <= 200) return 'Unhealthy'; if (aqi <= 300) return 'Very unhealthy'; return 'Hazardous'; }
function floodText(rain) { if (rain >= 20) return ['High', 'Heavy current rain']; if (rain >= 5) return ['Moderate', 'Rain-derived screening risk']; return ['Low', 'No significant current rain']; }

async function loadCity(query) {
  const error = $('#city-error');
  error.textContent = '';
  $('#updated').textContent = 'Finding city\u2026';
  try {
    const geo = await fetch(`https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(query)}&count=1&language=en&format=json`).then(r => r.ok ? r.json() : Promise.reject(new Error('City search failed')));
    const place = geo.results?.[0];
    if (!place) throw new Error('City not found. Try a city and country name.');
    const [weather, air] = await Promise.all([
      fetch(`https://api.open-meteo.com/v1/forecast?latitude=${place.latitude}&longitude=${place.longitude}&current=temperature_2m,relative_humidity_2m,precipitation,rain,weather_code,wind_speed_10m&temperature_unit=celsius&wind_speed_unit=kmh&timezone=auto`).then(r => r.ok ? r.json() : Promise.reject(new Error('Weather data unavailable'))),
      fetch(`https://air-quality-api.open-meteo.com/v1/air-quality?latitude=${place.latitude}&longitude=${place.longitude}&current=us_aqi,pm2_5,pm10&timezone=auto`).then(r => r.ok ? r.json() : Promise.reject(new Error('Air-quality data unavailable'))),
    ]);
    updateLiveData(place, weather.current, air.current);
    $('#city-modal').classList.remove('open');
    toast(`Live public data loaded for ${place.name}`);
  } catch (err) {
    $('#updated').textContent = 'Live public data unavailable';
    error.textContent = err.message;
    toast(err.message);
  }
}

function updateLiveData(place, weather, air) {
  const aqi = Math.round(air.us_aqi ?? 0);
  const [flood, floodDetail] = floodText(weather.rain ?? weather.precipitation ?? 0);
  const environment = Math.max(0, Math.round(100 - aqi * 0.28));
  currentCity = place.name;
  $('#city-name').textContent = place.name;
  $('#weather-icon').textContent = weatherIcon(weather.weather_code);
  $('#weather-temp').textContent = `${Math.round(weather.temperature_2m)}\u00b0C`;
  $('#weather-condition').textContent = weatherText(weather.weather_code);
  $('#humidity').textContent = `${weather.relative_humidity_2m}%`;
  $('#wind').textContent = `${Math.round(weather.wind_speed_10m)} km/h`;
  $('#rain-now').textContent = `${weather.rain ?? weather.precipitation ?? 0} mm`;
  $('#aqi').textContent = aqi || '\u2014';
  $('#aqi-label').textContent = aqi ? aqiText(aqi) : 'Unavailable';
  $('#flood-risk-card').textContent = flood;
  $('#flood-detail').textContent = floodDetail;
  $('#environment-score').textContent = environment;
  $('#updated').textContent = `Live public data: ${weather.time.replace('T', ' ')}`;

  latestWeather = { rainMm: weather.rain ?? weather.precipitation ?? 0, tempC: weather.temperature_2m, clouds: 40, weatherMain: weatherMainFor(weather.weather_code), aqi };
  pushSpark('aqi', aqi);
  pushSpark('flood', Math.min(100, latestWeather.rainMm * 4 + 8));

  if (place.name.toLowerCase() === 'new delhi') {
    renderAssetMarkers(overview?.assets, overview?.aiSuggestions);
    map.setView([28.6139, 77.2090], 12);
  } else {
    setCityMarker(place);
  }
  loadTrafficForecast();

  alerts = [
    ['Air quality reading', `${place.name} \u00b7 US AQI ${aqi}`, aqi > 150 ? 'high' : aqi > 100 ? 'medium' : 'low', 'live'],
    ['Current rainfall', `${place.name} \u00b7 ${weather.rain ?? weather.precipitation ?? 0} mm`, flood === 'High' ? 'high' : flood === 'Moderate' ? 'medium' : 'low', 'live'],
  ];
  insights = [
    ['\ud83c\udf26\ufe0f', 'Live weather conditions', `${weatherText(weather.weather_code)} \u00b7 ${Math.round(weather.temperature_2m)}\u00b0C \u00b7 ${Math.round(weather.wind_speed_10m)} km/h wind.`],
    ['\ud83d\udca8', 'Live air quality', `US AQI ${aqi} (${aqiText(aqi)}), PM2.5 ${air.pm2_5 ?? '\u2014'} \u00b5g/m\u00b3.`],
    ['\u2248', 'Rain-derived flood screening', `${flood} current risk based on observed rain. A calibrated local flood model is still required.`],
  ];
  renderAlerts();
  renderInsights();
}

// ---------- telemetry (simulated live stream from the backend) ----------
function connectTelemetry() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    $('#traffic-detail').textContent = `Avg. speed ${msg.trafficSpeedKmh} km/h`;
    pushSpark('traffic', msg.trafficSpeedKmh);
  };
  ws.onclose = () => setTimeout(connectTelemetry, 4000);
  ws.onerror = () => ws.close();
}

// ---------- wiring ----------
$('#rain').addEventListener('input', e => $('#rain-out').textContent = `${e.target.value} mm`);
$('#traffic-increase').addEventListener('input', e => $('#traffic-out').textContent = `+${e.target.value}%`);
$('#simulate').addEventListener('click', runSimulation);
$('#forecast-location').addEventListener('change', () => loadTrafficForecast());
$$('nav button').forEach(b => b.addEventListener('click', () => changeView(b.dataset.view)));
$$('.tabs button').forEach(b => b.addEventListener('click', () => setRisk(b.dataset.risk)));
$$('.layers input').forEach(i => i.addEventListener('change', () => {
  const key = i.dataset.layer;
  const label = i.parentElement.textContent.trim();
  if (key === 'flood') {
    if (i.checked) floodHeat?.addTo(map); else if (floodHeat) map.removeLayer(floodHeat);
  } else if (key === 'traffic' || key === 'incidents') {
    const group = layerGroups[key];
    if (group) { if (i.checked) group.addTo(map); else map.removeLayer(group); }
  } else {
    toast(`${label}: provider connection pending \u2014 layer is illustrative for now`);
  }
}));
$('#city-change').addEventListener('click', () => { $('#city-modal').classList.add('open'); $('#city-input').focus(); });
$('#city-close').addEventListener('click', () => $('#city-modal').classList.remove('open'));
$('#city-modal').addEventListener('click', e => { if (e.target === $('#city-modal')) $('#city-modal').classList.remove('open'); });
$('#city-form').addEventListener('submit', e => { e.preventDefault(); loadCity($('#city-input').value.trim()); });
$('#assistant-close').addEventListener('click', () => $('#assistant-modal').classList.remove('open'));
$('#assistant-modal').addEventListener('click', e => { if (e.target === $('#assistant-modal')) $('#assistant-modal').classList.remove('open'); });
$('#assistant-form').addEventListener('submit', async e => {
  e.preventDefault();
  const answer = $('#assistant-answer');
  answer.textContent = 'Analyzing live city data…';
  try {
    const res = await fetch('/api/assistant', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: $('#assistant-question').value, location: currentCity, temp_c: latestWeather.tempC, rain_mm: latestWeather.rainMm, aqi: latestWeather.aqi, weather: latestWeather.weatherMain }),
    }).then(r => r.ok ? r.json() : Promise.reject(new Error('Assistant unavailable')));
    answer.textContent = res.answer;
    $('#assistant-source').textContent = `Grounded ${res.assistant} · live weather + trained traffic ensemble`;
  } catch (err) {
    answer.textContent = 'The grounded assistant could not reach the API.';
  }
});
$$('[data-city]').forEach(b => b.addEventListener('click', () => { $('#city-input').value = b.dataset.city; loadCity(b.dataset.city); }));
$('#search').addEventListener('click', () => { $('#city-modal').classList.add('open'); $('#city-input').focus(); });
$('#profile').addEventListener('click', e => { e.stopPropagation(); $('#profile-menu').classList.toggle('open'); });
$$('[data-account]').forEach(b => b.addEventListener('click', () => profileAction(b.dataset.account)));
$('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const error = $('#login-error');
  error.textContent = 'Signing in…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('#login-username').value.trim(), password: $('#login-password').value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Login failed');
    error.textContent = '';
    startDashboard(data.user);
  } catch (err) {
    error.textContent = err.message;
  }
});
$$('.view-all').forEach(b => b.addEventListener('click', () => {
  const all = b.dataset.list, expanded = b.dataset.expanded === 'yes';
  (all === 'alerts' ? renderAlerts : renderInsights)(expanded ? 3 : 99);
  b.dataset.expanded = expanded ? 'no' : 'yes';
  b.textContent = expanded ? 'View All' : 'Show Less';
}));
$('#bell-btn').addEventListener('click', () => { renderAlerts(99); toast('Showing all active alerts'); });
$('#settings-btn').addEventListener('click', () => toast('Settings panel is on the roadmap.'));
$('#zoom-in').addEventListener('click', () => map?.zoomIn());
$('#zoom-out').addEventListener('click', () => map?.zoomOut());
document.addEventListener('click', () => $('#profile-menu').classList.remove('open'));
document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#search').click(); } });

function tickClock() {
  $('#clock').textContent = new Date().toLocaleString('en-US', { month: 'short', day: '2-digit', year: 'numeric', hour: 'numeric', minute: '2-digit' });
}
setInterval(tickClock, 1000);
tickClock();

// ---------- boot ----------
checkAuth();
