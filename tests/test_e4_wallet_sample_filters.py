from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_live_market_stress as subject


def test_quote_assets_are_excluded_from_e4_trade_reconstruction() -> None:
    assert subject.USDC_MINT in subject.QUOTE_ASSET_MINTS
    assert subject.USDT_MINT in subject.QUOTE_ASSET_MINTS
    assert subject.WSOL_MINT in subject.QUOTE_ASSET_MINTS
    assert subject.PUMP_TOKEN_MINT in subject.QUOTE_ASSET_MINTS


def test_token_receipt_requires_material_sol_outflow() -> None:
    assert subject.trade_like_wallet_event(1_000.0, -1.0) is True
    assert subject.trade_like_wallet_event(1_000.0, -0.00001) is False
    assert subject.trade_like_wallet_event(1_000.0, 0.0) is False


def test_token_disposal_requires_material_sol_inflow() -> None:
    assert subject.trade_like_wallet_event(-1_000.0, 1.0) is True
    assert subject.trade_like_wallet_event(-1_000.0, -0.00001) is False
    assert subject.trade_like_wallet_event(-1_000.0, 0.0) is False
