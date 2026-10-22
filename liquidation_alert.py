#!/usr/bin/env python3
"""
OKX 永续合约 大额爆仓提醒

每次运行：
  1) 取成交额靠前的币（含 BTC、ETH），从 OKX 公开接口读取各自最近的爆仓单
  2) 统计最近 WINDOW_MIN 分钟内：多头爆仓金额、空头爆仓金额、笔数、最大单笔
  3) 达到阈值就推送到钉钉；消息里附上：
       - 近 1 小时价格涨跌、现价、资金费率
       - 同期新闻标题（CoinDesk / Cointelegraph RSS，近 NEWS_MAX_AGE_HOURS 小时）
       - 全市场爆仓总额（达到 MARKET_TOTAL_USD 时会额外提示）

关于"原因"：脚本只能提供线索（价格怎么走的、哪边被爆、同期有什么新闻），
不能判断真正原因。新闻标题只是时间上相近，不代表因果，请自己再确认。

爆仓方向：
  多头爆仓 = 多单被强平（卖出平仓），通常发生在价格快速下跌时
  空头爆仓 = 空单被强平（买入平仓），通常发生在价格快速上涨时

独立文件，不依赖其他脚本。环境变量 DINGTALK_WEBHOOK_LIQ（或 DINGTALK_WEBHOOK）不设置时只打印不推送。
"""

import datetime
import email.utils
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

import requests

# ========== 可调参数 ==========
WINDOW_MIN = 15                 # 统计最近多少分钟的爆仓（比触发间隔 5 分钟长，防止漏掉）
TOP_N = 40                      # 只看成交额前 N 的币
MIN_TURNOVER_USDT = 20_000_000  # 24h成交额下限

# BTC、ETH 体量大，阈值单独设高一点
MAJOR_COINS = {"BTC", "ETH"}
MAJOR_SINGLE_USD = 1_000_000    # 单笔爆仓 >= 100 万美元
MAJOR_TOTAL_USD = 5_000_000     # 某一方向 15 分钟累计 >= 500 万美元
ALT_SINGLE_USD = 200_000        # 其他币：单笔 >= 20 万
ALT_TOTAL_USD = 1_000_000       # 其他币：某一方向累计 >= 100 万

MARKET_TOTAL_USD = 20_000_000   # 全市场（扫描范围内）15 分钟爆仓合计 >= 2000 万，消息顶部加"全市场"提示
MARKET_COOLDOWN_MIN = 30        # 全市场提示的冷却时间
MAX_PUSH = 5                    # 每次最多推几个币

NEWS_ENABLED = True
NEWS_MAX_AGE_HOURS = 3          # 只看最近 3 小时的新闻
NEWS_MAX_SHOW = 3               # 每个币最多附几条新闻
NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]
TRANSLATE_NEWS = True           # 尝试把标题翻译成中文，失败就显示英文原文
STATE_FILE = "liq_state.json"
DINGTALK_SECURITY_KEYWORD = "合约"   # 要和钉钉机器人"自定义关键词"一致
# ==============================

TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
LIQ_URL = "https://www.okx.com/api/v5/public/liquidation-orders"
CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"

COIN_KEYWORDS = {                # 新闻标题里和币相关的词（小写），没列出的币用币名本身匹配
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether ", "eth"],
    "SOL": ["solana", "sol "],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
    "BNB": ["bnb", "binance"],
}
MACRO_KEYWORDS = ["fed ", "federal reserve", "fomc", "rate cut", "rate hike", "inflation", "cpi", "sec ", "etf",
                  "hack", "exploit", "tariff", "trump", "liquidat", "crash", "plunge", "surge", "sell-off",
                  "selloff", "war", "ban", "lawsuit", "bankrupt", "stablecoin", "treasury", "jobs report"]


# ---------- 基础工具 ----------
def get_json(url, params=None, timeout=(5, 10)):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX 返回错误: {data}")
    return data["data"]


def fmt_time(ms):
    dt = datetime.datetime.fromtimestamp(int(ms) / 1000, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M")


def fmt_px(x):
    """价格：大于等于 1 用 6 位有效数字，小于 1 显示成普通小数（不用科学计数法）"""
    return f"{x:.6g}" if x >= 1 else f"{x:.8f}".rstrip("0").rstrip(".")


def fmt_usd(x):
    if x >= 1e8:
        return f"{x/1e8:.2f}亿美元"
    if x >= 1e4:
        return f"{x/1e4:.0f}万美元"
    return f"{x:.0f}美元"


def push_to_dingtalk(title, content):
    webhook = os.environ.get("DINGTALK_WEBHOOK_LIQ") or os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        print("未设置 DINGTALK_WEBHOOK_LIQ / DINGTALK_WEBHOOK，仅打印：")
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
    return {"last_alert_ts": {}, "market_alert_at": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------- 数据 ----------
def list_coins():
    """成交额靠前的币 -> [(coin, last_price)]"""
    rows = []
    for t in get_json(TICKERS_URL, {"instType": "SWAP"}, (5, 15)):
        inst = t["instId"]
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"])
            turnover = float(t["volCcy24h"]) * last
        except (ValueError, KeyError):
            continue
        if turnover >= MIN_TURNOVER_USDT:
            rows.append((inst.split("-")[0], last, turnover))
    rows.sort(key=lambda r: r[2], reverse=True)
    return [(c, p) for c, p, _ in rows[:TOP_N]]


def contract_values():
    """每张合约对应多少个币：instId -> ctVal * ctMult"""
    out = {}
    for x in get_json(INSTRUMENTS_URL, {"instType": "SWAP"}, (5, 15)):
        try:
            out[x["instId"]] = float(x["ctVal"]) * float(x.get("ctMult") or 1)
        except (ValueError, KeyError):
            pass
    return out


def fetch_liquidations(coin, ctval):
    """最近的爆仓单 -> [(ts_ms, side, usd, price)]；side: 'long' 多头被爆 / 'short' 空头被爆"""
    out = []
    for blk in get_json(LIQ_URL, {"instType": "SWAP", "uly": f"{coin}-USDT", "state": "filled", "limit": "100"}):
        inst = blk.get("instId") or f"{coin}-USDT-SWAP"
        cv = ctval.get(inst)
        if not cv:
            continue
        for d in blk.get("details", []):
            try:
                px, sz = float(d["bkPx"]), float(d["sz"])
                # 卖出平仓 = 多头被强平；买入平仓 = 空头被强平
                out.append((int(d["ts"]), "long" if d["side"] == "sell" else "short", sz * cv * px, px))
            except (ValueError, KeyError):
                continue
    return out


def aggregate(orders, since_ms):
    agg = {"long": [0.0, 0, 0.0, 0.0], "short": [0.0, 0, 0.0, 0.0]}   # 金额, 笔数, 最大单笔, 最大单笔价格
    latest = 0
    for ts, side, usd, px in orders:
        if ts < since_ms:
            continue
        a = agg[side]
        a[0] += usd
        a[1] += 1
        if usd > a[2]:
            a[2], a[3] = usd, px
        latest = max(latest, ts)
    return agg, latest


def hour_move(coin):
    """近 1 小时涨跌幅（按 5 分钟线），取不到返回 None"""
    try:
        rows = get_json(CANDLES_URL, {"instId": f"{coin}-USDT-SWAP", "bar": "5m", "limit": "15"}, (5, 8))
        ks = [k for k in reversed(rows) if k[8] == "1"]
        return float(ks[-1][4]) / float(ks[-13][4]) - 1
    except Exception as e:
        print(f"{coin} 近1小时走势获取失败: {e}", file=sys.stderr)
        return None


def get_funding(coin):
    try:
        d = get_json(FUNDING_URL, {"instId": f"{coin}-USDT-SWAP"}, (5, 8))
        return float(d[0]["fundingRate"]) if d else None
    except Exception:
        return None


# ---------- 新闻 ----------
_news_cache = None


def load_news():
    """近 NEWS_MAX_AGE_HOURS 小时的新闻标题 -> [(age_hours, source, title)]，一次运行只取一次"""
    global _news_cache
    if _news_cache is not None:
        return _news_cache
    items = []
    now = datetime.datetime.fromtimestamp(time.time(), tz=datetime.timezone.utc)
    for source, url in NEWS_FEEDS:
        try:
            resp = requests.get(url, timeout=(5, 8), headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                pub = it.findtext("pubDate")
                if not title or not pub:
                    continue
                try:
                    dt = email.utils.parsedate_to_datetime(pub)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                except Exception:
                    continue
                age = (now - dt).total_seconds() / 3600
                if 0 <= age <= NEWS_MAX_AGE_HOURS:
                    items.append((age, source, title))
        except Exception as e:
            print(f"新闻源 {source} 获取失败: {e}", file=sys.stderr)
    items.sort(key=lambda x: x[0])
    _news_cache = items
    return items


def translate(text):
    if not TRANSLATE_NEWS:
        return text
    try:
        resp = requests.get("https://translate.googleapis.com/translate_a/single",
                            params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}, timeout=(3, 5))
        return "".join(seg[0] for seg in resp.json()[0] if seg and seg[0]) or text
    except Exception:
        return text


def related_news(coin):
    """挑出和这个币相关的、或宏观/监管类的新闻标题（时间相近，不代表因果）"""
    items = load_news()
    coin_kw = COIN_KEYWORDS.get(coin, [coin.lower()])
    picked = []
    for age, source, title in items:
        low = f" {title.lower()} "
        if any(k in low for k in coin_kw):
            picked.append((0, age, source, title))
        elif any(k in low for k in MACRO_KEYWORDS):
            picked.append((1, age, source, title))
    picked.sort(key=lambda x: (x[0], x[1]))
    return picked[:NEWS_MAX_SHOW]


# ---------- 消息 ----------
def fmt_coin(coin, price, agg, move, funding):
    lines = [f"🔥 {coin}-USDT  现价 {fmt_px(price)}"
             + (f"，近1小时 {move*100:+.1f}%" if move is not None else "")
             + (f"，资金费率 {funding*100:+.3f}%" if funding is not None else "")]
    for side, label, hint in (("long", "多头爆仓", "多单被强平，多见于快速下跌"),
                              ("short", "空头爆仓", "空单被强平，多见于快速上涨")):
        usd, n, big, big_px = agg[side]
        if n:
            lines.append(f"{label} {fmt_usd(usd)}（{n} 笔，最大单笔 {fmt_usd(big)} @ {fmt_px(big_px)}）— {hint}")
    if NEWS_ENABLED:
        news = related_news(coin)
        if news:
            lines.append(f"同期新闻（近{NEWS_MAX_AGE_HOURS}小时，仅供参考，不一定是原因）：")
            for _, age, source, title in news:
                ago = f"{int(age*60)}分钟前" if age < 1 else f"{age:.1f}小时前"
                lines.append(f"· [{source} {ago}] {translate(title)}")
        else:
            lines.append(f"近{NEWS_MAX_AGE_HOURS}小时没有明显相关的新闻，可能是纯盘面/杠杆行情。")
    return "\n".join(lines)


def main():
    state = load_state()
    state.setdefault("last_alert_ts", {})
    now_ms = int(time.time() * 1000)
    since_window = now_ms - WINDOW_MIN * 60 * 1000

    coins = list_coins()
    ctval = contract_values()
    print(f"扫描 {len(coins)} 个币的爆仓单（最近 {WINDOW_MIN} 分钟）")

    hits, market_total = [], 0.0
    market_long = market_short = 0.0
    for coin, price in coins:
        try:
            orders = fetch_liquidations(coin, ctval)
        except Exception as e:
            print(f"{coin} 爆仓单获取失败: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue
        time.sleep(0.45)    # OKX 该接口限频约 5 次/2 秒

        since = max(since_window, int(state["last_alert_ts"].get(coin, 0)) + 1)   # 已经推过的那批不再算
        agg, latest = aggregate(orders, since)
        market_long += agg["long"][0]
        market_short += agg["short"][0]
        market_total += agg["long"][0] + agg["short"][0]

        single_min, total_min = (MAJOR_SINGLE_USD, MAJOR_TOTAL_USD) if coin in MAJOR_COINS else (ALT_SINGLE_USD, ALT_TOTAL_USD)
        biggest = max(agg["long"][2], agg["short"][2])
        biggest_side_total = max(agg["long"][0], agg["short"][0])
        if biggest >= single_min or biggest_side_total >= total_min:
            hits.append((coin, price, agg, latest, biggest_side_total))

    print(f"全市场爆仓合计 {fmt_usd(market_total)}（多头 {fmt_usd(market_long)}，空头 {fmt_usd(market_short)}），命中 {len(hits)} 个币")

    market_hit = (market_total >= MARKET_TOTAL_USD
                  and now_ms - int(state.get("market_alert_at", 0)) >= MARKET_COOLDOWN_MIN * 60 * 1000)
    if not hits and not market_hit:
        print("本次没有达到阈值的爆仓。")
        save_state(state)
        return

    hits.sort(key=lambda h: h[4], reverse=True)
    parts = []
    if market_hit:
        side_txt = "多头占主导（下跌行情）" if market_long > market_short * 1.5 else \
                   "空头占主导（上涨行情）" if market_short > market_long * 1.5 else "多空都有"
        parts.append(f"⚠️ 全市场近{WINDOW_MIN}分钟爆仓 {fmt_usd(market_total)}：多头 {fmt_usd(market_long)} / 空头 {fmt_usd(market_short)}，{side_txt}")
        state["market_alert_at"] = now_ms
    for coin, price, agg, latest, _ in hits[:MAX_PUSH]:
        parts.append(fmt_coin(coin, price, agg, hour_move(coin), get_funding(coin)))
        time.sleep(0.2)
        state["last_alert_ts"][coin] = latest

    header = f"时间 {fmt_time(now_ms)}（北京时间）\n"
    push_to_dingtalk("合约爆仓提醒", header + "\n\n———\n\n".join(parts))

    # 清理超过 1 天的记录
    state["last_alert_ts"] = {k: v for k, v in state["last_alert_ts"].items() if now_ms - int(v) < 24 * 3600 * 1000}
    save_state(state)


if __name__ == "__main__":
    main()
