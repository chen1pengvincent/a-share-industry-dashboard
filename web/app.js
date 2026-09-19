"use strict";

/* Financial values are computed by Python. Browser work is presentation only. */
(() => {
  const decimal = value => {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const text = String(value).trim();
    const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(text);
    if (!match) return null;
    const fraction = match[3] || "";
    return { coefficient: BigInt((match[1] === "-" ? "-" : "") + match[2] + fraction), scale: fraction.length };
  };
  const compareDecimal = (left, right) => {
    const a = decimal(left), b = decimal(right);
    if (!a || !b) return null;
    const scale = Math.max(a.scale, b.scale);
    const av = a.coefficient * 10n ** BigInt(scale - a.scale);
    const bv = b.coefficient * 10n ** BigInt(scale - b.scale);
    return av < bv ? -1 : av > bv ? 1 : 0;
  };
  const missing = value => value === null || value === undefined || value === "" || (typeof value === "number" && !Number.isFinite(value));
  const compareValues = (left, right, direction, numeric = true) => {
    const lm = missing(left) || (numeric && !decimal(left));
    const rm = missing(right) || (numeric && !decimal(right));
    if (lm || rm) return lm === rm ? 0 : lm ? 1 : -1;
    const comparison = numeric ? compareDecimal(left, right) : String(left) < String(right) ? -1 : String(left) > String(right) ? 1 : 0;
    return direction === "desc" ? -comparison : comparison;
  };
  const nextSort = (sort, key) => sort.key !== key || sort.direction === "default"
    ? { key, direction: "asc" }
    : sort.direction === "asc" ? { key, direction: "desc" } : { key: null, direction: "default" };
  const sortRows = (rows, sort, columns) => {
    const column = columns.find(item => item.key === sort.key);
    if (!column || sort.direction === "default") return rows.slice();
    const identity = row => String(row.uid || "") + "/" + String(row.ts_code || "");
    return rows.map((row, index) => ({ row, index })).sort((a, b) => {
      const primary = compareValues(column.value(a.row), column.value(b.row), sort.direction, column.numeric !== false);
      if (primary) return primary;
      if (column.key === "name") { const rawName = compareValues(a.row.name, b.row.name, "asc", false); if (rawName) return rawName; }
      return compareValues(identity(a.row), identity(b.row), "asc", false) || a.index - b.index;
    }).map(item => item.row);
  };
  const formatDecimal = (value, digits = 2, shift = 0) => {
    const parsed = decimal(value);
    if (!parsed) return "—";
    let coefficient = parsed.coefficient;
    const negative = coefficient < 0n;
    if (negative) coefficient = -coefficient;
    const adjustment = parsed.scale + shift - digits;
    if (adjustment > 0) {
      const divisor = 10n ** BigInt(adjustment);
      coefficient = (coefficient + divisor / 2n) / divisor;
    } else coefficient *= 10n ** BigInt(-adjustment);
    const text = coefficient.toString().padStart(digits + 1, "0");
    const whole = (digits ? text.slice(0, -digits) : text).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (negative && coefficient !== 0n ? "−" : "") + whole + (digits ? "." + text.slice(-digits) : "");
  };
  const metric = (row, key) => {
    const value = row && row.metrics && row.metrics[key];
    return value && typeof value === "object" && !Array.isArray(value)
      ? value : { value: null, status: "NA", reason_codes: ["METRIC_UNAVAILABLE"] };
  };
  const valueOf = (row, key) => metric(row, key).value;
  const periodKey = (kind, date) => kind === "month" ? date.slice(0, 7) : date.replaceAll("-", "");
  const makeQuery = state => new URLSearchParams({ taxonomy: state.taxonomy, level_or_series: state.level,
    period_kind: state.periodKind, period_key: state.periodKey }).toString();
  const sameBatch = (response, batch) => Boolean(response && response.batch_id === batch);
  const defaultRows = (rows, page, view = "detail") => {
    const key = page === "moneyflow" ? "flow_cent" : page === "valuation" ? view === "ranking" ? "return_5d" : "name" : "code";
    const numeric = ["flow_cent", "return_5d"].includes(key);
    return sortRows(rows, { key, direction: numeric ? "desc" : "asc" }, [{ key, numeric, value: row => numeric ? valueOf(row, key) : key === "name" ? row.name_sort_key || row.name : row.code }]);
  };
  const core = { decimal, compareDecimal, compareValues, nextSort, sortRows, defaultRows, formatDecimal, metric, valueOf, periodKey, makeQuery, sameBatch };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== null && text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const svgEl = (tag, attributes = {}, text) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const wire = (id, event, action) => $(id).addEventListener(event, action);
  const state = {
    batchId: null, catalog: null, rows: [], period: null, page: "fusion", view: "detail",
    taxonomy: "SW", level: "L1", parentUid: "", periodKind: "day", periodKey: "", query: "", source: "median",
    sort: { key: null, direction: "default" }, memberSort: { key: null, direction: "default" },
    readyBatch: null, requestSequence: 0, chartSequence: 0, detailSequence: 0, nonce: null,
    chartVisible: false, detailRow: null, detailTab: "members", memberQuery: "", memberRows: [],
    jobId: null, jobKind: null, pendingUpdateId: null, jobGeneration: 0, jobTimer: null, jobFailures: 0, jobProgress: 0,
    trendUid: null, trendMetric: "pe_ttm_median", rankMetric: "return_5d", chartMetric: "pe_percentile",
    historyCache: new Map(), qualityCache: new Map(), qualitySequence: 0,
    qualitySort: { key: null, direction: "default" }, unknownSort: { key: null, direction: "default" }, availabilitySort: { key: null, direction: "default" },
    pageSorts: {}, development: false, updateBlocked: null, scheduler: null, currentTimer: null, submitting: false,
    historyScan: null, historyScanError: null, lastHistoryResult: null, historyGapSort: { key: null, direction: "default" }
  };
  const labels = {
    OK: "完整", SMALL_SAMPLE: "小样本", NA: "暂无数据", ACTIVE: "有效成员", COMPLETE: "完整", PARTIAL: "部分缺失",
    INCOMPLETE: "部分缺失", PROVISIONAL: "期间进行中", OPEN: "期间进行中", CURRENT: "期间进行中",
    HISTORY_UNAVAILABLE: "缺少历史归属", MEMBERSHIP_BOUNDARY_UNKNOWN: "归属边界未确认",
    MEMBERSHIP_OVERLAP_UNKNOWN: "归属重叠未确认", OBSERVED_SAME_DAY: "当日采集成员", OFFICIAL_DATED: "官方日期成员",
    UNKNOWN: "未确认", NOT_INSTALLED: "未导入", INVALID: "验证未通过", AVAILABLE: "可用",
    METRIC_UNAVAILABLE: "尚无该指标", INSUFFICIENT_HISTORY: "有效历史样本不足", HISTORY_INSUFFICIENT: "有效历史样本不足",
    MEMBERSHIP_UNKNOWN: "行业归属未确认", MISSING_FLOW: "资金数据缺失", PERIOD_INCOMPLETE: "期间数据不全",
    NO_VALID_VALUES: "没有有效正值", NOT_PUBLISHED: "官方未发布", NA_NOT_PUBLISHED: "官方未发布"
  };
  Object.assign(labels, {
    IN_PROGRESS: "期间进行中", DATA_GAP: "必需交易日缺失", WITH_GAPS: "存在数据缺口", NO_TRADING_DAYS: "期间无交易日",
    CURRENT_VALUE_MISSING: "当前指标缺失", RETURN_ANCHOR_MISSING: "收益基准交易日缺失", PERIOD_ENDPOINT_MISSING: "期末交易日缺失",
    PERIOD_DATA_GAP: "期间必需数据缺失", HISTORICAL_VALUE_UNAVAILABLE: "历史指标不可用", NO_DAILY_DATA: "该日尚无批次数据",
    OFFICIAL_UNAVAILABLE: "该分类无官方估值", NO_VALID_POSITIVE_VALUES: "没有有效正值", MEMBERSHIP_UNAVAILABLE: "缺少行业归属证据",
    MEMBERSHIP_HISTORY_UNAVAILABLE: "缺少该日历史成员证据", MEMBERSHIP_COVERAGE_UNKNOWN: "该分类成员覆盖未确认",
    MEMBER_LIFECYCLE_UNKNOWN: "成员上市或退市时点未确认", NO_MEMBERS: "没有已确认的有效成员",
    DAILY_BASIC_RECORD_MISSING: "成分股估值记录缺失", NO_VALID_ESTIMATE: "没有有效正值估值样本",
    OFFICIAL_NOT_PROVIDED: "该分类不提供官方估值", OFFICIAL_VALUE_MISSING: "官方指数数据缺失",
    FETCH: "获取数据", DAY: "按交易日处理", VERIFY: "校验批次", COMPILE: "生成展示数据", QUEUED: "等待执行", RUNNING: "执行中",
    HISTORY_INCOMPLETE: "历史仍有未通过的交易日", COMPLETE_WITH_GAPS: "扫描完成，历史仍有缺口", CHUNK_COMPLETE: "本批处理结束，仍待续补", CHUNK_COMPLETE_WITH_GAPS: "本批处理结束，仍有待尝试与未通过日期",
    INDEPENDENT_UNIVERSE_INCOMPLETE: "股票范围与交易、停牌证据不一致", MONEYFLOW_COVERAGE_GAP: "个股资金流覆盖不完整",
    DAILY_BASIC_COVERAGE_GAP: "个股估值记录覆盖不完整", SYNCHRONIZED_PARTIAL_SNAPSHOT: "多项响应可能同时缺失",
    THS_MULTIMEMBERSHIP_NO_SUM_CONSERVATION: "同花顺多重归属，行业合计不等于市场合计", WITHIN_LEVEL_OR_SERIES_ONLY: "仅在同一层级或系列核对"
  });
  const human = value => labels[String(value)] || String(value || "未确认");
  const dates = value => /^\d{8}$/.test(String(value || "")) ? String(value).replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3") : String(value || "—");
  const reasons = record => [record.status, ...(Array.isArray(record.reason_codes) ? record.reason_codes : [])].filter(Boolean).map(human).join(" · ");
  const finite = value => { const n = Number(value); return !missing(value) && Number.isFinite(n) ? n : null; };
  const signClass = value => !decimal(value) ? "missing" : compareDecimal(value, "0") > 0 ? "positive" : compareDecimal(value, "0") < 0 ? "negative" : "";
  const fmtMetric = (key, value) => key === "flow_cent" ? formatDecimal(value, 2, 6)
    : key.startsWith("return_") ? (decimal(value) ? formatDecimal(value, 2) + "%" : "—")
    : key.includes("percentile") ? (decimal(value) ? formatDecimal(value, 1) + "%" : "—")
    : formatDecimal(value, key === "net_mf_vol" ? 0 : 2);
  const metricLabel = key => ({ close: "官方指数收盘点位", pe_ttm_median: "成分 PE_TTM 中位数", pb_median: "成分 PB 中位数", pe_percentile: "PE 中位数历史百分位", pb_percentile: "PB 中位数历史百分位", official_pe: "官方 PE", official_pb: "官方 PB", official_pe_percentile: "官方 PE 历史百分位", official_pb_percentile: "官方 PB 历史百分位", flow_cent: "净流入（亿元）", net_mf_vol: "净流入量（手）", return_5d: "近 5 个交易日收益", return_mtd: "月初至今收益", return_ytd: "年初至今收益" })[key] || key;
  const metricCell = (row, key) => {
    const record = metric(row, key), box = el("span", fmtMetric(key, record.value));
    box.title = metricLabel(key) + "：" + (missing(record.value) ? "暂无" : key === "flow_cent" ? formatDecimal(record.value, 6, 6) : String(record.value)) + "\n" + reasons(record)
      + (record.metric_date ? "\n数据日：" + dates(record.metric_date) : "")
      + (record.valid_count !== undefined ? "\n有效样本：" + record.valid_count : "")
      + (record.expected_count !== undefined ? "\n应有记录：" + record.expected_count : "")
      + (record.received_count !== undefined ? "\n已取记录：" + record.received_count : "")
      + (key === "flow_cent" && missing(record.value) && !missing(record.known_subtotal) ? "\n已知部分净流入：" + formatDecimal(record.known_subtotal, 6, 6) + " 亿元（非完整总额）" : "")
      + (record.expected_days !== undefined ? "\n应有交易日：" + record.expected_days : "")
      + (record.missing_days !== undefined ? "\n缺失交易日：" + record.missing_days : "")
      + (record.equal_count !== undefined ? "\n与当前值相同的样本：" + record.equal_count : "")
      + (record.first_valid_date ? "\n有效历史：" + dates(record.first_valid_date) + " — " + dates(record.last_valid_date) : "")
      + (record.minimum_valid_days !== undefined ? "\n最低有效日数：" + record.minimum_valid_days : "");
    if (missing(record.value)) box.className = "missing";
    if (record.status === "SMALL_SAMPLE") { box.classList.add("small-sample"); box.append(el("small", " 小样本", "subtext")); }
    if (key === "flow_cent" || key.startsWith("return_")) {
      const tone = signClass(record.value);
      if (tone) box.classList.add(tone);
    }
    if (key.includes("percentile") && finite(record.value) !== null) {
      const wrap = el("div", null, "percent-cell"), track = el("span", null, "percent-track"), fill = el("span");
      fill.style.width = Math.max(0, Math.min(100, Number(record.value))) + "%";
      track.append(fill); wrap.append(box, track); return wrap;
    }
    return box;
  };
  const idColumn = { key: "name", label: "行业 / 代码", numeric: false, value: row => row.name_sort_key || row.name, render: row => {
    const box = el("div"), link = el("button", row.name || row.code, "industry-link");
    link.type = "button"; link.addEventListener("click", () => openDetail(row)); box.append(link, el("small", row.code, "subtext")); return box;
  } };
  const metricColumn = (key, label) => ({ key, label: label || metricLabel(key), value: row => valueOf(row, key), render: row => metricCell(row, key) });
  const flowRankColumn = { key: "flow_rank", label: "资金排名", title: "按同一分类、层级或系列的完整净流入排名；并列同名次，缺失不排名。搜索和父行业筛选保留原名次。", value: row => row.flow_rank, render: row => el("span", formatDecimal(row.flow_rank, 0)) };
  const countHints = {
    member_count: "截至期末、有证据的成员数；历史缺口时不代表行业实际成员总数",
    flow_expected: "期末已确认成员范围内应有的资金记录数；不是整个周期的日×股记录总数",
    flow_received: "期末已确认成员范围内已取得的资金记录数；不是整个周期的日×股记录总数"
  };
  const countColumn = (key, label) => ({ key, label, title: countHints[key], value: row => row.counts && row.counts[key], render: row => {
    const node = el("span", formatDecimal(row.counts && row.counts[key], 0));
    if (countHints[key]) node.title = countHints[key];
    return node;
  } });
  const statusColumn = { key: "status", label: "数据状态", numeric: false, value: row => typeof row.status === "string" ? row.status : "UNKNOWN", render: row => {
    const status = typeof row.status === "string" ? row.status : "UNKNOWN";
    const box = el("span", human(status), "status-label" + (["OK", "COMPLETE"].includes(status) ? "" : " warning"));
    box.title = [status, row.membership_evidence_kind].filter(Boolean).map(human).join(" · "); return box;
  } };
  function columnsForPage() {
    if (state.page === "moneyflow") return [idColumn, flowRankColumn, metricColumn("flow_cent"), metricColumn("net_mf_vol"), countColumn("member_count", "已确认成员"), countColumn("flow_expected", "期末应有记录"), countColumn("flow_received", "期末已取记录"), statusColumn];
    if (state.page === "valuation") return [idColumn, metricColumn("pe_ttm_median", "PE_TTM 中位数"), metricColumn("pe_percentile", "PE 历史百分位"), metricColumn("pb_median", "PB 中位数"), metricColumn("pb_percentile", "PB 历史百分位"), metricColumn("official_pe"), metricColumn("official_pe_percentile", "官方 PE 百分位"), metricColumn("official_pb"), metricColumn("official_pb_percentile", "官方 PB 百分位"), metricColumn("return_5d", "5D 收益"), metricColumn("return_mtd", "MTD 收益"), metricColumn("return_ytd", "YTD 收益"), countColumn("member_count", "已确认成员"), countColumn("pe_valid", "PE 有效数"), countColumn("pb_valid", "PB 有效数"), statusColumn];
    return [idColumn, flowRankColumn, metricColumn("flow_cent"), metricColumn("pe_ttm_median", "PE_TTM 中位数"), metricColumn("pe_percentile", "PE 历史百分位"), metricColumn("pb_median", "PB 中位数"), metricColumn("pb_percentile", "PB 历史百分位"), metricColumn("official_pe"), metricColumn("official_pb"), countColumn("member_count", "已确认成员"), countColumn("pe_valid", "PE 有效数"), countColumn("pb_valid", "PB 有效数"), statusColumn];
  }
  function renderTable(container, rows, columns, sort, onSort, caption) {
    const scrollLeft = container.scrollLeft, scrollTop = container.scrollTop;
    const table = el("table"), accessibleCaption = el("caption", caption);
    accessibleCaption.style.position = "absolute"; accessibleCaption.style.width = "1px"; accessibleCaption.style.height = "1px"; accessibleCaption.style.overflow = "hidden";
    table.append(accessibleCaption);
    const thead = el("thead"), header = el("tr"), tbody = el("tbody");
    for (const column of columns) {
      const th = el("th"), button = el("button", column.label), active = sort.key === column.key;
      th.scope = "col"; th.setAttribute("aria-sort", active ? (sort.direction === "asc" ? "ascending" : "descending") : "none");
      button.type = "button"; button.dataset.sort = column.key;
      if (column.title) button.title = column.title;
      button.setAttribute("aria-label", column.label + "，" + (active ? sort.direction === "asc" ? "当前升序，下次降序" : "当前降序，下次恢复默认" : "当前默认，下次升序"));
      button.append(el("span", active ? sort.direction === "asc" ? "↑" : "↓" : "↕", "sort-icon"));
      button.addEventListener("click", () => { onSort(nextSort(sort, column.key)); container.querySelector('[data-sort="' + column.key + '"]')?.focus(); });
      th.append(button); header.append(th);
    }
    thead.append(header);
    for (const row of sortRows(rows, sort, columns)) {
      const tr = el("tr");
      for (const column of columns) { const td = el("td"); td.append(column.render ? column.render(row) : el("span", column.value(row) ?? "—")); tr.append(td); }
      tbody.append(tr);
    }
    if (!rows.length) { const tr = el("tr"), td = el("td", "没有匹配的记录", "missing"); td.colSpan = columns.length; tr.append(td); tbody.append(tr); }
    table.append(thead, tbody); container.replaceChildren(table); container.scrollLeft = scrollLeft; container.scrollTop = scrollTop;
  }
  const visibleRows = () => defaultRows(state.rows.filter(row => (!state.parentUid || row.parent_uid === state.parentUid) && (!state.query || [row.name, row.code].some(value => String(value || "").toLowerCase().includes(state.query)))), state.page, state.view);
  const queryIdentity = () => [state.batchId, state.taxonomy, state.level, state.periodKind, state.periodKey].join("/");
  async function api(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options });
    let body;
    try { body = await response.json(); } catch (_) { throw new Error("服务返回了无法识别的数据"); }
    if (!response.ok) { const error = new Error(body.error && (body.error.message || body.error.code) || "请求未完成"); error.code = body.error && body.error.code; error.httpStatus = response.status; throw error; }
    return body;
  }
  function showError(error) { $("errorPanel").hidden = false; $("errorText").textContent = String(error.message || error); }
  function hideError() { $("errorPanel").hidden = true; }
  function updateLevels() {
    const taxonomy = state.catalog && state.catalog.taxonomies.find(item => item.id === state.taxonomy);
    const levels = taxonomy ? taxonomy.levels : [];
    const previous = state.level;
    $("level").replaceChildren(...levels.map(item => { const option = el("option", item.name); option.value = item.id; return option; }));
    state.level = levels.some(item => item.id === previous) ? previous : levels[0] && levels[0].id || "";
    $("level").value = state.level;
    updateParents();
  }
  function updateParents() {
    const parentLevel = ["SW", "CI"].includes(state.taxonomy) ? ({ L2: "L1", L3: "L2" })[state.level] : null;
    $("parentFilter").hidden = !parentLevel;
    const identities = (state.catalog?.industries || []).filter(row => row.taxonomy === state.taxonomy && row.level === parentLevel);
    const rows = defaultRows(identities, "valuation"), all = el("option", "全部父行业"); all.value = "";
    $("parentIndustry").replaceChildren(all, ...rows.map(row => { const option = el("option", row.name + " · " + row.code); option.value = row.uid; return option; }));
    if (!rows.some(row => row.uid === state.parentUid)) state.parentUid = "";
    $("parentIndustry").value = state.parentUid;
  }
  function updatePeriodInput(date) {
    const field = $("periodDate");
    const base = date || field.value || dates(state.catalog && state.catalog.as_of);
    field.type = state.periodKind === "month" ? "month" : "date";
    field.value = state.periodKind === "month" ? base.slice(0, 7) : base.length === 7 ? base + "-01" : base;
    $("dateLabel").textContent = state.periodKind === "month" ? "月份" : state.periodKind === "week" ? "该周任意日期" : "交易日";
    state.periodKey = periodKey(state.periodKind, field.value);
    document.querySelectorAll("[data-period]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.period === state.periodKind)));
  }
  async function loadBatch(batch, { useLatestDate = false } = {}) {
    const sequence = ++state.requestSequence;
    const catalog = await api("/api/v2/batches/" + encodeURIComponent(batch) + "/catalog");
    if (sequence !== state.requestSequence) return;
    if (!sameBatch(catalog, batch)) throw new Error("快照身份不一致，已停止切换");
    state.batchId = batch; state.catalog = catalog; state.rows = []; state.period = null; state.historyCache.clear(); state.qualityCache.clear(); state.qualitySequence++;
    state.sort = { key: null, direction: "default" }; state.trendUid = null; state.detailSequence++;
    if ($("detailDialog").open) $("detailDialog").close();
    state.readyBatch = null; $("newBatch").hidden = true;
    updateLevels(); updatePeriodInput(useLatestDate || !$("periodDate").value ? dates(catalog.as_of) : undefined);
    $("batchDate").textContent = "数据至 " + dates(catalog.as_of);
    $("batchIdentity").textContent = "固定批次 " + batch;
    renderRuntime();
    $("emptyState").hidden = true; $("dataArea").hidden = false;
    await loadRows();
  }
  async function loadRows() {
    const sequence = ++state.requestSequence, identity = queryIdentity(), batch = state.batchId;
    state.rows = []; state.period = null; state.memberRows = []; state.detailSequence++; state.qualitySequence++;
    if ($("detailDialog").open) $("detailDialog").close();
    $("qualityContent").replaceChildren(); $("qualitySummary").textContent = "查看每日覆盖与未知归属股票";
    hideError(); $("dataArea").classList.remove("loading");
    render();
    if (!batch || !state.level || !state.periodKey) {
      if (batch && !state.periodKey) showError(new Error("请选择有效的交易日或月份。"));
      return;
    }
    $("dataArea").classList.add("loading");
    try {
      const response = await api("/api/v2/batches/" + encodeURIComponent(batch) + "/industries?" + makeQuery(state));
      if (sequence !== state.requestSequence || identity !== queryIdentity()) return;
      if (!sameBatch(response, batch) || !Array.isArray(response.rows)) throw new Error("数据批次或结构不一致");
      state.rows = response.rows; state.period = response.period;
      state.detailSequence++; if ($("detailDialog").open) $("detailDialog").close();
      render();
    } catch (error) {
      if (sequence === state.requestSequence) { state.rows = []; state.period = null; render(); showError(error); }
    } finally { if (sequence === state.requestSequence) $("dataArea").classList.remove("loading"); }
  }
  function render() {
    const text = {
      valuation: ["行业估值，一目了然", "四套行业分类；成分估值中位数与官方指数估值独立展示。", "行业估值明细"],
      moneyflow: ["跟踪行业资金流向", "按当日真实行业归属聚合。正值净流入，负值净流出。", "行业资金流"],
      fusion: ["估值与资金，一起观察", "同一批次、同一行业归属。估值取期末，资金流按期间累计。", "行业对照"]
    }[state.page];
    $("pageTitle").textContent = text[0]; $("pageDescription").textContent = text[1]; $("tableTitle").textContent = text[2];
    $("mainPanel").setAttribute("aria-labelledby", "tab-" + state.page);
    document.querySelectorAll("[data-page]").forEach(button => { const active = button.dataset.page === state.page; button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; });
    $("valuationToolbar").hidden = state.page !== "valuation";
    $("chartToolbar").hidden = state.page !== "fusion";
    document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === state.view)));
    const rows = visibleRows();
    $("rowCount").textContent = rows.length + " / " + state.rows.length + " 个行业";
    const p = state.period;
    $("periodSummary").textContent = p ? dates(p.start) + (p.end !== p.start ? " — " + dates(p.end) : "") + " · 估值日 " + dates(p.endpoint || p.as_of) + " · 已有 " + (Array.isArray(p.available_days) ? p.available_days.length : p.available_days ?? "—") + " / " + (Array.isArray(p.expected_days) ? p.expected_days.length : p.expected_days ?? "—") + " 交易日 · " + human(p.status) : state.batchId ? "所选期间尚无可展示数据" : "尚无已验证批次";
    renderTable($("industryTable"), rows, columnsForPage(), state.sort, sort => { state.sort = sort; render(); }, text[2]);
    $("tablePanel").hidden = false;
    renderVisual(rows);
    if ($("qualityPanel").open) void loadQuality();
  }

  async function loadQuality() {
    if (!state.batchId || !state.periodKey) return;
    const sequence = ++state.qualitySequence, identity = queryIdentity();
    const key = [state.batchId, state.periodKind, state.periodKey].join("/");
    $("qualityContent").replaceChildren(el("p", "正在读取本批次质量记录…", "rank-note"));
    try {
      if (!state.qualityCache.has(key)) state.qualityCache.set(key, api("/api/v2/batches/" + encodeURIComponent(state.batchId) + "/quality?period_kind=" + state.periodKind + "&period_key=" + encodeURIComponent(state.periodKey)).catch(error => { state.qualityCache.delete(key); throw error; }));
      const body = await state.qualityCache.get(key);
      if (sequence !== state.qualitySequence || identity !== queryIdentity()) return;
      if (!sameBatch(body, state.batchId) || !Array.isArray(body.days)) throw new Error("质量记录批次或结构不一致");
      drawQuality(body);
    } catch (error) {
      if (sequence !== state.qualitySequence) return;
      const retry = el("button", "重试读取质量记录", "quiet"); retry.addEventListener("click", () => void loadQuality());
      $("qualityContent").replaceChildren(el("p", error.message, "error-text"), retry);
    }
  }
  function drawQuality(body) {
    const rows = [], unknown = [];
    for (const day of body.days) {
      const coverage = (day.audit?.classification_coverage || []).find(item => item.taxonomy === state.taxonomy && item.level === state.level);
      if (!coverage) { rows.push({ uid: day.trade_date, trade_date: day.trade_date, complete: null, unknown_count: null }); continue; }
      rows.push({ ...coverage, uid: day.trade_date, trade_date: day.trade_date, unknown_count: coverage.unknown_stocks?.length || 0 });
      for (const stock of coverage.unknown_stocks || []) unknown.push({ ...stock, uid: day.trade_date + "/" + state.taxonomy + "/" + state.level, trade_date: day.trade_date });
    }
    for (const date of body.missing_dates || []) rows.push({ uid: date, trade_date: date, complete: null, missing: true, unknown_count: null });
    const byDate = [{ key: "trade_date", numeric: false, value: row => row.trade_date }];
    const daily = sortRows(rows, { key: "trade_date", direction: "asc" }, byDate);
    const stockRows = sortRows(unknown, { key: "trade_date", direction: "asc" }, byDate);
    $("qualitySummary").textContent = unknown.length + " 条每日未知归属 · " + (body.missing_dates || []).length + " 个缺失交易日";
    const note = el("p", "质量范围：当前体系及层级的全部行业，不受父行业或搜索筛选影响。市场资金按股票去重；未知归属不会强分行业。以下逐日列示，不在浏览器合计。", "rank-note");
    const dailyTable = el("div", null, "table-scroll quality-table"), unknownTable = el("div", null, "table-scroll quality-table");
    const dateColumn = { key: "trade_date", label: "交易日", numeric: false, value: row => row.trade_date, render: row => el("span", dates(row.trade_date)) };
    const amount = (key, label) => ({ key, label, value: row => row[key], render: row => { const node = el("span", formatDecimal(row[key], 2, 6), signClass(row[key])); node.title = formatDecimal(row[key], 6, 6) + " 亿元"; return node; } });
    const columns = [dateColumn, { key: "complete", label: "归属覆盖", numeric: false, value: row => row.complete === null ? null : row.complete ? "1" : "0", render: row => el("span", row.missing ? "必需交易日缺失" : row.complete === null ? "暂无分类证据" : row.complete ? "完整" : "部分归属未确认", row.complete ? "status-label" : "status-label warning") }, { key: "unknown_count", label: "未知归属股票数", value: row => row.unknown_count, render: row => el("span", formatDecimal(row.unknown_count, 0)) }, amount("known_unique_flow_cent", "已归属净流入（亿元）"), amount("unknown_flow_cent", "未知归属净流入（亿元）"), amount("market_flow_cent", "市场净流入（亿元）")];
    const drawDays = () => renderTable(dailyTable, daily, columns, state.qualitySort, sort => { state.qualitySort = sort; drawDays(); }, "每日分类覆盖审计");
    const unknownColumns = [dateColumn, { key: "ts_code", label: "股票代码", numeric: false, value: row => row.ts_code }, amount("flow_cent", "个股净流入（亿元）"), { key: "reason", label: "原因与处理", numeric: false, value: () => "该日行业归属证据不足", render: () => el("span", "该日成员证据无法确认有效归属；资金保留为未知归属", "subtext") }];
    const drawStocks = () => renderTable(unknownTable, stockRows, unknownColumns, state.unknownSort, sort => { state.unknownSort = sort; drawStocks(); }, "每日未知归属股票");
    const scopeNote = state.taxonomy === "THS" ? "同花顺允许多重归属，行业相加会重复计数；此处已归属资金按股票去重，仅用于覆盖核对。" : "仅在当前分类、同一层级或系列核对，不跨体系、层级或系列相加。";
    $("qualityContent").replaceChildren(availabilitySection(), note, dailyTable, el("p", scopeNote, "rank-note"), el("h3", "未知归属股票 · 逐日记录"), unknownTable);
    drawDays(); drawStocks();
  }

  function availabilitySection() {
    const section = el("section"), title = el("h3", "本批次历史 · 指标可用区间");
    const note = el("p", "覆盖本批次已保存历史，不限于当前观察周期。样本数按“行业 × 交易日”计数，不是整个分类有效的交易日数；起止日期之间仍可能存在缺口。", "rank-note");
    const scope = (state.catalog?.history?.availability_by_scope || []).find(item => item.taxonomy === state.taxonomy && item.level === state.level);
    section.append(title, note);
    if (!scope) { section.append(el("p", "该分类层级尚无历史可用区间记录。", "rank-note")); return section; }
    const keys = ["pe_ttm_median", "pb_median", "flow_cent", "official_pe", "official_pb", "close"];
    const nameKeys = { pe_ttm_median: "cheng fen pe ttm zhong wei shu", pb_median: "cheng fen pb zhong wei shu", flow_cent: "jing liu ru", official_pe: "guan fang pe", official_pb: "guan fang pb", close: "guan fang zhi shu shou pan dian wei" };
    const rows = keys.map(key => ({ ...(scope.metrics?.[key] || {}), uid: state.taxonomy + "/" + state.level + "/" + key, name: metricLabel(key), name_sort_key: nameKeys[key] }));
    const table = el("div", null, "table-scroll quality-table");
    const columns = [{ key: "name", label: "指标", numeric: false, value: row => row.name_sort_key, render: row => el("span", row.name) },
      ...[["first_valid_date", "首个有效日期"], ["last_valid_date", "最后有效日期"]].map(([key, label]) => ({ key, label, numeric: false, value: row => row[key], render: row => el("span", dates(row[key])) })),
      ...[["valid_industry_days", "有效行业×日样本"], ["missing_industry_days", "缺失行业×日样本"]].map(([key, label]) => ({ key, label, value: row => row[key], render: row => el("span", formatDecimal(row[key], 0)) }))];
    const draw = () => renderTable(table, rows, columns, state.availabilitySort, sort => { state.availabilitySort = sort; draw(); }, "当前分类层级的历史指标可用区间");
    section.append(table); draw(); return section;
  }

  function renderRuntime() {
    $("batchState").textContent = state.development ? "开发验收数据 · 研究用途" : "研究用途";
    $("batchState").classList[state.development ? "add" : "remove"]("development-badge");
    const notes = [];
    if (state.development) notes.push("当前实例为隔离开发验收数据，未作为正式批次发布；自动调度已关闭。");
    if (state.updateBlocked) notes.push(state.updateBlocked.message || human(state.updateBlocked.code));
    if (state.scheduler?.last_error && !state.updateBlocked) notes.push("自动更新：" + (state.scheduler.last_error.message || human(state.scheduler.last_error.code)));
    if (state.historyScanError) notes.push(historyUnavailableText());
    else if (state.scheduler?.history_error) notes.push("历史回补：" + (state.scheduler.history_error.message || human(state.scheduler.history_error.code)));
    const gaps = state.historyScanError ? null : state.historyScan || state.scheduler?.history_gaps || state.lastHistoryResult;
    if (Array.isArray(gaps?.blocked_dates) && gaps.blocked_dates.length) notes.push("历史有 " + gaps.blocked_dates.length + " 个失败日期，另有 " + (gaps.remaining_attemptable_days ?? "未确认") + " 日待尝试；可在“历史补齐”查看原因，修复后明确选择重试。");
    if (state.scheduler && !state.scheduler.enabled && !state.development) notes.push("自动调度已关闭；当前可浏览已有批次。" + (state.updateBlocked || state.historyScanError ? "" : "手动更新仍可用。"));
    $("runtimeNotice").hidden = !notes.length; $("runtimeNotice").textContent = notes.join(" ");
    for (const id of ["updateButton", "emptyUpdate"]) $(id).disabled = Boolean(state.pendingUpdateId || state.jobKind === "update" || state.updateBlocked || state.historyScanError || state.submitting);
    $("historyButton").disabled = Boolean(state.jobId || state.updateBlocked || state.historyScanError || state.submitting);
    if ($("backfillDialog").open) renderBackfillStatus();
  }

  const historyUnavailableText = () => "历史状态未知：" + (state.historyScanError?.message || human(state.historyScanError?.code)) + " 请先修复历史状态记录；回填已停止，已有批次仍可阅读。";
  function renderBackfillStatus() {
    if (state.historyScanError) {
      $("backfillStatus").textContent = historyUnavailableText();
      $("backfillGaps").hidden = true; $("backfillGapTable").replaceChildren();
      return;
    }
    const history = state.historyScan || state.scheduler?.history_gaps || state.lastHistoryResult;
    const start = $("backfillStart").value.replaceAll("-", ""), end = $("backfillEnd").value.replaceAll("-", "");
    const inRange = day => (!start || day >= start) && (!end || day <= end);
    const blocked = (history?.blocked_dates || []).filter(row => inRange(row.trade_date)).map(row => ({ ...row, uid: row.trade_date }));
    const pending = Array.isArray(history?.pending_dates) ? history.pending_dates.filter(inRange).length : null;
    $("backfillStatus").textContent = history
      ? (state.historyScan || state.scheduler?.history_gaps ? "当前记录的所选范围" : "上次作业在所选范围内") + "：" + blocked.length + " 个已失败日期；" + (pending === null ? "待尝试数量未确认" : pending + " 日待尝试") + "。默认跳过当前证据下已失败日期；勾选后每个失败日仅重试一次。"
      : "暂无历史回填状态；默认不重试已失败日期。可隔离的历史日期问题保留原因后继续；网络、权限等错误仍会暂停任务。";
    $("backfillGaps").hidden = !blocked.length;
    if (!blocked.length) { $("backfillGapTable").replaceChildren(); return; }
    const columns = [
      { key: "trade_date", label: "失败日期", numeric: false, value: row => row.trade_date, render: row => el("span", dates(row.trade_date)) },
      { key: "code", label: "未通过原因", numeric: false, value: row => row.code, render: row => { const node = el("span", human(row.code)); node.append(el("small", row.code, "subtext")); return node; } },
      { key: "reason", label: "说明", numeric: false, value: row => row.reason, render: row => el("span", row.reason || "当前证据下未通过，未认定永久不可恢复") }
    ];
    const rows = blocked.slice().sort((a, b) => compareValues(a.trade_date, b.trade_date, "asc", false));
    renderTable($("backfillGapTable"), rows, columns, state.historyGapSort, sort => { state.historyGapSort = sort; renderBackfillStatus(); }, "历史失败日期及原因");
  }
  function chartEmpty(target, text) { target.replaceChildren(el("div", text, "chart-empty")); }
  function addSelect(target, items, value, onChange, label) {
    const select = el("select"); select.setAttribute("aria-label", label);
    items.forEach(([key, text]) => { const option = el("option", text); option.value = key; select.append(option); });
    select.value = value; select.addEventListener("change", () => onChange(select.value)); target.append(select); return select;
  }
  function renderVisual(rows) {
    state.chartSequence++;
    const show = state.page === "valuation" ? state.view !== "detail" : state.page === "fusion" ? state.chartVisible : false;
    $("visualPanel").hidden = !show; $("summaryCards").hidden = !(state.page === "valuation" && state.view === "overview");
    $("chartControls").replaceChildren(); $("chartLegend").replaceChildren();
    if (!show) return;
    const official = state.source === "official", pe = official ? "official_pe" : "pe_ttm_median", pb = official ? "official_pb" : "pb_median", percentile = official ? "official_pe_percentile" : "pe_percentile";
    if (state.page === "fusion") {
      $("chartTitle").textContent = "估值位置 × 期间资金流";
      $("chartDescription").textContent = "横轴为中位数历史百分位；纵轴为净流入。缺失值不绘点。";
      addSelect($("chartControls"), [["pe_percentile", "PE 中位数历史百分位"], ["pb_percentile", "PB 中位数历史百分位"]], state.chartMetric, value => { state.chartMetric = value; renderVisual(rows); }, "散点横轴");
      scatter($("chart"), rows, state.chartMetric, "flow_cent"); return;
    }
    if (state.view === "overview") {
      const cards = [["当前行业", rows.length, "仅当前筛选"], ["估值有值", rows.filter(row => !missing(valueOf(row, pe))).length, metricLabel(pe)], ["历史位置可用", rows.filter(row => !missing(valueOf(row, percentile))).length, "有效历史 ≥ 252 日"], ["有资金记录", rows.filter(row => !missing(valueOf(row, "flow_cent"))).length, "不跨行业加总"]];
      $("summaryCards").replaceChildren(...cards.map(([label, value, note]) => { const box = el("article", null, "stat"); box.append(el("div", label, "stat-label"), el("div", value, "stat-value"), el("div", note, "stat-note")); return box; }));
      $("chartTitle").textContent = "行业历史估值位置"; $("chartDescription").textContent = metricLabel(percentile) + " · 按百分位升序";
      bars($("chart"), rows, percentile, "asc");
    } else if (state.view === "heatmap") {
      $("chartTitle").textContent = "历史位置热力图"; $("chartDescription").textContent = metricLabel(percentile) + " · 颜色只表示统计位置";
      const grid = el("div", null, "heat-grid"), colors = ["#254d48", "#2d555f", "#485a75", "#755b49", "#7c464e"];
      rows.forEach(row => { const value = finite(valueOf(row, percentile)), tile = el("button", null, "heat-tile"); tile.style.backgroundColor = value === null ? "#252d38" : colors[Math.min(4, Math.floor(value / 20))]; tile.title = row.name + " · " + reasons(metric(row, percentile)); tile.append(el("strong", row.name), el("b", fmtMetric(percentile, valueOf(row, percentile))), el("small", row.code)); tile.addEventListener("click", () => openDetail(row)); grid.append(tile); });
      if (rows.length) $("chart").replaceChildren(grid); else chartEmpty($("chart"), "没有匹配的行业");
      $("chartLegend").textContent = "0–20%　20–40%　40–60%　60–80%　80–100%　灰色：不可用";
    } else if (state.view === "trend") {
      $("chartTitle").textContent = "行业历史走势"; $("chartDescription").textContent = "图中缺口保留；不做前值填充。";
      if (!rows.length) { chartEmpty($("chart"), "没有匹配的行业"); return; }
      if (!rows.some(row => row.uid === state.trendUid)) state.trendUid = rows[0].uid;
      if (![pe, pb, "close"].includes(state.trendMetric)) state.trendMetric = pe;
      addSelect($("chartControls"), rows.map(row => [row.uid, row.name]), state.trendUid, uid => { state.trendUid = uid; renderVisual(rows); }, "走势图行业");
      addSelect($("chartControls"), [pe, pb, "close"].map(key => [key, metricLabel(key)]), state.trendMetric, key => { state.trendMetric = key; renderVisual(rows); }, "走势图指标");
      const sequence = state.chartSequence, batch = state.batchId, uid = state.trendUid;
      chartEmpty($("chart"), "正在读取已验证历史…");
      loadHistory(uid).then(history => { if (sequence === state.chartSequence && batch === state.batchId) lineChart($("chart"), history, state.trendMetric); }).catch(error => { if (sequence === state.chartSequence) chartEmpty($("chart"), error.message); });
    } else if (state.view === "ranking") {
      $("chartTitle").textContent = "官方指数收益排行"; $("chartDescription").textContent = "收益由后端按官方收盘价与精确交易日锚点计算；无行情的行业不补造。";
      addSelect($("chartControls"), [["return_5d", "近 5 个交易日"], ["return_mtd", "月初至今"], ["return_ytd", "年初至今"]], state.rankMetric, key => { state.rankMetric = key; state.sort = {key, direction: "desc"}; render(); }, "收益区间");
      bars($("chart"), rows, state.rankMetric, "desc");
    } else if (state.view === "scatter") {
      $("chartTitle").textContent = "PE–PB 象限"; $("chartDescription").textContent = metricLabel(pe) + " × " + metricLabel(pb) + "；仅比较原始数值，不划定低估或高估。";
      scatter($("chart"), rows, pe, pb);
    }
  }
  function bars(target, rows, key, direction) {
    const available = sortRows(rows.filter(row => finite(valueOf(row, key)) !== null), { key, direction }, [metricColumn(key)]);
    if (!available.length) { chartEmpty(target, "该范围暂无可用指标；详情表保留缺失原因。"); return; }
    const max = Math.max(...available.map(row => Math.abs(Number(valueOf(row, key)))), 1e-12), wrap = el("div", null, "bars");
    available.forEach(row => { const item = el("div", null, "bar-row"), label = el("button", row.name, "bar-label"), track = el("div", null, "bar-track"), fill = el("div", null, "bar-fill"), number = valueOf(row, key);
      label.title = row.name; label.addEventListener("click", () => openDetail(row)); fill.style.width = Math.max(.3, Math.abs(Number(number)) / max * 100) + "%";
      if (key.startsWith("return_") || key === "flow_cent") fill.style.backgroundColor = Number(number) >= 0 ? "#ff7b72" : "#56d49b";
      track.append(fill); item.append(label, track, el("span", fmtMetric(key, number), "bar-value " + (key.startsWith("return_") ? signClass(number) : ""))); wrap.append(item);
    }); target.replaceChildren(wrap);
  }
  function svgBase(target, name) { const svg = svgEl("svg", { viewBox: "0 0 950 330", role: "img", "aria-label": name }); target.replaceChildren(svg); return svg; }
  function scatter(target, rows, xkey, ykey) {
    const points = rows.map(row => ({ row, x: finite(valueOf(row, xkey)), y: finite(valueOf(row, ykey)) })).filter(p => p.x !== null && p.y !== null);
    if (!points.length) { chartEmpty(target, "没有同时具备两项指标的行业。数据不足时不补点。"); return; }
    if (ykey === "flow_cent") points.forEach(p => { p.y /= 1000000; });
    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    let xmin = Math.min(0, ...xs), xmax = Math.max(...xs), ymin = Math.min(0, ...ys), ymax = Math.max(...ys);
    if (xkey.includes("percentile")) { xmin = 0; xmax = 100; }
    if (xmin === xmax) xmax = xmin + 1; if (ymin === ymax) ymax = ymin + 1;
    const width = 824, height = 242, px = value => 74 + (value - xmin) / (xmax - xmin) * width, py = value => 270 - (value - ymin) / (ymax - ymin) * height;
    const svg = svgBase(target, metricLabel(xkey) + "与" + metricLabel(ykey));
    for (let i = 0; i <= 4; i++) {
      const x = xmin + (xmax - xmin) * i / 4, y = ymin + (ymax - ymin) * i / 4;
      svg.append(svgEl("line", { x1: 74, x2: 898, y1: py(y), y2: py(y), class: "axis-line" }), svgEl("text", { x: 65, y: py(y) + 4, "text-anchor": "end" }, formatDecimal(String(y.toFixed(2)), 2)), svgEl("text", { x: px(x), y: 292, "text-anchor": "middle" }, formatDecimal(String(x.toFixed(1)), 1)));
    }
    if (ymin < 0 && ymax > 0) svg.append(svgEl("line", { x1: 74, x2: 898, y1: py(0), y2: py(0), stroke: "#8295a9", "stroke-dasharray": "4 4" }));
    svg.append(svgEl("text", { x: 486, y: 322, "text-anchor": "middle" }, metricLabel(xkey)), svgEl("text", { x: 74, y: 15 }, metricLabel(ykey)));
    points.forEach(p => { const circle = svgEl("circle", { cx: px(p.x), cy: py(p.y), r: 5.5, fill: ykey === "flow_cent" ? p.y >= 0 ? "#ff7b72" : "#56d49b" : "#58a6ff", class: "plot-point", tabindex: 0, role: "button", "aria-label": p.row.name + "，" + metricLabel(xkey) + " " + fmtMetric(xkey, valueOf(p.row, xkey)) + "，" + metricLabel(ykey) + " " + fmtMetric(ykey, valueOf(p.row, ykey)) });
      circle.append(svgEl("title", {}, p.row.name + "\n" + metricLabel(xkey) + "：" + fmtMetric(xkey, valueOf(p.row, xkey)) + "\n" + metricLabel(ykey) + "：" + fmtMetric(ykey, valueOf(p.row, ykey))));
      circle.addEventListener("click", () => openDetail(p.row)); circle.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); openDetail(p.row); } }); svg.append(circle);
    });
  }
  function lineChart(target, rows, key) {
    const axis = rows.map((row, index) => ({ date: row.trade_date || row.period?.endpoint || row.period?.as_of || row.period?.key, value: finite(valueOf(row, key)), index }));
    const valid = axis.filter(point => point.value !== null && (key === "flow_cent" || key.includes("percentile") || key.startsWith("return_") || point.value > 0));
    if (!valid.length) { chartEmpty(target, "该指标暂无可用历史；缺失记录没有被前值填充。"); return; }
    const factor = key === "flow_cent" ? 1000000 : 1;
    const values = valid.map(point => point.value / factor); let min = Math.min(...values), max = Math.max(...values); if (min === max) { min -= .5; max += .5; }
    const px = i => 74 + i / Math.max(axis.length - 1, 1) * 824, py = value => 268 - (value / factor - min) / (max - min) * 235;
    const svg = svgBase(target, metricLabel(key) + "历史走势");
    for (let i = 0; i <= 4; i++) { const value = min + (max - min) * i / 4, y = 268 - i / 4 * 235; svg.append(svgEl("line", { x1: 74, x2: 898, y1: y, y2: y, class: "axis-line" }), svgEl("text", { x: 65, y: y + 4, "text-anchor": "end" }, value.toFixed(2))); }
    let path = "", connected = false;
    axis.forEach(point => {
      const usable = point.value !== null && (key === "flow_cent" || key.includes("percentile") || key.startsWith("return_") || point.value > 0);
      if (!usable) { connected = false; return; }
      path += (connected ? " L " : " M ") + px(point.index) + " " + py(point.value); connected = true;
    });
    svg.append(svgEl("path", { d: path, fill: "none", stroke: "#58a6ff", "stroke-width": 2.2 }));
    valid.forEach(point => { const circle = svgEl("circle", { cx: px(point.index), cy: py(point.value), r: axis.length < 80 ? 3 : 1.8, fill: "#58a6ff" }); circle.append(svgEl("title", {}, dates(point.date) + " · " + fmtMetric(key, valueOf(rows[point.index], key)))); svg.append(circle); });
    const ticks = [...new Set([0, Math.floor((axis.length - 1) / 2), axis.length - 1])];
    ticks.forEach(index => svg.append(svgEl("text", { x: px(index), y: 295, "text-anchor": index === 0 ? "start" : index === axis.length - 1 ? "end" : "middle" }, dates(axis[index].date))));
    svg.append(svgEl("text", { x: 74, y: 16 }, metricLabel(key)), svgEl("text", { x: 898, y: 322, "text-anchor": "end" }, "缺失处断线 · " + rows.length + " 个期间"));
  }
  async function loadHistory(uid) {
    const batch = state.batchId, kind = state.periodKind, cacheKey = [batch, uid, kind].join("/");
    if (!state.historyCache.has(cacheKey)) state.historyCache.set(cacheKey, api("/api/v2/batches/" + encodeURIComponent(batch) + "/industries/" + encodeURIComponent(uid) + "/history?period_kind=" + kind).then(body => {
      if (!sameBatch(body, batch) || body.uid !== uid || !Array.isArray(body.rows)) throw new Error("历史数据身份不一致"); return body.rows;
    }).catch(error => { state.historyCache.delete(cacheKey); throw error; }));
    return state.historyCache.get(cacheKey);
  }
  async function openDetail(row) {
    state.detailRow = row; state.detailTab = state.page === "moneyflow" ? "contribution" : "members"; state.memberQuery = ""; state.memberSort = { key: null, direction: "default" };
    $("detailTitle").textContent = row.name; $("detailIdentity").textContent = row.uid;
    $("detailContext").textContent = $("periodSummary").textContent + " · 批次 " + state.batchId;
    if (!$("detailDialog").open) $("detailDialog").showModal();
    await renderDetail();
  }
  async function renderDetail() {
    const sequence = ++state.detailSequence, identity = queryIdentity(), row = state.detailRow;
    if (!row) return;
    document.querySelectorAll("[data-detail]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.detail === state.detailTab)));
    $("detailError").hidden = true; $("detailContent").replaceChildren(el("div", "正在读取已验证数据…", "chart-empty"));
    try {
      if (["members", "contribution"].includes(state.detailTab)) {
        const body = await api("/api/v2/batches/" + encodeURIComponent(state.batchId) + "/industries/" + encodeURIComponent(row.uid) + "/members?period_kind=" + state.periodKind + "&period_key=" + encodeURIComponent(state.periodKey));
        if (sequence !== state.detailSequence || identity !== queryIdentity()) return;
        if (!sameBatch(body, state.batchId) || body.uid !== row.uid || !Array.isArray(body.rows)) throw new Error("成分数据身份不一致");
        state.memberRows = body.rows; renderMembers();
      } else {
        const history = await loadHistory(row.uid);
        if (sequence !== state.detailSequence || identity !== queryIdentity()) return;
        const controls = el("div", null, "chart-controls"), chart = el("div", null, "chart"), tableWrap = el("div", null, "table-scroll");
        const keys = ["flow_cent", "pe_ttm_median", "pb_median", "pe_percentile", "pb_percentile", "official_pe", "official_pb", "close"];
        addSelect(controls, keys.map(key => [key, metricLabel(key)]), "flow_cent", key => lineChart(chart, history, key), "历史指标");
        const columns = [{ key: "trade_date", label: "数据日期", numeric: false, value: item => item.trade_date, render: item => el("span", dates(item.trade_date)) }, ...keys.map(key => metricColumn(key))];
        let sort = { key: null, direction: "default" };
        const drawTable = () => renderTable(tableWrap, history, columns, sort, next => { sort = next; drawTable(); }, "行业历史明细");
        $("detailContent").replaceChildren(controls, chart, tableWrap); lineChart(chart, history, "flow_cent"); drawTable();
      }
    } catch (error) { if (sequence === state.detailSequence) { $("detailError").hidden = false; $("detailError").textContent = error.message; $("detailContent").replaceChildren(); } }
  }
  function renderMembers() {
    const tools = el("div", null, "member-tools"), input = el("input"), tableWrap = el("div", null, "table-scroll");
    input.type = "search"; input.placeholder = "搜索股票名称或代码"; input.value = state.memberQuery; input.setAttribute("aria-label", "搜索行业成分");
    const count = el("span"); tools.append(input, count);
    const rawColumn = (key, label, digits = 2, shift = 0) => ({ key, label, value: row => row[key], render: row => el("span", formatDecimal(row[key], digits, shift), key === "flow_cent" ? signClass(row[key]) : "") });
    const columns = [{ key: "name", label: "股票 / 代码", numeric: false, value: row => row.name_sort_key || row.name, render: row => { const node = el("span", row.name || row.ts_code); node.append(el("small", row.ts_code, "subtext")); return node; } }, rawColumn("flow_cent", "期间净流入（亿元）", 2, 6), rawColumn("pe_ttm", "期末 PE_TTM"), rawColumn("pe_peer_percentile", "PE 同业百分位（%）", 1), rawColumn("pb", "期末 PB"), rawColumn("pb_peer_percentile", "PB 同业百分位（%）", 1), rawColumn("peer_pe_count", "PE 有效同业", 0), rawColumn("peer_pb_count", "PB 有效同业", 0), rawColumn("pe", "期末 PE"), rawColumn("ps_ttm", "PS_TTM"), rawColumn("dv_ttm", "股息率（%）"), rawColumn("total_mv", "总市值（亿元）", 2, 4), rawColumn("circ_mv", "流通市值（亿元）", 2, 4), { key: "is_endpoint_member", label: "期末归属", numeric: false, value: row => row.is_endpoint_member ? "1" : "0", render: row => el("span", row.is_endpoint_member ? "期末成员" : "期末归属未确认", "status-label" + (row.is_endpoint_member ? "" : " warning")) }, { key: "membership_state", label: "成员状态", numeric: false, value: row => row.membership_state, render: row => el("span", human(row.membership_state), "subtext") }];
    const showKnown = state.memberRows.some(row => missing(row.flow_cent) && !missing(row.known_subtotal));
    if (showKnown) columns.splice(2, 0, { key: "known_subtotal", label: "已知净流入（非完整总额）· 亿元", value: row => row.known_subtotal, render: row => {
      const node = el("span", formatDecimal(row.known_subtotal, 2, 6), signClass(row.known_subtotal));
      node.title = missing(row.known_subtotal) ? "没有可确认的已知资金小计" : "已确认归属部分：" + formatDecimal(row.known_subtotal, 6, 6) + " 亿元；不能替代完整净流入";
      return node;
    } });
    const contribution = state.detailTab === "contribution";
    const shownColumns = contribution ? columns.filter(column => ["name", "flow_cent", "known_subtotal", "is_endpoint_member", "membership_state"].includes(column.key)) : columns;
    const draw = () => { const query = state.memberQuery.toLowerCase(), selectedRows = state.memberRows.filter(row => !query || [row.ts_code, row.name].some(text => String(text || "").toLowerCase().includes(query))); const key = contribution ? "flow_cent" : "ts_code"; const rows = sortRows(selectedRows, {key,direction:contribution ? "desc" : "asc"}, [{key,numeric:contribution,value:row=>row[key]}]); count.textContent = rows.length + " / " + state.memberRows.length + " 个期间成分 · 搜索不改变分母";
      renderTable(tableWrap, rows, shownColumns, state.memberSort, sort => { state.memberSort = sort; draw(); }, contribution ? "期间成员资金贡献" : "期间成分与期末估值"); };
    input.addEventListener("input", () => { state.memberQuery = input.value.trim(); draw(); });
    const notes = [el("p", "期内退出成员保留其资金贡献；非期末成员的期末估值留空。成员估值百分位来自后端，低于 5 个有效样本留空。", "rank-note")];
    if (showKnown) notes.unshift(el("p", "完整净流入缺失时仍显示“—”，不会用小计补齐。“已知净流入”仅含已确认有效归属的资金，不是完整总额；小计 0 表示已知部分为零，不代表未知部分为零。可点击小计表头单独排序。资金贡献默认按完整净流入排序，期间成分默认按股票代码排序。", "rank-note"));
    $("detailContent").replaceChildren(tools, tableWrap, ...notes); draw();
  }
  function jobRecord(response) { return response && response.job_id ? response : response && response.job || null; }
  function showJob(job) {
    $("jobPanel").hidden = false; $("retryJob").hidden = true;
    $("jobTitle").textContent = ({ update: "统一更新", backfill: "历史补齐", UPDATE: "统一更新", BACKFILL: "历史补齐" })[job.kind] || "数据作业";
    const result = job.result || {};
    const hasTotal = Number.isFinite(job.total_units) && job.total_units > 0;
    const showFraction = job.status !== "SUCCEEDED" && hasTotal;
    const alreadyCurrent = ["update", "UPDATE"].includes(job.kind) && job.status === "SUCCEEDED" && result.already_current === true;
    $("jobDetail").textContent = alreadyCurrent
      ? "已是最新" + (result.as_of ? "（数据至 " + dates(result.as_of) + "）" : "")
      : [job.message || human(job.phase), showFraction && Number.isFinite(job.completed_units) && job.completed_units >= 0 ? job.completed_units + " / " + job.total_units : ""].filter(Boolean).join(" · ");
    const history = job.kind === "backfill" || job.kind === "BACKFILL";
    const progress = (history ? result.scan_complete === true : job.status === "SUCCEEDED") ? 100 : job.total_units > 0 ? Math.min(99, job.completed_units / job.total_units * 100) : 0;
    state.jobProgress = Math.max(state.jobProgress, progress); $("jobProgress").value = state.jobProgress;
    $("jobProgress").setAttribute("aria-label", history ? "可尝试交易日扫描进度，不代表历史数据完整" : "作业进度");
    const active = ["QUEUED", "RUNNING"].includes(job.status);
    state.jobKind = active ? job.kind : null;
    if (job.kind === "update") state.pendingUpdateId = active ? job.job_id : null;
    if (job.status === "FAILED") { $("jobTitle").textContent += "未完成"; $("jobDetail").textContent = job.error && (job.error.message || job.error.code) || job.message || "请检查数据可用性后再更新"; }
    if (history) {
      if (job.result) state.lastHistoryResult = result;
      const attempted = Array.isArray(result.attempted_days) ? result.attempted_days.length : job.completed_units;
      const captured = Array.isArray(result.captured_days) ? result.captured_days.length : null;
      const blocked = Array.isArray(result.blocked_dates) ? result.blocked_dates.length : null;
      const pending = result.remaining_attemptable_days;
      const detail = [result.scan_complete === true ? "可尝试日期已扫描完" : job.message || human(job.phase)];
      if (attempted !== undefined) detail.push("已尝试 " + attempted + (showFraction ? " / " + job.total_units : "") + " 日");
      if (captured !== null) detail.push("本次已获取 " + captured + " 日");
      if (blocked !== null) detail.push("失败 " + blocked + " 日");
      if (pending !== undefined) detail.push("待尝试 " + pending + " 日");
      if (job.error?.code === "HISTORY_INCOMPLETE" || blocked > 0 && job.status === "SUCCEEDED") {
        $("jobTitle").textContent = "历史补齐仍有缺口";
        detail.push("历史未完整；已成功日期保留，可查看失败原因后重试");
      } else if (job.status === "SUCCEEDED") {
        $("jobTitle").textContent = result.history_complete === true ? "所选范围交易日已补齐" : "本次历史补齐已结束";
        detail.push(result.history_complete === true ? "交易日分片已齐；各分类指标仍可能缺少历史证据" : "历史仍未完整，可继续补齐待尝试日期");
      } else if (job.status === "FAILED") detail.push("作业已停止：" + human(job.error?.code));
      $("jobDetail").textContent = detail.filter(Boolean).join(" · ");
    }
    renderRuntime();
    return active;
  }
  async function checkCurrent() {
    const current = await api("/api/v2/current");
    state.development = current.development === true; state.updateBlocked = current.update_blocked || null; state.scheduler = current.scheduler || null; state.historyScan = current.history_scan || null; state.historyScanError = current.history_scan_error || null;
    if (current.batch_id && !state.batchId) await loadBatch(current.batch_id, { useLatestDate: true });
    else if (current.batch_id && current.batch_id !== state.batchId) { state.readyBatch = current.batch_id; $("newBatchText").textContent = "新批次已就绪（" + dates(current.as_of) + "）。三页仍固定在当前批次。"; $("newBatch").hidden = false; }
    if (current.legacy) $("legacyLink").title = "旧版档案：" + human(current.legacy.state);
    if (current.job && ["QUEUED", "RUNNING"].includes(current.job.status) && current.job.job_id !== state.jobId && !state.pendingUpdateId) followJob(current.job);
    renderRuntime();
    return current;
  }
  function scheduleCurrentRead() {
    if (state.currentTimer) clearTimeout(state.currentTimer);
    state.currentTimer = setTimeout(async () => {
      try { if (document.visibilityState === "visible") await checkCurrent(); } catch (_) { /* An unavailable status read must not replace the pinned batch. */ }
      scheduleCurrentRead();
    }, 30000);
  }
  async function pollJob(id, generation) {
    if (generation !== state.jobGeneration) return;
    try {
      const response = await api("/api/v2/jobs/" + encodeURIComponent(id)), job = jobRecord(response);
      if (generation !== state.jobGeneration) return;
      if (!job || job.job_id !== id) throw new Error("作业状态暂不可用");
      state.jobFailures = 0;
      if (showJob(job)) state.jobTimer = setTimeout(() => pollJob(id, generation), 1500);
      else { state.jobId = null; await checkCurrent(); }
    } catch (error) {
      if (generation !== state.jobGeneration) return;
      state.jobFailures++; $("jobDetail").textContent = "进度读取暂不可达；不会重复发起作业。";
      if (state.jobFailures <= 4) state.jobTimer = setTimeout(() => pollJob(id, generation), Math.min(10000, 1500 * 2 ** state.jobFailures));
      else $("retryJob").hidden = false;
    }
  }
  function followJob(job) {
    if (!job) return;
    if (state.jobTimer) clearTimeout(state.jobTimer);
    state.jobId = job.job_id; state.jobGeneration++; state.jobFailures = 0; state.jobProgress = 0;
    const active = showJob(job);
    if (active) void pollJob(job.job_id, state.jobGeneration); else { state.jobId = null; void checkCurrent().catch(showError); }
  }
  async function submitJob(kind, body) {
    if (state.submitting) return;
    if (kind === "backfill" && state.historyScanError) { showError(new Error(historyUnavailableText())); return; }
    state.submitting = true; hideError(); renderRuntime();
    try {
      const job = jobRecord(await api("/api/v2/jobs/" + kind, { method: "POST", headers: { "Content-Type": "application/json", "X-Workbench-Nonce": state.nonce }, body: JSON.stringify(body) }));
      if (!job) throw new Error("作业响应缺少标识，请读取当前进度"); followJob(job);
    } catch (error) {
      // A lost POST response is not evidence that no job was created.
      try { const active = jobRecord(await api("/api/v2/jobs/active")); if (active) followJob(active); else showError(error); }
      catch (_) { showError(new Error("提交结果暂不明确。请恢复连接后读取状态；页面没有重复提交。")); }
    } finally { state.submitting = false; renderRuntime(); }
  }
  const changeFilter = () => { state.sort = { key: null, direction: "default" }; state.pageSorts = {}; state.qualitySequence++; void loadRows(); };
  function bind() {
    document.querySelectorAll("[data-page]").forEach(button => button.addEventListener("click", () => { state.pageSorts[state.page] = state.sort; state.page = button.dataset.page; state.sort = state.pageSorts[state.page] || { key: null, direction: "default" }; render(); }));
    $("mainTabs").addEventListener("keydown", event => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; const buttons = [...$("mainTabs").querySelectorAll("button")], index = buttons.findIndex(button => button.dataset.page === state.page); const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length; event.preventDefault(); buttons[next].click(); buttons[next].focus(); });
    document.querySelectorAll("[data-period]").forEach(button => button.addEventListener("click", () => { state.periodKind = button.dataset.period; updatePeriodInput(); changeFilter(); }));
    document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => { state.view = button.dataset.view; render(); }));
    document.querySelectorAll("[data-detail]").forEach(button => button.addEventListener("click", () => { state.detailTab = button.dataset.detail; state.memberSort = {key:null,direction:"default"}; void renderDetail(); }));
    wire("taxonomy", "change", () => { state.taxonomy = $("taxonomy").value; updateLevels(); changeFilter(); });
    wire("level", "change", () => { state.level = $("level").value; updateParents(); changeFilter(); });
    wire("parentIndustry", "change", () => { state.parentUid = $("parentIndustry").value; render(); });
    wire("qualityPanel", "toggle", () => { if ($("qualityPanel").open) void loadQuality(); });
    wire("periodDate", "change", () => { state.periodKey = periodKey(state.periodKind, $("periodDate").value); changeFilter(); });
    wire("search", "input", () => { state.query = $("search").value.trim().toLowerCase(); render(); });
    wire("resetSort", "click", () => { state.sort = { key: null, direction: "default" }; state.rankMetric = "return_5d"; render(); });
    wire("valuationSource", "change", () => { state.source = $("valuationSource").value; render(); });
    wire("toggleChart", "click", () => { state.chartVisible = !state.chartVisible; $("toggleChart").textContent = state.chartVisible ? "收起估值–资金散点图" : "展开估值–资金散点图"; $("toggleChart").setAttribute("aria-expanded", String(state.chartVisible)); render(); });
    wire("retryData", "click", () => { if (state.batchId) void loadRows(); else void checkCurrent().catch(showError); });
    wire("updateButton", "click", () => void submitJob("update", {})); wire("emptyUpdate", "click", () => void submitJob("update", {}));
    wire("switchBatch", "click", () => { if (state.readyBatch) void loadBatch(state.readyBatch).catch(showError); });
    wire("retryJob", "click", () => { state.jobFailures = 0; if (state.jobId) void pollJob(state.jobId, ++state.jobGeneration); else void api("/api/v2/jobs/active").then(response => followJob(jobRecord(response))).catch(showError); });
    wire("historyButton", "click", () => { const end = state.catalog && dates(state.catalog.as_of); if (end) { $("backfillEnd").value = end; $("backfillStart").value = state.catalog.history?.start ? dates(state.catalog.history.start) : end; } $("backfillRetryFailed").checked = false; $("backfillError").textContent = ""; renderBackfillStatus(); $("backfillDialog").showModal(); });
    wire("closeBackfill", "click", () => $("backfillDialog").close());
    for (const id of ["backfillStart", "backfillEnd"]) wire(id, "change", renderBackfillStatus);
    wire("backfillForm", "submit", event => { event.preventDefault(); if (state.historyScanError) { $("backfillError").textContent = historyUnavailableText(); return; } const start = $("backfillStart").value, end = $("backfillEnd").value; if (!start || !end || start > end) { $("backfillError").textContent = "请填写有效起止日期，开始日期不得晚于结束日期。"; return; } $("backfillError").textContent = ""; $("backfillDialog").close(); void submitJob("backfill", { start_date: start.replaceAll("-", ""), end_date: end.replaceAll("-", ""), retry_failed: $("backfillRetryFailed").checked === true }); });
    wire("closeDetail", "click", () => { state.detailSequence++; $("detailDialog").close(); });
    $("detailDialog").addEventListener("cancel", () => { state.detailSequence++; });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") void checkCurrent().catch(() => {}); });
  }
  async function start() {
    if (location.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(location.hostname)) { $("guardText").textContent = "请在 Chrome 中访问本机 HTTP 地址。直接打开文件不会连接数据。"; return; }
    try {
      const bootstrap = await api("/api/v2/bootstrap");
      if (!bootstrap || typeof bootstrap.nonce !== "string") throw new Error("缺少本地会话信息");
      state.nonce = bootstrap.nonce; state.taxonomy = bootstrap.default_taxonomy || "SW"; state.level = bootstrap.default_level || "L1";
      $("taxonomy").value = state.taxonomy; bind(); $("serviceGuard").hidden = true; $("app").hidden = false;
      const current = await checkCurrent();
      if (!current.batch_id) { $("emptyState").hidden = false; $("dataArea").hidden = true; render(); }
      const job = current.job || jobRecord(await api("/api/v2/jobs/active")); if (job && job.job_id !== state.jobId) followJob(job);
      scheduleCurrentRead();
    } catch (error) { if ($("app").hidden) $("guardText").textContent = error.message + "。请检查本地服务是否已启动。"; else showError(error); }
  }
  if (window.__WORKBENCH_TEST_HARNESS__ === true) window.__WORKBENCH_TEST__ = { ...core, state, start, loadRows, loadBatch, render, renderTable, lineChart, checkCurrent, followJob, submitJob, openDetail, loadQuality, updateParents, visibleRows };
  void start();
})();
