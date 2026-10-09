#!/usr/bin/env python3
"""
OKX 永续合约 - 山寨币"均线粘合横盘 → 突然拉针冲高 → 迅速回落"（假突破/诱多）扫描

对应最新截图红圈的走势：
  1) 价格横盘，MA7 / MA25 / MA99 粘在一起（缠绕收敛）
  2) 突然一根大阳线/长上影线冲出横盘上沿，常常冲到前高附近
  3) 随后几根K线迅速回落，收盘跌回横盘区间里面（突破失败）
  之后常见走势：诱多结束，顺势下跌（偏空信号）

每次运行：扫描成交额靠前的山寨币，命中就合并成一条钉钉消息推送。
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
    resp = requests.get(OKX_KLINES_URL, params={"instId": symbol, "bar": interval, "limit": limit}, timeout=10)
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
TOP_N = 120                 # 只扫成交额前N的山寨币
MIN_TURNOVER_USDT = 5_000_000   # 24h成交额下限
EXCLUDE = {"BTC", "ETH"}    # 不扫的币

BASE_WIN = 16               # 冲高前的横盘窗口（根）
BASE_RANGE_MAX = 0.04       # 横盘窗口内 最高-最低 不超过 4%
MA_CONVERGE_MAX = 0.02      # 冲高前一根：MA7/MA25/MA99 最大最小相差不超过 2%（粘合）

SPIKE_MIN = 0.015           # 冲高K线的最高价，至少高出横盘上沿 1.5%
SPIKE_VOL_RATIO = 1.2       # 冲高K线成交量 >= 之前20根均量 * 该倍数（放量冲高）
SPIKE_MAX_AGO = 6           # 冲高K线必须在最近 6 根以内
NEAR_PEAK_TOL = 0.015       # 冲高高点与前高相差不超过 1.5%，标注"二次冲高到前高"（只标注，不强制）

MAX_PUSH = 8                # 单次最多推多少个币
STATE_FILE = "seen_pattern_ts.txt"
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"


def ma(values, n, end):
    """end 为最后一根的下标（含），返回 n 周期均线；数据不够返回 None"""
    if end + 1 < n:
        return None
    seg = values[end - n + 1:end + 1]
    return sum(seg) / n


def detect(klines):
    """
    klines: 已收盘K线，从旧到新，每根 [ts,o,h,l,c,vol,volCcy,volQuote,confirm]
    命中返回信息字典（含冲高K线时间 spike_ts，用来去重），否则返回 None
    """
    n = len(klines)
    if n < 130:
        return None
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    v = [float(k[6]) for k in klines]
    i = n - 1

    # 从最近往前找冲高K线 s（s < i，也就是冲高之后至少已经有一根收盘K线确认）
    for s in range(i - 1, max(i - SPIKE_MAX_AGO, BASE_WIN + 20) - 1, -1):
        b0 = s - BASE_WIN
        base_hi = max(h[b0:s])
        base_lo = min(l[b0:s])
        if base_lo <= 0 or (base_hi - base_lo) / c[s - 1] > BASE_RANGE_MAX:
            continue

        # 冲高：最高价明显冲出横盘上沿
        if h[s] < base_hi * (1 + SPIKE_MIN):
            continue

        # 冲高前均线粘合
        m7 = ma(c, 7, s - 1)
        m25 = ma(c, 25, s - 1)
        m99 = ma(c, 99, s - 1)
        if None in (m7, m25, m99):
            continue
        if (max(m7, m25, m99) - min(m7, m25, m99)) / m25 > MA_CONVERGE_MAX:
            continue

        # 放量冲高
        prev_vol = sum(v[s - 20:s]) / 20
        if prev_vol <= 0 or v[s] < prev_vol * SPIKE_VOL_RATIO:
            continue

        # 突破失败：冲高之后，最新收盘已经跌回横盘区间里面，且低于冲高那根的开盘价
        if not (c[i] < base_hi and c[i] < o[s]):
            continue

        prior_peak = max(h[max(0, s - 100):s])
        near_peak = abs(h[s] - prior_peak) / prior_peak <= NEAR_PEAK_TOL or h[s] >= prior_peak
        return {
            "spike_ts": klines[s][0],
            "ts": klines[i][0],
            "price": c[i],
            "spike_high": h[s],
            "base_hi": base_hi,
            "base_lo": base_lo,
            "spike_pct": (h[s] - base_hi) / base_hi,
            "vol_ratio": v[s] / prev_vol,
            "ma_gap": (max(m7, m25, m99) - min(m7, m25, m99)) / m25,
            "near_peak": near_peak,
            "prior_peak": prior_peak,
            "below_ma25": c[i] < ma(c, 25, i),
            "bars_after": i - s,
        }
    return None


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
    resp = requests.get(TICKERS_URL, params={"instType": "SWAP"}, timeout=15)
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


def main():
    state = load_state()

    symbols = list_candidates()
    print(f"本次扫描 {len(symbols)} 个币 ({BAR})")

    hits = []
    for sym in symbols:
        try:
            ks = get_klines(sym, BAR, 150)
            closed = [k for k in ks if k[8] == "1"]
            r = detect(closed)
        except Exception as e:
            print(f"{sym} 检查失败: {e}", file=sys.stderr)
            continue
        finally:
            time.sleep(0.12)   # OKX K线接口限频，慢一点更稳

        if not r:
            continue
        last = state.get(sym)
        if last and int(r["spike_ts"]) <= int(last):   # 同一根冲高K线只推一次
            continue
        state[sym] = r["spike_ts"]
        hits.append((sym, r))

    save_state(state)

    if not hits:
        print("本次没有命中形态。")
        return

    hits.sort(key=lambda x: x[1]["spike_pct"], reverse=True)
    parts = []
    for sym, r in hits[:MAX_PUSH]:
        tags = []
        if r["near_peak"]:
            tags.append("二次冲高到前高")
        if r["below_ma25"]:
            tags.append("已跌破MA25")
        tag = "｜".join(tags) if tags else "冲高失败"
        parts.append(
            f"📉 {sym.replace('-SWAP', '')} ({BAR}) 假突破回落 [{tag}]\n"
            f"冲高K线: {format_candle_time(r['spike_ts'])}（北京时间），已过 {r['bars_after']} 根\n"
            f"冲高最高 {r['spike_high']}（高出横盘上沿 {r['spike_pct']*100:.1f}%），前高 {r['prior_peak']}\n"
            f"横盘区间 {r['base_lo']} ~ {r['base_hi']}，现价 {r['price']}（已跌回区间内）\n"
            f"冲高前均线粘合度 {r['ma_gap']*100:.2f}%，冲高量能 {r['vol_ratio']:.1f} 倍"
        )
    push_to_dingtalk("合约形态提醒：均线粘合后冲高回落", "\n\n———\n\n".join(parts))


if __name__ == "__main__":
    main()
