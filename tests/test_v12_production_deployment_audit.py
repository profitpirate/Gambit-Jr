from pathlib import Path

from scripts.v12_production_deployment_audit import audit


def test_production_deployment_audit_passes_repository_contract():
    result = audit(Path(__file__).resolve().parents[1])
    assert result["status"] == "PASS", result
    assert result["execution_architecture"] == "NATIVE_SOLANA"
    assert result["canonical_live_entrypoint"] == "memecoin_bot.e4_exec"
    assert result["axiom_role"] == "DISPLAY_ONLY"
    assert result["legacy_plaintext_keypair_surface"] is False
    assert result["checks"]["canonical_cli_gate"] is True
    assert result["checks"]["legacy_alias_safe"] is True
    assert all(result["checks"]["compose"]["prod"].values())
    assert all(result["checks"]["docker"]["prod"].values())
    assert all(result["checks"]["env"].values())
