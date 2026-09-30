const tokenInput = document.querySelector("#token-input");
const connectButton = document.querySelector("#connect-button");
const searchInput = document.querySelector("#search-input");
const grid = document.querySelector("#people-grid");
const emptyState = document.querySelector("#empty-state");
const connectionPill = document.querySelector("#connection-pill");
const connectionText = document.querySelector("#connection-text");
const onlineCount = document.querySelector("#online-count");
const highestRate = document.querySelector("#highest-rate");
const totalCount = document.querySelector("#total-count");
const historyPanel = document.querySelector("#history-panel");
const historyName = document.querySelector("#history-name");
const historyChart = document.querySelector("#history-chart");
const historyEmpty = document.querySelector("#history-empty");
const historyMeta = document.querySelector("#history-meta");
const rangeTabs = document.querySelectorAll(".range-tab");
const subtitle = document.querySelector(".subtitle");

// The desktop collector embeds this dashboard and sets window.HR_DASHBOARD before loading
// this script: monitorUrl(token), fetchHistory(clientId, minutes, token), getToken(),
// selfId, autoConnect. The web page itself sets nothing and uses the defaults below.
const config = window.HR_DASHBOARD || {};

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const EASE_OUT = "cubic-bezier(.22, 1, .36, 1)";
const HEART_PATH = "M12 21s-7.5-4.6-9.6-9.3C.9 8.3 3 4.5 6.7 4.5c2.1 0 3.6 1.1 4.4 2.5h1.8c.8-1.4 2.3-2.5 4.4-2.5 3.7 0 5.8 3.8 4.3 7.2C19.5 16.4 12 21 12 21z";
const emptyStateDefault = emptyState.innerHTML;

let socket = null;
let people = [];
let reconnectTimer = null;
let selectedPersonId = null;
let selectedMinutes = 5;
let historySamples = [];
let historyEnabled = true;
let hasRenderedCards = false;
const cards = new Map();

if (tokenInput) tokenInput.value = localStorage.getItem("hr-token") || "";

function currentToken() {
  return config.getToken ? config.getToken() : tokenInput.value.trim();
}

function setConnection(state, text) {
  connectionPill.className = `connection-pill ${state || ""}`;
  connectionText.textContent = text;
}

function websocketUrl() {
  const token = encodeURIComponent(currentToken());
  if (config.monitorUrl) return config.monitorUrl(token);
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/monitor?token=${token}`;
}

function disconnect() {
  clearTimeout(reconnectTimer);
  const previous = socket;
  socket = null;
  if (previous) previous.close();
}

function connect() {
  disconnect();
  const url = websocketUrl();
  if (!url) {
    people = [];
    render();
    setConnection("error", "未设置看板地址");
    return;
  }
  if (tokenInput) localStorage.setItem("hr-token", tokenInput.value.trim());
  setConnection("", "正在连接");
  socket = new WebSocket(url);
  socket.addEventListener("open", () => {
    setConnection("connected", "已连接");
    if (connectButton) connectButton.textContent = "重新连接";
  });
  socket.addEventListener("message", (event) => {
    if (socket !== event.target) return;
    try {
      const message = JSON.parse(event.data);
      if (message.type === "snapshot") {
        historyEnabled = message.history_enabled !== false;
        if (!historyEnabled) {
          historyPanel.hidden = true;
          selectedPersonId = null;
          if (subtitle) subtitle.textContent = "Action 最新状态 · 最多每 60 秒提交一次 · 3 分钟过期 · 不支持历史";
        }
        people = Array.isArray(message.people) ? message.people : [];
        render();
      }
    } catch (_) {
      setConnection("error", "数据格式错误");
    }
  });
  socket.addEventListener("close", (event) => {
    if (socket !== event.target) return;
    setConnection("error", event.code === 1008 ? "令牌无效" : "连接已断开");
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 4000);
  });
  socket.addEventListener("error", (event) => {
    if (socket === event.target) setConnection("error", "连接失败");
  });
}

function formatLastSeen(value) {
  if (!value) return "还没有心率数据";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "最近刚刚更新";
  return `最后更新 ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

// ---------- Animated numbers ----------

function tweenNumber(element, value) {
  if (!Number.isFinite(value)) {
    cancelAnimationFrame(element._tween);
    element._value = null;
    element.textContent = "--";
    return;
  }
  const from = element._value;
  element._value = value;
  if (!Number.isFinite(from) || from === value || reducedMotion.matches) {
    element.textContent = String(value);
    return;
  }
  cancelAnimationFrame(element._tween);
  const start = performance.now();
  const duration = 480;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    element.textContent = String(Math.round(from + (value - from) * eased));
    if (t < 1) element._tween = requestAnimationFrame(step);
  };
  element._tween = requestAnimationFrame(step);
}

// ---------- Person cards (keyed, updated in place so animations survive snapshots) ----------

function createCard(clientId) {
  const card = document.createElement("article");
  card.className = "person-card glass";
  card.dataset.id = clientId;
  card.innerHTML = `
    <div class="person-head">
      <div class="person-name"><span class="name-text"></span></div>
      <div class="status"></div>
    </div>
    <div class="bpm-row">
      <div class="bpm">--</div>
      <svg class="beat-heart" viewBox="0 0 24 24" aria-hidden="true"><path d="${HEART_PATH}"/></svg>
    </div>
    <div class="bpm-unit">BPM</div>
    <div class="last-seen"></div>
    <button class="trend-button" data-person-id="">查看趋势</button>`;
  card._refs = {
    nameWrap: card.querySelector(".person-name"),
    name: card.querySelector(".name-text"),
    status: card.querySelector(".status"),
    bpm: card.querySelector(".bpm"),
    lastSeen: card.querySelector(".last-seen"),
    trend: card.querySelector(".trend-button"),
  };
  card._refs.trend.dataset.personId = clientId;
  return card;
}

function updateCard(card, person) {
  const refs = card._refs;
  const isSelf = Boolean(config.selfId) && person.client_id === config.selfId;
  card.classList.toggle("offline", !person.online);
  card.classList.toggle("self", isSelf);
  refs.name.textContent = person.name;
  refs.nameWrap.title = person.name;
  if (isSelf !== Boolean(refs.selfTag)) {
    if (isSelf) {
      refs.selfTag = document.createElement("span");
      refs.selfTag.className = "self-tag";
      refs.selfTag.textContent = "我";
      refs.nameWrap.append(refs.selfTag);
    } else {
      refs.selfTag.remove();
      refs.selfTag = null;
    }
  }
  refs.status.textContent = person.online ? "在线" : "离线";
  const rate = Number.isFinite(person.heart_rate) ? person.heart_rate : null;
  tweenNumber(refs.bpm, rate);
  if (rate) card.style.setProperty("--beat", `${(60 / Math.max(rate, 30)).toFixed(2)}s`);
  refs.lastSeen.textContent = formatLastSeen(person.last_seen);
  refs.trend.hidden = !historyEnabled;
  refs.trend.classList.toggle("selected", selectedPersonId === person.client_id);
}

function removeCard(card) {
  if (reducedMotion.matches) {
    card.remove();
    return;
  }
  card.classList.add("leaving");
  card.addEventListener("animationend", (event) => {
    if (event.target === card && event.animationName === "card-out") card.remove();
  });
}

function render() {
  const keyword = searchInput.value.trim().toLowerCase();
  const visible = people.filter((person) => person.name.toLowerCase().includes(keyword));
  const online = people.filter((person) => person.online);
  const rates = online.map((person) => person.heart_rate).filter((rate) => Number.isFinite(rate));
  tweenNumber(onlineCount, online.length);
  tweenNumber(totalCount, people.length);
  tweenNumber(highestRate, rates.length ? Math.max(...rates) : null);

  // FLIP: remember where existing cards were before reordering.
  const before = new Map();
  for (const [id, card] of cards) before.set(id, card.getBoundingClientRect());

  const visibleIds = new Set(visible.map((person) => person.client_id));
  for (const [id, card] of cards) {
    if (!visibleIds.has(id)) {
      cards.delete(id);
      removeCard(card);
    }
  }
  // Only move nodes that are out of place: re-inserting a node restarts its CSS animations.
  let cursor = grid.firstElementChild;
  const skipLeaving = () => {
    while (cursor && cursor.classList.contains("leaving")) cursor = cursor.nextElementSibling;
  };
  visible.forEach((person, index) => {
    let card = cards.get(person.client_id);
    if (!card) {
      card = createCard(person.client_id);
      card.style.setProperty("--delay", hasRenderedCards ? "0ms" : `${index * 55}ms`);
      card.addEventListener("animationend", (event) => {
        if (event.target === card && event.animationName === "card-in") card.classList.add("entered");
      });
      cards.set(person.client_id, card);
    }
    updateCard(card, person);
    skipLeaving();
    if (cursor === card) cursor = card.nextElementSibling;
    else grid.insertBefore(card, cursor);
  });
  if (visible.length) hasRenderedCards = true;

  if (!reducedMotion.matches) {
    for (const [id, card] of cards) {
      const first = before.get(id);
      if (!first) continue;
      const last = card.getBoundingClientRect();
      const dx = first.left - last.left;
      const dy = first.top - last.top;
      if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
        card.animate(
          [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "translate(0, 0)" }],
          { duration: 520, easing: EASE_OUT },
        );
      }
    }
  }

  if (!people.length) {
    emptyState.innerHTML = emptyStateDefault;
    emptyState.style.display = "block";
  } else if (!visible.length) {
    emptyState.innerHTML = '<div class="empty-icon">⌕</div><h2>没有匹配的人</h2><p>换一个名字试试。</p>';
    emptyState.style.display = "block";
  } else {
    emptyState.style.display = "none";
  }
}

// Specular sheen follows the pointer over any .sheen glass surface.
document.addEventListener("pointermove", (event) => {
  const surface = event.target.closest && event.target.closest(".sheen");
  if (!surface) return;
  const rect = surface.getBoundingClientRect();
  surface.style.setProperty("--mx", `${event.clientX - rect.left}px`);
  surface.style.setProperty("--my", `${event.clientY - rect.top}px`);
});

// ---------- History ----------

async function selectPerson(clientId) {
  if (!historyEnabled) return;
  const person = people.find((item) => item.client_id === clientId);
  if (!person) return;
  selectedPersonId = clientId;
  historyName.textContent = person.name;
  historyPanel.hidden = false;
  render();
  await loadHistory();
  historyPanel.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
}

async function loadHistory() {
  if (!selectedPersonId) return;
  historyEmpty.textContent = "正在加载…";
  historyEmpty.style.display = "grid";
  const token = currentToken();
  try {
    const result = config.fetchHistory
      ? await config.fetchHistory(selectedPersonId, selectedMinutes, token)
      : await fetchHistory(selectedPersonId, selectedMinutes, token);
    historySamples = Array.isArray(result.samples) ? result.samples : [];
    historyName.textContent = result.name || historyName.textContent;
    animateHistory();
    historyMeta.textContent = `${selectedMinutes} 分钟内 ${historySamples.length} 个采样点`;
  } catch (_) {
    historySamples = [];
    drawHistory();
    historyEmpty.textContent = "历史数据加载失败，请检查连接或令牌";
    historyEmpty.style.display = "grid";
    historyMeta.textContent = "";
  }
}

async function fetchHistory(clientId, minutes, token) {
  const endpoint = `/api/people/${encodeURIComponent(clientId)}/history?minutes=${minutes}`;
  const response = await fetch(endpoint, token
    ? { headers: { Authorization: `Bearer ${token}` } }
    : undefined);
  if (!response.ok) throw new Error("history request failed");
  return response.json();
}

let historyAnimation = null;

function animateHistory() {
  cancelAnimationFrame(historyAnimation);
  if (reducedMotion.matches) {
    drawHistory(1);
    return;
  }
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 900);
    drawHistory(1 - Math.pow(1 - t, 3));
    if (t < 1) historyAnimation = requestAnimationFrame(step);
  };
  historyAnimation = requestAnimationFrame(step);
}

function drawHistory(progress = 1) {
  const width = historyChart.clientWidth || 600;
  const height = 260;
  const ratio = window.devicePixelRatio || 1;
  historyChart.width = width * ratio;
  historyChart.height = height * ratio;
  const context = historyChart.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  if (!historySamples.length) {
    historyEmpty.textContent = "暂无这段时间的心率数据";
    historyEmpty.style.display = "grid";
    return;
  }
  historyEmpty.style.display = "none";

  const values = historySamples.map((sample) => Number(sample.heart_rate));
  const minValue = Math.max(20, Math.floor(Math.min(...values) / 10) * 10 - 10);
  const maxValue = Math.ceil(Math.max(...values) / 10) * 10 + 10;
  const left = 42, right = 12, top = 14, bottom = 28;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  context.font = "11px 'Segoe UI', system-ui, sans-serif";
  context.strokeStyle = "rgba(30, 34, 48, .07)";
  context.fillStyle = "#a3a8b8";
  context.lineWidth = 1;
  for (let index = 0; index <= 4; index += 1) {
    const y = top + chartHeight * index / 4;
    const value = Math.round(maxValue - (maxValue - minValue) * index / 4);
    context.beginPath();
    context.moveTo(left, y);
    context.lineTo(width - right, y);
    context.stroke();
    context.fillText(String(value), 5, y + 4);
  }
  const points = historySamples.map((sample, index) => ({
    x: historySamples.length === 1 ? left + chartWidth / 2 : left + chartWidth * index / (historySamples.length - 1),
    y: top + chartHeight * (maxValue - Number(sample.heart_rate)) / (maxValue - minValue || 1),
  }));

  // Reveal left → right while animating.
  context.save();
  context.beginPath();
  context.rect(0, 0, left + chartWidth * progress + 6, height);
  context.clip();

  const tracePath = () => {
    context.beginPath();
    points.forEach((item, index) => {
      if (!index) return context.moveTo(item.x, item.y);
      const previous = points[index - 1];
      const midX = (previous.x + item.x) / 2;
      context.bezierCurveTo(midX, previous.y, midX, item.y, item.x, item.y);
    });
  };
  const area = context.createLinearGradient(0, top, 0, top + chartHeight);
  area.addColorStop(0, "rgba(229, 72, 77, .18)");
  area.addColorStop(1, "rgba(229, 72, 77, 0)");
  tracePath();
  context.lineTo(points[points.length - 1].x, top + chartHeight);
  context.lineTo(points[0].x, top + chartHeight);
  context.closePath();
  context.fillStyle = area;
  context.fill();

  const stroke = context.createLinearGradient(left, 0, left + chartWidth, 0);
  stroke.addColorStop(0, "rgba(229, 72, 77, .55)");
  stroke.addColorStop(1, "#e5484d");
  tracePath();
  context.strokeStyle = stroke;
  context.lineWidth = 2.5;
  context.lineJoin = "round";
  context.shadowColor = "rgba(229, 72, 77, .25)";
  context.shadowBlur = 6;
  context.stroke();
  context.shadowBlur = 0;

  if (points.length <= 60) {
    points.forEach((item) => {
      context.beginPath();
      context.arc(item.x, item.y, 3, 0, Math.PI * 2);
      context.fillStyle = "#fff";
      context.fill();
      context.strokeStyle = "#e5484d";
      context.lineWidth = 1.8;
      context.stroke();
    });
  }
  context.restore();
}

if (connectButton) connectButton.addEventListener("click", connect);
searchInput.addEventListener("input", render);
grid.addEventListener("click", (event) => {
  const button = event.target.closest("[data-person-id]");
  if (button) selectPerson(button.dataset.personId);
});
rangeTabs.forEach((tab) => tab.addEventListener("click", () => {
  selectedMinutes = Number(tab.dataset.minutes);
  rangeTabs.forEach((item) => item.classList.toggle("active", item === tab));
  loadHistory();
}));
window.addEventListener("resize", () => drawHistory());
window.hrDashboard = { connect, disconnect };
if (config.autoConnect !== false) connect();
