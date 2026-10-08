#!/usr/bin/env python3
"""
OKX 永续合约 - 山寨币"冲高回落后 均线缠绕小阳小阴震荡"形态扫描

对应截图里红圈的走势：
  1) 前面有一波拉升，冲到高点后回落（下跌趋势中）
  2) 回落后出现一段小实体K线的震荡反弹，价格在 MA7 / MA25 附近来回缠绕
  3) 反弹没能有效站上 MA25（下跌中继），量能没有放大
  之后常见走势：反弹乏力继续下跌（偏空信号）

每次运行：扫描成交额靠前的山寨币，命中形态就合并成一条钉钉消息推送。
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
TOP_N = 120                 # 只扫成交额前N的山寨币（流动性太差的币形态不可靠）
MIN_TURNOVER_USDT = 5_000_000   # 24h成交额下限
EXCLUDE = {"BTC", "ETH"}    # 不扫的币（大饼以太另有监控）

PEAK_LOOKBACK = 80          # 往回找前高的范围(根)
PEAK_MIN_AGO = 8            # 前高至少在多少根之前（保证已经回落了一段）
DROP_FROM_PEAK_MIN = 0.05   # 当前价比前高至少低 5%
PUMP_MIN = 0.08             # 前高比前高之前30根的最低价至少高 8%（确实拉升过）

WIN = 8                     # 震荡窗口：最近8根K线
WIN_RANGE_MAX = 0.04        # 窗口内 最高-最低 不超过 4%
MA_GAP_MAX = 0.015          # 最新一根 MA7 与 MA25 相距不超过 1.5%（缠绕）
MA7_CROSS_MIN = 2           # 窗口内收盘价上下穿越 MA7 至少2次（来回缠绕）
BELOW_MA25_TOL = 0.015      # 窗口内最高价不超过 MA25 的 1.5%（没能站上）
VOL_SHRINK_RATIO = 1.0      # 窗口均量 <= 之前20根均量 * 该比例（量不放大）

COOLDOWN_BARS = 12          # 同一个币触发后，多少根K线内不重复推送
MAX_PUSH = 8                # 单次最多推多少个币
STATE_FILE = "seen_pattern_ts.txt"
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
BAR_MS = {"5m": 5, "15m": 15, "30m": 30, "1H": 60, "1h": 60}


def ma(values, n, end):
    """end 为最后一根的下标（含），返回 n 周期均线；数据不够返回 None"""
    if end + 1 < n:
        return None
    seg = values[end - n + 1:end + 1]
    return sum(seg) / n


def detect(klines):
    """
    klines: 已收盘K线，从旧到新，每根 [ts,o,h,l,c,vol,volCcy,volQuote,confirm]
    命中返回信息字典，否则返回 None
    """
    n = len(klines)
    if n < 110:
        return None
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    v = [float(k[6]) for k in klines]
    i = n - 1

    # 1) 冲高回落
    lo_idx = max(0, i - PEAK_LOOKBACK)
    peak_idx = max(range(lo_idx, i + 1), key=lambda x: h[x])
    peak = h[peak_idx]
    if i - peak_idx < PEAK_MIN_AGO:
        return None
    drop = (peak - c[i]) / peak
    if drop < DROP_FROM_PEAK_MIN:
        return None
    base_lo = min(l[max(0, peak_idx - 30):peak_idx + 1])
    if base_lo <= 0 or (peak - base_lo) / base_lo < PUMP_MIN:
        return None

    # 2) 下跌趋势：MA25 向下，价格不在 MA25 上方
    ma7 = ma(c, 7, i)
    ma25 = ma(c, 25, i)
    ma25_prev = ma(c, 25, i - 8)
    ma99 = ma(c, 99, i)
    if None in (ma7, ma25, ma25_prev) or ma25 >= ma25_prev:
        return None
    if c[i] > ma25 * (1 + BELOW_MA25_TOL):
        return None

    # 3) 震荡缠绕
    w0 = i - WIN + 1
    win_hi = max(h[w0:i + 1])
    win_lo = min(l[w0:i + 1])
    if (win_hi - win_lo) / c[i] > WIN_RANGE_MAX:
        return None
    if abs(ma7 - ma25) / ma25 > MA_GAP_MAX:
        return None
    crosses = 0
    for j in range(w0 + 1, i + 1):
        m_prev, m_cur = ma(c, 7, j - 1), ma(c, 7, j)
        if (c[j - 1] - m_prev) * (c[j] - m_cur) < 0:
            crosses += 1
    if crosses < MA7_CROSS_MIN:
        return None
    # 反弹没能有效站上 MA25
    for j in range(w0, i + 1):
        m25 = ma(c, 25, j)
        if h[j] > m25 * (1 + BELOW_MA25_TOL):
            return None

    # 4) 量能不放大
    win_vol = sum(v[w0:i + 1]) / WIN
    prev_vol = sum(v[w0 - 20:w0]) / 20
    if prev_vol <= 0 or win_vol > prev_vol * VOL_SHRINK_RATIO:
        return None

    breakdown = c[i] < min(l[w0:i])   # 最新收盘跌破窗口前面所有低点
    return {
        "ts": klines[i][0],
        "price": c[i],
        "drop": drop,
        "peak": peak,
        "ma7": ma7,
        "ma25": ma25,
        "ma99": ma99,
        "range": (win_hi - win_lo) / c[i],
        "crosses": crosses,
        "vol_ratio": win_vol / prev_vol,
        "breakdown": breakdown,
    }


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
    bar_min = BAR_MS.get(BAR, 15)
    cooldown_ms = COOLDOWN_BARS * bar_min * 60 * 1000

    symbols = list_candidates()
    print(f"本次扫描 {len(symbols)} 个币 ({BAR})")

    hits = []
    for sym in symbols:
        try:
            ks = get_klines(sym, BAR, 130)
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
        if last and int(r["ts"]) - int(last) < cooldown_ms:
            continue
        state[sym] = r["ts"]
        hits.append((sym, r))

    save_state(state)

    if not hits:
        print("本次没有命中形态。")
        return

    hits.sort(key=lambda x: x[1]["drop"], reverse=True)
    parts = []
    for sym, r in hits[:MAX_PUSH]:
        tag = "🔻已跌破震荡低点" if r["breakdown"] else "⏳震荡中，等待方向"
        parts.append(
            f"📉 {sym.replace('-SWAP', '')} ({BAR}) {tag}\n"
            f"K线时间: {format_candle_time(r['ts'])}（北京时间）\n"
            f"现价: {r['price']}（距前高 -{r['drop']*100:.1f}%，前高 {r['peak']}）\n"
            f"MA7 {r['ma7']:.6g} / MA25 {r['ma25']:.6g}"
            + (f" / MA99 {r['ma99']:.6g}" if r["ma99"] else "")
            + f"\n震荡幅度 {r['range']*100:.1f}%，穿越MA7 {r['crosses']}次，量能比 {r['vol_ratio']:.2f}"
        )
    push_to_dingtalk("合约形态提醒：冲高回落后均线缠绕", "\n\n———\n\n".join(parts))


if __name__ == "__main__":
    main()
