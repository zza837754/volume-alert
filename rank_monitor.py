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
  3) A/B 同一个币同一方向 COOLDOWN_MIN 分钟内只推一次；C 同一次形态只推一次

说明：
  - 脚本每轮会把榜单快照存进 rank_state.json，排名变化靠它来算。
  - 第一次运行没有"上一轮"，A 不会触发；B 要累计约 45 分钟快照后才生效；C 第一次运行就可能触发。

独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK_RANK（或 DINGTALK_WEBHOOK）不设置时只打印不推送。
"""

import datetime
import json
import os
import sys
import time

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

# --- 触发开关 ---
ENABLE_RANK_TRIGGERS = True     # A/B：新进榜、排名飙升
ENABLE_SHAPE_TRIGGER = True     # C：回调再启动形态

# --- C：回调再启动形态（15 分钟线）的参数，含义和 pattern_scanner.py 里一致 ---
SHAPE_BAR = "15m"
PUMP_UP_MIN = 0.15              # 前面那波拉升（或暴跌）幅度至少 15%
PUMP_BASE_BARS = 60             # 拉升起点：峰值之前 60 根里的最低价
PEAK_SEARCH = 90                # 往回找峰值的范围（根）
PEAK_MIN_AGO = 6                # 峰值至少在 6 根之前
RETRACE_MIN = 0.20              # 回调幅度占整波的比例：20%～75%
RETRACE_MAX = 0.75
RESTART_CROSS_BARS = 4          # 最近 4 根内重新站上 MA25
RESTART_VOL_BASE_BARS = 8       # 成交量对比：回调整理期（再往前 8 根）
RESTART_VOL_RATIO = 1.0         # 站上时的成交量 >= 整理期均量 * 该倍数
RESTART_MA7_TOL = 0.02          # MA7 不能比 MA25 低超过 2%
RESTART_MAX_EXT = 0.06          # 现价离 MA25 超过 6% 不推（别追）
# 形态信号只给"现在正在大幅异动"的币（像 ARIA 那种），已经走完、成交量枯竭的币不推
SHAPE_MIN_CHANGE_24H = 0.20     # 24小时涨幅（跌幅榜则是跌幅）至少 20%
SHAPE_MIN_MOVE_1H = 0.005       # 近1小时同方向至少 0.5%（回调再启动正在往上走，而不是还在掉头）
SHAPE_MIN_VOL_BOOST = 1.0       # 近1小时成交额 >= 日均每小时的 1 倍（量能没有枯竭）
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
    return {"hist_up": [], "hist_down": [], "last_alert": {}, "shape_seen": {}}


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
    return up, down


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
        f"前一波{'拉升 +' if up else '下跌 -'}{sh['move_pct']*100:.0f}%（{'高' if up else '低'}点 {sh['extreme']:.6g}），"
        f"{'回调' if up else '反弹'}了 {sh['retrace']*100:.0f}%，{'站上' if up else '跌破'}MA25 的K线收盘于 "
        f"{fmt_time(int(sh['key_ts']) + _bar_ms(SHAPE_BAR))}（已过 {sh['bars_after']} 根）\n"
        f"MA7 {sh['ma7']:.6g} / MA25 {sh['ma25']:.6g} / MA99 {sh['ma99']:.6g}"
    )


def fmt_item(item, direction, tags, rank_line, mom, funding, shape_text="", now_ms=None):
    up = direction == "up"
    lines = [
        f"{'📈 做多' if up else '📉 做空'}  {'涨幅榜' if up else '跌幅榜'} #{item['rank']}  {item['inst'].replace('-SWAP', '')}  {' '.join(tags)}",
        f"时间 {fmt_time(now_ms if now_ms is not None else time.time() * 1000)}（北京时间）",
        *([rank_line] if rank_line else []),
        f"24h {item['chg']*100:+.1f}%，现价 {item['last']:.6g}，24h成交额 {item['turnover']/1e6:.0f}M USDT",
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
    now_ms = int(time.time() * 1000)
    print(f"读取状态：涨幅榜快照 {len(state.get('hist_up', []))} 份，跌幅榜快照 {len(state.get('hist_down', []))} 份，"
          f"已推过的形态 {len(state['shape_seen'])} 个，冷却记录 {len(state['last_alert'])} 个")

    up, down = build_rankings()
    print(f"榜单：涨幅榜 {len(up)} 个，跌幅榜 {len(down)} 个（成交额≥{MIN_TURNOVER_USDT/1e6:.0f}M）")
    if up:
        print("涨幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in up[:5]))
    if down:
        print("跌幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in down[:5]))

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
    save_state(state)


if __name__ == "__main__":
    main()
