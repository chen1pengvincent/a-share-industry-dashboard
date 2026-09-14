"""Contract v2.3 regressions; all inputs are synthetic and network-free."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from swivd.v2_domain import (
    V2DataError,
    compute_peer_rows,
    normalize_daily_basic,
    normalize_members,
    select_members_as_of,
)
from swivd.v2_identity import (
    IDENTITY_CANDIDATE_CODES,
    IDENTITY_RULE_VERSION,
    SPECIAL_INDUSTRY_UID,
    SPECIAL_STEEL_MEMBER_PATH,
    IdentityResolutionError,
    project_member,
    resolve_membership_identity,
    resolve_quote_identity,
)


OLD_CODE, NEW_CODE = IDENTITY_CANDIDATE_CODES
DATES = ("20211213", "20211214")


def member(code=OLD_CODE, *, stock="000001.SZ", name="原名", start="20211201", end=""):
    return {
        **SPECIAL_STEEL_MEMBER_PATH,
        "l3_code": code,
        "ts_code": stock,
        "name": name,
        "in_date": start,
        "out_date": end,
        "is_new": "N" if end else "Y",
    }


def paths():
    return {
        code: {
            **SPECIAL_STEEL_MEMBER_PATH,
            "l3_code": code,
            "industry_uid": SPECIAL_INDUSTRY_UID,
            "identity_rule_version": IDENTITY_RULE_VERSION,
        }
        for code in IDENTITY_CANDIDATE_CODES
    }


def valuation(stock="000001.SZ"):
    return {
        "ts_code": stock,
        "trade_date": DATES[-1],
        "close": 10,
        "pe": 10,
        "pe_ttm": 10,
        "pb": 1,
        "ps_ttm": 2,
        "dv_ttm": 1,
        "total_mv": 100,
        "circ_mv": 80,
    }


def quote_resolution(target_code=OLD_CODE, catalog_code=NEW_CODE):
    # Reuse only the small, pure taxonomy fixture, not a provider or runtime.
    from tests.test_v2_identity import catalog

    rows = [
        {"ts_code": target_code, "trade_date": date, "name": "特钢Ⅲ", "close": 100, "pe": 10, "pb": 1}
        for date in DATES
    ]
    return resolve_quote_identity(
        catalog(catalog_code),
        rows[-1:],
        {code: rows if code == target_code else [] for code in IDENTITY_CANDIDATE_CODES},
        DATES,
        target_trade_date=DATES[-1],
    )


def evidence(rows):
    by_code = {
        code: {state: [dict(row) for row in rows if row["l3_code"] == code and row["is_new"] == state] for state in ("Y", "N")}
        for code in IDENTITY_CANDIDATE_CODES
    }
    candidates = {number: copy.deepcopy(by_code) for number in (1, 2)}
    main = {
        number: {state: [dict(row) for row in rows if row["is_new"] == state] for state in ("Y", "N")}
        for number in (1, 2)
    }
    return candidates, main


class DataAvailabilityTests(unittest.TestCase):
    def test_empty_market_response_fails_but_legacy_replay_is_explicit(self):
        with self.assertRaisesRegex(V2DataError, "DAILY_BASIC_EMPTY"):
            normalize_daily_basic([], as_of=DATES[-1])
        self.assertEqual(normalize_daily_basic([], as_of=DATES[-1], allow_empty=True), {})

    def test_individual_missing_member_remains_visible_without_forward_fill(self):
        rows = [member(stock=f"00000{i}.SZ") for i in range(1, 6)]
        selected = select_members_as_of(normalize_members(rows, l3_paths=paths()), DATES[-1])
        values = normalize_daily_basic([valuation(row["ts_code"]) for row in rows[:4]], as_of=DATES[-1])
        peers = compute_peer_rows(selected, values, as_of=DATES[-1])
        absent = [row for row in peers if row["ts_code"] == "000005.SZ"]
        self.assertEqual(len(absent), 3)
        self.assertTrue(all(row["valuation_state"] == "VALUATION_UNAVAILABLE" for row in absent))
        self.assertTrue(all(row["pe_ttm"] is None for row in absent))
        self.assertTrue(all(row["pe_ttm_member_count"] == 5 and row["pe_ttm_valid_n"] == 4 for row in peers))
        self.assertTrue(all(row["pe_ttm_percentile_le"] is None for row in peers))


class MembershipNameTests(unittest.TestCase):
    def test_changed_name_cannot_resurrect_a_closed_episode(self):
        rows = [member(name="旧名"), member(name="新名", end="20211213")]
        original = copy.deepcopy(rows)
        normalized = normalize_members(rows, l3_paths=paths())
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["name"], "新名")
        self.assertEqual(normalized[0]["out_date"], "20211213")
        self.assertEqual(rows, original, "raw name values must not be rewritten")
        with self.assertRaisesRegex(V2DataError, "NO_MEMBERS_AS_OF"):
            select_members_as_of(normalized, DATES[-1])

    def test_legacy_name_key_remains_available_only_when_requested(self):
        normalized = normalize_members(
            [member(name="旧名"), member(name="新名", end="20211213")],
            l3_paths=paths(),
            legacy_display_name_key=True,
        )
        self.assertEqual(len(normalized), 2)
        self.assertEqual(select_members_as_of(normalized, DATES[-1])[0]["name"], "旧名")

    def test_different_start_dates_are_not_swallowed(self):
        normalized = normalize_members(
            [member(name="旧名"), member(name="新名", start="20211202")],
            l3_paths=paths(),
        )
        self.assertEqual(len(normalized), 2)
        self.assertTrue(all(row["membership_state"] == "MEMBERSHIP_OVERLAP_UNKNOWN" for row in select_members_as_of(normalized, DATES[-1])))

    def test_different_source_paths_are_not_swallowed(self):
        normalized = normalize_members(
            [member(OLD_CODE, name="旧名"), member(NEW_CODE, name="新名")],
            l3_paths=paths(),
        )
        self.assertEqual(len(normalized), 2)
        self.assertTrue(all(row["membership_state"] == "MEMBERSHIP_OVERLAP_UNKNOWN" for row in select_members_as_of(normalized, DATES[-1])))

    def test_different_exit_dates_still_conflict_despite_name_change(self):
        with self.assertRaisesRegex(V2DataError, "MEMBERSHIP_EPISODE_CONFLICT"):
            normalize_members(
                [member(name="旧名", end="20211213"), member(name="新名", end="20211214")],
                l3_paths=paths(),
            )


class HistoricalIdentityTests(unittest.TestCase):
    def transition_rows(self, historical_code=OLD_CODE, current_code=NEW_CODE):
        return [
            member(historical_code, end="20211215"),
            member(current_code, start="20211216"),
        ]

    def test_historical_identity_uses_target_episode_in_both_directions(self):
        for historical_code, current_code in ((OLD_CODE, NEW_CODE), (NEW_CODE, OLD_CODE)):
            with self.subTest(historical_code=historical_code):
                result = resolve_membership_identity(
                    quote_resolution(historical_code, current_code),
                    *evidence(self.transition_rows(historical_code, current_code)),
                    membership_as_of=True,
                )
                self.assertEqual(result.member_index_code, historical_code)
                self.assertEqual(dict(result.evidence)["endpoint_current_member_code"], current_code)
                self.assertEqual(dict(result.evidence)["membership_reference_date"], DATES[-1])
                self.assertEqual(dict(result.evidence)["member_as_of_row_count"], 1)

    def test_current_mode_keeps_strict_endpoint_synchronization(self):
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_QUOTE_MISMATCH"):
            resolve_membership_identity(quote_resolution(), *evidence(self.transition_rows()))

    def test_legacy_evidence_shape_does_not_acquire_new_fields(self):
        result = resolve_membership_identity(quote_resolution(NEW_CODE), *evidence([member(NEW_CODE)]))
        self.assertNotIn("endpoint_current_member_code", result.to_dict()["evidence"])
        self.assertNotIn("membership_reference_date", result.to_dict()["evidence"])

    def test_single_code_boundary_stays_unknown_in_member_snapshot(self):
        rows = [member(OLD_CODE, end=DATES[-1]), member(NEW_CODE, start="20211216")]
        result = resolve_membership_identity(quote_resolution(), *evidence(rows), membership_as_of=True)
        projected = [project_member(row, result) for row in rows]
        normalized = normalize_members(projected, l3_paths=paths())
        selected = select_members_as_of(normalized, DATES[-1])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["membership_state"], "MEMBERSHIP_BOUNDARY_UNKNOWN")

    def test_two_boundary_candidate_codes_are_not_arbitrarily_selected(self):
        rows = [member(OLD_CODE, end=DATES[-1]), member(NEW_CODE, start=DATES[-1])]
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_AS_OF_BOUNDARY_UNKNOWN"):
            resolve_membership_identity(quote_resolution(), *evidence(rows), membership_as_of=True)

    def test_two_strictly_active_candidate_codes_still_fail(self):
        rows = [member(OLD_CODE, end="20211216"), member(NEW_CODE, start="20211213")]
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_DUAL_AS_OF"):
            resolve_membership_identity(quote_resolution(), *evidence(rows), membership_as_of=True)

    def test_historical_mode_does_not_relax_dual_current_gate(self):
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_DUAL_CURRENT"):
            resolve_membership_identity(quote_resolution(), *evidence([member(OLD_CODE), member(NEW_CODE)]), membership_as_of=True)

    def test_historical_mode_does_not_relax_two_round_stability(self):
        candidates, main = evidence(self.transition_rows())
        candidates[2][NEW_CODE]["Y"][0]["name"] = "漂移"
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_ROUNDS_DRIFT"):
            resolve_membership_identity(quote_resolution(), candidates, main, membership_as_of=True)

    def test_historical_mode_does_not_relax_main_query_comparison(self):
        candidates, main = evidence(self.transition_rows())
        for number in (1, 2):
            main[number]["N"] = []
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_L1_COMPARISON_MISMATCH"):
            resolve_membership_identity(quote_resolution(), candidates, main, membership_as_of=True)

    def test_missing_target_episode_cannot_use_current_member_code(self):
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_AS_OF_MISSING"):
            resolve_membership_identity(quote_resolution(), *evidence([member(NEW_CODE, start="20211216")]), membership_as_of=True)

    def test_target_member_code_must_still_match_target_quote(self):
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_QUOTE_MISMATCH"):
            resolve_membership_identity(quote_resolution(), *evidence([member(NEW_CODE)]), membership_as_of=True)


class EmptyMarketPipelineTests(unittest.TestCase):
    def test_empty_market_response_cannot_publish_a_new_snapshot(self):
        from tests.test_v2 import FakeClient
        from swivd.tushare_client import RawApiResponse
        from swivd.v2_pipeline import run_snapshot

        class EmptyMarketClient(FakeClient):
            def call(self, api_name, params, fields):
                response = super().call(api_name, params, fields)
                if api_name != "daily_basic":
                    return response
                payload = dict(response)
                payload["data"] = {"fields": list(fields), "items": []}
                raw = json.dumps(payload, separators=(",", ":")).encode()
                return RawApiResponse(payload, raw_bytes=raw, http_status=200, headers={}, api_name=api_name, attempt_count=1)

        with tempfile.TemporaryDirectory(prefix="swivd-v23-empty-market-") as temporary:
            data = Path(temporary) / "data"
            with self.assertRaisesRegex(V2DataError, "DAILY_BASIC_EMPTY"):
                run_snapshot(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                    data_dir=data,
                    as_of=DATES[-1],
                    purpose="UPDATE_LATEST",
                    client=EmptyMarketClient(),
                )
            self.assertFalse((data / "latest_run.json").exists())
            self.assertFalse((data / "historical_index.json").exists())
            self.assertEqual(list((data / "transactions").iterdir()), [])


class HistoricalMaterializationPipelineTests(unittest.TestCase):
    def test_materialize_before_endpoint_code_transition_publishes_history_only(self):
        from tests.test_v2 import FakeClient
        from swivd.io_utils import read_csv, read_json
        from swivd.tushare_client import ENDPOINT_FIELDS, RawApiResponse
        from swivd.v2_pipeline import run_snapshot
        from swivd.v2_validator import validate_run_v2

        class TransitionClient(FakeClient):
            def __init__(self):
                super().__init__()
                for row in self.classes["L3"]:
                    if row["industry_code"] == "230501":
                        row["index_code"] = NEW_CODE
                self.quote_names.pop(NEW_CODE)
                self.quote_names[OLD_CODE] = "特钢Ⅲ"
                self.members = [
                    member(OLD_CODE, stock="000001.SZ", end="20211215"),
                    member(NEW_CODE, stock="000002.SZ", start="20211216"),
                ]

            def call(self, api_name, params, fields):
                if api_name != "index_member_all":
                    return super().call(api_name, params, fields)
                selector = next(key for key in ("l1_code", "l2_code", "l3_code") if key in params)
                rows = [row for row in self.members if row[selector] == params[selector] and row["is_new"] == params["is_new"]]
                ordered = list(ENDPOINT_FIELDS[api_name])
                payload = {"code": 0, "msg": None, "data": {"fields": ordered, "items": [[row[field] for field in ordered] for row in rows]}}
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                return RawApiResponse(payload, raw_bytes=raw, http_status=200, headers={}, api_name=api_name, attempt_count=1)

        with tempfile.TemporaryDirectory(prefix="swivd-v23-historical-identity-") as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of=DATES[-1],
                purpose="MATERIALIZE_DATE",
                client=TransitionClient(),
            )
            run = data / "runs" / manifest["run_id"]
            validate_run_v2(run)
            self.assertFalse((data / "latest_run.json").exists())
            self.assertEqual(read_json(data / "historical_index.json")["dates"][DATES[-1]]["run_id"], manifest["run_id"])
            identity = read_json(run / "inputs" / "normalized" / "industry_identity_resolution.json")
            self.assertEqual(identity["member_index_code"], OLD_CODE)
            self.assertEqual(identity["evidence"]["endpoint_current_member_code"], NEW_CODE)
            selected = read_csv(run / "inputs" / "normalized" / "membership_snapshot.csv")
            self.assertEqual([row["ts_code"] for row in selected], ["000001.SZ"])
            self.assertEqual(selected[0]["source_l3_code"], OLD_CODE)

    def test_historical_transition_splits_l1_and_l2_into_both_l3_candidate_queries(self):
        from tests.test_v2 import FakeClient
        from swivd.io_utils import read_csv, read_json
        from swivd.tushare_client import ENDPOINT_FIELDS, RawApiResponse
        from swivd.v2_pipeline import run_snapshot
        from swivd.v2_validator import validate_run_v2

        class SplitTransitionClient(FakeClient):
            def __init__(self):
                super().__init__()
                for row in self.classes["L3"]:
                    if row["industry_code"] == "230501":
                        row["index_code"] = NEW_CODE
                self.quote_names.pop(NEW_CODE)
                self.quote_names[OLD_CODE] = "特钢Ⅲ"
                # N reaches exactly 2000 at both parent levels.  Every row
                # has its own stock/episode key; each L3 stays below the cap.
                self.members = [
                    member(OLD_CODE, stock=f"{100000 + index:06d}.SZ", end="20211215")
                    for index in range(1001)
                ] + [
                    member(NEW_CODE, stock=f"{200000 + index:06d}.SZ", start="20211216", end="20211217")
                    for index in range(999)
                ] + [member(NEW_CODE, stock="300001.SZ", start="20211218")]

            def call(self, api_name, params, fields):
                if api_name != "index_member_all":
                    return super().call(api_name, params, fields)
                selector = next(key for key in ("l1_code", "l2_code", "l3_code") if key in params)
                rows = [row for row in self.members if row[selector] == params[selector] and row["is_new"] == params["is_new"]]
                self_outer.assertLessEqual(len(rows), 2000)
                ordered = list(ENDPOINT_FIELDS[api_name])
                payload = {"code": 0, "msg": None, "data": {"fields": ordered, "items": [[row[field] for field in ordered] for row in rows]}}
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                return RawApiResponse(payload, raw_bytes=raw, http_status=200, headers={}, api_name=api_name, attempt_count=1)

        self_outer = self
        with tempfile.TemporaryDirectory(prefix="swivd-v23-historical-split-") as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of=DATES[-1],
                purpose="MATERIALIZE_DATE",
                client=SplitTransitionClient(),
            )
            run = data / "runs" / manifest["run_id"]
            validate_run_v2(run)
            requests = read_json(run / "audit.json")["requests"]
            for round_number in (1, 2):
                for selector, code, count in (
                    ("l1_code", "801040.SI", 2000),
                    ("l2_code", "801045.SI", 2000),
                    ("l3_code", OLD_CODE, 1001),
                    ("l3_code", NEW_CODE, 999),
                ):
                    self.assertTrue(any(
                        request["params"] == {selector: code, "is_new": "N"}
                        and request["row_count"] == count
                        and f"index_member_all/round_{round_number}/N/{selector}/" in request["raw_path"]
                        for request in requests
                    ), (round_number, selector, code, count))
            selected = read_csv(run / "inputs" / "normalized" / "membership_snapshot.csv")
            self.assertEqual(len(selected), 1001)
            self.assertTrue(all(row["source_l3_code"] == OLD_CODE for row in selected))
            self.assertFalse((data / "latest_run.json").exists())


class FlatLineageOwnershipTests(unittest.TestCase):
    def make_fixture(
        self, root, *, linked=True, declared_by_owner=True,
        ancestor_status="COMPLETED", ancestor_as_of="20211213",
    ):
        owner = "SWIVD2-RUN-20211214-001"
        ancestor = f"SWIVD2-RUN-{ancestor_as_of}-001"
        raw_relative = "inputs/raw/sw_daily/observations.json"
        raw = b'{"synthetic":"ancestor evidence"}'

        def record(relative, body):
            return {"path": relative, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}

        def write(relative, body):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

        def manifest(run_id, parent, artifacts):
            return {
                "run_id": run_id,
                "as_of": run_id.split("-")[2],
                "purpose": "UPDATE_LATEST",
                "provider_kind": "TEST_INJECTED_CLIENT",
                "schema_version": "swivd-local-snapshot-manifest-v4",
                "spec_version": "swivd-project-spec-v4.3",
                "contract_version": "swivd-contract-v2.3.0",
                "decision_id": "GOV-20260906-001",
                "lineage_layout": "FLAT_ANCESTOR_RAW_V1",
                "execution_status": "COMPLETED",
                "validation": {"status": "PASS"},
                "research_grade": "RESEARCH_ONLY",
                "decision_eligible": False,
                "production_approved": False,
                "parent": parent,
                "artifacts": artifacts,
            }

        ancestor_raw = f"lineage/{ancestor}/{raw_relative}"
        write(ancestor_raw, raw)
        parent_link = None
        artifacts = [record(ancestor_raw, raw)]
        if linked:
            older = manifest(ancestor, None, [record(raw_relative, raw)] if declared_by_owner else [])
            older["execution_status"] = ancestor_status
            older_body = json.dumps(older, sort_keys=True).encode()
            older_path = f"lineage/{ancestor}/manifest.json"
            write(older_path, older_body)
            artifacts.append(record(older_path, older_body))
            parent_link = {"run_id": ancestor, "as_of": ancestor_as_of, "manifest_sha256": hashlib.sha256(older_body).hexdigest()}
        body = json.dumps(manifest(owner, parent_link, artifacts), sort_keys=True).encode()
        write(f"lineage/{owner}/manifest.json", body)
        return {"run_id": owner, "as_of": "20211214", "manifest_sha256": hashlib.sha256(body).hexdigest()}

    def test_linked_ancestor_owns_its_original_raw_record(self):
        from swivd.v2_lineage import validate_flat_lineage

        with tempfile.TemporaryDirectory(prefix="swivd-v23-lineage-owned-") as temporary:
            root = Path(temporary)
            link = self.make_fixture(root)
            validate_flat_lineage(root, link, provider_kind="TEST_INJECTED_CLIENT")

    def test_unlinked_owner_cannot_be_invented_by_an_ancestor_inventory(self):
        from swivd.v2_lineage import validate_flat_lineage

        with tempfile.TemporaryDirectory(prefix="swivd-v23-lineage-orphan-") as temporary:
            root = Path(temporary)
            link = self.make_fixture(root, linked=False)
            with self.assertRaisesRegex(ValueError, "LINEAGE_UNRELATED_ANCESTOR"):
                validate_flat_lineage(root, link, provider_kind="TEST_INJECTED_CLIENT")

    def test_valid_owner_cannot_receive_raw_missing_from_its_own_manifest(self):
        from swivd.v2_lineage import validate_flat_lineage

        with tempfile.TemporaryDirectory(prefix="swivd-v23-lineage-forged-owner-") as temporary:
            root = Path(temporary)
            link = self.make_fixture(root, declared_by_owner=False)
            with self.assertRaisesRegex(ValueError, "LINEAGE_RAW_WITHOUT_OWNER"):
                validate_flat_lineage(root, link, provider_kind="TEST_INJECTED_CLIENT")

    def test_failed_ancestor_cannot_be_made_valid_by_rehashing_its_chain(self):
        from swivd.v2_lineage import validate_flat_lineage

        with tempfile.TemporaryDirectory(prefix="swivd-v23-lineage-failed-") as temporary:
            root = Path(temporary)
            link = self.make_fixture(root, ancestor_status="FAILED")
            with self.assertRaisesRegex(ValueError, "LINEAGE_ANCESTOR_NOT_SUCCESSFUL_RESEARCH"):
                validate_flat_lineage(root, link, provider_kind="TEST_INJECTED_CLIENT")

    def test_ancestor_dates_cannot_run_forward_even_when_ids_and_hashes_match(self):
        from swivd.v2_lineage import validate_flat_lineage

        with tempfile.TemporaryDirectory(prefix="swivd-v23-lineage-date-order-") as temporary:
            root = Path(temporary)
            link = self.make_fixture(root, ancestor_as_of="20211215")
            with self.assertRaisesRegex(ValueError, "LINEAGE_PARENT_DATE_NOT_EARLIER"):
                validate_flat_lineage(root, link, provider_kind="TEST_INJECTED_CLIENT")


class CurrentParentDateTests(unittest.TestCase):
    def assert_invalid_update_parent_date(self, parent_date, parent_id):
        from tests.test_v2 import FakeClient, reclose_test_run
        from swivd.io_utils import read_json, sha256_file, write_json
        from swivd.v2_pipeline import run_snapshot
        from swivd.v2_validator import V2ValidationError, validate_run_v2

        with tempfile.TemporaryDirectory(prefix="swivd-v23-current-parent-date-") as temporary:
            data = Path(temporary) / "data"
            client = FakeClient()
            parent = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json", data_dir=data,
                as_of="20211213", purpose="UPDATE_LATEST", client=client,
            )
            child = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json", data_dir=data,
                as_of="20211214", purpose="UPDATE_LATEST", client=client,
            )
            run = data / "runs" / child["run_id"]
            old = run / "lineage" / parent["run_id"]
            new = run / "lineage" / parent_id
            old.rename(new)
            parent_manifest = read_json(new / "manifest.json")
            parent_manifest.update(run_id=parent_id, as_of=parent_date)
            write_json(new / "manifest.json", parent_manifest)
            child_manifest = read_json(run / "manifest.json")
            child_manifest["parent"] = {
                "run_id": parent_id,
                "as_of": parent_date,
                "manifest_sha256": sha256_file(new / "manifest.json"),
            }
            write_json(run / "manifest.json", child_manifest)
            audit = read_json(run / "audit.json")
            audit["history"]["parent"].update(child_manifest["parent"])
            write_json(run / "audit.json", audit)
            reclose_test_run(run)
            with self.assertRaisesRegex(V2ValidationError, "latest parent date must precede current as_of"):
                validate_run_v2(run)

    def test_update_parent_same_date_is_rejected_after_hashes_are_reclosed(self):
        self.assert_invalid_update_parent_date("20211214", "SWIVD2-RUN-20211214-999")

    def test_update_parent_later_date_is_rejected_after_hashes_are_reclosed(self):
        self.assert_invalid_update_parent_date("20211215", "SWIVD2-RUN-20211215-001")

    def test_historical_materialization_may_precede_its_current_parent(self):
        from tests.test_v2 import FakeClient
        from swivd.v2_pipeline import run_snapshot
        from swivd.v2_validator import validate_run_v2

        with tempfile.TemporaryDirectory(prefix="swivd-v23-historical-parent-date-") as temporary:
            data = Path(temporary) / "data"
            client = FakeClient()
            current = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json", data_dir=data,
                as_of="20211214", purpose="UPDATE_LATEST", client=client,
            )
            current_pointer = (data / "latest_run.json").read_bytes()
            historical = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json", data_dir=data,
                as_of="20211213", purpose="MATERIALIZE_DATE", client=client,
            )
            run = data / "runs" / historical["run_id"]
            validated = validate_run_v2(run)
            self.assertEqual(validated["parent"]["run_id"], current["run_id"])
            self.assertLess(validated["as_of"], validated["parent"]["as_of"])
            self.assertEqual((data / "latest_run.json").read_bytes(), current_pointer)


if __name__ == "__main__":
    unittest.main()
