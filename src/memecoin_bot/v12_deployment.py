"""Deployment fingerprint helpers for Gambit V12 production runtime."""
from __future__ import annotations

import os
from typing import Any


_UNVERIFIED = {"", "UNVERIFIED", "UNKNOWN", "NONE", "N/A"}


def deployment_fingerprint() -> dict[str, Any]:
    sha = (
        os.getenv("V12_DEPLOYED_GIT_SHA")
        or os.getenv("V12_BUILD_SHA")
        or os.getenv("GIT_COMMIT_SHA")
        or ""
    ).strip()
    ref = (
        os.getenv("V12_DEPLOYED_GIT_REF")
        or os.getenv("V12_BUILD_REF")
        or ""
    ).strip()
    image = os.getenv("V12_DEPLOYED_IMAGE_ID", "").strip()
    normalized = sha.upper()
    verified = normalized not in _UNVERIFIED and len(sha) >= 7
    return {
        "git_sha": sha or "UNVERIFIED",
        "git_ref": ref or "UNVERIFIED",
        "image_id": image or "UNVERIFIED",
        "verified": verified,
    }
