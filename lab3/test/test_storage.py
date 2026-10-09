import os
import hashlib
import tempfile
from pathlib import Path

from lab3.blockchain.storage import (
    InMemoryStorage,
    IndexedStorage,
    _DATA_FILENAME,
    _INDEX_FILENAME,
    _DATA_HDR,
    _DATA_MAGIC,
    _pack_record,
    FLAG_FULL,
    FLAG_HEADER_ONLY,
)
from lab3.blockchain.chain import Block, Transaction
from lab3.blockchain import crypto


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def make_block(height: int, prev_hash: bytes = b"0") -> Block:
    return Block(
        height=height,
        prev_hash=prev_hash,
        txs_hash=b"x" * 32,
        timestamp=1_000_000 + height,
        difficulty=10,
        nonce=height,
        block_hash=bytes([height % 256]) * 32,
        transactions=(),
    )


def dummy_tx(seed: int) -> Transaction:
    return Transaction(sender_key=b"k", data=b"d", timestamp=seed, signature=b"s",
                       tx_hash=hashlib.sha256(f"tx{seed}".encode()).digest())


def real_block(height: int, prev_hash: bytes, txs: tuple[Transaction, ...], difficulty: int = 8) -> Block:
    """A block with a valid PoW header so verify_header() passes (low difficulty = fast)."""
    txs_hash = crypto.compute_txs_hash([t.tx_hash for t in txs])
    ts = 1_000 + height
    nonce, block_hash = crypto.mine_block(prev_hash, txs_hash, ts, difficulty)
    return Block(block_hash=block_hash, prev_hash=prev_hash, txs_hash=txs_hash,
                 timestamp=ts, difficulty=difficulty, nonce=nonce, height=height, transactions=txs)


def heights(blocks_bytes):
    return [Block._decode(b).height for b in blocks_bytes]


# --------------------------------------------------------------------------
# InMemoryStorage
# --------------------------------------------------------------------------
class TestInMemoryStorage:
    def test_append_and_load(self):
        s = InMemoryStorage()
        s.append(make_block(0)._encode())
        s.append(make_block(1)._encode())
        assert [Block._decode(b) for b in s.load()] == [make_block(0), make_block(1)]

    def test_replace_all_overwrites(self):
        s = InMemoryStorage()
        s.append(make_block(0)._encode())
        s.replace_all([make_block(1)._encode()])
        assert [Block._decode(b) for b in s.load()] == [make_block(1)]


# --------------------------------------------------------------------------
# IndexedStorage — protocol parity
# --------------------------------------------------------------------------
class TestIndexedBasic:
    def test_append_and_load_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            s.append(make_block(0)._encode())
            s.append(make_block(1)._encode())
            assert heights(s.load()) == [0, 1]
            # reopen
            assert heights(IndexedStorage(tmp).load()) == [0, 1]

    def test_empty_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert IndexedStorage(tmp).load() == []

    def test_replace_all_overwrites_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for b in (make_block(0), make_block(1)):
                s.append(b._encode())
            s.replace_all([make_block(0)._encode(), make_block(1)._encode(), make_block(2)._encode()])
            assert heights(IndexedStorage(tmp).load()) == [0, 1, 2]

    def test_replace_all_can_shorten(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(5):
                s.append(make_block(i)._encode())
            s.replace_all([make_block(0)._encode(), make_block(1)._encode(),
                           make_block(2, prev_hash=b"fork")._encode()])
            reopened = IndexedStorage(tmp)
            assert heights(reopened.load()) == [0, 1, 2]
            assert reopened.read_at_height(3) is None
            assert Block._decode(reopened.read_at_height(2)).prev_hash == b"fork"


# --------------------------------------------------------------------------
# O(1) random access
# --------------------------------------------------------------------------
class TestRandomAccess:
    def test_read_at_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(10):
                s.append(make_block(i)._encode())
            for h in (0, 4, 9):
                assert Block._decode(s.read_at_height(h)).height == h
            assert s.tip_height() == 9
            assert s.read_at_height(10) is None
            assert s.read_at_height(-1) is None

    def test_read_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(4):
                s.append(make_block(i)._encode())
            assert Block._decode(IndexedStorage(tmp).read_at_height(2)).height == 2


# --------------------------------------------------------------------------
# crash safety
# --------------------------------------------------------------------------
class TestCrashSafety:
    def _append_raw(self, tmp, data: bytes):
        with open(Path(tmp) / _DATA_FILENAME, "ab") as f:
            f.write(data)

    def test_truncated_tail_is_healed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(2):
                s.append(make_block(i)._encode())
            # a record with its tail cut off (torn write)
            rec = _pack_record(make_block(2)._encode(), FLAG_FULL)
            self._append_raw(tmp, rec[:-4])
            size_before = os.path.getsize(Path(tmp) / _DATA_FILENAME)

            reopened = IndexedStorage(tmp)
            assert heights(reopened.load()) == [0, 1]
            assert os.path.getsize(Path(tmp) / _DATA_FILENAME) < size_before
            reopened.append(make_block(2)._encode())
            assert heights(IndexedStorage(tmp).load()) == [0, 1, 2]

    def test_bitflip_in_record_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            s.append(make_block(0)._encode())
            s.append(make_block(1)._encode())
            # flip a byte in the last record's body -> CRC fails -> tail discarded
            path = Path(tmp) / _DATA_FILENAME
            data = bytearray(path.read_bytes())
            data[-6] ^= 0xFF
            path.write_bytes(bytes(data))
            assert heights(IndexedStorage(tmp).load()) == [0]

    def test_garbage_tail_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            s.append(make_block(0)._encode())
            self._append_raw(tmp, b"\x00\x00\x00\xff" + b"broken")
            assert heights(IndexedStorage(tmp).load()) == [0]

    def test_stale_index_after_appends(self):
        # index flushed at close, then more appends with no index update (crash-like)
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(3):
                s.append(make_block(i)._encode())
            s.close()  # flushes index at current size
            s2 = IndexedStorage(tmp)
            s2.append(make_block(3)._encode())  # index file now stale (no flush)
            assert heights(IndexedStorage(tmp).load()) == [0, 1, 2, 3]

    def test_corrupt_index_rebuilt_from_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(3):
                s.append(make_block(i)._encode())
            s.close()
            idx = Path(tmp) / _INDEX_FILENAME
            idx.write_bytes(b"\x00" * 8)  # garbage / too short
            assert heights(IndexedStorage(tmp).load()) == [0, 1, 2]

    def test_replace_all_crash_between_renames(self):
        # simulate: new data file swapped in, index NOT swapped (stale epoch) -> rebuild
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(4):
                s.append(make_block(i)._encode())
            s.close()
            stale_index = (Path(tmp) / _INDEX_FILENAME).read_bytes()
            # a reorg writes a new data file (new epoch)
            s2 = IndexedStorage(tmp)
            s2.replace_all([make_block(0)._encode(), make_block(1, prev_hash=b"z")._encode()])
            s2.close()
            # roll the index back to the pre-reorg (stale) one, as a crash between renames would
            (Path(tmp) / _INDEX_FILENAME).write_bytes(stale_index)
            reopened = IndexedStorage(tmp)
            # stale index epoch != data epoch -> rebuilt from data -> the reorg result
            assert heights(reopened.load()) == [0, 1]
            assert Block._decode(reopened.read_at_height(1)).prev_hash == b"z"


# --------------------------------------------------------------------------
# pruning + compaction
# --------------------------------------------------------------------------
class TestPruneCompact:
    def _real_chain(self, s, n):
        prev = b"\x00" * 32
        for h in range(n):
            b = real_block(h, prev, (dummy_tx(h), dummy_tx(h + 1000)))
            s.append(b._encode())
            prev = b.block_hash

    def test_compaction_reclaims_and_keeps_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            for i in range(6):
                s.append(make_block(i)._encode())
            # rewrite a few heights via replace_all to create dead bytes
            s.replace_all([make_block(i)._encode() for i in range(6)])
            s.compact()
            assert heights(IndexedStorage(tmp).load()) == list(range(6))

    def test_prune_keeps_chain_verifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp, prune_after=2)
            self._real_chain(s, 6)  # tip = 5
            size_before = os.path.getsize(Path(tmp) / _DATA_FILENAME)
            s.compact()  # prune heights <= 5-2 = 3
            size_after = os.path.getsize(Path(tmp) / _DATA_FILENAME)
            assert size_after < size_before

            reopened = IndexedStorage(tmp)
            for h in range(4):
                assert reopened.is_pruned(h)
                b = Block._decode(reopened.read_at_height(h))
                assert b.verify_header() is True
                assert b.has_body is False
            for h in (4, 5):
                assert not reopened.is_pruned(h)
                assert Block._decode(reopened.read_at_height(h)).has_body is True
            assert heights(reopened.load()) == list(range(6))


# --------------------------------------------------------------------------
# concurrency: background compaction does not block / lose appends
# --------------------------------------------------------------------------
class TestConcurrency:
    def test_compaction_concurrent_with_appends_loses_nothing(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            s = IndexedStorage(tmp)
            stop = threading.Event()

            def compactor():
                while not stop.is_set():
                    s.compact()

            t = threading.Thread(target=compactor, daemon=True)
            t.start()
            n = 500
            try:
                for i in range(n):
                    s.append(make_block(i)._encode())
            finally:
                stop.set()
                t.join()
            s.compact()
            # every append survived and the chain is contiguous despite the swaps
            assert heights(IndexedStorage(tmp).load()) == list(range(n))
