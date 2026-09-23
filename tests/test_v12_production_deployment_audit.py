from pathlib import Path
from scripts.v12_production_deployment_audit import audit

def test_production_deployment_audit_passes_repository_contract():
    result=audit(Path(__file__).resolve().parents[1])
    assert result["status"]=="PASS", result
    assert result["execution_architecture"]=="NATIVE_SOLANA"
    assert result["axiom_role"]=="DISPLAY_ONLY"
