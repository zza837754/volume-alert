#!/usr/bin/env python3
"""
OKX 永续合约 - 成交量异常放大监控脚本
逻辑：取最近一根已收盘的 5分钟K线成交量，与前面 N 根K线的平均成交量对比，
如果放大倍数超过阈值，就通过 PushPlus 推送微信通知。

数据源用 OKX 而不是币安：因为币安合约接口(fapi.binance.com)对美国地区IP
直接返回451拒绝访问，而 GitHub Actions 的服务器全部在美国机房，无法绕过。
OKX 的公开行情接口没有这个限制。

运行方式：由 GitHub Actions 定时触发（见 .github/workflows/volume_monitor.yml）
也可以在自己电脑上手动跑：python volume_alert.py
"""

import os
import sys
import requests

# ========== 可自行修改的配置 ==========
# OKX 永续合约命名格式：币种-USDT-SWAP
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]   # 想监控的合约品种，可自行增删
INTERVAL = "5m"                     # K线周期：1m, 5m, 15m, 1H ...
LOOKBACK = 20                       # 用前面多少根K线计算平均成交量
THRESHOLD_MULTIPLIER = 1.1         # 放大倍数阈值，超过这个倍数才报警
# =======================================

OKX_KLINES_URL = "https://www.okx.com/api/v5/market/candles"
PUSHPLUS_URL = "https://www.pushplus.plus/send"


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
