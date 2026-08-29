#!/usr/bin/env python3
"""
OKX 永续合约 - 成交量异常放大监控脚本
逻辑：取最近一根已收盘的 5分钟K线成交量，与前面 N 根K线的平均成交量对比，
如果放大倍数超过阈值，就通过 钉钉机器人 推送通知到手机。

数据源用 OKX 而不是币安：因为币安合约接口(fapi.binance.com)对美国地区IP
直接返回451拒绝访问，而 GitHub Actions 的服务器全部在美国机房，无法绕过。
OKX 的公开行情接口没有这个限制。

运行方式：由 GitHub Actions 定时触发（见 .github/workflows/volume_monitor.yml）
也可以在自己电脑上手动跑：python volume_alert.py
"""

import os
import sys
import requests
import xml.etree.ElementTree as ET

# ========== 可自行修改的配置 ==========
# OKX 永续合约命名格式：币种-USDT-SWAP
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]   # 想监控的合约品种，可自行增删
INTERVAL = "5m"                     # K线周期：1m, 5m, 15m, 1H ...
LOOKBACK = 20                       # 用前面多少根K线计算平均成交量
THRESHOLD_MULTIPLIER = 3.0          # 放大倍数阈值，超过这个倍数才报警

NEWS_ENABLED = True                 # 是否开启币圈大事新闻推送
NEWS_STATE_FILE = "seen_news_ids.txt"  # 记录已推送过的新闻链接，避免重复推送
NEWS_MAX_PUSH_PER_RUN = 5           # 单次最多推送几条新闻，防止刷屏
# 新闻来源：不需要注册、不需要token的官方RSS订阅源，全球最主流的两家加密货币新闻网站
NEWS_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
# =======================================

OKX_KLINES_URL = "https://www.okx.com/api/v5/market/candles"


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


def check_symbol(symbol: str):
    """
    检查单个品种是否出现放量。
    返回 (是否触发, 详情文本)
    """
    # OKX K线字段：[ts, o, h, l, c, vol(张数), volCcy(币本位), volCcyQuote(USDT计价), confirm]
    # confirm="1" 表示这根K线已收盘，"0" 表示还在进行中
    klines = get_klines(symbol, INTERVAL, LOOKBACK + 2)
    if len(klines) < LOOKBACK + 2:
        return False, f"{symbol}: 数据不足，跳过"

    # 只保留已收盘的K线
    closed_klines = [k for k in klines if k[8] == "1"]
    if len(closed_klines) < LOOKBACK + 1:
        return False, f"{symbol}: 已收盘K线数据不足，跳过"

    current = closed_klines[-1]          # 最新已收盘的一根
    history = closed_klines[-(LOOKBACK + 1):-1]  # 再往前 LOOKBACK 根作为基准

    current_vol = float(current[7])      # 用成交额(USDT)更能反映真实"钱"的规模
    avg_vol = sum(float(k[7]) for k in history) / len(history)

    if avg_vol <= 0:
        return False, f"{symbol}: 历史均量异常，跳过"

    ratio = current_vol / avg_vol

    if ratio >= THRESHOLD_MULTIPLIER:
        price = float(current[4])
        msg = (
            f"🚨 {symbol} 成交量放大 {ratio:.1f} 倍！\n"
            f"周期: {INTERVAL}\n"
            f"最新价: {price}\n"
            f"本期成交额: {current_vol:,.0f} USDT\n"
            f"前{LOOKBACK}期均值: {avg_vol:,.0f} USDT"
        )
        return True, msg

    return False, (
        f"{symbol}: 当前放量倍数 {ratio:.2f}，未达阈值 {THRESHOLD_MULTIPLIER}"
        f"（本期成交额={current_vol:,.0f} USDT，前{LOOKBACK}期均值={avg_vol:,.0f} USDT）"
    )


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


def fetch_rss_items(feed_url: str):
    """
    拉取并解析一个RSS订阅源，返回 [(标题, 链接), ...] 列表。
    不需要API key，就是普通的HTTP GET + XML解析。
    """
    resp = requests.get(feed_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if title and link:
            items.append((title, link))
    return items


def check_news():
    """
    检查币圈大事新闻：轮询几个主流加密货币新闻网站的官方RSS订阅源（不需要
    注册、不需要token），只推送之前没推送过的新文章，避免重复刷屏。
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

    seen_ids = load_seen_news_ids()
    first_run = seen_ids is None
    if first_run:
        seen_ids = set()

    # 用链接(url)作为每条新闻的唯一标识
    current_ids = {link for _, link in all_items}

    if first_run:
        # 第一次运行：只记录当前已有新闻为"已读"，不推送（避免把历史新闻当成新事件一次性刷屏）
        save_seen_news_ids(current_ids)
        print(f"新闻监控首次初始化，记录了 {len(current_ids)} 条现有新闻，之后只推送新出现的。")
        return

    new_items = [(title, link) for title, link in all_items if link not in seen_ids]

    if not new_items:
        print("没有新的新闻。")
        return

    for title, link in new_items[:NEWS_MAX_PUSH_PER_RUN]:
        content = f"{title}\n{link}"
        push_to_dingtalk("🌐 币圈大事提醒", content)

    seen_ids.update(current_ids)
    save_seen_news_ids(seen_ids)
    print(f"本次推送了 {min(len(new_items), NEWS_MAX_PUSH_PER_RUN)} 条新新闻。")


def main():
    triggered_any = False
    for symbol in SYMBOLS:
        try:
            triggered, detail = check_symbol(symbol)
            print(detail)
            if triggered:
                triggered_any = True
                push_to_dingtalk(f"合约放量提醒 - {symbol}", detail)
        except Exception as e:
            print(f"{symbol} 检查失败: {e}", file=sys.stderr)

    if not triggered_any:
        print("本次检查没有触发放量条件。")

    if NEWS_ENABLED:
        try:
            check_news()
        except Exception as e:
            print(f"新闻检查失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
