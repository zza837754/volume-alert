#!/usr/bin/env python3
"""
OKX 永续合约 涨幅榜 / 跌幅榜 监控

每次运行：
  1) 取全部 USDT 永续的 24 小时涨跌幅，排出涨幅榜前 30、跌幅榜前 30
  2) 满足下面任意一条就推送到钉钉：
       A. 新进榜：这一轮在前 30，上一轮不在
       B. 排名飙升：和大约 1 小时前相比，排名上升 >= RANK_RISE_MIN 名（1 小时前不在前 30 的按第 31 名算）
  3) 同一个币同一方向 COOLDOWN_MIN 分钟内只推一次（避免在第 30 名附近进进出出反复推）

说明：
  - 脚本每轮会把榜单快照存进 rank_state.json，排名变化靠它来算。
  - 第一次运行没有"上一轮"，不会推送；运行满约 45 分钟后，"排名飙升"才开始生效。

独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK 不设置时只打印不推送。
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
    return {"hist_up": [], "hist_down": [], "last_alert": {}}


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


def fmt_item(item, direction, tags, rank_line, mom, funding):
    up = direction == "up"
    lines = [
        f"{'📈 涨幅榜' if up else '📉 跌幅榜'} #{item['rank']}  {item['inst'].replace('-SWAP', '')}  {' '.join(tags)}",
        rank_line,
        f"24h {item['chg']*100:+.1f}%，现价 {item['last']:.6g}，24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
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
    now_ms = int(time.time() * 1000)

    up, down = build_rankings()
    print(f"榜单：涨幅榜 {len(up)} 个，跌幅榜 {len(down)} 个（成交额≥{MIN_TURNOVER_USDT/1e6:.0f}M）")
    if up:
        print("涨幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in up[:5]))
    if down:
        print("跌幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in down[:5]))

    for direction, board, hist_key, title in (
        ("up", up, "hist_up", "合约涨幅榜提醒"),
        ("down", down, "hist_down", "合约跌幅榜提醒"),
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
            tags, rank_line = [], ""
            is_new = prev is not None and inst not in prev
            if is_new:
                tags.append("🆕新进榜")
                rank_line = f"排名 #{item['rank']}（上一轮不在前{TOP_N_RANK}）"
            if hour_ago is not None:
                old_rank = hour_ago.get(inst, TOP_N_RANK + 1)
                rise = old_rank - item["rank"]
                if rise >= RANK_RISE_MIN:
                    tags.append(f"⚡排名飙升 +{rise}")
                    old_txt = f"#{old_rank}" if inst in hour_ago else f"前{TOP_N_RANK}之外"
                    rank_line = f"排名 #{item['rank']}（约1小时前 {old_txt}，上升 {rise} 名）"
            if not tags:
                continue

            key = f"{inst}|{direction}"
            last = state["last_alert"].get(key)
            if last and now_ms - int(last) < COOLDOWN_MIN * 60 * 1000:
                continue

            mom = None
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
            parts.append(fmt_item(item, direction, tags, rank_line, mom, funding))
            state["last_alert"][key] = str(now_ms)

        # 记录本轮快照，只保留最近 HIST_MAX_AGE 分钟
        hist.append((now_ms, {r["inst"]: r["rank"] for r in board}))
        hist = [(ts, ranks) for ts, ranks in hist if (now_ms - ts) / 60000 <= HIST_MAX_AGE]
        state[hist_key] = [[ts, ranks] for ts, ranks in hist]

        if parts:
            push_to_dingtalk(title, "\n\n———\n\n".join(parts))
        else:
            print(f"{'涨幅榜' if direction == 'up' else '跌幅榜'}：本轮没有新进榜或排名飙升的币")

    # 清理超过 1 天的冷却记录，文件不会越来越大
    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now_ms - int(v) < 24 * 3600 * 1000}
    state.pop("prev_up", None)
    state.pop("prev_down", None)
    save_state(state)


if __name__ == "__main__":
    main()
