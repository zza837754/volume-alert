# 币安合约成交量放量监控（免费云端，不用挂电脑）

原理：GitHub 免费提供的 Actions 服务器每 5 分钟帮你跑一次脚本，检测到成交量异常放大就通过 PushPlus 推送到你的微信。全程不花钱，也不需要自己开电脑或买服务器。

## 第一步：拿 PushPlus 推送 token（1分钟）

1. 打开 https://www.pushplus.plus
2. 用微信扫码登录
3. 登录后首页就能看到你的专属 **token**，复制它

## 第二步：新建 GitHub 仓库并上传这3个文件

1. 没有 GitHub 账号的话先免费注册：https://github.com
2. 新建一个仓库（Repository），比如叫 `volume-alert`，设为 Private（私有）也可以
3. 把这个文件夹里的内容上传上去，保持目录结构不变：
   ```
   volume_alert.py
   .github/workflows/volume_monitor.yml
   README.md
   ```
   （最简单的方式：网页上点 "Add file" → "Upload files"，把文件拖进去；`.github/workflows/volume_monitor.yml` 要保证路径不变，如果网页上传不方便建文件夹，可以先建一个空文件 `.github/workflows/volume_monitor.yml` 再粘贴内容进去）

## 第三步：把 token 配置成 GitHub Secret（重要，不要把token直接写进代码）

1. 进入你的仓库 → 上方 **Settings**
2. 左边菜单找 **Secrets and variables → Actions**
3. 点 **New repository secret**
4. Name 填：`PUSHPLUS_TOKEN`
5. Value 填：你在第一步拿到的 token
6. 保存

## 第四步：手动测试一次

1. 进入仓库上方 **Actions** 标签
2. 左边选 "Binance Volume Monitor"
3. 右边点 **Run workflow** 手动触发一次
4. 等半分钟，刷新看运行结果（绿勾=成功）；如果当时确实有品种放量超过阈值，你的微信应该会收到 PushPlus 推送

之后就不用管了，它会按 `.github/workflows/volume_monitor.yml` 里设置的每5分钟自动跑一次（GitHub 实际调度可能有几分钟延迟，属于正常现象，不影响使用）。

## 想改监控参数？

打开 `volume_alert.py` 顶部这几行改一下就行，改完重新上传覆盖即可：

```python
SYMBOLS = ["BTCUSDT", "ETHUSDT"]   # 想监控哪些合约，自己加/删
INTERVAL = "5m"                     # K线周期
LOOKBACK = 20                       # 用前面多少根K线算平均成交量
THRESHOLD_MULTIPLIER = 3.0          # 放大超过几倍才报警，改小更敏感，改大更少报警
```

## 常见问题

- **一直没收到推送**：先去 Actions 页面看有没有报错；也可能是当前确实没有触发放量条件（可以临时把 `THRESHOLD_MULTIPLIER` 改成 1.1 测试一下推送链路通不通）。
- **想同时用手机 App 而不是微信**：把 `push_to_wechat` 函数换成 Bark（iOS）或 Server酱 的推送接口即可，逻辑类似。
- **免费额度够用吗**：GitHub Actions 公共仓库无限免费；私有仓库个人账号每月有 2000 分钟免费额度，这个脚本每次运行几秒钟，5分钟跑一次完全够用不会超额。
