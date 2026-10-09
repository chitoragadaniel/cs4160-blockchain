# `test/` — unit tests

Pytest unit tests for the [`../blockchain/`](../blockchain/) core. No networking is
exercised here — the IPv8 communities are integration-tested by running real nodes.

## Run

From the repository root:

```bash
uv run pytest assignment_3/test -v          # all tests
uv run pytest assignment_3/test/test_chain.py -v   # one file
```

`pyproject.toml` sets `pythonpath = ["assignment_3"]` so imports resolve.

## Files

| File | Covers | Notable cases |
|---|---|---|
| `test_crypto.py` | header pack/unpack, hashing, PoW | 84-byte header size, big-endian round-trip, `txs_hash` of empty/single/multiple txs, leading-zero counting, `mine_block` finds a valid nonce |
| `test_chain.py` | `Block` / `Blockchain` | deterministic genesis, PoW + body verification, extend chain, reject wrong height / prev_hash, **fork switch** (longer wins, shorter ignored), **orphan** adoption when parent arrives, orphan-pool bound |
| `test_dynamic_difficulty.py` | `DynamicDifficultyPolicy` | timestamp clamping, EMA stability at target, rise/fall on fast/slow blocks, resistance to a single timestamp liar, no oscillation after a hashpower jump, min/max bounds |
| `test_mempool.py` | `Mempool` | add, duplicate rejection, FIFO ordering, eviction replaces worst / rejects worse, pending ordering |
| `test_storage.py` | `InMemoryStorage` / `IndexedStorage` | append+load+reopen, `replace_all` (incl. shortening), **O(1) `read_at_height`**, **torn tail healed**, **bit-flip detected** (CRC), stale/corrupt index rebuilt, `replace_all` crash between renames, prune keeps chain verifiable, **compaction concurrent with appends loses nothing** |
