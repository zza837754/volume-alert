#!/usr/bin/env python3
"""
OKX 永续合约 涨幅榜 / 跌幅榜 监控

每次运行：
  1) 取全部 USDT 永续的 24 小时涨跌幅，排出涨幅榜前 30、跌幅榜前 30
  2) 榜内的币满足下面任意一条就推送到钉钉：
       A. 新进榜：这一轮在前 30，上一轮不在
       B. 排名飙升：和大约 1 小时前相比，排名上升 >= RANK_RISE_MIN 名
       C. 回调再启动形态（15 分钟线）：
            涨幅榜：前面一波拉升 → 回调 → 重新站上 MA25 和 MA7（ARIA 那种走势）
            跌幅榜：镜像，前面一波暴跌 → 反弹 → 重新跌破 MA25 和 MA7
       D. 早期异动（默认开启，主要靠它抢在行情前面）：在成交额靠前的 EARLY_TOP_N 个币里（不限榜单），
          最近 15 分钟涨/跌幅达到 EARLY_MOVE_15M、成交量明显放大、而此前 1 小时还很平静，且现价仍在极值附近
       E. 放量榜（默认开启，不看涨跌幅）：同样的币池里，按"最近 15 分钟成交额 / 此前均值"排名，
          放量 >= VOLBOARD_RATIO 倍、但价格还没怎么动（没被 D 抓到）的币上榜，提示"放量待变盘"
       F. 急速拉升 / 急速下跌（默认开启）：5 分钟内涨跌 >= 3% 或 15 分钟内涨跌 >= 5%，放量，并且
          突破（跌破）了此前 1 小时的高点（低点）、现价仍在极值附近。和 D 的区别：不要求此前很平静，所以
          持续拉升、连续下跌的币也能抓到
  4) 效果复盘：每条推送（D/F/C）都会记下方向和价格，1 小时、4 小时后自动回头看涨跌，
     每天早上 REPORT_HOUR_BJ 点推一份复盘，告诉你每种触发的真实胜率，用来调参数
       H. 暴涨见顶后反弹做空（默认开启）：24 小时大涨的币里，找"暴涨 100%+ → 从高点回落 20%+ → 反弹没创新高、
          在 MA25/MA99/50% 回撤位受阻 → 出现确认阴线"的，提示做空
  3) A/B 同一个币同一方向 COOLDOWN_MIN 分钟内只推一次；C 同一次形态只推一次

说明：
  - 脚本每轮会把榜单快照存进 rank_state.json，排名变化靠它来算。
  - 第一次运行没有"上一轮"，A 不会触发；B 要累计约 45 分钟快照后才生效；C 第一次运行就可能触发。

独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK_RANK（或 DINGTALK_WEBHOOK）不设置时只打印不推送。
"""

import datetime
import json
import math
import os
import sys
import time

from concurrent.futures import ThreadPoolExecutor

import requests

# ========== 可调参数 ==========
BAR = "5m"                      # 用来看"近1小时"走势的K线周期
TOP_N_RANK = 30                 # 看榜单前多少名
MIN_TURNOVER_USDT = 20_000_000  # 24h成交额下限（2000万U），小盘币不进榜单
EXCLUDE = {"BTC", "ETH"}        # 不参与榜单的币

MIN_CHANGE_24H = 0.03           # 兜底：24小时涨/跌幅至少 3% 才算（避免行情平静时前30名都只有零点几个点）；设 0 就是完全不限制
RANK_RISE_MIN = 10              # 排名飙升：和约1小时前相比，上升至少 10 名
HIST_MIN_AGE = 45               # 用来比较的"1小时前"快照，至少要多旧（分钟）
HIST_MAX_AGE = 90               # 快照最多保留多久（分钟）
PREV_MAX_AGE = 30               # "上一轮"快照超过这么久就不算（比如中途停跑过），避免误判新进榜

# --- 效果复盘 ---
ENABLE_TRACKING = True          # 记录信号并统计胜率
TRACK_FEE = 0.001               # 统计收益时扣掉的手续费+滑点（一进一出合计 0.1%）
TRACK_KEEP_HOURS = 72           # 信号记录保留多久
REPORT_HOUR_BJ = 9              # 每天北京时间几点之后推送复盘（只推一次）

# --- 触发开关 ---
# A/B 按 24 小时涨跌幅排名，币要先涨/跌很多才进得了榜，天然滞后，所以默认关闭
ENABLE_RANK_TRIGGERS = False    # A/B：新进榜、排名飙升
ENABLE_SHAPE_TRIGGER = True     # C：回调再启动形态
ENABLE_EARLY_TRIGGER = True     # D：早期异动

ENABLE_VOLUME_BOARD = True      # E：放量榜（成交额先放大，价格还没动）
ENABLE_RAPID_TRIGGER = True     # F：急速拉升 / 急速下跌
ENABLE_PUMPDUMP_SHORT = True    # H：暴涨见顶后反弹做空

# --- H：暴涨见顶后反弹做空（15 分钟线）---
PD_MIN_CHANGE_24H = 0.25        # 先从 24 小时涨幅 >= 25% 的币里找（暴涨后回落 30%，24 小时涨幅通常还在这个水平以上）
PD_BASE_BARS = 100              # 暴涨起点：高点之前 100 根里的最低价
PD_PUMP_MIN = 1.0               # 暴涨幅度（起点→高点）至少 100%；想抓小一点的，调成 0.5
PD_DROP_MIN = 0.20              # 从高点回落至少 20%
PD_DROP_MAX = 0.65              # 回落超过 65% 就算崩盘了，不做反弹
PD_BOUNCE_MIN = 0.06            # 回落之后反弹至少 6%（相对回落低点）
PD_RETRACE_MIN = 0.20           # 反弹收回的跌幅比例：20%～75%
PD_RETRACE_MAX = 0.75
PD_LOWER_HIGH = 0.05            # 反弹高点至少比暴涨高点低 5%（没有创新高）
PD_RES_TOL = 0.015              # 反弹高点要落在阻力位附近（MA25、MA99、跌幅的 50% 或 61.8% 回撤位，相差 1.5% 以内）
PD_MAX_BARS_AFTER_BOUNCE = 3    # 反弹高点之后 3 根内出现确认阴线
PD_RISK_MAX = 0.08              # 止损（反弹高点上方）距离最大 8%
PD_MIN_RR = 1.5                 # 到"等幅下跌目标"的盈亏比至少 1.5
PD_FUNDING_MIN = -0.0010        # 资金费率低于 -0.10%（空头太拥挤，容易被逼空）不推
PD_MAX_PUSH = 5

# --- F：急速拉升 / 急速下跌（5 分钟线）---
RAPID_MOVE_5M = 0.03            # 最近 1 根 5 分钟线涨/跌 >= 3%
RAPID_MOVE_15M = 0.05           # 或者最近 15 分钟（3 根）涨/跌 >= 5%
RAPID_VOL_RATIO = 2.0           # 最近 15 分钟成交额 >= 此前均值的 2 倍（有量才算，避免薄盘瞎拉）
RAPID_STILL_MOVING = 0.007      # 现价离最近 15 分钟极值不超过 0.7%（还在走，不是已经回头）
RAPID_COOLDOWN_MIN = 60         # 同一个币同一方向 60 分钟内只推一次
RAPID_MAX_PUSH = 5              # 每个方向每轮最多推几个

# --- E：放量榜 ---
VOLBOARD_RATIO = 4.0            # 最近 15 分钟成交额 >= 此前均值的 4 倍（没有价格确认，所以比 D 的 3 倍更严）
VOLBOARD_MAX_PUSH = 5           # 每轮最多推几个（按放量倍数从高到低）
VOLBOARD_COOLDOWN_MIN = 60      # 同一个币 60 分钟内只推一次

# --- D：早期异动（5 分钟线）---
EARLY_TOP_N = 150               # 在成交额前 150 的币里找（不限于涨跌幅榜前 30；D、E、F 共用这批 5 分钟线）
EARLY_MOVE_15M = 0.025          # 最近 15 分钟（3 根）涨/跌幅至少 2.5%
EARLY_PRIOR_MAX = 0.02          # 此前 1 小时（再往前 12 根）累计波动不超过 2%：说明是刚开始，不是追在已经走完的行情后面
EARLY_VOL_RATIO = 3.0           # 最近 15 分钟成交额 >= 此前 2 小时同样长度均值的 3 倍
EARLY_STILL_MOVING = 0.005      # 现价离最近 15 分钟的极值不超过 0.5%（还在走，不是已经反弹回来）
EARLY_COOLDOWN_MIN = 60         # 同一个币同一方向 60 分钟内只推一次
EARLY_MAX_PUSH = 5             # 每个方向每轮最多推几个

# --- C：回调再启动形态（15 分钟线）的参数，含义和 pattern_scanner.py 里一致 ---
SHAPE_BAR = "15m"
PUMP_UP_MIN = 0.15              # 前面那波拉升（或暴跌）幅度至少 15%
PUMP_BASE_BARS = 60             # 拉升起点：峰值之前 60 根里的最低价
PEAK_SEARCH = 90                # 往回找峰值的范围（根）
PEAK_MIN_AGO = 6                # 峰值至少在 6 根之前
RETRACE_MIN = 0.20              # 回调幅度占整波的比例：20%～75%
RETRACE_MAX = 0.55                # 回调不能超过整波的 55%（回调越深，说明买盘越弱；复盘里亏的那几条回调都超过了一半）
RESTART_CROSS_BARS = 4          # 最近 4 根内重新站上 MA25
RESTART_VOL_BASE_BARS = 8       # 成交量对比：回调整理期（再往前 8 根）
RESTART_VOL_RATIO = 1.3         # 站上时的成交量 >= 整理期均量 * 该倍数（要明显放量）
RESTART_MA7_TOL = 0.02          # MA7 不能比 MA25 低超过 2%
RESTART_MAX_EXT = 0.06          # 现价离 MA25 超过 6% 不推（别追）
# 形态信号只给"现在正在大幅异动"的币（像 ARIA 那种），已经走完、成交量枯竭的币不推
SHAPE_MIN_CHANGE_24H = 0.20     # 24小时涨幅（跌幅榜则是跌幅）至少 20%
SHAPE_MIN_MOVE_1H = 0.005       # 近1小时同方向至少 0.5%（回调再启动正在往上走，而不是还在掉头）
SHAPE_MIN_VOL_BOOST = 1.2       # 近1小时成交额 >= 日均每小时的 1.2 倍（量能没有枯竭）
SHAPE_REQUIRE_BREAKOUT = True   # 现价必须突破"站上MA25之前整理期"的最高点，只站上均线不够
SHAPE_REQUIRE_HTF = True        # 1 小时趋势必须同向：做多要求 1H 收盘在 MA25 上方且 MA25 上行（做空反过来）
SHAPE_BTC_FILTER = True         # BTC 逆势时不推：做多时 BTC 近 1 小时跌超 1%、做空时涨超 1% 就跳过
SHAPE_BTC_BLOCK = 0.01
SHAPE_COOLDOWN_MIN = 720        # 同一个币同一方向，形态信号 12 小时内只推一次
SHAPE_SAME_PEAK_TOL = 0.003     # 高点（低点）相差不到 0.3% 视为同一波，同一波永远只推一次

# 下面三项只用来在消息里显示"动量"，默认不作为推送条件；想当条件就把 REQUIRE_MOMENTUM 改成 True
REQUIRE_MOMENTUM = False
MIN_MOVE_1H = 0.015             # REQUIRE_MOMENTUM=True 时：近1小时同方向至少 1.5%
MIN_VOL_BOOST = 2.0             # REQUIRE_MOMENTUM=True 时：近1小时成交额 >= 日均每小时的 2 倍

COOLDOWN_MIN = 120              # 同一个币同一方向，多少分钟内不重复推
MAX_PUSH = 5                    # 涨幅榜、跌幅榜每轮各最多推几个

REQUIRE_BINANCE = True          # 只看币安也有合约的币（需要 binance_perp_symbols.txt，见 pattern_scanner.py 的说明）
BINANCE_FILE = "binance_perp_symbols.txt"
BINANCE_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
STATE_FILE = "rank_state.json"
DINGTALK_SECURITY_KEYWORD = "合约"   # 要和钉钉机器人"自定义关键词"一致
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"


# ---------- 基础工具 ----------
def push_to_dingtalk(title, content):
    # 优先用本脚本专用的群（DINGTALK_WEBHOOK_RANK），没设置就用默认群（DINGTALK_WEBHOOK）
    webhook = os.environ.get("DINGTALK_WEBHOOK_RANK") or os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        print("未设置 DINGTALK_WEBHOOK_RANK / DINGTALK_WEBHOOK，仅打印：")
        print(title)
        print(content)
        return
    if DINGTALK_SECURITY_KEYWORD not in title and DINGTALK_SECURITY_KEYWORD not in content:
        title = f"【{DINGTALK_SECURITY_KEYWORD}】{title}"
    resp = requests.post(webhook, json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}}, timeout=10)
    print("钉钉返回:", resp.text)


def get_klines(inst, bar, limit):
    resp = requests.get(CANDLES_URL, params={"instId": inst, "bar": bar, "limit": limit}, timeout=(5, 8))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX接口返回错误: {data}")
    return list(reversed(data["data"]))


def get_funding(inst):
    try:
        resp = requests.get(FUNDING_URL, params={"instId": inst}, timeout=(5, 8))
        d = resp.json()
        if d.get("code") == "0" and d["data"]:
            return float(d["data"][0]["fundingRate"])
    except Exception as e:
        print(f"{inst} 资金费率获取失败: {e}", file=sys.stderr)
    return None


def _norm_coin(base):
    b = base.upper()
    for p_ in ("1000000", "100000", "10000", "1000"):
        if b.startswith(p_) and len(b) > len(p_):
            return b[len(p_):]
    return b


def _bases_from_info(info):
    return sorted({x["baseAsset"] for x in info["symbols"]
                   if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT"
                   and x.get("status") == "TRADING"})


def load_binance_coins():
    """币安 USDT 永续币种集合：在线获取 -> binance_exchangeInfo.json -> 缓存 txt；都没有返回 None"""
    try:
        resp = requests.get(BINANCE_URL, timeout=(3, 8))
        resp.raise_for_status()
        bases = _bases_from_info(resp.json())
        if len(bases) > 50:
            with open(BINANCE_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(bases))
            return {_norm_coin(b) for b in bases}
    except Exception as e:
        print(f"币安合约列表在线获取失败（{e}），尝试本地文件", file=sys.stderr)
    for path, loader in (("binance_exchangeInfo.json", lambda p_: _bases_from_info(json.load(open(p_, encoding="utf-8")))),
                         (BINANCE_FILE, lambda p_: [x.strip() for x in open(p_, encoding="utf-8") if x.strip()])):
        if os.path.exists(path):
            try:
                bases = loader(path)
                if len(bases) > 50:
                    return {_norm_coin(b) for b in bases}
            except Exception as e:
                print(f"读取 {path} 失败: {e}", file=sys.stderr)
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"hist_up": [], "hist_down": [], "last_alert": {}, "shape_seen": {}, "signals": [], "last_report_date": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------- 榜单 ----------
def build_rankings():
    """返回 (涨幅榜, 跌幅榜)，每项是 dict：inst, last, chg, turnover, rank"""
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
        try:
            last, open24 = float(t["last"]), float(t["open24h"])
            turnover = float(t["volCcy24h"]) * last
        except (ValueError, KeyError):
            continue
        if open24 <= 0 or turnover < MIN_TURNOVER_USDT:
            continue
        rows.append({"inst": inst, "last": last, "chg": last / open24 - 1, "turnover": turnover})

    if REQUIRE_BINANCE:
        bn = load_binance_coins()
        if bn is None:
            print("警告：拿不到币安合约列表，本轮不做'币安也有'过滤", file=sys.stderr)
        else:
            before = len(rows)
            rows = [r for r in rows if _norm_coin(r["inst"].split("-")[0]) in bn]
            print(f"币安也有的币: {len(rows)}/{before}")

    up = sorted((r for r in rows if r["chg"] > 0), key=lambda r: r["chg"], reverse=True)[:TOP_N_RANK]
    down = sorted((r for r in rows if r["chg"] < 0), key=lambda r: r["chg"])[:TOP_N_RANK]
    for i, r in enumerate(up, 1):
        r["rank"] = i
    for i, r in enumerate(down, 1):
        r["rank"] = i
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return up, down, rows


def get_momentum(item):
    """近1小时走势：返回 (1小时涨跌幅, 放量倍数)，取不到返回 None"""
    per_hour = max(1, 60 // {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(BAR, 5))
    ks = [k for k in get_klines(item["inst"], BAR, per_hour + 5) if k[8] == "1"]
    if len(ks) < per_hour + 1:
        return None
    closes = [float(k[4]) for k in ks]
    move = closes[-1] / closes[-1 - per_hour] - 1
    recent = ks[-per_hour:]
    quote = sum(float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in recent)
    avg_hour = item["turnover"] / 24
    return move, (quote / avg_hour if avg_hour > 0 else 0)


def fmt_px(x):
    """价格：保留 6 位有效数字，不用科学计数法（4.373e-06 会显示成 0.000004373）"""
    x = float(x)
    if x == 0:
        return "0"
    digits = max(0, 5 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{x:.0f}"


# ---------- 效果复盘 ----------
KIND_NAMES = {"early": "早期异动", "rapid": "急速拉升/下跌", "shape": "回调再启动形态", "pdshort": "暴涨见顶做空"}


def log_signal(state, inst, kind, direction, price, now_ms):
    """记下一条已推送的信号：以推送时的价格作为参考入场价"""
    if ENABLE_TRACKING:
        state["signals"].append({"inst": inst, "kind": kind, "dir": direction, "entry": float(price),
                                 "at": now_ms, "r1": None, "r4": None})


def update_signals(state, last_prices, now_ms):
    """到期的信号（满 1 小时 / 4 小时）按当前价算收益；收益按信号方向，扣手续费"""
    for sig in state["signals"]:
        px = last_prices.get(sig["inst"])
        if not px or sig["entry"] <= 0:
            continue
        sgn = 1 if sig["dir"] == "up" else -1
        ret = sgn * (px / sig["entry"] - 1) - TRACK_FEE
        age_min = (now_ms - sig["at"]) / 60000
        if sig["r1"] is None and age_min >= 60:
            sig["r1"] = ret
        if sig["r4"] is None and age_min >= 240:
            sig["r4"] = ret
    state["signals"] = [x for x in state["signals"] if (now_ms - x["at"]) / 3600000 <= TRACK_KEEP_HOURS]


def build_report(state, now_ms):
    lines = []
    for kind, name in KIND_NAMES.items():
        rows = [x for x in state["signals"] if x["kind"] == kind]
        r1 = [x["r1"] for x in rows if x["r1"] is not None]
        r4 = [x["r4"] for x in rows if x["r4"] is not None]
        if not r1:
            continue
        part = f"{name}：共 {len(rows)} 条｜1小时 胜率 {sum(1 for v in r1 if v > 0)/len(r1)*100:.0f}%（平均 {sum(r1)/len(r1)*100:+.2f}%，{len(r1)}条）"
        if r4:
            part += f"｜4小时 胜率 {sum(1 for v in r4 if v > 0)/len(r4)*100:.0f}%（平均 {sum(r4)/len(r4)*100:+.2f}%，{len(r4)}条）"
        lines.append(part)
        if len(rows) <= 8:                                   # 样本少的时候，把每条信号列出来，方便看是哪些币、怎么亏的
            for x in rows:
                f = lambda v: "待定" if v is None else f"{v*100:+.1f}%"
                lines.append(f"    · {x['inst'].replace('-SWAP', '')} {'做多' if x['dir'] == 'up' else '做空'}｜入场 {fmt_px(x['entry'])}｜1小时 {f(x['r1'])}｜4小时 {f(x['r4'])}")
        for d, dn in (("up", "做多"), ("down", "做空")):
            rr = [x["r1"] for x in rows if x["dir"] == d and x["r1"] is not None]
            if rr:
                lines.append(f"    └ {dn}：{len(rr)} 条，1小时胜率 {sum(1 for v in rr if v > 0)/len(rr)*100:.0f}%，平均 {sum(rr)/len(rr)*100:+.2f}%")
    if not lines:
        return None
    return (f"时间 {fmt_time(now_ms)}（北京时间）\n近{TRACK_KEEP_HOURS//24}天内已到期的信号：\n" + "\n".join(lines) +
            "\n说明：按推送时的价格入场、不设止损止盈，收益按信号方向并扣 0.1% 手续费，胜率=收益>0 的比例。样本少于 30 条时仅供参考。")


def maybe_report(state, now_ms):
    if not ENABLE_TRACKING:
        return
    bj = datetime.datetime.fromtimestamp(now_ms / 1000, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    today = bj.strftime("%Y-%m-%d")
    if bj.hour < REPORT_HOUR_BJ or state.get("last_report_date") == today:
        return
    state["last_report_date"] = today
    text = build_report(state, now_ms)
    if text:
        push_to_dingtalk("合约信号复盘", text)
    else:
        print("复盘：还没有到期的信号，暂不推送")


def htf_trend(inst):
    """1 小时趋势：+1 向上（收盘在 MA25 上方且 MA25 上行）/ -1 向下 / 0 震荡；取不到返回 None（不拦截）"""
    try:
        ks = [k for k in get_klines(inst, "1H", 130) if k[8] == "1"]
        c = [float(k[4]) for k in ks]
        i = len(c) - 1
        m25, m25_4 = ma(c, 25, i), ma(c, 25, i - 4)
        if None in (m25, m25_4):
            return None
        if c[i] > m25 and m25 > m25_4:
            return 1
        if c[i] < m25 and m25 < m25_4:
            return -1
        return 0
    except Exception as e:
        print(f"{inst} 1H趋势获取失败: {e}", file=sys.stderr)
        return None


def btc_move_1h():
    """BTC 近 1 小时涨跌幅，取不到返回 None"""
    try:
        ks = [k for k in get_klines("BTC-USDT-SWAP", "5m", 20) if k[8] == "1"]
        c = [float(k[4]) for k in ks]
        return c[-1] / c[-13] - 1
    except Exception as e:
        print(f"BTC 行情获取失败: {e}", file=sys.stderr)
        return None


def fmt_time(ms):
    """毫秒时间戳 -> 北京时间 'MM-DD HH:MM'"""
    dt = datetime.datetime.fromtimestamp(int(ms) / 1000, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


def _bar_ms(bar):
    return {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60}.get(bar, 15) * 60 * 1000


# ---------- C：回调再启动形态 ----------
def ma(values, n, end):
    if end + 1 < n:
        return None
    return sum(values[end - n + 1:end + 1]) / n


def _invert(klines):
    """价格取倒数（O,H,L,C -> 1/O,1/L,1/H,1/C），用来把"暴跌后反弹"当成"拉升后回调"的镜像来检测"""
    return [[k[0], 1 / float(k[1]), 1 / float(k[3]), 1 / float(k[2]), 1 / float(k[4]), k[5], k[6], k[7], k[8]]
            for k in klines]


def _restart_up(klines):
    """拉升 -> 回调 -> 重新站上 MA25 和 MA7。命中返回 dict，否则 None"""
    n = len(klines)
    if n < 130:
        return None
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    v = [float(k[6]) for k in klines]
    i = n - 1

    pk = max(range(max(0, i - PEAK_SEARCH), i - PEAK_MIN_AGO + 1), key=lambda x: h[x])
    peak = h[pk]
    base = min(l[max(0, pk - PUMP_BASE_BARS):pk + 1])
    if base <= 0 or (peak - base) / base < PUMP_UP_MIN:
        return None
    pull_low = min(l[pk:i + 1])
    retrace = (peak - pull_low) / (peak - base)
    if not (RETRACE_MIN <= retrace <= RETRACE_MAX):
        return None

    m7, m7_prev, m25, m99 = ma(c, 7, i), ma(c, 7, i - 3), ma(c, 25, i), ma(c, 99, i)
    if None in (m7, m7_prev, m25, m99):
        return None
    if not (c[i] > m25 and c[i] > m7 and m7 > m7_prev and c[i] > m99):
        return None
    if m7 < m25 * (1 - RESTART_MA7_TOL):
        return None
    if (c[i] - m25) / m25 > RESTART_MAX_EXT:
        return None

    low_idx = l.index(pull_low, pk)
    cross = None
    for j in range(i, i - RESTART_CROSS_BARS, -1):
        if j > low_idx and c[j - 1] <= ma(c, 25, j - 1) and c[j] > ma(c, 25, j):
            cross = j
            break
    if cross is None:
        return None
    if SHAPE_REQUIRE_BREAKOUT and c[i] <= max(h[max(0, cross - 8):cross]):     # 还没突破整理期的高点，只是站上了均线
        return None

    prev_vol = sum(v[i - RESTART_CROSS_BARS - RESTART_VOL_BASE_BARS + 1:i - RESTART_CROSS_BARS + 1]) / RESTART_VOL_BASE_BARS
    recent_vol = max(v[i - RESTART_CROSS_BARS + 1:i + 1])
    if prev_vol <= 0 or recent_vol < prev_vol * RESTART_VOL_RATIO:
        return None

    return {"key_ts": klines[cross][0], "peak": peak, "pump_pct": (peak - base) / base, "retrace": retrace,
            "new_extreme": max(h[i - RESTART_CROSS_BARS + 1:i + 1]) >= peak, "bars_after": i - cross}


def detect_shape(klines, direction):
    """direction='up' 找拉升后回调再启动；'down' 找暴跌后反弹再下跌（镜像）。返回消息里要用的字段"""
    c = [float(k[4]) for k in klines]
    i = len(c) - 1
    if direction == "up":
        r = _restart_up(klines)
        if not r:
            return None
        return {"key_ts": r["key_ts"], "extreme": r["peak"], "move_pct": r["pump_pct"], "retrace": r["retrace"],
                "new_extreme": r["new_extreme"], "bars_after": r["bars_after"],
                "ma7": ma(c, 7, i), "ma25": ma(c, 25, i), "ma99": ma(c, 99, i)}
    r = _restart_up(_invert(klines))
    if not r:
        return None
    return {"key_ts": r["key_ts"], "extreme": 1 / r["peak"], "move_pct": r["pump_pct"] / (1 + r["pump_pct"]),
            "retrace": r["retrace"], "new_extreme": r["new_extreme"], "bars_after": r["bars_after"],
            "ma7": ma(c, 7, i), "ma25": ma(c, 25, i), "ma99": ma(c, 99, i)}


def shape_is_new(state, key, sh, now_ms):
    """同一个币同一方向：冷却期内不重复推；同一波（高点/低点几乎一样）不重复推。旧格式记录按 key_ts 比较"""
    seen = state["shape_seen"].get(key)
    if seen is None:
        return True
    if not isinstance(seen, dict):                      # 旧版本留下的记录
        return str(seen) != str(sh["key_ts"])
    if now_ms - int(seen["at"]) < SHAPE_COOLDOWN_MIN * 60 * 1000:
        return False
    if abs(sh["extreme"] / float(seen["peak"]) - 1) < SHAPE_SAME_PEAK_TOL:
        return False
    return True


def fmt_shape(direction, sh):
    up = direction == "up"
    return (
        f"形态（{SHAPE_BAR}）：{'强势回调再启动' if up else '弱势反弹再下跌'}"
        f"{'，已创新高' if up and sh['new_extreme'] else '，已创新低' if (not up) and sh['new_extreme'] else ''}\n"
        f"前一波{'拉升 +' if up else '下跌 -'}{sh['move_pct']*100:.0f}%（{'高' if up else '低'}点 {fmt_px(sh['extreme'])}），"
        f"{'回调' if up else '反弹'}了 {sh['retrace']*100:.0f}%，{'站上' if up else '跌破'}MA25 的K线收盘于 "
        f"{fmt_time(int(sh['key_ts']) + _bar_ms(SHAPE_BAR))}（已过 {sh['bars_after']} 根）\n"
        f"MA7 {fmt_px(sh['ma7'])} / MA25 {fmt_px(sh['ma25'])} / MA99 {fmt_px(sh['ma99'])}"
    )


# ---------- E：放量榜 ----------
def volume_board_info(klines):
    """放量倍数、近15分钟涨跌、与此前1小时区间的关系。数据不够返回 None"""
    if len(klines) < 40:
        return None
    c = [float(k[4]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    q = [float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in klines]
    base = sum(q[-27:-3]) / 24 * 3
    if base <= 0:
        return None
    hi, lo = max(h[-15:-3]), min(l[-15:-3])          # 此前 1 小时的区间（不含最近 15 分钟）
    pos = "up" if c[-1] > hi else "down" if c[-1] < lo else "inside"
    return {"ratio": sum(q[-3:]) / base, "move15": c[-1] / c[-4] - 1, "pos": pos, "hi": hi, "lo": lo, "price": c[-1]}


def fmt_volume(rank, item, r, funding, now_ms):
    lean = ("偏多（已向上突破此前区间）" if r["pos"] == "up" else
            "偏空（已向下跌破此前区间）" if r["pos"] == "down" else
            "偏多（价格在抬升）" if r["move15"] >= 0.003 else
            "偏空（价格在走低）" if r["move15"] <= -0.003 else "方向待定")
    pos_txt = {"up": "已突破上沿", "down": "已跌破下沿", "inside": "仍在区间内"}[r["pos"]]
    lines = [
        f"👀 放量榜 #{rank}  {item['inst'].replace('-SWAP', '')}  放量待变盘",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        f"最近15分钟成交额是此前均值的 {r['ratio']:.1f} 倍，近15分钟价格 {r['move15']*100:+.1f}%（还没怎么动）",
        f"此前1小时区间 {fmt_px(r['lo'])} ~ {fmt_px(r['hi'])}，现价 {fmt_px(r['price'])}（{pos_txt}）",
        f"倾向：{lean}",
        f"24h {item['chg']*100:+.1f}%，24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    return "\n".join(lines)


# ---------- F：急速拉升 / 急速下跌 ----------
def detect_rapid(klines):
    """急速拉升/下跌：短时间大幅涨跌 + 放量 + 突破此前 1 小时高点（低点）+ 还在极值附近。命中返回 dict"""
    if len(klines) < 40:
        return None
    c = [float(k[4]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    q = [float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in klines]

    move5, move15, move60 = c[-1] / c[-2] - 1, c[-1] / c[-4] - 1, c[-1] / c[-13] - 1
    up = move5 >= RAPID_MOVE_5M or move15 >= RAPID_MOVE_15M
    down = move5 <= -RAPID_MOVE_5M or move15 <= -RAPID_MOVE_15M
    if not (up or down):
        return None
    direction = "up" if (up and not down) else "down" if (down and not up) else ("up" if move15 > 0 else "down")

    base = sum(q[-27:-3]) / 24 * 3
    ratio = sum(q[-3:]) / base if base > 0 else 0
    if ratio < RAPID_VOL_RATIO:
        return None

    hi1h, lo1h = max(h[-15:-3]), min(l[-15:-3])          # 此前 1 小时的高点/低点（不含最近 15 分钟）
    if direction == "up":
        if c[-1] <= hi1h or (max(h[-3:]) - c[-1]) / max(h[-3:]) > RAPID_STILL_MOVING:
            return None
    else:
        if c[-1] >= lo1h or (c[-1] - min(l[-3:])) / min(l[-3:]) > RAPID_STILL_MOVING:
            return None
    return {"direction": direction, "move5": move5, "move15": move15, "move60": move60, "ratio": ratio,
            "price": c[-1], "level": hi1h if direction == "up" else lo1h}


# ---------- H：暴涨见顶后反弹做空 ----------
def detect_pump_dump_short(klines):
    """暴涨 -> 见顶回落 -> 反弹受阻 -> 确认阴线。命中返回 dict，否则 None"""
    n = len(klines)
    if n < 130:
        return None
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    i = n - 1

    pk = max(range(max(0, i - PEAK_SEARCH), i - PEAK_MIN_AGO + 1), key=lambda x: h[x])     # 暴涨高点
    peak = h[pk]
    base = min(l[max(0, pk - PD_BASE_BARS):pk + 1])
    if base <= 0 or (peak - base) / base < PD_PUMP_MIN:
        return None

    tr = min(range(pk, i + 1), key=lambda x: l[x])                                          # 回落低点
    trough = l[tr]
    drop = (peak - trough) / peak
    if not (PD_DROP_MIN <= drop <= PD_DROP_MAX) or i - tr < 2:
        return None

    bo = max(range(tr + 1, i + 1), key=lambda x: h[x])                                      # 反弹高点
    bounce_high = h[bo]
    if (bounce_high - trough) / trough < PD_BOUNCE_MIN:
        return None
    retrace = (bounce_high - trough) / (peak - trough)
    if not (PD_RETRACE_MIN <= retrace <= PD_RETRACE_MAX):
        return None
    if bounce_high > peak * (1 - PD_LOWER_HIGH):                                            # 必须是更低的高点
        return None

    # 反弹要在阻力位附近受阻：MA25、MA99、跌幅的 50% / 61.8% 回撤位
    levels = {"MA25": ma(c, 25, bo), "MA99": ma(c, 99, bo),
              "50%回撤位": trough + 0.5 * (peak - trough), "61.8%回撤位": trough + 0.618 * (peak - trough)}
    hit = [(name, lv) for name, lv in levels.items() if lv and abs(bounce_high - lv) / lv <= PD_RES_TOL]
    if not hit:
        return None

    # 确认：反弹高点之后 1~PD_MAX_BARS_AFTER_BOUNCE 根内，出现阴线，收盘低于上一根收盘，并且跌破 MA7 或上一根最低价
    m7 = ma(c, 7, i)
    if not (1 <= i - bo <= PD_MAX_BARS_AFTER_BOUNCE):
        return None
    if not (c[i] < o[i] and c[i] < c[i - 1] and (c[i] < m7 or c[i] < l[i - 1])):
        return None

    stop = bounce_high * 1.003
    risk = (stop - c[i]) / c[i]
    if not (0.003 <= risk <= PD_RISK_MAX):
        return None
    t1, t2 = trough, trough - (bounce_high - trough)                                          # 目标1：前低；目标2：等幅下跌
    if c[i] <= t1:
        return None
    rr1, rr2 = (c[i] - t1) / (stop - c[i]), (c[i] - t2) / (stop - c[i])
    if rr2 < PD_MIN_RR:
        return None
    return {"key_ts": klines[pk][0], "ts": klines[i][0], "price": c[i], "stop": stop, "risk": risk,
            "peak": peak, "base": base, "pump_pct": (peak - base) / base, "trough": trough, "drop_pct": drop,
            "bounce_high": bounce_high, "bounce_pct": (bounce_high - trough) / trough, "retrace": retrace,
            "res": "、".join(f"{nm} {fmt_px(lv)}" for nm, lv in hit),
            "target1": t1, "target2": t2, "rr1": rr1, "rr2": rr2, "bars_after": i - bo}


def fmt_pumpdump(item, r, funding, now_ms):
    lines = [
        f"📉 做空  暴涨见顶后反弹受阻  {item['inst'].replace('-SWAP', '')}（15m）",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        f"暴涨 +{r['pump_pct']*100:.0f}%（{fmt_px(r['base'])} → {fmt_px(r['peak'])}），回落 -{r['drop_pct']*100:.0f}% 到 {fmt_px(r['trough'])}，"
        f"反弹 +{r['bounce_pct']*100:.0f}% 到 {fmt_px(r['bounce_high'])}（收回 {r['retrace']*100:.0f}% 的跌幅，没有创新高）",
        f"反弹受阻位置：{r['res']}；确认阴线收盘时间 {fmt_time(int(r['ts']) + 15 * 60 * 1000)}",
        f"参考入场价 {fmt_px(r['price'])}，止损 {fmt_px(r['stop'])}（距离 {r['risk']*100:.1f}%）",
        f"目标1 {fmt_px(r['target1'])}（前低，盈亏比 {r['rr1']:.1f}），目标2 {fmt_px(r['target2'])}（等幅下跌，盈亏比 {r['rr2']:.1f}）",
        f"24h {item['chg']*100:+.1f}%，24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    lines.append("提示：暴涨币反弹容易被逼空，止损务必执行。")
    return "\n".join(lines)


def fmt_rapid(item, r, rank_txt, funding, now_ms):
    up = r["direction"] == "up"
    lines = [
        f"{'🚀 急速拉升' if up else '💥 急速下跌'}  {'📈 偏多' if up else '📉 偏空'}  {item['inst'].replace('-SWAP', '')}",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        f"近5分钟 {r['move5']*100:+.1f}%，近15分钟 {r['move15']*100:+.1f}%，近1小时 {r['move60']*100:+.1f}%",
        f"已{'突破' if up else '跌破'}此前1小时{'高点' if up else '低点'} {fmt_px(r['level'])}，最近15分钟成交额是此前均值的 {r['ratio']:.1f} 倍",
        f"现价 {fmt_px(r['price'])}，24h {item['chg']*100:+.1f}%，24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
    if rank_txt:
        lines.append(rank_txt)
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    return "\n".join(lines)


# ---------- D：早期异动 ----------
def detect_early(klines):
    """最近 15 分钟（3 根 5 分钟线）刚启动的放量急涨/急跌。命中返回 dict，否则 None"""
    if len(klines) < 40:
        return None
    c = [float(k[4]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    q = [float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in klines]

    move15 = c[-1] / c[-4] - 1
    if abs(move15) < EARLY_MOVE_15M:
        return None
    prior = c[-4] / c[-16] - 1                     # 此前 1 小时
    if abs(prior) > EARLY_PRIOR_MAX:
        return None
    base = sum(q[-27:-3]) / 24 * 3                 # 此前 24 根（2 小时）折算成 3 根的成交额
    recent = sum(q[-3:])
    if base <= 0 or recent / base < EARLY_VOL_RATIO:
        return None
    if move15 < 0:                                  # 还在往下：现价离最近 15 分钟最低点不远
        direction = "down"
        if (c[-1] - min(l[-3:])) / min(l[-3:]) > EARLY_STILL_MOVING:
            return None
    else:
        direction = "up"
        if (max(h[-3:]) - c[-1]) / max(h[-3:]) > EARLY_STILL_MOVING:
            return None
    return {"direction": direction, "move15": move15, "prior": prior, "ratio": recent / base, "price": c[-1]}


def fmt_early(item, r, rank_txt, funding, now_ms):
    up = r["direction"] == "up"
    lines = [
        f"{'📈 做多' if up else '📉 做空'}  ⚡早期异动  {item['inst'].replace('-SWAP', '')}",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        f"近15分钟 {r['move15']*100:+.1f}%（此前1小时 {r['prior']*100:+.1f}%，刚启动），成交额是此前均值的 {r['ratio']:.1f} 倍",
        f"现价 {fmt_px(r['price'])}，24h {item['chg']*100:+.1f}%，24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
    if rank_txt:
        lines.append(rank_txt)
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    return "\n".join(lines)


def fmt_item(item, direction, tags, rank_line, mom, funding, shape_text="", now_ms=None):
    up = direction == "up"
    lines = [
        f"{'📈 做多' if up else '📉 做空'}  {'涨幅榜' if up else '跌幅榜'} #{item['rank']}  {item['inst'].replace('-SWAP', '')}  {' '.join(tags)}",
        f"时间 {fmt_time(now_ms if now_ms is not None else time.time() * 1000)}（北京时间）",
        *([rank_line] if rank_line else []),
        f"24h {item['chg']*100:+.1f}%，现价 {fmt_px(item['last'])}，24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
    if shape_text:
        lines.append(shape_text)
    if mom:
        lines.append(f"近1小时 {mom[0]*100:+.1f}%，近1小时成交额是日均每小时的 {mom[1]:.1f} 倍")
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    return "\n".join(lines)


def _pick(hist, now_ms, lo_min, hi_min, target_min=60):
    """从快照列表里挑一个年龄在 [lo_min, hi_min] 分钟内、最接近 target_min 的；没有返回 None"""
    best, best_gap = None, None
    for ts, ranks in hist:
        age = (now_ms - ts) / 60000
        if lo_min <= age <= hi_min:
            gap = abs(age - target_min)
            if best is None or gap < best_gap:
                best, best_gap = ranks, gap
    return best


def main():
    state = load_state()
    state.setdefault("last_alert", {})
    state.setdefault("shape_seen", {})
    state.setdefault("signals", [])
    state.setdefault("last_report_date", "")
    now_ms = int(time.time() * 1000)
    print(f"读取状态：涨幅榜快照 {len(state.get('hist_up', []))} 份，跌幅榜快照 {len(state.get('hist_down', []))} 份，"
          f"已推过的形态 {len(state['shape_seen'])} 个，冷却记录 {len(state['last_alert'])} 个")

    btc_cache = {"v": None}
    up, down, universe = build_rankings()
    if ENABLE_TRACKING:
        update_signals(state, {r["inst"]: r["last"] for r in universe}, now_ms)
    print(f"榜单：涨幅榜 {len(up)} 个，跌幅榜 {len(down)} 个（成交额≥{MIN_TURNOVER_USDT/1e6:.0f}M）")
    if up:
        print("涨幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in up[:5]))
    if down:
        print("跌幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in down[:5]))

    # ---- D：早期异动（不限榜单，看成交额前 EARLY_TOP_N 的币）----
    if ENABLE_EARLY_TRIGGER or ENABLE_VOLUME_BOARD or ENABLE_RAPID_TRIGGER:
        board_rank = {("up", r["inst"]): r["rank"] for r in up}
        board_rank.update({("down", r["inst"]): r["rank"] for r in down})
        cand = universe[:EARLY_TOP_N]

        def fetch5(item):
            try:
                ks = [k for k in get_klines(item["inst"], "5m", 40) if k[8] == "1"]
                return item, ks
            except Exception as e:
                print(f"{item['inst']} 5m K线获取失败: {e}", file=sys.stderr)
                return item, None
            finally:
                time.sleep(0.2)

        with ThreadPoolExecutor(max_workers=3) as ex:
            fetched = list(ex.map(fetch5, cand))
        early_hits = {"up": [], "down": []}
        for item, ks in fetched:
            if not ks:
                continue
            r = detect_early(ks) if ENABLE_EARLY_TRIGGER else None
            if not r:
                continue
            key = f"{item['inst']}|early_{r['direction']}"
            last = state["last_alert"].get(key)
            if last and now_ms - int(last) < EARLY_COOLDOWN_MIN * 60 * 1000:
                continue
            early_hits[r["direction"]].append((item, r))
        print(f"早期异动：扫描 {len(cand)} 个币，命中 做多 {len(early_hits['up'])} 个 / 做空 {len(early_hits['down'])} 个")
        for direction, title in (("up", "合约异动提醒【偏多】"), ("down", "合约异动提醒【偏空】")):
            hits = sorted(early_hits[direction], key=lambda x: abs(x[1]["move15"]), reverse=True)[:EARLY_MAX_PUSH]
            parts = []
            for item, r in hits:
                rk = board_rank.get((direction, item["inst"]))
                rank_txt = f"当前{'涨幅榜' if direction == 'up' else '跌幅榜'} #{rk}" if rk else ""
                parts.append(fmt_early(item, r, rank_txt, get_funding(item["inst"]), now_ms))
                time.sleep(0.1)
                state["last_alert"][f"{item['inst']}|early_{direction}"] = str(now_ms)
                log_signal(state, item["inst"], "early", direction, r["price"], now_ms)
            if parts:
                push_to_dingtalk(title, "\n\n———\n\n".join(parts))

        rapid_insts = set()
        if ENABLE_RAPID_TRIGGER:
            rapid_hits = {"up": [], "down": []}
            for item, ks in fetched:
                if not ks:
                    continue
                r = detect_rapid(ks)
                if not r:
                    continue
                d_ = r["direction"]
                last = state["last_alert"].get(f"{item['inst']}|rapid_{d_}")
                last_early = state["last_alert"].get(f"{item['inst']}|early_{d_}")      # 刚被"早期异动"推过同方向的，不重复
                if (last and now_ms - int(last) < RAPID_COOLDOWN_MIN * 60 * 1000) or \
                   (last_early and now_ms - int(last_early) < RAPID_COOLDOWN_MIN * 60 * 1000):
                    continue
                rapid_hits[d_].append((item, r))
            print(f"急速拉升/下跌：命中 拉升 {len(rapid_hits['up'])} 个 / 下跌 {len(rapid_hits['down'])} 个")
            for direction, title in (("up", "合约急速拉升提醒【偏多】"), ("down", "合约急速下跌提醒【偏空】")):
                hits = sorted(rapid_hits[direction], key=lambda x: abs(x[1]["move15"]), reverse=True)[:RAPID_MAX_PUSH]
                parts = []
                for item, r in hits:
                    rk = board_rank.get((direction, item["inst"]))
                    rank_txt = f"当前{'涨幅榜' if direction == 'up' else '跌幅榜'} #{rk}" if rk else ""
                    parts.append(fmt_rapid(item, r, rank_txt, get_funding(item["inst"]), now_ms))
                    time.sleep(0.1)
                    state["last_alert"][f"{item['inst']}|rapid_{direction}"] = str(now_ms)
                    log_signal(state, item["inst"], "rapid", direction, r["price"], now_ms)
                    rapid_insts.add(item["inst"])
                if parts:
                    push_to_dingtalk(title, "\n\n———\n\n".join(parts))

        if ENABLE_VOLUME_BOARD:
            early_keys = {(it["inst"], r["direction"]) for d_ in early_hits for it, r in early_hits[d_]}
            early_insts = {k[0] for k in early_keys}
            vol_hits = []
            for item, ks in fetched:
                if not ks or item["inst"] in early_insts or item["inst"] in rapid_insts:   # 已经被"早期异动/急速异动"抓到的不重复
                    continue
                info = volume_board_info(ks)
                if not info or info["ratio"] < VOLBOARD_RATIO or abs(info["move15"]) >= EARLY_MOVE_15M:
                    continue
                last = state["last_alert"].get(f"{item['inst']}|volboard")
                if last and now_ms - int(last) < VOLBOARD_COOLDOWN_MIN * 60 * 1000:
                    continue
                vol_hits.append((item, info))
            vol_hits.sort(key=lambda x: x[1]["ratio"], reverse=True)
            print(f"放量榜：命中 {len(vol_hits)} 个币")
            parts = []
            for rank_, (item, info) in enumerate(vol_hits[:VOLBOARD_MAX_PUSH], 1):
                parts.append(fmt_volume(rank_, item, info, get_funding(item["inst"]), now_ms))
                time.sleep(0.1)
                state["last_alert"][f"{item['inst']}|volboard"] = str(now_ms)
            if parts:
                push_to_dingtalk("合约放量榜提醒", "\n\n———\n\n".join(parts))

    # ---- H：暴涨见顶后反弹做空 ----
    if ENABLE_PUMPDUMP_SHORT:
        pumped = sorted([r for r in universe if r["chg"] >= PD_MIN_CHANGE_24H], key=lambda r: r["chg"], reverse=True)[:40]
        pd_hits = []
        for item in pumped:
            try:
                ks = [k for k in get_klines(item["inst"], "15m", 150) if k[8] == "1"]
                r = detect_pump_dump_short(ks)
            except Exception as e:
                print(f"{item['inst']} 暴涨见顶检测失败: {e}", file=sys.stderr)
                r = None
            time.sleep(0.15)
            if not r:
                continue
            key = f"{item['inst']}|pdshort"
            if not shape_is_new(state, key, {"key_ts": r["key_ts"], "extreme": r["peak"]}, now_ms):      # 同一个暴涨高点 12 小时内只推一次
                continue
            if SHAPE_BTC_FILTER:
                if btc_cache["v"] is None:
                    btc_cache["v"] = btc_move_1h()
                if btc_cache["v"] is not None and btc_cache["v"] >= SHAPE_BTC_BLOCK:
                    print(f"{item['inst']} 暴涨见顶形态成立但 BTC 近1小时 {btc_cache['v']*100:+.1f}% 在涨，做空逆势，跳过")
                    continue
            fr = get_funding(item["inst"])
            time.sleep(0.1)
            if fr is not None and fr <= PD_FUNDING_MIN:
                print(f"{item['inst']} 暴涨见顶形态成立但资金费率 {fr*100:+.3f}% 空头太拥挤，容易逼空，跳过")
                continue
            pd_hits.append((item, r, fr))
        print(f"暴涨见顶做空：检查 {len(pumped)} 个大涨的币，命中 {len(pd_hits)} 个")
        parts = []
        for item, r, fr in pd_hits[:PD_MAX_PUSH]:
            parts.append(fmt_pumpdump(item, r, fr, now_ms))
            state["shape_seen"][f"{item['inst']}|pdshort"] = {"key_ts": str(r["key_ts"]), "peak": r["peak"], "at": now_ms}
            log_signal(state, item["inst"], "pdshort", "down", r["price"], now_ms)
        if parts:
            push_to_dingtalk("合约暴涨见顶做空提醒【偏空】", "\n\n———\n\n".join(parts))

    for direction, board, hist_key, title in (
        ("up", up, "hist_up", "合约涨幅榜提醒【偏多】"),
        ("down", down, "hist_down", "合约跌幅榜提醒【偏空】"),
    ):
        hist = [(int(ts), ranks) for ts, ranks in state.get(hist_key, [])]
        prev = _pick(hist, now_ms, 0, PREV_MAX_AGE, target_min=0)       # 最近一轮
        hour_ago = _pick(hist, now_ms, HIST_MIN_AGE, HIST_MAX_AGE)        # 约 1 小时前

        parts = []
        for item in board:
            if len(parts) >= MAX_PUSH:
                break
            if abs(item["chg"]) < MIN_CHANGE_24H:
                continue
            inst = item["inst"]
            tags, rank_line, shape_text = [], "", ""

            # A/B：新进榜、排名飙升（受冷却限制）
            if ENABLE_RANK_TRIGGERS:
                is_new = prev is not None and inst not in prev
                rank_tags, rank_txt = [], ""
                if is_new:
                    rank_tags.append("🆕新进榜")
                    rank_txt = f"排名 #{item['rank']}（上一轮不在前{TOP_N_RANK}）"
                if hour_ago is not None:
                    old_rank = hour_ago.get(inst, TOP_N_RANK + 1)
                    rise = old_rank - item["rank"]
                    if rise >= RANK_RISE_MIN:
                        rank_tags.append(f"⚡排名飙升 +{rise}")
                        old_txt = f"#{old_rank}" if inst in hour_ago else f"前{TOP_N_RANK}之外"
                        rank_txt = f"排名 #{item['rank']}（约1小时前 {old_txt}，上升 {rise} 名）"
                last = state["last_alert"].get(f"{inst}|{direction}")
                cooling = bool(last and now_ms - int(last) < COOLDOWN_MIN * 60 * 1000)
                if rank_tags and not cooling:
                    tags += rank_tags
                    rank_line = rank_txt

            # C：回调再启动形态（同一次形态只推一次；只给正在大幅异动、量能没枯竭的币）
            shape_key = None
            mom = None
            if ENABLE_SHAPE_TRIGGER and abs(item["chg"]) >= SHAPE_MIN_CHANGE_24H:
                try:
                    ks = [k for k in get_klines(inst, SHAPE_BAR, 150) if k[8] == "1"]
                    sh = detect_shape(ks, direction)
                except Exception as e:
                    print(f"{inst} 形态检测失败: {e}", file=sys.stderr)
                    sh = None
                time.sleep(0.15)
                if sh:                                          # 形态成立后，再看近1小时动量和量能
                    try:
                        mom = get_momentum(item)
                    except Exception as e:
                        print(f"{inst} 取K线失败: {e}", file=sys.stderr)
                        mom = None
                    time.sleep(0.15)
                    move_ok = bool(mom) and (mom[0] >= SHAPE_MIN_MOVE_1H if direction == "up" else mom[0] <= -SHAPE_MIN_MOVE_1H)
                    if not (move_ok and mom[1] >= SHAPE_MIN_VOL_BOOST):
                        print(f"{inst} 形态成立但动量/量能不足，跳过（近1小时 {mom[0]*100:+.1f}%，量能 {mom[1]:.1f} 倍）" if mom else f"{inst} 形态成立但取不到动量，跳过")
                        sh = None
                if sh and SHAPE_REQUIRE_HTF:                        # 1 小时趋势要同向
                    t1h = htf_trend(inst)
                    time.sleep(0.15)
                    if t1h is not None and t1h != (1 if direction == "up" else -1):
                        print(f"{inst} 形态成立但 1H 趋势{'向上' if t1h == 1 else '向下' if t1h == -1 else '震荡'}，与{'做多' if direction == 'up' else '做空'}不一致，跳过")
                        sh = None
                if sh and SHAPE_BTC_FILTER:                         # BTC 不能逆势
                    if btc_cache["v"] is None:
                        btc_cache["v"] = btc_move_1h()
                    b = btc_cache["v"]
                    if b is not None and ((direction == "up" and b <= -SHAPE_BTC_BLOCK) or (direction == "down" and b >= SHAPE_BTC_BLOCK)):
                        print(f"{inst} 形态成立但 BTC 近1小时 {b*100:+.1f}% 逆势，跳过")
                        sh = None
                if sh:
                    shape_key = f"{inst}|{direction}"
                    if shape_is_new(state, shape_key, sh, now_ms):
                        tags.append("📐回调再启动" if direction == "up" else "📐反弹再下跌")
                        shape_text = fmt_shape(direction, sh)
                    else:
                        sh = None
                if not sh:
                    shape_key = None

            if not tags:
                continue

            if mom is None:
                try:
                    mom = get_momentum(item)
                except Exception as e:
                    print(f"{inst} 取K线失败: {e}", file=sys.stderr)
                time.sleep(0.15)
            if REQUIRE_MOMENTUM:
                if not mom:
                    continue
                dir_ok = mom[0] >= MIN_MOVE_1H if direction == "up" else mom[0] <= -MIN_MOVE_1H
                if not (dir_ok and mom[1] >= MIN_VOL_BOOST):
                    continue

            funding = get_funding(inst)
            time.sleep(0.1)
            parts.append(fmt_item(item, direction, tags, rank_line, mom, funding, shape_text, now_ms))
            if rank_line:
                state["last_alert"][f"{inst}|{direction}"] = str(now_ms)
            if shape_key:
                state["shape_seen"][shape_key] = {"key_ts": str(sh["key_ts"]), "peak": sh["extreme"], "at": now_ms}
                log_signal(state, inst, "shape", direction, item["last"], now_ms)

        # 记录本轮快照，只保留最近 HIST_MAX_AGE 分钟
        hist.append((now_ms, {r["inst"]: r["rank"] for r in board}))
        hist = [(ts, ranks) for ts, ranks in hist if (now_ms - ts) / 60000 <= HIST_MAX_AGE]
        state[hist_key] = [[ts, ranks] for ts, ranks in hist]

        if parts:
            push_to_dingtalk(title, "\n\n———\n\n".join(parts))
        else:
            print(f"{'涨幅榜' if direction == 'up' else '跌幅榜'}：本轮没有触发推送的币")

    # 清理超过 1 天的冷却记录，文件不会越来越大
    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now_ms - int(v) < 24 * 3600 * 1000}
    state["shape_seen"] = {k: v for k, v in state["shape_seen"].items()
                           if not isinstance(v, dict) or now_ms - int(v["at"]) < 3 * 24 * 3600 * 1000}
    state.pop("prev_up", None)
    state.pop("prev_down", None)
    maybe_report(state, now_ms)
    save_state(state)


if __name__ == "__main__":
    main()
