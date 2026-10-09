# Lab 3 — Proof-of-Work Blockchain over IPv8

A 3-node Proof-of-Work blockchain built on [py-ipv8](https://github.com/Tribler/py-ipv8).
Each group member runs one node. The nodes mine blocks, gossip them, converge on a
single chain via the longest-chain rule, and answer queries from the Lab 3 grading
server. See [`assignment_3.md`](assignment_3.md) for the full assignment spec.

## What it does

1. **Registers** the blockchain's community ID with the grading server on a separate
   *registration* community.
2. The server then joins the *blockchain* community, submits a signed test transaction,
   and walks all three chains to verify PoW, header linking, body commitment, and 3-way
   consistency.
3. The node answers the server's queries (submit tx, get height, get block) and keeps
   mining so the test transaction ends up buried under ≥3 confirmations on every node.

## Layout

```
assignment_3/
├─ client.py              # entry point: wires deps, builds IPv8, starts overlays
├─ config.py              # all constants: keys, community IDs, message IDs, tunables
├─ assignment_3.md        # assignment specification
│
├─ blockchain/            # chain logic, no networking  → blockchain/README.md
│  ├─ chain.py            #   Transaction, Block, Blockchain (fork tree)
│  ├─ crypto.py           #   header packing, hashing, PoW search
│  ├─ difficulty.py       #   Fixed / Dynamic (EMA) difficulty policies
│  ├─ mempool.py          #   pending-transaction pool with eviction
│  ├─ miner.py            #   multi-threaded nonce search
│  └─ storage.py          #   InMemory / crash-safe IndexedStorage persistence
│
├─ network/              # IPv8 communities and wire payloads → network/README.md
│  ├─ blockchain_community.py   # the blockchain overlay (mining, sync, server queries)
│  ├─ registering_community.py  # registration overlay
│  ├─ payloads.py               # all wire message definitions
│  └─ peers.py                  # trusted-peer (teammate / server) key filtering
│
├─ test/                 # pytest unit tests → test/README.md
└─ wal/                  # per-node chain storage (chain.blocks + chain.index) → wal/README.md
```

## Run

The package uses relative imports, so run it as a module from the **repository root**
(one level above `assignment_3/`):

```bash
# start a node (registers with the server by default)
uv run python -m assignment_3.client

# use a specific IPv8 key (each of the 3 members runs with their own)
uv run python -m assignment_3.client --key_path assignment_3/key.pem

# wipe local chain storage and start from genesis
uv run python -m assignment_3.client --start_fresh
```

### CLI flags (`client.py`)

| Flag | Effect |
|---|---|
| `--key_path PATH` | IPv8 key file to load (default `assignment_3/key.pem`). The storage dir is derived from the key file stem, so two nodes on one machine never clobber each other's chain. |
| `--register` | Flag stored with `store_false` → registration is **on** by default; passing `--register` *disables* it and additionally starts the `RegisteringCommunity` overlay. |
| `--start_fresh` | Delete the node's `chain.blocks` / `chain.index` (and tmp files) before booting so it rebuilds from genesis. |

## Run the tests

```bash
uv run pytest assignment_3/test -v
```

`pyproject.toml` sets `pythonpath = ["assignment_3"]` for the test runner.

## How it fits together

```
                      registration community
   our node  ──RegisterBlockchain──▶  grading server
            ◀──RegisterResponse────

                       blockchain community
   server ──SubmitTransaction──▶ our node ──mempool──▶ Miner (threads)
                                     │                    │ mines block
          ◀─SubmitTransactionResponse                     ▼
   server ──GetChainHeight / GetBlock─▶ our node    add to Blockchain (fork tree)
          ◀──ChainHeightResponse / BlockResponse──        │
                                                          ▼ announce
   teammate ◀─AnnounceBlock / RequestBlock / BlockResponseInner─▶ teammate
   teammate ◀─────────────── MempoolTransaction ───────────────▶ teammate
```

- **Server-facing** messages (IDs 1–6) answer the grader.
- **Inner** messages (IDs 90–93) sync chain and mempool between the three teammate nodes.
- Every packet is sent with IPv8's authenticated `ez_send`; peers are filtered by public
  key via `TrustedPeers`, so the server is never impersonated and untrusted peers are
  ignored.

## Design highlights

- **Fork handling** (`blockchain/chain.py`) — blocks form a tree; the best tip is chosen
  by `(height, smallest-hash)` so all three nodes converge deterministically on ties.
  Orphans (parent not yet seen) are parked and adopted recursively when the parent lands.
- **Multi-threaded mining** (`blockchain/miner.py`) — workers split the nonce space by
  interleaving (`step = num_threads`); the first to find a block pauses the rest until the
  next tip is set, so there's no busy-spin or duplicate emission.
- **Pluggable difficulty** (`blockchain/difficulty.py`) — `FixedDifficultyPolicy` is used
  in production; `DynamicDifficultyPolicy` adjusts via an exponential moving average of
  block times (kept off because exponential work growth can freeze the tip).
- **Crash-safe storage** (`blockchain/storage.py`) — `IndexedStorage` keeps an append-only
  `chain.blocks` (source of truth, per-record CRC32) plus a rebuildable `chain.index` for
  O(1) reads by height. A torn or bit-flipped tail is healed on boot; a background daemon
  compacts dead bytes without blocking appends; old blocks are pruned to header-only while
  staying verifiable. See [`blockchain/README.md`](blockchain/README.md#storagepy).
- **Re-registration loop** (`network/registering_community.py`) — re-registers after the
  server's attempt window elapses until the group's pass is recorded.
