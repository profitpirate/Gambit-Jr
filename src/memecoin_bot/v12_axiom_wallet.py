"""Axiom-visible wallet binding for V12.

Axiom is intentionally a display/portfolio surface only. Gambit never stores an
Axiom credential, browser session or cookie. The trading wallet is verified
cryptographically and all execution remains native Solana.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from solders.pubkey import Pubkey
from solders.signature import Signature

DOMAIN = b"gambit:v12:axiom-visible-wallet:v1:"


@dataclass(frozen=True, slots=True)
class AxiomWalletBinding:
    wallet: str
    verified: bool
    execution_mode: str = "NATIVE_SOLANA"
    axiom_automation: bool = False


def challenge(wallet: str, nonce: str) -> bytes:
    Pubkey.from_string(wallet)
    if len(nonce) < 16:
        raise ValueError("wallet-binding nonce must contain at least 16 characters")
    return DOMAIN + wallet.encode() + b":" + nonce.encode()


def verify_binding(wallet: str, nonce: str, signature_b64: str) -> AxiomWalletBinding:
    public_key = Pubkey.from_string(wallet)
    signature = Signature.from_bytes(base64.b64decode(signature_b64, validate=True))
    if not signature.verify(public_key, challenge(wallet, nonce)):
        raise PermissionError("wallet ownership signature verification failed")
    return AxiomWalletBinding(wallet=wallet, verified=True)


def assert_display_only(binding: AxiomWalletBinding) -> None:
    if not binding.verified:
        raise PermissionError("unverified trading wallet")
    if binding.axiom_automation or binding.execution_mode != "NATIVE_SOLANA":
        raise RuntimeError("Axiom must remain display-only; execution is native Solana")
