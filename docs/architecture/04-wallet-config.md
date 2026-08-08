# Wallet configuration

The wallet is system-level: registered once via the Wallet panel.
**Paper-mode runs (M1–M3) need no wallet at all** — paper uses a virtual book.

openPoly is a single system, so there is **one wallet** — no per-run wallets,
no sidecar executor (those belonged to the dropped heavy-isolation design; see
[01-isolation.md](01-isolation.md)).

## Storage (current — Polymarket V2 DepositWallet model)

Wallet config does not get DB tables. Polymarket's V2 upgrade
(post-2026-04-28) replaced the old mnemonic + on-the-fly BIP-44 derivation
model with a **DepositWallet smart contract** (holds pUSD collateral + CTF
positions) plus a **separate EOA that signs orders** — the CLOB validates
orders against the funder address via EIP-1271 (`signature_type=3`
POLY_1271), so signer != funder is the expected shape, not a
misconfiguration. See `openpoly/wallet/runtime_state.py`'s `WalletSpec` for
the authoritative definition. The split is:

| Data | Where | Why |
|---|---|---|
| Signer EOA private key (secret) | Secret store, referenced as `private_key_ref` (e.g. `env:OPENPOLY_POLYMARKET_PK` in `.env`, or `local:<name>`) | Open-source norm: secrets in `.env` (gitignored) or the local secret store, not in app-managed files |
| DepositWallet contract address | `funder_address` — a plain (non-secret) on-chain address; must be a contract, the CLOB rejects EOA funders post-V2 | Public identity, not a secret |
| `private_key_ref` + `funder_address` selection + `exec_mode` | `~/.openpoly/runtime.json` (dotfile, like `canvas.json` / `secrets.json`) | Mutable from UI without DB migration; symmetric with existing dotfile pattern |

`OPENPOLY_RUNTIME_STATE` env var can override the dotfile path (tests).

## Live execution audit trail

The originally-planned standalone `wallet_tx_log` table was dropped —
`openpoly/execution/live_executor.py` (`LiveExecutor`, built on
`py-clob-client`) instead extends the existing `fill` table with
`order_id`/`tx_hash` columns (see `openpoly/db/tables.py`'s `FillRow`), so a
live fill's on-chain identity rides the same append-only ledger every paper
fill already writes to, rather than a second audit table to keep in sync.

The originally-planned `wallet_config` and `wallet_state` tables were also
dropped — the dotfile + on-demand key resolution already cover what they
would have held. If balance caching becomes a hot-path concern (e.g.
dashboard polling), revisit `wallet_state` then.

## Prod (M4)

Live trading (M4) signs orders with the signer EOA's private key, resolved
via `private_key_ref` and materialized in memory only when a signature is
needed. Intended for grain-scale capital ($5–$50), which itself caps blast
radius. Paper runs never touch a wallet.
