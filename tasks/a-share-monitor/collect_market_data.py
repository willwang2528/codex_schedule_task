#!/usr/bin/env python3
"""Collect deterministic A-share evidence for the monitoring Agent.

The collector deliberately returns facts, scopes, timestamps, and warnings only.
Narrative market interpretation remains in TASK.md and the structured Agent.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
EASTMONEY_QUOTE_API = "https://push2delay.eastmoney.com/api/qt"
EASTMONEY_HISTORY_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_LIMIT_API = "https://push2ex.eastmoney.com"
TENCENT_KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
USER_AGENT = "Mozilla/5.0 AutomationHubMarketMonitor/1.0"
INDEX_SPECS = (
    ("shanghai", "上证指数", "1.000001", "sh000001"),
    ("shenzhen", "深证成指", "0.399001", "sz399001"),
    ("chinext", "创业板指", "0.399006", "sz399006"),
    ("star50", "科创50", "1.000688", "sh000688"),
)
MARKET_PHASE_BY_TRIGGER = {
    "09:20": "盘前/集合竞价进行中",
    "09:25": "集合竞价结束",
    "09:35": "开盘十分钟",
    "09:45": "开盘半小时",
    "11:20": "上午盘后段",
    "13:15": "午后开盘",
    "14:30": "尾盘",
    "15:01": "收盘",
}


class CollectionError(RuntimeError):
    """Raised when a public market endpoint cannot be validated."""


def _request_json(url: str, *, timeout: float = 15.0, attempts: int = 2) -> Dict[str, Any]:
    last_error = "request failed"
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, dict):
                raise CollectionError("JSON root is not an object")
            return value
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, CollectionError) as exc:
            last_error = " ".join(str(exc).split())[:300]
            if attempt < attempts:
                time.sleep(0.5 * attempt)
    raise CollectionError(last_error)


def _query_url(base: str, params: Dict[str, Any]) -> str:
    return f"{base}?{urlencode(params, safe=',:+')}"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def _percent(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _round(numerator / denominator * 100)


def _epoch_text(value: Any) -> Optional[str]:
    epoch = _number(value)
    if epoch is None or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, TIMEZONE).isoformat(timespec="seconds")


def _parse_scheduled_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CollectionError("scheduled_at must include timezone")
    return parsed.astimezone(TIMEZONE)


def _eastmoney_indices() -> Tuple[List[Dict[str, Any]], List[str]]:
    params = {
        "fltt": 2,
        "secids": ",".join(item[2] for item in INDEX_SPECS),
        "fields": "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f124",
    }
    payload = _request_json(_query_url(f"{EASTMONEY_QUOTE_API}/ulist.np/get", params))
    rows = ((payload.get("data") or {}).get("diff") if isinstance(payload.get("data"), dict) else None)
    if not isinstance(rows, list):
        raise CollectionError("Eastmoney index response has no data.diff")
    by_code = {str(row.get("f12")): row for row in rows if isinstance(row, dict)}
    indices: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for key, expected_name, secid, _ in INDEX_SPECS:
        code = secid.split(".", 1)[1]
        row = by_code.get(code)
        if not row:
            warnings.append(f"{expected_name}缺失")
            continue
        indices.append(
            {
                "key": key,
                "name": str(row.get("f14") or expected_name),
                "code": code,
                "last": _round(_number(row.get("f2"))),
                "change_pct": _round(_number(row.get("f3"))),
                "change": _round(_number(row.get("f4"))),
                "open": _round(_number(row.get("f17"))),
                "high": _round(_number(row.get("f15"))),
                "low": _round(_number(row.get("f16"))),
                "previous_close": _round(_number(row.get("f18"))),
                "turnover_yuan": _round(_number(row.get("f6")), 0),
                "data_timestamp": _epoch_text(row.get("f124")),
                "source": "Eastmoney delayed quote API",
            }
        )
    return indices, warnings


def _tencent_payload(code: str, start: str, end: str, limit: int = 8) -> Dict[str, Any]:
    param = f"{code},day,{start},{end},{limit},qfq"
    return _request_json(_query_url(TENCENT_KLINE_API, {"param": param}))


def _parse_tencent_quote(payload: Dict[str, Any], code: str) -> Dict[str, Any]:
    data = payload.get("data")
    node = data.get(code) if isinstance(data, dict) else None
    qt = node.get("qt") if isinstance(node, dict) else None
    row = qt.get(code) if isinstance(qt, dict) else None
    if not isinstance(row, list) or len(row) < 38:
        raise CollectionError(f"Tencent quote missing for {code}")
    turnover_ten_thousand = _number(row[37])
    return {
        "name": str(row[1]),
        "code": str(row[2]),
        "last": _round(_number(row[3])),
        "previous_close": _round(_number(row[4])),
        "open": _round(_number(row[5])),
        "change": _round(_number(row[31])),
        "change_pct": _round(_number(row[32])),
        "high": _round(_number(row[33])),
        "low": _round(_number(row[34])),
        "turnover_yuan": _round(turnover_ten_thousand * 10_000, 0) if turnover_ten_thousand is not None else None,
        "data_timestamp": datetime.strptime(str(row[30]), "%Y%m%d%H%M%S").replace(tzinfo=TIMEZONE).isoformat(timespec="seconds"),
    }


def _parse_tencent_breadth(payload: Dict[str, Any], code: str) -> Dict[str, int]:
    data = payload.get("data")
    node = data.get(code) if isinstance(data, dict) else None
    qt = node.get("qt") if isinstance(node, dict) else None
    row = qt.get("zhishu") if isinstance(qt, dict) else None
    if not isinstance(row, list) or len(row) < 6:
        raise CollectionError(f"Tencent breadth missing for {code}")
    values = [int(float(row[index])) for index in (2, 3, 4, 5)]
    if sum(values[:3]) != values[3]:
        raise CollectionError(f"Tencent breadth total mismatch for {code}")
    return {"up": values[0], "flat": values[1], "down": values[2], "total": values[3]}


def _current_tencent(target_date: str) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[str]]:
    responses: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_tencent_payload, qq_code, target_date, target_date, 2): (key, qq_code)
            for key, _, _, qq_code in INDEX_SPECS
        }
        for future in as_completed(futures):
            key, qq_code = futures[future]
            try:
                responses[key] = future.result()
            except CollectionError as exc:
                warnings.append(f"腾讯{qq_code}行情失败: {exc}")

    quotes: Dict[str, Dict[str, Any]] = {}
    for key, _, _, qq_code in INDEX_SPECS:
        payload = responses.get(key)
        if not payload:
            continue
        try:
            quotes[key] = _parse_tencent_quote(payload, qq_code)
        except (CollectionError, ValueError) as exc:
            warnings.append(str(exc))

    breadth_parts: List[Dict[str, int]] = []
    for key, qq_code in (("shanghai", "sh000001"), ("shenzhen", "sz399001")):
        payload = responses.get(key)
        if not payload:
            continue
        try:
            breadth_parts.append(_parse_tencent_breadth(payload, qq_code))
        except CollectionError as exc:
            warnings.append(str(exc))
    breadth: Dict[str, Any] = {
        "scope": "沪深A股，不含北交所",
        "up": None,
        "flat": None,
        "down": None,
        "total": None,
        "source": "Tencent market breadth",
    }
    if len(breadth_parts) == 2:
        for field in ("up", "flat", "down", "total"):
            breadth[field] = sum(part[field] for part in breadth_parts)
        breadth["up_share_pct"] = _percent(breadth["up"], breadth["total"])
        breadth["down_share_pct"] = _percent(breadth["down"], breadth["total"])
        breadth["advance_decline_ratio"] = _round(
            breadth["up"] / breadth["down"] if breadth["down"] else None
        )
    return quotes, breadth, warnings


def _limit_pool(endpoint: str, target_compact: str) -> Dict[str, Any]:
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 1000,
        "sort": "fbt:asc",
        "date": target_compact,
        "_": int(time.time() * 1000),
    }
    payload = _request_json(_query_url(f"{EASTMONEY_LIMIT_API}/{endpoint}", params))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CollectionError(f"{endpoint} response has no data")
    pool = data.get("pool")
    if not isinstance(pool, list):
        pool = []
    return {
        "qdate": str(data.get("qdate") or ""),
        "total": int(data.get("tc") or 0),
        "pool": [row for row in pool if isinstance(row, dict)],
    }


def _current_limit_activity(target_date: str) -> Tuple[Dict[str, Any], List[str]]:
    compact = target_date.replace("-", "")
    endpoints = {
        "limit_up": "getTopicZTPool",
        "limit_down": "getTopicDTPool",
        "broken_limit_up": "getTopicZBPool",
    }
    values: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_limit_pool, endpoint, compact): key
            for key, endpoint in endpoints.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                value = future.result()
                if value["qdate"] != compact:
                    warnings.append(f"{key}返回日期{value['qdate']}，不等于{compact}，已拒绝")
                else:
                    values[key] = value
            except CollectionError as exc:
                warnings.append(f"{key}失败: {exc}")

    up_pool = values.get("limit_up", {}).get("pool", [])
    limit_up_total = values.get("limit_up", {}).get("total")
    limit_down_total = values.get("limit_down", {}).get("total")
    broken_total = values.get("broken_limit_up", {}).get("total")
    touched_limit_up_total = (
        limit_up_total + broken_total
        if isinstance(limit_up_total, int) and isinstance(broken_total, int)
        else None
    )
    board_counts = Counter(int(row.get("lbc") or 0) for row in up_pool)
    highest_board = max(board_counts, default=0)
    leaders = [
        {
            "code": str(row.get("c") or ""),
            "name": str(row.get("n") or ""),
            "change_pct": _round(_number(row.get("zdp"))),
            "consecutive_boards": int(row.get("lbc") or 0),
            "industry": str(row.get("hybk") or ""),
            "open_count": int(row.get("zbc") or 0),
        }
        for row in sorted(up_pool, key=lambda item: (int(item.get("lbc") or 0), _number(item.get("amount")) or 0), reverse=True)[:8]
    ]
    theme_counts = Counter(str(row.get("hybk") or "未分类") for row in up_pool)
    return {
        "scope": "东方财富涨跌停专题池；按板块实际涨跌停规则归集",
        "limit_up": limit_up_total,
        "limit_down": limit_down_total,
        "broken_limit_up": broken_total,
        "touched_limit_up": touched_limit_up_total,
        "seal_rate_pct": _percent(limit_up_total, touched_limit_up_total),
        "broken_rate_pct": _percent(broken_total, touched_limit_up_total),
        "highest_consecutive_boards": highest_board or None,
        "board_distribution": {str(key): count for key, count in sorted(board_counts.items()) if key > 0},
        "leaders": leaders,
        "leading_limit_up_industries": [
            {"industry": industry, "count": count}
            for industry, count in theme_counts.most_common(5)
        ],
    }, warnings


def _market_list(order: int) -> List[Dict[str, Any]]:
    params = {
        "pn": 1,
        "pz": 100,
        "po": order,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "fields": "f12,f14,f2,f3,f5,f6,f8,f100,f124",
    }
    payload = _request_json(_query_url(f"{EASTMONEY_QUOTE_API}/clist/get", params))
    data = payload.get("data")
    rows = data.get("diff") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _current_movers() -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    results: Dict[int, List[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(_market_list, order): order for order in (1, 0)}
        for future in as_completed(futures):
            order = futures[future]
            try:
                results[order] = future.result()
            except CollectionError as exc:
                warnings.append(f"涨跌幅榜失败: {exc}")

    def select(rows: Iterable[Dict[str, Any]], *, descending: bool) -> List[Dict[str, Any]]:
        selected = []
        for row in rows:
            pct = _number(row.get("f3"))
            turnover = _number(row.get("f6"))
            if pct is None or turnover is None or turnover < 100_000_000:
                continue
            if (descending and pct <= 0) or (not descending and pct >= 0):
                continue
            selected.append(
                {
                    "code": str(row.get("f12") or ""),
                    "name": str(row.get("f14") or ""),
                    "change_pct": _round(pct),
                    "last": _round(_number(row.get("f2"))),
                    "turnover_yuan": _round(turnover, 0),
                    "industry": str(row.get("f100") or ""),
                    "data_timestamp": _epoch_text(row.get("f124")),
                }
            )
            if len(selected) >= 6:
                break
        return selected

    return {
        "scope": "沪深京A股；成交额至少1亿元；新股仍保留并以N前缀名称识别",
        "gainers": select(results.get(1, []), descending=True),
        "decliners": select(results.get(0, []), descending=False),
    }, warnings


def _historical_eastmoney(key: str, name: str, secid: str, target_date: str) -> Dict[str, Any]:
    params = {
        "secid": secid,
        "klt": 101,
        "fqt": 1,
        "lmt": 10,
        "end": target_date.replace("-", ""),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    payload = _request_json(_query_url(EASTMONEY_HISTORY_API, params))
    data = payload.get("data")
    lines = data.get("klines") if isinstance(data, dict) else None
    if not isinstance(lines, list):
        raise CollectionError(f"{name}历史K线缺失")
    parsed = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 11:
            continue
        parsed.append(
            {
                "date": parts[0],
                "open": _number(parts[1]),
                "close": _number(parts[2]),
                "high": _number(parts[3]),
                "low": _number(parts[4]),
                "volume": _number(parts[5]),
                "turnover_yuan": _number(parts[6]),
                "amplitude_pct": _number(parts[7]),
                "change_pct": _number(parts[8]),
                "change": _number(parts[9]),
                "turnover_rate_pct": _number(parts[10]),
            }
        )
    target_index = next((index for index, row in enumerate(parsed) if row["date"] == target_date), None)
    if target_index is None:
        raise CollectionError(f"{name}没有{target_date}历史数据")
    row = parsed[target_index]
    previous = parsed[target_index - 1] if target_index > 0 else None
    return {
        "key": key,
        "name": str(data.get("name") or name),
        "code": secid.split(".", 1)[1],
        "last": _round(row["close"]),
        "change_pct": _round(row["change_pct"]),
        "change": _round(row["change"]),
        "open": _round(row["open"]),
        "high": _round(row["high"]),
        "low": _round(row["low"]),
        "previous_close": _round(previous["close"] if previous else None),
        "turnover_yuan": _round(row["turnover_yuan"], 0),
        "previous_turnover_yuan": _round(previous["turnover_yuan"] if previous else None, 0),
        "data_timestamp": f"{target_date}T15:00:00+08:00",
        "source": "Eastmoney historical daily K-line API",
    }


def _historical_tencent_series(
    payload: Dict[str, Any], code: str, target_date: str
) -> Optional[Dict[str, Any]]:
    data = payload.get("data")
    node = data.get(code) if isinstance(data, dict) else None
    rows = node.get("day") if isinstance(node, dict) else None
    if not isinstance(rows, list):
        return None
    parsed = [row for row in rows if isinstance(row, list) and len(row) >= 6]
    for index, row in enumerate(parsed):
        if row[0] == target_date:
            previous_close = _number(parsed[index - 1][2]) if index > 0 else None
            close = _number(row[2])
            change = close - previous_close if close is not None and previous_close else None
            change_pct = change / previous_close * 100 if change is not None and previous_close else None
            return {
                "open": _round(_number(row[1])),
                "close": _round(close),
                "high": _round(_number(row[3])),
                "low": _round(_number(row[4])),
                "volume": _round(_number(row[5]), 0),
                "previous_close": _round(previous_close),
                "change": _round(change),
                "change_pct": _round(change_pct),
            }
    return None


def _collect_historical(target_date: str, trigger_slot: str, collected_at: datetime) -> Dict[str, Any]:
    indices: List[Dict[str, Any]] = []
    tencent_series: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures: Dict[Any, Tuple[str, str, str]] = {}
        history_start = (
            datetime.fromisoformat(target_date).date() - timedelta(days=21)
        ).isoformat()
        for key, name, secid, qq_code in INDEX_SPECS:
            futures[executor.submit(_historical_eastmoney, key, name, secid, target_date)] = ("eastmoney", key, qq_code)
            futures[executor.submit(_tencent_payload, qq_code, history_start, target_date, 20)] = ("tencent", key, qq_code)
        for future in as_completed(futures):
            source, key, qq_code = futures[future]
            try:
                value = future.result()
                if source == "eastmoney":
                    indices.append(value)
                else:
                    series = _historical_tencent_series(value, qq_code, target_date)
                    if series:
                        tencent_series[key] = series
            except CollectionError as exc:
                warnings.append(f"{source}:{key}失败: {exc}")
    order = {key: index for index, (key, _, _, _) in enumerate(INDEX_SPECS)}
    existing = {item["key"] for item in indices}
    for key, name, secid, _ in INDEX_SPECS:
        series = tencent_series.get(key)
        if key in existing or not series:
            continue
        indices.append(
            {
                "key": key,
                "name": name,
                "code": secid.split(".", 1)[1],
                "last": series["close"],
                "change_pct": series["change_pct"],
                "change": series["change"],
                "open": series["open"],
                "high": series["high"],
                "low": series["low"],
                "previous_close": series["previous_close"],
                "turnover_yuan": None,
                "previous_turnover_yuan": None,
                "data_timestamp": f"{target_date}T15:00:00+08:00",
                "source": "Tencent historical daily K-line fallback",
            }
        )
    indices.sort(key=lambda item: order[item["key"]])
    checks = []
    for item in indices:
        series = tencent_series.get(item["key"])
        other = series.get("close") if series else None
        has_eastmoney = str(item.get("source", "")).startswith("Eastmoney")
        delta = (
            abs(item["last"] - other)
            if has_eastmoney and other is not None and item["last"] is not None
            else None
        )
        checks.append(
            {
                "index": item["name"],
                "eastmoney_close": item["last"] if has_eastmoney else None,
                "tencent_close": other,
                "absolute_difference": _round(delta, 4),
                "matched": delta is not None and delta <= 0.02,
            }
        )
    sh = next((row for row in indices if row["key"] == "shanghai"), None)
    sz = next((row for row in indices if row["key"] == "shenzhen"), None)
    turnover = None
    previous_turnover = None
    if (
        sh
        and sz
        and sh.get("turnover_yuan") is not None
        and sz.get("turnover_yuan") is not None
    ):
        turnover = sh["turnover_yuan"] + sz["turnover_yuan"]
    if (
        sh
        and sz
        and sh.get("previous_turnover_yuan") is not None
        and sz.get("previous_turnover_yuan") is not None
    ):
        previous_turnover = (
            sh["previous_turnover_yuan"] + sz["previous_turnover_yuan"]
        )
    turnover_change_pct = (
        (turnover / previous_turnover - 1) * 100
        if turnover is not None and previous_turnover
        else None
    )
    return {
        "schema_version": 1,
        "status": "ok" if len(indices) == 4 and turnover is not None else "partial",
        "data_mode": "historical_close",
        "target_date": target_date,
        "trigger_slot": trigger_slot,
        "market_phase": "历史收盘复盘",
        "collected_at": collected_at.isoformat(timespec="seconds"),
        "data_timestamp": f"{target_date}T15:00:00+08:00",
        "indices": indices,
        "turnover": {
            "scope": "沪深两市，以上证指数与深证成指行情成交额之和计算",
            "total_yuan": _round(turnover, 0),
            "previous_total_yuan": _round(previous_turnover, 0),
            "change_pct": _round(turnover_change_pct),
        },
        "breadth": {"scope": "历史快照", "up": None, "flat": None, "down": None, "reason": "公开历史指数接口不提供全市场历史涨跌家数，未用当前值倒灌"},
        "limit_activity": {"limit_up": None, "limit_down": None, "broken_limit_up": None, "reason": "涨跌停专题接口拒绝返回目标历史日期，未误用最新交易日数据"},
        "movers": {"gainers": [], "decliners": [], "reason": "历史个股榜单未取得可交叉验证快照"},
        "cross_checks": checks,
        "sources": [
            {"name": "东方财富历史日线", "url": "https://quote.eastmoney.com/center/gridlist.html"},
            {"name": "腾讯证券历史日线交叉校验", "url": "https://gu.qq.com/"},
            {"name": "上交所股票成交历史概况", "url": "https://www.sse.com.cn/market/stockdata/overview/day/index_his.shtml"},
        ],
        "warnings": warnings,
        "usage_rules": [
            "仅将非空字段写入卡片；空字段要逐项说明，不得把整张卡写成当前不可核验。",
            "历史卡必须明确标注历史收盘测试，不能冒充当前实时盘面。",
        ],
    }


def _collect_current(target_date: str, trigger_slot: str, collected_at: datetime) -> Dict[str, Any]:
    warnings: List[str] = []
    indices: List[Dict[str, Any]] = []
    try:
        indices, index_warnings = _eastmoney_indices()
        warnings.extend(index_warnings)
    except CollectionError as exc:
        warnings.append(f"东方财富指数失败: {exc}")

    tencent_quotes, breadth, tencent_warnings = _current_tencent(target_date)
    warnings.extend(tencent_warnings)
    if not indices:
        for key, name, _, _ in INDEX_SPECS:
            quote = tencent_quotes.get(key)
            if quote:
                indices.append({"key": key, **quote, "name": quote.get("name") or name, "source": "Tencent quote fallback"})

    limit_activity: Dict[str, Any]
    movers: Dict[str, Any]
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_limits = executor.submit(_current_limit_activity, target_date)
        future_movers = executor.submit(_current_movers)
        try:
            limit_activity, limit_warnings = future_limits.result()
            warnings.extend(limit_warnings)
        except CollectionError as exc:
            limit_activity = {"limit_up": None, "limit_down": None, "broken_limit_up": None, "reason": str(exc)}
        try:
            movers, mover_warnings = future_movers.result()
            warnings.extend(mover_warnings)
        except CollectionError as exc:
            movers = {"gainers": [], "decliners": [], "reason": str(exc)}

    checks = []
    for item in indices:
        other = tencent_quotes.get(item["key"])
        delta = abs(item["last"] - other["last"]) if other and item.get("last") is not None and other.get("last") is not None else None
        checks.append(
            {
                "index": item["name"],
                "eastmoney_last": item.get("last"),
                "tencent_last": other.get("last") if other else None,
                "absolute_difference": _round(delta, 4),
                "matched": delta is not None and delta <= 0.02,
            }
        )
    timestamps = [datetime.fromisoformat(item["data_timestamp"]) for item in indices if item.get("data_timestamp")]
    latest_timestamp = max(timestamps) if timestamps else None
    freshness_minutes = (collected_at - latest_timestamp).total_seconds() / 60 if latest_timestamp else None
    sh = next((row for row in indices if row["key"] == "shanghai"), None)
    sz = next((row for row in indices if row["key"] == "shenzhen"), None)
    turnover = (sh.get("turnover_yuan") or 0) + (sz.get("turnover_yuan") or 0) if sh and sz else None
    valid_core = len(indices) >= 3 and turnover is not None
    return {
        "schema_version": 1,
        "status": "ok" if valid_core else "partial",
        "data_mode": "current_delayed_quote",
        "target_date": target_date,
        "trigger_slot": trigger_slot,
        "market_phase": MARKET_PHASE_BY_TRIGGER.get(trigger_slot, "盘中/阶段快照"),
        "collected_at": collected_at.isoformat(timespec="seconds"),
        "data_timestamp": latest_timestamp.isoformat(timespec="seconds") if latest_timestamp else None,
        "freshness_minutes": _round(max(0.0, freshness_minutes) if freshness_minutes is not None else None, 1),
        "indices": indices,
        "turnover": {
            "scope": "沪深两市，以上证指数与深证成指行情成交额之和计算",
            "total_yuan": _round(turnover, 0),
        },
        "breadth": breadth,
        "limit_activity": limit_activity,
        "movers": movers,
        "cross_checks": checks,
        "sources": [
            {"name": "东方财富延迟行情与涨跌停池", "url": "https://quote.eastmoney.com/center/hszs.html"},
            {"name": "腾讯证券行情交叉校验与沪深广度", "url": "https://gu.qq.com/"},
            {"name": "上交所股票成交概况", "url": "https://www.sse.com.cn/market/stockdata/overview/day/"},
        ],
        "warnings": warnings,
        "usage_rules": [
            "指数、成交额和涨跌家数必须带各自scope与data_timestamp；沪深广度不得写成沪深京全市场。",
            "cross_checks中matched为true的指数已由两个独立行情源交叉校验。",
            "仅将真正缺失的字段标注数据暂缺，禁止用当前不可核验覆盖已有有效核心字段。",
        ],
    }


def collect(scheduled_at: str, trigger_slot: str) -> Dict[str, Any]:
    scheduled = _parse_scheduled_at(scheduled_at)
    collected_at = datetime.now(TIMEZONE)
    target_date = scheduled.date().isoformat()
    if scheduled.date() < collected_at.date():
        return _collect_historical(target_date, trigger_slot, collected_at)
    return _collect_current(target_date, trigger_slot, collected_at)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled-at", required=True)
    parser.add_argument("--trigger-slot", required=True)
    args = parser.parse_args()
    try:
        result = collect(args.scheduled_at, args.trigger_slot)
    except (CollectionError, ValueError) as exc:
        result = {
            "schema_version": 1,
            "status": "unavailable",
            "target_date": args.scheduled_at[:10],
            "trigger_slot": args.trigger_slot,
            "error": " ".join(str(exc).split())[:500],
            "usage_rules": ["collector unavailable; use authoritative fallback sources and preserve missing-field scope"],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") != "unavailable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
