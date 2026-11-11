#!/usr/bin/env python3
"""
OKX 永续合约 主力动向监控（俗称"庄家"手法的量价特征）

主力/庄家操盘常见的剧本是：吸筹 → 洗盘 → 拉升 → 出货 → 砸盘。每个阶段在K线、成交量、持仓量上会留下痕迹，
这个脚本把几个典型痕迹量化后监控，命中就推送"线索"：

  1) 吸筹/蓄势    （1小时线）波动明显收窄 + 成交额放大 + 价格还没走出去，低点抬高（或高点降低）
  2) 插针洗盘     （15分钟线）长下影/长上影针 + 放量，收盘又收回 —— 扫止损、洗盘、诱多诱空
  3) 高位出货     （1小时线）24 小时内暴涨后，在高位放量滞涨、连续长上影
  4) 放量不涨     （1小时线）成交额暴增但价格几乎不动 —— 对倒、吸筹或出货前的换手
  5) 持仓量激增   （持仓量快照）持仓量 1 小时/4 小时内大幅增加而价格没动 —— 杠杆在堆积，变盘在即

重要说明：
  - 这些是经验规则，不是"识别庄家"。同样的形状，很多时候只是普通的横盘、对冲或消息交易，假信号不少。
  - 命中不等于一定大涨/大跌，也不保证方向；消息里的"倾向"只是依据特征做的粗略判断。
  - 小币种流动性差，来回插针很常见，务必控制仓位、设好止损。
  - 想知道每种特征出现之后实际怎么走，用 whale_replay.py 在历史数据上回放统计。

独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK_WHALE（或 DINGTALK_WEBHOOK）不设置时只打印不推送。
手动测试推送通道：python whale_watch.py --test
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
TOP_N = 150                     # 只看成交额前 N 的币
MIN_TURNOVER_USDT = 5_000_000   # 24h成交额下限（庄家手法多发生在中小币，所以门槛比其他脚本低）
MAX_TURNOVER_USDT = 3_000_000_000   # 上限：BTC、ETH 这类巨无霸不适用这些规则
EXCLUDE_NON_CRYPTO = True       # 排除美股、商品、外汇等非加密永续合约（OKX 上有 NVDA、MSTR、QQQ、XAU 这类）。
                                # 它们有开收盘和周末休市，周末会天然出现"波动收窄"，被误判成吸筹。回放里 25 个吸筹信号有 22 个是这样来的
# 兜底名单：接口没返回资产类别时，按名字排除
NON_CRYPTO = {"AAOI", "CL", "CRCL", "INTC", "KORU", "MU", "MSTR", "NBIS", "NVDA", "QQQ", "SKHY", "SKHYNIX", "SNDK",
              "SNXX", "SOXL", "SOXS", "SPCX", "XAG", "XAU", "XPT", "XPD", "TSLA", "AAPL", "COIN", "SPY", "GLD", "SLV"}
REQUIRE_BINANCE = True          # 只看币安也有合约的币（需要 binance_perp_symbols.txt，没有会自动跳过这个过滤）
BINANCE_FILE = "binance_perp_symbols.txt"
BINANCE_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# 1) 吸筹/蓄势（1 小时线）
ACC_RANGE_MAX = 0.08            # 最近 24 小时最高-最低 <= 8%
ACC_ATR_RATIO = 0.65            # 最近 24 小时平均波幅 <= 此前 3 天平均波幅的 65%（波动收窄）
ACC_VOL_RATIO = 1.6             # 最近 12 小时成交额 >= 此前 2 天均值的 1.6 倍（成交放大）
ACC_CHG_MAX = 0.05              # 最近 24 小时净涨跌 <= 5%（价格还没走出去）

# 2) 插针洗盘（15 分钟线）
WICK_PCT = 0.03                 # 影线长度 >= 3%
WICK_BODY_MAX = 0.01            # 收盘与开盘相差 <= 1%（说明针尖已经被收回）
WICK_VOL_RATIO = 3.0            # 成交额 >= 最近 20 根均值的 3 倍

# 3) 高位出货（1 小时线）
DIST_PUMP_MIN = 0.25            # 最近 24 小时内，从低点到高点涨过 25%
DIST_NEAR_PEAK = 0.06           # 现价离高点不超过 6%
DIST_VOL_RATIO = 2.5            # 最近 6 小时成交额 >= 此前 2 天均值的 2.5 倍
DIST_WICK_BARS = 3              # 最近 6 根里，至少这么多根上影线占整根波幅 40% 以上
DIST_PROGRESS_MAX = 0.03        # 最近 6 小时净涨幅 <= 3%（放量却涨不动）

# 4) 放量不涨（1 小时线）
WASH_VOL_RATIO = 5.0            # 最近 4 小时成交额 >= 此前 2 天均值的 5 倍
WASH_MOVE_MAX = 0.006           # 最近 4 小时净涨跌 <= 0.6%
WASH_RANGE_MAX = 0.015          # 最近 4 小时最高-最低 <= 1.5%

# 5) 持仓量激增
OI_SURGE_1H = 0.10              # 持仓量 1 小时增加 >= 10%
OI_SURGE_4H = 0.25              # 或 4 小时增加 >= 25%
OI_PRICE_FLAT = 0.03            # 同期价格涨跌 <= 3%（价格没动，持仓量却大增）
OI_SAMPLE_MIN = 30              # 持仓量快照的记录间隔（分钟）

# 要推送哪几种线索：跑完 whale_replay.py 后，把"没有明显好于随机基准"的特征从这里删掉
ENABLED_KINDS = {"accumulation", "wick_up", "distribution", "wash", "oi_surge"}
COOLDOWN_MIN = 360              # 同一个币同一种线索，6 小时内只推一次
MAX_PUSH = 6                    # 每轮最多推几个
FUNDING_CROWDED = 0.0003        # 资金费率绝对值超过 0.03% 视为一边拥挤
STATE_FILE = "whale_state.json"
DINGTALK_SECURITY_KEYWORD = "合约"   # 要和钉钉机器人"自定义关键词"一致
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
OI_URL = "https://www.okx.com/api/v5/public/open-interest"
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


def get_json(url, params=None, timeout=(5, 10)):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX 返回错误: {data}")
    return data["data"]


def get_klines(inst, bar, limit):
    rows = get_json(CANDLES_URL, {"instId": inst, "bar": bar, "limit": limit}, (5, 8))
    return [k for k in reversed(rows) if k[8] == "1"]


def get_funding(inst):
    try:
        d = get_json(FUNDING_URL, {"instId": inst}, (5, 8))
        return float(d[0]["fundingRate"]) if d else None
    except Exception:
        return None


def push_to_dingtalk(title, content):
    webhook = os.environ.get("DINGTALK_WEBHOOK_WHALE") or os.environ.get("DINGTALK_WEBHOOK")
    if webhook:
        print("发送到：" + ("专用群（DINGTALK_WEBHOOK_WHALE）" if os.environ.get("DINGTALK_WEBHOOK_WHALE") else "默认群（DINGTALK_WEBHOOK）"))
    else:
        print("未设置 DINGTALK_WEBHOOK_WHALE / DINGTALK_WEBHOOK，仅打印：")
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
    return {"last_alert": {}, "oi": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))


def _norm_coin(base):
    b = base.upper()
    for p_ in ("1000000", "100000", "10000", "1000"):
        if b.startswith(p_) and len(b) > len(p_):
            return b[len(p_):]
    return b


def _bases_from_info(info):
    return sorted({x["baseAsset"] for x in info["symbols"]
                   if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT" and x.get("status") == "TRADING"})


def load_binance_coins():
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


def non_crypto_insts():
    """非加密合约的 instId 集合：优先看接口返回的资产类别（instCategory：1 加密，其他是股票/商品/外汇等），再加兜底名单"""
    out = set()
    try:
        for x in get_json(INSTRUMENTS_URL, {"instType": "SWAP"}, (5, 15)):
            cat = str(x.get("instCategory") or "")
            if cat and cat != "1":
                out.add(x["instId"])
    except Exception as e:
        print(f"资产类别获取失败（用兜底名单排除）: {e}", file=sys.stderr)
    return out


def list_universe():
    skip = non_crypto_insts() if EXCLUDE_NON_CRYPTO else set()
    rows = []
    for t in get_json(TICKERS_URL, {"instType": "SWAP"}, (5, 15)):
        inst = t["instId"]
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            last, open24 = float(t["last"]), float(t["open24h"])
            turnover = float(t["volCcy24h"]) * last
        except (ValueError, KeyError, TypeError):
            continue
        if EXCLUDE_NON_CRYPTO and (inst in skip or inst.split("-")[0] in NON_CRYPTO):
            continue
        if open24 > 0 and MIN_TURNOVER_USDT <= turnover <= MAX_TURNOVER_USDT:
            rows.append({"coin": inst.split("-")[0], "inst": inst, "last": last, "chg": last / open24 - 1, "turnover": turnover})
    if REQUIRE_BINANCE:
        bn = load_binance_coins()
        if bn is None:
            print("警告：拿不到币安合约列表，本轮不做'币安也有'过滤", file=sys.stderr)
        else:
            before = len(rows)
            rows = [r for r in rows if _norm_coin(r["coin"]) in bn]
            print(f"币安也有的币: {len(rows)}/{before}")
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return rows[:TOP_N]


def ma(values, n, end):
    if end < 0 or end + 1 < n:
        return None
    return sum(values[end - n + 1:end + 1]) / n


def _arr(ks):
    o = [float(k[1]) for k in ks]
    h = [float(k[2]) for k in ks]
    l = [float(k[3]) for k in ks]
    c = [float(k[4]) for k in ks]
    q = [float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in ks]
    return o, h, l, c, q


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


# ---------- 五类特征（每个函数只看K线，也被 whale_replay.py 用来在历史上回放）----------
def detect_accumulation(ks):
    """1 小时线：波动收窄 + 成交放大 + 价格没走出去。lean: long / short / None"""
    if len(ks) < 120:
        return None
    o, h, l, c, q = _arr(ks)
    rng = (max(h[-24:]) - min(l[-24:])) / _mean(c[-24:])
    if rng > ACC_RANGE_MAX:
        return None
    atr_now = _mean([(h[j] - l[j]) / c[j] for j in range(-24, 0)])
    atr_before = _mean([(h[j] - l[j]) / c[j] for j in range(-96, -24)])
    if atr_before <= 0 or atr_now / atr_before > ACC_ATR_RATIO:
        return None
    vol_ratio = _mean(q[-12:]) / max(_mean(q[-60:-12]), 1e-9)
    if vol_ratio < ACC_VOL_RATIO:
        return None
    chg24 = c[-1] / c[-25] - 1
    if abs(chg24) > ACC_CHG_MAX:
        return None
    lows = [min(l[-24:-16]), min(l[-16:-8]), min(l[-8:])]
    highs = [max(h[-24:-16]), max(h[-16:-8]), max(h[-8:])]
    m99 = ma(c, 99, len(c) - 1)
    lean, why = None, "方向待定（价格还在区间里）"
    if lows[0] < lows[1] < lows[2] and m99 and c[-1] > m99:
        lean, why = "long", "低点逐段抬高，并且站在 MA99 上方"
    elif highs[0] > highs[1] > highs[2] and m99 and c[-1] < m99:
        lean, why = "short", "高点逐段降低，并且在 MA99 下方"
    return {"kind": "accumulation", "lean": lean, "why": why, "range": rng, "atr_ratio": atr_now / atr_before,
            "vol_ratio": vol_ratio, "chg24": chg24, "price": c[-1], "ts": ks[-1][0]}


def detect_wick(ks):
    """15 分钟线：最新一根是放量长影针，且针尖已被收回。lean: 下影针偏多，上影针偏空"""
    if len(ks) < 25:
        return None
    o, h, l, c, q = _arr(ks)
    base = _mean(q[-21:-1])
    if base <= 0 or q[-1] / base < WICK_VOL_RATIO:
        return None
    lower = (min(o[-1], c[-1]) - l[-1]) / o[-1]
    upper = (h[-1] - max(o[-1], c[-1])) / o[-1]
    if lower >= WICK_PCT and (o[-1] - c[-1]) / o[-1] <= WICK_BODY_MAX:
        return {"kind": "wick_down", "lean": "long", "why": "长下影针被收回，常见于扫止损后的洗盘", "wick": lower,
                "vol_ratio": q[-1] / base, "price": c[-1], "ts": ks[-1][0], "tip": l[-1]}
    if upper >= WICK_PCT and (c[-1] - o[-1]) / o[-1] <= WICK_BODY_MAX:
        return {"kind": "wick_up", "lean": "short", "why": "冲高后被打回的长上影针，常见于诱多或出货", "wick": upper,
                "vol_ratio": q[-1] / base, "price": c[-1], "ts": ks[-1][0], "tip": h[-1]}
    return None


def detect_distribution(ks):
    """1 小时线：24 小时内暴涨后，高位放量滞涨、连续长上影"""
    if len(ks) < 80:
        return None
    o, h, l, c, q = _arr(ks)
    low24, high24 = min(l[-24:]), max(h[-24:])
    if low24 <= 0 or (high24 - low24) / low24 < DIST_PUMP_MIN or (high24 - c[-1]) / high24 > DIST_NEAR_PEAK:
        return None
    vol_ratio = _mean(q[-6:]) / max(_mean(q[-54:-6]), 1e-9)
    if vol_ratio < DIST_VOL_RATIO:
        return None
    wicks = sum(1 for j in range(-6, 0) if h[j] > l[j] and (h[j] - max(o[j], c[j])) / (h[j] - l[j]) >= 0.4)
    progress = c[-1] / c[-7] - 1
    if wicks < DIST_WICK_BARS or progress > DIST_PROGRESS_MAX:
        return None
    return {"kind": "distribution", "lean": "short", "why": "暴涨后高位放量却涨不动，上影线密集，像在派发筹码",
            "pump": (high24 - low24) / low24, "vol_ratio": vol_ratio, "wicks": wicks, "progress": progress,
            "price": c[-1], "ts": ks[-1][0]}


def detect_wash(ks):
    """1 小时线：成交额暴增，价格几乎不动"""
    if len(ks) < 60:
        return None
    o, h, l, c, q = _arr(ks)
    vol_ratio = _mean(q[-4:]) / max(_mean(q[-52:-4]), 1e-9)
    if vol_ratio < WASH_VOL_RATIO:
        return None
    move = abs(c[-1] / c[-5] - 1)
    rng = (max(h[-4:]) - min(l[-4:])) / c[-1]
    if move > WASH_MOVE_MAX or rng > WASH_RANGE_MAX:
        return None
    return {"kind": "wash", "lean": None, "why": "大量换手但价格不动，可能是对倒、吸筹，或出货前的换手，方向不明",
            "vol_ratio": vol_ratio, "move": move, "price": c[-1], "ts": ks[-1][0]}


# ---------- 持仓量快照 ----------
def update_oi(state, universe, now_ms):
    """取全部合约当前持仓量，写入快照（每 OI_SAMPLE_MIN 分钟一份），返回 {inst: 持仓量(美元)}"""
    cur = {}
    try:
        for x in get_json(OI_URL, {"instType": "SWAP"}, (5, 15)):
            try:
                cur[x["instId"]] = float(x["oiUsd"]) if x.get("oiUsd") not in (None, "") else None
            except ValueError:
                pass
    except Exception as e:
        print(f"持仓量获取失败: {e}", file=sys.stderr)
        return {}
    last_px = {u["inst"]: u["last"] for u in universe}
    for inst, px in last_px.items():
        oi = cur.get(inst)
        if oi is None:
            continue
        hist = state["oi"].setdefault(inst, [])
        if not hist or now_ms - hist[-1][0] >= OI_SAMPLE_MIN * 60 * 1000 - 60000:
            hist.append([now_ms, oi, px])
        state["oi"][inst] = [s for s in hist if now_ms - s[0] <= 30 * 3600 * 1000]
    return {i: cur[i] for i in last_px if cur.get(i) is not None}


def _sample_ago(hist, now_ms, minutes, tol):
    best, gap = None, None
    for s in hist:
        g = abs((now_ms - s[0]) / 60000 - minutes)
        if g <= tol and (gap is None or g < gap):
            best, gap = s, g
    return best


def detect_oi_surge(inst, cur_oi, price, state, now_ms):
    hist = state["oi"].get(inst, [])
    for minutes, thr, tol, label in ((60, OI_SURGE_1H, 20, "1小时"), (240, OI_SURGE_4H, 40, "4小时")):
        s = _sample_ago(hist, now_ms, minutes, tol)
        if not s or s[1] <= 0:
            continue
        chg = cur_oi / s[1] - 1
        pchg = price / s[2] - 1
        if chg >= thr and abs(pchg) <= OI_PRICE_FLAT:
            return {"kind": "oi_surge", "lean": None, "why": "持仓量大增而价格没动，杠杆在堆积，常见于变盘前夜",
                    "window": label, "oi_chg": chg, "price_chg": pchg, "oi": cur_oi, "price": price, "ts": str(now_ms)}
    return None


# ---------- 消息 ----------
KIND_NAME = {
    "accumulation": ("🧲 吸筹/蓄势", "吸筹阶段（可能在为拉升或下砸做准备）"),
    "wick_down": ("📌 插针洗盘（下影）", "洗盘阶段（扫止损）"),
    "wick_up": ("📌 插针洗盘（上影）", "诱多/出货阶段"),
    "distribution": ("📤 高位出货", "出货阶段"),
    "wash": ("🔁 放量不涨", "换手阶段（对倒/吸筹/出货前）"),
    "oi_surge": ("📈 持仓量激增", "建仓/杠杆堆积阶段"),
}


def describe(r):
    k = r["kind"]
    if k == "accumulation":
        return (f"最近24小时波动只有 {r['range']*100:.1f}%，波动收窄到此前的 {r['atr_ratio']*100:.0f}%，"
                f"成交额放大到此前的 {r['vol_ratio']:.1f} 倍，价格几乎没走（24h {r['chg24']*100:+.1f}%）")
    if k in ("wick_down", "wick_up"):
        return f"15分钟线出现 {r['wick']*100:.1f}% 的{'下' if k == 'wick_down' else '上'}影针（针尖 {fmt_px(r['tip'])}），成交额是此前均值的 {r['vol_ratio']:.1f} 倍，已收回"
    if k == "distribution":
        return (f"24小时内先涨了 {r['pump']*100:.0f}%，现在在高位附近放量（{r['vol_ratio']:.1f} 倍）却涨不动"
                f"（近6小时 {r['progress']*100:+.1f}%），最近6根里有 {r['wicks']} 根长上影")
    if k == "wash":
        return f"最近4小时成交额是此前均值的 {r['vol_ratio']:.1f} 倍，价格只动了 {r['move']*100:.2f}%"
    if k == "oi_surge":
        return f"持仓量{r['window']}内增加 {r['oi_chg']*100:.0f}%（现 {r['oi']/1e6:.1f}M 美元），同期价格只动了 {r['price_chg']*100:+.1f}%"
    return ""


def lean_text(r, funding):
    lean, why = r.get("lean"), r["why"]
    extra = ""
    if funding is not None and abs(funding) >= FUNDING_CROWDED:
        if funding > 0:
            extra = f"；资金费率 {funding*100:+.3f}%，多头拥挤，下砸/洗盘的风险偏大"
            lean = lean or "short"
        else:
            extra = f"；资金费率 {funding*100:+.3f}%，空头拥挤，被逼空拉升的可能偏大"
            lean = lean or "long"
    tag = {"long": "偏多", "short": "偏空", None: "方向待定"}[lean]
    return f"倾向：{tag}（{why}{extra}）"


def fmt_item(item, r, funding, now_ms):
    title, stage = KIND_NAME[r["kind"]]
    lines = [
        f"{title}  {item['inst'].replace('-SWAP', '')}",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        describe(r),
        lean_text(r, funding),
        f"现价 {fmt_px(r['price'])}，24h {item['chg']*100:+.1f}%，24h成交额 {item['turnover']/1e6:.0f}M USDT"
        + (f"，资金费率 {funding*100:+.3f}%" if funding is not None else ""),
        f"阶段判断：更像「{stage}」（经验推断，不是定论）",
    ]
    return "\n".join(lines)


def main():
    now_ms = int(time.time() * 1000)
    if "--test" in sys.argv:
        push_to_dingtalk("合约主力动向（测试消息）",
                         f"时间 {fmt_time(now_ms)}（北京时间）\n这是一条测试消息，用来确认钉钉群的 Webhook 配置正确。\n"
                         "之后发现吸筹、插针洗盘、高位出货、放量不涨、持仓量激增这些主力痕迹，会在这里推送线索。")
        return

    state = load_state()
    state.setdefault("last_alert", {})
    state.setdefault("oi", {})
    print(f"读取状态：{len(state['last_alert'])} 条冷却记录，{len(state['oi'])} 个币的持仓量快照")

    universe = list_universe()
    print(f"扫描 {len(universe)} 个币")
    oi_now = update_oi(state, universe, now_ms)

    def fetch(item):
        try:
            k1h = get_klines(item["inst"], "1H", 130)
            time.sleep(0.2)
            k15 = get_klines(item["inst"], "15m", 50)
            return item, k1h, k15
        except Exception as e:
            print(f"{item['inst']} 取K线失败: {e}", file=sys.stderr)
            return item, None, None
        finally:
            time.sleep(0.2)

    with ThreadPoolExecutor(max_workers=3) as ex:
        fetched = list(ex.map(fetch, universe))

    hits = []
    for item, k1h, k15 in fetched:
        if not k1h:
            continue
        found = [detect_accumulation(k1h), detect_distribution(k1h), detect_wash(k1h)]
        if k15:
            found.append(detect_wick(k15))
        if item["inst"] in oi_now:
            found.append(detect_oi_surge(item["inst"], oi_now[item["inst"]], item["last"], state, now_ms))
        for r in found:
            if not r or r["kind"] not in ENABLED_KINDS:
                continue
            key = f"{item['inst']}|{r['kind']}"
            last = state["last_alert"].get(key)
            if last and now_ms - int(last) < COOLDOWN_MIN * 60 * 1000:
                continue
            hits.append((item, r, key))

    print("命中：" + "，".join(f"{k}={sum(1 for _, r, _ in hits if r['kind'] == k)}" for k in KIND_NAME))
    # 优先推"有明确倾向"和成交额放大的；同一个币只推一条最强的线索
    best = {}
    for item, r, key in hits:
        score = (2 if r.get("lean") else 1, r.get("vol_ratio", r.get("oi_chg", 0)))
        if item["inst"] not in best or score > best[item["inst"]][0]:
            best[item["inst"]] = (score, item, r, key)
    chosen = sorted(best.values(), key=lambda x: x[0], reverse=True)[:MAX_PUSH]

    parts = []
    for _, item, r, key in chosen:
        funding = get_funding(item["inst"])
        time.sleep(0.1)
        parts.append(fmt_item(item, r, funding, now_ms))
        state["last_alert"][key] = str(now_ms)
    if parts:
        parts.append("提示：以上是量价特征的经验规则，假信号不少，小币种流动性差，请控制仓位并设好止损。")
        push_to_dingtalk("合约主力动向线索", "\n\n———\n\n".join(parts))
    else:
        print("本次没有发现主力痕迹。")
    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now_ms - int(v) < 24 * 3600 * 1000}
    save_state(state)


if __name__ == "__main__":
    main()
