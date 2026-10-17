"""Pinned creator-library recognition with explicit owner activation and no auto-buy."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from types import MappingProxyType

from solders.pubkey import Pubkey

from .v12_creator_library import CreatorLibrary, lifetime_history_certified
from .v12_prearmed_policy import FrozenPrearmedPolicy

LIBRARY_PATH = "models/e4/v12-creator-library.json"
ACTIVATION_PATH = "models/e4/v12-prearmed-library-activation.json"


class PrearmedLibrary:
    def __init__(self, payload, activation, model):
        # This activation records the owner's requested extension separately from
        # the library's historical certification, which must remain truthful.
        if activation.get("schema") != 1 or activation.get("mode") != "PROMOTED_RECOGNITION":
            raise ValueError("LIBRARY_ACTIVATION_INVALID")
        if activation.get("basis") != "OWNER_REQUEST" or activation.get("auto_buy") is not False:
            raise ValueError("LIBRARY_ACTIVATION_INVALID")
        if activation.get("preserve_entry_and_risk_rules") is not True:
            raise ValueError("LIBRARY_ENTRY_RULES_REQUIRED")
        if activation.get("creator_recognition_rule") != "PROMOTED_OR_FROZEN_CREATOR_HANDLE":
            raise ValueError("LIBRARY_RECOGNITION_RULE_REQUIRED")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if activation.get("library_sha256") != digest:
            raise ValueError("LIBRARY_ACTIVATION_CHECKSUM_MISMATCH")
        seen = set()
        for bucket, status, authority in (
            ("promoted", "PROMOTED", "RECOGNISED_CREATOR_ONLY"),
            ("shortlisted", "SHORTLISTED", "RESEARCH_ONLY"),
        ):
            rows = payload.get(bucket)
            if not isinstance(rows, list):
                raise TypeError("LIBRARY_RECORDS_INVALID")
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("LIBRARY_RECORD_INVALID")
                creator = row.get("creator")
                try:
                    Pubkey.from_string(creator)
                except (ValueError, TypeError):
                    raise ValueError("LIBRARY_CREATOR_INVALID") from None
                if creator in seen:
                    raise ValueError("LIBRARY_DUPLICATE_CREATOR")
                seen.add(creator)
                score = row.get("quality_score")
                if (
                    type(score) not in (int, float)
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ValueError("LIBRARY_SCORE_INVALID")
                if (
                    row.get("status") != status
                    or row.get("selection_authority") != authority
                    or row.get("auto_buy") is not False
                ):
                    raise ValueError("LIBRARY_AUTHORITY_INVALID")
        library = CreatorLibrary(payload)
        self.promoted = MappingProxyType(dict(library.promoted))
        self.shortlisted = MappingProxyType(dict(library.shortlisted))
        self.frozen = frozenset(model["creator_handles"])
        self.handles = MappingProxyType(
            {
                creator: frozenset(str(h).lower().lstrip("@") for h in handles)
                for creator, handles in model["creator_handles"].items()
            }
        )
        self.sha256 = digest
        self.history_certified = lifetime_history_certified(payload)

    @classmethod
    def from_root(cls, root: Path, model):
        # Both data files must be covered by the signed release manifest.
        manifest = json.loads((root / "release-manifest.json").read_text())
        documents = []
        for name in (LIBRARY_PATH, ACTIVATION_PATH):
            path = root / name
            if path.resolve() != path or path.is_symlink():
                raise ValueError("LIBRARY_PATH_INVALID")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest.get("files", {}).get(name):
                raise ValueError("LIBRARY_RELEASE_CHECKSUM_MISMATCH")
            documents.append(json.loads(raw))
        return cls(*documents, model)

    def recognises(self, creator):
        return creator in self.frozen or creator in self.promoted

    def describe(self, creator):
        row = self.promoted.get(creator) or self.shortlisted.get(creator)
        return {
            "creator": creator,
            "library_status": row.status if row else "NOT_LISTED",
            "library_tier": row.tier if row else "NONE",
            "creator_recognised": self.recognises(creator),
            "creator_source": "FROZEN_MODEL"
            if creator in self.frozen
            else ("PROMOTED_LIBRARY" if creator in self.promoted else "NONE"),
            "prearmed_social_available": bool(self.handles.get(creator)),
            "historical_social_required": creator not in self.promoted,
            "library_history_certified": self.history_certified,
            "library_sha256": self.sha256,
        }

    def status(self):
        additional = set(self.promoted) - self.frozen
        return {
            "state": "CONNECTED",
            "mode": "PROMOTED_RECOGNITION",
            "activation_basis": "OWNER_REQUEST",
            "history_certified": self.history_certified,
            "sha256": self.sha256,
            "promoted": len(self.promoted),
            "shortlisted": len(self.shortlisted),
            "elite": sum(row.elite for row in self.promoted.values()),
            "additional_creators": len(additional),
            "recognised_creators": len(self.frozen | set(self.promoted)),
            "promoted_missing_social_history": sum(not self.handles.get(c) for c in self.promoted),
            "promoted_social_history_required": False,
            "auto_buy": False,
        }


class LibraryPrearmedPolicy(FrozenPrearmedPolicy):
    """Match should_select_nextgen recognition; keep launch and execution guards."""

    __slots__ = ("library",)
    family = "prearmed_promoted_creator_extension"

    def __init__(self, model, library):
        super().__init__(model)
        self.library = library

    def creator_known(self, creator):
        return isinstance(creator, str) and self.library.recognises(creator)

    def social_handle_known(self, creator, handle):
        # Promotion is the alternative recognition path in the existing nextgen
        # selector. A current social status is still parsed and age-checked.
        if creator in self.library.promoted:
            return isinstance(handle, str) and bool(re.fullmatch(r"[a-z0-9_]{1,15}", handle.lower().lstrip('@')))
        return super().social_handle_known(creator, handle)


def validate_creator_query(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value):
        raise ValueError("Enter a full creator wallet address.")
    try:
        Pubkey.from_string(value)
    except ValueError:
        raise ValueError("Enter a valid creator wallet address.") from None
    return value
