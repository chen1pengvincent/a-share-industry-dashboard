"use strict";
// No browser or provider is replaced in production. This VM is test-only.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
const core = require(path.join(root, "web/app.js"));
const cases = [];
function test(name, fn) { cases.push([name, fn]); }
function equal(actual, expected) { assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected); }

class Node {
  constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.dataset = {}; this.style = {}; this.listeners = {}; this._text = ""; this.hidden = false; this.value = ""; this.checked = false; this.scrollLeft = 0; this.scrollTop = 0; this.open = false; this.className = ""; const validTokens = args => { if (args.some(value => value === "" || /\s/.test(value))) throw new Error("Invalid DOMTokenList token"); }; this.classList = { add: (...args) => { validTokens(args); args.forEach(value => { if (!this.className.split(" ").includes(value)) this.className += " " + value; }); }, remove: (...args) => { validTokens(args); this.className = this.className.split(" ").filter(value => !args.includes(value)).join(" "); } }; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("Untrusted HTML insertion is forbidden in new UI"); }
  append(...nodes) { nodes.forEach(node => { if (typeof node === "string") { const text = new Node("text"); text.textContent = node; node = text; } node.parentNode = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this._text = ""; this.children = []; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(event, handler) { (this.listeners[event] ||= []).push(handler); }
  dispatch(event, fields = {}) { const data = { target: this, preventDefault() {}, ...fields }; (this.listeners[event] || []).forEach(handler => handler(data)); }
  click() { this.dispatch("click"); }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  matches(selector) { if (selector === "button") return this.tagName === "BUTTON"; const match = /^\[data-([\w-]+)(?:="([^"]+)")?\]$/.exec(selector); if (!match) return false; const key = match[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()); return match[2] === undefined ? key in this.dataset : this.dataset[key] === match[2]; }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function harness() {
  const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8"), nodes = new Map(), buttons = [];
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*)>/g)) {
    const [, tag, attrs] = match, id = /\bid="([^"]+)"/.exec(attrs)?.[1];
    if (!id && tag !== "button") continue;
    const node = new Node(tag); node.id = id || "";
    if (id) nodes.set(id, node);
    node.hidden = /\bhidden\b/.test(attrs);
    for (const attr of attrs.matchAll(/\b(data-[\w-]+|aria-[\w-]+)="([^"]*)"/g)) node.setAttribute(attr[1], attr[2]);
    if (tag === "button") buttons.push(node);
  }
  nodes.get("mainTabs").append(...buttons.filter(node => node.dataset.page));
  const doc = { getElementById: id => { if (!nodes.has(id)) throw new Error("Missing DOM ID " + id); return nodes.get(id); }, createElement: tag => new Node(tag), createElementNS: (_ns, tag) => new Node(tag), querySelectorAll: selector => buttons.filter(node => node.matches(selector)), visibilityState: "visible", listeners: {}, addEventListener(event, callback) { this.listeners[event] = callback; } };
  const requests = [], timers = new Map(); let timerId = 0;
  const h = { nodes, doc, buttons, requests, timers, current: { batch_id: "B1", as_of: "20260916", legacy: { state: "NOT_INSTALLED" } }, interceptor: null };
  h.catalog = batch => ({ batch_id: batch, as_of: "20260916", taxonomies: [{ id: "SW", name: "申万", levels: [{ id: "L1", name: "一级行业" }, { id: "L2", name: "二级行业" }] }, { id: "TDX", name: "通达信", levels: [{ id: "880", name: "880 系列" }] }], trade_dates: ["20260915", "20260916"], history: {}, publication_state: "DEVELOPMENT" });
  h.rows = [{ uid: "SW:L1:2", name: "<img src=x onerror=alert(1)>", name_sort_key: "b", code: "2", counts: { member_count: 6, pe_valid: 6, pb_valid: 5 }, metrics: { flow_cent: { value: "900719925474099301", status: "OK" }, pe_ttm_median: { value: "10", status: "OK" }, pb_median: { value: "2", status: "OK" }, pe_percentile: { value: "20", status: "OK" } }, status: "OK" }, { uid: "SW:L1:1", name: "银行", name_sort_key: "yin hang", code: "1", counts: {}, metrics: { flow_cent: { value: null, status: "NA" }, pe_ttm_median: { value: "2", status: "SMALL_SAMPLE" } }, status: "HISTORY_UNAVAILABLE" }];
  const fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (h.interceptor) { const result = await h.interceptor(url, options); if (result !== undefined) return result; }
    let body;
    if (url === "/api/v2/bootstrap") body = { nonce: "CANARY_READ_ONLY_TEST", default_taxonomy: "SW", default_level: "L1" };
    else if (url === "/api/v2/current") body = h.current;
    else if (url === "/api/v2/jobs/active") body = { job: null };
    else if (/\/catalog$/.test(url)) body = h.catalog(url.split("/")[4]);
    else if (url.includes("/industries?")) body = { batch_id: url.split("/")[4], as_of: "20260916", rows: h.rows, period: { start: "20260916", end: "20260916", endpoint: "20260916", expected_days: 1, available_days: 1, status: "COMPLETE" } };
    else if (url.includes("/members?")) body = { batch_id: "B1", uid: decodeURIComponent(url.split("/")[6]), rows: [], period: {} };
    else if (url.includes("/history?")) body = { batch_id: "B1", uid: decodeURIComponent(url.split("/")[6]), rows: [] };
    else throw new Error("Unexpected URL " + url);
    return { ok: true, status: 200, json: async () => body };
  };
  const window = { __WORKBENCH_TEST_HARNESS__: true };
  vm.runInNewContext(source, { document: doc, window, location: { protocol: "http:", hostname: "127.0.0.1", origin: "http://127.0.0.1:8765" }, fetch, URL, URLSearchParams, console, setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; }, clearTimeout(id) { timers.delete(id); } }, { filename: "web/app.js" });
  h.ui = window.__WORKBENCH_TEST__; return h;
}
async function settle() { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); }
const ok = body => ({ ok: true, status: 200, json: async () => body });

test("exact decimals preserve large financial order and display units", () => {
  assert.equal(core.compareDecimal("900719925474099301", "900719925474099302"), -1);
  assert.equal(core.compareDecimal("-1.20", "-1.2"), 0);
  assert.equal(core.formatDecimal("123456789", 2, 6), "123.46");
  assert.equal(core.formatDecimal("-1", 2, 6), "0.00");
  assert.equal(core.formatDecimal(null), "—");
  assert.equal(core.decimal("NaN"), null); assert.equal(core.decimal(true), null);
});
test("three sort states restore default; null stays last in both directions", () => {
  const rows = [{ uid: "z", x: null }, { uid: "b", x: "900719925474099302" }, { uid: "a", x: "900719925474099301" }], cols = [{ key: "x", value: row => row.x }];
  let sort = { key: null, direction: "default" };
  sort = core.nextSort(sort, "x"); equal(core.sortRows(rows, sort, cols).map(row => row.uid), ["a", "b", "z"]);
  sort = core.nextSort(sort, "x"); equal(core.sortRows(rows, sort, cols).map(row => row.uid), ["b", "a", "z"]);
  sort = core.nextSort(sort, "x"); equal(core.sortRows(rows, sort, cols).map(row => row.uid), ["z", "b", "a"]);
  assert.equal(sort.direction, "default");
});
test("each page uses its specified default and pinyin name order", () => {
  const rows = [{ uid: "a", code: "2", name: "银行", name_sort_key: "yin hang", metrics: { flow_cent: { value: "10" } } }, { uid: "b", code: "1", name: "传媒", name_sort_key: "chuan mei", metrics: { flow_cent: { value: null } } }];
  equal(core.defaultRows(rows, "valuation").map(row => row.uid), ["b", "a"]);
  equal(core.defaultRows(rows, "moneyflow").map(row => row.uid), ["a", "b"]);
  equal(core.defaultRows(rows, "fusion").map(row => row.uid), ["b", "a"]);
});
test("main tables render untrusted text safely and header cycles work", async () => {
  const h = harness(); await settle();
  assert.equal(h.ui.state.batchId, "B1"); assert.equal(h.nodes.get("app").hidden, false);
  const table = h.nodes.get("industryTable"); assert.match(table.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(table.querySelectorAll("img").length, 0);
  for (const direction of ["asc", "desc", "default"]) { table.querySelector('[data-sort="flow_cent"]').click(); assert.equal(h.ui.state.sort.direction, direction); }
  const before = h.requests.length;
  h.buttons.find(button => button.dataset.page === "moneyflow").click();
  assert.equal(h.ui.state.batchId, "B1"); assert.equal(h.requests.length, before);
});
test("late filter responses cannot overwrite the selected period", async () => {
  const h = harness(); await settle(); const pending = [];
  h.interceptor = async url => url.includes("/industries?") ? new Promise(resolve => pending.push(resolve)) : undefined;
  h.ui.state.periodKey = "20260915"; const first = h.ui.loadRows();
  h.ui.state.periodKey = "20260916"; const second = h.ui.loadRows();
  await settle(); assert.equal(pending.length, 2);
  pending[1](ok({ batch_id: "B1", rows: [{ uid: "new", name: "最新筛选", code: "1", metrics: {} }], period: {} })); await second;
  pending[0](ok({ batch_id: "B1", rows: [{ uid: "old", name: "过期响应", code: "0", metrics: {} }], period: {} })); await first;
  assert.equal(h.ui.state.rows[0].uid, "new");
});
test("new publication only offers an explicit unified batch switch", async () => {
  const h = harness(); await settle(); h.current = { batch_id: "B2", as_of: "20260917" };
  await h.ui.checkCurrent(); assert.equal(h.ui.state.batchId, "B1"); assert.equal(h.ui.state.readyBatch, "B2"); assert.equal(h.nodes.get("newBatch").hidden, false);
  h.nodes.get("switchBatch").click(); await settle(); assert.equal(h.ui.state.batchId, "B2");
});
test("history line preserves missing periods and breaks the path", async () => {
  const h = harness(); await settle(); const target = new Node();
  h.ui.lineChart(target, [{ trade_date: "20260914", metrics: { pb_median: { value: "1" } } }, { trade_date: "20260915", metrics: { pb_median: { value: null } } }, { trade_date: "20260916", metrics: { pb_median: { value: "3" } } }], "pb_median");
  const svg = target.children[0], plot = svg.children.find(node => node.tagName === "PATH");
  assert.equal((plot.attributes.d.match(/ M /g) || []).length, 2); assert.equal((plot.attributes.d.match(/ L /g) || []).length, 0);
  assert.match(svg.textContent, /2026-09-15/);
});
test("lost POST result recovers the existing job without second submission", async () => {
  const h = harness(); await settle(); let posts = 0;
  const job = { job_id: "J1", kind: "update", status: "RUNNING", phase: "FETCH", completed_units: 1, total_units: 5 };
  h.interceptor = async (url, options) => { if (options.method === "POST") { posts++; throw new Error("lost response"); } if (url === "/api/v2/jobs/active" || url === "/api/v2/jobs/J1") return ok(job); return undefined; };
  await h.ui.submitJob("update", {}); await settle(); assert.equal(posts, 1); assert.equal(h.ui.state.jobId, "J1"); assert.equal(h.ui.state.batchId, "B1");
});
test("CSV exports fixed batch filter and exact sort context", async () => {
  const h = harness(); await settle(); let payload;
  h.interceptor = async (url, options) => { if (url === "/api/v2/jobs/export") { payload = JSON.parse(options.body); return ok({ job_id: "E1", kind: "export", status: "SUCCEEDED", result: {} }); } return undefined; };
  h.ui.state.query = "银行"; h.ui.state.sort = { key: "flow_cent", direction: "desc" }; h.nodes.get("exportCsv").click(); await settle();
  equal({ batch: payload.batch_id, query: payload.query, key: payload.sort_key, direction: payload.sort_direction, page: payload.page }, { batch: "B1", query: "银行", key: "flow_cent", direction: "desc", page: "fusion" });
});
test("same-day update completion clearly says already current without 1 over zero", async () => {
  const h = harness(); await settle();
  h.ui.followJob({ job_id: "U1", kind: "update", status: "SUCCEEDED", message: "已完成", completed_units: 1, total_units: 0,
    result: { already_current: true, as_of: "20260917", batch_id: "B1" } }); await settle();
  assert.equal(h.nodes.get("jobDetail").textContent, "已是最新（数据至 2026-09-17）");
  assert.equal(h.nodes.get("jobProgress").value, 100);
  assert.equal(h.ui.state.batchId, "B1");
});
test("export completion with no total hides zero over zero and keeps download ready", async () => {
  const h = harness(); await settle();
  h.ui.followJob({ job_id: "E1", kind: "export", status: "SUCCEEDED", message: "已完成", completed_units: 0, total_units: 0,
    result: { download_url: "/api/v2/exports/fixture.csv" } }); await settle();
  assert.equal(h.nodes.get("jobDetail").textContent, "已完成");
  assert.equal(h.nodes.get("jobProgress").value, 100);
  assert.equal(h.nodes.get("downloadLink").hidden, false);
});
test("successful completion hides stale zero over one verification phase counts", async () => {
  const h = harness(); await settle();
  h.ui.followJob({ job_id: "U1", kind: "update", status: "SUCCEEDED", message: "已完成", phase: "COMPLETE", completed_units: 0, total_units: 1, result: {} }); await settle();
  assert.equal(h.nodes.get("jobDetail").textContent, "已完成");
  assert.equal(h.nodes.get("jobProgress").value, 100);
});
test("a normal quantified job phase retains its count and progress", async () => {
  const h = harness(); await settle();
  const job = { job_id: "U1", kind: "update", status: "RUNNING", message: "校验数据引用闭包", completed_units: 2, total_units: 5 };
  h.interceptor = async url => url === "/api/v2/jobs/U1" ? ok(job) : undefined;
  h.ui.followJob(job); await settle();
  assert.equal(h.nodes.get("jobDetail").textContent, "校验数据引用闭包 · 2 / 5");
  assert.equal(h.nodes.get("jobProgress").value, 40);
});
test("API return percentages are not multiplied a second time", async () => {
  const h = harness(); h.rows[0].metrics.return_5d = { value: "1.23", status: "OK" }; await settle();
  h.buttons.find(button => button.dataset.page === "valuation").click();
  assert.match(h.nodes.get("industryTable").textContent, /1\.23%/);
  assert.doesNotMatch(h.nodes.get("industryTable").textContent, /123\.00%/);
});
test("true zero returns and moneyflow render with strict native class tokens", async () => {
  const h = harness();
  for (const key of ["flow_cent", "return_5d", "return_mtd", "return_ytd"]) h.rows[0].metrics[key] = { value: "0", status: "OK" };
  await settle();
  assert.equal(h.nodes.get("errorPanel").hidden, true);
  assert.match(h.nodes.get("industryTable").textContent, /0\.00/);
  for (const page of ["valuation", "moneyflow", "fusion"]) {
    h.buttons.find(button => button.dataset.page === page).click();
    assert.equal(h.nodes.get("errorPanel").hidden, true);
    assert.equal(h.ui.state.rows.length, 2);
    assert.equal(h.nodes.get("exportCsv").disabled, false);
    if (page === "valuation") assert.match(h.nodes.get("industryTable").textContent, /0\.00%/);
  }
  assert.throws(() => new Node().classList.add(""), /Invalid DOMTokenList/);
});
test("parent filter applies before sorting and is retained in CSV request", async () => {
  const h = harness(); await settle();
  h.ui.state.catalog.industries = [{ uid: "parent1", taxonomy: "SW", level: "L1", name: "银行", code: "1", name_sort_key: "yin hang" }, { uid: "parent2", taxonomy: "SW", level: "L1", name: "传媒", code: "2", name_sort_key: "chuan mei" }];
  h.ui.state.level = "L2"; h.ui.state.rows[0].parent_uid = "parent1"; h.ui.state.rows[1].parent_uid = "parent2";
  h.ui.updateParents(); assert.equal(h.nodes.get("parentFilter").hidden, false);
  h.nodes.get("parentIndustry").value = "parent1"; h.nodes.get("parentIndustry").dispatch("change");
  assert.equal(h.ui.visibleRows().length, 1); assert.equal(h.ui.visibleRows()[0].uid, "SW:L1:2");
  let payload;
  h.interceptor = async (url, options) => { if (url === "/api/v2/jobs/export") { payload = JSON.parse(options.body); return ok({ job_id: "E1", status: "SUCCEEDED", kind: "export", result: {} }); } return undefined; };
  h.nodes.get("exportCsv").click(); await settle(); assert.equal(payload.parent_uid, "parent1");
  h.ui.state.taxonomy = "TDX"; h.ui.state.level = "880"; h.ui.updateParents();
  assert.equal(h.ui.state.parentUid, ""); assert.equal(h.nodes.get("parentFilter").hidden, true);
});
test("quality shows unknown stock evidence and missing days with tri-state sorting", async () => {
  const h = harness(); await settle();
  h.interceptor = async url => url.includes("/quality?") ? ok({ batch_id: "B1", missing_dates: ["20260915"], days: [{ trade_date: "20260916", audit: { classification_coverage: [{ taxonomy: "SW", level: "L1", complete: false, market_flow_cent: "1000", known_unique_flow_cent: "300", unknown_flow_cent: "700", unknown_stocks: [{ ts_code: "600002.SH", flow_cent: "900719925474099302" }, { ts_code: "600001.SH", flow_cent: "900719925474099301" }] }] } }] }) : undefined;
  h.nodes.get("qualityPanel").open = true; h.nodes.get("qualityPanel").dispatch("toggle"); await settle();
  assert.match(h.nodes.get("qualityContent").textContent, /600001\.SH/);
  assert.match(h.nodes.get("qualityContent").textContent, /必需交易日缺失/);
  assert.match(h.nodes.get("qualityContent").textContent, /该日成员证据无法确认有效归属/);
  const content = h.nodes.get("qualityContent");
  for (const direction of ["asc", "desc", "default"]) { content.querySelector('[data-sort="flow_cent"]').click(); assert.equal(h.ui.state.unknownSort.direction, direction); }
  assert.equal(h.requests.filter(request => request.url.includes("/quality?")).length, 1);
});
test("background current reads are read-only and preserve the selected batch", async () => {
  const h = harness(); await settle(); h.current = { batch_id: "B2", as_of: "20260917", development: true, update_blocked: { code: "BLOCKED", message: "源码需先审阅" }, scheduler: { enabled: false } };
  const timer = [...h.timers.values()].find(item => item.ms === 30000); assert.ok(timer);
  await timer.callback(); await settle();
  assert.equal(h.ui.state.batchId, "B1"); assert.equal(h.ui.state.readyBatch, "B2");
  assert.match(h.nodes.get("batchState").textContent, /开发验收/); assert.match(h.nodes.get("runtimeNotice").textContent, /源码需先审阅/);
  assert.equal(h.nodes.get("updateButton").disabled, true);
  assert.equal(h.requests.filter(request => request.options.method === "POST").length, 0);
});
test("each primary page remembers its own header sort", async () => {
  const h = harness(); await settle(); h.nodes.get("industryTable").querySelector('[data-sort="flow_cent"]').click();
  h.buttons.find(button => button.dataset.page === "valuation").click(); h.nodes.get("industryTable").querySelector('[data-sort="pb_median"]').click();
  h.buttons.find(button => button.dataset.page === "fusion").click();
  equal(h.ui.state.sort, { key: "flow_cent", direction: "asc" });
});
test("moneyflow detail defaults to contribution order; member view restores stock code order", async () => {
  const h = harness(); await settle();
  h.interceptor = async url => url.includes("/members?") ? ok({ batch_id: "B1", uid: h.rows[0].uid, rows: [{ uid: h.rows[0].uid, ts_code: "600001.SH", name: "股票甲", flow_cent: "100", is_endpoint_member: true }, { uid: h.rows[0].uid, ts_code: "600002.SH", name: "股票乙", flow_cent: "200", is_endpoint_member: true }] }) : undefined;
  h.ui.state.page = "moneyflow"; await h.ui.openDetail(h.rows[0]);
  assert.equal(h.ui.state.detailTab, "contribution");
  let text = h.nodes.get("detailContent").textContent; assert.ok(text.indexOf("股票乙") < text.indexOf("股票甲"));
  h.buttons.find(button => button.dataset.detail === "members").click(); await settle();
  text = h.nodes.get("detailContent").textContent; assert.ok(text.indexOf("股票甲") < text.indexOf("股票乙"));
  assert.match(text, /期末 PE_TTM/);
});
test("manual daily update can queue during backfill and is then deduplicated in the UI", async () => {
  const h = harness(); await settle();
  const backfill = { job_id: "H1", kind: "backfill", status: "RUNNING", total_units: 3, completed_units: 1 };
  const update = { job_id: "U1", kind: "update", status: "QUEUED", total_units: 0, completed_units: 0 };
  h.interceptor = async (url, options) => { if (url === "/api/v2/jobs/H1") return ok(backfill); if (url === "/api/v2/jobs/U1" || (url === "/api/v2/jobs/update" && options.method === "POST")) return ok(update); return undefined; };
  h.ui.followJob(backfill); await settle(); assert.equal(h.nodes.get("updateButton").disabled, false);
  assert.equal(h.nodes.get("historyButton").disabled, true);
  await h.ui.submitJob("update", {}); await settle(); assert.equal(h.nodes.get("updateButton").disabled, true);
  h.current.job = backfill; await h.ui.checkCurrent();
  assert.equal(h.ui.state.jobId, "U1"); assert.equal(h.ui.state.pendingUpdateId, "U1");
});
test("all six valuation views render safely with sparse history and retain the data table", async () => {
  const h = harness(); await settle(); h.buttons.find(button => button.dataset.page === "valuation").click();
  for (const view of ["overview", "heatmap", "trend", "ranking", "scatter", "detail"]) {
    h.buttons.find(button => button.dataset.view === view).click(); await settle();
    assert.equal(h.ui.state.view, view); assert.equal(h.nodes.get("tablePanel").hidden, false);
    assert.equal(h.nodes.get("visualPanel").hidden, view === "detail");
    assert.equal(h.nodes.get("errorPanel").hidden, true);
  }
  h.buttons.find(button => button.dataset.view === "trend").click(); await settle();
  assert.match(h.nodes.get("chartControls").textContent, /官方指数收盘点位/);
});
test("known contribution is independently sortable and never fills an unknown complete amount", async () => {
  const h = harness(); await settle();
  const rows = [
    { ts_code: "600001.SH", name: "甲", flow_cent: null, known_subtotal: "0" },
    { ts_code: "600002.SH", name: "乙", flow_cent: null, known_subtotal: "900719925474099301" },
    { ts_code: "600003.SH", name: "丙", flow_cent: null, known_subtotal: null },
    { ts_code: "600004.SH", name: "丁", flow_cent: "0", known_subtotal: "0" }
  ].map(row => ({ ...row, uid: h.rows[0].uid, is_endpoint_member: true }));
  h.interceptor = async url => url.includes("/members?") ? ok({ batch_id: "B1", uid: h.rows[0].uid, rows }) : undefined;
  h.ui.state.page = "moneyflow"; await h.ui.openDetail(h.rows[0]);
  const tableRows = () => h.nodes.get("detailContent").children.find(node => node.className.includes("table-scroll")).children[0].children.find(node => node.tagName === "TBODY").children;
  const names = () => tableRows().map(row => row.children[0].textContent.slice(0, 1));
  equal(names(), ["丁", "甲", "乙", "丙"]);
  assert.equal(tableRows()[1].children[1].textContent, "—");
  assert.equal(tableRows()[1].children[2].textContent, "0.00");
  assert.equal(tableRows()[3].children[2].textContent, "—");
  assert.match(h.nodes.get("detailContent").textContent, /不代表未知部分为零/);
  for (const expected of [["甲", "丁", "乙", "丙"], ["乙", "甲", "丁", "丙"], ["丁", "甲", "乙", "丙"]]) {
    h.nodes.get("detailContent").querySelector('[data-sort="known_subtotal"]').click(); equal(names(), expected);
  }
  h.buttons.find(button => button.dataset.detail === "members").click(); await settle();
  assert.ok(h.nodes.get("detailContent").querySelector('[data-sort="known_subtotal"]'));
  equal(names(), ["甲", "乙", "丙", "丁"]);
});
test("availability ranges use the current scope and disclose industry-day sample counts", async () => {
  const h = harness(); await settle();
  h.ui.state.catalog.history.availability_by_scope = [
    { taxonomy: "CI", level: "L1", metrics: { pe_ttm_median: { first_valid_date: "19990101", last_valid_date: "19990102", valid_industry_days: 999 } } },
    { taxonomy: "SW", level: "L1", metrics: { pe_ttm_median: { first_valid_date: "20250915", last_valid_date: "20260916", valid_industry_days: 600, missing_industry_days: 20 }, flow_cent: { first_valid_date: null, last_valid_date: null, valid_industry_days: 0, missing_industry_days: 620 } } }
  ];
  h.interceptor = async url => url.includes("/quality?") ? ok({ batch_id: "B1", days: [], missing_dates: [] }) : undefined;
  await h.ui.loadQuality();
  const content = h.nodes.get("qualityContent");
  assert.match(content.textContent, /行业 × 交易日/); assert.match(content.textContent, /不是整个分类有效的交易日数/);
  assert.match(content.textContent, /2025-09-15/); assert.doesNotMatch(content.textContent, /1999-01-01/);
  assert.match(content.textContent, /600/); assert.match(content.textContent, /620/);
  for (const direction of ["asc", "desc", "default"]) { content.querySelector('[data-sort="valid_industry_days"]').click(); assert.equal(h.ui.state.availabilitySort.direction, direction); }
});
test("history retry defaults to false, is explicit per submission and resets on reopen", async () => {
  const h = harness(); await settle(); const payloads = [];
  h.interceptor = async (url, options) => {
    if (url === "/api/v2/jobs/backfill") {
      payloads.push(JSON.parse(options.body));
      return ok({ job_id: "H" + payloads.length, kind: "backfill", status: "SUCCEEDED", phase: "COMPLETE", completed_units: 1, total_units: 1,
        result: { attempted_days: ["20260916"], captured_days: ["20260916"], blocked_dates: [], pending_dates: [], remaining_attemptable_days: 0, scan_complete: true, history_complete: true } });
    }
    return undefined;
  };
  h.nodes.get("historyButton").click();
  assert.equal(h.nodes.get("backfillRetryFailed").checked, false);
  h.nodes.get("backfillForm").dispatch("submit"); await settle();
  equal(payloads[0], { start_date: "20260916", end_date: "20260916", retry_failed: false });
  assert.equal(h.ui.state.jobId, null); assert.equal(h.nodes.get("historyButton").disabled, false);
  assert.match(h.nodes.get("jobDetail").textContent, /各分类指标仍可能缺少历史证据/);
  h.nodes.get("historyButton").click(); h.nodes.get("backfillRetryFailed").checked = true;
  h.nodes.get("backfillForm").dispatch("submit"); await settle();
  assert.equal(payloads[1].retry_failed, true);
  h.nodes.get("historyButton").click(); assert.equal(h.nodes.get("backfillRetryFailed").checked, false);
  assert.equal(payloads.length, 2);
});
test("current history scan works without scheduler and failed-date table filters safely", async () => {
  const h = harness(); await settle();
  const gap = trade_date => ({ trade_date, code: "INDEPENDENT_UNIVERSE_INCOMPLETE", reason: "<img src=x onerror=alert(1)> 未认定永久不可恢复" });
  h.current.history_scan = { blocked_dates: [gap("20260901"), gap("20260915"), gap("20260916")], pending_dates: ["20260902", "20260914"], remaining_attemptable_days: 2, history_complete: false };
  h.current.scheduler = { enabled: false, history_gaps: { blocked_dates: [], pending_dates: [], remaining_attemptable_days: 0 } };
  h.ui.state.catalog.history.start = "20260901";
  await h.ui.checkCurrent();
  assert.match(h.nodes.get("runtimeNotice").textContent, /历史有 3 个失败日期，另有 2 日待尝试/);
  h.nodes.get("historyButton").click();
  h.nodes.get("backfillStart").value = "2026-09-14"; h.nodes.get("backfillStart").dispatch("change");
  assert.match(h.nodes.get("backfillStatus").textContent, /2 个已失败日期；1 日待尝试/);
  const table = h.nodes.get("backfillGapTable");
  assert.doesNotMatch(table.textContent, /2026-09-01/);
  assert.match(table.textContent, /股票范围与交易、停牌证据不一致/);
  assert.match(table.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(table.querySelectorAll("img").length, 0);
  for (const direction of ["asc", "desc", "default"]) { table.querySelector('[data-sort="trade_date"]').click(); assert.equal(h.ui.state.historyGapSort.direction, direction); }
  assert.equal(h.requests.filter(request => request.options.method === "POST").length, 0);
});
test("history state failure cannot fall back to stale success or submit a backfill", async () => {
  const h = harness(); await settle();
  h.nodes.get("historyButton").click();
  const stale = { blocked_dates: [], pending_dates: [], remaining_attemptable_days: 0, scan_complete: true, history_complete: true };
  h.ui.state.lastHistoryResult = stale;
  h.current.scheduler = { enabled: false, history_gaps: stale };
  h.current.history_scan = null;
  h.current.history_scan_error = { code: "HISTORY_STATE_INVALID", message: "历史失败记录损坏" };
  h.current.update_blocked = h.current.history_scan_error;
  await h.ui.checkCurrent();
  assert.match(h.nodes.get("runtimeNotice").textContent, /历史状态未知.*请先修复/);
  assert.doesNotMatch(h.nodes.get("runtimeNotice").textContent, /手动更新仍可用/);
  assert.match(h.nodes.get("backfillStatus").textContent, /历史状态未知/);
  assert.doesNotMatch(h.nodes.get("backfillStatus").textContent, /0 个已失败日期|0 日待尝试/);
  assert.equal(h.nodes.get("backfillGapTable").textContent, "");
  assert.equal(h.nodes.get("historyButton").disabled, true);
  h.nodes.get("backfillForm").dispatch("submit");
  await h.ui.submitJob("backfill", { start_date: "20260916", end_date: "20260916", retry_failed: true });
  assert.match(h.nodes.get("backfillError").textContent, /回填已停止/);
  assert.equal(h.requests.filter(request => request.options.method === "POST").length, 0);
  for (const page of ["valuation", "moneyflow", "fusion"]) {
    h.buttons.find(button => button.dataset.page === page).click();
    assert.equal(h.ui.state.batchId, "B1"); assert.match(h.nodes.get("industryTable").textContent, /银行/);
  }
  h.current.history_scan_error = null; h.current.update_blocked = null; h.current.history_scan = stale;
  await h.ui.checkCurrent();
  assert.equal(h.nodes.get("historyButton").disabled, false);
  assert.doesNotMatch(h.nodes.get("backfillStatus").textContent, /历史状态未知/);
});
test("scan completion with failed dates is never shown as full history success", async () => {
  const h = harness(); await settle();
  const result = { attempted_days: ["20260915", "20260916"], captured_days: ["20260916"], blocked_dates: [{ trade_date: "20260915", code: "INDEPENDENT_UNIVERSE_INCOMPLETE" }], pending_dates: [], remaining_attemptable_days: 0, remaining_days: 1, scan_complete: true, history_complete: false };
  h.ui.followJob({ job_id: "H1", kind: "backfill", status: "FAILED", phase: "COMPLETE_WITH_GAPS", completed_units: 2, total_units: 2, error: { code: "HISTORY_INCOMPLETE" }, result }); await settle();
  assert.equal(h.nodes.get("jobTitle").textContent, "历史补齐仍有缺口");
  assert.match(h.nodes.get("jobDetail").textContent, /已尝试 2 \/ 2 日.*本次已获取 1 日.*失败 1 日.*待尝试 0 日.*历史未完整/);
  assert.equal(h.nodes.get("jobProgress").value, 100);
  assert.match(h.nodes.get("jobProgress").getAttribute("aria-label"), /不代表历史数据完整/);
  assert.equal(h.ui.state.jobId, null); assert.equal(h.nodes.get("historyButton").disabled, false);
  assert.equal(h.ui.state.batchId, "B1"); equal(h.ui.state.lastHistoryResult, result);
  assert.match(h.nodes.get("runtimeNotice").textContent, /1 个失败日期/);
});
test("bounded success and global failure preserve their different unfinished states", async () => {
  const h = harness(); await settle();
  const result = { attempted_days: ["20260915"], captured_days: ["20260915"], blocked_dates: [], pending_dates: ["20260916"], remaining_attemptable_days: 1, scan_complete: false, history_complete: false };
  h.ui.followJob({ job_id: "H1", kind: "backfill", status: "SUCCEEDED", phase: "CHUNK_COMPLETE", completed_units: 1, total_units: 2, result }); await settle();
  assert.equal(h.nodes.get("jobTitle").textContent, "本次历史补齐已结束");
  assert.match(h.nodes.get("jobDetail").textContent, /历史仍未完整/); assert.equal(h.nodes.get("jobProgress").value, 50);
  h.ui.followJob({ job_id: "H2", kind: "backfill", status: "FAILED", error: { code: "TEST_GLOBAL_ERROR" }, result: { ...result, blocked_dates: [{ trade_date: "20260901", code: "INDEPENDENT_UNIVERSE_INCOMPLETE" }] } }); await settle();
  assert.match(h.nodes.get("jobDetail").textContent, /作业已停止：TEST_GLOBAL_ERROR/);
});
test("member and record counts explicitly disclose confirmed endpoint scope", async () => {
  const h = harness(); await settle();
  for (const page of ["valuation", "moneyflow", "fusion"]) {
    h.buttons.find(button => button.dataset.page === page).click();
    const header = h.nodes.get("industryTable").querySelector('[data-sort="member_count"]');
    assert.match(header.textContent, /已确认成员/); assert.match(header.title, /历史缺口时不代表行业实际成员总数/);
    if (page === "moneyflow") {
      for (const [key, title] of [["flow_expected", "期末应有记录"], ["flow_received", "期末已取记录"]]) {
        const count = h.nodes.get("industryTable").querySelector('[data-sort="' + key + '"]');
        assert.match(count.textContent, new RegExp(title)); assert.match(count.title, /不是整个周期的日×股记录总数/);
      }
    }
  }
});
(async () => {
  let failed = 0;
  for (const [name, fn] of cases) { try { await fn(); console.log("PASS " + name); } catch (error) { failed++; console.error("FAIL " + name + "\n" + error.stack); } }
  console.log(JSON.stringify({ tests: cases.length, passed: cases.length - failed, failed, provider: "TEST_ONLY_IN_MEMORY" }));
  if (failed) process.exitCode = 1;
})();
