"""Integration endpoint contract over the inherited HTTPS-only transport.

The old client is reused without modifying its module globals or old contracts.
Only this subclass admits the additional read-only Tushare endpoints.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from swivd.tushare_client import (
    DEFAULT_BASE_URL, ENDPOINT_FIELDS as SW_FIELDS,
    RawApiResponse, SecureTushareClient as _SecureClient,
    TushareApiError, TushareClientError, TushareConfigurationError,
    TushareProtocolError, TushareTransportError, TushareRedirectError,
    decode_rows as _decode_rows, load_tushare_token,
)

ENDPOINT_FIELDS = dict(SW_FIELDS)
ENDPOINT_FIELDS.update({
    "stock_basic": ("ts_code", "name", "market", "list_status", "list_date", "delist_date"),
    "daily": ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"),
    "moneyflow": ("ts_code", "trade_date", "net_mf_vol", "net_mf_amount"),
    "suspend_d": ("ts_code", "trade_date", "suspend_type", "suspend_timing"),
    "index_member": ("index_code", "con_code", "in_date", "out_date", "is_new"),
    "ci_index_member": SW_FIELDS["index_member_all"],
    # Official close-only series, not PE/PB or industry money-flow sources.
    # Tushare primary docs: document/2?doc_id=308 and document/2?doc_id=260.
    "ci_daily": ("ts_code", "trade_date", "close"),
    "ths_daily": ("ts_code", "trade_date", "close"),
    "moneyflow_ind_ths": ("trade_date", "ts_code", "industry", "company_num"),
    "ths_index": ("ts_code", "name", "count", "exchange", "type"),
    "ths_member": ("ts_code", "con_code", "con_name", "in_date", "out_date", "is_new"),
    "tdx_index": ("ts_code", "trade_date", "name", "idx_type", "idx_count"),
    "tdx_member": ("ts_code", "trade_date", "con_code", "con_name"),
    "tdx_daily": ("ts_code", "trade_date", "close", "pe", "pb"),
})
ALLOWED_APIS = frozenset(ENDPOINT_FIELDS)
ROW_LIMITS = {
    "trade_cal": 6000, "stock_basic": 6000, "daily": 6000,
    "daily_basic": 6000, "moneyflow": 6000, "suspend_d": 5000,
    "index_classify": 2000, "sw_daily": 4000,
    "index_member_all": 2000, "index_member": 2000,
    "ci_index_member": 5000, "ci_daily": 4000, "ths_daily": 3000, "moneyflow_ind_ths": 5000,
    "ths_index": 5000, "ths_member": 6000,
    "tdx_index": 1000, "tdx_member": 3000, "tdx_daily": 3000,
}
# Accepted *complete* query shapes. No unlimited implicit all-history requests.
QUERY_SHAPES = {
    "trade_cal": ({"exchange", "start_date", "end_date"}, {"exchange", "start_date", "end_date", "is_open"}),
    "stock_basic": ({"list_status"}, {"list_status", "exchange"}),
    "index_classify": ({"level", "src"},),
    "sw_daily": ({"trade_date"}, {"ts_code", "start_date", "end_date"}, {"ts_code", "trade_date"}),
    "daily": ({"trade_date"}, {"trade_date", "ts_code"}),
    "daily_basic": ({"trade_date"}, {"trade_date", "ts_code"}),
    "moneyflow": ({"trade_date"}, {"trade_date", "ts_code"}),
    "suspend_d": ({"trade_date", "suspend_type"},),
    "index_member_all": tuple({selector, "is_new"} for selector in ("l1_code", "l2_code", "l3_code", "ts_code")),
    "index_member": ({"index_code"}, {"index_code", "is_new"}),
    "ci_index_member": ({"is_new"}, *tuple({selector, "is_new"} for selector in ("l1_code", "l2_code", "l3_code", "ts_code"))),
    "ci_daily": ({"trade_date"},),
    "ths_daily": ({"trade_date"},),
    "moneyflow_ind_ths": ({"trade_date"},),
    "ths_index": ({"exchange", "type"},),
    "ths_member": ({"ts_code"},),
    "tdx_index": ({"trade_date", "idx_type"},),
    "tdx_member": ({"trade_date", "ts_code"},),
    "tdx_daily": ({"trade_date"}, {"trade_date", "ts_code"}),
}


class SecureTushareClient(_SecureClient):
    def __init__(self, token=None, **kwargs):
        if kwargs.get("base_url", DEFAULT_BASE_URL).rstrip("/") != DEFAULT_BASE_URL:
            raise TushareConfigurationError("only the official HTTPS Tushare endpoint is allowed")
        super().__init__(token, **kwargs)

    @staticmethod
    def _validate_request(api_name: str, params: Mapping[str, Any], fields: Sequence[str] | str | None) -> tuple[str, ...]:
        if api_name not in ALLOWED_APIS or not isinstance(params, Mapping):
            raise TushareConfigurationError("endpoint or query is outside the integration contract")
        if set(params) not in QUERY_SHAPES[api_name]:
            raise TushareConfigurationError("query shape is outside the integration contract")
        for key, value in params.items():
            if not isinstance(value, str) or not value.strip():
                raise TushareConfigurationError("query values must be nonempty strings")
            if any(c in value for c in ("\n", "\r", "\x00")):
                raise TushareConfigurationError("invalid query value")
            if key in {"trade_date", "start_date", "end_date"}:
                try:
                    if len(value) != 8 or not value.isdigit():
                        raise ValueError()
                    datetime.strptime(value, "%Y%m%d")
                except ValueError:
                    raise TushareConfigurationError("query date is invalid") from None
        if "start_date" in params and params["start_date"] > params["end_date"]:
            raise TushareConfigurationError("query date range is reversed")
        if "is_new" in params and params["is_new"] not in {"Y", "N"}:
            raise TushareConfigurationError("invalid membership state")
        if api_name == "stock_basic" and params["list_status"] not in {"L", "D", "P"}:
            raise TushareConfigurationError("invalid listing state")
        if api_name == "index_classify" and (params["src"] != "SW2021" or params["level"] not in {"L1", "L2", "L3"}):
            raise TushareConfigurationError("invalid SW taxonomy scope")
        if api_name == "tdx_index" and params["idx_type"] != "行业板块":
            raise TushareConfigurationError("only TDX industry boards are allowed")
        if api_name == "ths_index" and dict(params) != {"exchange": "A", "type": "I"}:
            raise TushareConfigurationError("only THS A-share industry boards are allowed")
        if api_name == "suspend_d" and params["suspend_type"] != "S":
            raise TushareConfigurationError("only suspension records are allowed")
        normalized = ENDPOINT_FIELDS[api_name] if fields is None else tuple(fields.split(",") if isinstance(fields, str) else fields)
        normalized = tuple(str(field).strip() for field in normalized)
        if not normalized or len(set(normalized)) != len(normalized) or set(normalized) - set(ENDPOINT_FIELDS[api_name]):
            raise TushareConfigurationError("requested fields are outside the integration contract")
        json.dumps(dict(params), allow_nan=False)
        return normalized


def decode_rows(response, *, api_name, expected_fields=None, row_limit=None):
    if api_name not in ALLOWED_APIS or getattr(response, "api_name", api_name) != api_name:
        raise TushareProtocolError("response endpoint identity mismatch")
    return _decode_rows(response, expected_fields=expected_fields or ENDPOINT_FIELDS[api_name], row_limit=row_limit)


TushareClient = SecureTushareClient
