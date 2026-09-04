import "./fast-preload-v4.mjs";

import {PUMP_SDK, creatorVaultPda} from "@pump-fun/pump-sdk";
import {TOKEN_PROGRAM_ID, getAssociatedTokenAddressSync} from "@solana/spl-token";

// @pump-fun/pump-sdk@1.36.0 bundles buy_exact_sol_in in its Anchor IDL, but
// PumpSdk does not expose a public wrapper for it. Reuse the SDK's own offline
// Anchor Program and PDA helper instead of duplicating the full account list or
// relying on a different package/version.
if (typeof PUMP_SDK.buyExactSolInInstruction !== "function") {
  const program = PUMP_SDK.offlinePumpProgram;
  if (!program?.methods?.buyExactSolIn) {
    throw new Error("installed Pump SDK IDL does not expose buyExactSolIn");
  }

  PUMP_SDK.buyExactSolInInstruction = async ({
    user,
    mint,
    creator,
    feeRecipient,
    solAmount,
    minTokenAmount,
    tokenProgram = TOKEN_PROGRAM_ID,
  }) => {
    const associatedUser = getAssociatedTokenAddressSync(
      mint,
      user,
      true,
      tokenProgram,
    );
    return await program.methods
      .buyExactSolIn(solAmount, minTokenAmount, {0: true})
      .accountsPartial({
        feeRecipient,
        mint,
        associatedUser,
        user,
        creatorVault: creatorVaultPda(creator),
        tokenProgram,
      })
      .instruction();
  };
}
