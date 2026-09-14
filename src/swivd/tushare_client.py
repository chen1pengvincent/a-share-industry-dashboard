"""Small, fail-closed Tushare HTTP client used by the dashboard.

The module deliberately does not use ``tushare.pro_api``.  The project contract
requires an HTTPS-only transport, redirect rejection, a very small API
allow-list, and preservation of the response bytes.  ``urllib`` gives us those
properties without adding a runtime dependency.

Nothing in this module logs a token.  Callers should persist ``raw_bytes`` from
the returned :class:`RawApiResponse` when freezing an input response.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)


DEFAULT_BASE_URL = "https://api.tushare.pro"
ALLOWED_APIS = frozenset(
    {"trade_cal", "index_classify", "sw_daily", "index_member_all", "daily_basic"}
)
ENDPOINT_PARAMS: dict[str, frozenset[str]] = {
    "trade_cal": frozenset({"exchange", "start_date", "end_date", "is_open"}),
    "index_classify": frozenset({"level", "src"}),
    "sw_daily": frozenset({"ts_code", "trade_date", "start_date", "end_date"}),
    "index_member_all": frozenset(
        {"l1_code", "l2_code", "l3_code", "ts_code", "is_new"}
    ),
    "daily_basic": frozenset({"ts_code", "trade_date", "start_date", "end_date"}),
}
REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    "trade_cal": frozenset({"start_date", "end_date"}),
    "index_classify": frozenset({"level", "src"}),
    "sw_daily": frozenset(),
    "index_member_all": frozenset({"is_new"}),
    "daily_basic": frozenset({"trade_date"}),
}
ENDPOINT_FIELDS: dict[str, tuple[str, ...]] = {
    "trade_cal": ("exchange", "cal_date", "is_open", "pretrade_date"),
    "index_classify": (
        "index_code",
        "industry_name",
        "parent_code",
        "level",
        "industry_code",
        "is_pub",
        "src",
    ),
    "sw_daily": (
        "ts_code",
        "trade_date",
        "name",
        "open",
        "low",
        "high",
        "close",
        "change",
        "pct_change",
        "vol",
        "amount",
        "pe",
        "pb",
        "total_mv",
        "float_mv",
    ),
    "index_member_all": (
        "l1_code",
        "l1_name",
        "l2_code",
        "l2_name",
        "l3_code",
        "l3_name",
        "ts_code",
        "name",
        "in_date",
        "out_date",
        "is_new",
    ),
    "daily_basic": (
        "ts_code",
        "trade_date",
        "close",
        "pe",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dv_ttm",
        "total_mv",
        "circ_mv",
    ),
}
TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TushareClientError(RuntimeError):
    """Base class for all client-side failures."""


class TushareConfigurationError(TushareClientError):
    """The client or request is not compatible with the frozen contract."""


class TushareTransportError(TushareClientError):
    """The HTTPS request could not be completed."""


class TushareRedirectError(TushareTransportError):
    """The endpoint attempted to redirect the request."""


class TushareProtocolError(TushareClientError):
    """The server returned malformed or schema-incompatible JSON."""


class TushareApiError(TushareClientError):
    """Tushare returned a non-zero application code."""

    def __init__(self, code: int, message: str, response: Mapping[str, Any]) -> None:
        # The server message is intentionally not interpolated: an adversarial
        # or broken upstream must not be able to make us echo request secrets.
        super().__init__(f"Tushare API returned non-zero code={code}")
        self.code = code
        self.response = response


class _RejectRedirects(HTTPRedirectHandler):
    """Redirect handler that fails before a redirected request is emitted."""

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        raise TushareRedirectError(f"redirect rejected (HTTP {code})")


def _verified_ssl_context() -> ssl.SSLContext:
    """Build a verified context even when framework Python lacks its CA install.

    The local Python 3.14 framework may have no populated OpenSSL certificate
    directory.  ``certifi`` is already a transitive dependency of Tushare's
    installed HTTP stack; loading its Mozilla CA bundle preserves certificate
    verification instead of disabling TLS checks.
    """

    context = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    default_cafile_exists = bool(paths.cafile and os.path.isfile(paths.cafile))
    default_capath_exists = bool(paths.capath and os.path.isdir(paths.capath))
    if not default_cafile_exists and not default_capath_exists:
        try:
            import certifi  # type: ignore[import-not-found]

            context.load_verify_locations(cafile=certifi.where())
        except Exception as exc:
            raise TushareConfigurationError(
                "no usable system CA store or certifi CA bundle"
            ) from exc
    return context


class RawApiResponse(dict[str, Any]):
    """Parsed response mapping plus the exact HTTP response bytes.

    It subclasses ``dict`` so orchestration code can serialize and inspect it as
    a normal Tushare response while still retaining transport evidence.
    """

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        raw_bytes: bytes,
        http_status: int,
        headers: Mapping[str, str],
        api_name: str,
        attempt_count: int,
    ) -> None:
        super().__init__(payload)
        self.raw_bytes = raw_bytes
        self.http_status = http_status
        self.headers = dict(headers)
        self.api_name = api_name
        self.attempt_count = attempt_count

    @property
    def raw_text(self) -> str:
        return self.raw_bytes.decode("utf-8", errors="strict")


def _redact(text: Any, secret: str | None) -> str:
    rendered = str(text or "")
    return rendered.replace(secret, "[REDACTED_TOKEN]") if secret else rendered


def load_tushare_token(
    *,
    env: Mapping[str, str] | None = None,
    env_name: str = "TUSHARE_TOKEN",
) -> str:
    """Load a token without printing or embedding it in an error message.

    Environment state is authoritative when present.  Only when it is absent do
    we consult ``tushare.get_token()``, which reads Tushare's local setting.
    """

    environ = os.environ if env is None else env
    token = (environ.get(env_name) or "").strip()
    if not token:
        try:
            import tushare  # type: ignore[import-not-found]

            getter = getattr(tushare, "get_token", None)
            if callable(getter):
                token = str(getter() or "").strip()
        except Exception:  # token lookup failures are deliberately opaque
            raise TushareConfigurationError(
                f"no usable {env_name}; local Tushare token lookup failed"
            ) from None
    if not token:
        raise TushareConfigurationError(
            f"no usable {env_name} or locally configured Tushare token"
        )
    if any(character in token for character in ("\r", "\n", "\x00")):
        raise TushareConfigurationError("Tushare token contains forbidden characters")
    return token


def _validated_url(base_url: str) -> str:
    candidate = base_url.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https":
        raise TushareConfigurationError("Tushare base URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise TushareConfigurationError("Tushare base URL has an invalid authority")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise TushareConfigurationError("Tushare base URL must not contain path/query/fragment")
    return candidate


def _reject_credential_material(value: Any, credential: str) -> None:
    if isinstance(value, str):
        if credential in value:
            raise TushareProtocolError("response contains credential material")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_credential_material(key, credential)
            _reject_credential_material(item, credential)
    elif isinstance(value, list):
        for item in value:
            _reject_credential_material(item, credential)


def _strict_json_loads(raw_bytes: bytes, *, credential: str) -> Mapping[str, Any]:
    def checked_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        # Check before dict conversion can discard earlier duplicate-key values.
        for key, value in pairs:
            _reject_credential_material(key, credential)
            _reject_credential_material(value, credential)
        return dict(pairs)

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            parse_float=Decimal,
            object_pairs_hook=checked_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TushareProtocolError("response is not strict UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise TushareProtocolError("response JSON root must be an object")
    return payload


@dataclass(frozen=True)
class ClientPolicy:
    timeout_seconds: float = 30.0
    max_transient_retries: int = 2
    backoff_seconds: tuple[float, ...] = (1.0, 3.0)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise TushareConfigurationError("timeout_seconds must be positive")
        if self.max_transient_retries < 0:
            raise TushareConfigurationError("max_transient_retries must be non-negative")
        if len(self.backoff_seconds) < self.max_transient_retries:
            raise TushareConfigurationError("backoff schedule is shorter than retry count")
        if any(delay < 0 for delay in self.backoff_seconds):
            raise TushareConfigurationError("backoff delays must be non-negative")


class SecureTushareClient:
    """Allow-listed HTTPS JSON POST client with bounded transient retries."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30,
        max_transient_retries: int = 2,
        backoff_seconds: Sequence[float] = (1, 3),
        opener: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._token = (token or "").strip() or load_tushare_token(env=env)
        if any(character in self._token for character in ("\r", "\n", "\x00")):
            raise TushareConfigurationError("Tushare token contains forbidden characters")
        self.base_url = _validated_url(base_url)
        self.policy = ClientPolicy(
            timeout_seconds=float(timeout_seconds),
            max_transient_retries=int(max_transient_retries),
            backoff_seconds=tuple(float(value) for value in backoff_seconds),
        )
        self._opener = (
            opener
            if opener is not None
            else build_opener(_RejectRedirects(), HTTPSHandler(context=_verified_ssl_context()))
        )
        self._sleep = sleep

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"timeout_seconds={self.policy.timeout_seconds!r}, token='[REDACTED_TOKEN]')"
        )

    def call(
        self,
        api_name: str,
        params: Mapping[str, Any],
        fields: Sequence[str] | str | None = None,
    ) -> RawApiResponse:
        """Call one approved endpoint and return parsed JSON plus raw bytes."""

        normalized_fields = self._validate_request(api_name, params, fields)
        body = json.dumps(
            {
                "api_name": api_name,
                "token": self._token,
                "params": dict(params),
                "fields": ",".join(normalized_fields),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

        for attempt in range(self.policy.max_transient_retries + 1):
            request = Request(
                self.base_url,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "sw-industry-valuation-dashboard/1.0",
                },
                method="POST",
            )
            try:
                response = self._open(request)
                try:
                    status_value = getattr(response, "status", None)
                    if status_value is None:
                        status_value = response.getcode()
                    status = int(status_value)
                    final_url = str(response.geturl())
                    if final_url.rstrip("/") != self.base_url.rstrip("/"):
                        raise TushareRedirectError("redirected response URL rejected")
                    raw_bytes = response.read()
                    if self._token.encode("utf-8") in raw_bytes:
                        raise TushareProtocolError(
                            "response contains credential material"
                        )
                    headers = {str(k): str(v) for k, v in response.headers.items()}
                finally:
                    response.close()
                if not 200 <= status < 300:
                    if status in TRANSIENT_HTTP_STATUS and attempt < self.policy.max_transient_retries:
                        self._sleep(self.policy.backoff_seconds[attempt])
                        continue
                    raise TushareTransportError(f"unexpected HTTP status {status}")
                payload = _strict_json_loads(raw_bytes, credential=self._token)
                _reject_credential_material(payload, self._token)
                return RawApiResponse(
                    payload,
                    raw_bytes=raw_bytes,
                    http_status=status,
                    headers=headers,
                    api_name=api_name,
                    attempt_count=attempt + 1,
                )
            except TushareRedirectError:
                raise
            except HTTPError as exc:
                try:
                    exc.read()
                finally:
                    exc.close()
                if 300 <= exc.code < 400:
                    raise TushareRedirectError(f"redirect rejected (HTTP {exc.code})") from None
                if exc.code in TRANSIENT_HTTP_STATUS and attempt < self.policy.max_transient_retries:
                    self._sleep(self.policy.backoff_seconds[attempt])
                    continue
                raise TushareTransportError(f"HTTP request failed with status {exc.code}") from None
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
                if attempt < self.policy.max_transient_retries:
                    self._sleep(self.policy.backoff_seconds[attempt])
                    continue
                raise TushareTransportError(
                    f"HTTPS transport failed after {attempt + 1} attempt(s): "
                    f"{_redact(type(exc).__name__, self._token)}"
                ) from None

        raise AssertionError("retry loop exhausted without returning or raising")

    def _open(self, request: Request) -> Any:
        if hasattr(self._opener, "open"):
            return self._opener.open(request, timeout=self.policy.timeout_seconds)
        if callable(self._opener):
            return self._opener(request, timeout=self.policy.timeout_seconds)
        raise TushareConfigurationError("opener must be callable or expose open()")

    @staticmethod
    def _validate_request(
        api_name: str,
        params: Mapping[str, Any],
        fields: Sequence[str] | str | None,
    ) -> tuple[str, ...]:
        if api_name not in ALLOWED_APIS:
            raise TushareConfigurationError(f"API is not allowed: {api_name!r}")
        if not isinstance(params, Mapping):
            raise TushareConfigurationError("params must be a mapping")
        keys = set(params)
        unexpected = keys - ENDPOINT_PARAMS[api_name]
        missing = REQUIRED_PARAMS[api_name] - keys
        if unexpected:
            raise TushareConfigurationError(
                f"unexpected params for {api_name}: {sorted(unexpected)!r}"
            )
        if missing:
            raise TushareConfigurationError(
                f"missing params for {api_name}: {sorted(missing)!r}"
            )
        empty_required = sorted(
            key
            for key in REQUIRED_PARAMS[api_name]
            if params[key] is None
            or (isinstance(params[key], str) and not params[key].strip())
        )
        if empty_required:
            raise TushareConfigurationError(
                f"empty required params for {api_name}: {empty_required!r}"
            )
        if api_name == "sw_daily":
            by_date = keys == {"trade_date"}
            by_code_range = keys == {"ts_code", "start_date", "end_date"}
            if not (by_date or by_code_range):
                raise TushareConfigurationError(
                    "sw_daily requires exactly trade_date or ts_code/start_date/end_date"
                )
        elif api_name == "index_member_all":
            selectors = keys & {"l1_code", "l2_code", "l3_code", "ts_code"}
            if len(selectors) != 1 or keys != selectors | {"is_new"}:
                raise TushareConfigurationError(
                    "index_member_all requires is_new and exactly one code selector"
                )
            if params.get("is_new") not in {"Y", "N"}:
                raise TushareConfigurationError("index_member_all is_new must be Y or N")
        elif api_name == "daily_basic" and keys != {"trade_date"}:
            raise TushareConfigurationError(
                "daily_basic is restricted to one explicit trade_date"
            )
        empty_supplied = sorted(
            key
            for key, value in params.items()
            if value is None or (isinstance(value, str) and not value.strip())
        )
        if empty_supplied:
            raise TushareConfigurationError(
                f"empty params for {api_name}: {empty_supplied!r}"
            )
        try:
            json.dumps(dict(params), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TushareConfigurationError("params are not strict JSON values") from exc

        if fields is None:
            normalized = ENDPOINT_FIELDS[api_name]
        elif isinstance(fields, str):
            normalized = tuple(part.strip() for part in fields.split(",") if part.strip())
        else:
            normalized = tuple(str(part).strip() for part in fields)
        if not normalized or any(not item for item in normalized):
            raise TushareConfigurationError("fields must not be empty")
        if len(set(normalized)) != len(normalized):
            raise TushareConfigurationError("fields contain duplicates")
        unexpected_fields = set(normalized) - set(ENDPOINT_FIELDS[api_name])
        if unexpected_fields:
            raise TushareConfigurationError(
                f"unexpected fields for {api_name}: {sorted(unexpected_fields)!r}"
            )
        return normalized


def decode_rows(
    response: Mapping[str, Any],
    *,
    api_name: str | None = None,
    expected_fields: Sequence[str] | None = None,
    row_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Decode Tushare's fields/items envelope into independent row mappings."""

    if not isinstance(response, Mapping):
        raise TushareProtocolError("response must be a mapping")
    if api_name is not None:
        if api_name not in ALLOWED_APIS:
            raise TushareConfigurationError(f"API is not allowed: {api_name!r}")
        response_api = getattr(response, "api_name", api_name)
        if response_api != api_name:
            raise TushareProtocolError(
                f"response endpoint mismatch: expected {api_name!r}, got {response_api!r}"
            )
    code = response.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        raise TushareProtocolError("response code must be an integer")
    if code != 0:
        raise TushareApiError(code, "upstream message withheld", response)
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise TushareProtocolError("response data must be an object")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise TushareProtocolError("response data.fields must be a list of strings")
    if len(set(fields)) != len(fields):
        raise TushareProtocolError("response fields contain duplicates")
    if expected_fields is None and api_name is not None:
        expected_fields = ENDPOINT_FIELDS[api_name]
    if expected_fields is not None and set(fields) != set(expected_fields):
        raise TushareProtocolError(
            "response field set differs from the frozen endpoint schema"
        )
    if not isinstance(items, list):
        raise TushareProtocolError("response data.items must be a list")
    if row_limit is not None:
        if row_limit <= 0:
            raise TushareConfigurationError("row_limit must be positive")
        if len(items) >= row_limit:
            raise TushareProtocolError(
                f"response touched endpoint row limit ({len(items)} >= {row_limit})"
            )
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        if not isinstance(item, list) or len(item) != len(fields):
            raise TushareProtocolError(
                f"response item {position} does not match the returned field list"
            )
        rows.append(dict(zip(fields, item, strict=True)))
    return rows


# Backwards-friendly short name for orchestration code.
TushareClient = SecureTushareClient


__all__ = [
    "ALLOWED_APIS",
    "DEFAULT_BASE_URL",
    "ENDPOINT_FIELDS",
    "RawApiResponse",
    "SecureTushareClient",
    "TushareApiError",
    "TushareClient",
    "TushareClientError",
    "TushareConfigurationError",
    "TushareProtocolError",
    "TushareRedirectError",
    "TushareTransportError",
    "decode_rows",
    "load_tushare_token",
]
