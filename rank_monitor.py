#!/usr/bin/env python3
"""
OKX 永续合约 涨幅榜 / 跌幅榜 监控

每次运行：
  1) 取全部 USDT 永续的 24 小时涨跌幅，排出涨幅榜前 30、跌幅榜前 30
  2) 对榜内的币逐条检查"要求"（见下面参数），全部满足才推送到钉钉：
       - 24 小时涨幅 >= MIN_CHANGE_24H（跌幅榜则是跌幅）
       - 近 1 小时仍朝同一方向走，幅度 >= MIN_MOVE_1H（说明还在动，不是早就走完）
       - 近 1 小时成交额 >= 平均每小时成交额的 MIN_VOL_BOOST 倍（放量）
       - 可选：必须是"新进榜"（REQUIRE_NEW_ENTRY = True）
  3) 同一个币同一方向 COOLDOWN_MIN 分钟内只推一次

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

MIN_CHANGE_24H = 0.08           # 24小时涨/跌幅至少 8%
MIN_MOVE_1H = 0.015             # 近1小时同方向至少 1.5%
MIN_VOL_BOOST = 2.0             # 近1小时成交额 >= 日均每小时成交额的 2 倍
REQUIRE_NEW_ENTRY = False       # True：只推"这一轮刚进榜"的币；False：在榜内且满足要求就推

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
    return {"prev_up": None, "prev_down": None, "last_alert": {}}


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


def check_momentum(item, direction):
    """近1小时走势检查，满足返回 (1小时涨跌幅, 放量倍数)，不满足或取不到返回 None"""
    per_hour = max(1, 60 // {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(BAR, 5))
    ks = [k for k in get_klines(item["inst"], BAR, per_hour + 5) if k[8] == "1"]
    if len(ks) < per_hour + 1:
        return None
    closes = [float(k[4]) for k in ks]
    move = closes[-1] / closes[-1 - per_hour] - 1
    if direction == "up" and move < MIN_MOVE_1H:
        return None
    if direction == "down" and move > -MIN_MOVE_1H:
        return None
    recent = ks[-per_hour:]
    quote = sum(float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in recent)
    avg_hour = item["turnover"] / 24
    boost = quote / avg_hour if avg_hour > 0 else 0
    if boost < MIN_VOL_BOOST:
        return None
    return move, boost


def fmt_item(item, direction, move, boost, is_new, funding):
    up = direction == "up"
    lines = [
        f"{'📈 涨幅榜' if up else '📉 跌幅榜'} #{item['rank']}  {item['inst'].replace('-SWAP', '')}"
        + ("  🆕新进榜" if is_new else ""),
        f"24h {item['chg']*100:+.1f}%，现价 {item['last']:.6g}",
        f"近1小时 {move*100:+.1f}%，近1小时成交额是日均每小时的 {boost:.1f} 倍",
        f"24h成交额 {item['turnover']/1e6:.0f}M USDT",
    ]
    if funding is not None:
        lines.append(f"资金费率 {funding*100:+.3f}%")
    return "\n".join(lines)


def main():
    state = load_state()
    now_ms = int(time.time() * 1000)

    up, down = build_rankings()
    print(f"榜单：涨幅榜 {len(up)} 个，跌幅榜 {len(down)} 个（成交额≥{MIN_TURNOVER_USDT/1e6:.0f}M）")
    if up:
        print("涨幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in up[:5]))
    if down:
        print("跌幅榜前5: " + "，".join(f"{r['inst'].split('-')[0]} {r['chg']*100:+.1f}%" for r in down[:5]))

    for direction, board, prev_key, title in (
        ("up", up, "prev_up", "合约涨幅榜提醒"),
        ("down", down, "prev_down", "合约跌幅榜提醒"),
    ):
        prev = state.get(prev_key)                       # 上一轮榜单；第一次运行为 None，不算"新进榜"
        prev_set = set(prev) if prev is not None else None
        parts = []
        for item in board:
            if len(parts) >= MAX_PUSH:
                break
            if abs(item["chg"]) < MIN_CHANGE_24H:
                continue
            is_new = prev_set is not None and item["inst"] not in prev_set
            if REQUIRE_NEW_ENTRY and not is_new:
                continue
            key = f"{item['inst']}|{direction}"
            last = state["last_alert"].get(key)
            if last and now_ms - int(last) < COOLDOWN_MIN * 60 * 1000:
                continue
            try:
                m = check_momentum(item, direction)
            except Exception as e:
                print(f"{item['inst']} 取K线失败: {e}", file=sys.stderr)
                m = None
            time.sleep(0.15)
            if not m:
                continue
            funding = get_funding(item["inst"])
            time.sleep(0.1)
            parts.append(fmt_item(item, direction, m[0], m[1], is_new, funding))
            state["last_alert"][key] = str(now_ms)
        state[prev_key] = [r["inst"] for r in board]
        if parts:
            push_to_dingtalk(title, "\n\n———\n\n".join(parts))
        else:
            print(f"{'涨幅榜' if direction == 'up' else '跌幅榜'}：本轮没有达到要求的币")

    # 清理超过 1 天的冷却记录，文件不会越来越大
    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now_ms - int(v) < 24 * 3600 * 1000}
    save_state(state)


if __name__ == "__main__":
    main()
