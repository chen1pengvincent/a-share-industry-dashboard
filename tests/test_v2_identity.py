from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from swivd.v2_identity import (
    IDENTITY_CANDIDATE_CODES,
    IDENTITY_RULE_VERSION,
    PROJECTED_CLASSIFICATION_FIELDS,
    SPECIAL_INDUSTRY_UID,
    IdentityResolutionError,
    project_classification,
    project_member,
    project_quote,
    resolve_membership_identity,
    resolve_quote_identity,
)


DATES = ("20211213", "20211214", "20211215", "20211216")
OLD_CODE, NEW_CODE = IDENTITY_CANDIDATE_CODES


def catalog(code: str = OLD_CODE):
    return [
        {
            "src": "SW2021",
            "level": "L1",
            "index_code": "801040.SI",
            "industry_name": "钢铁",
            "industry_code": "230000",
            "parent_code": "0",
            "is_pub": 1,
        },
        {
            "src": "SW2021",
            "level": "L2",
            "index_code": "801045.SI",
            "industry_name": "特钢Ⅱ",
            "industry_code": "230500",
            "parent_code": "230000",
            "is_pub": 1,
        },
        {
            "src": "SW2021",
            "level": "L3",
            "index_code": code,
            "industry_name": "特钢Ⅲ",
            "industry_code": "230501",
            "parent_code": "230500",
            "is_pub": 1,
        },
        {
            "src": "SW2021",
            "level": "L3",
            "index_code": "850402.SI",
            "industry_name": "其他钢材",
            "industry_code": "230502",
            "parent_code": "230500",
            "is_pub": 1,
        },
    ]


def quote(code: str, trade_date: str):
    return {
        "ts_code": code,
        "trade_date": trade_date,
        "name": "特钢Ⅲ",
        "close": 100,
        "pe": 10,
        "pb": 1,
    }


def histories(codes):
    result = {OLD_CODE: [], NEW_CODE: []}
    for trade_date, code in zip(DATES, codes, strict=True):
        result[code].append(quote(code, trade_date))
    return result


def member(
    code: str,
    stock: str,
    *,
    is_new: str = "Y",
    out_date: str = "",
    l2_name: str = "特钢Ⅱ",
):
    return {
        "l1_code": "801040.SI",
        "l1_name": "钢铁",
        "l2_code": "801045.SI",
        "l2_name": l2_name,
        "l3_code": code,
        "l3_name": "特钢Ⅲ",
        "ts_code": stock,
        "name": stock,
        "in_date": "20211213",
        "out_date": out_date,
        "is_new": is_new,
    }


def membership_evidence(current_code: str):
    inactive_code = NEW_CODE if current_code == OLD_CODE else OLD_CODE
    by_code = {
        OLD_CODE: {"Y": [], "N": []},
        NEW_CODE: {"Y": [], "N": []},
    }
    by_code[current_code]["Y"] = [member(current_code, "000001.SZ")]
    by_code[inactive_code]["N"] = [
        member(
            inactive_code,
            "000002.SZ",
            is_new="N",
            out_date="20211214",
        )
    ]
    candidate_rounds = {
        number: {
            code: {state: [dict(row) for row in by_code[code][state]] for state in ("Y", "N")}
            for code in IDENTITY_CANDIDATE_CODES
        }
        for number in (1, 2)
    }
    l1_rounds = {
        number: {
            state: [
                dict(row)
                for code in IDENTITY_CANDIDATE_CODES
                for row in by_code[code][state]
            ]
            for state in ("Y", "N")
        }
        for number in (1, 2)
    }
    return candidate_rounds, l1_rounds


def quote_resolution(catalog_code: str, codes):
    target_code = codes[-1]
    return resolve_quote_identity(
        catalog(catalog_code),
        [quote(target_code, DATES[-1])],
        histories(codes),
        DATES,
        target_trade_date=DATES[-1],
    )


class QuoteIdentityTests(unittest.TestCase):
    def test_continuous_catalog_code_is_direct(self):
        result = quote_resolution(OLD_CODE, [OLD_CODE] * len(DATES))
        self.assertEqual(result.identity_state, "DIRECT")
        self.assertEqual(result.quote_index_code, OLD_CODE)
        self.assertEqual(result.industry_uid, SPECIAL_INDUSTRY_UID)
        self.assertEqual(len(result.intervals), 1)
        self.assertEqual(result.intervals[0].row_count, len(DATES))

    def test_continuous_other_code_is_evidence_gated_alias(self):
        result = quote_resolution(OLD_CODE, [NEW_CODE] * len(DATES))
        self.assertEqual(result.identity_state, "EVIDENCE_GATED_ALIAS")
        self.assertEqual(result.quote_index_code, NEW_CODE)
        self.assertEqual(result.catalog_index_code, OLD_CODE)

    def test_one_switch_in_either_direction_is_dated_transition(self):
        forward = quote_resolution(OLD_CODE, [OLD_CODE, OLD_CODE, NEW_CODE, NEW_CODE])
        reverse = quote_resolution(OLD_CODE, [NEW_CODE, NEW_CODE, NEW_CODE, OLD_CODE])
        self.assertEqual(forward.identity_state, "EVIDENCE_GATED_TRANSITION")
        self.assertEqual(reverse.identity_state, "EVIDENCE_GATED_TRANSITION")
        self.assertEqual(
            [(item.start_date, item.end_date, item.source_ts_code) for item in forward.intervals],
            [
                (DATES[0], DATES[1], OLD_CODE),
                (DATES[2], DATES[3], NEW_CODE),
            ],
        )

    def test_same_day_dual_code_fails_closed(self):
        raw = histories([NEW_CODE] * len(DATES))
        raw[OLD_CODE].append(quote(OLD_CODE, DATES[1]))
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_HISTORY_OVERLAP"):
            resolve_quote_identity(
                catalog(),
                [quote(NEW_CODE, DATES[-1])],
                raw,
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_open_day_gap_fails_closed(self):
        raw = histories([NEW_CODE] * len(DATES))
        raw[NEW_CODE] = [row for row in raw[NEW_CODE] if row["trade_date"] != DATES[1]]
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_HISTORY_GAP"):
            resolve_quote_identity(
                catalog(),
                [quote(NEW_CODE, DATES[-1])],
                raw,
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_catalog_candidate_collision_fails_closed(self):
        rows = catalog()
        rows[-1]["index_code"] = NEW_CODE
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_CATALOG_COLLISION"):
            resolve_quote_identity(
                rows,
                [quote(OLD_CODE, DATES[-1])],
                histories([OLD_CODE] * len(DATES)),
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_name_is_a_hard_gate_not_a_join(self):
        raw = histories([NEW_CODE] * len(DATES))
        raw[NEW_CODE][0]["name"] = "同名但不可替代"
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_NAME_MISMATCH"):
            resolve_quote_identity(
                catalog(),
                [quote(NEW_CODE, DATES[-1])],
                raw,
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_catalog_parent_path_and_publication_are_hard_gates(self):
        rows = catalog()
        rows[1]["industry_name"] = "错误父行业"
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_CATALOG_PATH_MISMATCH"):
            resolve_quote_identity(
                rows,
                [quote(NEW_CODE, DATES[-1])],
                histories([NEW_CODE] * len(DATES)),
                DATES,
                target_trade_date=DATES[-1],
            )
        rows = catalog()
        rows[2]["is_pub"] = 0
        with self.assertRaisesRegex(
            IdentityResolutionError, "IDENTITY_CATALOG_PUBLICATION_MISMATCH"
        ):
            resolve_quote_identity(
                rows,
                [quote(NEW_CODE, DATES[-1])],
                histories([NEW_CODE] * len(DATES)),
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_unapproved_same_name_quote_is_not_a_join_candidate(self):
        extra = quote("859999.SI", DATES[-1])
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_SECOND_ALIAS"):
            resolve_quote_identity(
                catalog(),
                [quote(NEW_CODE, DATES[-1]), extra],
                histories([NEW_CODE] * len(DATES)),
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_target_day_and_history_payload_must_be_identical(self):
        target = quote(NEW_CODE, DATES[-1])
        target["pe"] = 11
        with self.assertRaisesRegex(
            IdentityResolutionError, "IDENTITY_TARGET_HISTORY_ROW_MISMATCH"
        ):
            resolve_quote_identity(
                catalog(),
                [target],
                histories([NEW_CODE] * len(DATES)),
                DATES,
                target_trade_date=DATES[-1],
            )

    def test_oscillation_is_not_mislabelled_as_transition(self):
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_HISTORY_OSCILLATION"):
            quote_resolution(OLD_CODE, [OLD_CODE, NEW_CODE, OLD_CODE, NEW_CODE])


class MembershipIdentityTests(unittest.TestCase):
    def setUp(self):
        self.quote = quote_resolution(OLD_CODE, [NEW_CODE] * len(DATES))
        self.candidate, self.l1 = membership_evidence(NEW_CODE)

    def test_stable_dual_rounds_resolve_same_current_code(self):
        result = resolve_membership_identity(self.quote, self.candidate, self.l1)
        self.assertEqual(result.member_index_code, NEW_CODE)
        self.assertEqual(result.quote_index_code, NEW_CODE)
        self.assertEqual(result.identity_state, "EVIDENCE_GATED_ALIAS")
        payload = result.to_dict()
        self.assertEqual(payload["rule_version"], IDENTITY_RULE_VERSION)
        self.assertEqual(payload["current_index_code"], NEW_CODE)

    def test_candidate_round_drift_fails_closed(self):
        self.candidate[2][NEW_CODE]["Y"][0]["name"] = "drift"
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_ROUNDS_DRIFT"):
            resolve_membership_identity(self.quote, self.candidate, self.l1)

    def test_current_member_and_quote_must_be_synchronized(self):
        candidate, l1 = membership_evidence(OLD_CODE)
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_QUOTE_MISMATCH"):
            resolve_membership_identity(self.quote, candidate, l1)

    def test_both_current_codes_fail_closed(self):
        extra = member(OLD_CODE, "000003.SZ")
        for round_number in (1, 2):
            self.candidate[round_number][OLD_CODE]["Y"].append(dict(extra))
            self.l1[round_number]["Y"].append(dict(extra))
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_DUAL_CURRENT"):
            resolve_membership_identity(self.quote, self.candidate, self.l1)

    def test_member_path_drift_fails_closed(self):
        self.candidate[1][NEW_CODE]["Y"][0]["l2_name"] = "普钢"
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_MEMBER_PATH_MISMATCH"):
            resolve_membership_identity(self.quote, self.candidate, self.l1)

    def test_l1_filter_must_equal_direct_candidate_queries(self):
        self.l1[1]["Y"] = []
        self.l1[2]["Y"] = []
        with self.assertRaisesRegex(
            IdentityResolutionError, "IDENTITY_MEMBER_L1_COMPARISON_MISMATCH"
        ):
            resolve_membership_identity(self.quote, self.candidate, self.l1)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        quote_result = quote_resolution(OLD_CODE, [NEW_CODE] * len(DATES))
        candidate, l1 = membership_evidence(NEW_CODE)
        self.resolution = resolve_membership_identity(quote_result, candidate, l1)

    def test_projection_preserves_endpoint_native_codes(self):
        source_classification = catalog(OLD_CODE)[2]
        classification = project_classification(source_classification, self.resolution)
        self.assertEqual(tuple(classification), PROJECTED_CLASSIFICATION_FIELDS)
        self.assertEqual(classification["catalog_index_code"], OLD_CODE)
        self.assertEqual(classification["index_code"], NEW_CODE)

        source_quote = quote(NEW_CODE, DATES[-1])
        quote_row = project_quote(source_quote, self.resolution)
        self.assertEqual(source_quote["ts_code"], NEW_CODE)
        self.assertEqual(quote_row["source_ts_code"], NEW_CODE)
        self.assertEqual(quote_row["ts_code"], NEW_CODE)

        source_member = member(NEW_CODE, "000001.SZ")
        member_row = project_member(source_member, self.resolution)
        self.assertEqual(source_member["l3_code"], NEW_CODE)
        self.assertEqual(member_row["source_l3_code"], NEW_CODE)
        self.assertEqual(member_row["l3_code"], NEW_CODE)
        self.assertEqual(member_row["industry_uid"], SPECIAL_INDUSTRY_UID)

    def test_transition_quote_projection_keeps_old_source_code(self):
        quote_result = quote_resolution(
            OLD_CODE, [OLD_CODE, OLD_CODE, NEW_CODE, NEW_CODE]
        )
        projected = project_quote(quote(OLD_CODE, DATES[0]), quote_result)
        self.assertEqual(projected["source_ts_code"], OLD_CODE)
        self.assertEqual(projected["ts_code"], NEW_CODE)

    def test_existing_provenance_cannot_be_overwritten(self):
        row = quote(NEW_CODE, DATES[-1])
        row["source_ts_code"] = OLD_CODE
        with self.assertRaisesRegex(IdentityResolutionError, "IDENTITY_PROVENANCE_CONFLICT"):
            project_quote(row, self.resolution)


if __name__ == "__main__":
    unittest.main()
