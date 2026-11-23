"""Deterministic structured data converter, statistical profiler, query & utility engine.

Pure domain module: stdlib only, zero I/O. Eliminates model hallucinations on
CSV/JSON/TSV/INI/TOML/Markdown conversions, numeric column profiling, safe
filtering/aggregation, Base64/URL/Hex/timestamp conversions, and 5-field cron
schedule calculations.
"""
from __future__ import annotations

import base64
import configparser
import csv
import datetime as _dt
import io
import json
import math
import statistics
import tomllib
from collections import Counter
from typing import Any

_UNRESERVED_URL_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _percent_encode(text: str) -> str:
    return "".join(
        chr(b) if b in _UNRESERVED_URL_BYTES else f"%{b:02X}"
        for b in text.encode("utf-8")
    )


def _percent_decode(text: str, *, plus_as_space: bool = False) -> str:
    raw = text.replace("+", " ") if plus_as_space else text
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] == "%" and i + 2 < n:
            try:
                out.append(int(raw[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.extend(raw[i].encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _parse_query_string(qs: str) -> dict[str, Any]:
    out: dict[str, list[str]] = {}
    for pair in qs.lstrip("?").split("&"):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
        else:
            k, v = pair, ""
        dk = _percent_decode(k, plus_as_space=True)
        dv = _percent_decode(v, plus_as_space=True)
        out.setdefault(dk, []).append(dv)
    return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


def _parse_input(data: str, input_format: str) -> tuple[Any, str]:
    fmt = (input_format or "auto").strip().lower()
    text = str(data or "")
    stripped = text.strip()

    if fmt == "auto":
        if stripped.startswith(("{", "[")):
            fmt = "json"
        elif "\t" in stripped.splitlines()[0] if stripped else False:
            fmt = "tsv"
        elif stripped.startswith("[") and "=" in stripped:
            fmt = "ini"
        elif "," in (stripped.splitlines()[0] if stripped else "") and len(stripped.splitlines()) >= 2:
            fmt = "csv"
        elif "=" in stripped:
            fmt = "toml"
        else:
            fmt = "json"

    if fmt == "json":
        return json.loads(stripped), "json"
    if fmt == "jsonl":
        rows = [json.loads(ln) for ln in stripped.splitlines() if ln.strip()]
        return rows, "jsonl"
    if fmt in ("csv", "tsv"):
        delim = "\t" if fmt == "tsv" else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        return [dict(r) for r in reader], fmt
    if fmt == "toml":
        return tomllib.loads(text), "toml"
    if fmt == "ini":
        cp = configparser.ConfigParser()
        cp.read_string(text)
        out: dict[str, Any] = {}
        if cp.defaults():
            out["DEFAULT"] = dict(cp.defaults())
        for sec in cp.sections():
            out[sec] = dict(cp.items(sec))
        return out, "ini"
    if fmt == "query":
        return _parse_query_string(stripped), "query"
    raise ValueError(f"Unsupported input_format '{fmt}'. Use auto, json, jsonl, csv, tsv, toml, ini, or query.")


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(_flatten(v, key))
            else:
                out[key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                out.update(_flatten(v, key))
            else:
                out[key] = v
    else:
        out[prefix or "$"] = obj
    return out


def _as_table_rows(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        rows: list[dict[str, Any]] = []
        for item in obj:
            if isinstance(item, dict):
                rows.append(item)
            else:
                rows.append({"value": item})
        return rows
    if isinstance(obj, dict):
        return [obj]
    return [{"value": obj}]


def _format_output(obj: Any, output_format: str) -> str:
    fmt = (output_format or "json").strip().lower()
    if fmt == "json":
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if fmt == "json_compact":
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
    if fmt == "jsonl":
        rows = obj if isinstance(obj, list) else [obj]
        return "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
    if fmt == "flat":
        return json.dumps(_flatten(obj), indent=2, ensure_ascii=False, default=str)
    if fmt in ("csv", "tsv"):
        rows = _as_table_rows(obj)
        cols: list[str] = []
        for r in rows:
            for k in r.keys():
                if k not in cols:
                    cols.append(str(k))
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, delimiter="\t" if fmt == "tsv" else ",")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        return buf.getvalue().rstrip("\r\n")
    if fmt == "markdown":
        rows = _as_table_rows(obj)
        if not rows:
            return "| (empty) |\n| --- |"
        cols = []
        for r in rows:
            for k in r.keys():
                if k not in cols:
                    cols.append(str(k))
        header = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join("---" for _ in cols) + " |"
        body = []
        for r in rows[:200]:
            vals = [str(r.get(c, "")).replace("|", "\\|").replace("\n", " ") for c in cols]
            body.append("| " + " | ".join(vals) + " |")
        return "\n".join([header, sep, *body])
    if fmt == "ini":
        if not isinstance(obj, dict):
            raise ValueError("INI output requires a top-level JSON object/mapping.")
        cp = configparser.ConfigParser()
        for sec, val in obj.items():
            if isinstance(val, dict):
                cp[str(sec)] = {str(k): str(v) for k, v in val.items()}
            else:
                if "DEFAULT" not in cp:
                    pass
                cp.setdefault("main", {})[str(sec)] = str(val)
        buf = io.StringIO()
        cp.write(buf)
        return buf.getvalue().rstrip("\r\n")
    raise ValueError(f"Unsupported output_format '{fmt}'. Use json, json_compact, jsonl, csv, tsv, markdown, flat, or ini.")


def _to_float(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(float(val)) else None
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s:
            return None
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def _profile_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cols: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(str(k))
    col_profiles: dict[str, Any] = {}
    for c in cols:
        values = [r.get(c) for r in rows]
        non_null = [v for v in values if v is not None and v != ""]
        null_count = len(values) - len(non_null)
        nums = [n for n in (_to_float(v) for v in non_null) if n is not None]
        distinct_strs = [str(v) for v in non_null]
        top_vals = [{"value": val, "count": cnt} for val, cnt in Counter(distinct_strs).most_common(5)]

        prof: dict[str, Any] = {
            "non_null": len(non_null),
            "null_count": null_count,
            "unique_count": len(set(distinct_strs)),
            "inferred_type": "numeric" if non_null and len(nums) == len(non_null) else ("mixed" if nums else "string"),
            "top_values": top_vals,
        }
        if nums:
            nums_sorted = sorted(nums)
            p95_idx = min(len(nums_sorted) - 1, int(math.ceil(0.95 * len(nums_sorted))) - 1)
            prof["numeric"] = {
                "count": len(nums_sorted),
                "min": nums_sorted[0],
                "max": nums_sorted[-1],
                "sum": round(sum(nums_sorted), 6),
                "mean": round(statistics.fmean(nums_sorted), 6),
                "median": round(statistics.median(nums_sorted), 6),
                "p95": round(nums_sorted[p95_idx], 6),
            }
        col_profiles[c] = prof
    return {
        "row_count": len(rows),
        "column_count": len(cols),
        "columns": col_profiles,
    }


def _extract_path(obj: Any, path: str) -> Any:
    if not path or path == "$":
        return obj
    cur = obj
    for part in path.lstrip("$.").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


def _parse_cron_field(expr: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Invalid cron step '{part}'")
        else:
            base = part
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        if start < low or end > high or start > end:
            raise ValueError(f"Cron value '{part}' out of range [{low}, {high}]")
        for v in range(start, end + 1, step):
            values.add(v)
    if not values:
        raise ValueError(f"Empty cron field '{expr}'")
    return values


def _cron_next_runs(expr: str, *, count: int = 5, start_iso: str = "") -> dict[str, Any]:
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have exactly 5 fields: 'min hour dom month dow'")
    mins = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    dows_raw = _parse_cron_field(fields[4], 0, 7)
    # Standard cron: 0 and 7 are Sunday; Python weekday(): Monday=0..Sunday=6
    dows_py = {(d - 1) % 7 if d in (1, 2, 3, 4, 5, 6) else 6 for d in dows_raw}
    dom_wild = fields[2] == "*"
    dow_wild = fields[4] == "*"

    if start_iso:
        base = _dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if base.tzinfo is None:
            base = base.replace(tzinfo=_dt.timezone.utc)
    else:
        base = _dt.datetime.now(_dt.timezone.utc)

    cur = (base + _dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    runs: list[str] = []
    # Bound scan to 366 days of minutes (~527k iterations max) with fast day/hour skips
    limit_dt = cur + _dt.timedelta(days=366)
    want = max(1, min(20, int(count)))

    while cur <= limit_dt and len(runs) < want:
        if cur.month not in months:
            next_month = cur.month + 1
            next_year = cur.year + (1 if next_month > 12 else 0)
            next_month = 1 if next_month > 12 else next_month
            cur = cur.replace(year=next_year, month=next_month, day=1, hour=0, minute=0)
            continue
        dom_ok = cur.day in doms
        dow_ok = cur.weekday() in dows_py
        day_match = (dom_ok and dow_ok) if (dom_wild or dow_wild) else (dom_ok or dow_ok)
        if not day_match:
            cur = (cur + _dt.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cur.hour not in hours:
            cur = (cur + _dt.timedelta(hours=1)).replace(minute=0)
            continue
        if cur.minute in mins:
            runs.append(cur.isoformat().replace("+00:00", "Z"))
        cur = cur + _dt.timedelta(minutes=1)

    return {
        "expression": expr.strip(),
        "start_utc": base.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "next_runs_utc": runs,
    }


def _handle_timestamp(value: str) -> dict[str, Any]:
    s = value.strip()
    if not s or s.lower() == "now":
        dt = _dt.datetime.now(_dt.timezone.utc)
    else:
        try:
            num = float(s)
            # Heuristic: >= 1e12 is milliseconds
            if abs(num) >= 1e12:
                num = num / 1000.0
            dt = _dt.datetime.fromtimestamp(num, tz=_dt.timezone.utc)
        except ValueError:
            dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
    utc_dt = dt.astimezone(_dt.timezone.utc)
    return {
        "iso_utc": utc_dt.isoformat().replace("+00:00", "Z"),
        "unix_seconds": int(utc_dt.timestamp()),
        "unix_milliseconds": int(utc_dt.timestamp() * 1000),
        "weekday": utc_dt.strftime("%A"),
        "date": utc_dt.strftime("%Y-%m-%d"),
        "time_utc": utc_dt.strftime("%H:%M:%S"),
    }


def run_data_transform(
    *,
    action: str = "convert",
    data: str = "",
    input_format: str = "auto",
    output_format: str = "json",
    expression: str = "",
    encoding: str = "base64",
    limit: int = 50,
) -> dict[str, Any]:
    """Run deterministic structured data conversion, profiling, querying, encoding, or cron math."""
    act = (action or "convert").strip().lower()
    try:
        if act == "convert":
            parsed, detected = _parse_input(data, input_format)
            rendered = _format_output(parsed, output_format)
            return {
                "ok": True,
                "action": act,
                "detected_format": detected,
                "output_format": output_format,
                "result": rendered,
            }
        if act == "profile":
            parsed, detected = _parse_input(data, input_format)
            rows = _as_table_rows(parsed)
            return {
                "ok": True,
                "action": act,
                "detected_format": detected,
                "profile": _profile_rows(rows),
            }
        if act == "query":
            parsed, detected = _parse_input(data, input_format)
            expr = (expression or "$").strip()
            # Supports dot-path OR "where field==val" / "group_by field" / "sort field"
            if expr.lower().startswith("group_by "):
                field = expr.split(None, 1)[1].strip()
                rows = _as_table_rows(parsed)
                counts = Counter(str(r.get(field, "<null>")) for r in rows)
                result = [{"key": k, "count": c} for k, c in counts.most_common(limit)]
            elif expr.lower().startswith("sort "):
                parts = expr.split()
                field = parts[1]
                desc = len(parts) > 2 and parts[2].lower() in ("desc", "-1")
                rows = _as_table_rows(parsed)
                result = sorted(
                    rows,
                    key=lambda r: (
                        _to_float(r.get(field)) is None,
                        _to_float(r.get(field)) if _to_float(r.get(field)) is not None else str(r.get(field, "")),
                    ),
                    reverse=desc,
                )[:limit]
            elif expr.lower().startswith("where "):
                cond = expr.split(None, 1)[1].strip()
                rows = _as_table_rows(parsed)
                for op in ("!=", ">=", "<=", "==", ">", "<", "~="):
                    if op in cond:
                        lhs, rhs = [x.strip() for x in cond.split(op, 1)]
                        rhs_clean = rhs.strip("'\"")
                        matched = []
                        for r in rows:
                            val = r.get(lhs)
                            lf, rf = _to_float(val), _to_float(rhs_clean)
                            if op == "==":
                                ok = str(val) == rhs_clean
                            elif op == "!=":
                                ok = str(val) != rhs_clean
                            elif op == "~=":
                                ok = rhs_clean.lower() in str(val or "").lower()
                            elif lf is not None and rf is not None:
                                ok = (lf > rf) if op == ">" else (lf < rf) if op == "<" else (lf >= rf) if op == ">=" else (lf <= rf)
                            else:
                                ok = False
                            if ok:
                                matched.append(r)
                        result = matched[:limit]
                        break
                else:
                    raise ValueError("`where` expression must use one of ==, !=, >=, <=, >, <, ~=")
            else:
                result = _extract_path(parsed, expr)
            return {
                "ok": True,
                "action": act,
                "detected_format": detected,
                "expression": expr,
                "result": result,
            }
        if act in ("encode", "decode"):
            enc = (encoding or "base64").strip().lower()
            raw = str(data or "")
            if enc == "base64":
                if act == "encode":
                    out = base64.b64encode(raw.encode("utf-8")).decode("ascii")
                else:
                    padded = raw.strip() + "=" * ((4 - len(raw.strip()) % 4) % 4)
                    out = base64.b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
            elif enc == "base64url":
                if act == "encode":
                    out = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
                else:
                    padded = raw.strip() + "=" * ((4 - len(raw.strip()) % 4) % 4)
                    out = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
            elif enc == "url":
                out = _percent_encode(raw) if act == "encode" else _percent_decode(raw)
            elif enc == "hex":
                out = raw.encode("utf-8").hex() if act == "encode" else bytes.fromhex(raw.strip()).decode("utf-8", errors="replace")
            elif enc == "unicode":
                out = [{"char": ch, "codepoint": f"U+{ord(ch):04X}", "ord": ord(ch)} for ch in raw[:128]]
            else:
                raise ValueError(f"Unsupported encoding '{enc}'. Use base64, base64url, url, hex, or unicode.")
            return {"ok": True, "action": act, "encoding": enc, "result": out}
        if act == "timestamp":
            return {"ok": True, "action": act, "result": _handle_timestamp(data or expression)}
        if act == "cron":
            return {"ok": True, "action": act, "result": _cron_next_runs(expression or data, count=min(limit, 20))}
        return {
            "ok": False,
            "error": f"Unknown action '{act}'. Use convert, profile, query, encode, decode, timestamp, or cron.",
        }
    except Exception as exc:
        return {"ok": False, "action": act, "error": f"{type(exc).__name__}: {exc}"}
