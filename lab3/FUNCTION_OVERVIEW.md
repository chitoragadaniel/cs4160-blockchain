# Assignment 3 — Function Overview

Per-file breakdown of every Python module in `assignment_3/`, with what each function / method does. Organised by package.

---

## Top Level

### `client.py`

Entry point. Wires up storage, chain, mempool, miner, difficulty policy and the IPv8 communities, then runs the node.

| Function | Does |
|---|---|
| `_UnsupportedCurveFilter.filter(record)` | Logging filter; drops noisy "Curve X is not supported" messages from old peers. |
| `main()` | Parses CLI args (`--key_path`, `--register`, `--start_fresh`). Builds per-key storage dir, optionally wipes old chain files, creates `IndexedStorage` + `Blockchain` + `Mempool` + `TrustedPeers` + `FixedDifficultyPolicy`, starts compaction worker, configures IPv8 overlays (`BlockchainCommunity` always, `RegisteringCommunity` optionally), starts IPv8 and idles forever. |

> **CLI flags note:** `--register` uses `store_false`, so passing it disables registration (default = register).

---

### `config.py`

Constants only, no functions. Holds: community IDs, server/teammate public keys, group id, key file path, message-id enums (registration / blockchain / inner), confirmation count, timeouts, and dynamic-difficulty params (`DEFAULT_DIFFICULTY=26`, `MIN/MAX_DIFFICULTY`, `TARGET_BLOCK_TIME_S=60`, `FUTURE_DRIFT_S`, `EMA_WINDOW`, `_ALPHA`, `MAX_ADJUSTMENT`).

> **Note:** `MSG_ANNOUNCE_BLOCK` is defined twice (7 then 90); the inner value 90 wins.

---

## `blockchain/`

### `crypto.py`

Pure crypto / serialization helpers. No state.

| Function | Does |
|---|---|
| `serialize_header(prev_hash, txs_hash, timestamp, difficulty, nonce)` | Pack a header into exactly 84 bytes (`>32s32sQIQ`). |
| `deserialize_header(raw)` | Unpack 84 bytes back into the 5-tuple. |
| `hash_header(...)` | SHA-256 of the packed header. |
| `sha256(data)` | Raw SHA-256 digest. |
| `serialize_transaction(sender_key, timestamp, signature, tx_hash, data)` | Pack fixed 136-byte tx prefix + arbitrary trailing data. |
| `deserialize_transaction(raw)` | Split the fixed prefix from the trailing data. |
| `hash_transaction(sender_key, data, timestamp, signature)` | SHA-256 over `sender_key + data + 8-byte timestamp + signature`. |
| `compute_txs_hash(tx_hashes)` | SHA-256 over concatenated tx hashes. Empty block = `SHA256(b"")`, not zero bytes. |
| `count_leading_zero_bits(data)` | Number of leading zero bits (uses precomputed `LEADING_ZEROS_BIT_TABLE`). |
| `satisfies_pow(block_hash, difficulty)` | `True` if hash has ≥ difficulty leading zero bits. |
| `mine_block(prev_hash, txs_hash, timestamp, difficulty, start_nonce, count, step)` | Scan nonces for one satisfying PoW; returns `(nonce, hash)` or `(None, None)`. `step` enables interleaved multi-thread search. |

---

### `chain.py`

Core data model and the block-tree with longest-chain selection.

#### `Transaction` (frozen dataclass)

| Method | Does |
|---|---|
| `to_dict()` | Serialise to primitive dict. |
| `__repr__()` | Short `<Tx …>`. |

#### `Block` (frozen dataclass)

| Method | Does |
|---|---|
| `to_dict()` | Serialise block + its transactions to a dict. |
| `_dict_to_block(d)` *(static)* | Rebuild a `Block` from a dict. |
| `_decode(data)` *(static)* | msgpack bytes → `Block`. |
| `_encode()` | `Block` → msgpack bytes. |
| `verify_header()` | `block_hash` hashes the header and satisfies PoW. |
| `verify_pow()` | Alias of `verify_header()`. |
| `verify_txs_hash()` | Recompute `txs_hash` from tx hashes and compare. |
| `has_body` *(property)* | `True` if we hold the tx bodies (or it's genuinely empty). |
| `is_valid()` | Header consistent; if bodies present, `txs_hash` must match. Header-only synced blocks pass on header alone. |
| `tx_hashes` *(property)* | List of contained tx hashes. |
| `__repr__()` | Short block summary. |

#### Module-level functions / classes

| Name | Does |
|---|---|
| `make_genesis_block(difficulty, prev_hash, height)` | Deterministic, `lru_cached` genesis block (zeros `prev_hash`, empty `txs_hash`, `ts=0`, mined nonce). |
| `AddResult` *(dataclass)* | Outcome of `add_block`: `added`, `extended_tip`, `is_orphan`, `missing_parent`, `reverted`, `applied`. `__bool__` returns `added`. |
| `_chain_score(block)` | Best-chain ordering key `(height, inverted_hash)` — taller wins, smaller hash wins ties; deterministic across nodes. |

#### `Blockchain`

| Method | Does |
|---|---|
| `__init__(genesisses, max_orphans, storage)` | Accepts single/multiple genesis blocks; loads from storage if present else seeds fresh; sets up `_blocks`, `_chain` (main path), `_orphans`. |
| `height` *(prop)* | Max height in main chain. |
| `tip` *(prop)* | Block at max height. |
| `get_block(height)` | Block at height on main chain, or `None`. |
| `get_block_by_hash(hash)` | Any connected block by hash, or `None`. |
| `contains(hash)` | `True` if hash is on the current main chain. |
| `knows(hash)` | `True` if seen at all (connected or parked orphan). |
| `add_block(block)` | Validate, dedup, connect to parent or park as orphan, cascade waiting orphans, then reselect best tip. Returns `AddResult`. |
| `_select_best_tip()` | Pick highest-scoring tip; fast-append if it directly extends, else full `_reorg`. |
| `_add_orphan(block)` | Park an unconnected valid block (bounded by `max_orphans`). |
| `_store_and_cascade(block)` | Store block, then recursively adopt orphans waiting on it (and their children). |
| `_reorg(new_tip)` | Rebuild main chain genesis→new_tip; compute reverted/applied; rewrite storage. |
| `__repr__()` | Chain summary. |

---

### `difficulty.py`

Difficulty policies.

| Class / method | Does |
|---|---|
| `DifficultyPolicy.get_difficulty(tip)` | Interface stub. |
| `FixedDifficultyPolicy.get_difficulty(tip)` | Always returns a fixed difficulty. |
| `DynamicDifficultyPolicy.__init__(get_block)` | EMA-based adaptive policy; stateful (`_ema`, `_last_height`). |
| `DynamicDifficultyPolicy.get_difficulty(tip)` | Update EMA once per height, compute clamped ratio (`TARGET/EMA`), scale tip difficulty, clamp to `[MIN, MAX]`. Genesis → `DEFAULT_DIFFICULTY`. |
| `DynamicDifficultyPolicy._update_ema(tip)` | Update EMA from observed parent→tip block time (skips if parent missing). |
| `DynamicDifficultyPolicy.clamp_timestamp(ts, prev_ts)` | Clamp to `[prev_ts+1, now+FUTURE_DRIFT_S]`. |

---

### `mempool.py`

Pending-transaction pool with priority eviction.

| Method | Does |
|---|---|
| `Mempool.__init__(max_size, key_fn)` | Priority fn (default FIFO by timestamp), backing dict, max size. |
| `add(tx)` | Add tx; if full, evict lowest-priority only if new tx beats the worst. Returns accepted bool. |
| `remove_included(tx_hashes)` | Drop transactions confirmed in a block. |
| `get_pending(max_count)` | Top-`max_count` transactions by priority (descending). |
| `contains(tx_hash)` | Membership check. |
| `__len__`, `__repr__` | Size helpers. |

---

### `miner.py`

Multi-threaded, chain-stateless miner.

| Method | Does |
|---|---|
| `Miner.__init__(mempool, difficulty_policy, on_block_mined, num_threads)` | Set up events (`interrupt`, `_stop_event`, `_resume`), tip lock + generation counter, found-flag lock. |
| `start(tip)` | Launch `num_threads` daemon worker threads (no-op if already alive). |
| `stop()` | Signal stop, wake workers, join them. |
| `mine(tip)` | Set a new tip, bump generation, clear found flag, interrupt current search, resume workers. |
| `_mine_loop(worker_id)` | Worker loop: wait for resume, read current tip+generation, mine one block. |
| `_mine_one_block(tip, worker_id, generation)` | Build header from mempool, search interleaved nonce ranges; first finder claims the block (pauses all workers), builds `Block`, calls `on_block_mined`. Aborts on stop / new generation / interrupt. |

---

### `storage.py`

Crash-safe block persistence with O(1) reads, header-only pruning and background compaction.

#### Module-level functions

| Function | Does |
|---|---|
| `_encode_header_only(block_dict)` | Re-pack a block dict with empty transactions (body pruned). |
| `_decode_block_dict(data)` | msgpack bytes → dict. |
| `_height_from_bytes(data)` | Extract height from a serialised block. |
| `_pack_record(block, flags)` | One data record: `len + flags + block + crc32`. |
| `_safe_unlink(path)` | `unlink` ignoring missing file. |

`BlockStorage` (Protocol) — `load()`, `append(block)`, `replace_all(blocks)` interface.

`InMemoryStorage` — list-backed `load` / `append` / `replace_all` (tests / no-persist).

#### `IndexedStorage`

| Method | Does |
|---|---|
| `__init__(directory, prune_after, compact_interval)` | Set up file paths, runtime index, locks, then `_recover()`. |
| `load()` | Return all blocks ascending by height, CRC-checked. |
| `append(block)` | Append one record + fsync, with rollback (`ftruncate`) on torn write; advance index only on success. |
| `replace_all(blocks)` | Atomically rewrite data+index (new epoch), fsync, rename both over live files. |
| `read_at_height(height)` | O(1) read of one block via index, or `None`. |
| `tip_height()` | Highest stored height, or `-1`. |
| `is_pruned(height)` | `True` if that height was pruned to header-only. |
| `start_compaction_worker()` / `stop_compaction_worker()` | Manage the background compaction daemon thread. |
| `compact()` | Run one synchronous compaction pass. |
| `close()` | Stop worker and flush index (idempotent). |
| `_recover()` | Rebuild runtime index: adopt `chain.index` if epoch matches, else scan data file; heal torn tail. |
| `_init_empty_data()` | Write a fresh empty data file with header. |
| `_scan_from(start)` | Scan records from offset, index them, truncate corrupt/torn tail. |
| `_compaction_loop()` | Periodic background driver calling `_compact_once`. |
| `_compact_once()` | Two-phase compaction: build pruned tmp file (lock-free), then under lock catch up new appends and atomically swap. Aborts on a raced `replace_all`. |
| `_write_all(fd, data)` *(static)* | Full write loop over a raw fd. |
| `_read_record(fh, entry)` | Seek + read + CRC-verify one block; `None` on failure. |
| `_read_record_via_path(entry)` | Same but opens the file itself; raises on failure. |
| `_write_data_file(path, epoch, items, heights)` | Write fresh data file (header + records); return `(index, size)`. |
| `_append_data_file(path, pos, items, heights)` | Append records to an existing tmp file; return `(added index, new size)`. |
| `_read_index_file()` | Parse + CRC-check `chain.index`; return `(epoch, valid_size, index)` or `None`. |
| `_flush_index(path, epoch, valid_size, index)` | Atomically write index file (tmp + rename). |
| `_fsync_dir()` | fsync the directory entry (best-effort). |

---

## `network/`

### `payloads.py`

IPv8 wire message dataclasses (each carries `format_list` + `names`). No methods.

- **Registration:** `RegisterBlockchain` (`group_id`, `community_id`), `RegisterResponse` (`success`, `message`).
- **Blockchain (server-facing):** `SubmitTransaction`, `SubmitTransactionResponse`, `GetChainHeight`, `ChainHeightResponse`, `GetBlock`, `BlockResponse`.
- **Inner (teammate-facing):** `AnnounceBlock`, `MempoolTransaction`, `RequestBlock`, `BlockResponseInner`.

Block responses carry the header fields plus `tx_hashes` as flat concatenated 32-byte hashes.

---

### `peers.py`

`TrustedPeers` — recognises server / teammate keys.

| Method | Does |
|---|---|
| `__init__()` | Load server key + teammate keys from config. |
| `is_server_b` / `is_teammate_b` / `is_trusted_b(peer_key)` | Checks against raw key bytes. |
| `is_server` / `is_teammate` / `is_trusted(peer)` | Same checks against a `Peer` object. |

---

### `registering_community.py`

`RegisteringCommunity` — registers the group's blockchain community with the grading server and re-registers periodically.

| Method | Does |
|---|---|
| `__init__(settings)` | Set up trusted peers, flags, response handler, periodic status task. |
| `_log_status()` | Periodic; if server found, trigger `submit()`. |
| `peer_added(peer)` | On server discovery, `submit()`. |
| `peer_removed(peer)` | Clear `server_peer` if server leaves. |
| `submit()` | Send `RegisterBlockchain` to the server (once). |
| `on_response(peer, payload)` | Handle server reply; if "already passed" stop, else schedule re-register after the attempt window. |
| `_re_register()` | Reset `submitted` and resubmit (unless already passed). |

---

### `blockchain_community.py`

`BlockchainCommunity` — the running blockchain node: handles transactions, mempool gossip, chain sync, mining.

| Method | Does |
|---|---|
| `__init__(settings)` | Inject chain/mempool/peers/difficulty; build `Miner`; register all message handlers; init seen-tx set and sync state. |
| `started()` | Capture event loop, start miner on current tip, register periodic `chain_sync` task. |
| `peer_added(peer)` | On teammate/sync peer join, request their height and share mempool. |
| `peer_removed(peer)` | Log server/teammate departures. |
| `on_submit_transaction(peer, payload)` | Verify signature, add to mempool, reply, broadcast to teammates. |
| `_transaction_hash_from_payload(payload)` | Compute tx hash (zeros on malformed). |
| `_transaction_from_payload(payload, source)` | Validate signature/timestamp, build `Transaction` or `None`. |
| `_add_transaction_to_mempool(tx)` | Add if new; mark seen; kick miner. Returns `(accepted, added_now)`. |
| `_transaction_has_body(tx)` *(static)* | Distinguish real mempool tx from header-sync placeholder. |
| `_broadcast_transaction(tx, exclude_peer)` | Gossip a tx to all teammates (optionally excluding sender). |
| `_share_mempool_with(peer)` | Send all pending txs to a newly seen teammate. |
| `on_mempool_transaction(peer, payload)` | Accept a gossiped tx and relay once. |
| `_is_sync_peer(peer)` | `True` for any non-server community peer. |
| `_teammate_peers()` | List of sync-capable peers. |
| `_sync_chains()` | Periodic: ask all teammates for their height. |
| `_request_chain_height(peer)` | Send `GetChainHeight`. |
| `_request_next_missing_block(peer)` | Request next height needed to catch up to a peer's target. |
| `on_chain_height_response(peer, payload)` | If peer ahead → start syncing; behind → announce our tip; equal but different tip → fetch their tip to inspect fork. |
| `on_get_chain_height(peer, payload)` | Reply with our height + tip hash; share mempool with sync peers. |
| `on_get_block(peer, payload)` | Reply with a `BlockResponse` for a requested height (server-facing). |
| `on_announce_block(peer, payload)` | If announced height higher (or same height, unknown hash) → request that block. |
| `on_request_block(peer, payload)` | Teammate block request → reply with `BlockResponseInner`. |
| `_block_from_internal_response(payload)` | Decode tx hashes, verify `txs_hash` + header hash, build and validate a (header-only) `Block`, or `None`. |
| `on_block_response_internal(peer, payload)` | Verify and `add_block`; if orphan request parent; if tip changed reconcile; keep pulling missing blocks. |
| `_reconcile_after_chain_update(result)` | On reorg: return reverted txs to mempool, remove applied txs, restart miner on new tip. |
| `_announce_block(block)` | Broadcast `AnnounceBlock` to teammates. |
| `_on_block_mined_thread_safe(block)` | Marshal a mined block from the miner thread onto the event loop. |
| `_on_block_mined(block)` | Add mined block; on reject re-mine; else reconcile, restart miner if it landed on a side branch, and announce. |

---

## `test/`

Pytest suites (each module also has local `make_*` helpers to build fixtures).

### `test_crypto.py`

Header size / roundtrip / big-endian; `hash_transaction` length; `compute_txs_hash` empty/single/multiple; leading-zero-bit counting edge cases; `satisfies_pow` true/false; `mine_block` finds a valid nonce.

### `test_chain.py`

Helper `make_valid_block`. Genesis invariants (height 0, zero `prev_hash`, empty `txs_hash`, deterministic). Block validation (PoW valid / wrong hash, `txs_hash` empty / with tx). Chain ops: extend, reject wrong height, reject wrong `prev_hash`, `get_block`. Fork handling: longer chain wins (with deterministic tie-break), shorter chain ignored, orphan adopted when parent arrives, orphan pool bounded.

### `test_mempool.py`

Helper `make_tx`. Basic add, duplicate rejection, FIFO ordering, eviction replaces worst, eviction rejects worse tx, `get_pending` ordering.

### `test_dynamic_difficulty.py`

Helpers `make_chain` / `policy_from_chain` / `run_blocks`. Classes: `TestClampTimestamp` (bounds), `TestDynamicGenesis`, `TestSteadyRate` (stable at target, no double-update per height), `TestFastAdaptation` (rises/falls within window, adapts quickly), `TestTimestampLiar` (one liar moves difficulty minimally), `TestSettlesWithoutOscillation` (monotone settle, bounded by min/max).

### `test_storage.py`

Helpers `make_block` / `dummy_tx` / `real_block` / `heights`. Classes: `TestInMemoryStorage`, `TestIndexedBasic` (append/load/reopen, `replace_all`, shorten), `TestRandomAccess` (`read_at_height`, reopen), `TestCrashSafety` (torn tail healed, bitflip detected, garbage tail ignored, stale/corrupt index rebuilt, crash between renames), `TestPruneCompact` (compaction reclaims space, pruned chain still header-verifiable), `TestConcurrency` (background compaction loses no appends).