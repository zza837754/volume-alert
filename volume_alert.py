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
import requests

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
NEWS_STATE_FILE = "seen_news_ids.txt"  # 记录已推送过的新闻链接/ID，避免重复推送
NEWS_MAX_PUSH_PER_RUN = 3           # 单次最多推送几条新闻，防止刷屏
NEWS_MAX_AGE_HOURS = 3              # 新鲜度过滤：新闻实际发布时间超过这个小时数就丢弃，不管是不是"没见过"

# 关键词筛选：标题或内容里必须命中下面任意一个词，才认为是"大事"，才会推送。
# 命中不到任何词的普通日常快讯直接过滤掉，不推送。可以自己增删这个列表。
NEWS_KEYWORDS_MUST_HAVE = [
    # 监管/政策/宏观
    "SEC", "美联储", "加息", "降息", "监管", "立法", "合规", "制裁", "白宫", "特朗普",
    "政府", "央行", "关税", "法案", "起诉", "罚款", "调查",
    # 价格/走势级别事件
    "暴涨", "暴跌", "新高", "新低", "突破", "跌破", "闪崩", "插针", "崩盘",
    # 安全事件
    "黑客", "被盗", "攻击", "漏洞", "跑路", "盗币",
    # 资金/机构级别
    "ETF", "破产", "清算", "爆仓", "收购", "融资", "上市", "退市", "增持", "减持",
    "巨鲸", "转账",
]

# 通用请求头，伪装浏览器，防止 HTTP 请求被拦截
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
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
    resp = requests.get(OKX_KLINES_URL, params=params, headers=DEFAULT_HEADERS, timeout=10)
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
    达到 VOLUME_THRESHOLDS 里设置的数量就推送。
    """
    threshold = VOLUME_THRESHOLDS.get(symbol)
    if threshold is None:
        return [], f"{symbol}: 未设置阈值，跳过", last_ts

    fetch_limit = MAX_CANDLES_TO_SCAN + 2
    klines = get_klines(symbol, INTERVAL, fetch_limit)
    closed_klines = [k for k in klines if k[8] == "1"]  # 只保留已收盘的K线，从旧到新排列

    if not closed_klines:
        return [], f"{symbol}: 数据不足，跳过", last_ts

    if last_ts is None:
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


def fetch_jinse_lives():
    """
    直连金色财经官方 API 获取最新快讯，免去第三方 RSSHub 依赖。
    返回 [(id_str, title, content, link, pub_dt), ...]
    """
    url = "https://api.jinse.cn/noauth/v1/lives/list?limit=20"
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    items = []
    # 解析 API 返回数据
    list_data = data.get("list", [])
    for group in list_data:
        for live in group.get("lives", []):
            live_id = str(live.get("id"))
            created_at = live.get("created_at")  # 秒级时间戳
            content_text = live.get("content", "")

            # 清理 HTML 格式
            content_text = html.unescape(content_text)
            content_text = re.sub(r'<[^>]+>', '', content_text).strip()

            # 金色快讯一般格式为【标题】正文内容
            title_match = re.search(r'【(.*?)】', content_text)
            if title_match:
                title = title_match.group(1).strip()
                summary = content_text.replace(f"【{title}】", "").strip()
            else:
                title = content_text[:30] + "..." if len(content_text) > 30 else content_text
                summary = content_text

            link = f"https://www.jinse.cn/lives/{live_id}.html"

            pub_dt = None
            if created_at:
                try:
                    pub_dt = datetime.datetime.fromtimestamp(int(created_at), tz=datetime.timezone.utc)
                except Exception:
                    pub_dt = None

            items.append((live_id, title, summary, link, pub_dt))
    return items


def check_news():
    """
    检查币圈大事新闻：直接轮询金色财经 API，进行时间新鲜度过滤与关键词筛选。
    """
    try:
        all_items = fetch_jinse_lives()
        print(f"成功从金色财经官方 API 抓取 {len(all_items)} 条快讯。")
    except Exception as e:
        print(f"抓取新闻失败: {e}", file=sys.stderr)
        return []

    if not all_items:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    fresh_items = []
    stale_count = 0
    for live_id, title, summary, link, pub_dt in all_items:
        if pub_dt is not None:
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours > NEWS_MAX_AGE_HOURS:
                stale_count += 1
                continue
        fresh_items.append((live_id, title, summary, link))

    if stale_count:
        print(f"过滤掉 {stale_count} 条超过 {NEWS_MAX_AGE_HOURS} 小时的旧快讯。")

    seen_ids = load_seen_news_ids()
    first_run = seen_ids is None
    if first_run:
        seen_ids = set()

    current_ids = {live_id for live_id, _, _, _ in fresh_items}

    if first_run:
        save_seen_news_ids(current_ids)
        print(f"新闻监控首次初始化，记录了 {len(current_ids)} 条现有新闻，之后只推送新出现的。")
        return []

    new_items = [item for item in fresh_items if item[0] not in seen_ids]

    # 关键词筛选：标题或摘要中包含关键词
    important_items = [
        (title, summary, link) for live_id, title, summary, link in new_items
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
            # 限制摘要展示长度
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
