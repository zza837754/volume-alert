#!/usr/bin/env python3
"""
OKX 永续合约 - 山寨币形态扫描（做空 / 做多 三种形态），命中后合并推送到钉钉

【偏空】假突破回落：均线粘合横盘 → 放量拉针冲出上沿 → 迅速跌回区间内（诱多）
【偏多】放量突破：均线粘合横盘 → 放量冲出上沿 → 收盘站稳在上沿之上
【偏多】强势回调再启动：先暴拉一波 → 回调（不破位）→ 重新站上 MA7/MA25、MA7 上翘（可能再创新高）

【顺势】趋势回踩进场（做多/做空）：MA25>MA99 且双双向上，缩量回踩到 MA25 附近不破 MA99，
        出现收盘越过上一根极值的反转K线时推送，附带止损位和目标位，盈亏比不到 1.5 的不推。

独立文件，不依赖 volume_alert.py，直接运行即可。
"""

import os
import sys
import time

import datetime
import requests

DINGTALK_SECURITY_KEYWORD = "合约"   # 要和钉钉机器人"自定义关键词"一致
OKX_KLINES_URL = "https://www.okx.com/api/v5/market/candles"


def format_candle_time(ts_ms):
    ts = int(ts_ms) / 1000
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M")


def get_klines(symbol, interval, limit):
    resp = requests.get(OKX_KLINES_URL, params={"instId": symbol, "bar": interval, "limit": limit}, timeout=(5, 8))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX接口返回错误: {data}")
    return list(reversed(data["data"]))


def push_to_dingtalk(title, content):
    webhook = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        print("未设置 DINGTALK_WEBHOOK，仅打印：")
        print(title)
        print(content)
        return
    if DINGTALK_SECURITY_KEYWORD not in title and DINGTALK_SECURITY_KEYWORD not in content:
        title = f"【{DINGTALK_SECURITY_KEYWORD}】{title}"
    resp = requests.post(webhook, json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}}, timeout=10)
    print("钉钉返回:", resp.text)

# ========== 可调参数 ==========
BAR = "15m"                 # 扫描周期，截图看不出周期，默认15分钟，可改 "5m"/"1h"
TOP_N = 300                 # 只扫成交额前N的山寨币
MIN_TURNOVER_USDT = 2_000_000   # 24h成交额下限
EXCLUDE = {"BTC", "ETH"}    # 不扫的币

BASE_WIN = 16               # 冲高前的横盘窗口（根）
BASE_RANGE_MAX = 0.04       # 横盘窗口内 最高-最低 不超过 4%
MA_CONVERGE_MAX = 0.02      # 冲高前一根：MA7/MA25/MA99 最大最小相差不超过 2%（粘合）

SPIKE_MIN = 0.015           # 冲高K线的最高价，至少高出横盘上沿 1.5%
SPIKE_VOL_RATIO = 1.2       # 冲高K线成交量 >= 之前20根均量 * 该倍数（放量冲高）
SPIKE_MAX_AGO = 6           # 冲高K线必须在最近 6 根以内
NEAR_PEAK_TOL = 0.015       # 冲高高点与前高相差不超过 1.5%，标注"二次冲高到前高"（只标注，不强制）

# --- 放量突破（偏多）---
BREAKOUT_HOLD = 0.003       # 最新收盘要高出横盘上沿至少 0.3%，且冲高后每根收盘都在上沿之上

# --- 强势回调再启动（偏多）---
PUMP_UP_MIN = 0.15          # 前面那波拉升幅度至少 15%（峰值相对前面30根最低价）
PEAK_SEARCH = 90            # 往回找峰值的范围（根）
PEAK_MIN_AGO = 6            # 峰值至少在 6 根之前（说明确实回调了一段）
RETRACE_MIN = 0.20          # 回调幅度占整波拉升的比例：至少 20%
RETRACE_MAX = 0.75          # 最多 75%（回调太深就算走坏）
RESTART_CROSS_BARS = 3      # 最近 3 根内重新站上 MA25
RESTART_VOL_RATIO = 1.3     # 站上时最近 3 根里最大成交量 >= 之前20根均量 * 该倍数（放量确认）
RESTART_MA7_TOL = 0.003     # MA7 至少要接近或已上穿 MA25（MA7 不能比 MA25 低超过 0.3%）

# --- 趋势回踩进场（顺势，做多/做空）---
TOUCH_TOL = 0.005           # 回踩时影线触及 MA25 附近（相差 0.5% 以内）
PB_WIN = 8                  # 回踩窗口：触发K线之前的 8 根
PB_DEPTH_MIN = 0.01         # 回踩幅度（相对前高/前低）至少 1%
PB_VOL_RATIO = 1.0          # 回踩期间均量 <= 之前上涨/下跌段均量 * 该倍数（缩量回踩）
STOP_BUFFER = 0.002         # 止损放在回踩极值外侧再留 0.2%
RISK_MIN = 0.003            # 止损距离最小 0.3%
RISK_MAX = 0.05             # 止损距离最大 5%（太远的不推）
MIN_RR = 1.5                # 到前高/前低的盈亏比至少 1.5 才推

TREND_TRIGGER_VOL_RATIO = 1.2   # 趋势回踩：触发K线成交量 >= 回踩期均量 * 该倍数（反转放量）

# --- 市场/资金面过滤（推送前再筛一遍）---
USE_FUNDING_FILTER = True
FUNDING_LONG_MAX = 0.0005   # 做多：资金费率 > +0.05% 说明多头太拥挤，不推
FUNDING_SHORT_MIN = -0.0005 # 做空：资金费率 < -0.05% 说明空头太拥挤，不推
USE_BTC_FILTER = True
BTC_BLOCK_PCT = 0.015       # BTC 最近 1 小时跌超 1.5% 不推做多；涨超 1.5% 不推做空

# --- 信号冲突 / 资金费结算时段 ---
DROP_CONFLICT = True            # 同一个币本轮同时出现做多和做空信号，两边都不推
AVOID_FUNDING_WINDOW = True     # 资金费结算前后波动大，这段时间不扫描、不推送
FUNDING_BEFORE_MIN = 20         # 结算前 20 分钟开始避开
FUNDING_AFTER_MIN = 10          # 结算后 10 分钟结束避开
# OKX 资金费每 8 小时结算：北京时间 0 点、8 点、16 点

MAX_PUSH = 8                # 每种形态单次最多推多少个币
STATE_FILE = "seen_pattern_ts.txt"
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"


def ma(values, n, end):
    """end 为最后一根的下标（含），返回 n 周期均线；数据不够返回 None"""
    if end + 1 < n:
        return None
    seg = values[end - n + 1:end + 1]
    return sum(seg) / n


def _arrays(klines):
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    v = [float(k[6]) for k in klines]
    return o, h, l, c, v


def spike_candidates(klines):
    """
    找"均线粘合横盘后放量冲出上沿"的K线，从最近往前依次给出（偏空/偏多共用）。
    """
    n = len(klines)
    if n < 130:
        return
    o, h, l, c, v = _arrays(klines)
    i = n - 1
    for s in range(i - 1, max(i - SPIKE_MAX_AGO, BASE_WIN + 20) - 1, -1):
        b0 = s - BASE_WIN
        base_hi = max(h[b0:s])
        base_lo = min(l[b0:s])
        if base_lo <= 0 or (base_hi - base_lo) / c[s - 1] > BASE_RANGE_MAX:
            continue
        if h[s] < base_hi * (1 + SPIKE_MIN):
            continue
        m7, m25, m99 = ma(c, 7, s - 1), ma(c, 25, s - 1), ma(c, 99, s - 1)
        if None in (m7, m25, m99):
            continue
        if (max(m7, m25, m99) - min(m7, m25, m99)) / m25 > MA_CONVERGE_MAX:
            continue
        prev_vol = sum(v[s - 20:s]) / 20
        if prev_vol <= 0 or v[s] < prev_vol * SPIKE_VOL_RATIO:
            continue
        prior_peak = max(h[max(0, s - 100):s])
        yield {
            "s": s, "i": i, "o": o, "h": h, "l": l, "c": c,
            "base_hi": base_hi, "base_lo": base_lo,
            "spike_ts": klines[s][0], "ts": klines[i][0],
            "spike_high": h[s], "spike_pct": (h[s] - base_hi) / base_hi,
            "vol_ratio": v[s] / prev_vol,
            "ma_gap": (max(m7, m25, m99) - min(m7, m25, m99)) / m25,
            "prior_peak": prior_peak,
            "near_peak": abs(h[s] - prior_peak) / prior_peak <= NEAR_PEAK_TOL or h[s] >= prior_peak,
            "bars_after": i - s,
        }


def detect_fake_breakout(klines):
    """偏空：冲高后收盘跌回横盘区间内，并低于冲高那根的开盘价"""
    for r in spike_candidates(klines):
        c, o, i, s = r["c"], r["o"], r["i"], r["s"]
        if c[i] < r["base_hi"] and c[i] < o[s]:
            r["price"] = c[i]
            r["below_ma25"] = c[i] < ma(c, 25, i)
            r["key_ts"] = r["spike_ts"]
            return r
    return None


def detect_breakout(klines):
    """偏多：冲高后收盘站稳在横盘上沿之上，且在 MA25 上方"""
    for r in spike_candidates(klines):
        c, i, s = r["c"], r["i"], r["s"]
        line = r["base_hi"] * (1 + BREAKOUT_HOLD)
        if all(c[j] > line for j in range(s, i + 1)) and c[i] > ma(c, 25, i):
            r["price"] = c[i]
            r["key_ts"] = r["spike_ts"]
            return r
    return None


def detect_pullback_restart(klines):
    """偏多：暴拉 → 回调 → 重新站上 MA25 和 MA7，MA7 上翘，仍在 MA99 上方"""
    n = len(klines)
    if n < 130:
        return None
    o, h, l, c, v = _arrays(klines)
    i = n - 1

    lo_idx = max(0, i - PEAK_SEARCH)
    pk = max(range(lo_idx, i - PEAK_MIN_AGO + 1), key=lambda x: h[x])
    peak = h[pk]
    base = min(l[max(0, pk - 30):pk + 1])
    if base <= 0 or (peak - base) / base < PUMP_UP_MIN:
        return None

    pull_low = min(l[pk:i + 1])
    retrace = (peak - pull_low) / (peak - base)
    if not (RETRACE_MIN <= retrace <= RETRACE_MAX):
        return None

    m7, m7_prev, m25, m99 = ma(c, 7, i), ma(c, 7, i - 3), ma(c, 25, i), ma(c, 99, i)
    if None in (m7, m7_prev, m25, m99):
        return None
    # 收盘站上 MA7 和 MA25，MA7 开始上翘，且还在 MA99 上方（没有走坏）
    if not (c[i] > m25 and c[i] > m7 and m7 > m7_prev and c[i] > m99):
        return None
    if m7 < m25 * (1 - RESTART_MA7_TOL):     # MA7 还远在 MA25 下面，说明只是试探性站上
        return None

    # 最近几根内刚从下方重新站上 MA25，而且是回调低点之后发生的
    low_idx = l.index(pull_low, pk)
    cross = None
    for j in range(i, i - RESTART_CROSS_BARS, -1):
        mj, mj1 = ma(c, 25, j), ma(c, 25, j - 1)
        if j > low_idx and c[j - 1] <= mj1 and c[j] > mj:
            cross = j
            break
    if cross is None:
        return None

    prev_vol = sum(v[i - RESTART_CROSS_BARS - 20 + 1:i - RESTART_CROSS_BARS + 1]) / 20
    recent_vol = max(v[i - RESTART_CROSS_BARS + 1:i + 1])
    if prev_vol <= 0 or recent_vol < prev_vol * RESTART_VOL_RATIO:
        return None

    return {
        "key_ts": klines[cross][0], "ts": klines[i][0], "price": c[i],
        "peak": peak, "pump_pct": (peak - base) / base, "retrace": retrace,
        "ma7": m7, "ma25": m25, "ma99": m99,
        "new_high": max(h[i - RESTART_CROSS_BARS + 1:i + 1]) >= peak,
        "bars_after": i - cross,
    }


def detect_trend_pullback(klines, side):
    """
    顺势回踩进场。side = "long" / "short"。
    做空用价格取倒数后复用做多的逻辑（O/H/L/C -> 1/O,1/L,1/H,1/C），输出时再换回真实价格。
    """
    n = len(klines)
    if n < 130:
        return None
    o0, h0, l0, c0, v = _arrays(klines)
    if side == "long":
        o, h, l, c = o0, h0, l0, c0
    else:
        o = [1 / x for x in o0]
        h = [1 / x for x in l0]
        l = [1 / x for x in h0]
        c = [1 / x for x in c0]
    i = n - 1

    m7, m25, m99 = ma(c, 7, i), ma(c, 25, i), ma(c, 99, i)
    m25_12, m99_12 = ma(c, 25, i - 12), ma(c, 99, i - 12)
    if None in (m7, m25, m99, m25_12, m99_12):
        return None
    # 趋势：MA25 在 MA99 之上，两条线都向上
    if not (m25 > m99 and m25 > m25_12 and m99 >= m99_12):
        return None

    # 回踩窗口：触发K线之前的 PB_WIN 根
    w0 = i - PB_WIN
    touched = False
    for j in range(w0, i):
        if c[j] < ma(c, 99, j):        # 回踩期间收盘跌破 MA99 -> 趋势走坏
            return None
        if l[j] <= ma(c, 25, j) * (1 + TOUCH_TOL):
            touched = True
    if not touched:
        return None

    # 回踩幅度：先有前高，再回踩出低点
    sh_idx = max(range(i - 30, i), key=lambda x: h[x])
    sh = h[sh_idx]
    pb_idx = min(range(max(sh_idx, w0), i), key=lambda x: l[x])
    pb_low = l[pb_idx]
    if pb_idx <= sh_idx or (sh - pb_low) / sh < PB_DEPTH_MIN:
        return None

    # 缩量回踩
    pb_vol = sum(v[sh_idx + 1:i]) / max(1, i - sh_idx - 1)
    run_vol = sum(v[sh_idx - 20:sh_idx + 1]) / 21
    if run_vol <= 0 or pb_vol > run_vol * PB_VOL_RATIO:
        return None

    # 触发：最新收盘是阳线（做空为阴线），且收盘高过上一根最高价（做空为低于上一根最低价）
    if not (c[i] > o[i] and c[i] > h[i - 1]):
        return None
    if pb_vol > 0 and v[i] < pb_vol * TREND_TRIGGER_VOL_RATIO:   # 反转K线要放量
        return None

    stop = pb_low * (1 - STOP_BUFFER)
    risk = (c[i] - stop) / c[i]
    if not (RISK_MIN <= risk <= RISK_MAX):
        return None
    rr = (sh - c[i]) / (c[i] - stop)
    if rr < MIN_RR:
        return None

    inv = (lambda x: x) if side == "long" else (lambda x: 1 / x)
    price, stop_p, tgt1 = inv(c[i]), inv(stop), inv(sh)
    tgt2 = price + 2 * (price - stop_p)
    return {
        "key_ts": klines[i][0], "ts": klines[i][0], "side": side,
        "price": price, "stop": stop_p, "target1": tgt1, "target2": tgt2,
        "risk": abs(price - stop_p) / price, "rr": rr,
        "depth": (sh - pb_low) / sh,
        "ma25": inv(m25), "ma99": inv(m99),
    }


FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
# 每种形态的方向：long 做多 / short 做空
SIDE = {"fake": "short", "breakout": "long", "restart": "long", "trend_long": "long", "trend_short": "short"}


def get_funding(inst_id):
    """当前资金费率（小数，0.0001 = 0.01%），取不到返回 None（取不到就不拦截）"""
    try:
        resp = requests.get(FUNDING_URL, params={"instId": inst_id}, timeout=(5, 8))
        data = resp.json()
        if data.get("code") == "0" and data["data"]:
            return float(data["data"][0]["fundingRate"])
    except Exception as e:
        print(f"{inst_id} 资金费率获取失败: {e}", file=sys.stderr)
    return None


def btc_move_1h():
    """BTC 最近约 1 小时涨跌幅（按已收盘K线），取不到返回 None"""
    try:
        ks = [k for k in get_klines("BTC-USDT-SWAP", BAR, 30) if k[8] == "1"]
        c = [float(k[4]) for k in ks]
        bars = max(1, 60 // {"5m": 5, "15m": 15, "30m": 30}.get(BAR, 15))
        return c[-1] / c[-1 - bars] - 1
    except Exception as e:
        print(f"BTC 行情获取失败: {e}", file=sys.stderr)
        return None


def in_funding_window(now_utc=None):
    """当前是否处在资金费结算（北京时间 0/8/16 点）前后的避开时段"""
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    bj = now + datetime.timedelta(hours=8)
    m = bj.hour * 60 + bj.minute
    for t in (0, 480, 960, 1440):
        if t - FUNDING_BEFORE_MIN <= m <= t + FUNDING_AFTER_MIN:
            return True
    return False


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    d = {}
    with open(STATE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if ":" in line:
                k, t = line.rsplit(":", 1)
                d[k] = t
    return d


def save_state(d):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{k}:{t}" for k, t in d.items()))


def list_candidates():
    resp = requests.get(TICKERS_URL, params={"instType": "SWAP"}, timeout=(5, 15))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX tickers 错误: {data}")
    rows = []
    for t in data["data"]:
        inst = t["instId"]
        if not inst.endswith("-USDT-SWAP"):
            continue
        coin = inst.split("-")[0]
        if coin in EXCLUDE:
            continue
        turnover = float(t["volCcy24h"]) * float(t["last"])   # 币本位数量 × 最新价 ≈ USDT成交额
        if turnover >= MIN_TURNOVER_USDT:
            rows.append((inst, turnover))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [r[0] for r in rows[:TOP_N]]


def fmt_fake(sym, r):
    tags = []
    if r["near_peak"]:
        tags.append("二次冲高到前高")
    if r["below_ma25"]:
        tags.append("已跌破MA25")
    tag = "｜".join(tags) if tags else "冲高失败"
    return (
        f"📉 {sym.replace('-SWAP', '')} ({BAR}) 假突破回落 [{tag}]\n"
        f"冲高K线: {format_candle_time(r['spike_ts'])}（北京时间），已过 {r['bars_after']} 根\n"
        f"冲高最高 {r['spike_high']}（高出横盘上沿 {r['spike_pct']*100:.1f}%），前高 {r['prior_peak']}\n"
        f"横盘区间 {r['base_lo']} ~ {r['base_hi']}，现价 {r['price']}（已跌回区间内）\n"
        f"冲高前均线粘合度 {r['ma_gap']*100:.2f}%，冲高量能 {r['vol_ratio']:.1f} 倍"
    )


def fmt_breakout(sym, r):
    tag = "突破前高" if r["near_peak"] else "突破横盘"
    return (
        f"📈 {sym.replace('-SWAP', '')} ({BAR}) 放量突破 [{tag}]\n"
        f"突破K线: {format_candle_time(r['spike_ts'])}（北京时间），已过 {r['bars_after']} 根\n"
        f"冲高最高 {r['spike_high']}（高出横盘上沿 {r['spike_pct']*100:.1f}%），现价 {r['price']}（站稳上沿 {r['base_hi']} 之上）\n"
        f"横盘区间 {r['base_lo']} ~ {r['base_hi']}\n"
        f"突破前均线粘合度 {r['ma_gap']*100:.2f}%，突破量能 {r['vol_ratio']:.1f} 倍"
    )


def fmt_restart(sym, r):
    tag = "已创新高" if r["new_high"] else "回调后再启动"
    return (
        f"📈 {sym.replace('-SWAP', '')} ({BAR}) 强势回调再启动 [{tag}]\n"
        f"站上MA25: {format_candle_time(r['key_ts'])}（北京时间），已过 {r['bars_after']} 根\n"
        f"前一波拉升 +{r['pump_pct']*100:.0f}%（高点 {r['peak']}），回调了 {r['retrace']*100:.0f}%，现价 {r['price']}\n"
        f"MA7 {r['ma7']:.6g} / MA25 {r['ma25']:.6g} / MA99 {r['ma99']:.6g}"
    )


def fmt_trend(sym, r):
    long_ = r["side"] == "long"
    return (
        f"{'📈 做多' if long_ else '📉 做空'} {sym.replace('-SWAP', '')} ({BAR}) 顺势回踩进场\n"
        f"信号K线收盘: {format_candle_time(r['ts'])}（北京时间），参考入场价 {r['price']:.6g}\n"
        f"止损 {r['stop']:.6g}（距离 {r['risk']*100:.1f}%），"
        f"目标1 {r['target1']:.6g}（前{'高' if long_ else '低'}，盈亏比 {r['rr']:.1f}），目标2 {r['target2']:.6g}（2R）\n"
        f"回踩幅度 {r['depth']*100:.1f}%，MA25 {r['ma25']:.6g} / MA99 {r['ma99']:.6g}"
    )


# 形态名 -> (检测函数, 推送格式, 钉钉标题)
PATTERNS = {
    "fake": (detect_fake_breakout, fmt_fake, "合约形态提醒【偏空】均线粘合后冲高回落"),
    "breakout": (detect_breakout, fmt_breakout, "合约形态提醒【偏多】均线粘合后放量突破"),
    "restart": (detect_pullback_restart, fmt_restart, "合约形态提醒【偏多】强势回调再启动"),
    "trend_long": (lambda k: detect_trend_pullback(k, "long"), fmt_trend, "合约形态提醒【偏多】趋势回踩进场"),
    "trend_short": (lambda k: detect_trend_pullback(k, "short"), fmt_trend, "合约形态提醒【偏空】趋势回踩进场"),
}


def main():
    if AVOID_FUNDING_WINDOW and in_funding_window():
        print("当前处在资金费结算前后的避开时段，本轮不扫描。")
        return

    state = load_state()

    symbols = list_candidates()
    print(f"本次扫描 {len(symbols)} 个币 ({BAR})")

    btc = btc_move_1h() if USE_BTC_FILTER else None
    if btc is not None:
        print(f"BTC 近1小时涨跌 {btc*100:+.2f}%")

    hits = {name: [] for name in PATTERNS}
    t_start = time.time()
    for idx, sym in enumerate(symbols, 1):
        if idx == 1 or idx % 10 == 0:
            print(f"进度 {idx}/{len(symbols)}  已用 {time.time() - t_start:.0f} 秒", flush=True)
        try:
            ks = get_klines(sym, BAR, 150)
            closed = [k for k in ks if k[8] == "1"]
        except Exception as e:
            print(f"{sym} 取K线失败: {e}", file=sys.stderr)
            time.sleep(0.12)
            continue
        time.sleep(0.12)   # OKX K线接口限频，慢一点更稳

        for name, (fn, _, _) in PATTERNS.items():
            try:
                r = fn(closed)
            except Exception as e:
                print(f"{sym} {name} 检测失败: {e}", file=sys.stderr)
                continue
            if not r:
                continue
            key = f"{sym}|{name}"
            last = state.get(key)
            if last and int(r["key_ts"]) <= int(last):   # 同一次信号只推一次
                continue
            state[key] = r["key_ts"]
            hits[name].append((sym, r))

    save_state(state)

    total = sum(len(v) for v in hits.values())
    print("命中数量: " + "，".join(f"{n}={len(v)}" for n, v in hits.items()))
    if not total:
        print("本次没有命中形态。")
        return

    if DROP_CONFLICT:
        long_syms = {sym for n, v in hits.items() if SIDE[n] == "long" for sym, _ in v}
        short_syms = {sym for n, v in hits.items() if SIDE[n] == "short" for sym, _ in v}
        conflict = long_syms & short_syms
        if conflict:
            print("多空信号冲突，不推送: " + ", ".join(sorted(conflict)))
            for n in hits:
                hits[n] = [(sym, r) for sym, r in hits[n] if sym not in conflict]

    for name, (_, fmt, title) in PATTERNS.items():
        items = hits[name]
        if not items:
            continue
        side = SIDE[name]

        # 大盘过滤：BTC 正在单边逆着方向走，就先不推
        if btc is not None and ((side == "long" and btc <= -BTC_BLOCK_PCT) or (side == "short" and btc >= BTC_BLOCK_PCT)):
            print(f"{name}: BTC 近1小时 {btc*100:+.2f}%，逆势，{len(items)} 个信号不推送")
            continue

        items.sort(key=lambda x: x[1]["ts"], reverse=True)
        parts = []
        for sym, r in items:
            fr = None
            if USE_FUNDING_FILTER:
                fr = get_funding(sym)
                time.sleep(0.1)
                if fr is not None and ((side == "long" and fr > FUNDING_LONG_MAX) or (side == "short" and fr < FUNDING_SHORT_MIN)):
                    print(f"{sym} {name}: 资金费率 {fr*100:+.3f}% 过于拥挤，跳过")
                    continue
            text = fmt(sym, r)
            if fr is not None:
                text += f"\n资金费率 {fr*100:+.3f}%"
            parts.append(text)
            if len(parts) >= MAX_PUSH:
                break
        if parts:
            push_to_dingtalk(title, "\n\n———\n\n".join(parts))


if __name__ == "__main__":
    main()
