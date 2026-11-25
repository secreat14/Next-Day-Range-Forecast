# Next-Day Range Forecast

使用随机森林预测美股和 ETF 的下一交易日振幅。双击 [run-dashboard.command](stock-dashboard/run-dashboard.command)，再打开终端显示的本地地址，默认端口为 18765。也可在仓库根目录运行以下命令。

```sh
cd stock-dashboard
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

- **实时预测**从 Yahoo Finance 公共日线接口获取近期复权 OHLC 和成交量，并写入 `.data/market.sqlite3`。页面打开时每分钟请求本地服务，服务对同一证券缓存行情五分钟。手动刷新会立即尝试重新取数。可在搜索栏输入新的美国股票或 ETF 代码，再点击「添加代码」。
- **历史回测**可直接打开 [stock-amplitude-forecast.html](stock-amplitude-forecast.html)，使用内嵌行情和 2015–2017 年样本外预测。行情来自 Boris Marjanovic 在 Kaggle 发布的 [Huge Stock Market Dataset](https://www.kaggle.com/datasets/borismarjanovic/price-volume-data-for-all-us-stocks-etfs)（CC0）。模型节点取自各证券和年份实际使用的随机森林。
- 两页均可查看森林中的前 3 棵决策树。实时页标出最新预测输入的决策路径。节点的 `n` 是该树在该节点的不同训练样本数。
- 实时模型使用最近约两年的日线，输入为最近 1、5、22 个交易日的振幅，收盘后约 20 分钟起接纳完整日线。行情源缺少完整数据时会标明上一次预测的适用日期。服务绑定本机回环地址，数据保存在本机。

进入 `stock-dashboard/` 后运行 `.venv/bin/python -m pytest -q` 验证。

## Alpaca Paper 行情

在 [Alpaca Paper Trading](https://alpaca.markets/learn/start-paper-trading) 生成 Paper API Key ID 和 Secret Key 后，可以双击 [set_alpaca_keys.command](stock-dashboard/set_alpaca_keys.command) 在本机隐藏输入。脚本通过 Alpaca SIP 历史日线接口验证凭据，随后写入 Git 忽略的 `stock-dashboard/local-secrets/notes/project-secrets.json`。重启看板会自动选择 Alpaca，并使用单独的 SQLite 行情缓存。从仓库根目录运行 `stock-dashboard/run-dashboard.command --provider yahoo` 可显式选择 Yahoo Finance。
