const SESSION_KEY = "gambit_session";

const $ = (id) => document.getElementById(id);
const session = () => sessionStorage.getItem(SESSION_KEY);

function base58(bytes) {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const digits = [0];
  for (const byte of bytes) {
    let carry = byte;
    for (let i = 0; i < digits.length; i += 1) {
      const value = digits[i] * 256 + carry;
      digits[i] = value % 58;
      carry = Math.floor(value / 58);
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = Math.floor(carry / 58);
    }
  }
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros += 1;
  return "1".repeat(zeros) + digits.reverse().map((x) => alphabet[x]).join("");
}

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (session()) headers.Authorization = \`Bearer \${session()}\`;
  const response = await fetch(path, {...options, headers});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || \`HTTP \${response.status}\`);
  return body;
}

function text(node, value) {
  node.textContent = String(value ?? "");
}

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

async function redeemCode(code) {
  const body = await api("/v1/auth/redeem", {
    method: "POST",
    body: JSON.stringify({code}),
  });
  sessionStorage.setItem(SESSION_KEY, body.session);
  history.replaceState({}, "", "/");
}

function renderProviders(providers) {
  const root = $("providers");
  root.replaceChildren();
  for (const provider of providers) {
    const card = document.createElement("article");
    card.className = "provider";
    const name = document.createElement("strong");
    text(name, provider.id === "NATIVE_WALLET" ? "Native wallet" : "Axiom");
    const state = document.createElement("span");
    state.className = provider.state === "SUPPORTED" ? "pill good" : "pill";
    text(state, provider.state);
    const note = document.createElement("p");
    note.className = "muted";
    text(
      note,
      provider.id === "AXIOM"
        ? "Direct Axiom account authorization is intentionally disabled until an official third-party authorization path exists."
        : "Ownership can be verified now. Trading authority is configured separately through a delegated signer/vault reference."
    );
    card.append(name, state, note);
    root.append(card);
  }
}

async function loadAccount() {
  const [{account}, providerBody] = await Promise.all([
    api("/v1/me"),
    api("/v1/providers"),
  ]);
  hide("login-view");
  show("app-view");
  text($("auth-state"), \`Discord \${account.user.discord_user_id}\`);

  const dl = $("account");
  dl.replaceChildren();
  for (const [label, value] of [
    ["Account ID", account.user.id],
    ["Access", account.user.enabled ? "enabled" : "disabled"],
  ]) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    text(dt, label); text(dd, value);
    dl.append(dt, dd);
  }

  const wallets = $("wallets");
  wallets.replaceChildren();
  for (const wallet of account.wallets) {
    const li = document.createElement("li");
    text(li, wallet.wallet);
    wallets.append(li);
  }
  renderProviders(providerBody.providers);

  if (account.mandate) {
    $("bankroll").value = account.mandate.max_active_bankroll_sol;
    $("fraction").value = account.mandate.max_position_fraction;
    $("concurrency").value = account.mandate.max_concurrent_positions;
    $("storage-wallet").value = account.mandate.storage_wallet || "";
    $("mandate-enabled").checked = Boolean(account.mandate.enabled);
  }
}

async function verifyPhantom() {
  const result = $("wallet-result");
  try {
    const provider = window.phantom?.solana || window.solana;
    if (!provider?.isPhantom) throw new Error("Phantom wallet extension not detected");
    const connected = await provider.connect();
    const wallet = connected.publicKey.toString();
    const challenge = await api("/v1/wallet/challenge", {
      method: "POST",
      body: JSON.stringify({wallet}),
    });
    const signed = await provider.signMessage(
      new TextEncoder().encode(challenge.message),
      "utf8"
    );
    const signature = base58(new Uint8Array(signed.signature));
    await api("/v1/wallet/verify", {
      method: "POST",
      body: JSON.stringify({
        challenge_id: challenge.challenge_id,
        signature,
        label: "Phantom",
      }),
    });
    text(result, "Wallet ownership verified.");
    await loadAccount();
  } catch (error) {
    text(result, error.message);
  }
}

async function saveMandate(event) {
  event.preventDefault();
  const result = $("mandate-result");
  try {
    await api("/v1/mandate", {
      method: "POST",
      body: JSON.stringify({
        enabled: $("mandate-enabled").checked,
        max_active_bankroll_sol: Number($("bankroll").value),
        max_position_fraction: Number($("fraction").value),
        max_concurrent_positions: Number($("concurrency").value),
        storage_wallet: $("storage-wallet").value.trim() || null,
      }),
    });
    text(result, "Mandate saved.");
  } catch (error) {
    text(result, error.message);
  }
}

async function boot() {
  $("connect-wallet").addEventListener("click", verifyPhantom);
  $("mandate-form").addEventListener("submit", saveMandate);
  $("logout").addEventListener("click", () => {
    sessionStorage.removeItem(SESSION_KEY);
    location.replace("/");
  });

  const code = new URLSearchParams(location.search).get("code");
  try {
    if (code) await redeemCode(code);
    if (!session()) {
      text($("auth-state"), "invite required");
      show("login-view");
      return;
    }
    await loadAccount();
  } catch (error) {
    sessionStorage.removeItem(SESSION_KEY);
    text($("auth-state"), "access failed");
    text($("login-view").querySelector("p"), error.message);
    show("login-view");
  }
}

window.addEventListener("DOMContentLoaded", boot);
