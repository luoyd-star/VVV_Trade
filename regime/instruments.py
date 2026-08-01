"""品种注册表：symbol → 类别 / 数据源路由 / Hyperliquid coin 映射。

instruments.json 每次调用热读（与 agent.json 同哲学，改完即生效）；
未登记的 symbol 回退到加密默认（deribit→okx→binance 现货），存量行为零变化。
类别 class：
  crypto        加密（默认）
  us_stock_perp 美股永续（币安 EQUITY 区 / Hyperliquid builder dex）——
                标的正股仅 9:30-16:00 ET 交易，合约 24/7，休市期波动塌陷
                可能产生"假挤压"，面板与 VVVhermes 都会标注。
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "instruments.json")

CRYPTO_DEFAULT = {
    "class": "crypto",
    "sources": ["deribit", "okx", "binance"],
    "hl_coin": None,
    "display": None,
}


def load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
    except OSError:
        return {}
    except Exception:  # noqa: BLE001  配置写坏时回退默认而不是拖垮服务
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get(symbol: str) -> dict:
    cfg = dict(CRYPTO_DEFAULT)
    cfg.update(load().get(symbol.upper(), {}))
    return cfg
