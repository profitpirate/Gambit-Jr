from __future__ import annotations

from pathlib import Path

import pytest

from memecoin_bot.access_service import AccessService
from memecoin_bot.access_store import AccessStore


class AlwaysValidVerifier:
    def verify(self, wallet: str, message: bytes, signature: str) -> bool:
        return bool(wallet and message and signature == "valid")


@pytest.fixture
def access(tmp_path: Path):
    store = AccessStore(tmp_path / "access.db")
    service = AccessService(store, portal_base_url="https://portal.invalid")
    try:
        yield store, service
    finally:
        store.close()


def test_discord_login_grant_is_one_time(access) -> None:
    _store, service = access
    grant = service.issue_discord_login("12345")
    assert grant.magic_url.startswith("https://portal.invalid/login?code=")
    session = service.redeem_login(grant.code)
    user_id = service.authenticate(session)
    assert user_id == grant.user_id
    with pytest.raises(PermissionError):
        service.redeem_login(grant.code)


def test_wallet_link_requires_signature_proof(access) -> None:
    store, service = access
    grant = service.issue_discord_login("12345")
    session = service.redeem_login(grant.code)
    user_id = service.authenticate(session)
    challenge = service.create_wallet_challenge(user_id, "Wallet111")
    assert "does not authorize a transaction" in challenge.message

    with pytest.raises(PermissionError):
        service.verify_wallet(
            user_id,
            challenge.challenge_id,
            "invalid",
            AlwaysValidVerifier(),
        )

    challenge = service.create_wallet_challenge(user_id, "Wallet111")
    wallet = service.verify_wallet(
        user_id,
        challenge.challenge_id,
        "valid",
        AlwaysValidVerifier(),
    )
    assert wallet == "Wallet111"
    snapshot = store.account_snapshot(user_id)
    assert snapshot["wallets"][0]["wallet"] == "Wallet111"


def test_axiom_linking_fails_closed_and_raw_secrets_are_forbidden(access) -> None:
    _store, service = access
    grant = service.issue_discord_login("12345")
    user_id = service.authenticate(service.redeem_login(grant.code))

    with pytest.raises(RuntimeError, match="Axiom direct account linking is disabled"):
        service.set_execution_connection(
            user_id,
            provider="AXIOM",
            mode="SESSION",
            public_identifier="axiom-user",
            secret_ref="vault://axiom",
        )

    with pytest.raises(ValueError, match="raw private keys are forbidden"):
        service.set_execution_connection(
            user_id,
            provider="NATIVE_WALLET",
            mode="DELEGATED",
            public_identifier="Wallet111",
            secret_ref="plain-private-key",
        )


def test_native_wallet_connection_requires_verified_wallet(access) -> None:
    store, service = access
    grant = service.issue_discord_login("12345")
    user_id = service.authenticate(service.redeem_login(grant.code))
    with pytest.raises(PermissionError, match="verified wallet"):
        service.set_execution_connection(
            user_id,
            provider="NATIVE_WALLET",
            mode="DELEGATED",
            public_identifier="Wallet111",
            secret_ref="vault://wallet-signer",
        )

    store.conn.execute(
        "INSERT INTO access_wallets(user_id,wallet,verified_ns) VALUES(?,?,1)",
        (user_id, "Wallet111"),
    )
    connection = service.set_execution_connection(
        user_id,
        provider="NATIVE_WALLET",
        mode="DELEGATED",
        public_identifier="Wallet111",
        secret_ref="vault://wallet-signer",
    )
    assert connection > 0


def test_execution_mandate_has_hard_risk_bounds(access) -> None:
    _store, service = access
    grant = service.issue_discord_login("12345")
    user_id = service.authenticate(service.redeem_login(grant.code))
    with pytest.raises(ValueError):
        service.set_mandate(
            user_id,
            enabled=True,
            max_active_bankroll_sol=10,
            max_position_fraction=0.25,
            max_concurrent_positions=2,
            storage_wallet=None,
        )
