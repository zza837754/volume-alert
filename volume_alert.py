#!/usr/bin/env python3
"""
币安永续合约 - 成交量异常放大监控脚本
逻辑：取最近一根已收盘的 5分钟K线成交量，与前面 N 根K线的平均成交量对比，
如果放大倍数超过阈值，就通过 PushPlus 推送微信通知。

运行方式：由 GitHub Actions 定时触发（见 .github/workflows/volume_monitor.yml）
也可以在自己电脑上手动跑：python volume_alert.py
"""

import os
import sys
import requests

# ========== 可自行修改的配置 ==========
SYMBOLS = ["BTCUSDT", "ETHUSDT"]   # 想监控的合约品种，可自行增删
INTERVAL = "5m"                     # K线周期：1m, 5m, 15m, 1h ...
LOOKBACK = 20                       # 用前面多少根K线计算平均成交量
THRESHOLD_MULTIPLIER = 1.1         # 放大倍数阈值，超过这个倍数才报警
# =======================================

BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
PUSHPLUS_URL = "https://www.pushplus.plus/send"


def get_klines(symbol: str, interval: str, limit: int):
    """从币安合约公开接口获取K线数据，不需要API Key"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def check_symbol(symbol: str):
    """
    检查单个品种是否出现放量。
    返回 (是否触发, 详情文本) 
    """
    # 多取1根，因为最后一根可能还没收盘，我们只用倒数第2根(已收盘)当"当前"K线
    klines = get_klines(symbol, INTERVAL, LOOKBACK + 2)
    if len(klines) < LOOKBACK + 2:
        return False, f"{symbol}: 数据不足，跳过"

    # 币安K线字段：[开盘时间, 开, 高, 低, 收, 成交量(币本位), 收盘时间, 成交额(USDT), ...]
    closed_klines = klines[:-1]          # 去掉最后一根未收盘的
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

    return False, f"{symbol}: 当前放量倍数 {ratio:.2f}，未达阈值 {THRESHOLD_MULTIPLIER}"


def push_to_wechat(title: str, content: str):
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        print("未设置 PUSHPLUS_TOKEN，跳过推送，仅打印：")
        print(content)
        return

    data = {
        "token": token,
        "title": title,
        "content": content.replace("\n", "<br>"),
        "template": "html",
    }
    resp = requests.post(PUSHPLUS_URL, json=data, timeout=10)
    print("PushPlus 返回:", resp.text)


def main():
    triggered_any = False
    for symbol in SYMBOLS:
        try:
            triggered, detail = check_symbol(symbol)
            print(detail)
            if triggered:
                triggered_any = True
                push_to_wechat(f"合约放量提醒 - {symbol}", detail)
        except Exception as e:
            print(f"{symbol} 检查失败: {e}", file=sys.stderr)

    if not triggered_any:
        print("本次检查没有触发放量条件。")


if __name__ == "__main__":
    main()
