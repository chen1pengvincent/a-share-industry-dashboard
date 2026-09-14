"use strict";

// Execute the shipped browser script with a tiny DOM and fake HTTP/timers.
// No browser dependency, network call, data directory, or production test hook.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const projectRoot = process.argv[2];
const scenario = process.argv[3];
const source = fs.readFileSync(path.join(projectRoot, "web", "app.js"), "utf8");
const html = fs.readFileSync(path.join(projectRoot, "web", "index.html"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(tag = "div") {
    this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {};
    this.listeners = {}; this.value = ""; this.textContent = ""; this.innerHTML = "";
    this.hidden = false; this.disabled = false; this.namespaceURI = "http://www.w3.org/2000/svg";
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  appendChild(child) {
    this.children.push(child);
    if (this.tag === "select" && this.children.length === 1) this.value = child.value;
    return child;
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children = []; if (this.tag === "select") this.value = ""; this.append(...children); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type, event = {}) {
    for (const listener of this.listeners[type] || []) await listener({ target: this, preventDefault() {}, ...event });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || new Element(selector); }
  querySelectorAll(selector) {
    if (selector === "button[data-sort]") {
      return [...this.innerHTML.matchAll(/<button[^>]+data-sort="([^"]+)"[^>]*>/g)].map(match => {
        const button = new Element("button"); button.dataset.sort = match[1]; return button;
      });
    }
    return [];
  }
  focus() {}
}

function harness({ boot = {}, fetcher } = {}) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) {
      const element = new Element(["trendIndustry", "memberIndustry", "archiveIndustry"].includes(id) ? "select" : "div");
      element.hidden = id.startsWith("panel-") && id !== "panel-overview";
      elements.set(id, element);
    }
    return elements.get(id);
  };
  const timers = new Map(), requests = [];
  let timerId = 0;
  const tabs = ["overview", "trend", "constituents", "archive"].map(name => {
    const element = new Element("button"); element.dataset.tab = name; return element;
  });
  const panels = tabs.map(tab => { const e = get(`panel-${tab.dataset.tab}`); e.dataset.panel = tab.dataset.tab; return e; });
  const window = {
    __SWIVD__: { nonce: "FAKE_SESSION_NONCE", current_run_id: null, active_jobs: [], ...boot },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); }
  };
  const context = {
    window, location: { protocol: "http:", hostname: "127.0.0.1" },
    document: {
      getElementById: get,
      createElement: tag => new Element(tag),
      createElementNS: (_namespace, tag) => new Element(tag),
      querySelectorAll: selector => selector.startsWith(".tab-btn") ? tabs : selector.startsWith(".tab-content") ? panels : []
    },
    fetch: async (url, options) => {
      requests.push({ url, method: options.method || "GET" });
      if (!fetcher) throw new Error(`Unexpected HTTP request: ${url}`);
      const result = await fetcher(url, options);
      return { ok: true, status: 200, json: async () => result };
    }
  };
  vm.createContext(context);
  const hook = `globalThis.ui = {
    fmtMarketValue, fmtDividend, peerText, compareTableValues, renderStockRows,
    renderLine, renderScatter, renderIndustryTable, renderArchive, beginTrackingJob,
    startJob, discoverActiveJob, loadCatalog,
    setState(state) {
      if ('catalog' in state) catalog = state.catalog;
      if ('runId' in state) runId = state.runId;
      if ('currentShard' in state) currentShard = state.currentShard;
      if ('archiveCatalog' in state) archiveCatalog = state.archiveCatalog;
      if ('activeLevel' in state) activeLevel = state.activeLevel;
    },
    state() { return { activeJobId, lastJobPercent, pollFailures, readyRun, runId, activeLevel, archiveCacheSize: archiveCache.size }; }
  };`;
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `${hook}\n})();`), context);
  const runTimer = async () => {
    const first = timers.entries().next().value;
    assert.ok(first, "expected a scheduled retry");
    timers.delete(first[0]); first[1].callback(); await settle();
    return first[1].delay;
  };
  return { ui: context.ui, get, requests, timers, runTimer };
}

function descendants(element, tag) {
  return element.children.flatMap(child => [ ...(child.tag === tag ? [child] : []), ...descendants(child, tag) ]);
}
const jobId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const peer = {
  stock_name: "样例股", ts_code: "000001.SZ", total_mv: "6899465.8062", circ_mv: "10000", dv_ttm: "4.7242",
  pe_ttm: 11, pb: 2, pe_ttm_percentile_state: "OK", pe_ttm_percentile_le: 22.22,
  pe_ttm_valid_n: 9, pe_ttm_member_count: 13, pe_ttm_coverage: "0.6923076923", pe_ttm_tie_count: 1,
  pb_percentile_state: "OK", pb_percentile_le: 66.67, pb_valid_n: 12, pb_member_count: 13,
  pb_coverage: "0.9230769231", pb_tie_count: 1
};

const tests = {
  units_and_coverage() {
    const h = harness();
    h.ui.setState({ currentShard: { constituents: [peer, { ...peer, stock_name: "另一个", ts_code: "000002.SZ" }] } });
    h.get("stockSearch").value = "000001";
    h.ui.renderStockRows();
    const rendered = h.get("stocks").innerHTML;
    assert.equal((rendered.match(/<tr>/g) || []).length, 1);
    for (const value of ["689.95", "4.72%", "N=9 / 成员13", "覆盖率69.23%", "N=12 / 成员13", "覆盖率92.31%", "并列1"]) assert.ok(rendered.includes(value), value);
    assert.equal(h.ui.fmtMarketValue(null), "—");
    assert.equal(h.ui.fmtDividend(null), "—");
    assert.equal(h.ui.fmtMarketValue(-10000), "-1");
    assert.equal(h.ui.fmtDividend(0), "0%");
    assert.ok(h.ui.peerText({ ...peer, pe_ttm_percentile_state: "INSUFFICIENT_PEERS", pe_ttm_valid_n: 4 }, "pe_ttm").startsWith("INSUFFICIENT_PEERS"));
    assert.ok(h.ui.peerText({}, "pe_ttm").includes("成员—"));
    assert.ok(html.includes("总市值（亿元）") && html.includes("DV_TTM（%）"));
  },
  null_sorting() {
    const h = harness(), values = [10, null, 20, 0, -2, "", undefined];
    assert.deepEqual(values.slice().sort((a,b)=>h.ui.compareTableValues(a,b,true)), [-2,0,10,20,null,"",undefined]);
    assert.deepEqual(values.slice().sort((a,b)=>h.ui.compareTableValues(a,b,false)), [20,10,0,-2,null,"",undefined]);
    assert.equal(h.ui.compareTableValues(null, "", false), 0);
    assert.ok(h.ui.compareTableValues("10", "10", false) === 0);
  },
  history_gaps() {
    const h = harness();
    const rows = [null,10,11,"",-1,20,21,null].map((pe,i)=>({ trade_date: `2026010${i+1}`, pe }));
    h.ui.renderLine("trendChart", "trendStats", rows, "pe", "测试");
    const paths = descendants(h.get("trendChart"), "path").filter(node=>node.attributes.class === "series-line");
    assert.equal(paths.length, 2, "the missing block must split the line");
    assert.equal((paths[0].attributes.d.match(/L /g)||[]).length, 1);
    assert.ok(paths[0].attributes.d.startsWith("M 192.00,"), "first valid point must retain its original date position");
    const text = h.get("trendStats").innerHTML;
    assert.ok(text.includes("20260101–20260108"));
    assert.ok(text.includes("缺失或非正观测 <b>4</b>"));
    assert.ok(text.includes("末次有效日 <b>20260107</b>"));
    h.ui.renderLine("trendChart", "trendStats", [{trade_date:"20260101",pe:null}], "pe", "测试");
    assert.equal(h.get("trendStats").textContent, "有效样本 0 · 缺失或非正观测 1");
    h.ui.renderLine("trendChart", "trendStats", [{trade_date:"20260101",pe:10}], "pe", "测试");
    assert.equal(descendants(h.get("trendChart"), "path").length, 0);
  },
  async poll_recovers() {
    let calls = 0;
    const h = harness({fetcher:async()=>{
      calls++;
      if(calls===2) throw new Error("transient");
      if(calls<4) return { state:"BUILD", percent:calls===1?60:55 };
      return {state:"SUCCEEDED",percent:100,run_id:"NEW-RUN",as_of:"20260904",safe_message_code:"NEW_DATA_READY"};
    }});
    h.ui.setState({runId:"OLD-RUN", catalog:{industries:[],as_of:"20260901"}});
    h.ui.beginTrackingJob(jobId); await settle();
    assert.equal(h.ui.state().lastJobPercent,60);
    await h.runTimer(); assert.equal(h.get("update").disabled,true);
    assert.equal(h.ui.state().activeJobId,jobId);
    assert.equal(await h.runTimer(),1000);
    assert.equal(h.ui.state().lastJobPercent,60,"progress must not regress after reconnect");
    await h.runTimer();
    assert.equal(h.ui.state().readyRun,"NEW-RUN");
    assert.equal(h.ui.state().runId,"OLD-RUN","success must not switch the viewed snapshot");
    assert.equal(h.get("update").disabled,false);
    assert.equal(h.timers.size,0);
    assert.ok(h.requests.every(request=>request.method==="GET"));
  },
  async poll_retry_limit() {
    let fail = true;
    const h = harness({fetcher:async()=>{ if(fail) throw new Error("offline"); return {state:"FAILED",percent:35,safe_message_code:"INPUT_FAILED"}; }});
    h.ui.beginTrackingJob(jobId); await settle();
    const delays=[];
    for(let i=0;i<5;i++) delays.push(await h.runTimer());
    assert.deepEqual(delays,[1000,2000,4000,8000,8000]);
    assert.equal(h.timers.size,0);
    assert.equal(h.get("retryProgress").hidden,false);
    assert.equal(h.get("update").disabled,true);
    assert.equal(h.ui.state().activeJobId,jobId);
    fail=false;
    await h.get("retryProgress").dispatch("click"); await settle();
    assert.equal(h.get("update").disabled,false);
    assert.equal(h.ui.state().activeJobId,null);
    assert.ok(h.requests.every(request=>request.method==="GET"));
  },
  async refresh_restores_job() {
    const h=harness({boot:{active_jobs:[{job_id:jobId}], current_snapshot_state:"PUBLICATION_RECOVERY_REQUIRED"},fetcher:async()=>({state:"BUILD",percent:72})});
    await settle();
    assert.equal(h.ui.state().activeJobId,jobId);
    assert.equal(h.ui.state().lastJobPercent,72);
    assert.equal(h.get("update").disabled,true);
    assert.ok(h.get("snapshot").textContent.includes("发布恢复中"));
    assert.deepEqual(h.requests.map(r=>r.url),[`/api/v1/jobs/${jobId}`]);
  },
  async active_discovery_and_lost_post() {
    let discovery=0;
    const h=harness({fetcher:async(url,options)=>{
      if(options.method==="POST") throw new Error("lost accepted response");
      if(url==="/api/v1/jobs/active") { if(++discovery===1) throw new Error("one read failure"); return {jobs:[{job_id:jobId}]}; }
      return {state:"BUILD",percent:45};
    }});
    await h.ui.startJob("UPDATE_LATEST"); await settle();
    assert.equal(h.get("update").disabled,true);
    await h.runTimer();
    assert.equal(h.ui.state().activeJobId,jobId);
    assert.equal(h.ui.state().lastJobPercent,45);
    assert.equal(h.requests.filter(r=>r.method==="POST").length,1);
  },
  async archive_retry() {
    let calls=0;
    const h=harness({fetcher:async()=>{ if(++calls===1) throw new Error("one archive failure"); return {history:[{trade_date:"20211210",pe:10}],summary:{industry_name:"档案"}}; }});
    h.ui.setState({runId:"RUN",archiveCatalog:{__run_id:"RUN",industries:[]}});
    h.get("archiveIndustry").value="801010.SI";
    await h.ui.renderArchive(); assert.equal(h.ui.state().archiveCacheSize,0);
    await h.ui.renderArchive(); assert.equal(calls,2);
    assert.ok(h.get("archiveStats").innerHTML.includes("20211210"));
  },
  async keyboard_controls() {
    const h=harness({fetcher:async()=>({industry:{industry_name:"测试"},constituents:[]})});
    h.ui.setState({runId:"RUN",catalog:{as_of:"20260901",industries:[{level:"L1",index_code:"801010.SI",industry_name:"测试",is_pub:1,pe:10,pb:2}]}});
    h.ui.renderIndustryTable();
    assert.ok(h.get("industryTable").innerHTML.includes('<button type="button" class="table-sort"'));
    assert.ok(h.get("industryTable").innerHTML.includes('aria-sort="ascending"'));
    h.ui.renderScatter();
    const circle=descendants(h.get("scatterChart"),"circle")[0];
    assert.equal(circle.attributes.role,"button");
    assert.equal(circle.attributes.tabindex,"0");
    for(const key of ["Enter"," "]) {
      let prevented=false;
      await circle.dispatch("keydown",{key,preventDefault(){prevented=true;}}); await settle();
      assert.equal(prevented,true);
      assert.equal(h.get("panel-constituents").hidden,false);
      assert.equal(h.get("memberIndustry").value,"801010.SI");
    }
  },
  async current_snapshot_readonly() {
    const h=harness({fetcher:async(url)=>url==="/api/v1/snapshots/current"?{run_id:"CURRENT",as_of:"20260904"}:{industries:[],as_of:"20260904"}});
    h.ui.setState({runId:"HISTORICAL",catalog:{industries:[],as_of:"20250101"}});
    await h.get("currentSnapshot").dispatch("click");
    assert.equal(h.ui.state().runId,"CURRENT");
    assert.deepEqual(h.requests.map(r=>r.url),["/api/v1/snapshots/current","/api/v1/snapshots/CURRENT/catalog"]);
    assert.ok(h.requests.every(r=>r.method==="GET"));
  },
  async catalog_responses_ordered() {
    const pending = new Map();
    const h=harness({boot:{current_run_id:"INITIAL"},fetcher:url=>new Promise((resolve,reject)=>pending.set(url,{resolve,reject}))});
    const next = h.ui.loadCatalog("NEW", "20260904");
    pending.get("/api/v1/snapshots/NEW/catalog").resolve({industries:[],as_of:"20260904"});
    assert.equal(await next,true);
    pending.get("/api/v1/snapshots/INITIAL/catalog").resolve({industries:[],as_of:"20260901"});
    await settle();
    assert.equal(h.ui.state().runId,"NEW","late initial response cannot replace the chosen snapshot");
    const oldFailure = h.ui.loadCatalog("OLD-FAIL");
    const last = h.ui.loadCatalog("LAST");
    pending.get("/api/v1/snapshots/LAST/catalog").resolve({industries:[],as_of:"20260905"});
    assert.equal(await last,true);
    pending.get("/api/v1/snapshots/OLD-FAIL/catalog").reject(new Error("stale failure"));
    assert.equal(await oldFailure,false);
    assert.equal(h.ui.state().runId,"LAST");
    const finalFailure = h.ui.loadCatalog("FINAL-FAIL");
    pending.get("/api/v1/snapshots/FINAL-FAIL/catalog").reject(new Error("latest request failed"));
    await assert.rejects(finalFailure,/latest request failed/);
    assert.equal(h.ui.state().runId,"LAST","a failed request must retain already browsable data");
  }
};

(async()=>{
  assert.ok(Object.hasOwn(tests,scenario),`unknown scenario ${scenario}`);
  await tests[scenario]();
  process.stdout.write(JSON.stringify({scenario,status:"PASS"})+"\n");
})().catch(error=>{process.stderr.write(error.stack+"\n");process.exitCode=1;});
