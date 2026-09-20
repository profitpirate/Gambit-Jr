"""HashiCorp Vault Transit Ed25519 signer for V12 Solana transactions."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import aiohttp
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction


@dataclass(frozen=True, slots=True)
class VaultRef:
    mount: str
    key: str


def parse_vault_ref(value: str) -> VaultRef:
    parsed = urlparse(value)
    if parsed.scheme != "vault":
        raise ValueError("signer reference must use vault://")
    mount = parsed.netloc.strip("/")
    key = parsed.path.strip("/")
    if not mount or not key:
        raise ValueError("vault signer reference must be vault://<mount>/<key>")
    return VaultRef(mount, key)


def _vault_token() -> str:
    token = os.getenv("VAULT_TOKEN", "").strip()
    token_file = os.getenv("VAULT_TOKEN_FILE", "").strip()
    if token:
        return token
    if token_file:
        return open(token_file, encoding="utf-8").read().strip()
    raise RuntimeError("VAULT_TOKEN or VAULT_TOKEN_FILE is required")


class VaultTransitSigner:
    def __init__(
        self,
        reference: VaultRef,
        *,
        address: str,
        token: str,
        namespace: str = "",
        timeout: float = 3.0,
    ):
        parsed = urlparse(address)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("VAULT_ADDR must be HTTPS")
        self.reference = reference
        self.address = address.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"X-Vault-Token": self.token}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                self.address + path,
                headers=self._headers(),
                json=payload,
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(
                        f"Vault HTTP {response.status}: {json.dumps(body)[:500]}"
                    )
                return body

    async def public_key(self) -> Pubkey:
        path = (
            f"/v1/{quote(self.reference.mount)}/keys/"
            f"{quote(self.reference.key, safe='')}"
        )
        payload = await self._request("GET", path)
        keys = (payload.get("data") or {}).get("keys") or {}
        latest = str((payload.get("data") or {}).get("latest_version") or "")
        row = keys.get(latest) or next(iter(keys.values()), {})
        encoded = str(row.get("public_key") or "")
        raw = base64.b64decode(encoded)
        if len(raw) != 32:
            raise RuntimeError("Vault Transit key is not raw Ed25519 public key")
        return Pubkey.from_bytes(raw)

    async def sign(
        self,
        transaction_b64: str,
        expected_public_key: str,
    ) -> tuple[str, str]:
        transaction = VersionedTransaction.from_bytes(
            base64.b64decode(transaction_b64)
        )
        public_key = await self.public_key()
        if str(public_key) != str(expected_public_key):
            raise RuntimeError("Vault key does not match expected Solana public key")

        message = bytes(transaction.message)
        path = (
            f"/v1/{quote(self.reference.mount)}/sign/"
            f"{quote(self.reference.key, safe='')}"
        )
        payload = await self._request(
            "POST",
            path,
            {
                "input": base64.b64encode(message).decode(),
                "marshaling_algorithm": "raw",
                "signature_algorithm": "pure",
            },
        )
        vault_signature = str((payload.get("data") or {}).get("signature") or "")
        encoded = vault_signature.rsplit(":", 1)[-1]
        raw_signature = base64.b64decode(encoded)
        if len(raw_signature) != 64:
            raise RuntimeError("Vault returned invalid Ed25519 signature length")
        signature = Signature.from_bytes(raw_signature)
        if not signature.verify(public_key, message):
            raise RuntimeError("Vault signature failed local verification")

        signatures = list(transaction.signatures)
        if not signatures:
            signatures = [signature]
        else:
            signatures[0] = signature
        signed = VersionedTransaction.populate(transaction.message, signatures)
        return base64.b64encode(bytes(signed)).decode(), str(signature)


async def sign_from_env(request: dict) -> dict:
    reference = parse_vault_ref(os.environ["V12_SIGNER_SECRET_REF"])
    signer = VaultTransitSigner(
        reference,
        address=os.environ["VAULT_ADDR"],
        token=_vault_token(),
        namespace=os.getenv("VAULT_NAMESPACE", ""),
        timeout=float(os.getenv("V12_VAULT_TIMEOUT_SECONDS", "3")),
    )
    signed, signature = await signer.sign(
        str(request["transaction_base64"]),
        str(request["expected_public_key"]),
    )
    return {
        "signed_transaction_base64": signed,
        "signature": signature,
    }


def main() -> int:
    raw = sys.stdin.buffer.readline()
    request = json.loads(raw)
    result = asyncio.run(sign_from_env(request))
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
