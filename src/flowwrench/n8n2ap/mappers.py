"""Per-node mappers to Activepieces steps (port of ``lib/mappers.js``)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from .graph import node_kind
from .input import InputError, validate_json_data
from .jscompat import (
    OBJECT_PROTOTYPE_KEYS,
    UNDEFINED,
    JSONSyntaxError,
    coalesce,
    includes,
    is_array,
    is_finite_number,
    is_integer,
    is_number,
    is_object,
    js_entries,
    js_items,
    js_keys,
    js_string,
    js_trim,
    js_typeof,
    number_to_string,
    own,
    parse_json,
    prop,
    quote,
    split_whitespace,
    strict_equal,
    truthy,
    utf8,
)
from .timezones import is_valid_timezone

Translate = Callable[[Any], Any]

AP_COMMIT = "bf145a07489f5bc32a83e35e68564b861389aaed"
EPOCH = "2026-11-11T00:00:00.000Z"
PIECES = {"http": "0.13.3", "webhook": "0.1.43", "schedule": "0.1.22"}
# All pinned source kinds, including those outside our representable subset.
SCHEDULE_FIELDS = ("seconds", "minutes", "hours", "days", "weeks", "months", "cronExpression")
PASS_CODE = "export const code = async (inputs) => inputs.data;"
_UNSAFE_NAMES = ("__proto__", "constructor", "prototype")
_SAFE_KEY = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,40}")
_SET_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_ABSOLUTE_HTTP = re.compile(r"https?://")
_CRON_TERM = re.compile(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?")
_WEEKDAY_TERM = re.compile(r"(?:[a-z]{3}|[0-9]+)(?:-(?:[a-z]{3}|[0-9]+))?", re.IGNORECASE | re.ASCII)
_WEEKDAY_NAME = re.compile(r"[a-z]{3}", re.IGNORECASE | re.ASCII)
_WEEKDAY_NAMES = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


def base(n: dict[str, Any], name: str) -> dict[str, Any]:
    return {"name": name, "displayName": n["name"], "valid": True, "lastUpdatedDate": EPOCH}


def code_step(n: dict[str, Any], name: str, code: str, input_: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**base(n, name), "type": "CODE",
            "settings": {"input": {} if input_ is None else input_,
                         "sourceCode": {"code": code, "packageJson": '{"dependencies":{}}'}}}


def fail_step(n: dict[str, Any], name: str, reason: str) -> dict[str, Any]:
    return code_step(n, name, f"export const code = async () => {{ throw new Error({quote('n8n2ap: ' + reason)}); }};")


def piece(n: dict[str, Any], name: str, kind: str, operation: str, input_: dict[str, Any],
          trigger: bool = False) -> dict[str, Any]:
    return {**base(n, name), "type": "PIECE_TRIGGER" if trigger else "PIECE", "settings": {
        "pieceName": f"@activepieces/piece-{kind}", "pieceVersion": PIECES[kind],
        ("triggerName" if trigger else "actionName"): operation, "input": input_, "propertySettings": {},
    }}


def _safe_key(key: str) -> str:
    return key if _SAFE_KEY.fullmatch(key) else "custom field"


def check_keys(obj: dict[str, Any], allowed: list[str] | tuple[str, ...], issues: list[str],
               label: str = "parameter") -> None:
    for key in js_keys(obj):
        if key not in allowed:
            issues.append(f"Unsupported {label} ({_safe_key(key)})")


def _pair_object(spec: Any, issues: list[str], label: str) -> dict[str, Any]:
    if spec is UNDEFINED:
        return {}
    if not is_object(spec) or not is_array(spec.get("parameters")):
        issues.append(f"Unsupported {label} format")
        return {}
    result: dict[str, Any] = {}
    for p in spec["parameters"]:
        name = p.get("name") if is_object(p) else None
        # ``p.name in result`` also sees Object.prototype members such as "toString".
        if (not is_object(p) or not isinstance(name, str) or not name or name.startswith("=") or "{{" in name
                or name in _UNSAFE_NAMES or name in result or name in OBJECT_PROTOTYPE_KEYS):
            issues.append(f"Invalid, dynamic or duplicate {label} name")
            continue
        check_keys(p, ["name", "value"], issues, label)
        result[name] = coalesce(p.get("value"), "")
    # A JavaScript object enumerates integer-like keys first.
    return dict(js_items(result))


def _timezone(workflow: dict[str, Any], issues: list[str]) -> Any:
    tz = prop(workflow.get("settings"), "timezone")
    if not truthy(tz):
        issues.append("Instance timezone is unknown; UTC is a review-only default")
        return "UTC"
    # ``new Intl.DateTimeFormat('en', { timeZone: tz })`` converts tz with ToString.
    if is_valid_timezone(js_string(tz)):
        return tz
    issues.append("Invalid schedule timezone")
    return "UTC"


def _num(text: str) -> float:
    # JavaScript ``Number(digits)``; float() has no digit-count limit, unlike int().
    return float(text)


def _cron_valid(cron: Any) -> bool:
    # Portable numeric five-field subset; AP also accepts more dialects which we deliberately refuse.
    ranges = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    if not isinstance(cron, str):
        return False
    fields = split_whitespace(js_trim(cron))
    if len(fields) != 5:
        return False
    for index, field in enumerate(fields):
        low, high = ranges[index]
        for term in field.split(","):
            m = _CRON_TERM.fullmatch(term)
            if not m or (m.group(2) is not None and (_num(m.group(2)) < 1 or _num(m.group(2)) > high - low + 1)):
                return False
            if m.group(1) == "*":
                continue
            bounds = [_num(x) for x in m.group(1).split("-")]
            a = bounds[0]
            b = bounds[1] if len(bounds) > 1 else a
            if not (a >= low and b <= high and a <= b):
                return False
    return True


def _weekday_name(match: re.Match[str]) -> str:
    name = match.group()
    upper = name.upper()
    return str(_WEEKDAY_NAMES.index(upper)) if upper in _WEEKDAY_NAMES else name


def _cron_weekdays(field: str) -> str:
    terms = []
    for term in field.split(","):
        # Names are complete range endpoints, never steps or dialect operators.
        if not _WEEKDAY_TERM.fullmatch(term):
            terms.append(term)
        else:
            terms.append(_WEEKDAY_NAME.sub(_weekday_name, term))
    numeric = ",".join(terms)
    if not _cron_valid(f"0 0 * * {numeric}"):
        return numeric
    days: set[int] = set()
    for term in numeric.split(","):
        parts = term.split("/")
        range_text = parts[0]
        step = parts[1] if len(parts) > 1 else None
        if range_text == "*":
            a, b = 0, 7
        else:
            bounds = [int(_num(x)) for x in range_text.split("-")]
            a = bounds[0]
            b = bounds[1] if len(bounds) > 1 else None
        end = b if b is not None else (a if step is None else 7)
        increment = int(_num(step)) if step is not None else 1
        day = a
        while day <= end:
            days.add(day % 7)
            day += increment
    # n8n treats all seven weekdays as wildcard for DOM/DOW matching. The
    # pinned target parser needs 0..7, including the second Sunday spelling.
    # Keep the bare wildcard and partial-week numeric spellings unchanged.
    return "0-7" if len(days) == 7 and numeric != "*" else numeric


def _custom_cron(value: Any, issues: list[str]) -> str:
    if not isinstance(value, str):
        issues.append("Missing cron expression")
        return "0 0 * * *"
    fields = split_whitespace(js_trim(value))
    if len(fields) == 6 and fields[0] == "0":
        fields = fields[1:]
    if len(fields) == 5:
        fields[4] = _cron_weekdays(fields[4])
    cron = " ".join(fields)
    if not _cron_valid(cron):
        issues.append("Only numeric five-field cron (or six fields with zero seconds), with optional English weekday"
                      " names in singles/lists/ascending ranges, is supported")
    return cron


def identity_clock(source_key: str, label: str, size: int) -> int:
    digest = hashlib.sha256(utf8(f"{source_key}:{label}")).digest()
    return int.from_bytes(digest[:4], "big") % size


def _preset_identity(n: dict[str, Any], workflow: dict[str, Any], issues: list[str]) -> str | None:
    # Pinned n8n derives preset clocks from workflow/node IDs. Return the seed
    # only when its second is representable by our five-field cron subset.
    workflow_id = workflow.get("id")
    if not isinstance(workflow_id, str) or not workflow_id or not truthy(n.get("id")):
        issues.append("Schedule second default requires source identity; export workflow/node IDs or use an explicit"
                      " zero-second cronExpression")
        return None
    source_key = f"{workflow_id}:{js_string(n['id'])}"
    if identity_clock(source_key, "second", 60) == 0:
        return source_key
    issues.append("Source schedule second default is nonzero and cannot be preserved by five-field cron; review the"
                  " schedule or use an explicit zero-second cronExpression")
    return None


def _integer(value: Any, low: int, high: int, issues: list[str]) -> Any:
    if not is_integer(value) or value < low or value > high:
        issues.append("Schedule integer is out of range")
        return low
    return value


def _preset_clock(r: dict[str, Any], field: str, high: int, source_key: str | None, issues: list[str]) -> Any:
    if r.get(field) is None:
        if source_key is not None:
            return identity_clock(source_key, "hour" if field == "triggerAtHour" else "minute", high + 1)
        issues.append(f"Missing {field}: source identity must establish a zero-second phase before deriving this"
                      " clock; export workflow/node IDs or use an explicit zero-second cronExpression")
        return 0  # Review-only: any issue disables the trigger in convert().
    return _integer(r[field], 0, high, issues)


def _text(value: Any) -> str:
    return number_to_string(value) if is_number(value) else js_string(value)


def _schedule_cron(n: dict[str, Any], workflow: dict[str, Any], issues: list[str]) -> str:
    p = n["parameters"]
    kind = node_kind(n)
    check_keys(p, ["triggerTimes"] if kind == "cron" else ["rule"], issues)
    rules = prop(prop(p, "triggerTimes"), "item") if kind == "cron" else prop(prop(p, "rule"), "interval")
    if not is_array(rules) or len(rules) != 1 or not is_object(rules[0]):
        issues.append("Exactly one schedule rule is supported")
        return "0 0 * * *"
    r: dict[str, Any] = rules[0]
    if kind == "cron":
        check_keys(r, ["mode", "cronExpression", "hour", "minute", "weekday"], issues, "cron option")
        mode = r.get("mode")
        if strict_equal(mode, "custom"):
            return _custom_cron(r.get("cronExpression"), issues)
        if includes(("everyMinute", "everyHour", "everyDay", "everyWeek"), mode):
            # Legacy Cron v1 samples randomInt(60) when activated. Exported IDs and
            # explicit business clocks cannot establish that runtime second.
            issues.append("Legacy cron preset uses a runtime-random second that cannot be established from an export;"
                          " review the schedule or use an explicit zero-second custom cronExpression")
            return "0 0 * * *"  # Review-only; convert disables triggers with issues.
    else:
        check_keys(r, ["field", "expression", "minutesInterval", "hoursInterval", "daysInterval", "weeksInterval",
                       "monthsInterval", "triggerAtDayOfMonth", "triggerAtDay", "triggerAtHour", "triggerAtMinute"],
                   issues, "schedule option")
        # Pinned withIntervalDefaults selects days for unrecognized fields. JSON
        # numbers, booleans, plain records and JSON arrays cannot match string kinds.
        # Keep non-plain library objects partial; never coerce container contents.
        # Preserve recognized unsupported kinds, expressions and the legacy boundary.
        raw = r.get("field")
        plain_record = type(raw) is dict
        json_array = type(raw) is list
        default_to_days = (raw is None or is_number(raw) or isinstance(raw, bool) or plain_record or json_array
                           or (isinstance(raw, str) and not raw.startswith("=") and raw not in SCHEDULE_FIELDS))
        field: Any = "days" if default_to_days else raw
        is_field = (lambda name: isinstance(field, str) and field == name)
        if is_field("cronExpression"):
            return _custom_cron(r.get("expression"), issues)
        source_key = (_preset_identity(n, workflow, issues)
                      if any(is_field(name) for name in ("minutes", "hours", "days", "weeks", "months")) else None)
        if is_field("minutes"):
            m = _integer(coalesce(r.get("minutesInterval"), 5), 1, 59, issues)
            if 60 % m:
                issues.append("Minute interval must divide 60 to preserve cadence")
            return f"*/{_text(m)} * * * *"
        if is_field("hours"):
            h = _integer(coalesce(r.get("hoursInterval"), 1), 1, 23, issues)
            if 24 % h:
                issues.append("Hour interval must divide 24 to preserve cadence")
            minute = _preset_clock(r, "triggerAtMinute", 59, source_key, issues)
            return f"{_text(minute)} */{_text(h)} * * *"
        if is_field("days") and strict_equal(coalesce(r.get("daysInterval"), 1), 1):
            minute = _preset_clock(r, "triggerAtMinute", 59, source_key, issues)
            hour = _preset_clock(r, "triggerAtHour", 23, source_key, issues)
            return f"{_text(minute)} {_text(hour)} * * *"
        months = coalesce(r.get("monthsInterval"), 1)
        if is_field("months") and is_integer(months) and months >= 1 and 12 % months == 0:
            # January-anchored divisors have N-month gaps, including year rollover.
            # Pinned absolute-month recurrence accepts the first candidate and every
            # later one; skipped dates only widen the gap. No state needs migrating.
            # The derived day is 1-28; explicit 29-31 skip short months.
            day = r.get("triggerAtDayOfMonth")
            if day is None:
                if source_key is not None:
                    day = 1 + identity_clock(source_key, "dayOfMonth", 28)
                else:
                    issues.append("Missing triggerAtDayOfMonth: source identity must establish a zero-second phase"
                                  " before deriving this day")
            else:
                day = _integer(day, 1, 31, issues)
            minute = _preset_clock(r, "triggerAtMinute", 59, source_key, issues)
            hour = _preset_clock(r, "triggerAtHour", 23, source_key, issues)
            return f"{_text(minute)} {_text(hour)} {_text(coalesce(day, 1))} */{_text(months)} *"
        if is_field("weeks") and strict_equal(coalesce(r.get("weeksInterval"), 1), 1):
            # Pinned one-week rules need no recurrence filter. Nullish days default
            # to Sunday, but an explicitly empty array is the source wildcard.
            days = coalesce(r.get("triggerAtDay"), [0])
            if not is_array(days) or any(not is_integer(d) or d < 0 or d > 6 for d in days):
                issues.append("Schedule weekdays must be an array of integers from 0 to 6")
                return "0 0 * * *"  # Review-only; the trigger is disabled on any issue.
            minute = _preset_clock(r, "triggerAtMinute", 59, source_key, issues)
            hour = _preset_clock(r, "triggerAtHour", 23, source_key, issues)
            weekdays = "*" if len(days) == 0 else ",".join(_text(d) for d in days)
            return f"{_text(minute)} {_text(hour)} * * {weekdays}"
    issues.append("Schedule cadence is unsupported")
    return "0 0 * * *"


def map_trigger(n: dict[str, Any], workflow: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    p = n["parameters"]
    kind = node_kind(n)
    if kind == "manualTrigger":
        check_keys(p, [], issues)
        issues.append("Manual trigger becomes an EMPTY test entry; select a real trigger before publishing")
        return {**base(n, "trigger"), "valid": False, "type": "EMPTY", "settings": {}}
    if kind == "webhook":
        check_keys(p, ["httpMethod", "path", "responseMode", "authentication", "options"], issues)
        if not strict_equal(coalesce(p.get("authentication"), "none"), "none"):
            issues.append("Webhook authentication must be recreated; trigger is disabled")
        if not includes(("onReceived", UNDEFINED), own(p, "responseMode")):
            issues.append("Custom webhook response modes are unsupported")
        method = coalesce(p.get("httpMethod"), "GET")
        if not strict_equal(method, "POST") and not strict_equal(method, "GET"):
            issues.append("Webhook method restriction is not portable")
        if is_object(p.get("options")):
            check_keys(p["options"], [], issues, "webhook option")
        elif "options" in p:
            issues.append("Invalid webhook options")
        # AP catch_webhook receives every method. The generated guard enforces GET/POST.
        return piece(n, "trigger", "webhook", "catch_webhook", {"authType": "none", "authFields": {}}, True)
    cron = _schedule_cron(n, workflow, issues)
    return piece(n, "trigger", "schedule", "cron_expression", {"cronExpression": cron, "timezone": _timezone(workflow, issues)},
                 True)


def _parse_json_field(value: Any, issues: list[str], label: str) -> Any:
    if not isinstance(value, str):
        if value is UNDEFINED:
            issues.append(f"Missing JSON {label}")
        return coalesce(value, {})
    if value.startswith("="):
        return value
    try:
        result = parse_json(value)
        validate_json_data(result)
        return result
    except (JSONSyntaxError, InputError):
        issues.append(f"Invalid or unsafe JSON {label}")
        return {}


def map_http(n: dict[str, Any], name: str, translate: Translate, issues: list[str],
             auth: dict[str, Any] | None = None) -> dict[str, Any]:
    p = n["parameters"]
    check_keys(p, ["method", "url", "authentication", *(["genericAuthType"] if auth else []), "sendHeaders",
                   "headerParameters", "specifyHeaders", "jsonHeaders", "sendQuery", "queryParameters", "specifyQuery",
                   "jsonQuery", "sendBody", "contentType", "specifyBody", "bodyParameters", "jsonBody", "options"], issues)
    if not auth and not strict_equal(coalesce(p.get("authentication"), "none"), "none"):
        issues.append("HTTP authentication must be recreated")
    method = coalesce(p.get("method"), "GET")
    if not includes(("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"), method):
        issues.append("Unsupported HTTP method")
    url = p.get("url")
    if not isinstance(url, str) or not url:
        issues.append("HTTP URL is required")
    elif not url.startswith("=") and not _ABSOLUTE_HTTP.match(url):
        issues.append("Only absolute HTTP(S) URLs are supported")
    opts = coalesce(p.get("options"), {})
    if not is_object(opts):
        issues.append("Invalid HTTP options")
    else:
        check_keys(opts, ["timeout"], issues, "HTTP option")
    body: dict[str, Any] = {}
    body_type = "none"
    if truthy(p.get("sendBody")):
        if includes(("GET", "HEAD"), method):
            issues.append("GET/HEAD request bodies are unsupported")
        if not strict_equal(coalesce(p.get("contentType"), "json"), "json"):
            issues.append("Only JSON request bodies are supported")
        body_type = "json"
        if strict_equal(coalesce(p.get("specifyBody"), "keypair"), "json"):
            body["data"] = _parse_json_field(own(p, "jsonBody"), issues, "body")
        else:
            body["data"] = _pair_object(own(p, "bodyParameters"), issues, "body")
        if not includes(("json", "keypair", UNDEFINED), own(p, "specifyBody")):
            issues.append("Unsupported body specification")

    def pairs_or_json(enabled: Any, mode: Any, pairs: Any, raw: Any, label: str) -> Any:
        if not truthy(enabled):
            return {}
        if strict_equal(mode, "json"):
            value = _parse_json_field(raw, issues, label)
            if not is_object(value):
                issues.append(f"{label} must be a JSON object")
            return value
        if mode is not UNDEFINED and not strict_equal(mode, "keypair"):
            issues.append(f"Unsupported {label} specification")
        return _pair_object(pairs, issues, label)

    url_value = coalesce(p.get("url"), "")
    headers = pairs_or_json(p.get("sendHeaders"), own(p, "specifyHeaders"), own(p, "headerParameters"),
                            own(p, "jsonHeaders"), "headers")
    query = pairs_or_json(p.get("sendQuery"), own(p, "specifyQuery"), own(p, "queryParameters"),
                          own(p, "jsonQuery"), "query")
    type_version = coalesce(n.get("typeVersion"), 1)
    input_ = translate({
        "method": method, "url": url_value, "headers": headers, "queryParams": query,
        "authType": "NONE", "authFields": {}, "body_type": body_type, "body": body,
        "response_is_binary": False, "use_proxy": False, "followRedirects": type_version >= 4, "timeout": 300,
        "failureMode": "continue_none",
    })
    # Add only converter-created references after translating untrusted source expressions.
    if auth:
        input_.update(auth)
    if is_object(opts) and "timeout" in opts:
        timeout = opts["timeout"]
        if not is_number(timeout) or timeout < 1000:
            issues.append("HTTP timeout must be at least 1000 milliseconds (Activepieces minimum)")
        else:
            input_["timeout"] = timeout / 1000
    return piece(n, name, "http", "send_request", input_)


SET_CODE_TEMPLATE = """export const code = async (inputs) => {
  const output = %s;
  for (const entry of inputs.values) {
    const value = entry.value;
    if (entry.type === 'number' && (typeof value !== 'number' || !Number.isFinite(value))) throw new Error('Set expected number');
    if (entry.type === 'boolean' && typeof value !== 'boolean') throw new Error('Set expected boolean');
    if (entry.type === 'object' && (value === null || typeof value !== 'object' || Array.isArray(value))) throw new Error('Set expected object');
    if (entry.type === 'array' && !Array.isArray(value)) throw new Error('Set expected array');
    output[entry.name] = entry.type === 'string' ? String(value) : value;
  }
  return output;
};"""


def _legacy_set_rows(values: Any, issues: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value_type, xs in js_entries(coalesce(values, {})):
        if value_type not in ("string", "number", "boolean") or not is_array(xs):
            issues.append("Unsupported legacy Set values")
            continue
        for x in xs:
            # ``{ ...x, type }``: spreading a non-object yields no usable ``name``.
            rows.append({**(x if is_object(x) else {}), "type": value_type})
    return rows


def map_set(n: dict[str, Any], name: str, current: dict[str, Any] | None, translate: Translate,
            issues: list[str]) -> dict[str, Any]:
    p = n["parameters"]
    modern = coalesce(n.get("typeVersion"), 1) >= 3
    check_keys(p, ["mode", "assignments", "includeOtherFields", "options"] if modern
               else ["values", "keepOnlySet", "options"], issues)
    if "options" in p:
        if is_object(p["options"]):
            check_keys(p["options"], [], issues, "Set option")
        else:
            issues.append("Invalid Set options")
    if modern and not strict_equal(coalesce(p.get("mode"), "manual"), "manual"):
        issues.append("Only manual Set assignments are supported")
    rows: Any = coalesce(prop(p.get("assignments"), "assignments"), []) if modern else _legacy_set_rows(p.get("values"), issues)
    if not is_array(rows):
        issues.append("Invalid Set assignments")
        return fail_step(n, name, "Invalid Set assignments")
    seen: set[str] = set()
    values: list[dict[str, Any]] = []
    for row in rows:
        row_name = row.get("name") if is_object(row) else None
        if (not is_object(row) or not isinstance(row_name, str) or not _SET_NAME.fullmatch(row_name)
                or row_name in _UNSAFE_NAMES or row_name in seen):
            issues.append("Set requires unique simple field names; dotted/dynamic keys are unsupported")
            continue
        seen.add(row_name)
        check_keys(row, ["id", "name", "value", "type"], issues, "Set assignment")
        value_type = coalesce(row.get("type"), "string")
        if not includes(("string", "number", "boolean", "object", "array"), value_type):
            issues.append("Unsupported Set value type")
        value = own(row, "value")
        if not (isinstance(value, str) and value.startswith("=")):
            if strict_equal(value_type, "number") and not is_finite_number(value):
                issues.append("Set number must be a JSON number")
            if strict_equal(value_type, "boolean") and not isinstance(value, bool):
                issues.append("Set boolean must be a JSON boolean")
            if strict_equal(value_type, "string") and not isinstance(value, str):
                issues.append("Set string must be a string")
            if strict_equal(value_type, "object") and not is_object(value):
                issues.append("Set object must be an object")
            if strict_equal(value_type, "array") and not is_array(value):
                issues.append("Set array must be an array")
        values.append({"name": row_name, "type": value_type, "value": translate(coalesce(value, None))})
    keep = truthy(p.get("includeOtherFields")) if modern else not truthy(p.get("keepOnlySet"))
    if keep and current is None:
        issues.append("Set cannot preserve an ambiguous input")
    input_: dict[str, Any] = {"values": values}
    if keep:
        input_["data"] = "{{" + (js_string(current.get("ref", UNDEFINED)) if current is not None else "undefined") + "}}"
    code = SET_CODE_TEMPLATE % ("{ ...inputs.data }" if keep else "{}")
    return code_step(n, name, code, input_)


OPS = {
    "string:equals": "TEXT_EXACTLY_MATCHES", "string:notEquals": "TEXT_DOES_NOT_EXACTLY_MATCH",
    "string:contains": "TEXT_CONTAINS", "string:notContains": "TEXT_DOES_NOT_CONTAIN",
    "string:startsWith": "TEXT_START_WITH", "string:endsWith": "TEXT_ENDS_WITH",
    "number:equals": "NUMBER_IS_EQUAL_TO", "number:gt": "NUMBER_IS_GREATER_THAN", "number:lt": "NUMBER_IS_LESS_THAN",
    "boolean:true": "BOOLEAN_IS_TRUE", "boolean:false": "BOOLEAN_IS_FALSE",
}
_SINGLE_VALUE_OPERATORS = ("BOOLEAN_IS_TRUE", "BOOLEAN_IS_FALSE")


def map_if(n: dict[str, Any], name: str, translate: Translate, issues: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(step, checks)`` (JavaScript ``{ step, checks }``)."""
    p = n["parameters"]
    check_keys(p, ["conditions", "combineOperation", "options"], issues)
    if "options" in p:
        if is_object(p["options"]):
            check_keys(p["options"], [], issues, "IF option")
        else:
            issues.append("Invalid IF options")
    spec = p.get("conditions")
    if not is_object(spec):
        issues.append("IF conditions are required")
        return fail_step(n, name, "Missing conditions"), []
    modern = is_array(spec.get("conditions"))
    rows: list[dict[str, Any]]
    if modern:
        check_keys(spec, ["options", "conditions", "combinator"], issues, "condition option")
        if "options" in spec:
            options = spec["options"]
            if not is_object(options):
                issues.append("Invalid IF condition options")
            else:
                check_keys(options, ["caseSensitive", "leftValue", "typeValidation", "version"], issues, "condition option")
                validation = options.get("typeValidation")
                if truthy(validation) and not strict_equal(validation, "strict"):
                    issues.append("Loose IF type coercion is unsupported")
        combine = coalesce(spec.get("combinator"), "and")
        rows = []
        for r in spec["conditions"]:
            if is_object(r):
                operator = r.get("operator")
                rows.append({"value1": r.get("leftValue"), "value2": r.get("rightValue"),
                             "type": prop(operator, "type"), "operation": prop(operator, "operation"), "original": r})
            else:
                rows.append({"original": r})
    else:
        rows = []
        for value_type, xs in js_entries(spec):
            if not is_array(xs):
                issues.append("Invalid legacy IF conditions")
                continue
            for r in xs:
                rows.append({**(r if is_object(r) else {}), "type": value_type, "original": r})
        combine_operation = own(p, "combineOperation")
        combine = "or" if strict_equal(combine_operation, "any") else "and"
        if combine_operation is not UNDEFINED and not includes(("all", "any"), combine_operation):
            issues.append("Invalid IF combineOperation")
    if not rows or not includes(("and", "or"), combine):
        issues.append("IF requires conditions with AND/OR combination")
    checks: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    for r in rows:
        original = r["original"]
        if not is_object(original):
            issues.append("Invalid IF condition")
            conditions.append({"firstValue": "", "operator": "EXISTS"})
            continue
        check_keys(original, ["id", "leftValue", "rightValue", "operator"] if modern
                   else ["value1", "value2", "operation"], issues, "IF condition")
        if modern and is_object(original.get("operator")):
            check_keys(original["operator"], ["type", "operation", "singleValue"], issues, "IF operator")
        row_type = r.get("type")
        op = r.get("operation")
        value2 = r.get("value2")
        if not modern and strict_equal(row_type, "boolean") and strict_equal(op, "equal") and isinstance(value2, bool):
            op = js_string(value2)
        if strict_equal(op, "equal"):
            op = "equals"
        if strict_equal(op, "notEqual"):
            op = "notEquals"
        if not modern and strict_equal(op, "larger"):
            op = "gt"
        if not modern and strict_equal(op, "smaller"):
            op = "lt"
        operator = OPS.get(f"{js_string(row_type)}:{js_string(op)}")
        if operator is None:
            issues.append("Unsupported IF comparison operator")
        first = translate(coalesce(r.get("value1"), ""))
        second = translate(coalesce(value2, ""))
        if (js_typeof(first) == "object" and first is not None) or (js_typeof(second) == "object" and second is not None):
            issues.append("IF object/array comparisons are unsupported")
        single = operator in _SINGLE_VALUE_OPERATORS
        for value in ([first] if single else [first, second]):
            checks.append({"type": row_type, "value": value})
            if not (isinstance(value, str) and "{{" in value) and not strict_equal(js_typeof(value), row_type):
                issues.append("IF literal operand has the wrong comparison type")
        condition: dict[str, Any] = {"firstValue": js_string(coalesce(first, ""))}
        if not single:
            condition["secondValue"] = js_string(coalesce(second, ""))
        condition["operator"] = coalesce(operator, "EXISTS")
        if strict_equal(row_type, "string"):
            condition["caseSensitive"] = coalesce(prop(spec.get("options"), "caseSensitive"), True) if modern else True
        conditions.append(condition)
    step = {**base(n, name), "type": "ROUTER", "settings": {
        "executionType": "EXECUTE_FIRST_MATCH", "branches": [
            {"branchName": "True", "branchType": "CONDITION",
             "conditions": [conditions] if strict_equal(combine, "and") else [[c] for c in conditions]},
            {"branchName": "False", "branchType": "FALLBACK"},
        ],
    }, "children": [None, None]}
    return step, checks


def map_pass(n: dict[str, Any], name: str, current: dict[str, Any] | None, issues: list[str]) -> dict[str, Any]:
    check_keys(n["parameters"], [], issues)
    if current is None:
        issues.append("Pass-through has no unambiguous input")
    ref = js_string(current.get("ref", UNDEFINED)) if current is not None else "undefined"
    return code_step(n, name, PASS_CODE, {"data": "{{" + ref + "}}"})
