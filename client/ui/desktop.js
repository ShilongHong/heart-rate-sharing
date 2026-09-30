// Desktop shell: settings + "my heart rate" panel. Talks to Python via window.pywebview.api
// and configures the shared dashboard (web/app.js, loaded after this file).
const $ = (selector) => document.querySelector(selector);

const fields = {
  server_url: $("#server-url"),
  github_repo: $("#github-repo"),
  github_token: $("#github-token"),
  dashboard_url: $("#dashboard-url"),
  name: $("#name"),
  token: $("#token"),
};
const meCard = $("#me-card");
const stateChip = $("#state-chip");
const myBpm = $("#my-bpm");
const statusText = $("#status-text");
const toggleButton = $("#toggle-button");
const settingsPanel = $("#settings");
const deviceSelect = $("#device-select");
const scanButton = $("#scan-button");
const modeButtons = document.querySelectorAll("[data-mode]");
const modeHint = $("#mode-hint");
const segmented = $("#segmented");
const ecgCanvas = $("#ecg");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const STATE_LABELS = { idle: "未开始", working: "连接中", live: "采集中", error: "异常" };
const MODE_HINTS = {
  backend: "实时上传，支持历史趋势",
  action: "经 GitHub Actions 转发，每 60 秒提交一次最新值",
};

let mode = "backend";
let collecting = false;
let deviceNames = {};

const api = () => window.pywebview.api;

// A bridge call whose Python side dies silently never settles; don't let the UI wait forever.
function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function dashboardServer() {
  return (mode === "action" ? fields.dashboard_url : fields.server_url).value.trim();
}

function websocketBase(value) {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (/^https:\/\//i.test(trimmed)) return "wss://" + trimmed.slice(8);
  if (/^http:\/\//i.test(trimmed)) return "ws://" + trimmed.slice(7);
  if (/^wss?:\/\//i.test(trimmed)) return trimmed;
  return "ws://" + trimmed;
}

window.HR_DASHBOARD = {
  autoConnect: false,
  selfId: null,
  getToken: () => fields.token.value.trim(),
  monitorUrl: (token) => {
    const server = dashboardServer();
    return server ? `${websocketBase(server)}/ws/monitor?token=${token}` : "";
  },
  fetchHistory: (clientId, minutes, token) => api().fetch_history(dashboardServer(), clientId, minutes, token),
};

function settings() {
  const values = { mode, device_address: deviceSelect.value, device_name: deviceNames[deviceSelect.value] || "" };
  for (const [key, input] of Object.entries(fields)) values[key] = input.value.trim();
  return values;
}

function restartAnimation(element, className) {
  element.classList.remove(className);
  void element.offsetWidth;
  element.classList.add(className);
}

function setMode(value) {
  mode = value === "action" ? "action" : "backend";
  segmented.dataset.active = mode;
  modeButtons.forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  document.querySelectorAll("[data-show]").forEach((element) => {
    const show = element.dataset.show === mode;
    if (show && element.hidden) restartAnimation(element, "reveal");
    element.hidden = !show;
  });
  modeHint.textContent = MODE_HINTS[mode];
}

function setState(state, text) {
  meCard.className = `me-card glass sheen state-${state}`;
  stateChip.textContent = STATE_LABELS[state] || STATE_LABELS.idle;
  ecg.live = state === "live";
  if (text && text !== statusText.textContent) {
    statusText.textContent = text;
    restartAnimation(statusText, "flash");
  }
}

function showBpm(value) {
  // tweenNumber comes from web/app.js (loaded after this file, called only at runtime).
  if (window.tweenNumber) window.tweenNumber(myBpm, value);
  else myBpm.textContent = value == null ? "--" : String(value);
}

// ---------- ECG trace: scrolls continuously, beats at the live BPM ----------

const ecg = {
  live: false,
  bpm: 72,
  amplitude: 0,
  phase: 0,
  samples: [],
  last: 0,
};

function ecgWave(p) {
  const bump = (center, width) => Math.exp(-((p - center) ** 2) / (2 * width * width));
  return 0.12 * bump(0.12, 0.025)   // P
    - 0.14 * bump(0.235, 0.008)      // Q
    + 1.00 * bump(0.25, 0.010)       // R
    - 0.28 * bump(0.268, 0.009)      // S
    + 0.24 * bump(0.46, 0.045);      // T
}

function drawEcg(now) {
  const width = ecgCanvas.clientWidth;
  const height = ecgCanvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  if (ecgCanvas.width !== Math.round(width * ratio)) {
    ecgCanvas.width = Math.round(width * ratio);
    ecgCanvas.height = Math.round(height * ratio);
  }
  const dt = Math.min(0.05, (now - (ecg.last || now)) / 1000);
  ecg.last = now;
  ecg.amplitude += ((ecg.live ? 1 : 0) - ecg.amplitude) * Math.min(1, dt * 4);

  const pixelsPerSecond = 110;
  let fresh = Math.max(1, Math.round(dt * pixelsPerSecond));
  while (fresh--) {
    ecg.phase = (ecg.phase + (ecg.bpm / 60) / pixelsPerSecond) % 1;
    ecg.samples.push(ecgWave(ecg.phase) * ecg.amplitude);
  }
  if (ecg.samples.length > width) ecg.samples.splice(0, ecg.samples.length - width);

  const context = ecgCanvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const baseline = height * 0.64;
  const scale = height * 0.52;
  const offset = width - ecg.samples.length;
  const stroke = context.createLinearGradient(0, 0, width, 0);
  stroke.addColorStop(0, "rgba(229, 72, 77, 0)");
  stroke.addColorStop(0.55, `rgba(229, 72, 77, ${0.2 + 0.45 * ecg.amplitude})`);
  stroke.addColorStop(1, `rgba(229, 72, 77, ${0.3 + 0.6 * ecg.amplitude})`);
  context.beginPath();
  ecg.samples.forEach((value, index) => {
    const x = offset + index;
    const y = baseline - value * scale;
    index ? context.lineTo(x, y) : context.moveTo(x, y);
  });
  context.strokeStyle = ecg.amplitude > 0.02 ? stroke : "rgba(107, 113, 133, .25)";
  context.lineWidth = 2;
  context.lineJoin = "round";
  context.shadowColor = "rgba(229, 72, 77, .3)";
  context.shadowBlur = 5 * ecg.amplitude;
  context.stroke();
  if (ecg.samples.length) {
    const headY = baseline - ecg.samples[ecg.samples.length - 1] * scale;
    context.beginPath();
    context.arc(width - 1.5, headY, 3, 0, Math.PI * 2);
    context.fillStyle = ecg.amplitude > 0.02 ? "#e5484d" : "rgba(107, 113, 133, .4)";
    context.fill();
  }
  if (!reduceMotion.matches) requestAnimationFrame(drawEcg);
}
requestAnimationFrame(drawEcg);

function setCollecting(value) {
  collecting = value;
  toggleButton.disabled = false;
  toggleButton.textContent = value ? "停止采集" : "开始采集";
  toggleButton.classList.toggle("stop", value);
  settingsPanel.classList.toggle("locked", value);
  scanButton.disabled = value;
}

function deviceLabel(device) {
  const label = `${device.name} · ${device.address} · ${device.rssi} dBm`;
  return device.heart_rate ? `❤ ${label}` : `${label}（未广播心率服务）`;
}

function fillDevices(devices, placeholder) {
  const previous = deviceSelect.value;
  deviceSelect.innerHTML = "";
  if (!devices.length) {
    deviceSelect.add(new Option(placeholder, ""));
    return;
  }
  for (const device of devices) deviceSelect.add(new Option(deviceLabel(device), device.address));
  if (devices.some((device) => device.address === previous)) deviceSelect.value = previous;
}

async function scan() {
  scanButton.disabled = true;
  toggleButton.disabled = true;
  scanButton.classList.add("loading");
  scanButton.textContent = "扫描中";
  setState("working", "正在扫描附近的蓝牙设备（约 6 秒）…");
  try {
    const devices = await withTimeout(api().scan(), 25000, "扫描没有响应，请重试；如果反复出现，请打开日志文件夹把日志发给开发者");
    const heartRateDevices = devices.filter((device) => device.heart_rate);
    // Some straps don't advertise 0x180D; only then fall back to listing everything.
    const shown = heartRateDevices.length ? heartRateDevices : devices;
    deviceNames = Object.fromEntries(shown.map((device) => [device.address, device.name]));
    fillDevices(shown, "未发现设备，请重新扫描");
    if (heartRateDevices.length) restartAnimation(deviceSelect, "updated");
    setState("idle", heartRateDevices.length
      ? `找到 ${heartRateDevices.length} 个心率设备，请确认下拉框中的选择后开始采集`
      : "没有发现心率设备：请确认设备已开启心率广播，且没有被手机 App 占用；也可以在列表中手动选择");
    await api().save_settings(settings());
  } catch (error) {
    setState("error", error.message || String(error));
  } finally {
    scanButton.classList.remove("loading");
    scanButton.textContent = "扫描";
    scanButton.disabled = false;
    toggleButton.disabled = false;
  }
}

async function toggle() {
  if (collecting) {
    toggleButton.disabled = true;
    toggleButton.textContent = "正在停止…";
    await api().stop();
    return;
  }
  const result = await api().start(settings());
  if (!result.ok) {
    setState("error", result.error);
    return;
  }
  setCollecting(true);
  setState("working", "正在启动…");
}

window.desktop = {
  onStatus(text, state) {
    if (!collecting && state !== "idle") return;
    setState(state, text);
  },
  onHeartRate(bpm) {
    showBpm(bpm);
    ecg.bpm = Math.max(30, Math.min(220, bpm));
    meCard.style.setProperty("--beat", `${(60 / ecg.bpm).toFixed(2)}s`);
  },
  onStopped() {
    setCollecting(false);
    showBpm(null);
    setState("idle", "已停止");
  },
  async init() {
    const state = await api().get_state();
    const config = state.config;
    setMode(config.mode);
    for (const [key, input] of Object.entries(fields)) input.value = config[key] || "";
    if (!fields.server_url.value) fields.server_url.value = "http://127.0.0.1:8000";
    if (config.device_address) {
      deviceNames[config.device_address] = config.device_name;
      deviceSelect.add(new Option(`${config.device_name || "上次使用的设备"} · ${config.device_address}`, config.device_address));
    } else {
      deviceSelect.add(new Option("请点击“扫描”查找心率设备", ""));
    }
    window.HR_DASHBOARD.selfId = state.client_id;
    setCollecting(state.collecting);
    window.hrDashboard.connect();
  },
};

modeButtons.forEach((button) => button.addEventListener("click", () => {
  if (collecting || button.dataset.mode === mode) return;
  setMode(button.dataset.mode);
  api().save_settings(settings());
  window.hrDashboard.connect();
}));
for (const [key, input] of Object.entries(fields)) {
  input.addEventListener("change", () => {
    api().save_settings(settings());
    if (["server_url", "dashboard_url", "token"].includes(key)) window.hrDashboard.connect();
  });
}
deviceSelect.addEventListener("change", () => api().save_settings(settings()));
scanButton.addEventListener("click", scan);
toggleButton.addEventListener("click", toggle);
$("#log-button").addEventListener("click", () => api().open_log_folder());

// Init needs both the Python bridge and web/app.js (window.hrDashboard), in whichever order they arrive.
const domReady = new Promise((resolve) => document.addEventListener("DOMContentLoaded", resolve, { once: true }));
const bridgeReady = window.pywebview && window.pywebview.api
  ? Promise.resolve()
  : new Promise((resolve) => window.addEventListener("pywebviewready", resolve, { once: true }));
Promise.all([domReady, bridgeReady]).then(() => window.desktop.init());
