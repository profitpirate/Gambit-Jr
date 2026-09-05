from __future__ import annotations

import unittest
from datetime import UTC, datetime

from memecoin_bot.discord.cupsey_watch import (
    CUPSEY_WALLET,
    CupseyDropWatcher,
    DropEvidence,
    extract_official_contract,
    extract_wallet_launch,
    valid_solana_address,
)

MINT = "3hCyCV1JhuF6Rup98djLbh1fyKxHyQjTcTGEQcA1pump"


class CupseyWatchTests(unittest.TestCase):
    def test_known_mint_is_valid_solana_address(self) -> None:
        self.assertTrue(valid_solana_address(MINT))

    def test_official_contract_link_is_detected(self) -> None:
        html = f'<a href="https://pump.fun/coin/{MINT}">Pump.fun</a>'
        self.assertEqual(extract_official_contract(html), MINT)

    def test_unrelated_bare_address_is_not_detected(self) -> None:
        html = f'<script>const cacheKey = "{MINT}";</script>'
        self.assertIsNone(extract_official_contract(html))

    def test_contract_label_allows_bare_official_ca(self) -> None:
        html = f'<div>Official contract address <code>{MINT}</code></div>'
        self.assertEqual(extract_official_contract(html), MINT)

    def test_cupsey_wallet_create_is_detected(self) -> None:
        payload = {
            "txType": "create",
            "traderPublicKey": CUPSEY_WALLET,
            "mint": MINT,
        }
        self.assertEqual(extract_wallet_launch(payload), MINT)

    def test_other_wallet_or_buy_is_rejected(self) -> None:
        self.assertIsNone(
            extract_wallet_launch(
                {"txType": "create", "traderPublicKey": MINT, "mint": MINT}
            )
        )
        self.assertIsNone(
            extract_wallet_launch(
                {"txType": "buy", "traderPublicKey": CUPSEY_WALLET, "mint": MINT}
            )
        )

    def test_alert_explicitly_allows_only_everyone_mention(self) -> None:
        watcher = CupseyDropWatcher(
            discord_token="token",
            channel_id=123,
            state_path=None,
        )
        payload = watcher._payload(
            DropEvidence(
                mint=MINT,
                source="test",
                detected_at=datetime.now(UTC),
                detail="test evidence",
            )
        )
        self.assertEqual(payload["content"].count("@everyone"), 1)
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})


if __name__ == "__main__":
    unittest.main()
