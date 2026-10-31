#!/usr/bin/env python3
"""
BTC / ETH 顺势单提醒（OKX 永续合约）

思路：顺着大周期趋势，等价格回踩到关键均线附近，再出现反转 K 线才进场。
  1) 大周期定方向：
       做多：收盘 > MA25 > MA99，且 MA25 在上行
       做空：收盘 < MA25 < MA99，且 MA25 在下行
       其他情况（震荡）不推送
  2) 入场周期找位置：
       📍 到位提醒：趋势明确，价格刚回踩到 MA25 附近（还没出确认K线），提醒你盯盘
       ✅ 确认信号：回踩触及 MA25 附近、没有跌破 MA99，随后出现收盘越过上一根极值的反转K线，
                    并给出参考入场价、止损、两个目标位（前高/前低、等幅延伸），到等幅目标的盈亏比不够 1.5 的不推
  3) 默认监控四组：BTC 15 分钟（看 1H 趋势）、ETH 15 分钟（看 1H 趋势）、
                    BTC 1 小时（看 4H 趋势）、ETH 1 小时（看 4H 趋势）

说明：这是顺势回踩的经典做法，但并不保证胜率；请把止损位当成必须执行的保护，不要无止损持仓。
独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK_TREND（或 DINGTALK_WEBHOOK）不设置时只打印不推送。
"""

import datetime
import json
import math
import os
import sys
import time

import requests

# ========== 可调参数 ==========
# (合约, 入场周期, 大周期)
TARGETS = [
    ("BTC-USDT-SWAP", "15m", "1H"),
    ("ETH-USDT-SWAP", "15m", "1H"),
    ("BTC-USDT-SWAP", "1H", "4H"),
    ("ETH-USDT-SWAP", "1H", "4H"),
]

# 按入场周期设置：BTC/ETH 波动比山寨小，阈值要小得多
PB_WIN = 8                                          # 回踩窗口：最近 8 根
SWING_LOOKBACK = 30                                 # 往回找前高/前低的范围（根）
PB_DEPTH_MIN = {"15m": 0.003, "1H": 0.006}          # 回踩幅度（相对前高）至少 0.3% / 0.6%
TOUCH_TOL = {"15m": 0.0015, "1H": 0.003}            # 回踩影线触及 MA25 附近（0.15% / 0.3% 以内）
RISK_MIN = {"15m": 0.0015, "1H": 0.003}             # 止损距离下限
RISK_MAX = {"15m": 0.015, "1H": 0.03}               # 止损距离上限：太远的不推
STOP_BUFFER = 0.001                                 # 止损放在回踩极值外侧再留 0.1%
MIN_RR = 1.5                                        # 到"等幅延伸目标"的盈亏比至少 1.5，不够的不推

CONFIRM_COOLDOWN_MIN = {"15m": 60, "1H": 360}       # 同一组、同一方向的确认信号冷却
ZONE_COOLDOWN_MIN = {"15m": 45, "1H": 240}          # 到位提醒冷却
ENABLE_ZONE_ALERT = True                            # 是否推送"到位提醒"
STATE_FILE = "trend_state.json"
DINGTALK_SECURITY_KEYWORD = "合约"                   # 要和钉钉机器人"自定义关键词"一致
# ==============================

CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"


# ---------- 基础工具 ----------
def fmt_px(x):
    x = float(x)
    if x == 0:
        return "0"
    digits = max(0, 5 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{x:.0f}"


def fmt_time(ms):
    dt = datetime.datetime.fromtimestamp(int(ms) / 1000, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


def get_klines(inst, bar, limit):
    """已收盘的K线，从旧到新"""
    resp = requests.get(CANDLES_URL, params={"instId": inst, "bar": bar, "limit": limit}, timeout=(5, 8))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX 返回错误: {data}")
    return [k for k in reversed(data["data"]) if k[8] == "1"]


def get_funding(inst):
    try:
        d = requests.get(FUNDING_URL, params={"instId": inst}, timeout=(5, 8)).json()
        if d.get("code") == "0" and d["data"]:
            return float(d["data"][0]["fundingRate"])
    except Exception as e:
        print(f"{inst} 资金费率获取失败: {e}", file=sys.stderr)
    return None


def push_to_dingtalk(title, content):
    webhook = os.environ.get("DINGTALK_WEBHOOK_TREND") or os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        print("未设置 DINGTALK_WEBHOOK_TREND / DINGTALK_WEBHOOK，仅打印：")
        print(title)
        print(content)
        return
    if DINGTALK_SECURITY_KEYWORD not in title and DINGTALK_SECURITY_KEYWORD not in content:
        title = f"【{DINGTALK_SECURITY_KEYWORD}】{title}"
    resp = requests.post(webhook, json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}}, timeout=10)
    print("钉钉返回:", resp.text)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alert": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def ma(values, n, end):
    if end < 0 or end + 1 < n:
        return None
    return sum(values[end - n + 1:end + 1]) / n


def invert(klines):
    """价格取倒数（O,H,L,C -> 1/O,1/L,1/H,1/C），让做多的逻辑直接用于做空"""
    return [[k[0], 1 / float(k[1]), 1 / float(k[3]), 1 / float(k[2]), 1 / float(k[4]), k[5], k[6], k[7], k[8]]
            for k in klines]


# ---------- 趋势与入场 ----------
def trend_of(klines):
    """大周期趋势：+1 向上 / -1 向下 / 0 震荡；数据不够返回 None"""
    c = [float(k[4]) for k in klines]
    i = len(c) - 1
    m25, m99, m25_4 = ma(c, 25, i), ma(c, 99, i), ma(c, 25, i - 4)
    if None in (m25, m99, m25_4):
        return None
    if c[i] > m25 > m99 and m25 > m25_4:
        return 1
    if c[i] < m25 < m99 and m25 < m25_4:
        return -1
    return 0


def analyze(klines, bar):
    """
    按"做多"的逻辑分析入场周期的K线（做空时传入取倒数后的K线）。
    返回 None 或 dict：kind = 'confirm'（确认信号）/ 'zone'（到位提醒）
    """
    n = len(klines)
    if n < 130:
        return None
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    i = n - 1

    sh_idx = max(range(i - SWING_LOOKBACK, i + 1), key=lambda x: h[x])     # 前高
    if sh_idx >= i:
        return None
    sh = h[sh_idx]
    w0 = max(sh_idx + 1, i - PB_WIN)
    pb_idx = min(range(w0, i + 1), key=lambda x: l[x])
    pb_low = l[pb_idx]
    if (sh - pb_low) / sh < PB_DEPTH_MIN[bar]:
        return None

    m25_i, m99_i, m7_i = ma(c, 25, i), ma(c, 99, i), ma(c, 7, i)
    touched = False
    for j in range(w0, i + 1):
        m25_j, m99_j = ma(c, 25, j), ma(c, 99, j)
        if c[j] < m99_j:                       # 收盘跌破 MA99，趋势走坏
            return None
        if l[j] <= m25_j * (1 + TOUCH_TOL[bar]):
            touched = True
    if not touched:
        return None

    base = {"sh": sh, "pb_low": pb_low, "ma25": m25_i, "ma99": m99_i, "ts": klines[i][0], "price": c[i]}

    # ✅ 确认：阳线，收盘高过上一根最高价，并且站上 MA7
    if c[i] > o[i] and c[i] > h[i - 1] and c[i] > m7_i:
        stop = pb_low * (1 - STOP_BUFFER)
        risk = (c[i] - stop) / c[i]
        t1, t2 = sh, sh + (sh - pb_low)              # 目标1：前高；目标2：等幅延伸（前高 + 这一波回踩的幅度）
        if RISK_MIN[bar] <= risk <= RISK_MAX[bar] and c[i] < t1:
            rr1, rr2 = (t1 - c[i]) / (c[i] - stop), (t2 - c[i]) / (c[i] - stop)
            if rr2 >= MIN_RR:
                return {**base, "kind": "confirm", "stop": stop, "risk": risk, "rr1": rr1, "rr2": rr2,
                        "target1": t1, "target2": t2}
        return None

    # 📍 到位：最新一根刚触及 MA25 附近，且还没有出现反转确认
    if ENABLE_ZONE_ALERT and l[i] <= m25_i * (1 + TOUCH_TOL[bar]):
        return {**base, "kind": "zone"}
    return None


def convert_short(r, klines):
    """把取倒数空间里的结果换回真实价格（做空）"""
    c = [float(k[4]) for k in klines]
    i = len(c) - 1
    out = dict(r)
    for k in ("sh", "pb_low", "price", "ma25", "ma99"):
        out[k] = 1 / r[k]
    out["ma25"], out["ma99"] = ma(c, 25, i), ma(c, 99, i)
    if r["kind"] == "confirm":
        out["stop"] = 1 / r["stop"]
        out["target1"] = out["sh"]
        out["target2"] = 1 / r["target2"]
        out["risk"] = (out["stop"] - out["price"]) / out["price"]
    return out


# ---------- 消息 ----------
TREND_TXT = {1: "向上", -1: "向下", 0: "震荡", None: "未知"}


def fmt_alert(inst, bar, htf, side, r, trend_htf, trend_4h, funding):
    long_ = side == "long"
    name = inst.replace("-SWAP", "")
    kind_txt = "✅确认信号" if r["kind"] == "confirm" else "📍回踩到位（等确认）"
    lines = [
        f"{'📈 做多' if long_ else '📉 做空'}  顺势{'回踩' if long_ else '反弹'}  {name}（{bar}）  {kind_txt}",
        f"时间 {fmt_time(r['ts'])}（北京时间，K线收盘）",
        f"大周期趋势：{htf} {TREND_TXT[trend_htf]}" + (f"；4H {TREND_TXT[trend_4h]}" if htf != "4H" else ""),
        f"{'回踩' if long_ else '反弹'}到 {bar} MA25（{fmt_px(r['ma25'])}）附近，MA99 {fmt_px(r['ma99'])}，"
        f"{'前高' if long_ else '前低'} {fmt_px(r['sh'])}，{'回踩低点' if long_ else '反弹高点'} {fmt_px(r['pb_low'])}",
    ]
    if r["kind"] == "confirm":
        lines.append(f"参考入场价 {fmt_px(r['price'])}，止损 {fmt_px(r['stop'])}（距离 {r['risk']*100:.2f}%）")
        lines.append(f"目标1 {fmt_px(r['target1'])}（{'前高' if long_ else '前低'}，盈亏比 {r['rr1']:.1f}），"
                     f"目标2 {fmt_px(r['target2'])}（等幅延伸，盈亏比 {r['rr2']:.1f}）")
    else:
        lines.append(f"现价 {fmt_px(r['price'])}。等出现{'阳线收盘越过上一根最高价' if long_ else '阴线收盘跌破上一根最低价'}再考虑进场，"
                     f"{'跌破' if long_ else '涨破'} {fmt_px(r['pb_low'])} 则这次回踩失败")
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    return "\n".join(lines)


def main():
    state = load_state()
    state.setdefault("last_alert", {})
    now_ms = int(time.time() * 1000)

    cache = {}

    def klines(inst, bar, limit=150):
        if (inst, bar) not in cache:
            cache[(inst, bar)] = get_klines(inst, bar, limit)
            time.sleep(0.25)
        return cache[(inst, bar)]

    parts = []
    for inst, bar, htf in TARGETS:
        try:
            ks = klines(inst, bar)
            trend = trend_of(klines(inst, htf, 130))
            trend_4h = trend if htf == "4H" else trend_of(klines(inst, "4H", 130))
        except Exception as e:
            print(f"{inst} {bar} 数据获取失败: {e}", file=sys.stderr)
            continue
        if trend in (None, 0):
            print(f"{inst} {bar}：{htf} 趋势{TREND_TXT[trend]}，不推送")
            continue
        side = "long" if trend == 1 else "short"
        r = analyze(ks if side == "long" else invert(ks), bar)
        if not r:
            print(f"{inst} {bar}：{htf} 趋势{TREND_TXT[trend]}，但没有回踩到位/确认信号")
            continue
        if side == "short":
            r = convert_short(r, ks)

        cd = (CONFIRM_COOLDOWN_MIN if r["kind"] == "confirm" else ZONE_COOLDOWN_MIN)[bar]
        key = f"{inst}|{bar}|{side}|{r['kind']}"
        last = state["last_alert"].get(key)
        if last and now_ms - int(last) < cd * 60 * 1000:
            print(f"{inst} {bar} {side} {r['kind']}：冷却中，不重复推送")
            continue
        # 同一组里刚推过确认信号，就不再推到位提醒
        if r["kind"] == "zone" and state["last_alert"].get(f"{inst}|{bar}|{side}|confirm") and \
                now_ms - int(state["last_alert"][f"{inst}|{bar}|{side}|confirm"]) < cd * 60 * 1000:
            continue

        parts.append(fmt_alert(inst, bar, htf, side, r, trend, trend_4h, get_funding(inst)))
        state["last_alert"][key] = str(now_ms)
        time.sleep(0.1)

    if parts:
        push_to_dingtalk("合约顺势单提醒", "\n\n———\n\n".join(parts))
    else:
        print("本次没有顺势信号。")

    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now_ms - int(v) < 3 * 24 * 3600 * 1000}
    save_state(state)


if __name__ == "__main__":
    main()
