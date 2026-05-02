'use strict';

// ─────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────
const DATASET_COLORS = [
  '#58a6ff', '#3fb950', '#f0883e', '#ff7b72',
  '#bc8cff', '#39d353', '#ffa657', '#79c0ff',
];
const HZ_WINDOW_SEC       = 3;
const MAX_RECONNECT_DELAY = 30_000;
const ROS_POLL_INTERVAL   = 6_000;

// ─────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────
function getField(obj, path) {
  if (!path) return obj;
  return path.split('.').reduce((cur, k) => (cur != null ? cur[k] : undefined), obj);
}

function toNumber(val) {
  if (val == null) return null;
  if (Array.isArray(val)) {
    const nums = val.filter(v => typeof v === 'number');
    return nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : null;
  }
  const n = Number(val);
  return isNaN(n) ? null : n;
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('pt-BR', { hour12: false }) +
         '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ─────────────────────────────────────────────────────────
// ToastManager
// ─────────────────────────────────────────────────────────
class ToastManager {
  constructor() {
    this._el = document.getElementById('toast-container');
    this._lastMsg = '';
  }

  show(message, type = 'info', duration = 4500) {
    if (message === this._lastMsg) return;
    this._lastMsg = message;
    setTimeout(() => { if (this._lastMsg === message) this._lastMsg = ''; }, 2000);

    const icons = { info: 'ℹ', ok: '✓', warn: '⚠', error: '✕' };
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML =
      `<span class="toast-icon">${icons[type] || 'ℹ'}</span>` +
      `<span class="toast-msg">${escHtml(message)}</span>` +
      `<button class="toast-close" aria-label="Fechar">✕</button>`;

    const close = () => {
      toast.classList.remove('toast-visible');
      setTimeout(() => toast.remove(), 260);
    };
    toast.querySelector('.toast-close').addEventListener('click', close);
    this._el.appendChild(toast);
    requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('toast-visible')));
    setTimeout(close, duration);
  }
}

// ─────────────────────────────────────────────────────────
// ROSStatus — polls /api/v1/health/ros
// ─────────────────────────────────────────────────────────
class ROSStatus {
  constructor(toast) {
    this._toast  = toast;
    this._state  = 'unknown';
    this._timer  = null;
    this._dot    = document.getElementById('ros-dot');
    this._label  = document.getElementById('ros-label');
  }

  start() {
    this.stop();                                            // evita intervalo duplo
    this._check();
    this._timer = setInterval(() => this._check(), ROS_POLL_INTERVAL);
  }

  stop() { clearInterval(this._timer); this._timer = null; }

  async _check() {
    try {
      const ctrl = new AbortController();
      const tid  = setTimeout(() => ctrl.abort(), 4000);
      const res  = await fetch('/api/v1/health/ros', { signal: ctrl.signal });
      clearTimeout(tid);

      const body = await res.json().catch(() => ({}));

      if (res.ok) {
        if (body.ros_initialized === false) {
          this.stop();
          App.showTab('config');
          return;
        }

        if (body.status === 'ok' || body.ros_ok === true) {
          this._set('connected', 'ROS Conectado');
        } else {
          // roscore caiu após a inicialização — redireciona para config uma
          // única vez (na transição), mas mantém o polling ativo para detectar
          // quando o servidor Python for reiniciado.
          const wasOk = this._state === 'connected';
          this._set('degraded', 'ROS Offline');
          if (wasOk) {
            this._toast.show(
              'O roscore ficou offline. Reinicie o servidor Python e clique em Conectar.',
              'error'
            );
            App.showTab('config');
          }
        }
      } else {
        this._set('degraded', body.detail || 'ROS Degradado');
      }
    } catch (_) {
      if (this._state !== 'error') {
        this._toast.show('roscore offline ou inacessível', 'error');
      }
      this._set('error', 'ROS Offline');
    }
  }

  _set(state, label) {
    const prev   = this._state;
    this._state  = state;
    this._dot.className = `ros-dot ros-${state}`;
    this._label.textContent = label;
    if (prev === 'error' && state === 'connected') {
      this._toast.show('ROS reconectado', 'ok');
    }
  }
}

// ─────────────────────────────────────────────────────────
// ChartWidget — manages one chart with N SSE dataset streams
// ─────────────────────────────────────────────────────────
class ChartWidget {
  /**
   * @param {object} cfg
   * @param {string} cfg.id
   * @param {string} cfg.title
   * @param {Array<{topic,field,label,color}>} cfg.datasets
   * @param {number} cfg.interval
   * @param {number} cfg.maxPoints
   * @param {string} cfg.yLabel
   * @param {string} cfg.yScale      'linear'|'logarithmic'
   * @param {number|undefined} cfg.yMin
   * @param {number|undefined} cfg.yMax
   * @param {ToastManager} cfg.toast
   */
  constructor(cfg) {
    this.id        = cfg.id;
    this.title     = cfg.title;
    this.interval  = cfg.interval  || 0.1;
    this.maxPoints = cfg.maxPoints || 200;
    this.yLabel    = cfg.yLabel    || '';
    this.yScale    = cfg.yScale    || 'linear';
    this.yMin      = cfg.yMin;
    this.yMax      = cfg.yMax;
    this._toast    = cfg.toast;

    const cfgCopy = { ...cfg };
    delete cfgCopy.toast;
    this._cfg = cfgCopy;
    this._cfg.type = 'chart';

    this.paused      = false;
    this.totalEvents = 0;

    this._chart    = null;
    this._streams  = [];  // per-dataset SSE state
    this._hzTimer  = null;
    this._el       = null;

    this._buildDOM();
    this._initChart();
    cfg.datasets.forEach((ds, i) => this._addStream(ds, i));
    this._hzTimer = setInterval(() => this._tickHz(), 1000);
  }

  // ── DOM ──────────────────────────────────────────────
  _buildDOM() {
    const el = document.createElement('div');
    el.className = 'widget';
    el.id = `w-${this.id}`;
    el.draggable = true;
    el.dataset.wid = this.id;
    el.innerHTML = `
      <div class="widget-header">
        <div class="widget-info">
          <span class="widget-title">${escHtml(this.title)}</span>
          <span class="widget-stats" id="ws-${this.id}">0 eventos · — msg/s</span>
        </div>
        <div class="widget-actions">
          <button class="wbtn" id="wpause-${this.id}" title="Pausar/Retomar streaming">⏸</button>
          <button class="wbtn" title="Limpar dados do gráfico" onclick="App.clearWidget('${this.id}')">🗑</button>
          <button class="wbtn" title="Exportar PNG" onclick="App.exportWidget('${this.id}')">💾</button>
          <button class="wbtn danger" title="Remover gráfico" onclick="App.removeWidget('${this.id}')">✕</button>
        </div>
      </div>
      <div class="widget-body">
        <div class="widget-legend" id="wleg-${this.id}"></div>
        <div class="canvas-container">
          <div class="widget-empty-state" id="wemp-${this.id}">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
            </svg>
            <span>Aguardando dados…</span>
          </div>
          <div class="widget-paused-banner">⏸ Pausado</div>
          <canvas id="wc-${this.id}" style="display:none"></canvas>
        </div>
      </div>
    `;

    this._el = el;

    el.addEventListener('dragstart', (e) => {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', this.id);
      setTimeout(() => el.classList.add('dragging'), 0);
    });
    el.addEventListener('dragend', () => {
      el.classList.remove('dragging');
    });

    this._el.querySelector(`#wpause-${this.id}`)
      .addEventListener('click', () => this.togglePause());
  }

  // ── Chart.js ─────────────────────────────────────────
  _initChart() {
    const canvas = this._el.querySelector(`#wc-${this.id}`);
    this._chart  = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        animation:           false,
        interaction:         { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#161b22',
            borderColor:     '#30363d',
            borderWidth:     1,
            titleColor:      '#8b949e',
            bodyColor:       '#e6edf3',
            padding:         10,
            callbacks: {
              title:  items => items[0].label,
              label:  item  => ` ${item.dataset.label}: ${Number(item.raw).toPrecision(6)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 8, maxRotation: 0 },
            grid:  { color: '#1c2128' },
          },
          y: {
            type:  this.yScale,
            title: { display: !!this.yLabel, text: this.yLabel, color: '#8b949e', font: { size: 11 } },
            min:   this.yMin !== undefined ? this.yMin : undefined,
            max:   this.yMax !== undefined ? this.yMax : undefined,
            ticks: { color: '#8b949e', font: { size: 10 } },
            grid:  { color: '#1c2128' },
          },
        },
      },
    });
  }

  // ── Dataset / SSE stream ──────────────────────────────
  _addStream(ds, idx) {
    const stream = {
      topic:          ds.topic,
      field:          ds.field,
      label:          ds.label || ds.field,
      color:          ds.color || DATASET_COLORS[idx % DATASET_COLORS.length],
      evtSource:      null,
      hzBuffer:       [],
      connected:      false,
      reconnectDelay: 1000,
      reconnectTimer: null,
    };
    this._streams.push(stream);

    // Add Chart.js dataset
    this._chart.data.datasets.push({
      label:            stream.label,
      data:             [],
      borderColor:      stream.color,
      backgroundColor:  stream.color + '18',
      borderWidth:      1.8,
      pointRadius:      0,
      pointHoverRadius: 4,
      tension:          0.25,
      fill:             true,
      spanGaps:         true,
    });
    this._chart.update('none');
    this._renderLegend();
    this._openSSE(idx);
  }

  _openSSE(idx) {
    const stream = this._streams[idx];
    if (stream.evtSource) stream.evtSource.close();

    const topicName = stream.topic.startsWith('/') ? stream.topic.slice(1) : stream.topic;
    const url = `/api/v1/topic/${encodeURIComponent(topicName)}/stream?interval=${this.interval}`;

    const es = new EventSource(url);
    stream.evtSource = es;

    es.addEventListener('message', (e) => {
      if (this.paused) return;
      try {
        const { timestamp, data } = JSON.parse(e.data);
        const raw = getField(data, stream.field);
        const val = toNumber(raw);
        if (val === null) {
          return;
        }
        this._pushPoint(idx, fmtTime(timestamp), val);
        stream.hzBuffer.push(Date.now());
        this.totalEvents++;
        this._updateStats();
        // Show canvas
        this._el.querySelector(`#wemp-${this.id}`).style.display = 'none';
        this._el.querySelector(`#wc-${this.id}`).style.display   = 'block';
      } catch (_) {}
    });

    es.addEventListener('error', (rawE) => {
      try {
        const body = JSON.parse(rawE.data || '{}');
        if (body.error) {
          this._toast.show(`${stream.topic}: ${body.error}`, 'error');
        }
      } catch (_) {}
    });

    es.onopen = () => {
      stream.connected      = true;
      stream.reconnectDelay = 1000;
      clearTimeout(stream.reconnectTimer);
    };

    es.onerror = () => {
      if (!stream.connected) return;
      stream.connected = false;
      es.close();
      stream.evtSource = null;
      stream.reconnectTimer = setTimeout(
        () => { if (!stream.connected) this._openSSE(idx); },
        stream.reconnectDelay,
      );
      stream.reconnectDelay = Math.min(stream.reconnectDelay * 2, MAX_RECONNECT_DELAY);
    };
  }

  // ── Data push ─────────────────────────────────────────
  _pushPoint(datasetIdx, label, value) {
    const c   = this._chart;
    const len = this._streams.length;

    c.data.labels.push(label);
    for (let i = 0; i < len; i++) {
      c.data.datasets[i].data.push(i === datasetIdx ? value : null);
    }
    if (c.data.labels.length > this.maxPoints) {
      c.data.labels.shift();
      c.data.datasets.forEach(ds => ds.data.shift());
    }
    c.update('none');
  }

  // ── Legend ────────────────────────────────────────────
  _renderLegend() {
    const el = this._el.querySelector(`#wleg-${this.id}`);
    el.innerHTML = this._streams.map(s =>
      `<span class="legend-item">
        <span class="legend-swatch" style="background:${escHtml(s.color)}"></span>
        <span>${escHtml(s.label)}</span>
        <span class="legend-field">${escHtml(s.topic)} · ${escHtml(s.field)}</span>
      </span>`
    ).join('');
  }

  // ── Hz / Stats ────────────────────────────────────────
  _tickHz() {
    const now    = Date.now();
    const cutoff = now - HZ_WINDOW_SEC * 1000;
    let hz = 0;
    this._streams.forEach(s => {
      while (s.hzBuffer.length && s.hzBuffer[0] < cutoff) s.hzBuffer.shift();
      hz += s.hzBuffer.length / HZ_WINDOW_SEC;
    });
    this._renderStats(hz);
  }

  _updateStats() {
    const el = this._el.querySelector(`#ws-${this.id}`);
    if (el) el.textContent = `${this.totalEvents} eventos · … msg/s`;
  }

  _renderStats(hz) {
    const el = this._el.querySelector(`#ws-${this.id}`);
    if (el) el.textContent = `${this.totalEvents} eventos · ${hz > 0 ? hz.toFixed(1) : '—'} msg/s`;
  }

  // ── Public controls ───────────────────────────────────
  togglePause() {
    this.paused = !this.paused;
    this._el.classList.toggle('is-paused', this.paused);
    const btn = this._el.querySelector(`#wpause-${this.id}`);
    if (btn) {
      btn.textContent = this.paused ? '▶' : '⏸';
      btn.classList.toggle('active', this.paused);
      btn.title = this.paused ? 'Retomar streaming' : 'Pausar streaming';
    }
  }

  clear() {
    this._chart.data.labels = [];
    this._chart.data.datasets.forEach(ds => { ds.data = []; });
    this._chart.update('none');
    this.totalEvents = 0;
    this._streams.forEach(s => { s.hzBuffer = []; });
    this._renderStats(0);
    this._el.querySelector(`#wemp-${this.id}`).style.display = 'flex';
    this._el.querySelector(`#wc-${this.id}`).style.display   = 'none';
  }

  exportPNG() {
    const a    = document.createElement('a');
    a.href     = this._chart.toBase64Image('image/png', 1);
    const ts   = new Date().toISOString().replace(/[:.]/g, '-');
    a.download = `ros-${this.title.replace(/\s+/g, '-')}-${ts}.png`;
    a.click();
  }

  destroy() {
    clearInterval(this._hzTimer);
    this._streams.forEach(s => {
      clearTimeout(s.reconnectTimer);
      if (s.evtSource) s.evtSource.close();
    });
    this._chart.destroy();
    this._el.remove();
  }
}


// ─────────────────────────────────────────────────────────
// GpsWidget — manages a scatter plot for GPS trajectories
// ─────────────────────────────────────────────────────────
class GpsWidget {
  constructor(cfg) {
    this.id        = cfg.id;
    this.title     = cfg.title;
    this.topic     = cfg.topic;
    this.color     = cfg.color;
    this._toast    = cfg.toast;

    const cfgCopy = { ...cfg };
    delete cfgCopy.toast;
    this._cfg = cfgCopy;
    this._cfg.type = 'gps';

    this.paused      = false;
    this.totalEvents = 0;

    this._chart    = null;
    this._evtSource = null;
    this._hzBuffer = [];
    this._hzTimer  = null;
    this._el       = null;
    
    this.connected = false;
    this.reconnectDelay = 1000;
    this.reconnectTimer = null;
    
    // ENU coordinates state
    this._origin   = null;
    this._R        = 6378137; // Earth radius in meters

    this._buildDOM();
    this._initChart();
    this._openSSE();
    this._hzTimer = setInterval(() => this._tickHz(), 1000);
  }

  // ── DOM ──────────────────────────────────────────────
  _buildDOM() {
    const el = document.createElement('div');
    el.className = 'widget';
    el.id = `w-${this.id}`;
    el.draggable = true;
    el.dataset.wid = this.id;
    el.innerHTML = `
      <div class="widget-header">
        <div class="widget-info">
          <span class="widget-title">${escHtml(this.title)}</span>
          <span class="widget-stats" id="ws-${this.id}">0 eventos · — msg/s</span>
        </div>
        <div class="widget-actions">
          <button class="wbtn" id="wpause-${this.id}" title="Pausar/Retomar streaming">⏸</button>
          <button class="wbtn" title="Limpar dados do gráfico" onclick="App.clearWidget('${this.id}')">🗑</button>
          <button class="wbtn" title="Exportar PNG" onclick="App.exportWidget('${this.id}')">💾</button>
          <button class="wbtn danger" title="Remover gráfico" onclick="App.removeWidget('${this.id}')">✕</button>
        </div>
      </div>
      <div class="widget-body">
        <div class="widget-legend" id="wleg-${this.id}">
          <span class="legend-item">
            <span class="legend-swatch" style="background:${escHtml(this.color)}"></span>
            <span>Trajeto</span>
            <span class="legend-field">${escHtml(this.topic)} (ENU)</span>
          </span>
        </div>
        <div class="canvas-container">
          <div class="widget-empty-state" id="wemp-${this.id}">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
            </svg>
            <span>Aguardando sinal GPS…</span>
          </div>
          <div class="widget-paused-banner">⏸ Pausado</div>
          <canvas id="wc-${this.id}" style="display:none"></canvas>
        </div>
      </div>
    `;
    this._el = el;

    el.addEventListener('dragstart', (e) => {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', this.id);
      setTimeout(() => el.classList.add('dragging'), 0);
    });
    el.addEventListener('dragend', () => {
      el.classList.remove('dragging');
    });

    this._el.querySelector(`#wpause-${this.id}`).addEventListener('click', () => this.togglePause());
  }

  // ── Chart.js ─────────────────────────────────────────
  _initChart() {
    const canvas = this._el.querySelector(`#wc-${this.id}`);
    this._chart  = new Chart(canvas.getContext('2d'), {
      type: 'scatter',
      data: {
        datasets: [{
          label:            'Trajeto',
          data:             [],
          borderColor:      this.color,
          backgroundColor:  this.color + '18',
          borderWidth:      2,
          pointRadius:      2,
          pointHoverRadius: 5,
          tension:          0.2,
          showLine:         true,
          fill:             false,
        }]
      },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        animation:           false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#161b22', borderColor: '#30363d', borderWidth: 1,
            titleColor: '#8b949e', bodyColor: '#e6edf3', padding: 10,
            callbacks: {
              label: (ctx) => `Leste (X): ${Number(ctx.raw.x).toFixed(2)}m, Norte (Y): ${Number(ctx.raw.y).toFixed(2)}m`,
            },
          },
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Leste (m)', color: '#8b949e', font: { size: 11 } },
            ticks: { color: '#8b949e', font: { size: 10 } },
            grid:  { color: '#1c2128' },
          },
          y: {
            type: 'linear',
            title: { display: true, text: 'Norte (m)', color: '#8b949e', font: { size: 11 } },
            ticks: { color: '#8b949e', font: { size: 10 } },
            grid:  { color: '#1c2128' },
          },
        },
      },
    });
  }

  // ── SSE / ENU ─────────────────────────────────────────
  _openSSE() {
    if (this._evtSource) this._evtSource.close();
    
    const topicName = this.topic.startsWith('/') ? this.topic.slice(1) : this.topic;
    const url = `/api/v1/topic/${encodeURIComponent(topicName)}/stream?interval=0.1`;
    
    const es = new EventSource(url);
    this._evtSource = es;
    
    es.addEventListener('message', (e) => {
      if (this.paused) return;
      try {
        const { data } = JSON.parse(e.data);
        const lat = toNumber(data.latitude);
        const lon = toNumber(data.longitude);
        
        if (lat === null || lon === null) return;
        
        if (!this._origin) {
          this._origin = { lat: lat * Math.PI/180, lon: lon * Math.PI/180 };
        }
        
        const latRad = lat * Math.PI/180;
        const lonRad = lon * Math.PI/180;
        
        const x = this._R * (lonRad - this._origin.lon) * Math.cos(this._origin.lat);
        const y = this._R * (latRad - this._origin.lat);
        
        this._chart.data.datasets[0].data.push({ x, y });
        this._chart.update('none');
        
        this._hzBuffer.push(Date.now());
        this.totalEvents++;
        this._updateStats();
        
        this._el.querySelector(`#wemp-${this.id}`).style.display = 'none';
        this._el.querySelector(`#wc-${this.id}`).style.display   = 'block';
      } catch (_) {}
    });

    es.addEventListener('error', (rawE) => {
      try {
        const body = JSON.parse(rawE.data || '{}');
        if (body.error) this._toast.show(`${this.topic}: ${body.error}`, 'error');
      } catch (_) {}
    });

    es.onopen = () => {
      this.connected = true;
      this.reconnectDelay = 1000;
      clearTimeout(this.reconnectTimer);
    };

    es.onerror = () => {
      if (!this.connected) {
        this._toast.show(`Falha na conexão SSE para ${this.topic}`, 'error');
        es.close();
        return;
      }
      this.connected = false;
      es.close();
      this._evtSource = null;
      this.reconnectTimer = setTimeout(
        () => { if (!this.connected) this._openSSE(); },
        this.reconnectDelay,
      );
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY);
    };
  }

  // ── Hz / Stats / Controls ─────────────────────────────
  _tickHz() {
    const now = Date.now();
    const cutoff = now - HZ_WINDOW_SEC * 1000;
    while (this._hzBuffer && this._hzBuffer.length && this._hzBuffer[0] < cutoff) this._hzBuffer.shift();
    const hz = (this._hzBuffer ? this._hzBuffer.length : 0) / HZ_WINDOW_SEC;
    this._renderStats(hz);
  }

  _updateStats() {
    const el = this._el.querySelector(`#ws-${this.id}`);
    if (el) el.textContent = `${this.totalEvents} eventos · … msg/s`;
  }

  _renderStats(hz) {
    const el = this._el.querySelector(`#ws-${this.id}`);
    if (el) el.textContent = `${this.totalEvents} eventos · ${hz > 0 ? hz.toFixed(1) : '—'} msg/s`;
  }

  togglePause() {
    this.paused = !this.paused;
    this._el.classList.toggle('is-paused', this.paused);
    const btn = this._el.querySelector(`#wpause-${this.id}`);
    if (btn) {
      btn.textContent = this.paused ? '▶' : '⏸';
      btn.classList.toggle('active', this.paused);
      btn.title = this.paused ? 'Retomar streaming' : 'Pausar streaming';
    }
  }

  clear() {
    this._chart.data.datasets[0].data = [];
    this._chart.update('none');
    this.totalEvents = 0;
    this._hzBuffer = [];
    this._origin = null; // reset origin
    this._renderStats(0);
    this._el.querySelector(`#wemp-${this.id}`).style.display = 'flex';
    this._el.querySelector(`#wc-${this.id}`).style.display   = 'none';
  }

  exportPNG() {
    const a = document.createElement('a');
    a.href = this._chart.toBase64Image('image/png', 1);
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    a.download = `ros-${this.title.replace(/\s+/g, '-')}-${ts}.png`;
    a.click();
  }

  destroy() {
    clearInterval(this._hzTimer);
    if (this._evtSource) this._evtSource.close();
    this._chart.destroy();
    this._el.remove();
  }
}

// ─────────────────────────────────────────────────────────
// DashboardManager
// ─────────────────────────────────────────────────────────
class DashboardManager {
  constructor(toast) {
    this._toast   = toast;
    this._windows = new Map();   // id → { id, title, widgets: Map() }
    this._nextId  = 1;
    this._nextWinId = 1;
    this._activeWindowId = null;
  }

  // ── Tab Management ────────────────────────────────────
  showTab(tabId) {
    // Esconde todos
    document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    if (tabId === 'config') {
      document.getElementById('tab-config').classList.add('active');
      document.getElementById('nav-config').classList.add('active');
      this._activeWindowId = null;
    } else {
      const winEl = document.getElementById(`win-${tabId}`);
      if (winEl) winEl.classList.add('active');
      const navEl = document.getElementById(`nav-${tabId}`);
      if (navEl) navEl.classList.add('active');
      this._activeWindowId = tabId;
    }
  }

  // ── Windows Management ────────────────────────────────
  openNewWindowModal() {
    document.getElementById('new-window-title').value = '';
    document.getElementById('modal-new-window').classList.remove('hidden');
    document.getElementById('new-window-title').focus();
  }

  confirmNewWindow() {
    const title = document.getElementById('new-window-title').value.trim();
    if (!title) {
      this._toast.show('Digite um nome para a janela', 'warn');
      return;
    }

    const winId = `w${this._nextWinId++}`;
    this._createWindowDOM(winId, title);

    App.closeModal('modal-new-window');
    this.showTab(winId);
    this.saveLayout();
  }

  _createWindowDOM(winId, title) {
    this._windows.set(winId, {
      id: winId,
      title: title,
      widgets: new Map()
    });

    // Criar o nav item
    const navList = document.getElementById('nav-windows');
    const li = document.createElement('li');
    li.className = 'nav-item';
    li.id = `nav-${winId}`;
    li.style.display = 'flex';
    li.style.alignItems = 'center';
    li.style.gap = '8px';
    li.innerHTML = `
      <span style="flex:1; overflow:hidden; text-overflow:ellipsis" onclick="App.showTab('${winId}')">${escHtml(title)}</span>
    `;
    navList.appendChild(li);

    // Criar o grid container
    const container = document.getElementById('windows-container');
    const winPane = document.createElement('div');
    winPane.id = `win-${winId}`;
    winPane.className = 'tab-pane';
    winPane.innerHTML = `
      <div class="tab-header">
        <h2>${escHtml(title)}</h2>
        <div class="tab-actions">
          <button class="btn btn-primary" onclick="App.openGpsModal('${winId}')" style="display:flex; align-items:center; gap:6px;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg>
            Adicionar GPS
          </button>
          <button class="btn btn-primary" onclick="App.openAddModal('${winId}')">+ Adicionar Gráfico</button>
          <button class="btn btn-ghost" style="color:var(--red); padding: 0 12px;" onclick="App.deleteWindow('${winId}')" title="Excluir Janela">🗑️</button>
        </div>
      </div>
      <div class="widget-grid" id="grid-${winId}">
        <div class="empty-dashboard" id="empty-${winId}">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
          <h2>Nenhum gráfico nesta janela</h2>
          <p>Clique em "+ Adicionar Gráfico" para começar.</p>
        </div>
      </div>
    `;
    container.appendChild(winPane);

    const gridEl = document.getElementById(`grid-${winId}`);
    this._setupGridDragDrop(gridEl, winId);
  }

  deleteWindow(winId) {
    if (!confirm('Tem certeza que deseja excluir esta janela e todos os seus gráficos?')) return;
    
    const win = this._windows.get(winId);
    if (!win) return;
    
    // Destroy all widgets
    for (const w of win.widgets.values()) {
      w.destroy();
    }
    
    // Remove DOM
    document.getElementById(`nav-${winId}`)?.remove();
    document.getElementById(`win-${winId}`)?.remove();
    
    this._windows.delete(winId);
    
    if (this._activeWindowId === winId) {
      this.showTab('config');
    }
    this.saveLayout();
    this._toast.show('Janela excluída.', 'info');
  }

  // ── Drag & Drop ───────────────────────────────────────
  _setupGridDragDrop(gridEl, winId) {
    gridEl.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const dragging = gridEl.querySelector('.widget.dragging');
      if (!dragging) return;
      const afterEl = this._getDragAfterElement(gridEl, e.clientY);
      if (afterEl == null) {
        gridEl.appendChild(dragging);
      } else {
        gridEl.insertBefore(dragging, afterEl);
      }
    });

    gridEl.addEventListener('drop', (e) => {
      e.preventDefault();
      this._syncWidgetOrderFromDOM(winId);
      this.saveLayout();
    });
  }

  _getDragAfterElement(grid, clientY) {
    const els = [...grid.querySelectorAll('.widget:not(.dragging)')];
    return els.reduce((closest, el) => {
      const box = el.getBoundingClientRect();
      const offset = clientY - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) {
        return { offset, el };
      }
      return closest;
    }, { offset: -Infinity, el: null }).el;
  }

  _syncWidgetOrderFromDOM(winId) {
    const win = this._windows.get(winId);
    if (!win) return;
    const gridEl = document.getElementById(`grid-${winId}`);
    const ordered = [...gridEl.querySelectorAll('.widget[data-wid]')];
    const newMap = new Map();
    for (const el of ordered) {
      const wid = el.dataset.wid;
      if (win.widgets.has(wid)) {
        newMap.set(wid, win.widgets.get(wid));
      }
    }
    win.widgets = newMap;
  }

  // ── Modal: Batch Window ───────────────────────────────
  openBatchModal() {
    document.getElementById('batch-window-title').value = '';
    // Reset to exact mode
    const radios = document.querySelectorAll('input[name="batch-mode"]');
    if (radios.length) radios[0].checked = true;
    document.getElementById('batch-exact-group').style.display  = '';
    document.getElementById('batch-prefix-group').style.display = 'none';
    document.getElementById('batch-topics-loading').style.display = '';
    document.getElementById('batch-topic-select').style.display   = 'none';
    document.getElementById('batch-prefix-input').value           = '';
    document.getElementById('batch-prefix-preview').style.display = 'none';
    document.getElementById('modal-batch-window').classList.remove('hidden');
    document.getElementById('batch-window-title').focus();
    this._loadBatchTopics();
  }

  async _loadBatchTopics() {
    this._batchTopics = [];
    try {
      const res = await fetch('/api/v1/topics');
      if (!res.ok) throw new Error('Falha ao buscar tópicos do servidor.');
      const data = await res.json();
      this._batchTopics = (data.topics || []).map(t => t.name).sort();

      const loadingEl = document.getElementById('batch-topics-loading');
      const selectEl  = document.getElementById('batch-topic-select');
      if (!loadingEl || !selectEl) return;

      if (this._batchTopics.length === 0) {
        loadingEl.textContent = 'Nenhum tópico publicado encontrado.';
        return;
      }

      selectEl.innerHTML = '';
      this._batchTopics.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        selectEl.appendChild(opt);
      });
      if (selectEl.options.length > 0) selectEl.selectedIndex = 0;

      loadingEl.style.display = 'none';
      selectEl.style.display  = '';

      // If prefix tab is already active, refresh preview
      const mode = document.querySelector('input[name="batch-mode"]:checked')?.value;
      if (mode === 'prefix') this._batchUpdatePrefixPreview();
    } catch (err) {
      const loadingEl = document.getElementById('batch-topics-loading');
      if (loadingEl) loadingEl.textContent = `Erro: ${err.message}`;
    }
  }

  onBatchModeChange() {
    const mode       = document.querySelector('input[name="batch-mode"]:checked')?.value;
    const exactGroup  = document.getElementById('batch-exact-group');
    const prefixGroup = document.getElementById('batch-prefix-group');
    if (mode === 'exact') {
      exactGroup.style.display  = '';
      prefixGroup.style.display = 'none';
    } else {
      exactGroup.style.display  = 'none';
      prefixGroup.style.display = '';
      this._batchUpdatePrefixPreview();
    }
  }

  onBatchPrefixInput() {
    this._batchUpdatePrefixPreview();
  }

  _batchUpdatePrefixPreview() {
    const input   = document.getElementById('batch-prefix-input');
    const preview = document.getElementById('batch-prefix-preview');
    if (!input || !preview) return;

    const raw     = input.value.trim();
    const topics  = this._batchTopics || [];

    if (!raw || topics.length === 0) {
      preview.style.display = 'none';
      return;
    }

    const norm    = raw.startsWith('/') ? raw : `/${raw}`;
    const matches = topics.filter(n => n.startsWith(norm));

    preview.style.display = '';
    if (matches.length === 0) {
      preview.innerHTML = '<em style="color:var(--text-muted,#888)">Nenhum tópico corresponde a este prefixo.</em>';
    } else {
      preview.innerHTML = matches.map(n =>
        `<div style="padding:2px 0; color:var(--text,#ddd)">• ${n}</div>`
      ).join('');
    }
  }

  async confirmBatchModal() {
    const title = document.getElementById('batch-window-title').value.trim();
    const mode  = document.querySelector('input[name="batch-mode"]:checked').value;

    if (!title) { this._toast.show('Digite um nome para a janela.', 'warn'); return; }

    // ── 1. Resolve topics to process ──────────────────────────────────────────
    let topicsToProcess = [];

    if (mode === 'exact') {
      const sel = document.getElementById('batch-topic-select');
      if (!sel || !sel.value) {
        this._toast.show('Selecione um tópico na lista.', 'warn');
        return;
      }
      topicsToProcess = [sel.value];
    } else {
      const raw = document.getElementById('batch-prefix-input').value.trim();
      if (!raw) { this._toast.show('Informe o prefixo.', 'warn'); return; }
      const norm = raw.startsWith('/') ? raw : `/${raw}`;
      topicsToProcess = (this._batchTopics || []).filter(n => n.startsWith(norm));
      if (topicsToProcess.length === 0) {
        this._toast.show(`Nenhum tópico encontrado com prefixo "${norm}".`, 'warn');
        return;
      }
    }

    this.closeModal('modal-batch-window');
    this._toast.show(`Criando gráficos para ${topicsToProcess.length} tópico(s)…`, 'info', 3000);

    // ── 2. Create window ───────────────────────────────────────────────────────
    const winId = `w${this._nextWinId++}`;
    this._createWindowDOM(winId, title);
    this.showTab(winId);

    // ── 3. Fields to always skip ───────────────────────────────────────────────
    const SKIP_PREFIX = ['header.', 'header'];

    let totalCreated = 0;
    let colorIdx     = 0;

    for (const topicName of topicsToProcess) {
      // ── 3a. Fetch fields (strip leading / — backend adds it back) ────────────
      let fields = [];
      try {
        const urlName = topicName.startsWith('/') ? topicName.slice(1) : topicName;
        const res     = await fetch(`/api/v1/topics/${urlName}/fields`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `Falha ao buscar campos de ${topicName}`);
        }
        const data = await res.json();
        let raw = data.fields || [];

        // Remove header fields
        raw = raw.filter(f => !SKIP_PREFIX.some(p => f === p || f.startsWith(p + '.')));

        // Keep only leaf fields (no other field is a child of this one)
        fields = raw.filter(f => !raw.some(other => other !== f && other.startsWith(f + '.')));
      } catch (err) {
        this._toast.show(err.message, 'warn');
        continue;
      }

      if (fields.length === 0) continue;

      // ── 3b. Subscribe ────────────────────────────────────────────────────────
      try {
        const res = await fetch('/api/v1/subscribe', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ topic: topicName }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(`Falha ao subscrever ${topicName}: ${body.detail || ''}`);
        }
      } catch (err) {
        this._toast.show(err.message, 'warn');
        continue;
      }

      // ── 3c. One ChartWidget per leaf field ───────────────────────────────────
      for (const field of fields) {
        const color  = DATASET_COLORS[colorIdx++ % DATASET_COLORS.length];
        const cfg    = {
          id:        String(this._nextId++),
          title:     `${topicName} · ${field}`,
          datasets:  [{ topic: topicName, field, label: field, color }],
          interval:  0.1,
          maxPoints: 200,
          yLabel:    '',
          yScale:    'linear',
          toast:     this._toast,
        };
        const widget = new ChartWidget(cfg);
        const win    = this._windows.get(winId);
        win.widgets.set(widget.id, widget);
        document.getElementById(`grid-${winId}`).appendChild(widget._el);
        totalCreated++;
      }
      this._updateEmptyState(winId);
    }

    if (totalCreated > 0) {
      this._toast.show(`${totalCreated} gráfico(s) criado(s) em "${title}".`, 'ok');
      this.saveLayout();
    } else {
      // Remove the empty window that was created
      const tab = document.querySelector(`.win-tab[data-win-id="${winId}"]`);
      if (tab) tab.remove();
      document.getElementById(`win-${winId}`)?.remove();
      this._windows.delete(winId);
      this._toast.show(
        'Nenhum gráfico criado. Verifique se os tópicos têm publishers ativos.',
        'warn'
      );
    }
  }

  // ── Persistence ───────────────────────────────────────
  saveLayout() {
    const data = { windows: [] };
    for (const [winId, win] of this._windows.entries()) {
      const wData = { id: winId, title: win.title, widgets: [] };
      for (const widget of win.widgets.values()) {
        wData.widgets.push(widget._cfg);
      }
      data.windows.push(wData);
    }
    localStorage.setItem('ros_dashboard_layout', JSON.stringify(data));
  }

  loadLayout() {
    const raw = localStorage.getItem('ros_dashboard_layout');
    if (!raw) return;
    try {
      const data = JSON.parse(raw);
      for (const wData of data.windows || []) {
        // Handle max win id
        const nId = parseInt(wData.id.replace('w',''));
        if (!isNaN(nId)) this._nextWinId = Math.max(this._nextWinId, nId + 1);
        
        this._createWindowDOM(wData.id, wData.title);
        
        for (const cfg of wData.widgets || []) {
          const wNId = parseInt(cfg.id);
          if (!isNaN(wNId)) this._nextId = Math.max(this._nextId, wNId + 1);
          
          cfg.toast = this._toast;
          let widget;
          if (cfg.type === 'gps') {
             widget = new GpsWidget(cfg);
          } else {
             widget = new ChartWidget(cfg);
          }
          const win = this._windows.get(wData.id);
          win.widgets.set(widget.id, widget);
          document.getElementById(`grid-${wData.id}`).appendChild(widget._el);
          
          // Background re-subscribe
          if (cfg.type === 'gps') {
            fetch('/api/v1/subscribe', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({topic: cfg.topic}) }).catch(()=>{});
          } else if (cfg.datasets) {
            for (const ds of cfg.datasets) {
              fetch('/api/v1/subscribe', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({topic: ds.topic}) }).catch(()=>{});
            }
          }
        }
        this._updateEmptyState(wData.id);
      }
    } catch (e) {
      console.error('Falha ao carregar layout:', e);
    }
  }

  _updateEmptyState(winId) {
    const win = this._windows.get(winId);
    if (!win) return;
    const emptyEl = document.getElementById(`empty-${winId}`);
    if (emptyEl) {
      emptyEl.style.display = win.widgets.size === 0 ? 'flex' : 'none';
    }
  }

  // ── Modal: Add chart ──────────────────────────────────
  async openAddModal(targetWinId) {
    if (targetWinId) this._activeWindowId = targetWinId;
    try {
      const res = await fetch('/api/v1/topics');
      if (!res.ok) throw new Error('API request failed');
      const data = await res.json();
      // A API retorna { count: X, topics: [...] }
      this._availableTopics = (data && Array.isArray(data.topics)) ? data.topics : null;
      if (!this._availableTopics) throw new Error('Formato inesperado');
    } catch (err) {
      this._availableTopics = null;
      this._toast.show('Aviso: Não foi possível carregar os tópicos, ativando modo manual.', 'warn');
    }

    document.getElementById('modal-title').value    = '';
    document.getElementById('modal-ylabel').value   = '';
    document.getElementById('modal-yscale').value   = 'linear';
    document.getElementById('modal-ymin').value     = '';
    document.getElementById('modal-ymax').value     = '';
    document.getElementById('modal-interval').value = '0.1';
    document.getElementById('modal-maxpts').value   = '200';

    const rows = document.getElementById('dataset-rows');
    rows.innerHTML = '';
    this._addDatasetRow();

    document.getElementById('modal-add').classList.remove('hidden');
    document.getElementById('modal-title').focus();
  }

  // ── Modal: Connection ROS ───────────────────────────
  toggleRosMode() {
    const isWifi = document.querySelector('input[name="ros-mode"]:checked').value === 'wifi';
    const fields = document.getElementById('ros-wifi-fields');
    if (isWifi) {
      fields.classList.remove('hidden');
    } else {
      fields.classList.add('hidden');
    }
  }

  async confirmConnectROS() {
    const btn = document.getElementById('btn-connect-ros');
    btn.disabled = true;
    btn.textContent = 'Conectando...';

    const mode = document.querySelector('input[name="ros-mode"]:checked').value;
    const masterUri = document.getElementById('ros-master-uri').value.trim();
    const rosIp = document.getElementById('ros-ip').value.trim();

    try {
      const res = await fetch('/api/v1/health/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode,
          master_uri: mode === 'wifi' ? masterUri : null,
          ros_ip: mode === 'wifi' && rosIp ? rosIp : null
        })
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Falha ao conectar no ROS');
      }

      this._toast.show('ROS conectado com sucesso!', 'ok');
      
      // Reinicia o polling
      rosStatus.start();
      btn.textContent = 'Conectado';
    } catch (err) {
      this._toast.show(err.message, 'error');
      btn.disabled = false;
      btn.textContent = 'Conectar ao ROS';
    }
  }

  // ── Auto IP Detection ───────────────────────────────
  onMasterUriChange() {
    const isWifi = document.querySelector('input[name="ros-mode"]:checked').value === 'wifi';
    if (isWifi) this.autoDetectIp();
  }

  async autoDetectIp() {
    const masterUri = document.getElementById('ros-master-uri').value.trim();
    if (!masterUri) return;
    
    const inputIp = document.getElementById('ros-ip');
    inputIp.placeholder = "Detectando...";
    
    try {
      const res = await fetch(`/api/v1/health/detect-ip?target_uri=${encodeURIComponent(masterUri)}`);
      if (res.ok) {
        const data = await res.json();
        if (data.ip) {
          inputIp.value = data.ip;
          this._toast.show(`IP local ${data.ip} detectado com sucesso.`, 'info', 3000);
          return;
        }
      }
      inputIp.placeholder = "Ex: 10.42.0.x";
    } catch (err) {
      inputIp.placeholder = "Falha ao detectar";
    }
  }

  // ── Modal: Add GPS ──────────────────────────────────
  async openGpsModal(targetWinId) {
    if (targetWinId) this._activeWindowId = targetWinId;
    try {
      const res = await fetch('/api/v1/topics');
      if (!res.ok) throw new Error('API request failed');
      const data = await res.json();
      const topics = data.topics || [];
      const gpsTopics = topics.filter(t => t.type === 'sensor_msgs/NavSatFix');
      
      const container = document.getElementById('gps-topic-container');
      if (gpsTopics.length > 0) {
        const options = gpsTopics.map(t => 
          `<option value="${escHtml(t.name)}">${escHtml(t.name)}</option>`
        ).join('');
        container.innerHTML = `
          <select id="modal-gps-topic" class="ds-topic">
            ${options}
          </select>`;
      } else {
        container.innerHTML = `<input type="text" id="modal-gps-topic" class="ds-topic" placeholder="/fix" autocomplete="off" />`;
        this._toast.show('Nenhum tópico NavSatFix detectado. Ativando modo manual.', 'info');
      }
    } catch (err) {
      document.getElementById('gps-topic-container').innerHTML = `<input type="text" id="modal-gps-topic" class="ds-topic" placeholder="/fix" autocomplete="off" />`;
      this._toast.show('Não foi possível carregar os tópicos, ativando modo manual.', 'warn');
    }

    document.getElementById('modal-gps-title').value = '';
    document.getElementById('modal-add-gps').classList.remove('hidden');
    document.getElementById('modal-gps-title').focus();
  }

  async confirmGpsModal() {
    const title = document.getElementById('modal-gps-title').value.trim() || `Mapa ${this._nextId}`;
    const topic = document.getElementById('modal-gps-topic').value.trim();
    const color = document.getElementById('modal-gps-color').value;

    if (!topic) {
      this._toast.show('Informe o tópico do GPS.', 'warn');
      return;
    }

    try {
      const res = await fetch('/api/v1/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(`Falha ao subscrever: ${body.detail || 'Erro desconhecido'}`);
      }
    } catch (err) {
      this._toast.show(err.message, 'error');
      return;
    }

    const cfg = {
      id: String(this._nextId++),
      title,
      topic,
      color,
      toast: this._toast,
    };

    const w = new GpsWidget(cfg);
    
    if (!this._activeWindowId) {
      this._toast.show('Você precisa criar/selecionar uma janela primeiro!', 'error');
      return;
    }
    const win = this._windows.get(this._activeWindowId);
    win.widgets.set(w.id, w);
    document.getElementById(`grid-${this._activeWindowId}`).appendChild(w._el);
    this._updateEmptyState(this._activeWindowId);

    this.closeModal('modal-add-gps');
    this._toast.show(`"${title}" criado com sucesso.`, 'ok');
    this.saveLayout();
  }

  async confirmAddModal() {
    const title = document.getElementById('modal-title').value.trim()
                  || `Gráfico ${this._nextId}`;

    const datasets = [];
    document.querySelectorAll('#dataset-rows .dataset-row').forEach((row, i) => {
      const topic = row.querySelector('.ds-topic')?.value.trim();
      const field = row.querySelector('.ds-field')?.value.trim();
      const label = row.querySelector('.ds-label')?.value.trim();
      const color = row.querySelector('.ds-color')?.value
                    || DATASET_COLORS[i % DATASET_COLORS.length];
      if (topic && field) datasets.push({ topic, field, label: label || field, color });
    });

    if (datasets.length === 0) {
      this._toast.show('Informe pelo menos um tópico e campo.', 'warn');
      return;
    }

    // Subscribe to all topics before creating the widget
    try {
      await Promise.all(datasets.map(async ds => {
        const res = await fetch('/api/v1/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ topic: ds.topic })
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(`Falha ao subscrever ${ds.topic}: ${body.detail || 'Erro desconhecido'}`);
        }
      }));
    } catch (err) {
      this._toast.show(err.message, 'error');
      return;
    }

    const yMinRaw = document.getElementById('modal-ymin').value;
    const yMaxRaw = document.getElementById('modal-ymax').value;

    const cfg = {
      id:        String(this._nextId++),
      title,
      datasets,
      interval:  parseFloat(document.getElementById('modal-interval').value)  || 0.1,
      maxPoints: parseInt(document.getElementById('modal-maxpts').value, 10)  || 200,
      yLabel:    document.getElementById('modal-ylabel').value.trim(),
      yScale:    document.getElementById('modal-yscale').value,
      yMin:      yMinRaw !== '' ? parseFloat(yMinRaw) : undefined,
      yMax:      yMaxRaw !== '' ? parseFloat(yMaxRaw) : undefined,
      toast:     this._toast,
    };

    const widget = new ChartWidget(cfg);
    if (!this._activeWindowId) {
      this._toast.show('Você precisa criar/selecionar uma janela primeiro!', 'error');
      return;
    }
    const win = this._windows.get(this._activeWindowId);
    win.widgets.set(widget.id, widget);
    document.getElementById(`grid-${this._activeWindowId}`).appendChild(widget._el);
    this._updateEmptyState(this._activeWindowId);

    this.closeModal('modal-add');
    this._toast.show(`"${title}" criado com ${datasets.length} curva(s).`, 'ok');
    this.saveLayout();
  }

  // ── Dataset rows ──────────────────────────────────────
  _addDatasetRow() {
    const rows     = document.getElementById('dataset-rows');
    const idx      = rows.querySelectorAll('.dataset-row').length;
    const color    = DATASET_COLORS[idx % DATASET_COLORS.length];
    const isFirst  = idx === 0;

    let topicInputHtml = `<input type="text" class="ds-topic" placeholder="/cmd_vel" autocomplete="off" />`;
    if (this._availableTopics && this._availableTopics.length > 0) {
      const options = this._availableTopics.map(t => 
        `<option value="${escHtml(t.name)}">${escHtml(t.name)} (${escHtml(t.type)})</option>`
      ).join('');
      topicInputHtml = `<select class="ds-topic">
        <option value="">-- Selecione --</option>
        ${options}
      </select>`;
    }

    const dlId = `dl-fields-${Date.now()}-${idx}`;

    const row = document.createElement('div');
    row.className = 'dataset-row';
    row.innerHTML = `
      <div class="ds-color-wrap">
        <input type="color" class="ds-color" value="${color}" title="Cor da curva" />
      </div>
      <div class="ds-fields">
        <div class="form-row">
          <div class="form-group">
            <label>
              Tópico ROS
              <button type="button" class="help-btn" data-help="topic">ℹ️</button>
            </label>
            ${topicInputHtml}
          </div>
          <div class="form-group">
            <label>
              Campo (Field)
              <button type="button" class="help-btn" data-help="field">ℹ️</button>
            </label>
            <input type="text" class="ds-field" list="${dlId}" placeholder="linear.x" autocomplete="off" title="Use a notação de ponto para navegar na mensagem. Ex: pose.position.x ou data" />
            <datalist id="${dlId}"></datalist>
          </div>
          <div class="form-group" style="max-width:110px">
            <label>Rótulo</label>
            <input type="text" class="ds-label" placeholder="Curva" autocomplete="off" />
          </div>
        </div>
      </div>
      ${isFirst ? '' : `<div class="ds-remove-wrap">
        <button type="button" class="wbtn danger" title="Remover curva"
          onclick="this.closest('.dataset-row').remove()">✕</button>
      </div>`}
    `;

    row.querySelectorAll('[data-help]').forEach(btn => {
      btn.addEventListener('click', () => this.openHelp(btn.dataset.help));
    });

    const topicInput = row.querySelector('.ds-topic');
    const datalist = row.querySelector('datalist');

    topicInput.addEventListener('change', async (e) => {
      const topicName = e.target.value.trim();
      if (!topicName || !datalist) return;

      try {
        const cleanName = topicName.startsWith('/') ? topicName.slice(1) : topicName;
        const res = await fetch(`/api/v1/topics/${encodeURIComponent(cleanName)}/fields`);
        if (!res.ok) throw new Error('API failed');
        const data = await res.json();
        datalist.innerHTML = (data.fields || []).map(f => `<option value="${escHtml(f)}">`).join('');
      } catch (err) {
        datalist.innerHTML = '';
      }
    });

    rows.appendChild(row);
    row.querySelector('.ds-topic').focus();
  }

  // ── Widget controls ───────────────────────────────────
  _getWidgetInfo(id) {
    for (const win of this._windows.values()) {
      if (win.widgets.has(id)) return { widget: win.widgets.get(id), winId: win.id };
    }
    return null;
  }

  clearWidget(id) {
    const info = this._getWidgetInfo(id);
    if (!info) return;
    info.widget.clear();
    this._toast.show(`Dados de "${info.widget.title}" limpos.`, 'info');
  }

  exportWidget(id) {
    const info = this._getWidgetInfo(id);
    if (info) info.widget.exportPNG();
  }

  removeWidget(id) {
    const info = this._getWidgetInfo(id);
    if (!info) return;
    const title = info.widget.title;
    info.widget.destroy();
    this._windows.get(info.winId).widgets.delete(id);
    this._toast.show(`Gráfico "${title}" removido.`, 'info');
    this._updateEmptyState(info.winId);
    this.saveLayout();
  }

  togglePause(id) {
    const info = this._getWidgetInfo(id);
    if (!info) return;
    info.widget.togglePause();
    this._toast.show(info.widget.paused ? `"${info.widget.title}" pausado` : `"${info.widget.title}" retomado`, 'info');
  }

  // ── Help modal ────────────────────────────────────────
  openHelp(context) {
    const titles = {
      topic:    'Tópico ROS',
      field:    'Campo (Field) — Notação de Ponto',
      interval: 'Intervalo de Streaming',
    };

    const bodies = {
      topic: `
        <div class="help-section">
          <h3>O que é o tópico?</h3>
          <p>O nome completo do tópico ROS publicado no roscore, incluindo a barra inicial.</p>
          <div class="help-example">/chatter<br>/cmd_vel<br>/scan<br>/gps/fix</div>
        </div>
        <div class="help-section">
          <h3>Pré-requisito</h3>
          <p>O tópico precisa estar subscrito antes de visualizar. Use o endpoint:
          <code style="color:var(--accent)">POST /api/v1/subscribe</code> com body
          <code style="color:var(--accent)">{"topic": "/nome"}</code>.</p>
        </div>
      `,
      field: `
        <div class="help-section">
          <h3>Notação de Ponto</h3>
          <p>Use ponto (<code style="color:var(--accent)">.</code>) para acessar campos aninhados dentro da mensagem ROS.</p>
        </div>
        <div class="help-section">
          <h3>Exemplos por tipo de mensagem</h3>
          <div class="help-example">
            std_msgs/Float64  →  data<br>
            std_msgs/String   →  data<br>
            geometry_msgs/Twist  →  linear.x  |  angular.z<br>
            geometry_msgs/Pose   →  position.x  |  orientation.w<br>
            sensor_msgs/NavSatFix  →  latitude  |  longitude<br>
            sensor_msgs/Imu  →  linear_acceleration.x<br>
            nav_msgs/Odometry  →  pose.pose.position.x
          </div>
        </div>
        <div class="help-section">
          <h3>Arrays</h3>
          <p>Campos do tipo array são convertidos automaticamente para a média dos valores numéricos.</p>
        </div>
      `,
      interval: `
        <div class="help-section">
          <h3>Intervalo de Polling (segundos)</h3>
          <p>Define com que frequência o servidor envia dados via SSE. Valores menores resultam em mais atualizações por segundo, mas maior carga na rede e no browser.</p>
          <div class="help-example">
            0.05  →  20 msg/s (alta frequência)<br>
            0.1   →  10 msg/s (padrão recomendado)<br>
            0.5   →   2 msg/s (baixa carga)<br>
            1.0   →   1 msg/s (monitoramento lento)
          </div>
        </div>
      `,
    };

    document.getElementById('help-title').textContent  = titles[context]  || 'Ajuda';
    document.getElementById('help-body').innerHTML     = bodies[context]  || '<p>Sem informações disponíveis.</p>';
    document.getElementById('modal-help').classList.remove('hidden');
  }

  // ── Modal helpers ─────────────────────────────────────
  closeModal(id) {
    document.getElementById(id).classList.add('hidden');
  }

  onOverlayClick(event, modalId) {
    if (event.target === event.currentTarget) this.closeModal(modalId);
  }
}

// ─────────────────────────────────────────────────────────
// Bootstrap
// ─────────────────────────────────────────────────────────
const toast     = new ToastManager();
const rosStatus = new ROSStatus(toast);
const dashboard = new DashboardManager(toast);

// Global App namespace (used by inline onclick handlers)
const App = {
  openAddModal:       (wid)    => dashboard.openAddModal(wid),
  confirmAddModal:    ()       => dashboard.confirmAddModal(),
  openGpsModal:       (wid)    => dashboard.openGpsModal(wid),
  confirmGpsModal:    ()       => dashboard.confirmGpsModal(),
  toggleRosMode:      ()       => dashboard.toggleRosMode(),
  confirmConnectROS:  ()       => dashboard.confirmConnectROS(),
  showTab:            (id)     => dashboard.showTab(id),
  openNewWindowModal: ()       => dashboard.openNewWindowModal(),
  confirmNewWindow:   ()       => dashboard.confirmNewWindow(),
  deleteWindow:       (id)     => dashboard.deleteWindow(id),
  openBatchModal:     ()       => dashboard.openBatchModal(),
  confirmBatchModal:  ()       => dashboard.confirmBatchModal(),
  onBatchModeChange:  ()       => dashboard.onBatchModeChange(),
  onBatchPrefixInput: ()       => dashboard.onBatchPrefixInput(),
  autoDetectIp:       ()       => dashboard.autoDetectIp(),
  onMasterUriChange:  ()       => dashboard.onMasterUriChange(),
  addDatasetRow:      ()       => dashboard._addDatasetRow(),
  clearWidget:        (id)     => dashboard.clearWidget(id),
  exportWidget:       (id)     => dashboard.exportWidget(id),
  removeWidget:       (id)     => dashboard.removeWidget(id),
  togglePause:        (id)     => dashboard.togglePause(id),
  openHelp:           (ctx)    => dashboard.openHelp(ctx),
  closeModal:         (id)     => dashboard.closeModal(id),
  onOverlayClick:     (ev, id) => dashboard.onOverlayClick(ev, id),
};

document.addEventListener('DOMContentLoaded', () => {
  rosStatus.start();
  dashboard.loadLayout();

  // Global keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      ['modal-add', 'modal-add-gps', 'modal-help', 'modal-new-window', 'modal-batch-window'].forEach(id => {
        document.getElementById(id).classList.add('hidden');
      });
    }
    // Shift+A → open Add modal
    if (e.shiftKey && e.key === 'A' && !e.ctrlKey && !e.metaKey) {
      const focused = document.activeElement;
      if (focused.tagName !== 'INPUT' && focused.tagName !== 'TEXTAREA') {
        dashboard.openAddModal();
      }
    }
  });

  // Accessibility: trap focus in modals (simplified)
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        const focusable = overlay.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
        const first = focusable[0];
        const last  = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    });
  });
});
