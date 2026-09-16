"""Direct-Solana forward-paper adapter for the six-arm 2s-loss A/B campaign."""
from __future__ import annotations

import golden_top4_solana as base
from golden_loss2s_engine import ARMS, VERSION, CampaignEngine

# Reuse the already-hardened direct-Solana/E4 collector and provenance checks,
# but inject the fresh six-arm paper engine. No signer/broadcast path is added.
base.CampaignEngine = CampaignEngine
base.OriginalEngine = CampaignEngine
base.ARMS = ARMS
base.VERSION = VERSION

run = base.run
verify_manifest = base.verify_manifest


def main():
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
