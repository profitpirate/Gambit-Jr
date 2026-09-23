from __future__ import annotations
import base64
import pytest
from solders.keypair import Keypair
from memecoin_bot.v12_axiom_wallet import challenge, verify_binding, assert_display_only, AxiomWalletBinding

def test_axiom_visible_wallet_is_cryptographically_bound_without_axiom_credentials():
    kp=Keypair()
    wallet=str(kp.pubkey())
    nonce="0123456789abcdef0123456789abcdef"
    sig=kp.sign_message(challenge(wallet,nonce))
    binding=verify_binding(wallet,nonce,base64.b64encode(bytes(sig)).decode())
    assert binding.verified and binding.wallet==wallet
    assert binding.execution_mode=="NATIVE_SOLANA"
    assert binding.axiom_automation is False
    assert_display_only(binding)

def test_wrong_wallet_signature_fails_closed():
    owner=Keypair(); attacker=Keypair()
    nonce="0123456789abcdef0123456789abcdef"
    sig=attacker.sign_message(challenge(str(owner.pubkey()),nonce))
    with pytest.raises(PermissionError):
        verify_binding(str(owner.pubkey()),nonce,base64.b64encode(bytes(sig)).decode())

def test_axiom_automation_mode_is_rejected():
    with pytest.raises(RuntimeError):
        assert_display_only(AxiomWalletBinding("wallet",True,"AXIOM_BROWSER",True))
