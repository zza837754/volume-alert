#!/usr/bin/env python3
"""
OKX 永续合约 - 成交量达标监控脚本
逻辑：每根5分钟K线的成交量(按币的数量，比如多少个BTC/ETH)达到设定的阈值，
就通过 钉钉机器人 推送通知到手机。不用倍数比较，直接看绝对数量，逻辑更直接。

每次运行时，把上次检查之后新出现的所有已收盘K线都补扫一遍（不只是最新
一根）——因为 GitHub Actions 定时任务实际执行有延迟，如果只看最新一根，
两次运行之间出现又消失的达标K线会被直接跳过，永远检测不到。

数据源用 OKX 而不是币安：因为币安合约接口(fapi.binance.com)对美国地区IP
直接返回451拒绝访问，而 GitHub Actions 的服务器全部在美国机房，无法绕过。
OKX 的公开行情接口没有这个限制。

运行方式：由 GitHub Actions 定时触发（见 .github/workflows/volume_monitor.yml）
也可以在自己电脑上手动跑：python volume_alert.py
"""

import os
import sys
import re
import html
import datetime
import email.utils
import requests
import xml.etree.ElementTree as ET

# ========== 可自行修改的配置 ==========
# OKX 永续合约命名格式：币种-USDT-SWAP
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]   # 想监控的合约品种，可自行增删
INTERVAL = "5m"                     # K线周期：1m, 3m, 5m, 15m, 1H ...

# 绝对数量阈值：每根K线的成交量(按币的数量，不是USDT金额)达到这个数就推送
# 不同币价格差异大，所以每个品种分开设置
VOLUME_THRESHOLDS = {
    "BTC-USDT-SWAP": 5500,      # 5分钟内成交达到 5500 个 BTC 才推送
    "ETH-USDT-SWAP": 100000,    # 5分钟内成交达到 100000 个 ETH 才推送
}

NEWS_ENABLED = True                 # 是否开启币圈大事新闻推送
NEWS_STATE_FILE = "seen_news_ids.txt"  # 记录已推送过的新闻链接，避免重复推送
NEWS_MAX_PUSH_PER_RUN = 5           # 单次最多推送几条新闻，防止刷屏
NEWS_MAX_AGE_HOURS = 3              # 新鲜度过滤：新闻实际发布时间超过这个小时数就丢弃，不管是不是"没见过"
# 新闻来源：谷歌新闻的中文加密货币聚合订阅（谷歌服务很稳定，不需要注册/token，
# 本身就是从各大中文财经媒体汇总加密货币相关报道，缺点是国内需要翻墙才能打开）
# 加上 when:1d 限定谷歌只搜最近1天内的，减少旧新闻混进来的概率
NEWS_RSS_FEEDS = [
    "https://news.google.com/rss/search?q=%E5%8A%A0%E5%AF%86%E8%B4%A7%E5%B8%81+OR+%E6%AF%94%E7%89%B9%E5%B8%81+OR+%E5%8A%A0%E5%AF%86%E5%B8%82%E5%9C%BA+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
]
# =======================================

OKX_KLINES_URL = "https://www.okx.com/api/v5/market/candles"


def format_candle_time(ts_ms: str) -> str:
    """把OKX返回的毫秒时间戳(字符串)转成北京时间可读格式，方便核对是哪根K线触发的"""
    ts = int(ts_ms) / 1000
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M")


def get_klines(symbol: str, interval: str, limit: int):
    """从OKX合约公开接口获取K线数据，不需要API Key"""
    params = {"instId": symbol, "bar": interval, "limit": limit}
    resp = requests.get(OKX_KLINES_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX接口返回错误: {data}")
    # OKX 返回的K线是从新到旧排列，反转成从旧到新，跟原逻辑保持一致
    return list(reversed(data["data"]))


MAX_CANDLES_TO_SCAN = 30            # 一次最多往回补扫多少根新K线，防止长时间没跑积累太多
VOLUME_STATE_FILE = "seen_volume_ts.txt"  # 记录每个品种已经检查到哪一根K线了


def load_last_ts():
    if not os.path.exists(VOLUME_STATE_FILE):
        return {}
    result = {}
    with open(VOLUME_STATE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            symbol, ts = line.split(":", 1)
            result[symbol] = ts
    return result


def save_last_ts(mapping):
    with open(VOLUME_STATE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{s}:{t}" for s, t in mapping.items()))


def check_symbol(symbol: str, last_ts: str | None):
    """
    检查单个品种是否成交量达标。
    不再用"倍数"比较，改成简单直接的绝对数量判断：这根K线成交了多少个币，
    达到 VOLUME_THRESHOLDS 里设置的数量就推送。

    每次运行时，把上次检查之后新出现的所有K线都补扫一遍（不只是最新一根）——
    因为 GitHub Actions 定时任务实际执行有延迟，如果只看最新一根，两次运行
    之间出现又消失的放量K线会被直接跳过，永远检测不到。

    返回 (触发的报警消息列表, 日志文本, 这个品种最新已检查到的K线时间戳)
    """
    # OKX K线字段：[ts, o, h, l, c, vol(张数), volCcy(币本位数量), volCcyQuote(USDT计价), confirm]
    # confirm="1" 表示这根K线已收盘，"0" 表示还在进行中
    threshold = VOLUME_THRESHOLDS.get(symbol)
    if threshold is None:
        return [], f"{symbol}: 未设置阈值，跳过", last_ts

    fetch_limit = MAX_CANDLES_TO_SCAN + 2
    klines = get_klines(symbol, INTERVAL, fetch_limit)
    closed_klines = [k for k in klines if k[8] == "1"]  # 只保留已收盘的K线，从旧到新排列

    if not closed_klines:
        return [], f"{symbol}: 数据不足，跳过", last_ts

    if last_ts is None:
        # 第一次运行：只检查最新一根，不往回补扫历史（避免把老早以前的数据当成新事件推送）
        indices_to_check = [len(closed_klines) - 1]
    else:
        indices_to_check = [i for i, k in enumerate(closed_klines) if k[0] > last_ts]

    coin_symbol = symbol.split("-")[0]   # 比如 "BTC-USDT-SWAP" -> "BTC"

    alerts = []
    log_lines = []
    for i in indices_to_check:
        current = closed_klines[i]
        current_vol_coin = float(current[6])   # 币本位数量(比如多少个ETH/BTC)
        current_vol_usdt = float(current[7])   # 对应的USDT成交额，推送里附带展示

        if current_vol_coin >= threshold:
            price = float(current[4])
            candle_time = format_candle_time(current[0])
            msg = (
                f"🚨 {symbol} 5分钟成交量达标！\n"
                f"K线时间: {candle_time}（北京时间）\n"
                f"成交量: {current_vol_coin:,.1f} {coin_symbol}（阈值 {threshold:,}）\n"
                f"成交额: {current_vol_usdt:,.0f} USDT\n"
                f"最新价: {price}"
            )
            alerts.append(msg)
            log_lines.append(f"{symbol}: 🚨 成交量 {current_vol_coin:,.1f}，达标（已触发推送）")
        else:
            log_lines.append(
                f"{symbol}: 成交量 {current_vol_coin:,.1f} {coin_symbol}，未达阈值 {threshold:,}"
            )

    new_last_ts = closed_klines[-1][0]  # 不管有没有触发，都更新到最新收盘K线的时间戳
    log_text = "\n".join(log_lines) if log_lines else f"{symbol}: 没有需要检查的新K线"
    return alerts, log_text, new_last_ts


DINGTALK_SECURITY_KEYWORD = "合约"   # 要跟钉钉机器人"安全设置->自定义关键词"里填的完全一致


def push_to_dingtalk(title: str, content: str):
    webhook = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        print("未设置 DINGTALK_WEBHOOK，跳过推送，仅打印：")
        print(content)
        return

    # 钉钉自定义机器人：text 消息类型
    # 如果机器人安全设置选的是"关键词"，消息内容里必须包含那个关键词，
    # 否则会被钉钉直接拒绝(errcode 310000)。这里统一在标题里补上，
    # 保证不管是放量提醒还是新闻提醒都能带上关键词。
    if DINGTALK_SECURITY_KEYWORD not in title and DINGTALK_SECURITY_KEYWORD not in content:
        title = f"【{DINGTALK_SECURITY_KEYWORD}】{title}"

    text = f"{title}\n{content}"
    data = {
        "msgtype": "text",
        "text": {"content": text},
    }
    resp = requests.post(webhook, json=data, timeout=10)
    print("钉钉返回:", resp.text)


def load_seen_news_ids():
    if not os.path.exists(NEWS_STATE_FILE):
        return None  # None 表示这是第一次运行，还没有历史记录
    with open(NEWS_STATE_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_news_ids(ids):
    # 只保留最近1000条，防止文件无限增长
    ids = list(ids)[-1000:]
    with open(NEWS_STATE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(ids))


def fetch_article_summary(url: str, max_len: int = 150):
    """
    打开新闻链接，抓取网页自带的摘要信息（og:description 或 meta description，
    绝大多数正规新闻网站都会放这个，用来做搜索引擎/社交媒体预览），
    这样推送里除了标题还能看到一段内容概要。抓取失败就返回 None，不影响整体推送。
    """
    try:
        resp = requests.get(
            url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True
        )
        page_html = resp.text[:200000]  # 只看前面一部分，够找到meta标签了，避免大页面拖慢速度

        patterns = [
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, page_html, re.IGNORECASE)
            if match:
                summary = html.unescape(match.group(1)).strip()
                if summary:
                    return summary[:max_len] + ("…" if len(summary) > max_len else "")
    except Exception as e:
        print(f"抓取摘要失败 {url}: {e}", file=sys.stderr)
    return None


def fetch_rss_items(feed_url: str):
    """
    拉取并解析一个RSS订阅源，返回 [(标题, 链接, 发布时间datetime或None), ...] 列表。
    不需要API key，就是普通的HTTP GET + XML解析。
    """
    resp = requests.get(feed_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""

        pub_dt = None
        if pubdate_el is not None and pubdate_el.text:
            try:
                pub_dt = email.utils.parsedate_to_datetime(pubdate_el.text.strip())
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                pub_dt = None

        if title and link:
            items.append((title, link, pub_dt))
    return items


def check_news():
    """
    检查币圈大事新闻：轮询几个主流加密货币新闻网站的官方RSS订阅源（不需要
    注册、不需要token），只推送之前没推送过的新文章，避免重复刷屏。

    额外加了一层"新鲜度过滤"：不管这条新闻之前有没有见过，只要它的实际发布
    时间超过 NEWS_MAX_AGE_HOURS，就直接丢弃不推送——这是因为谷歌新闻的搜索
    结果不完全按时间排序，偶尔会把旧新闻重新翻出来，如果只靠"有没有见过"判断，
    旧新闻第一次出现在结果里时会被误判成"新新闻"推送出去。
    """
    all_items = []
    for feed_url in NEWS_RSS_FEEDS:
        try:
            all_items.extend(fetch_rss_items(feed_url))
        except Exception as e:
            print(f"抓取新闻源失败 {feed_url}: {e}", file=sys.stderr)

    if not all_items:
        print("本次没有抓到任何新闻，跳过。")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    fresh_items = []
    stale_count = 0
    for title, link, pub_dt in all_items:
        if pub_dt is not None:
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours > NEWS_MAX_AGE_HOURS:
                stale_count += 1
                continue
        fresh_items.append((title, link))
    if stale_count:
        print(f"过滤掉 {stale_count} 条超过{NEWS_MAX_AGE_HOURS}小时的旧新闻。")

    seen_ids = load_seen_news_ids()
    first_run = seen_ids is None
    if first_run:
        seen_ids = set()

    # 用链接(url)作为每条新闻的唯一标识
    current_ids = {link for _, link, _ in all_items}   # 记录全部抓到的，不止新鲜的，避免旧新闻一直反复触发判断

    if first_run:
        # 第一次运行：只记录当前已有新闻为"已读"，不推送（避免把历史新闻当成新事件一次性刷屏）
        save_seen_news_ids(current_ids)
        print(f"新闻监控首次初始化，记录了 {len(current_ids)} 条现有新闻，之后只推送新出现的。")
        return

    new_items = [(title, link) for title, link in fresh_items if link not in seen_ids]

    if not new_items:
        print("没有新的新闻。")
    else:
        for title, link in new_items[:NEWS_MAX_PUSH_PER_RUN]:
            summary = fetch_article_summary(link)
            if summary:
                content = f"{title}\n\n📝 {summary}\n\n{link}"
            else:
                content = f"{title}\n{link}"
            push_to_dingtalk("🌐 币圈大事提醒", content)
        print(f"本次推送了 {min(len(new_items), NEWS_MAX_PUSH_PER_RUN)} 条新新闻。")

    seen_ids.update(current_ids)
    save_seen_news_ids(seen_ids)


def main():
    last_ts_map = load_last_ts()
    triggered_any = False

    for symbol in SYMBOLS:
        try:
            alerts, log_text, new_ts = check_symbol(symbol, last_ts_map.get(symbol))
            print(log_text)
            for msg in alerts:
                triggered_any = True
                push_to_dingtalk(f"合约放量提醒 - {symbol}", msg)
            if new_ts:
                last_ts_map[symbol] = new_ts
        except Exception as e:
            print(f"{symbol} 检查失败: {e}", file=sys.stderr)

    save_last_ts(last_ts_map)

    if not triggered_any:
        print("本次检查没有触发放量条件。")

    if NEWS_ENABLED:
        try:
            check_news()
        except Exception as e:
            print(f"新闻检查失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
