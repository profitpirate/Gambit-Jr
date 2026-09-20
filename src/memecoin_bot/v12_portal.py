from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from memecoin_bot.access_service import AccessService
from memecoin_bot.access_store import AccessStore
from memecoin_bot.control_plane import build_from_env


def main() -> None:
    store = AccessStore(
        Path(os.getenv("GAMBIT_ACCESS_DB", "data/v12-access.db"))
    )
    access = AccessService(
        store,
        portal_base_url=os.getenv(
            "GAMBIT_PORTAL_BASE_URL",
            "http://127.0.0.1:8090",
        ),
        login_ttl_seconds=int(os.getenv("GAMBIT_LOGIN_TTL_SECONDS", "600")),
        session_ttl_seconds=int(os.getenv("GAMBIT_SESSION_TTL_SECONDS", "86400")),
    )
    plane = build_from_env(access)
    try:
        web.run_app(
            plane.app(),
            host=os.getenv("GAMBIT_PORTAL_HOST", "127.0.0.1"),
            port=int(os.getenv("GAMBIT_PORTAL_PORT", "8090")),
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
