from __future__ import annotations

from typing import Any

from memecoin_bot.models import SafetyAssessment, iso
from memecoin_bot.providers.base import ProviderError, ResilientJsonClient


class BscRpcProvider:
    name = "bsc_rpc"

    def __init__(self, rpc_url: str, client: ResilientJsonClient):
        self.rpc_url = rpc_url
        self.client = client
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
            raise ProviderError(f"BSC {method}: {data['error']}")
        return data.get("result")

    async def safety(self, token_address: str) -> SafetyAssessment:
        result = SafetyAssessment(checked_at=iso(), source=self.name, chain="bsc")
        if not token_address.startswith("0x") or len(token_address) != 42:
            result.rejection_reasons.append("INVALID_BSC_CONTRACT_ADDRESS")
            return result
        code = await self._rpc("eth_getCode", [token_address, "latest"])
        if not isinstance(code, str) or code in {"0x", "0x0", ""}:
            result.rejection_reasons.append("BSC_TOKEN_CONTRACT_NOT_FOUND")
            return result
        try:
            owner_raw = await self._rpc(
                "eth_call", [{"to": token_address, "data": "0x8da5cb5b"}, "latest"]
            )
            if isinstance(owner_raw, str) and len(owner_raw) >= 42:
                owner = "0x" + owner_raw[-40:]
                if int(owner[2:], 16) == 0:
                    result.warnings.append("BSC_OWNER_RENOUNCED")
                else:
                    result.mint_authority = owner
                    result.warnings.append("BSC_OWNER_ACTIVE")
            else:
                result.warnings.append("BSC_OWNER_ADMIN_STATE_UNKNOWN")
        except (ProviderError, ValueError):
            result.warnings.append("BSC_OWNER_ADMIN_STATE_UNKNOWN")
        result.warnings.extend(
            ["BSC_TRANSFER_RESTRICTIONS_UNKNOWN", "BSC_HOLDER_CONCENTRATION_UNKNOWN"]
        )
        return result


class ChainSafetyRouter:
    def __init__(self, providers: dict[str, object]):
        self.providers = providers
        self.name = "chain_safety_router"

    async def safety(self, chain: str, token_address: str) -> SafetyAssessment:
        provider = self.providers.get(chain)
        if provider is None:
            raise ProviderError(f"unsupported chain: {chain}")
        return await provider.safety(token_address)
