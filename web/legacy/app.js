"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const boot = window.__SWIVD__;
  const localHost = location.protocol === "http:" && ["127.0.0.1", "localhost"].includes(location.hostname);
  if (!localHost || !boot || typeof boot !== "object" || typeof boot.nonce !== "string") {
    const reason = location.protocol === "file:"
      ? "检测到 file:// 直接预览。该方式不会加载本地 API；请先启动 serve，再访问 127.0.0.1。"
      : "没有取得本地服务启动信息。请确认 serve 正在运行，并从 127.0.0.1 或 localhost 访问。";
    $("guardReason").textContent = reason;
    return;
  }

  $("serviceGuard").hidden = true;
  $("appShell").hidden = false;

  let runId = boot.current_run_id || null;
  let catalog = null;
  let readyRun = null;
  let readyAsOf = null;
  let activeLevel = "L1";
  let industryQuery = "";
  let heatMetric = "pe_percentile";
  let historyMetric = "pe";
  let rankField = "return_5d";
  let archiveMetric = "pe";
  let industrySort = { key: null, direction: "default" };
  let stockSort = { key: null, direction: "default" };
  let archiveSort = { key: null, direction: "default" };
  const readOnly = boot.read_only === true;
  const apiBase = boot.api_base === "/legacy" ? "/legacy" : "";
  let currentShard = null;
  let archiveCatalog = null;
  let lastJobPercent = 0;
  let trendRequest = 0;
  let memberRequest = 0;
  let archiveRequest = 0;
  let catalogRequest = 0;
  let activeJobId = null;
  let pollGeneration = 0;
  let pollFailures = 0;
  let pollTimer = null;
  let recoveryMode = "job";
  const maxPollRetries = 5;
  const shardCache = new Map();
  const archiveCache = new Map();

  const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[character]));
  const number = value => {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const fmt = (value, digits = 2) => {
    const parsed = number(value);
    return parsed === null ? "—" : parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits });
  };
  const fmtPercentile = value => number(value) === null ? "—" : `${Number(value).toFixed(1)}%`;
  const fmtMarketValue = value => number(value) === null ? "—" : fmt(Number(value) / 10000);
  const fmtDividend = value => number(value) === null ? "—" : `${fmt(value)}%`;
  const fmtCoverage = value => number(value) === null ? "—" : `${fmt(Number(value) * 100)}%`;
  const compareTableValues = (left, right, ascending) => {
    const missing = value => value === null || value === undefined || value === ""
      || (typeof value === "number" && !Number.isFinite(value));
    if (missing(left) && missing(right)) return 0;
    if (missing(left)) return 1;
    if (missing(right)) return -1;
    const a = number(left), b = number(right);
    const result = a !== null && b !== null
      ? a - b
      : String(left).localeCompare(String(right), "zh-CN");
    return ascending ? result : -result;
  };
  const fmtReturn = value => {
    const parsed = number(value);
    if (parsed === null) return "—";
    const percent = parsed * 100;
    return `${percent > 0 ? "+" : ""}${percent.toFixed(2)}%`;
  };
  const positionClass = value => {
    const parsed = number(value);
    if (parsed === null || parsed < 0 || parsed > 100) return "na";
    return `p${Math.min(4, Math.floor(parsed / 20))}`;
  };
  const positionLabel = label => typeof label === "string" && label.trim() ? label : "位置标签不可用";
  const returnClass = value => number(value) === null ? "flat" : Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "flat";
  const targetReasonLabels = Object.freeze({
    PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF: "北京时间尚未到 18:30，按规则选择前一交易日",
    CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF: "北京时间已到 18:30，选择今天这个开市日",
    LATEST_OPEN_DAY_NON_TRADING_DATE: "北京时间今天休市，选择最近交易日"
  });
  const identityFields = Object.freeze([
    "industry_uid",
    "catalog_index_code",
    "quote_index_code",
    "member_index_code",
    "current_index_code",
    "identity_state",
    "identity_rule_version",
    "identity_reason_disclosure"
  ]);
  const identityRecord = source => {
    if (!source || typeof source !== "object") return null;
    const candidate = source.identity && typeof source.identity === "object"
      ? source.identity
      : source;
    if (!identityFields.every(field => typeof candidate[field] === "string" && candidate[field].trim())) {
      return null;
    }
    const expectedReason = candidate.identity_state === "DIRECT"
      ? "NOT_APPLICABLE_DIRECT_IDENTITY"
      : "UNKNOWN_UPSTREAM_INTERNAL_CAUSE";
    if (candidate.identity_reason_disclosure !== expectedReason) return null;
    return candidate;
  };
  const identityDisclosure = identity => {
    if (!identity) return "旧快照未提供行业身份字段";
    const base = `UID=${identity.industry_uid} · 目录=${identity.catalog_index_code} · 行情=${identity.quote_index_code} · 成员=${identity.member_index_code} · 当前解析=${identity.current_index_code} · 规则=${identity.identity_rule_version}`;
    if (identity.identity_state !== "DIRECT") {
      return `${base} · 上游内部原因未知；本地仅做证据门连接`;
    }
    return base;
  };
  const displayDate = value => {
    const rendered = String(value || "");
    return /^\d{8}$/.test(rendered)
      ? `${rendered.slice(0, 4)}-${rendered.slice(4, 6)}-${rendered.slice(6, 8)}`
      : "日期不可用";
  };
  const targetSelectionText = job => {
    const selection = job && job.target_selection;
    if (!selection || typeof selection !== "object") return "";
    const targetDate = displayDate(job.as_of);
    if (String(selection.as_of || "") !== String(job.as_of || "")) {
      return `目标截止日 ${targetDate} · 目标日期选择记录不一致`;
    }
    const hasReason = Object.prototype.hasOwnProperty.call(targetReasonLabels, selection.reason_code);
    const reason = hasReason ? targetReasonLabels[selection.reason_code] : null;
    if (!reason || selection.timezone !== "Asia/Shanghai" || selection.cutoff !== "18:30:00") {
      return `目标截止日 ${targetDate} · 选择原因不可验证`;
    }
    return `目标截止日 ${targetDate} · ${reason}`;
  };
  const median = values => {
    const sorted = values.map(number).filter(value => value !== null).sort((a, b) => a - b);
    if (!sorted.length) return null;
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  };

  async function api(url, options = {}) {
    if (readOnly && options.method && options.method !== "GET") throw new Error("READ_ONLY_ARCHIVE");
    const response = await fetch(apiBase + url, { cache: "no-store", ...options });
    let body;
    try { body = await response.json(); } catch (_error) { throw new Error("INVALID_JSON_RESPONSE"); }
    if (!response.ok) {
      const error = new Error(body.error || "REQUEST_FAILED");
      error.httpStatus = response.status;
      throw error;
    }
    return body;
  }

  function filteredRows() {
    if (!catalog || !Array.isArray(catalog.industries)) return [];
    return catalog.industries.filter(row => {
      if (row.level !== activeLevel) return false;
      if (!industryQuery) return true;
      const identity = identityRecord(row);
      return [
        row.industry_name,
        row.index_code,
        ...(identity ? identityFields.map(field => identity[field]) : [])
      ].some(value => String(value || "").toLowerCase().includes(industryQuery));
    });
  }

  function allLevelRows() {
    if (!catalog || !Array.isArray(catalog.industries)) return [];
    return catalog.industries.filter(row => row.level === activeLevel);
  }

  function emptyChart(targetId, message) {
    const target = $(targetId);
    target.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "chart-empty";
    empty.textContent = message;
    target.appendChild(empty);
  }

  function svgRoot(targetId, viewBox = "0 0 960 420") {
    const target = $(targetId);
    target.replaceChildren();
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", viewBox);
    svg.setAttribute("role", "img");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    target.appendChild(svg);
    return svg;
  }

  function svgNode(svg, tag, attributes = {}, text = "") {
    const node = document.createElementNS(svg.namespaceURI, tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text) node.textContent = text;
    svg.appendChild(node);
    return node;
  }

  function colorForPercentile(value) {
    return { p0: "#1a9850", p1: "#66bd63", p2: "#a6d96a", p3: "#fdae61", p4: "#d73027", na: "#6e7681" }[positionClass(value)];
  }

  function activateTab(tabName) {
    const tabs = [...document.querySelectorAll(".tab-btn[data-tab]")];
    tabs.forEach(button => {
      const active = button.dataset.tab === tabName;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll(".tab-content[data-panel]").forEach(panel => {
      const active = panel.dataset.panel === tabName;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    });
    if (tabName === "trend") void renderTrend();
    if (tabName === "constituents") void renderConstituents();
    if (tabName === "archive") void renderArchive();
  }

  function bindTabs() {
    const tabs = [...document.querySelectorAll(".tab-btn[data-tab]")];
    tabs.forEach((button, index) => {
      button.addEventListener("click", () => activateTab(button.dataset.tab));
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let next = index;
        if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        activateTab(tabs[next].dataset.tab);
        tabs[next].focus();
      });
    });
  }

  function setPressed(selector, selected, datasetKey) {
    document.querySelectorAll(selector).forEach(button => {
      const active = button.dataset[datasetKey] === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setLevel(level) {
    if (!["L1", "L2", "L3"].includes(level)) return;
    activeLevel = level;
    setPressed("[data-level]", activeLevel, "level");
    currentShard = null;
    fillIndustrySelects();
    renderAll();
  }

  function renderSummaryCards() {
    const rows = filteredRows();
    const published = rows.filter(row => Number(row.is_pub) === 1).length;
    const available = rows.filter(row => number(row.pe) !== null || number(row.pb) !== null).length;
    const members = rows.reduce((sum, row) => sum + (number(row.member_row_count) || 0), 0);
    const medianPe = median(rows.map(row => row.pe_percentile));
    const cards = [
      ["行业目录", rows.length, `${activeLevel} · 当前筛选`],
      ["官方发布", published, `未发布 ${rows.length - published}`],
      ["估值可用", available, "官方 sw_daily"],
      ["成员记录", members, "目标日生命周期快照"],
      ["PE 百分位中位数", fmtPercentile(medianPe), "横截面描述"]
    ];
    $("summaryCards").innerHTML = cards.map(([label, value, sub]) => `<article class="summary-card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></article>`).join("");
  }

  function renderOverviewBars() {
    const rows = filteredRows().filter(row => number(row.pe_percentile) !== null)
      .sort((a, b) => Number(a.pe_percentile) - Number(b.pe_percentile));
    if (!rows.length) { emptyChart("overviewBars", "当前快照目录没有可用的 PE 百分位摘要。"); return; }
    const shown = rows;
    const W = 820, H = Math.max(400, shown.length * 22 + 48), left = 142, right = 46, top = 20, bottom = 28;
    const svg = svgRoot("overviewBars", `0 0 ${W} ${H}`);
    svg.setAttribute("aria-label", `${activeLevel} 行业 PE 历史百分位横向对比`);
    [0, 20, 40, 60, 80, 100].forEach(value => {
      const x = left + value / 100 * (W - left - right);
      svgNode(svg, "line", { x1: x, y1: top, x2: x, y2: H - bottom, class: "grid-line" });
      svgNode(svg, "text", { x, y: H - 8, "text-anchor": "middle", class: "svg-label" }, `${value}%`);
    });
    const rowHeight = (H - top - bottom) / shown.length;
    shown.forEach((row, index) => {
      const value = Number(row.pe_percentile);
      const y = top + index * rowHeight + rowHeight * .16;
      const height = Math.max(5, rowHeight * .62);
      svgNode(svg, "text", { x: left - 9, y: y + height * .75, "text-anchor": "end", class: "svg-label" }, String(row.industry_name).slice(0, 10));
      svgNode(svg, "rect", { x: left, y, width: value / 100 * (W - left - right), height, rx: 3, fill: colorForPercentile(value) });
      svgNode(svg, "text", { x: Math.min(W - right + 4, left + value / 100 * (W - left - right) + 5), y: y + height * .75, class: "svg-label" }, value.toFixed(1));
    });
  }

  function arcPath(cx, cy, radius, startAngle, endAngle) {
    const start = { x: cx + radius * Math.cos(startAngle), y: cy + radius * Math.sin(startAngle) };
    const end = { x: cx + radius * Math.cos(endAngle), y: cy + radius * Math.sin(endAngle) };
    const large = endAngle - startAngle > Math.PI ? 1 : 0;
    return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${large} 1 ${end.x} ${end.y}`;
  }

  function renderDistribution() {
    const values = filteredRows().map(row => number(row.pe_percentile)).filter(value => value !== null);
    if (!values.length) { emptyChart("distributionChart", "当前层级没有可用的 PE 百分位摘要。"); return; }
    const counts = [0, 0, 0, 0, 0];
    values.forEach(value => counts[Math.min(4, Math.floor(value / 20))] += 1);
    const svg = svgRoot("distributionChart", "0 0 620 330");
    svg.setAttribute("aria-label", "PE 历史百分位五档分布");
    const colors = ["#1a9850", "#66bd63", "#a6d96a", "#fdae61", "#d73027"];
    const labels = ["0–20", "20–40", "40–60", "60–80", "80–100"];
    let angle = -Math.PI / 2;
    counts.forEach((count, index) => {
      if (!count) return;
      const next = angle + count / values.length * Math.PI * 2;
      if (count === values.length) {
        svgNode(svg, "circle", { cx: 190, cy: 160, r: 105, fill: "none", stroke: colors[index], "stroke-width": 42 });
      } else {
        svgNode(svg, "path", { d: arcPath(190, 160, 105, angle, next), fill: "none", stroke: colors[index], "stroke-width": 42 });
      }
      angle = next;
    });
    svgNode(svg, "text", { x: 190, y: 154, "text-anchor": "middle", class: "svg-title" }, String(values.length));
    svgNode(svg, "text", { x: 190, y: 174, "text-anchor": "middle", class: "svg-label" }, "有效行业");
    counts.forEach((count, index) => {
      const y = 74 + index * 42;
      svgNode(svg, "rect", { x: 355, y: y - 12, width: 14, height: 14, rx: 3, fill: colors[index] });
      svgNode(svg, "text", { x: 381, y, class: "svg-label" }, `${labels[index]}：${count}（${(count / values.length * 100).toFixed(1)}%）`);
    });
  }

  function polygonPoints(values, cx, cy, radius) {
    return values.map((value, index) => {
      const angle = -Math.PI / 2 + index / values.length * Math.PI * 2;
      const r = radius * Math.max(0, Math.min(100, value)) / 100;
      return `${(cx + Math.cos(angle) * r).toFixed(2)},${(cy + Math.sin(angle) * r).toFixed(2)}`;
    }).join(" ");
  }

  function renderRadar() {
    const rows = filteredRows().filter(row => number(row.pe_percentile) !== null && number(row.pb_percentile) !== null)
      .sort((a, b) => (number(b.member_row_count) || 0) - (number(a.member_row_count) || 0)).slice(0, 8);
    if (rows.length < 3) { emptyChart("radarChart", "至少需要 3 个同时具备 PE/PB 百分位的行业。"); return; }
    const svg = svgRoot("radarChart", "0 0 920 390");
    svg.setAttribute("aria-label", "PE 与 PB 历史百分位轮廓图");
    const cx = 410, cy = 198, radius = 142, axes = rows.length;
    [20, 40, 60, 80, 100].forEach(value => {
      const points = Array.from({ length: axes }, (_unused, index) => {
        const angle = -Math.PI / 2 + index / axes * Math.PI * 2;
        const r = radius * value / 100;
        return `${cx + Math.cos(angle) * r},${cy + Math.sin(angle) * r}`;
      }).join(" ");
      svgNode(svg, "polygon", { points, class: "radar-grid" });
    });
    rows.forEach((row, index) => {
      const angle = -Math.PI / 2 + index / axes * Math.PI * 2;
      const x = cx + Math.cos(angle) * radius;
      const y = cy + Math.sin(angle) * radius;
      svgNode(svg, "line", { x1: cx, y1: cy, x2: x, y2: y, class: "grid-line" });
      const labelX = cx + Math.cos(angle) * (radius + 24);
      const labelY = cy + Math.sin(angle) * (radius + 24);
      svgNode(svg, "text", { x: labelX, y: labelY, "text-anchor": Math.abs(Math.cos(angle)) < .2 ? "middle" : Math.cos(angle) > 0 ? "start" : "end", class: "svg-label" }, String(row.industry_name).slice(0, 8));
    });
    svgNode(svg, "polygon", { points: polygonPoints(rows.map(row => Number(row.pe_percentile)), cx, cy, radius), class: "radar-pe" });
    svgNode(svg, "polygon", { points: polygonPoints(rows.map(row => Number(row.pb_percentile)), cx, cy, radius), class: "radar-pb" });
    svgNode(svg, "line", { x1: 660, y1: 150, x2: 696, y2: 150, stroke: "#58a6ff", "stroke-width": 3 });
    svgNode(svg, "text", { x: 708, y: 154, class: "svg-label" }, "PE 历史百分位");
    svgNode(svg, "line", { x1: 660, y1: 186, x2: 696, y2: 186, stroke: "#3fb950", "stroke-width": 3 });
    svgNode(svg, "text", { x: 708, y: 190, class: "svg-label" }, "PB 历史百分位");
    svgNode(svg, "text", { x: 660, y: 232, class: "svg-label" }, `展示成员记录较多的 ${rows.length} 个有效行业`);
  }

  function renderHeatmap() {
    const rows = filteredRows().slice().sort((a, b) => {
      const av = number(a[heatMetric]), bv = number(b[heatMetric]);
      if (av === null) return 1;
      if (bv === null) return -1;
      return av - bv;
    });
    const target = $("heatmapGrid");
    target.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "没有匹配的行业。";
      target.appendChild(empty);
      return;
    }
    rows.forEach(row => {
      const value = row[heatMetric];
      const label = row[heatMetric.startsWith("pe") ? "pe_history_label" : "pb_history_label"];
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = `heat-tile ${positionClass(value)}`;
      tile.setAttribute("aria-label", `${row.industry_name} ${heatMetric.startsWith("pe") ? "PE" : "PB"} 百分位 ${fmtPercentile(value)}`);
      const validN = row[heatMetric.startsWith("pe") ? "pe_valid_count" : "pb_valid_count"];
      tile.innerHTML = `<div class="name">${esc(row.industry_name)}</div><div class="code">${esc(row.index_code)}${Number(row.is_pub) === 1 ? "" : " · 官方未发布"}</div><div class="value">${esc(fmtPercentile(value))}</div><div class="meta">${esc(positionLabel(label))} · N=${esc(fmt(validN, 0))}</div>`;
      tile.addEventListener("click", () => openConstituent(row));
      target.appendChild(tile);
    });
  }

  function renderRanking() {
    const rows = filteredRows().filter(row => number(row[rankField]) !== null)
      .sort((a, b) => Number(b[rankField]) - Number(a[rankField]));
    const target = $("rankingList");
    target.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "所选区间没有可用的官方指数收益。";
      target.appendChild(empty);
      return;
    }
    const maxAbs = Math.max(...rows.map(row => Math.abs(Number(row[rankField]))), .000001);
    rows.forEach((row, index) => {
      const value = Number(row[rankField]);
      const line = document.createElement("div");
      line.className = "rank-row";
      const no = document.createElement("div"); no.className = "rank-no"; no.textContent = String(index + 1).padStart(2, "0");
      const name = document.createElement("div"); name.className = "rank-name"; name.textContent = row.industry_name;
      const track = document.createElement("div"); track.className = "rank-track";
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 100 12");
      svgNode(svg, "line", { x1: 50, y1: 1, x2: 50, y2: 11, stroke: "#484f58", "stroke-width": 1 });
      const width = Math.abs(value) / maxAbs * 48;
      svgNode(svg, "rect", { x: value >= 0 ? 50 : 50 - width, y: 3, width, height: 6, rx: 2, fill: value >= 0 ? "#f85149" : "#3fb950" });
      track.appendChild(svg);
      const amount = document.createElement("div"); amount.className = `rank-value ${returnClass(value)}`; amount.textContent = fmtReturn(value);
      line.append(no, name, track, amount);
      target.appendChild(line);
    });
  }

  function renderScatter() {
    const rows = filteredRows().filter(row => number(row.pe) !== null && number(row.pb) !== null && Number(row.pe) > 0 && Number(row.pb) > 0);
    if (!rows.length) { emptyChart("scatterChart", "没有同时具备正数 PE 与 PB 的匹配行业。"); return; }
    const svg = svgRoot("scatterChart", "0 0 960 420");
    svg.setAttribute("aria-label", "行业当前 PE 与 PB 散点图");
    const W = 960, H = 420, left = 68, right = 30, top = 28, bottom = 52;
    const maxPe = Math.max(...rows.map(row => Number(row.pe))) * 1.06;
    const maxPb = Math.max(...rows.map(row => Number(row.pb))) * 1.08;
    const medPe = median(rows.map(row => row.pe));
    const medPb = median(rows.map(row => row.pb));
    const x = value => left + Number(value) / maxPe * (W - left - right);
    const y = value => top + (maxPb - Number(value)) / maxPb * (H - top - bottom);
    for (let index = 0; index <= 4; index += 1) {
      const gx = left + index / 4 * (W - left - right);
      const gy = top + index / 4 * (H - top - bottom);
      svgNode(svg, "line", { x1: gx, y1: top, x2: gx, y2: H - bottom, class: "grid-line" });
      svgNode(svg, "line", { x1: left, y1: gy, x2: W - right, y2: gy, class: "grid-line" });
      svgNode(svg, "text", { x: gx, y: H - 27, "text-anchor": "middle", class: "svg-label" }, (maxPe * index / 4).toFixed(1));
      svgNode(svg, "text", { x: left - 9, y: gy + 4, "text-anchor": "end", class: "svg-label" }, (maxPb * (4 - index) / 4).toFixed(1));
    }
    if (medPe !== null) svgNode(svg, "line", { x1: x(medPe), y1: top, x2: x(medPe), y2: H - bottom, class: "median-line" });
    if (medPb !== null) svgNode(svg, "line", { x1: left, y1: y(medPb), x2: W - right, y2: y(medPb), class: "median-line" });
    svgNode(svg, "text", { x: W - right, y: H - 8, "text-anchor": "end", class: "svg-title" }, "PE →");
    svgNode(svg, "text", { x: left, y: 16, class: "svg-title" }, "PB ↑");
    const maxMembers = Math.max(...rows.map(row => number(row.member_row_count) || 0), 1);
    rows.forEach(row => {
      const point = svgNode(svg, "circle", {
        cx: x(row.pe), cy: y(row.pb), r: 4 + Math.sqrt((number(row.member_row_count) || 0) / maxMembers) * 6,
        fill: colorForPercentile(row.pe_percentile), class: "scatter-point", tabindex: 0
      });
      const title = document.createElementNS(svg.namespaceURI, "title");
      title.textContent = `${row.industry_name} · PE ${fmt(row.pe)} · PB ${fmt(row.pb)} · PE历史百分位 ${fmtPercentile(row.pe_percentile)}`;
      point.setAttribute("role", "button");
      point.setAttribute("aria-label", `${title.textContent}；查看行业成分`);
      point.appendChild(title);
      point.addEventListener("click", () => openConstituent(row));
      point.addEventListener("keydown", event => {
        if (!["Enter", " "].includes(event.key)) return;
        event.preventDefault();
        openConstituent(row);
      });
    });
  }

  function positionPill(value, label) {
    return `<span class="position-pill ${positionClass(value)}">${esc(number(value) === null ? "不可用" : positionLabel(label))}</span>`;
  }

  function nextLegacySort(sort, key) {
    return sort.key !== key || sort.direction === "default" ? {key, direction: "asc"}
      : sort.direction === "asc" ? {key, direction: "desc"} : {key: null, direction: "default"};
  }

  function exactLegacyCompare(left, right, direction, numeric) {
    const parse = value => {
      if (value === null || value === undefined || value === "") return null;
      const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(String(value));
      return match ? {value: BigInt((match[1] === "-" ? "-" : "") + match[2] + (match[3] || "")), scale: (match[3] || "").length} : null;
    };
    const a = numeric ? parse(left) : left, b = numeric ? parse(right) : right;
    const empty = value => value === null || value === undefined || value === "";
    if (empty(a) || empty(b)) return empty(a) === empty(b) ? 0 : empty(a) ? 1 : -1;
    let x = a, y = b;
    if (numeric) { const scale = Math.max(a.scale,b.scale); x = a.value * 10n ** BigInt(scale-a.scale); y = b.value * 10n ** BigInt(scale-b.scale); }
    else { x = String(x); y = String(y); }
    const result = numeric ? x < y ? -1 : x > y ? 1 : 0 : x.localeCompare(y,"zh-CN");
    return direction === "desc" ? -result : result;
  }

  function legacyTable(container, rows, columns, sort, update, renderCell, bodyId) {
    const table = document.createElement("table"), head = document.createElement("thead"), header = document.createElement("tr"), body = document.createElement("tbody");
    if (bodyId) body.id = bodyId;
    const selected = columns.find(column => column[0] === sort.key);
    const ordered = rows.map((row,index) => ({row,index}));
    const identity = row => String(row.uid || row.index_code || "") + "/" + String(row.ts_code || "");
    if (selected && sort.direction !== "default") ordered.sort((a,b) => exactLegacyCompare(a.row[sort.key],b.row[sort.key],sort.direction,selected[2] !== false) || (identity(a.row) < identity(b.row) ? -1 : identity(a.row) > identity(b.row) ? 1 : a.index-b.index));
    columns.forEach(([key,label]) => {
      const th = document.createElement("th"), button = document.createElement("button");
      th.scope = "col"; th.setAttribute("aria-sort",sort.key === key ? sort.direction === "asc" ? "ascending" : "descending" : "none");
      button.type = "button"; button.className = "table-sort"; button.dataset.sort = key;
      button.textContent = label + (sort.key === key ? sort.direction === "asc" ? " ↑" : " ↓" : " ↕");
      button.setAttribute("aria-label",label + "，默认、升序、降序切换");
      button.addEventListener("click",() => { update(nextLegacySort(sort,key)); container.querySelector('[data-sort="'+key+'"]')?.focus(); });
      th.append(button); header.append(th);
    });
    ordered.forEach(({row}) => { const tr=document.createElement("tr"); columns.forEach(([key]) => { const td=document.createElement("td"); td.textContent=renderCell(row,key); tr.append(td); }); body.append(tr); });
    head.append(header); table.append(head,body); container.replaceChildren(table);
  }

  function renderIndustryTable() {
    const columns = [["industry_name","行业",false],["index_code","当前指数代码",false],["identity_state","行业身份",false],["is_pub","发布"],["pe","官方 PE"],["pe_percentile","PE 历史百分位"],["pb","官方 PB"],["pb_percentile","PB 历史百分位"],["return_5d","5D"],["return_mtd","MTD"],["return_ytd","YTD"],["member_row_count","成员行数"],["valuation_state","状态",false]];
    legacyTable($("industryTable"),filteredRows(),columns,industrySort,sort => {industrySort=sort;renderIndustryTable();},(row,key) => {
      if (key === "is_pub") return Number(row[key]) === 1 ? "官方发布" : "官方未发布";
      if (["pe","pb"].includes(key)) return fmt(row[key]);
      if (key.includes("percentile")) return fmtPercentile(row[key]);
      if (key.startsWith("return_")) return fmtReturn(row[key]);
      return row[key] ?? "—";
    });
  }

  function fillSelect(selectId, rows) {
    const select = $(selectId);
    const previous = select.value;
    select.replaceChildren();
    rows.slice().sort((a, b) => String(a.industry_name).localeCompare(String(b.industry_name), "zh-CN")).forEach(row => {
      const option = document.createElement("option");
      const identity = identityRecord(row);
      option.value = row.index_code;
      option.textContent = `${row.industry_name} · ${row.index_code}${identity ? ` · ${identity.identity_state}` : ""}${Number(row.is_pub) === 1 ? "" : " · 未发布"}`;
      select.appendChild(option);
    });
    if (rows.some(row => row.index_code === previous)) select.value = previous;
  }

  function fillIndustrySelects() {
    const rows = filteredRows();
    fillSelect("trendIndustry", rows);
    fillSelect("memberIndustry", rows);
  }

  async function loadShard(code) {
    if (!runId || !code) return null;
    const key = `${runId}/${activeLevel}/${code}`;
    if (!shardCache.has(key)) {
      shardCache.set(key, api(`/api/v1/snapshots/${encodeURIComponent(runId)}/industries/${activeLevel}/${encodeURIComponent(code)}`));
    }
    try { return await shardCache.get(key); }
    catch (error) { shardCache.delete(key); throw error; }
  }

  function renderLine(targetId, statsId, rows, metric, label) {
    const series = (Array.isArray(rows) ? rows : []).slice()
      .sort((a, b) => String(a.trade_date).localeCompare(String(b.trade_date)));
    const validValue = row => number(row[metric]) !== null && Number(row[metric]) > 0;
    const values = series.filter(validValue);
    const missingCount = series.length - values.length;
    if (!values.length) {
      emptyChart(targetId, "该行业在所选指标下没有有限且大于零的官方观测。");
      $(statsId).textContent = `有效样本 0 · 缺失或非正观测 ${missingCount}`;
      return;
    }
    const svg = svgRoot(targetId, "0 0 960 420");
    svg.setAttribute("aria-label", `${label} ${metric.toUpperCase()} 历史走势`);
    const W = 960, H = 420, left = 68, right = 24, top = 30, bottom = 54;
    const nums = values.map(row => Number(row[metric]));
    let min = Math.min(...nums), max = Math.max(...nums);
    if (min === max) { min = Math.max(0, min * .98); max = max * 1.02 || 1; }
    const pad = (max - min) * .04;
    min = Math.max(0, min - pad); max += pad;
    const x = index => series.length === 1 ? (left + W - right) / 2 : left + index / (series.length - 1) * (W - left - right);
    const y = value => top + (max - value) / (max - min) * (H - top - bottom);
    for (let index = 0; index <= 4; index += 1) {
      const gy = top + index / 4 * (H - top - bottom);
      svgNode(svg, "line", { x1: left, y1: gy, x2: W - right, y2: gy, class: "grid-line" });
      svgNode(svg, "text", { x: left - 10, y: gy + 4, "text-anchor": "end", class: "svg-label" }, (max - index / 4 * (max - min)).toFixed(2));
    }
    const segments = [];
    let segment = [];
    series.forEach((row, index) => {
      if (!validValue(row)) {
        if (segment.length) segments.push(segment);
        segment = [];
      } else {
        segment.push({ x: x(index), y: y(Number(row[metric])) });
      }
    });
    if (segment.length) segments.push(segment);
    segments.forEach(points => {
      if (points.length === 1) {
        svgNode(svg, "circle", { cx: points[0].x, cy: points[0].y, r: 4, class: "series-point" });
        return;
      }
      const line = points.map((point, index) => `${index ? "L" : "M"} ${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
      const last = points[points.length - 1];
      const area = `${line} L ${last.x},${H - bottom} L ${points[0].x},${H - bottom} Z`;
      svgNode(svg, "path", { d: area, class: "series-area" });
      svgNode(svg, "path", { d: line, class: "series-line" });
    });
    const lastSegment = segments[segments.length - 1];
    const lastPoint = lastSegment[lastSegment.length - 1];
    if (lastSegment.length > 1) svgNode(svg, "circle", { cx: lastPoint.x, cy: lastPoint.y, r: 5, class: "series-point" });
    if (series.length === 1) {
      svgNode(svg, "text", { x: x(0), y: H - 20, "text-anchor": "middle", class: "svg-label" }, series[0].trade_date);
    } else {
      svgNode(svg, "text", { x: left, y: H - 20, class: "svg-label" }, series[0].trade_date);
      svgNode(svg, "text", { x: W - right, y: H - 20, "text-anchor": "end", class: "svg-label" }, series[series.length - 1].trade_date);
    }
    $(statsId).innerHTML = `<span>行业 <b>${esc(label)}</b></span><span>指标 <b>${esc(metric.toUpperCase())}</b></span><span>有效样本 <b>${values.length}</b></span><span>缺失或非正观测 <b>${missingCount}</b>（断线展示）</span><span>日期轴 <b>${esc(series[0].trade_date)}–${esc(series[series.length - 1].trade_date)}</b>（原始交易日等距）</span><span>末次有效日 <b>${esc(values[values.length - 1].trade_date)}</b></span><span>末次有效值 <b>${esc(fmt(nums[nums.length - 1]))}</b></span>`;
  }

  async function renderTrend() {
    const request = ++trendRequest;
    const code = $("trendIndustry").value;
    if (!runId || !code) { emptyChart("trendChart", "尚无可浏览的已验证快照。"); $("trendStats").textContent = ""; return; }
    emptyChart("trendChart", "正在载入行业历史…");
    $("trendStats").textContent = "";
    try {
      const data = await loadShard(code);
      if (request !== trendRequest || !data) return;
      renderLine("trendChart", "trendStats", data.history, historyMetric, data.industry.industry_name || code);
    } catch (error) {
      if (request === trendRequest) {
        emptyChart("trendChart", `历史载入失败：${error.message}`);
        $("trendStats").textContent = "载入失败；未显示上一行业统计。";
      }
    }
  }

  function peerText(row, field) {
    const state = row[`${field}_percentile_state`];
    const validN = row[`${field}_valid_n`];
    const sample = `N=${fmt(validN, 0)} / 成员${fmt(row[`${field}_member_count`], 0)} · 覆盖率${fmtCoverage(row[`${field}_coverage`])}`;
    if (state === "OK") return `${fmt(row[`${field}_percentile_le`])}% · ${sample} · 并列${fmt(row[`${field}_tie_count`], 0)}`;
    return `${state || "不可用"} · ${sample}`;
  }

  function renderStockRows() {
    if (!currentShard) { $("stocks").replaceChildren(); return; }
    const query = $("stockSearch").value.trim().toLowerCase();
    const rows = currentShard.constituents.filter(row => !query || String(row.stock_name || "").toLowerCase().includes(query) || String(row.ts_code || "").toLowerCase().includes(query));
    const container = $("stocks").closest(".data-table-wrap");
    const columns = [["stock_name","股票",false],["ts_code","代码",false],["pe_ttm","PE_TTM"],["pe_ttm_percentile_le","PE_TTM 百分位与覆盖"],["pb","PB"],["pb_percentile_le","PB 百分位与覆盖"],["pe","PE"],["ps_ttm","PS_TTM"],["dv_ttm","股息率（%）"],["total_mv","总市值（亿元）"],["circ_mv","流通市值（亿元）"],["membership_state","成员状态",false],["valuation_state","估值状态",false]];
    legacyTable(container,rows,columns,stockSort,sort=>{stockSort=sort;renderStockRows();},(row,key)=>{
      if (key === "pe_ttm_percentile_le") return peerText(row,"pe_ttm");
      if (key === "pb_percentile_le") return peerText(row,"pb");
      if (["pe_ttm","pb","pe","ps_ttm"].includes(key)) return fmt(row[key]);
      if (key === "dv_ttm") return fmtDividend(row[key]);
      if (["total_mv","circ_mv"].includes(key)) return fmtMarketValue(row[key]);
      return row[key] ?? "—";
    },"stocks");
  }

  async function renderConstituents() {
    const request = ++memberRequest;
    const code = $("memberIndustry").value;
    if (!runId || !code) {
      $("constituentEmpty").hidden = false;
      $("constituentDetail").hidden = true;
      return;
    }
    $("constituentEmpty").hidden = false;
    $("constituentEmpty").querySelector("h2").textContent = "正在载入行业成分…";
    $("constituentDetail").hidden = true;
    try {
      const data = await loadShard(code);
      if (request !== memberRequest || !data) return;
      currentShard = data;
      const row = allLevelRows().find(item => item.index_code === code) || data.industry;
      const identity = identityRecord(data) || identityRecord(row);
      $("constituentEmpty").hidden = true;
      $("constituentDetail").hidden = false;
      $("breadcrumb").textContent = `SW2021 ${activeLevel} · ${code}${identity ? ` · ${identity.industry_uid}` : ""}${row.parent_code ? ` · 父级 ${row.parent_code}` : ""}`;
      $("industryName").textContent = data.industry.industry_name || row.industry_name || code;
      $("published").textContent = Number(row.is_pub) === 1 ? "官方发布" : "官方未发布";
      $("published").classList.toggle("published", Number(row.is_pub) === 1);
      const industry = data.industry;
      const cards = [
        ["官方 PE", fmt(industry.pe), "sw_daily"], ["PE 历史百分位", fmtPercentile(industry.pe_percentile_le), `N=${fmt(industry.pe_valid_count, 0)}`],
        ["官方 PB", fmt(industry.pb), "sw_daily"], ["PB 历史百分位", fmtPercentile(industry.pb_percentile_le), `N=${fmt(industry.pb_valid_count, 0)}`],
        ["5D 收益", fmtReturn(industry.return_5d), industry.return_5d_status || industry.return_state || "—"],
        ["成分记录", data.constituents.length, "含不可用估值记录"],
        ["行业身份", identity ? identity.identity_state : "旧快照未提供", identityDisclosure(identity)]
      ];
      $("industryMetrics").innerHTML = cards.map(([label, value, sub]) => `<article class="summary-card"><div class="label">${esc(label)}</div><div class="value ${label === "5D 收益" ? returnClass(industry.return_5d) : ""}">${esc(value)}</div><div class="sub">${esc(sub)}</div></article>`).join("");
      renderStockRows();
    } catch (error) {
      if (request !== memberRequest) return;
      $("constituentEmpty").hidden = false;
      $("constituentEmpty").querySelector("h2").textContent = "行业成分载入失败";
      $("constituentEmpty").querySelector("p").textContent = error.message;
    }
  }

  function openConstituent(row) {
    setLevel(row.level);
    $("memberIndustry").value = row.index_code;
    activateTab("constituents");
    void renderConstituents();
  }

  function renderArchiveTable() {
    const rows = archiveCatalog && Array.isArray(archiveCatalog.industries) ? archiveCatalog.industries : [];
    const columns = [["industry_name","行业",false],["index_code","代码",false],["as_of","档案末日",false],["pe","PE"],["pe_percentile","PE 百分位"],["pb","PB"],["pb_percentile","PB 百分位"],["valuation_state","状态",false]];
    legacyTable($("archiveTable"),rows,columns,archiveSort,sort=>{archiveSort=sort;renderArchiveTable();},(row,key)=>key.includes("percentile") ? fmtPercentile(row[key]) : ["pe","pb"].includes(key) ? fmt(row[key]) : row[key] ?? "—");
  }

  async function loadArchiveCatalog() {
    if (!runId) return null;
    if (archiveCatalog && archiveCatalog.__run_id === runId) return archiveCatalog;
    const metadata = catalog && catalog.legacy_archive;
    if (!metadata || metadata.taxonomy !== "SW2014" || metadata.level !== "L1" || !metadata.run_id) {
      throw new Error("ARCHIVE_METADATA_INVALID");
    }
    const body = await api(`/api/v1/snapshots/${encodeURIComponent(runId)}/archive/sw2014/catalog`);
    if (body.source_run_id !== metadata.run_id || body.taxonomy !== metadata.taxonomy || body.level !== metadata.level) {
      throw new Error("ARCHIVE_RESPONSE_IDENTITY_MISMATCH");
    }
    archiveCatalog = { ...body, __run_id: runId };
    const select = $("archiveIndustry");
    const previous = select.value;
    select.replaceChildren();
    (archiveCatalog.industries || []).forEach(row => {
      const option = document.createElement("option");
      option.value = row.index_code;
      option.textContent = `${row.industry_name} · ${row.index_code}`;
      select.appendChild(option);
    });
    if ((archiveCatalog.industries || []).some(row => row.index_code === previous)) select.value = previous;
    renderArchiveTable();
    return archiveCatalog;
  }

  async function renderArchive() {
    const request = ++archiveRequest;
    if (!runId) { emptyChart("archiveChart", "尚无可浏览的已验证 v2 快照。"); $("archiveStats").textContent = ""; return; }
    try {
      await loadArchiveCatalog();
      const code = $("archiveIndustry").value;
      if (!code) { emptyChart("archiveChart", "SW2014 档案目录为空。"); return; }
      const key = `${runId}/${code}`;
      if (!archiveCache.has(key)) archiveCache.set(key, api(`/api/v1/snapshots/${encodeURIComponent(runId)}/archive/sw2014/industries/${encodeURIComponent(code)}`));
      let detail;
      try { detail = await archiveCache.get(key); }
      catch (error) { archiveCache.delete(key); throw error; }
      if (request !== archiveRequest) return;
      renderLine("archiveChart", "archiveStats", detail.history, archiveMetric, detail.summary.industry_name || code);
    } catch (error) {
      if (request !== archiveRequest) return;
      emptyChart("archiveChart", `SW2014 档案载入失败：${error.message}`);
      $("archiveStats").textContent = "档案保持封存；未回退或跨轴拼接。";
      $("archiveTable").innerHTML = '<div class="chart-empty">档案接口不可用。</div>';
    }
  }

  function renderLevelSummary() {
    const rows = allLevelRows();
    const published = rows.filter(row => Number(row.is_pub) === 1).length;
    $("levelSummary").textContent = catalog ? `${activeLevel} 共 ${rows.length} 个行业 · 官方发布 ${published} · 快照 ${catalog.as_of}` : "尚无 v2 快照";
  }

  function renderAll() {
    renderLevelSummary();
    renderSummaryCards();
    renderOverviewBars();
    renderDistribution();
    renderRadar();
    renderHeatmap();
    renderRanking();
    renderScatter();
    renderIndustryTable();
    if (!$("panel-trend").hidden) void renderTrend();
    if (!$("panel-constituents").hidden) void renderConstituents();
  }

  async function loadCatalog(id, expectedAsOf = null) {
    const request = ++catalogRequest;
    if (!id) {
      archiveCatalog = null;
      archiveCache.clear();
      currentShard = null;
      catalog = null;
      runId = null;
      $("snapshot").textContent = boot.current_snapshot_state === "PUBLICATION_RECOVERY_REQUIRED"
        ? "当前快照正在发布恢复中；可继续跟踪作业，完成后手动载入当前快照"
        : boot.current_snapshot_state === "CURRENT_SNAPSHOT_UNAVAILABLE"
          ? "当前快照暂不可验证；请检查服务状态后重新载入"
          : "尚无 v2 快照；可手动更新或载入历史开市日";
      fillIndustrySelects();
      renderAll();
      return true;
    }
    let nextCatalog;
    try {
      nextCatalog = await api(`/api/v1/snapshots/${encodeURIComponent(id)}/catalog`);
    } catch (error) {
      if (request !== catalogRequest) return false;
      throw error;
    }
    if (request !== catalogRequest) return false;
    if (expectedAsOf && String(nextCatalog.as_of) !== String(expectedAsOf)) {
      throw new Error("SNAPSHOT_AS_OF_MISMATCH");
    }
    archiveCatalog = null;
    archiveCache.clear();
    currentShard = null;
    catalog = nextCatalog;
    runId = id;
    $("snapshot").textContent = `快照 ${id} · ${catalog.as_of}`;
    fillIndustrySelects();
    renderAll();
    return true;
  }

  function setJobBusy(busy) {
    $("update").disabled = busy;
    $("materialize").disabled = busy;
  }

  function clearPollTimer() {
    if (pollTimer !== null) window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function resetProgress() {
    lastJobPercent = 0;
    $("progress").value = 0;
    $("progress").textContent = "0%";
    $("jobPercent").textContent = "0%";
    $("retryProgress").hidden = true;
  }

  function beginTrackingJob(jobId) {
    clearPollTimer();
    if (activeJobId !== jobId) resetProgress();
    activeJobId = jobId;
    pollFailures = 0;
    recoveryMode = "job";
    pollGeneration += 1;
    setJobBusy(true);
    $("job").hidden = false;
    $("retryProgress").hidden = true;
    void poll(jobId, pollGeneration);
  }

  function retryProgressRead(message, retry) {
    pollFailures += 1;
    setJobBusy(true);
    $("job").hidden = false;
    if (pollFailures <= maxPollRetries) {
      const delay = Math.min(8000, 1000 * 2 ** (pollFailures - 1));
      $("jobText").textContent = `${message}；作业状态尚未确认，${delay / 1000} 秒后重试（${pollFailures}/${maxPollRetries}）`;
      clearPollTimer();
      pollTimer = window.setTimeout(retry, delay);
    } else {
      $("jobText").textContent = `${message}；自动重试已暂停。更新可能仍在进行，请重试读取进度。`;
      $("retryProgress").hidden = false;
    }
  }

  async function discoverActiveJob(initialJobs = null) {
    const generation = pollGeneration;
    recoveryMode = "discover";
    try {
      const jobs = initialJobs === null ? (await api("/api/v1/jobs/active")).jobs : initialJobs;
      if (generation !== pollGeneration) return;
      if (!Array.isArray(jobs) || jobs.length > 1
        || jobs.some(job => !job || typeof job.job_id !== "string" || !/^[0-9a-f-]+$/.test(job.job_id))) {
        throw new Error("ACTIVE_JOB_RESPONSE_INVALID");
      }
      pollFailures = 0;
      $("retryProgress").hidden = true;
      if (jobs.length) {
        beginTrackingJob(jobs[0].job_id);
      } else {
        activeJobId = null;
        setJobBusy(false);
        if (initialJobs === null) {
          $("job").hidden = false;
          $("jobText").textContent = "未发现正在运行的网页作业；可载入当前快照确认结果，或手动重新发起更新。";
        }
      }
    } catch (_error) {
      if (generation !== pollGeneration) return;
      retryProgressRead("暂时无法查询正在运行的作业", () => void discoverActiveJob());
    }
  }

  async function startJob(kind, asOf) {
    if (activeJobId) return;
    try {
      const payload = { kind };
      if (asOf) payload.as_of = asOf;
      setJobBusy(true);
      clearPollTimer();
      resetProgress();
      pollGeneration += 1;
      $("job").hidden = false;
      $("jobText").textContent = "正在提交作业…";
      const result = await api("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-SWIVD-Nonce": boot.nonce },
        body: JSON.stringify(payload)
      });
      beginTrackingJob(result.job_id);
    } catch (error) {
      if (error.httpStatus >= 400 && error.httpStatus < 500 && error.httpStatus !== 409) {
        setJobBusy(false);
        $("jobText").textContent = `无法创建作业 · ${error.message}`;
        return;
      }
      $("jobText").textContent = `无法确认作业创建结果 · ${error.message}`;
      // A lost POST response does not prove the server rejected the job.
      // Discover the existing job before permitting another submission.
      pollFailures = 0;
      void discoverActiveJob();
    }
  }

  async function poll(jobId, generation = pollGeneration) {
    if (generation !== pollGeneration || jobId !== activeJobId) return;
    try {
      const job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== pollGeneration || jobId !== activeJobId) return;
      pollFailures = 0;
      $("retryProgress").hidden = true;
      const observed = Math.max(lastJobPercent, Math.min(100, number(job.percent) || 0));
      lastJobPercent = observed;
      $("progress").value = observed;
      $("progress").textContent = `${observed}%`;
      $("jobPercent").textContent = `${observed}%`;
      const selectionText = targetSelectionText(job);
      $("jobText").textContent = [
        job.state,
        job.current_item || job.safe_message_code || "",
        selectionText
      ].filter(Boolean).join(" · ");
      if (job.state === "SUCCEEDED") {
        activeJobId = null;
        clearPollTimer();
        if (job.safe_message_code === "ALREADY_UP_TO_DATE") {
          const viewingSelected = Boolean(
            catalog
            && runId === job.run_id
            && String(catalog.as_of) === String(job.as_of)
          );
          if (viewingSelected) {
            readyRun = null;
            readyAsOf = null;
            $("ready").hidden = true;
            $("jobText").textContent = `SUCCEEDED · 当前浏览的快照已是最新 · ${selectionText || `目标截止日 ${displayDate(job.as_of)}`}`;
          } else {
            readyRun = job.run_id;
            readyAsOf = job.as_of;
            const browsing = catalog && runId
              ? `当前仍浏览 ${displayDate(catalog.as_of)} 快照 ${runId}。`
              : "当前尚未载入最新快照。";
            const target = selectionText || `目标截止日 ${displayDate(job.as_of)}`;
            $("readyText").textContent = `最新快照 ${job.run_id} 已存在。${target}。${browsing}`;
            $("switch").textContent = `切换到 ${displayDate(job.as_of)} 快照`;
            $("switch").hidden = false;
            $("ready").hidden = false;
            $("job").hidden = true;
          }
          setJobBusy(false);
          return;
        }
        readyRun = job.run_id;
        readyAsOf = job.as_of;
        const browsing = catalog && runId
          ? `当前仍浏览 ${displayDate(catalog.as_of)} 快照 ${runId}。`
          : "当前尚无 v2 快照。";
        const target = selectionText || `目标截止日 ${displayDate(job.as_of)}`;
        $("readyText").textContent = `可用快照 ${job.run_id} 已就绪。${target}。${browsing}`;
        $("switch").textContent = `切换到 ${displayDate(job.as_of)} 快照`;
        $("switch").hidden = false;
        $("ready").hidden = false;
        $("job").hidden = true;
        setJobBusy(false);
        return;
      }
      if (["FAILED", "INTERRUPTED"].includes(job.state)) {
        activeJobId = null;
        clearPollTimer();
        setJobBusy(false);
        return;
      }
      clearPollTimer();
      pollTimer = window.setTimeout(() => void poll(jobId, generation), 800);
    } catch (_error) {
      if (generation !== pollGeneration || jobId !== activeJobId) return;
      retryProgressRead("进度暂时无法读取", () => void poll(jobId, generation));
    }
  }

  bindTabs();
  document.querySelectorAll("[data-level]").forEach(button => button.addEventListener("click", () => setLevel(button.dataset.level)));
  document.querySelectorAll("[data-heat-metric]").forEach(button => button.addEventListener("click", () => {
    heatMetric = button.dataset.heatMetric;
    setPressed("[data-heat-metric]", heatMetric, "heatMetric");
    renderHeatmap();
  }));
  document.querySelectorAll("[data-history-metric]").forEach(button => button.addEventListener("click", () => {
    historyMetric = button.dataset.historyMetric;
    setPressed("[data-history-metric]", historyMetric, "historyMetric");
    void renderTrend();
  }));
  document.querySelectorAll("[data-rank-field]").forEach(button => button.addEventListener("click", () => {
    rankField = button.dataset.rankField;
    setPressed("[data-rank-field]", rankField, "rankField");
    renderRanking();
  }));
  document.querySelectorAll("[data-archive-metric]").forEach(button => button.addEventListener("click", () => {
    archiveMetric = button.dataset.archiveMetric;
    setPressed("[data-archive-metric]", archiveMetric, "archiveMetric");
    void renderArchive();
  }));
  $("industrySearch").addEventListener("input", event => {
    industryQuery = event.target.value.trim().toLowerCase();
    currentShard = null;
    fillIndustrySelects();
    renderAll();
  });
  $("trendIndustry").addEventListener("change", () => void renderTrend());
  $("memberIndustry").addEventListener("change", () => void renderConstituents());
  $("stockSearch").addEventListener("input", renderStockRows);
  $("archiveIndustry").addEventListener("change", () => void renderArchive());
  $("retryProgress").addEventListener("click", () => {
    clearPollTimer();
    pollFailures = 0;
    $("retryProgress").hidden = true;
    if (recoveryMode === "job" && activeJobId) beginTrackingJob(activeJobId);
    else void discoverActiveJob();
  });
  $("currentSnapshot").addEventListener("click", async () => {
    $("currentSnapshot").disabled = true;
    try {
      const current = await api("/api/v1/snapshots/current");
      if (!current.run_id) throw new Error("CURRENT_SNAPSHOT_EMPTY");
      if (!await loadCatalog(current.run_id, current.as_of)) return;
      if (!readyRun || readyRun === current.run_id) {
        readyRun = null;
        readyAsOf = null;
        $("ready").hidden = true;
      }
    } catch (error) {
      $("readyText").textContent = `当前快照暂不可载入：${error.message}；继续保留正在浏览的内容。`;
      $("ready").hidden = false;
      $("switch").hidden = !readyRun;
    } finally {
      $("currentSnapshot").disabled = false;
    }
  });
  $("update").addEventListener("click", () => void startJob("UPDATE_LATEST"));
  $("materialize").addEventListener("click", () => {
    const value = $("historyDate").value;
    if (!value) { $("job").hidden = false; $("jobText").textContent = "请先选择历史开市日"; return; }
    void startJob("MATERIALIZE_DATE", value.replaceAll("-", ""));
  });
  $("switch").addEventListener("click", async () => {
    if (!readyRun) return;
    const selectedRun = readyRun;
    try {
      if (!await loadCatalog(readyRun, readyAsOf)) return;
      if (readyRun === selectedRun) {
        readyRun = null;
        readyAsOf = null;
        $("ready").hidden = true;
      }
    } catch (error) {
      $("readyText").textContent = `新快照载入失败：${error.message}；仍停留在旧快照。`;
      $("ready").hidden = false;
    }
  });

  void loadCatalog(runId).catch(error => {
    if (catalog) {
      $("readyText").textContent = `快照载入失败：${error.message}；继续保留正在浏览的内容。`;
      $("ready").hidden = false;
      $("switch").hidden = !readyRun;
      return;
    }
    $("snapshot").textContent = `快照载入失败 · ${error.message}`;
    fillIndustrySelects();
    renderAll();
  });
  if (!readOnly) void discoverActiveJob(Array.isArray(boot.active_jobs) ? boot.active_jobs : null);
  if (readOnly) {
    $("update").hidden = true;
    $("materialize").hidden = true;
    $("historyDate").closest("label").hidden = true;
    $("currentSnapshot").textContent = "重新读取已导入快照";
    if (!runId) $("snapshot").textContent = boot.legacy_state === "INVALID" ? "旧快照验证未通过" : "尚未导入旧快照";
  }
})();
