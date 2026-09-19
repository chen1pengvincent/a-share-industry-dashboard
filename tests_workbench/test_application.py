"""Application integration tests: isolated fixtures, local HTTP, no Tushare."""
from copy import deepcopy
from datetime import datetime, timedelta
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from industry_workbench.jobs import JobManager, Pipeline, SHANGHAI, candidate_day, five_year_start
from industry_workbench.models import DataError
from industry_workbench.query import QueryService
from industry_workbench.server import WorkbenchApp, WorkbenchServer
from industry_workbench.storage import FileStore
from unittest.mock import patch
from test_independent_domain import fixture, SW_A


class FixtureProvider:
    provider_kind = "TEST_INJECTED_CLIENT"
    calls = []
    fail_day = None

    def __init__(self, **kwargs):
        pass

    def calendar(self, start, end):
        value = datetime.strptime(start, "%Y%m%d").date()
        final = datetime.strptime(end, "%Y%m%d").date()
        rows = []
        while value <= final:
            rows.append({"cal_date": value.strftime("%Y%m%d"), "is_open": int(value.weekday() < 5), "exchange": "SSE"})
            value += timedelta(days=1)
        return rows

    def fetch_day(self, day):
        self.calls.append(day)
        if self.fail_day == day:
            raise DataError("MONEYFLOW_COVERAGE_GAP")
        data = fixture([("10", "1", "1.01"), ("30", "3", "-0.01")])
        data["trade_date"] = day
        for key in ("daily", "daily_basic", "moneyflow"):
            for row in data[key]:
                row["trade_date"] = day
        data["official"] = [{"uid": SW_A, "trade_date": day, "close": "100", "pe": "12", "pb": "1.2"}]
        return data


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "source"
        (self.source / "src/industry_workbench").mkdir(parents=True)
        (self.source / "src/industry_workbench/example.py").write_text("# frozen fixture\n")
        self.store = FileStore(root / "runtime", development=True)
        self.now = datetime(2026, 9, 17, 20, tzinfo=SHANGHAI)
        FixtureProvider.calls = []
        FixtureProvider.fail_day = None
        self.pipeline = Pipeline(self.store, self.source, provider_factory=FixtureProvider, clock=lambda: self.now)
        self.jobs = JobManager(self.pipeline)

    def tearDown(self):
        self.jobs.stop()
        self.tmp.cleanup()

    def test_latest_is_one_shared_batch_and_second_update_does_not_refetch(self):
        first = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(first["status"], "SUCCEEDED", first)
        manifest = self.store.current()
        self.assertEqual(manifest["as_of"], "20260917")
        self.assertEqual(manifest["history_start"], "20210917")
        self.assertTrue(manifest["views"]["coverage"]["missing_days"] > 1000)
        self.assertEqual(manifest["artifact_publish_state"], "DEVELOPMENT_ACCEPTANCE")
        query = QueryService(self.store)
        rows = query.industries(manifest["batch_id"])
        self.assertEqual(rows["batch_id"], manifest["batch_id"])
        self.assertEqual(rows["rows"][0]["metrics"]["flow_cent"]["value"], "100")
        self.assertEqual(rows["rows"][0]["metrics"]["pe_ttm_median"]["value"], "20")
        self.assertEqual(len(query.members(manifest["batch_id"], SW_A)["rows"]), 2)
        second = self.jobs.submit("update", asynchronous=False)
        self.assertTrue(second["result"]["already_current"])
        self.assertEqual(FixtureProvider.calls, ["20260917"])

    def test_failed_new_day_preserves_prior_batch_and_no_fallback_success(self):
        self.jobs.submit("update", asynchronous=False)
        before = self.store.path("current.json").read_bytes()
        self.now = self.now.replace(day=18)
        FixtureProvider.fail_day = "20260918"
        failed = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "MONEYFLOW_COVERAGE_GAP")
        self.assertEqual(before, self.store.path("current.json").read_bytes())
        self.assertEqual(FixtureProvider.calls, ["20260917", "20260918"])

    def test_query_before_history_range_is_not_a_false_closed_market_period(self):
        self.jobs.submit("update", asynchronous=False)
        batch=self.store.current()["batch_id"]
        query=QueryService(self.store)
        for method,args in ((query.industries,{}),(query.members,{"uid":SW_A}),(query.quality,{})):
            with self.subTest(method=method.__name__),self.assertRaisesRegex(DataError,"PERIOD_OUTSIDE_HISTORY_RANGE"):
                method(batch,period_kind="month",period_key="2000-01",**args)

    def test_formal_dependency_gate_stops_before_provider_or_any_data_write(self):
        formal=FileStore(Path(self.tmp.name)/"formal")
        pipeline=Pipeline(formal,self.source,provider_factory=FixtureProvider,clock=lambda:self.now)
        with patch("industry_workbench.jobs.source_identity",return_value={"git_dirty":False,"commit":"a"*40}),patch("industry_workbench.jobs.require_supported_environment",side_effect=DataError("DEPENDENCY_LOCK_MISMATCH")):
            with self.assertRaisesRegex(DataError,"DEPENDENCY_LOCK_MISMATCH"):
                pipeline.run("update",{})
        self.assertFalse(formal.root.exists())
        self.assertEqual(FixtureProvider.calls,[])

    def test_calendar_raw_lineage_and_same_day_request_receipt_are_retained(self):
        class CalendarEvidenceProvider(FixtureProvider):
            def __init__(self,**kwargs):self.put_raw=kwargs["put_raw"]
            def calendar(self,start,end):
                rows=super().calendar(start,end)
                self.put_raw({"api_name":"trade_cal","params":{"start_date":start,"end_date":end},"attempt_count":1},json.dumps({"rows":rows}).encode())
                return rows
        pipeline=Pipeline(self.store,self.source,provider_factory=CalendarEvidenceProvider,clock=lambda:self.now)
        first=pipeline.run("update",{})
        m=self.store.current()
        self.assertEqual(len(m["calendar_source_refs"]),1)
        self.assertEqual(m["calendar_source_refs"][0]["request"]["api_name"],"trade_cal")
        before=self.store.path("current.json").read_bytes()
        again=pipeline.run("update",{})
        self.assertTrue(again["already_current"])
        self.assertEqual(again["calendar_source_refs"],m["calendar_source_refs"])
        self.assertEqual(before,self.store.path("current.json").read_bytes())
        journal=[json.loads(line) for line in self.store.path("request_journal.ndjson").read_text().splitlines()]
        self.assertEqual(len(journal),2)
        self.assertEqual(FixtureProvider.calls,["20260917"])

    def test_rebuild_preserves_historical_metric_gap_and_availability(self):
        class HistoricalGapProvider(FixtureProvider):
            def fetch_day(self,day):
                data=super().fetch_day(day)
                if day=="20260916":
                    for row in data["daily_basic"]:row["pe_ttm"]="-10"
                return data
        pipeline=Pipeline(self.store,self.source,provider_factory=HistoricalGapProvider,clock=lambda:self.now)
        pipeline.run("backfill",{"start_date":"20260916","end_date":"20260917"})
        (self.source/"src/industry_workbench/example.py").write_text("# reviewed successor\n")
        pipeline.run("update",{})
        manifest=self.store.current()
        self.assertEqual(manifest["views"]["coverage"]["missing_days"],0)
        self.assertEqual(manifest["publication_state"],"PUBLISHED_WITH_GAPS")
        coverage=manifest["views"]["coverage"]
        self.assertEqual(coverage["days_with_metric_gaps"],["20260916"])
        pe=coverage["availability_by_scope"][0]["metrics"]["pe_ttm_median"]
        self.assertEqual(pe["first_valid_date"],"20260917")
        self.assertEqual(pe["missing_industry_days"],1)

    def test_backfill_keeps_asof_and_partial_week_flow_is_not_complete(self):
        self.jobs.submit("update", asynchronous=False)
        job = self.jobs.submit("backfill", {"start_date": "20260914", "end_date": "20260916", "max_days": 2}, asynchronous=False)
        self.assertEqual(job["status"], "SUCCEEDED", job)
        m = self.store.current()
        self.assertEqual(m["as_of"], "20260917")
        view = QueryService(self.store).industries(m["batch_id"], period_kind="week", period_key="20260917")
        self.assertIsNone(view["rows"][0]["metrics"]["flow_cent"]["value"])
        self.assertEqual(view["rows"][0]["metrics"]["flow_cent"]["known_subtotal"], "300")
        final = self.jobs.submit("backfill", {"start_date": "20260914", "end_date": "20260916"}, asynchronous=False)
        self.assertEqual(final["status"], "SUCCEEDED", final)
        m = self.store.current()
        view = QueryService(self.store).industries(m["batch_id"], period_kind="week", period_key="20260917")
        self.assertEqual(view["rows"][0]["metrics"]["flow_cent"]["value"], "400")
        self.assertEqual(view["period"]["status"], "IN_PROGRESS")

    def test_development_data_cannot_use_formal_root(self):
        with self.assertRaises(DataError):
            FileStore(Path.home() / "Library/Application Support/ashare-industry", development=True)
        formal = Pipeline(FileStore(Path(self.tmp.name)/"formal"), self.source, provider_factory=FixtureProvider)
        with self.assertRaisesRegex(DataError, "CLEAN_COMMITTED_SOURCE_REQUIRED"):
            formal.source_check()

    def test_removed_export_job_is_rejected_before_state_or_source_access(self):
        before = list(self.store.root.rglob("*")) if self.store.root.exists() else []
        with patch.object(self.pipeline, "source_check", side_effect=AssertionError("must reject unsupported job first")):
            with self.assertRaisesRegex(DataError, "INVALID_JOB_KIND"):
                self.jobs.submit("export", {"format": "csv"})
        self.assertEqual(list(self.store.root.rglob("*")) if self.store.root.exists() else [], before)
        self.assertEqual(FixtureProvider.calls, [])

    def test_removed_export_http_routes_do_not_read_files_or_create_jobs(self):
        import io
        from email.message import Message
        from types import SimpleNamespace
        from industry_workbench.server import Handler
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(app=SimpleNamespace(jobs=self.jobs, store=self.store))
        with patch.object(self.jobs, "submit", side_effect=AssertionError("removed route created a job")), \
                patch.object(self.jobs, "get", side_effect=AssertionError("removed route read an export job")):
            for extension in ("csv", "xlsx"):
                handler.path = "/api/v2/exports/JOB-" + "a" * 20 + "." + extension
                with self.assertRaisesRegex(DataError, "ROUTE_NOT_FOUND"):
                    handler._get()
            handler.path = "/api/v2/jobs/export"
            handler.rfile = io.BytesIO(b"{}")
            handler.headers = Message()
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = "2"
            with patch.object(handler, "_allowed"), patch.object(handler, "_json") as reply:
                handler.do_POST()
                self.assertEqual(reply.call_args.args[0], 404)
                self.assertEqual(reply.call_args.args[1]["error"]["code"], "ROUTE_NOT_FOUND")
        self.assertFalse(self.store.root.exists())

    def test_source_change_during_fetch_is_not_published(self):
        source = self.source
        class Changing(FixtureProvider):
            def fetch_day(self, day):
                (source/"src/industry_workbench/example.py").write_text("# changed during job\n")
                return super().fetch_day(day)
        self.pipeline.provider_factory = Changing
        failed = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "SOURCE_CHANGED_DURING_JOB")
        self.assertIsNone(self.store.current())

    def test_new_source_recomputes_old_days_and_preserves_capture_lineage(self):
        self.jobs.submit("update", asynchronous=False)
        previous=self.store.current()
        old_input=previous["days"]["20260917"]["input"]
        (self.source/"src/industry_workbench/example.py").write_text("# reviewed successor\n")
        from industry_workbench.metrics import compute_day as actual_compute
        with patch("industry_workbench.jobs.compute_day",wraps=actual_compute) as compute:
            job=self.jobs.submit("backfill",{"start_date":"20260916","end_date":"20260916"},asynchronous=False)
        self.assertEqual(job["status"],"SUCCEEDED",job)
        self.assertEqual(compute.call_count,2)
        updated=self.store.current()
        self.assertEqual(updated["days"]["20260917"]["input"],old_input)
        self.assertEqual(updated["days"]["20260917"]["input_source"]["identity"]["tree_sha256"],previous["source"]["tree_sha256"])
        self.assertEqual(updated["days"]["20260917"]["result_source_sha256"],updated["source"]["tree_sha256"])

    def test_same_day_source_upgrade_rebuilds_without_refetching_stock_data(self):
        self.jobs.submit("update",asynchronous=False)
        previous=self.store.current()["batch_id"]
        (self.source/"src/industry_workbench/example.py").write_text("# same-day successor\n")
        job=self.jobs.submit("update",asynchronous=False)
        self.assertEqual(job["status"],"SUCCEEDED",job)
        self.assertNotEqual(self.store.current()["batch_id"],previous)
        self.assertEqual(FixtureProvider.calls,["20260917"])

    def test_candidate_close_boundary_and_leap_start(self):
        calendar = FixtureProvider().calendar("20260916", "20260920")
        self.assertEqual(candidate_day(calendar, self.now.replace(hour=19, minute=29)), "20260916")
        self.assertEqual(candidate_day(calendar, self.now.replace(hour=19, minute=30)), "20260917")
        self.assertEqual(candidate_day(calendar, self.now.replace(day=20, hour=10)), "20260918")
        self.assertEqual(five_year_start("20240229"), "20190228")

    def test_transitive_raw_corruption_is_detected(self):
        with self.store.writer():
            raw = self.store.put_bytes(b'{"raw":"fixture"}', "tushare_response")
            inputs = self.store.put_json({"source_refs": [raw]}, "day_input")
            self.assertEqual(self.store.verify_refs({"input": inputs}), 2)
            self.store.path(raw["path"]).write_bytes(b"corrupted")
            with self.assertRaises(DataError):
                self.store.verify_refs({"input": inputs})

    def test_uncommitted_orphan_is_not_queryable_as_published_batch(self):
        self.jobs.submit("update",asynchronous=False)
        published=self.store.current()
        with self.store.writer():
            def crash(phase):
                if phase=="prepared":raise RuntimeError("injected")
            with self.assertRaises(RuntimeError):self.store.publish(published,fault=crash)
        candidates=[p.name for p in self.store.path("batches").iterdir() if p.name!=published["batch_id"]]
        self.assertEqual(len(candidates),1)
        with self.assertRaisesRegex(DataError,"BATCH_NOT_COMMITTED"):
            QueryService(self.store).catalog(candidates[0])

    def test_conflicting_duplicate_object_metadata_is_not_skipped(self):
        with self.store.writer():
            reference=self.store.put_json({"value":1})
            bad={**reference,"bytes":999}
            with self.assertRaises(DataError):self.store.verify_refs([bad,reference])

    def test_http_read_security_and_shared_batch_routes(self):
        self.jobs.submit("update", asynchronous=False)
        web = self.source / "web"
        web.mkdir(); (web/"index.html").write_text("<!doctype html><title>Fixture</title>")
        app = WorkbenchApp(self.source, self.jobs)
        server = WorkbenchServer(app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(method, path, body, headers or {})
            reply = conn.getresponse()
            result = reply.status, reply.read(), dict(reply.headers)
            conn.close()
            return result
        try:
            status, body, headers = request("GET", "/api/v2/bootstrap")
            self.assertEqual(status, 200)
            nonce = json.loads(body)["nonce"]
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            status, body, _ = request("GET", "/api/v2/current")
            batch = json.loads(body)["batch_id"]
            self.assertEqual(status, 200)
            self.assertIn("pending_dates", json.loads(body)["history_scan"])
            with patch.object(self.pipeline, "history_status", side_effect=DataError("HISTORY_STATE_INVALID")):
                scan_status, scan_body, _ = request("GET", "/api/v2/current")
                scan_state = json.loads(scan_body)
                self.assertEqual(scan_status, 200)
                self.assertEqual(scan_state["batch_id"], batch)
                self.assertIsNone(scan_state["history_scan"])
                self.assertEqual(scan_state["history_scan_error"]["code"], "HISTORY_STATE_INVALID")
                self.assertEqual(scan_state["update_blocked"]["code"], "HISTORY_STATE_INVALID")
            self.assertEqual(request("GET", f"/api/v2/batches/{batch}/industries?taxonomy=SW&level_or_series=L1")[0], 200)
            self.assertEqual(request("GET", "/api/v2/current", headers={"Host": "evil.example"})[0], 403)
            self.assertEqual(request("GET", "/api/v2/current", headers={"Origin": "https://evil.example"})[0], 403)
            self.assertEqual(request("POST", "/api/v2/jobs/update", "{}", {"Content-Type": "application/json"})[0], 403)
            self.assertEqual(request("POST", "/legacy/api/v1/jobs/update", "{}", {"Content-Type": "application/json", "X-Workbench-Nonce": nonce})[0], 403)
            self.assertEqual(request("GET", "/../../AGENTS.md")[0], 404)
            post_headers = {"Content-Type": "application/json", "X-Workbench-Nonce": nonce}
            status, body, _ = request("POST", "/api/v2/jobs/export", "{}", post_headers)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"]["code"], "ROUTE_NOT_FOUND")
            for extension in ("csv", "xlsx"):
                status, body, _ = request("GET", "/api/v2/exports/JOB-" + "a" * 20 + "." + extension)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body)["error"]["code"], "ROUTE_NOT_FOUND")
            for bad in ("true", 1, 0, None, []):
                payload = json.dumps({"start_date": "20260914", "end_date": "20260916", "retry_failed": bad})
                status, body, _ = request("POST", "/api/v2/jobs/backfill", payload, post_headers)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "INVALID_RETRY_FAILED_PARAMETER")
            with patch.object(self.jobs, "submit", return_value={"status": "QUEUED"}) as submit:
                for retry in (False, True):
                    payload = {"start_date": "20260914", "end_date": "20260916", "retry_failed": retry}
                    self.assertEqual(request("POST", "/api/v2/jobs/backfill", json.dumps(payload), post_headers)[0], 202)
                    submit.assert_called_with("backfill", payload)
            self.assertEqual(FixtureProvider.calls, ["20260917"])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
