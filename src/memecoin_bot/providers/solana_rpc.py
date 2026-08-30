from __future__ import annotations

from typing import Any

from memecoin_bot.models import SafetyAssessment, iso
from memecoin_bot.providers.base import ProviderError, ResilientJsonClient


class SolanaRpcProvider:
    name = "solana_rpc"

    def __init__(
        self,
        rpc_url: str,
        client: ResilientJsonClient,
        name: str = "solana_rpc",
    ):
        self.rpc_url = rpc_url
        self.client = client
        self.name = name
        self._request_id = 0

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        data = await self.client.request(
            self.rpc_url,
            "POST",
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            },
        )
        if data.get("error"):
            raise ProviderError(f"Solana {method}: {data['error']}")
        return data.get("result")

    async def safety(self, token_address: str) -> SafetyAssessment:
        assessment = SafetyAssessment(checked_at=iso(), source=self.name, chain="solana")
        account = await self._rpc(
            "getAccountInfo", [token_address, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        value = (account or {}).get("value")
        if not value:
            assessment.rejection_reasons.append("MINT_ACCOUNT_NOT_FOUND")
            return assessment
        parsed = ((value.get("data") or {}).get("parsed") or {}).get("info") or {}
        assessment.mint_authority = parsed.get("mintAuthority")
        assessment.freeze_authority = parsed.get("freezeAuthority")
        supply_text = parsed.get("supply")
        assessment.supply_raw = int(supply_text) if supply_text is not None else None
        assessment.decimals = parsed.get("decimals")

        try:
            supply_result = await self._rpc(
                "getTokenSupply", [token_address, {"commitment": "confirmed"}]
            )
            raw = ((supply_result or {}).get("value") or {}).get("amount")
            if raw is not None:
                assessment.supply_raw = int(raw)
            largest_result = await self._rpc(
                "getTokenLargestAccounts", [token_address, {"commitment": "confirmed"}]
            )
            accounts = (largest_result or {}).get("value") or []
            if assessment.supply_raw and accounts:
                top10 = sum(int(a["amount"]) for a in accounts[:10] if a.get("amount") is not None)
                assessment.top10_percent = top10 / assessment.supply_raw * 100
        except ProviderError as exc:
            # Mint authorities remain real data; distribution is explicitly unknown.
            assessment.warnings.append(f"DISTRIBUTION_UNAVAILABLE:{exc}")
        return assessment


class SolanaSafetyFailoverProvider:
    """Try independent Solana RPC safety providers without relaxing evidence rules."""

    name = "solana_safety_failover"

    def __init__(self, providers: list[SolanaRpcProvider]):
        if not providers:
            raise ValueError("at least one Solana safety provider is required")
        self.providers = providers

    async def safety(self, token_address: str) -> SafetyAssessment:
        failures: list[str] = []
        for provider in self.providers:
            try:
                assessment = await provider.safety(token_address)
            except ProviderError as error:
                failures.append(f"{provider.name}:{type(error).__name__}")
                continue
            if failures:
                assessment.warnings.append(f"RPC_FAILOVER_USED:{provider.name}")
            return assessment
        raise ProviderError(
            "SOLANA_SAFETY_ALL_RPC_FAILED:" + ",".join(failures or ["UNKNOWN"])
        )
