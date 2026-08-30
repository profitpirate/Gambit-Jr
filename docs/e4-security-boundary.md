# E4 signer and vault boundary

- The trading wallet signer is local to the runtime.
- No raw key, recovery phrase, login, or secret is stored in Git, SQLite, Discord, prompts, or the builder process.
- The transaction builder receives only public order parameters.
- The configured Phantom vault is a public destination only; Gambit has no ability to spend from it.
- The same signed transaction is raced across routes, preventing separate duplicate orders from one decision.
