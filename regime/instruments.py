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


_LAST_GOOD: dict = {}


def load() -> dict:
    """热读注册表。**损坏时回退最近一次成功解析的快照**，绝不回退空表——
    审计发现：半写 JSON 会让 61 个美股静默降级成 crypto 默认（错源、错时钟、
    丢 IV/休市语义），且无任何告警。last-known-good 是唯一安全的降级方向。"""
    global _LAST_GOOD
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
        good = {k: v for k, v in data.items() if not k.startswith("_")}
        if good:
            _LAST_GOOD = good
        return good or dict(_LAST_GOOD)
    except Exception:  # noqa: BLE001  配置读坏：用上一份好的，而非空表
        return dict(_LAST_GOOD)


def get(symbol: str) -> dict:
    cfg = dict(CRYPTO_DEFAULT)
    cfg.update(load().get(symbol.upper(), {}))
    return cfg
