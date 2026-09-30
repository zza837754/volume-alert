#!/usr/bin/env python3
"""
OKX 永续合约 - 成交量达标监控脚本
逻辑：每根5分钟K线的成交量(按币的数量)达到设定的阈值，
就通过钉钉推送通知。

加入了新闻监控：抓取 PANews（面向海外解析正常）的最新快讯接口。
"""

import os
import sys
import datetime
import requests

# ========== 可自行修改的配置 ==========
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
INTERVAL = "5m"

VOLUME_THRESHOLDS = {
    "BTC-USDT-SWAP": 5500,
    "ETH-USDT-SWAP": 100000,
}

NEWS_ENABLED = True
NEWS_STATE_FILE = "seen_news_ids.txt"
NEWS_MAX_PUSH_PER_RUN = 3
NEWS_MAX_AGE_HOURS = 3

NEWS_KEYWORDS_MUST_HAVE = [
    "SEC", "美联储", "加息", "降息", "监管", "立法", "合规", "制裁", "白宫", "特朗普",
    "政府", "央行", "关税", "法案", "起诉", "罚款", "调查",
    "暴涨", "暴跌", "新高", "新低", "突破", "跌破", "闪崩", "插针", "崩盘",
    "黑客", "被盗", "攻击", "漏洞", "跑路", "盗币",
    "ETF", "破产", "清算", "爆仓", "收购", "融资", "上市", "退市", "增持", "减持",
    "巨鲸", "转账",
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
# =======================================

OKX_KLINES_URL = "https://www.okx.com/api/v5/market/candles"


def format_candle_time(ts_ms: str) -> str:
    ts = int(ts_ms) / 1000
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc) + datetime.timedelta(hours=8)
    return dt.strftime("%Y-%m-%d %H:%M")


def get_klines(symbol: str, interval: str, limit: int):
    params = {"instId": symbol, "bar": interval, "limit": limit}
    resp = requests.get(OKX_KLINES_URL, params=params, headers=DEFAULT_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX接口返回错误: {data}")
    return list(reversed(data["data"]))


MAX_CANDLES_TO_SCAN = 30
VOLUME_STATE_FILE = "seen_volume_ts.txt"


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
    threshold = VOLUME_THRESHOLDS.get(symbol)
    if threshold is None:
        return [], f"{symbol}: 未设置阈值，跳过", last_ts

    fetch_limit = MAX_CANDLES_TO_SCAN + 2
    klines = get_klines(symbol, INTERVAL, fetch_limit)
    closed_klines = [k for k in klines if k[8] == "1"]

    if not closed_klines:
        return [], f"{symbol}: 数据不足，跳过", last_ts

    if last_ts is None:
        indices_to_check = [len(closed_klines) - 1]
    else:
        indices_to_check = [i for i, k in enumerate(closed_klines) if k[0] > last_ts]

    coin_symbol = symbol.split("-")[0]

    alerts = []
    log_lines = []
    for i in indices_to_check:
        current = closed_klines[i]
        current_vol_coin = float(current[6])
        current_vol_usdt = float(current[7])

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

    new_last_ts = closed_klines[-1][0]
    log_text = "\n".join(log_lines) if log_lines else f"{symbol}: 没有需要检查的新K线"
    return alerts, log_text, new_last_ts


DINGTALK_SECURITY_KEYWORD = "合约"


def push_to_dingtalk(title: str, content: str):
    webhook = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        print("未设置 DINGTALK_WEBHOOK，跳过推送，仅打印：")
        print(content)
        return

    if DINGTALK_SECURITY_KEYWORD not in title and DINGTALK_SECURITY_KEYWORD not in content:
        title = f"【{DINGTALK_SECURITY_KEYWORD}】{title}"

    text = f"{title}\n{content}"
    data = {
        "msgtype": "text",
        "text": {"content": text},
    }
    resp = requests.post(webhook, json=data, headers=DEFAULT_HEADERS, timeout=10)
    print("钉钉返回:", resp.text)


def load_seen_news_ids():
    if not os.path.exists(NEWS_STATE_FILE):
        return None
    with open(NEWS_STATE_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_news_ids(ids):
    ids = list(ids)[-1000:]
    with open(NEWS_STATE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(ids))


def fetch_panews_lives():
    """
    直连 PANews 官方 API 获取最新快讯。
    在海外解析与访问极度稳定。
    返回 [(id_str, title, content, link, pub_dt), ...]
    """
    # PANews 快讯公开接口
    url = "https://www.panewslab.com/webapi/flashnews?LId=1&rn=20"
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    items = []
    # 提取快讯列表
    list_data = data.get("data", {}).get("flashNews", [])
    for news in list_data:
        news_id = str(news.get("id"))
        publish_time = news.get("publishTime") # 可能是毫秒时间戳，也可能是秒
        title = news.get("title", "").strip()
        summary = news.get("desc", "").strip()
        
        # PANews 的链接规律
        link = f"https://www.panewslab.com/zh/sqarticledetails/{news_id}.html"

        pub_dt = None
        if publish_time:
            try:
                # 兼容可能是毫秒级的 publishTime
                ts = int(publish_time)
                if ts > 1e11:
                    ts = ts / 1000
                pub_dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            except Exception:
                pub_dt = None

        if title:
            items.append((news_id, title, summary, link, pub_dt))
    return items


def check_news():
    try:
        all_items = fetch_panews_lives()
        print(f"成功从 PANews 官方 API 抓取 {len(all_items)} 条快讯。")
    except Exception as e:
        print(f"抓取新闻失败: {e}", file=sys.stderr)
        return []

    if not all_items:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    fresh_items = []
    stale_count = 0
    for news_id, title, summary, link, pub_dt in all_items:
        if pub_dt is not None:
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours > NEWS_MAX_AGE_HOURS:
                stale_count += 1
                continue
        fresh_items.append((news_id, title, summary, link))

    if stale_count:
        print(f"过滤掉 {stale_count} 条超过 {NEWS_MAX_AGE_HOURS} 小时的旧快讯。")

    seen_ids = load_seen_news_ids()
    first_run = seen_ids is None
    if first_run:
        seen_ids = set()

    current_ids = {news_id for news_id, _, _, _ in fresh_items}

    if first_run:
        save_seen_news_ids(current_ids)
        print(f"新闻监控首次初始化，记录了 {len(current_ids)} 条现有新闻，之后只推送新出现的。")
        return []

    new_items = [item for item in fresh_items if item[0] not in seen_ids]

    important_items = [
        (title, summary, link) for news_id, title, summary, link in new_items
        if any(kw in title or kw in summary for kw in NEWS_KEYWORDS_MUST_HAVE)
    ]

    skipped_count = len(new_items) - len(important_items)
    if skipped_count:
        print(f"关键词筛选：过滤掉 {skipped_count} 条普通日常快讯。")

    result = []
    if not important_items:
        print("没有命中关键词的新快讯。")
    else:
        for title, summary, link in important_items[:NEWS_MAX_PUSH_PER_RUN]:
            summary_short = summary[:150] + ("…" if len(summary) > 150 else "") if summary else None
            result.append((title, summary_short, link))
        print(f"本次抓到了 {len(result)} 条命中关键词的新新闻，等待合并推送。")

    seen_ids.update(current_ids)
    save_seen_news_ids(seen_ids)
    return result


def main():
    last_ts_map = load_last_ts()
    volume_alert_msgs = []

    for symbol in SYMBOLS:
        try:
            alerts, log_text, new_ts = check_symbol(symbol, last_ts_map.get(symbol))
            print(log_text)
            volume_alert_msgs.extend(alerts)
            if new_ts:
                last_ts_map[symbol] = new_ts
        except Exception as e:
            print(f"{symbol} 检查失败: {e}", file=sys.stderr)

    save_last_ts(last_ts_map)

    if volume_alert_msgs:
        combined = "\n\n———\n\n".join(volume_alert_msgs)
        push_to_dingtalk("合约放量提醒", combined)
    else:
        print("本次检查没有触发放量条件。")

    if NEWS_ENABLED:
        try:
            news_items = check_news()
        except Exception as e:
            news_items = []
            print(f"新闻检查失败: {e}", file=sys.stderr)

        if news_items:
            parts = []
            for title, summary, link in news_items:
                if summary:
                    parts.append(f"{title}\n📝 {summary}\n{link}")
                else:
                    parts.append(f"{title}\n{link}")
            combined = "\n\n———\n\n".join(parts)
            push_to_dingtalk("🌐 币圈大事提醒", combined)


if __name__ == "__main__":
    main()
