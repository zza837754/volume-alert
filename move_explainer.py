#!/usr/bin/env python3
"""
OKX 永续合约 异动归因：价格大涨大跌时，告诉你"可能是为什么"

每次运行：
  1) 用 5 分钟线扫描成交额靠前的币（含 BTC、ETH），发现异动：
       BTC/ETH：近 15 分钟涨跌 >= 1%，或近 1 小时 >= 2%
       其他币  ：近 15 分钟涨跌 >= 3%，或近 1 小时 >= 6%，并且放量
  2) 对每个异动，收集"原因线索"：
       - 大盘：BTC 近 1 小时涨跌、全市场有多大比例的币同向（是大盘联动，还是个币独立行情）
       - 新闻：CoinDesk / Cointelegraph / Decrypt / The Block 等 RSS 中，近几个小时里提到这个币的标题
       - 交易所公告：OKX（以及能访问时的币安）最近的上币、下架、合约上线等公告
       - 爆仓：这个币最近 20 分钟的多头/空头爆仓金额
  3) 综合给出一句"推测"。这只是线索归纳，不是定论：很多行情没有公开原因，找不到时会直接写明。

独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK_MOVE（或 DINGTALK_WEBHOOK）不设置时只打印不推送。
手动测试推送通道：python move_explainer.py --test
"""

import datetime
import email.utils
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

# ========== 可调参数 ==========
TOP_N = 150                     # 只看成交额前 N 的币
MIN_TURNOVER_USDT = 10_000_000  # 24h成交额下限
MAJORS = {"BTC", "ETH"}

MAJOR_MOVE_15M = 0.010          # BTC/ETH：近 15 分钟涨跌 >= 1%
MAJOR_MOVE_60M = 0.020          # 或近 1 小时 >= 2%
ALT_MOVE_15M = 0.030            # 其他币：近 15 分钟 >= 3%
ALT_MOVE_60M = 0.060            # 或近 1 小时 >= 6%
ALT_VOL_RATIO = 2.0             # 其他币还要求：最近 15 分钟成交额 >= 此前均值的 2 倍

BREADTH_MOVE = 0.01             # 统计"同向"：近 1 小时涨跌超过 1% 的币
BREADTH_MIN = 0.50              # 一半以上的币同向，就算大盘联动
MARKET_LIQ_BIG_USD = 20_000_000 # 全市场爆仓合计超过这个数，推测里会提到"爆仓连锁"

COOLDOWN_MIN = 120              # 同一个币同一方向，多久内不重复推
MARKET_COOLDOWN_MIN = 90        # 大盘异动的冷却
MAX_ALT_PUSH = 4                # 每轮最多推几个个币异动

NEWS_MAX_AGE_HOURS = 6          # 只看最近 6 小时的新闻/公告
NEWS_MAX_SHOW = 4               # 每条异动最多附几条线索
TRANSLATE = True                # 尝试把英文标题翻译成中文，失败就显示原文
LIQ_WINDOW_MIN = 20             # 统计最近多少分钟的爆仓

NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("TheBlock", "https://www.theblock.co/rss.xml"),
    ("BitcoinMagazine", "https://bitcoinmagazine.com/.rss/full/"),
]
OKX_ANN_URL = "https://www.okx.com/api/v5/support/announcements"
BINANCE_ANN_URLS = [
    ("币安公告-上币", "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=48&pageNo=1&pageSize=20"),
    ("币安公告-下架", "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=161&pageNo=1&pageSize=20"),
]
STATE_FILE = "move_state.json"
DINGTALK_SECURITY_KEYWORD = "合约"   # 要和钉钉机器人"自定义关键词"一致
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
LIQ_URL = "https://www.okx.com/api/v5/public/liquidation-orders"
FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"

# 常见币的名字（小写），用来在新闻标题里认出是哪个币；没列出的币只按大写代码精确匹配（如 "APT"）
ALIASES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "XRP": ["ripple"],
    "BNB": ["bnb chain", "bnb"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "AVAX": ["avalanche"],
    "LINK": ["chainlink"], "DOT": ["polkadot"], "TRX": ["tron"], "TON": ["toncoin"], "SHIB": ["shiba inu"],
    "LTC": ["litecoin"], "BCH": ["bitcoin cash"], "UNI": ["uniswap"], "NEAR": ["near protocol"],
    "APT": ["aptos"], "ARB": ["arbitrum"], "OP": ["optimism"], "SUI": ["sui network"], "FIL": ["filecoin"],
    "ATOM": ["cosmos"], "INJ": ["injective"], "PEPE": ["pepe"], "WIF": ["dogwifhat"], "AAVE": ["aave"],
    "MKR": ["makerdao", "sky protocol"], "CRV": ["curve finance", "curve dao"], "LDO": ["lido"], "ENA": ["ethena"],
    "HYPE": ["hyperliquid"], "ONDO": ["ondo"], "TIA": ["celestia"], "SEI": ["sei network"], "JUP": ["jupiter"],
    "GNO": ["gnosis"], "STRK": ["starknet"], "PYTH": ["pyth"], "WLD": ["worldcoin"], "TAO": ["bittensor"],
    "ICP": ["internet computer"], "ETC": ["ethereum classic"], "XLM": ["stellar"], "HBAR": ["hedera"],
}
MACRO_KEYWORDS = ["fed ", "federal reserve", "fomc", "rate cut", "rate hike", "inflation", "cpi", "jobs report", "sec ",
                  "etf", "hack", "exploit", "breach", "tariff", "trump", "crash", "plunge", "surge", "sell-off",
                  "selloff", "liquidat", "war", "ban", "lawsuit", "bankrupt", "treasury", "stablecoin", "binance", "coinbase",
                  "regulat", "senate", "congress"]


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


def fmt_usd(x):
    if x >= 1e8:
        return f"{x/1e8:.2f}亿美元"
    if x >= 1e4:
        return f"{x/1e4:.0f}万美元"
    return f"{x:.0f}美元"


def get_json(url, params=None, timeout=(5, 10)):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX 返回错误: {data}")
    return data["data"]


def push_to_dingtalk(title, content):
    webhook = os.environ.get("DINGTALK_WEBHOOK_MOVE") or os.environ.get("DINGTALK_WEBHOOK")
    if webhook:
        print("发送到：" + ("专用群（DINGTALK_WEBHOOK_MOVE）" if os.environ.get("DINGTALK_WEBHOOK_MOVE") else "默认群（DINGTALK_WEBHOOK）"))
    else:
        print("未设置 DINGTALK_WEBHOOK_MOVE / DINGTALK_WEBHOOK，仅打印：")
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


def get_klines(inst, bar, limit):
    rows = get_json(CANDLES_URL, {"instId": inst, "bar": bar, "limit": limit}, (5, 8))
    return [k for k in reversed(rows) if k[8] == "1"]


def get_funding(inst):
    try:
        d = get_json(FUNDING_URL, {"instId": inst}, (5, 8))
        return float(d[0]["fundingRate"]) if d else None
    except Exception:
        return None


# ---------- 行情与异动 ----------
def list_universe():
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
        if open24 > 0 and turnover >= MIN_TURNOVER_USDT:
            rows.append({"coin": inst.split("-")[0], "inst": inst, "last": last, "chg": last / open24 - 1, "turnover": turnover})
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return rows[:TOP_N]


def analyze_moves(ks):
    """近 15 分钟/1 小时涨跌、放量倍数"""
    if len(ks) < 40:
        return None
    c = [float(k[4]) for k in ks]
    q = [float(k[7]) if float(k[7]) > 0 else float(k[6]) * float(k[4]) for k in ks]
    base = sum(q[-27:-3]) / 24 * 3
    return {"move15": c[-1] / c[-4] - 1, "move60": c[-1] / c[-13] - 1, "price": c[-1],
            "ratio": sum(q[-3:]) / base if base > 0 else 0.0}


def is_mover(coin, m):
    if coin in MAJORS:
        return abs(m["move15"]) >= MAJOR_MOVE_15M or abs(m["move60"]) >= MAJOR_MOVE_60M
    return (abs(m["move15"]) >= ALT_MOVE_15M or abs(m["move60"]) >= ALT_MOVE_60M) and m["ratio"] >= ALT_VOL_RATIO


def direction_of(m):
    return "up" if (m["move15"] if abs(m["move15"]) >= 0.5 * abs(m["move60"]) else m["move60"]) > 0 else "down"


# ---------- 线索：新闻、公告 ----------
_news_cache = None


def load_news():
    """近 NEWS_MAX_AGE_HOURS 小时的新闻标题和交易所公告 -> [(age_hours, source, title)]，一次运行只取一次"""
    global _news_cache
    if _news_cache is not None:
        return _news_cache
    items, now = [], time.time()

    for source, url in NEWS_FEEDS:
        try:
            resp = requests.get(url, timeout=(5, 8), headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            for it in ET.fromstring(resp.content).iter("item"):
                title, pub = (it.findtext("title") or "").strip(), it.findtext("pubDate")
                if not title or not pub:
                    continue
                try:
                    dt = email.utils.parsedate_to_datetime(pub)
                    ts = (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).timestamp()
                except Exception:
                    continue
                age = (now - ts) / 3600
                if 0 <= age <= NEWS_MAX_AGE_HOURS:
                    items.append((age, source, title))
        except Exception as e:
            print(f"新闻源 {source} 获取失败: {e}", file=sys.stderr)

    try:                                                         # OKX 公告：上币、下架、合约上线、代币互换等
        d = requests.get(OKX_ANN_URL, params={"page": "1"}, timeout=(5, 8)).json()
        for block in d.get("data", []):
            for it in block.get("details", []):
                age = (now - int(it["pTime"]) / 1000) / 3600
                if 0 <= age <= NEWS_MAX_AGE_HOURS:
                    items.append((age, "OKX公告", it["title"].strip()))
    except Exception as e:
        print(f"OKX 公告获取失败: {e}", file=sys.stderr)

    for source, url in BINANCE_ANN_URLS:                         # 币安公告：在 GitHub 的服务器上可能被拒绝，失败就跳过
        try:
            d = requests.get(url, timeout=(4, 8), headers={"User-Agent": "Mozilla/5.0"}).json()
            for cat in d["data"]["catalogs"]:
                for it in cat["articles"]:
                    age = (now - int(it["releaseDate"]) / 1000) / 3600
                    if 0 <= age <= NEWS_MAX_AGE_HOURS:
                        items.append((age, source, it["title"].strip()))
        except Exception as e:
            print(f"{source} 获取失败（可能是网络限制，已跳过）: {e}", file=sys.stderr)

    items.sort(key=lambda x: x[0])
    _news_cache = items
    return items


def match_coin(title, coin):
    low = title.lower()
    for name in ALIASES.get(coin, []):
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            return True
    return len(coin) >= 3 and re.search(r"(?<![A-Za-z0-9])" + re.escape(coin) + r"(?![A-Za-z0-9])", title) is not None


def translate(text):
    if not TRANSLATE:
        return text
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}, timeout=(3, 5))
        return "".join(seg[0] for seg in r.json()[0] if seg and seg[0]) or text
    except Exception:
        return text


def news_for_coin(coin):
    return [x for x in load_news() if match_coin(x[2], coin)][:NEWS_MAX_SHOW]


def macro_news():
    out = []
    for age, source, title in load_news():
        if any(k in f" {title.lower()} " for k in MACRO_KEYWORDS) or match_coin(title, "BTC") or match_coin(title, "ETH"):
            out.append((age, source, title))
    return out[:NEWS_MAX_SHOW]


def fmt_news(items):
    lines = []
    for age, source, title in items:
        ago = f"{int(age * 60)}分钟前" if age < 1 else f"{age:.1f}小时前"
        lines.append(f"· [{source} {ago}] {translate(title)}")
    return lines


# ---------- 线索：爆仓 ----------
_ctval = None


def liquidations(coin):
    """最近 LIQ_WINDOW_MIN 分钟的多头/空头爆仓金额（美元），取不到返回 None"""
    global _ctval
    try:
        if _ctval is None:
            _ctval = {x["instId"]: float(x["ctVal"]) * float(x.get("ctMult") or 1)
                      for x in get_json(INSTRUMENTS_URL, {"instType": "SWAP"}, (5, 15))}
        since = (time.time() - LIQ_WINDOW_MIN * 60) * 1000
        long_usd = short_usd = 0.0
        for blk in get_json(LIQ_URL, {"instType": "SWAP", "uly": f"{coin}-USDT", "state": "filled", "limit": "100"}):
            cv = _ctval.get(blk.get("instId") or f"{coin}-USDT-SWAP")
            if not cv:
                continue
            for d in blk.get("details", []):
                if int(d["ts"]) < since:
                    continue
                usd = float(d["sz"]) * cv * float(d["bkPx"])
                if d["side"] == "sell":
                    long_usd += usd
                else:
                    short_usd += usd
        return long_usd, short_usd
    except Exception as e:
        print(f"{coin} 爆仓数据获取失败: {e}", file=sys.stderr)
        return None


def liq_text(liq):
    if liq is None:
        return None
    lo, sh = liq
    if lo + sh < 50_000:
        return f"近{LIQ_WINDOW_MIN}分钟爆仓很少（不到 5 万美元），不是爆仓推动的"
    side = "多头被强平为主，对应下跌" if lo > sh * 1.5 else "空头被强平为主，对应上涨" if sh > lo * 1.5 else "多空都有"
    return f"近{LIQ_WINDOW_MIN}分钟爆仓：多头 {fmt_usd(lo)} / 空头 {fmt_usd(sh)}（{side}）"


# ---------- 消息 ----------
def breadth_of(moves):
    """全市场近 1 小时：涨超 1% 的占比、跌超 1% 的占比"""
    vals = [m["move60"] for m in moves.values()]
    n = max(len(vals), 1)
    return sum(1 for v in vals if v >= BREADTH_MOVE) / n, sum(1 for v in vals if v <= -BREADTH_MOVE) / n


def guess_market(direction, btc, breadth_dir, news, liq_total):
    up = direction == "up"
    parts = [f"大盘联动：BTC {'领涨' if up else '领跌'}（近1小时 {btc['move60']*100:+.1f}%），带动 {breadth_dir*100:.0f}% 的币同向{'上涨' if up else '下跌'}。"]
    if news:
        parts.append("同期有宏观/监管/安全/交易所类新闻，见上方线索，请先核实是否相关。")
    elif liq_total is not None and liq_total >= MARKET_LIQ_BIG_USD:
        parts.append(f"没有明显新闻，但爆仓合计 {fmt_usd(liq_total)}，更像杠杆连锁踩踏。")
    else:
        parts.append("近几个小时没有找到明显的新闻线索，可能是资金面/情绪面驱动，或者消息尚未被收录。")
    return "推测：" + "".join(parts)


def fmt_market(direction, btc, eth, breadth_up, breadth_down, movers, news, liq_lines, liq_total, now_ms):
    up = direction == "up"
    b = breadth_up if up else breadth_down
    lines = [
        f"🌍 大盘异动  {'📈 急涨' if up else '📉 急跌'}",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        f"BTC 近15分钟 {btc['move15']*100:+.1f}%，近1小时 {btc['move60']*100:+.1f}%，现价 {fmt_px(btc['price'])}",
    ]
    if eth:
        lines.append(f"ETH 近15分钟 {eth['move15']*100:+.1f}%，近1小时 {eth['move60']*100:+.1f}%")
    lines.append(f"全市场：近1小时 {breadth_up*100:.0f}% 的币涨超1%、{breadth_down*100:.0f}% 的币跌超1%")
    if movers:
        lines.append(("领涨：" if up else "领跌：") + "，".join(f"{c} {v*100:+.1f}%" for c, v in movers))
    lines += liq_lines
    if news:
        lines.append(f"可能相关的新闻/公告（近{NEWS_MAX_AGE_HOURS}小时，仅供参考，不一定是原因）：")
        lines += fmt_news(news)
    lines.append(guess_market(direction, btc, b, news, liq_total))
    return "\n".join(lines)


def fmt_alt(item, m, direction, btc, breadth_same, news, liq, funding, now_ms):
    up = direction == "up"
    lines = [
        f"{'🚀 个币急涨' if up else '💥 个币急跌'}  {item['inst'].replace('-SWAP', '')}",
        f"时间 {fmt_time(now_ms)}（北京时间）",
        f"近15分钟 {m['move15']*100:+.1f}%，近1小时 {m['move60']*100:+.1f}%，成交额是此前均值的 {m['ratio']:.1f} 倍",
        f"现价 {fmt_px(m['price'])}，24h {item['chg']*100:+.1f}%，24h成交额 {item['turnover']/1e6:.0f}M USDT"
        + (f"，资金费率 {funding*100:+.3f}%" if funding is not None else ""),
        f"大盘：BTC 近1小时 {btc['move60']*100:+.1f}%，全市场 {breadth_same*100:.0f}% 的币同向",
    ]
    lt = liq_text(liq)
    if lt:
        lines.append(lt)
    if news:
        lines.append(f"提到 {item['coin']} 的新闻/公告（近{NEWS_MAX_AGE_HOURS}小时，仅供参考）：")
        lines += fmt_news(news)
    same_dir_market = (btc["move60"] > 0.008 and up) or (btc["move60"] < -0.008 and not up)
    if news:
        guess = "个币消息驱动的可能性较大（见上方线索），请先核实消息真假、是否已被市场消化，再决定。"
    elif same_dir_market and breadth_same >= BREADTH_MIN:
        guess = "更像跟随大盘的联动行情，不是这个币自己的消息。"
    elif liq and liq[0] + liq[1] >= 500_000:
        guess = "没有找到新闻，爆仓金额偏大，更像杠杆连锁推动。"
    else:
        guess = "暂未找到公开原因：可能是资金/庄家行为，或消息还没有被新闻源收录。没有依据时不要追。"
    lines.append("推测：" + guess)
    return "\n".join(lines)


def main():
    now_ms = int(time.time() * 1000)
    if "--test" in sys.argv:
        push_to_dingtalk("合约异动归因（测试消息）",
                         f"时间 {fmt_time(now_ms)}（北京时间）\n这是一条测试消息，用来确认钉钉群的 Webhook 配置正确。\n"
                         "之后行情出现大涨大跌时，会在这里推送异动和原因线索。")
        return

    state = load_state()
    state.setdefault("last_alert", {})
    print(f"读取状态：{len(state['last_alert'])} 条冷却记录")

    universe = list_universe()
    print(f"扫描 {len(universe)} 个币")

    def fetch(item):
        try:
            return item, analyze_moves(get_klines(item["inst"], "5m", 40))
        except Exception as e:
            print(f"{item['inst']} 取K线失败: {e}", file=sys.stderr)
            return item, None
        finally:
            time.sleep(0.2)

    with ThreadPoolExecutor(max_workers=3) as ex:
        results = [(it, m) for it, m in ex.map(fetch, universe) if m]
    moves = {it["coin"]: m for it, m in results}
    by_coin = {it["coin"]: it for it, _ in results}
    if "BTC" not in moves:
        print("没有取到 BTC 行情，本轮结束")
        return
    btc, eth = moves["BTC"], moves.get("ETH")
    b_up, b_down = breadth_of(moves)
    print(f"BTC 近15分钟 {btc['move15']*100:+.2f}%、近1小时 {btc['move60']*100:+.2f}%；全市场涨 {b_up*100:.0f}% / 跌 {b_down*100:.0f}%")

    def cooling(key, minutes):
        last = state["last_alert"].get(key)
        return bool(last and now_ms - int(last) < minutes * 60 * 1000)

    parts = []

    # ① 大盘异动：BTC 触发阈值
    market_event = False
    if is_mover("BTC", btc):
        d = direction_of(btc)
        if not cooling(f"MARKET|{d}", MARKET_COOLDOWN_MIN):
            market_event = True
            ranked = sorted(((c, m["move60"]) for c, m in moves.items() if c not in ("BTC", "ETH")), key=lambda x: x[1], reverse=(d == "up"))[:3]
            liq_lines, liq_total = [], 0.0
            for c in ("BTC", "ETH"):
                lq = liquidations(c)
                time.sleep(0.45)
                if lq:
                    liq_total += lq[0] + lq[1]
                    liq_lines.append(f"{c} {liq_text(lq)}")
            parts.append(fmt_market(d, btc, eth, b_up, b_down, ranked, macro_news(), liq_lines, liq_total if liq_lines else None, now_ms))
            state["last_alert"][f"MARKET|{d}"] = str(now_ms)
        else:
            market_event = True      # 仍在大盘异动中，只是不重复推大盘消息；个币按"超额涨跌"来判断
    # ② 个币异动
    alt_hits = []
    for it, m in results:
        coin = it["coin"]
        if coin == "BTC" or not is_mover(coin, m):
            continue
        d = direction_of(m)
        if market_event and abs(m["move60"] - btc["move60"]) < 0.03:       # 大盘异动期间，只推明显强于/弱于大盘的个币
            continue
        if cooling(f"{it['inst']}|{d}", COOLDOWN_MIN):
            continue
        alt_hits.append((it, m, d))
    alt_hits.sort(key=lambda x: abs(x[1]["move15"]), reverse=True)
    print(f"个币异动：命中 {len(alt_hits)} 个")
    for it, m, d in alt_hits[:MAX_ALT_PUSH]:
        liq = liquidations(it["coin"])
        time.sleep(0.45)
        same = b_up if d == "up" else b_down
        parts.append(fmt_alt(it, m, d, btc, same, news_for_coin(it["coin"]), liq, get_funding(it["inst"]), now_ms))
        state["last_alert"][f"{it['inst']}|{d}"] = str(now_ms)

    if parts:
        push_to_dingtalk("合约异动归因", "\n\n———\n\n".join(parts))
    else:
        print("本次没有需要推送的异动。")
    state["last_alert"] = {k: v for k, v in state["last_alert"].items() if now_ms - int(v) < 24 * 3600 * 1000}
    save_state(state)


if __name__ == "__main__":
    main()
