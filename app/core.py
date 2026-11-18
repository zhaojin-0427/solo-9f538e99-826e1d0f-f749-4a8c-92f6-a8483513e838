"""核心领域逻辑：CSV 解析校验与 UTC 小时降雨量计算。

本模块为纯逻辑，不依赖 Flask / SQLite，便于独立测试。

业务规则：
- 每小时按 60 个相邻分钟间隔计算（整点 H:00 到下一整点，共 61 个端点、60 段）。
- 每个间隔：主计两端读数齐全就取主计差值，否则备计两端齐全取备计差值，
  否则该间隔无可用数据；绝不跨设备相减。
- 计数下降按一次 10000 回绕处理：差值 = (末读数 - 初读数) mod 10000。
- 每脉冲折合 0.2 毫米；任一间隔无可用数据则整小时不可供数。
- 同一（站点, 设备, 分钟）键只接受更高修订号的记录。
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

PULSE_MM = 0.2            # 每脉冲折合雨量（毫米）
COUNTER_MOD = 10000       # 翻斗计数器量程（0~9999，回绕一次加 10000）
INTERVALS_PER_HOUR = 60   # 每小时 60 个相邻分钟间隔
MINUTE_SECONDS = 60
HOUR_SECONDS = 3600

MIN_PAIRS = 8             # 候选时差判定所需的最少配对小时数
MAX_LAG_LIMIT = 6         # 允许的最大时差（小时）
TIE_EPS = 1e-9            # 最高系数并列判定容差

DEVICE_PRIMARY = "primary"
DEVICE_BACKUP = "backup"
SOURCE_MISSING = "missing"

_DEVICE_ALIASES = {
    "primary": DEVICE_PRIMARY, "pri": DEVICE_PRIMARY, "p": DEVICE_PRIMARY,
    "main": DEVICE_PRIMARY, "master": DEVICE_PRIMARY,
    "主": DEVICE_PRIMARY, "主计": DEVICE_PRIMARY,
    "backup": DEVICE_BACKUP, "bak": DEVICE_BACKUP, "bk": DEVICE_BACKUP,
    "b": DEVICE_BACKUP, "secondary": DEVICE_BACKUP,
    "备": DEVICE_BACKUP, "备计": DEVICE_BACKUP,
}

# 可选表头行的列名别名（小写后匹配）
_HEADER_NAMES = {
    "station": "station", "站点": "station", "站号": "station", "站名": "station",
    "device": "device", "主备": "device", "主备标记": "device", "设备": "device",
    "minute": "minute", "time": "minute", "timestamp": "minute", "datetime": "minute",
    "时间": "minute", "utc时间": "minute", "utc": "minute", "分钟": "minute",
    "counter": "counter", "count": "counter", "计数": "counter", "脉冲计数": "counter", "脉冲": "counter",
    "revision": "revision", "rev": "revision", "修订号": "revision", "修订": "revision",
}
_DEFAULT_ORDER = ["station", "device", "minute", "counter", "revision"]

# YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]，也允许空格分隔日期时间
_MINUTE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*"
    r"(Z|z|[+-]\d{2}:?\d{2})?\s*$"
)


class CsvFormatError(ValueError):
    """文件级 CSV 格式错误（整个文件不可用，不入库任何行）。"""


@dataclass(frozen=True)
class Reading:
    """一条合法的累计脉冲读数。"""

    station: str
    device: str           # DEVICE_PRIMARY / DEVICE_BACKUP
    minute_epoch: int     # UTC 整分钟的 epoch 秒
    counter: int          # 0~9999
    revision: int         # >= 0


@dataclass(frozen=True)
class RowError:
    """单行拒绝记录。"""

    row_no: int           # 在原文件中的行号（从 1 开始）
    reason: str
    raw: str


def iso_utc(epoch: int) -> str:
    """epoch 秒 -> 'YYYY-MM-DDTHH:MM:SSZ'。"""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_minute(text: str) -> int:
    """解析 UTC 整分钟时间，返回 epoch 秒。

    接受 'YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]'（日期时间间也允许空格）。
    未带时区按 UTC 处理；带偏移则换算为 UTC。秒必须为零（允许省略）。
    """
    s = (text or "").strip()
    if "." in s:
        raise ValueError(f"时间不允许秒以下精度: {s!r}")
    m = _MINUTE_RE.match(s)
    if not m:
        raise ValueError(
            f"时间格式无法解析（应为 YYYY-MM-DDTHH:MM，可带秒与 UTC 偏移）: {s!r}"
        )
    date, hh, mm, ss, off = m.groups()
    if ss is not None and ss != "00":
        raise ValueError(f"时间必须为整分钟（秒需为 00）: {s!r}")
    iso = f"{date}T{hh}:{mm}:00"
    if off:
        off = off.upper()
        if off == "Z":
            iso += "+00:00"
        else:
            o = off.replace(":", "")
            iso += f"{o[:3]}:{o[3:]}"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(f"时间无效: {s!r}（{exc}）") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 未带时区按 UTC
    return int(dt.astimezone(timezone.utc).timestamp())


def parse_hour(text: str) -> int:
    """解析整点小时参数，返回该小时起始的 epoch 秒；必须整点对齐。"""
    epoch = parse_minute(text)
    if epoch % HOUR_SECONDS != 0:
        raise ValueError(f"小时必须整点对齐（分钟需为 00）: {text!r}")
    return epoch


def parse_counter(text: str) -> int:
    s = (text or "").strip()
    try:
        value = int(s, 10)
    except ValueError:
        raise ValueError(f"计数不是整数: {s!r}") from None
    if not 0 <= value <= 9999:
        raise ValueError(f"计数超出 0~9999 范围: {value}")
    return value


def parse_revision(text: str) -> int:
    s = (text or "").strip()
    try:
        value = int(s, 10)
    except ValueError:
        raise ValueError(f"修订号不是整数: {s!r}") from None
    if value < 0:
        raise ValueError(f"修订号不能为负: {value}")
    return value


def _parse_fields(fields, order) -> Reading:
    vals = {name: fields[i] for i, name in enumerate(order) if name is not None}
    station = vals["station"].strip()
    if not station:
        raise ValueError("站点为空")
    if len(station) > 64:
        raise ValueError("站点名超过 64 字符")
    device = _DEVICE_ALIASES.get(vals["device"].strip().lower())
    if device is None:
        raise ValueError(
            f"无法识别的主备标记: {vals['device'].strip()!r}（应为 primary/backup 或 主/备）"
        )
    return Reading(
        station=station,
        device=device,
        minute_epoch=parse_minute(vals["minute"]),
        counter=parse_counter(vals["counter"]),
        revision=parse_revision(vals["revision"]),
    )


def parse_csv_text(text: str):
    """解析 CSV 文本。

    返回 (valid_rows, row_errors)：
    - valid_rows: [(row_no, raw_line, Reading)]，按文件顺序；
    - row_errors: [RowError]，单行校验失败记录。
    文件级问题（空文件、表头缺列等）抛 CsvFormatError，调用方应整体拒收。
    """
    if text is None or not text.strip():
        raise CsvFormatError("没有收到 CSV 内容")
    try:
        raw_rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise CsvFormatError(f"CSV 解析失败: {exc}") from exc

    # 跳过完全空白的行，行号保留原始文件行号
    rows = [(i + 1, r) for i, r in enumerate(raw_rows) if any(c.strip() for c in r)]
    if not rows:
        raise CsvFormatError("CSV 为空或没有数据行")

    _, first = rows[0]
    mapped = [_HEADER_NAMES.get(c.strip().lower()) for c in first]
    if any(name is not None for name in mapped):
        # 第一行是表头：必须能找齐 5 个必需列，未识别的列忽略
        missing = [n for n in _DEFAULT_ORDER if n not in mapped]
        if missing:
            raise CsvFormatError(f"表头缺少必需列: {', '.join(missing)}")
        order = mapped
        body = rows[1:]
        if not body:
            raise CsvFormatError("CSV 只有表头，没有数据行")
    else:
        order = _DEFAULT_ORDER
        body = rows

    max_idx = max(i for i, name in enumerate(order) if name is not None)
    valid, errors = [], []
    for row_no, fields in body:
        raw = ",".join(fields)
        if len(fields) <= max_idx:
            errors.append(
                RowError(row_no, f"列数不足（需要 {max_idx + 1} 列，实际 {len(fields)} 列）", raw)
            )
            continue
        if any(c.strip() for c in fields[max_idx + 1:]):
            errors.append(RowError(row_no, "存在多余的数据列", raw))
            continue
        try:
            reading = _parse_fields(fields, order)
        except ValueError as exc:
            errors.append(RowError(row_no, str(exc), raw))
            continue
        valid.append((row_no, raw, reading))
    return valid, errors


def counter_delta(start: int, end: int) -> int:
    """相邻两次累计计数之差；计数下降按一次 10000 回绕处理。"""
    return (end - start) % COUNTER_MOD


def compute_hour(counters: dict, station: str, hour_start_epoch: int) -> dict:
    """计算某站某 UTC 整点小时的雨量。

    counters: {(device, minute_epoch): counter}，需覆盖
    [hour_start, hour_start+3600] 的端点（缺失即查不到）。
    返回结果同时供网页展示与 API 输出，保证两者一致。
    """
    if hour_start_epoch % HOUR_SECONDS != 0:
        raise ValueError("hour_start_epoch 必须整点对齐")

    intervals = []
    total_pulses = 0
    missing = 0
    source_counts = {DEVICE_PRIMARY: 0, DEVICE_BACKUP: 0, SOURCE_MISSING: 0}
    for i in range(INTERVALS_PER_HOUR):
        m0 = hour_start_epoch + i * MINUTE_SECONDS
        m1 = m0 + MINUTE_SECONDS
        source, delta = SOURCE_MISSING, None
        # 主计两端齐全 -> 主计差值；否则备计两端齐全 -> 备计差值；不跨设备相减
        p0 = counters.get((DEVICE_PRIMARY, m0))
        p1 = counters.get((DEVICE_PRIMARY, m1))
        if p0 is not None and p1 is not None:
            source, delta = DEVICE_PRIMARY, counter_delta(p0, p1)
        else:
            b0 = counters.get((DEVICE_BACKUP, m0))
            b1 = counters.get((DEVICE_BACKUP, m1))
            if b0 is not None and b1 is not None:
                source, delta = DEVICE_BACKUP, counter_delta(b0, b1)
        source_counts[source] += 1
        if delta is None:
            missing += 1
        else:
            total_pulses += delta
        intervals.append({
            "index": i,
            "start": iso_utc(m0),
            "end": iso_utc(m1),
            "source": source,
            "pulses": delta,
            "rainfall_mm": round(delta * PULSE_MM, 1) if delta is not None else None,
        })

    complete = missing == 0
    return {
        "station": station,
        "hour_start": iso_utc(hour_start_epoch),
        "status": "ok" if complete else "incomplete",
        # 供数字段：任一间隔缺失则为 null，预警模型不得取用
        "rainfall_mm": round(total_pulses * PULSE_MM, 1) if complete else None,
        "total_pulses": total_pulses if complete else None,
        "available_pulses": total_pulses,
        "missing_intervals": missing,
        "source_counts": source_counts,
        "intervals": intervals,
    }


# ---------------- 上下游时差（候选提前量）分析 ----------------

def pearson(xs: list[float], ys: list[float]):
    """皮尔逊相关系数；任一序列无波动（方差为 0）时返回 None。"""
    n = len(xs)
    if n != len(ys) or n == 0:
        raise ValueError("两序列长度必须一致且非空")
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None  # 任一序列无波动，相关系数无定义
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    # 浮点误差可能略微越界，夹回 [-1, 1]
    return max(-1.0, min(1.0, r))


def build_hour_rainfall(counters: dict, station: str,
                        window_start: int, window_end: int) -> dict:
    """把某站在 [window_start, window_end] 各整点小时现算为可供数小时雨量。

    仅保留 status=ok（60 个间隔全部有可用数据）的小时；
    返回 {hour_epoch: rainfall_mm}，不可供数的小时不进入序列。
    """
    out = {}
    for hour in range(window_start, window_end + 1, HOUR_SECONDS):
        result = compute_hour(counters, station, hour)
        if result["status"] == "ok":
            out[hour] = result["rainfall_mm"]
    return out


def analyze_lag(up_rain: dict, down_rain: dict,
                window_start: int, window_end: int, max_lag: int) -> dict:
    """按候选提前量 0~max_lag 比较上、下游可供数小时雨量的皮尔逊相关系数。

    配对方式：上游小时 h 与下游小时 h+lag（下游延后 lag 小时）。
    所有候选只用共同可配对的上游小时——即对 0~max_lag 每个时差都能配上、
    且配对双方都在窗口内的上游小时，保证各候选的配对集合完全相同、可横向比较。

    up_rain / down_rain: {hour_epoch: rainfall_mm}，只含可供数小时。
    返回的 dict 同时供网页展示与 API 输出，两边结果一致。
    """
    if window_start % HOUR_SECONDS != 0 or window_end % HOUR_SECONDS != 0:
        raise ValueError("窗口起止必须整点对齐")
    if not isinstance(max_lag, int) or not 1 <= max_lag <= MAX_LAG_LIMIT:
        raise ValueError(f"max_lag 必须为 1~{MAX_LAG_LIMIT} 的整数")
    if window_end < window_start:
        raise ValueError("结束整点不能早于起始整点")

    # 共同可配对的上游小时：h 与 h+0..max_lag 全部落在窗口内，
    # 且上游 h、下游 h+lag 对每个 lag 都是可供数小时
    last_up = window_end - max_lag * HOUR_SECONDS
    common_hours = sorted(
        h for h in range(window_start, last_up + 1, HOUR_SECONDS)
        if h in up_rain
        and all(h + lag * HOUR_SECONDS in down_rain for lag in range(max_lag + 1))
    )
    pair_count = len(common_hours)

    candidates = []
    for lag in range(max_lag + 1):
        xs = [up_rain[h] for h in common_hours]
        ys = [down_rain[h + lag * HOUR_SECONDS] for h in common_hours]
        if pair_count < MIN_PAIRS:
            r = None  # 配对不足，不计算系数
        else:
            r = pearson(xs, ys)
        candidates.append({
            "lag_hours": lag,
            "coefficient": round(r, 4) if r is not None else None,
            "defined": r is not None,
            "pair_count": pair_count,
            "pairs": [
                {
                    "upstream_hour": iso_utc(h),
                    "downstream_hour": iso_utc(h + lag * HOUR_SECONDS),
                    "upstream_mm": up_rain[h],
                    "downstream_mm": down_rain[h + lag * HOUR_SECONDS],
                }
                for h in common_hours
            ],
        })

    if pair_count < MIN_PAIRS:
        conclusion, reason = "无法判断", "insufficient_pairs"
    else:
        defined = [c for c in candidates if c["defined"]]
        if not defined:
            conclusion, reason = "无法判断", "no_variation"
        else:
            best = max(c["coefficient"] for c in defined)
            if best <= 0:
                conclusion, reason = "无法判断", "non_positive"
            else:
                # 最高系数并列：全部并列时差都列出，不擅选一个
                tied = [
                    c["lag_hours"] for c in defined
                    if c["coefficient"] >= best - TIE_EPS
                ]
                conclusion, reason = "可判断", "ok"
                best = round(best, 4)

    result = {
        "window_start": iso_utc(window_start),
        "window_end": iso_utc(window_end),
        "max_lag_hours": max_lag,
        "common_pair_count": pair_count,
        "common_upstream_hours": [iso_utc(h) for h in common_hours],
        "conclusion": conclusion,
        "reason": reason,
        "candidates": candidates,
    }
    if conclusion == "可判断":
        result["best_coefficient"] = best
        result["best_lags"] = tied
        result["is_tie"] = len(tied) > 1
    else:
        result["best_coefficient"] = None
        result["best_lags"] = []
        result["is_tie"] = False
    return result


REASON_TEXT = {
    "insufficient_pairs": f"共同可配对的上游小时不足 {MIN_PAIRS} 对",
    "no_variation": "配对序列无波动（至少一个序列为常数），皮尔逊相关系数无定义",
    "non_positive": "所有可计算的候选时差相关系数均不大于 0，不支持正相关提前量",
    "ok": "存在正相关最高系数的候选时差",
}


def lag_conclusion_text(result: dict) -> str:
    """供网页展示的结论文案。"""
    if result["conclusion"] == "可判断":
        lags = "、".join(f"{lag} 小时" for lag in result["best_lags"])
        tie = "（最高系数并列，全部列出）" if result["is_tie"] else ""
        return (
            f"建议候选提前量：{lags}，皮尔逊相关系数 "
            f"{result['best_coefficient']:.4f}{tie}"
        )
    return "无法判断：" + REASON_TEXT[result["reason"]]
