from __future__ import annotations

from dataclasses import dataclass

import pytest

from memecoin_bot.v12_vault_signer import _validate_transaction_authority


@dataclass
class Header:
    num_required_signatures: int


@dataclass
class Message:
    header: Header
    account_keys: list[object]


@dataclass
class Transaction:
    message: Message
    signatures: list[object]


def tx(*, required: int = 1, payer: str = "wallet", signature_slots: int = 1) -> Transaction:
    return Transaction(
        message=Message(Header(required), [payer]),
        signatures=[object() for _ in range(signature_slots)],
    )


def test_vault_signer_accepts_exact_single_signer_wallet_layout() -> None:
    _validate_transaction_authority(tx(), "wallet")


def test_vault_signer_rejects_multisigner_transaction() -> None:
    with pytest.raises(RuntimeError, match="exactly one transaction signer"):
        _validate_transaction_authority(tx(required=2, signature_slots=2), "wallet")


def test_vault_signer_rejects_wrong_fee_payer_or_first_signer() -> None:
    with pytest.raises(RuntimeError, match="fee payer"):
        _validate_transaction_authority(tx(payer="different-wallet"), "wallet")


def test_vault_signer_rejects_mismatched_signature_slots() -> None:
    with pytest.raises(RuntimeError, match="signature-slot count"):
        _validate_transaction_authority(tx(signature_slots=0), "wallet")
