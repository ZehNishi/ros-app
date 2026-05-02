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
    this._check();
    this._timer = setInterval(() => this._check(), ROS_POLL_INTERVAL);
  }

  stop() { clearInterval(this._timer); }

  async _check() {
    try {
      const ctrl = new AbortController();
      const tid  = setTimeout(() => ctrl.abort(), 4000);
      const res  = await fetch('/api/v1/health/ros', { signal: ctrl.signal });
      clearTimeout(tid);

      const body = await res.json().catch(() => ({}));

      if (res.ok && (body.status === 'ok' || body.ros_ok === true)) {
        this._set('connected', 'ROS Conectado');
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

    const grid = document.getElementById('widget-grid');
    document.getElementById('empty-dashboard').style.display = 'none';
    grid.appendChild(el);
    this._el = el;

    document.getElementById(`wpause-${this.id}`)
      .addEventListener('click', () => this.togglePause());
  }

  // ── Chart.js ─────────────────────────────────────────
  _initChart() {
    const canvas = document.getElementById(`wc-${this.id}`);
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
        document.getElementById(`wemp-${this.id}`).style.display = 'none';
        document.getElementById(`wc-${this.id}`).style.display   = 'block';
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
    const el = document.getElementById(`wleg-${this.id}`);
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
    const el = document.getElementById(`ws-${this.id}`);
    if (el) el.textContent = `${this.totalEvents} eventos · … msg/s`;
  }

  _renderStats(hz) {
    const el = document.getElementById(`ws-${this.id}`);
    if (el) el.textContent = `${this.totalEvents} eventos · ${hz > 0 ? hz.toFixed(1) : '—'} msg/s`;
  }

  // ── Public controls ───────────────────────────────────
  togglePause() {
    this.paused = !this.paused;
    this._el.classList.toggle('is-paused', this.paused);
    const btn = document.getElementById(`wpause-${this.id}`);
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
    document.getElementById(`wemp-${this.id}`).style.display = 'flex';
    document.getElementById(`wc-${this.id}`).style.display   = 'none';
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
// DashboardManager
// ─────────────────────────────────────────────────────────
class DashboardManager {
  constructor(toast) {
    this._toast   = toast;
    this._widgets = new Map();   // id → ChartWidget
    this._nextId  = 1;
  }

  // ── Modal: Add chart ──────────────────────────────────
  openAddModal() {
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

  confirmAddModal() {
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
    this._widgets.set(cfg.id, widget);
    this.closeModal('modal-add');
    this._toast.show(`"${title}" criado com ${datasets.length} curva(s).`, 'ok');
  }

  // ── Dataset rows ──────────────────────────────────────
  _addDatasetRow() {
    const rows     = document.getElementById('dataset-rows');
    const idx      = rows.querySelectorAll('.dataset-row').length;
    const color    = DATASET_COLORS[idx % DATASET_COLORS.length];
    const isFirst  = idx === 0;

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
            <input type="text" class="ds-topic" placeholder="/cmd_vel" autocomplete="off" />
          </div>
          <div class="form-group">
            <label>
              Campo (Field)
              <button type="button" class="help-btn" data-help="field">ℹ️</button>
            </label>
            <input type="text" class="ds-field" placeholder="linear.x" autocomplete="off" />
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

    rows.appendChild(row);
    row.querySelector('.ds-topic').focus();
  }

  // ── Widget controls ───────────────────────────────────
  clearWidget(id) {
    const w = this._widgets.get(id);
    if (!w) return;
    w.clear();
    this._toast.show(`Dados de "${w.title}" limpos.`, 'info');
  }

  exportWidget(id) {
    const w = this._widgets.get(id);
    if (w) w.exportPNG();
  }

  removeWidget(id) {
    const w = this._widgets.get(id);
    if (!w) return;
    const title = w.title;
    w.destroy();
    this._widgets.delete(id);
    this._toast.show(`Gráfico "${title}" removido.`, 'info');
    if (this._widgets.size === 0) {
      document.getElementById('empty-dashboard').style.display = 'flex';
    }
  }

  togglePause(id) {
    const w = this._widgets.get(id);
    if (!w) return;
    w.togglePause();
    this._toast.show(w.paused ? `"${w.title}" pausado` : `"${w.title}" retomado`, 'info');
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
  openAddModal:    ()          => dashboard.openAddModal(),
  confirmAddModal: ()          => dashboard.confirmAddModal(),
  addDatasetRow:   ()          => dashboard._addDatasetRow(),
  clearWidget:     (id)        => dashboard.clearWidget(id),
  exportWidget:    (id)        => dashboard.exportWidget(id),
  removeWidget:    (id)        => dashboard.removeWidget(id),
  togglePause:     (id)        => dashboard.togglePause(id),
  openHelp:        (ctx)       => dashboard.openHelp(ctx),
  closeModal:      (id)        => dashboard.closeModal(id),
  onOverlayClick:  (ev, id)    => dashboard.onOverlayClick(ev, id),
};

document.addEventListener('DOMContentLoaded', () => {
  rosStatus.start();

  // Global keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      ['modal-add', 'modal-help'].forEach(id => {
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
