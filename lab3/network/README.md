# `network/` — IPv8 communities and wire protocol

The networking layer. Two IPv8 overlays plus the wire payloads and trusted-peer filtering
that connect the [`../blockchain/`](../blockchain/) core to the grading server and teammates.

| Module | Contents |
|---|---|
| `payloads.py` | every wire message (`@vp_compile` IPv8 dataclasses) |
| `peers.py` | `TrustedPeers` — server / teammate key filtering |
| `registering_community.py` | `RegisteringCommunity` — register the chain with the server |
| `blockchain_community.py` | `BlockchainCommunity` — the actual blockchain overlay |

All sends use IPv8's authenticated `ez_send`; every packet carries the sender's public key
and a signature. Inbound peers are filtered through `TrustedPeers`, so the server can't be
impersonated and untrusted peers are dropped.

## `peers.py`

`TrustedPeers` holds the server public key and the three teammate public keys (from
`config.py`) and answers `is_server(peer)` / `is_teammate(peer)` / `is_trusted(peer)`,
in both `Peer` and raw-`bytes` forms. This is the single trust gate used everywhere.

## `payloads.py`

All messages are `DataClassPayload`s with explicit `format_list` / `names` for the IPv8
serializer. Message IDs come from `config.py`.

**Registration community**

| ID | Message | Direction |
|---|---|---|
| 1 | `RegisterBlockchain` (group_id, community_id) | us → server |
| 2 | `RegisterResponse` (success, message) | server → us |

**Blockchain community — server-facing**

| ID | Message | Direction |
|---|---|---|
| 1 | `SubmitTransaction` (sender_key, data, timestamp, signature) | server → us |
| 2 | `SubmitTransactionResponse` (success, tx_hash, message) | us → server |
| 3 | `GetChainHeight` (request_id) | server → us |
| 4 | `ChainHeightResponse` (request_id, height, tip_hash) | us → server |
| 5 | `GetBlock` (height) | server → us |
| 6 | `BlockResponse` (full header + flat `tx_hashes`) | us → server |

**Blockchain community — inner (teammate ↔ teammate)**

| ID | Message | Purpose |
|---|---|---|
| 90 | `AnnounceBlock` (height, block_hash) | "I have a block at this height" |
| 91 | `RequestBlock` (height) | "send me the block at this height" |
| 92 | `BlockResponseInner` (full header + flat `tx_hashes`) | reply to `RequestBlock` |
| 93 | `MempoolTransaction` (sender_key, data, timestamp, signature) | gossip a pending tx |

`tx_hashes` in block responses is a flat concatenation of 32-byte hashes (the receiver
splits it into chunks and re-derives `txs_hash`); `b""` for an empty block.

## `registering_community.py`

`RegisteringCommunity` runs on the registration community ID. It watches for the server
peer (by key), then sends `RegisterBlockchain`. On the response it either marks the group
as passed (sticky, stops) or schedules a **re-registration** after
`RE_REGISTER_INTERVAL_S` (420s — longer than the server's ~5-minute attempt window, so a
re-register never cuts a running verification short). This keeps retrying until the pass
email lands.

## `blockchain_community.py`

`BlockchainCommunity` is the heart of the node. Dependencies (chain, mempool,
trusted_peers, difficulty_policy) are injected through IPv8 settings; it owns a `Miner`.

**On submit transaction** (from server) — verify signature via
`ECCrypto.key_from_public_bin`, add to mempool, reply with the tx hash, then gossip the tx
to teammates (`MempoolTransaction`).

**Mempool sync** — gossiped transactions are verified, added, and relayed once
(excluding the sender); a freshly discovered teammate is sent the whole current mempool.

**Chain sync** — a periodic task (`_sync_chains`, every 10s) asks teammates for their
height:
- peer **ahead** → request its missing blocks one height at a time until caught up;
- peer **behind** → announce our tip;
- **same height, different tip** → request the peer's tip to inspect the fork.
Orphans trigger a backfill request for the parent height. Every received block goes
through `Blockchain.add_block`, and `_reconcile_after_chain_update` returns reverted txs to
the mempool / removes applied ones and restarts the miner on the new tip.

**Mining** — the miner callback fires on a worker thread and is bounced onto the event
loop with `call_soon_threadsafe`. A mined block is added, reconciled, then announced to all
teammates. Tie-break losers (block landed on a side branch) still restart the miner so the
node never stalls.
