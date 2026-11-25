#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
exec .venv/bin/python -c '
from getpass import getpass
import json
import os
from tempfile import NamedTemporaryFile

from alpaca_data import AlpacaDaily, SECRETS_FILE
from market_data import MarketError

try:
    key_id = getpass("Alpaca Paper API Key ID: ").strip()
    secret_key = getpass("Alpaca Paper Secret Key: ").strip()
    if not key_id or not secret_key:
        raise SystemExit("请填写两项凭据。")
    AlpacaDaily((key_id, secret_key)).fetch("AAPL")
    config = json.loads(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else {}
    config["alpaca"] = {"key_id": key_id, "secret_key": secret_key}
    SECRETS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=SECRETS_FILE.parent, delete=False) as output:
        temporary = output.name
        json.dump(config, output, indent=2, ensure_ascii=False)
    os.replace(temporary, SECRETS_FILE)
    print("Alpaca SIP 验证成功，已保存到本机。重新启动看板后生效。")
except MarketError as error:
    messages = {
        "alpaca_auth": "Alpaca 拒绝了凭据，请核对 Paper Key ID 和 Secret Key。",
        "alpaca_feed_permission": "该账号没有此次 SIP 请求的权限。",
    }
    raise SystemExit(messages.get(error.code, "行情验证失败，请稍后重试。")) from None
except (ValueError, TypeError, OSError):
    raise SystemExit("无法读取或写入本机配置。") from None
except KeyboardInterrupt:
    raise SystemExit("已取消。") from None
'
