const CSRF_KEY = "gambit_csrf";

const $ = (id) => document.getElementById(id);
const csrf = () => sessionStorage.getItem(CSRF_KEY) || "";

function base58(bytes) {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const digits = [0];
  for (const byte of bytes) {
    let carry = byte;
    for (let i = 0; i < digits.length; i += 1) {
      const v = digits[i] * 256 + carry;
      digits[i] = v % 58;
      carry = Math.floor(v / 58);
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
  const method = String(options.method || "GET").toUpperCase();
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (method !== "GET" && csrf()) headers["X-CSRF-Token"] = csrf();
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
  });
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401) sessionStorage.removeItem(CSRF_KEY);
    throw new Error(body.error || ("HTTP " + response.status));
  }
  return body;
}

function text(node, v) {
  node.textContent = String(v ?? "");
}

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

function value(v, digits = 4) {
  const number = Number(v);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function renderDl(root, rows) {
  root.replaceChildren();
  for (const row of rows) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    text(dt, row[0]);
    text(dd, row[1]);
    root.append(dt, dd);
  }
}

function renderTable(root, columns, rows) {
  root.replaceChildren();
  if (!rows || rows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted";
    text(empty, "No records.");
    root.append(empty);
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  for (const column of columns) {
    const th = document.createElement("th");
    text(th, column[0]);
    header.append(th);
  }
  head.append(header);
  table.append(head);

  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const column of columns) {
      const td = document.createElement("td");
      text(td, column[1](row));
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(body);
  root.append(table);
}

async function redeemCode(code) {
  const body = await api("/v1/auth/redeem", {
    method: "POST",
    body: JSON.stringify({code}),
  });
  sessionStorage.setItem(CSRF_KEY, body.csrf);
  history.replaceState({}, "", "/");
}

function renderProviders(providers) {
  const root = $("providers");
  root.replaceChildren();
  for (const provider of providers) {
    const card = document.createElement("article");
    card.className = "provider";
    const name = document.createElement("strong");
    text(name, provider.id.replaceAll("_", " "));
    const state = document.createElement("span");
    state.className = provider.state.startsWith("SUPPORTED") ? "pill good" : "pill";
    text(state, provider.state);
    const note = document.createElement("p");
    note.className = "muted";
    if (provider.id === "AXIOM") {
      text(note, "Direct Axiom authorization is intentionally disabled until an official third-party authorization flow exists.");
    } else if (provider.id === "MANAGED_WALLET") {
      text(note, "Isolated trading wallet provisioned behind Vault Transit. Raw private keys never enter this browser or the trading process.");
    } else {
      text(note, "Wallet ownership is verified separately; live execution requires a Vault-backed signer provisioned by the operator.");
    }
    card.append(name, state, note);
    root.append(card);
  }
}

async function loadRuntime() {
  const body = await api("/v1/runtime/status");
  const runtime = body.runtime;
  const execution = runtime.execution || {};
  const safety = execution.safety || {};
  renderDl($("runtime-summary"), [
    ["Process", runtime.running ? "running" : "stopped"],
    ["Kill switch", runtime.kill_switch ? "TRIPPED" : "clear"],
    ["Safety", safety.mode || "not started"],
    ["Reason", safety.reason || "—"],
    ["Closed trades", execution.closed_trades ?? 0],
    ["Closed P&L", value(execution.closed_pnl_sol, 6) + " SOL"],
  ]);

  renderTable(
    $("open-positions"),
    [
      ["Mint", (row) => row.mint],
      ["Status", (row) => row.status],
      ["Entry", (row) => value(row.entry_sol, 4) + " SOL"],
      ["Realized", (row) => value(row.realized_sol, 4) + " SOL"],
    ],
    execution.open_positions || []
  );

  renderTable(
    $("recent-trades"),
    [
      ["Mint", (row) => row.mint],
      ["P&L", (row) => value(row.pnl_sol, 6) + " SOL"],
      ["Entry", (row) => value(row.entry_sol, 4) + " SOL"],
    ],
    execution.recent_trades || []
  );

  renderTable(
    $("storage-sweeps"),
    [
      ["Amount", (row) => value(row.amount, 6) + " SOL"],
      ["Confirmed", (row) => row.confirmed ? "yes" : "no"],
      ["Signature", (row) => row.signature || "—"],
    ],
    execution.storage_sweeps || []
  );
}

async function loadAccount() {
  const responses = await Promise.all([api("/v1/me"), api("/v1/providers")]);
  const account = responses[0].account;
  const providerBody = responses[1];
  hide("login-view");
  show("app-view");
  text($("auth-state"), "Discord " + account.user.discord_user_id);

  renderDl($("account"), [
    ["Account ID", account.user.id],
    ["Access", account.user.enabled ? "enabled" : "disabled"],
  ]);

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
  await loadRuntime();
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
    await loadAccount();
  } catch (error) {
    text(result, error.message);
  }
}

async function killRuntime() {
  const result = $("runtime-result");
  try {
    await api("/v1/runtime/kill", {method: "POST", body: "{}"});
    text(result, "Emergency stop requested. Existing positions are handled by the live safety layer.");
    await loadRuntime();
  } catch (error) {
    text(result, error.message);
  }
}

async function resumeRuntime() {
  const result = $("runtime-result");
  try {
    await api("/v1/runtime/resume", {method: "POST", body: "{}"});
    text(result, "Resume requested. Production readiness checks still control whether the runtime can arm.");
    await loadRuntime();
  } catch (error) {
    text(result, error.message);
  }
}

async function logout() {
  try {
    await api("/v1/auth/logout", {method: "POST", body: "{}"});
  } finally {
    sessionStorage.removeItem(CSRF_KEY);
    location.replace("/");
  }
}

async function boot() {
  $("connect-wallet").addEventListener("click", verifyPhantom);
  $("mandate-form").addEventListener("submit", saveMandate);
  $("runtime-kill").addEventListener("click", killRuntime);
  $("runtime-resume").addEventListener("click", resumeRuntime);
  $("logout").addEventListener("click", logout);

  const code = new URLSearchParams(location.search).get("code");
  try {
    if (code) await redeemCode(code);
    await loadAccount();
    window.setInterval(() => loadRuntime().catch(() => {}), 5000);
  } catch (error) {
    sessionStorage.removeItem(CSRF_KEY);
    text($("auth-state"), "invite required");
    const paragraph = $("login-view").querySelector("p");
    text(paragraph, error.message || "Open your one-time Discord login link.");
    show("login-view");
    hide("app-view");
  }
}

window.addEventListener("DOMContentLoaded", boot);
