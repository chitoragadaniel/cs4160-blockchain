from __future__ import annotations

import os
import zlib
import struct
import logging
import threading
from pathlib import Path
from typing import Protocol

import msgpack

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATA_FILENAME = "chain.blocks"
_INDEX_FILENAME = "chain.index"
_DATA_TMP = "chain.blocks.tmp"
_INDEX_TMP = "chain.index.tmp"

_DATA_MAGIC = b"BLKD"
_IDX_MAGIC = b"BIDX"

_DATA_HDR = struct.Struct(">4sI")        # magic, epoch                       (8)
_IDX_HDR = struct.Struct(">4sIQI")       # magic, epoch, data_valid_size, n   (20)
_INDEX_STRUCT = struct.Struct(">QQIB3x")  # height, offset, length, flags      (24)
INDEX_ENTRY_SIZE = _INDEX_STRUCT.size
assert INDEX_ENTRY_SIZE == 24

_LEN_STRUCT = struct.Struct(">I")        # data record length prefix
_CRC = struct.Struct(">I")               # crc32 trailer

# per-record overhead: len prefix + flags byte + crc trailer
_REC_OVERHEAD = _LEN_STRUCT.size + 1 + _CRC.size

# Flags
FLAG_FULL = 0x00
FLAG_HEADER_ONLY = 0x01

# Blocks more than this many heights behind the tip are pruned to header-only.
PRUNE_AFTER_BLOCKS: int = 100

# Background compaction interval in seconds.
COMPACT_INTERVAL_S: int = 60


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class BlockStorage(Protocol):
    def load(self) -> list[bytes]:
        """Return serialised blocks for the canonical chain, ascending height."""
        ...

    def append(self, block: bytes) -> None:
        """Durably store a newly confirmed block."""
        ...

    def replace_all(self, blocks: list[bytes]) -> None:
        """Replace the entire stored chain (fork switch)."""
        ...


# ---------------------------------------------------------------------------
# In-memory storage  (tests / no-persistence mode)
# ---------------------------------------------------------------------------

class InMemoryStorage:
    def __init__(self) -> None:
        self._blocks: list[bytes] = []

    def load(self) -> list[bytes]:
        return list(self._blocks)

    def append(self, block: bytes) -> None:
        self._blocks.append(block)

    def replace_all(self, blocks: list[bytes]) -> None:
        self._blocks = list(blocks)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _encode_header_only(block_dict: dict) -> bytes:
    """Strip transaction bodies, keep everything needed to verify the hash chain."""
    header = {
        "height":     block_dict["height"],
        "prev_hash":  block_dict["prev_hash"],
        "txs_hash":   block_dict["txs_hash"],
        "timestamp":  block_dict["timestamp"],
        "difficulty": block_dict["difficulty"],
        "nonce":      block_dict["nonce"],
        "block_hash": block_dict["block_hash"],
        "transactions": [],   # empty — body pruned
    }
    return msgpack.packb(header, use_bin_type=True)


def _decode_block_dict(data: bytes) -> dict:
    return msgpack.unpackb(data, raw=False)


def _height_from_bytes(data: bytes) -> int:
    return int(msgpack.unpackb(data, raw=False)["height"])


def _pack_record(block: bytes, flags: int) -> bytes:
    """One data-file record: len | flags | block | crc32(flags+block)."""
    flag_byte = bytes([flags])
    return (_LEN_STRUCT.pack(len(block)) + flag_byte + block
            + _CRC.pack(zlib.crc32(flag_byte + block)))


def _safe_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# IndexedStorage
# ---------------------------------------------------------------------------

class IndexedStorage(BlockStorage):
    """
    Crash-safe block storage with O(1) reads and background compaction.

    Parameters
    ----------
    directory:
        Where to create chain.blocks and chain.index.
    prune_after:
        Blocks this many heights behind the tip are pruned to header-only
        during compaction.
    compact_interval:
        Seconds between background compaction runs.
    """

    def __init__(
        self,
        directory: str | Path,
        prune_after: int = PRUNE_AFTER_BLOCKS,
        compact_interval: int = COMPACT_INTERVAL_S,
    ) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

        self._data_path = self._dir / _DATA_FILENAME
        self._index_path = self._dir / _INDEX_FILENAME
        self._data_tmp = self._dir / _DATA_TMP
        self._index_tmp = self._dir / _INDEX_TMP

        self._prune_after = prune_after
        self._compact_interval = compact_interval

        # Runtime index: height -> (block_data_offset, block_length, flags).
        self._index: dict[int, tuple[int, int, int]] = {}
        self._epoch = 0
        self._data_size = _DATA_HDR.size

        # _write_lock guards all mutable state + the file swaps. Compaction's long
        # read/write phase runs WITHOUT it so appends are not blocked.
        self._write_lock = threading.Lock()
        self._compact_lock = threading.Lock()
        self._rewrite_seq = 0   # bumped by replace_all; lets compaction detect a race

        self._compaction_thread: threading.Thread | None = None
        self._stop_compaction = threading.Event()

        self._recover()

    # ------------------------------------------------------------------
    # BlockStorage interface
    # ------------------------------------------------------------------

    def load(self) -> list[bytes]:
        """Return serialised block bytes for every height, ascending. CRC-checked."""
        with self._write_lock:
            if not self._index:
                return []
            results: list[bytes] = []
            with open(self._data_path, "rb") as fh:
                for height in sorted(self._index):
                    block = self._read_record(fh, self._index[height])
                    if block is None:
                        logger.warning("Unreadable record at height %d during load", height)
                        continue
                    results.append(block)
            return results

    def append(self, block: bytes) -> None:
        """Append one block record to the data file and update the index. One fsync.

        On a write/fsync failure the partial record is rolled back so the data file
        never keeps a half-written tail; the index is only advanced on success.
        """
        height = _height_from_bytes(block)
        rec = _pack_record(block, FLAG_FULL)
        with self._write_lock:
            start = self._data_size
            fh = os.open(self._data_path, os.O_WRONLY | os.O_APPEND)
            try:
                self._write_all(fh, rec)
                os.fsync(fh)
            except BaseException:
                try:
                    os.ftruncate(fh, start)   # undo a partial/torn write
                    os.fsync(fh)
                except OSError:
                    pass
                raise
            finally:
                os.close(fh)
            self._index[height] = (start + _LEN_STRUCT.size + 1, len(block), FLAG_FULL)
            self._data_size = start + len(rec)

    def replace_all(self, blocks: list[bytes]) -> None:
        """Atomically replace the stored chain (fork switch).

        Writes a fresh data + index pair (new epoch) to tmp files, fsyncs, then
        renames both over the live files. A crash between the renames leaves the new
        data file with a stale index whose epoch no longer matches -> the index is
        discarded and rebuilt from the data file on recovery.
        """
        with self._write_lock:
            epoch = (self._epoch + 1) & 0xFFFFFFFF
            new_index, data_size = self._write_data_file(
                self._data_tmp, epoch,
                ((b, FLAG_FULL) for b in blocks),
                heights=[_height_from_bytes(b) for b in blocks],
            )
            self._flush_index(self._index_tmp, epoch, data_size, new_index)
            self._fsync_dir()
            os.replace(self._data_tmp, self._data_path)
            os.replace(self._index_tmp, self._index_path)
            self._fsync_dir()
            self._index = new_index
            self._epoch = epoch
            self._data_size = data_size
            self._rewrite_seq += 1

    # ------------------------------------------------------------------
    # O(1) random access
    # ------------------------------------------------------------------

    def read_at_height(self, height: int) -> bytes | None:
        """Read and return the serialised block at `height` in O(1), or None."""
        with self._write_lock:
            entry = self._index.get(height)
            if entry is None:
                return None
            with open(self._data_path, "rb") as fh:
                return self._read_record(fh, entry)

    def tip_height(self) -> int:
        """Highest stored height, or -1 if empty."""
        with self._write_lock:
            return max(self._index) if self._index else -1

    def is_pruned(self, height: int) -> bool:
        """True if the block at this height has been pruned to header-only."""
        with self._write_lock:
            entry = self._index.get(height)
            return entry is not None and entry[2] == FLAG_HEADER_ONLY

    # ------------------------------------------------------------------
    # Background compaction
    # ------------------------------------------------------------------

    def start_compaction_worker(self) -> None:
        """Start a daemon thread that compacts storage periodically."""
        if self._compaction_thread and self._compaction_thread.is_alive():
            return
        self._stop_compaction.clear()
        self._compaction_thread = threading.Thread(
            target=self._compaction_loop, name="storage-compactor", daemon=True,
        )
        self._compaction_thread.start()
        logger.info("Storage compaction worker started (interval=%ds)", self._compact_interval)

    def stop_compaction_worker(self) -> None:
        self._stop_compaction.set()
        if self._compaction_thread:
            self._compaction_thread.join(timeout=self._compact_interval + 5)
            self._compaction_thread = None

    def compact(self) -> None:
        """Trigger one compaction pass immediately (synchronous)."""
        with self._compact_lock:
            self._compact_once()

    def close(self) -> None:
        """Stop the worker and persist the index. Idempotent."""
        self.stop_compaction_worker()
        with self._write_lock:
            try:
                self._flush_index(self._index_path, self._epoch, self._data_size, self._index)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover(self) -> None:
        """Rebuild the runtime index. Adopt chain.index if it matches chain.blocks,
        otherwise rebuild by scanning the data file. Heal a torn tail either way."""
        for tmp in (self._data_tmp, self._index_tmp,
                    Path(str(self._index_path) + ".w")):
            _safe_unlink(tmp)

        if (not self._data_path.exists()) or os.path.getsize(self._data_path) < _DATA_HDR.size:
            self._init_empty_data()
            return

        with open(self._data_path, "rb") as fh:
            magic, epoch = _DATA_HDR.unpack(fh.read(_DATA_HDR.size))
        if magic != _DATA_MAGIC:
            logger.warning("chain.blocks has unexpected magic, reinitialising")
            self._init_empty_data()
            return
        self._epoch = epoch

        adopted = self._read_index_file()
        data_size = os.path.getsize(self._data_path)
        if adopted is not None:
            idx_epoch, data_valid_size, index = adopted
            if idx_epoch == epoch and _DATA_HDR.size <= data_valid_size <= data_size:
                # Trust the index up to data_valid_size, then fold in any later appends.
                self._index = index
                self._data_size = data_valid_size
                if data_valid_size < data_size:
                    self._scan_from(data_valid_size)
                logger.debug("Adopted index cache (%d entries)", len(self._index))
                return

        logger.info("Rebuilding index from chain.blocks")
        self._index = {}
        self._scan_from(_DATA_HDR.size)
        self._flush_index(self._index_path, self._epoch, self._data_size, self._index)

    def _init_empty_data(self) -> None:
        with open(self._data_path, "wb") as fh:
            fh.write(_DATA_HDR.pack(_DATA_MAGIC, 0))
            fh.flush()
            os.fsync(fh.fileno())
        self._fsync_dir()
        self._epoch = 0
        self._index = {}
        self._data_size = _DATA_HDR.size

    def _scan_from(self, start: int) -> None:
        """Scan records from `start`, updating the index. Truncate a torn/corrupt tail."""
        file_size = os.path.getsize(self._data_path)
        last_good = start
        with open(self._data_path, "rb") as fh:
            fh.seek(start)
            while True:
                rec_start = fh.tell()
                head = fh.read(_LEN_STRUCT.size)
                if len(head) < _LEN_STRUCT.size:
                    break
                (length,) = _LEN_STRUCT.unpack(head)
                rest = fh.read(1 + length + _CRC.size)
                if len(rest) < 1 + length + _CRC.size:
                    break  # torn tail
                flags = rest[0]
                block = rest[1:1 + length]
                (crc,) = _CRC.unpack(rest[1 + length:])
                if zlib.crc32(bytes([flags]) + block) != crc:
                    break  # corrupt record
                try:
                    height = _height_from_bytes(block)
                except Exception:
                    break
                self._index[height] = (rec_start + _LEN_STRUCT.size + 1, length, flags)
                last_good = rec_start + _REC_OVERHEAD + length

        if last_good < file_size:
            logger.warning("Healing torn tail in chain.blocks (%d -> %d bytes)", file_size, last_good)
            os.truncate(self._data_path, last_good)
            tfd = os.open(self._data_path, os.O_WRONLY)
            try:
                os.fsync(tfd)
            finally:
                os.close(tfd)
            self._fsync_dir()
        self._data_size = last_good

    # ------------------------------------------------------------------
    # Compaction internals
    # ------------------------------------------------------------------

    def _compaction_loop(self) -> None:
        while not self._stop_compaction.wait(timeout=self._compact_interval):
            with self._compact_lock:
                try:
                    self._compact_once()
                except Exception:
                    logger.exception("Error during background compaction")

    def _compact_once(self) -> None:
        """Rewrite chain.blocks to one record per live height, pruning old bodies.

        Phase 1 (no write lock) builds the new file from a snapshot, so appends keep
        running. Phase 2 (write lock, brief) aborts on a raced replace_all, folds in
        appends made during phase 1, and atomically swaps both files.
        """
        with self._write_lock:
            if not self._index:
                return
            snapshot = dict(self._index)
            snap_rewrite = self._rewrite_seq
        tip = max(snapshot)
        epoch = (self._epoch + 1) & 0xFFFFFFFF

        # Phase 1: read snapshot records from the live file, prune old ones, write tmp.
        def records():
            with open(self._data_path, "rb") as src:
                for height in sorted(snapshot):
                    block = self._read_record(src, snapshot[height])
                    if block is None:
                        logger.warning("Skipping unreadable record at height %d in compaction", height)
                        continue
                    flags = snapshot[height][2]
                    if (tip - height) >= self._prune_after and flags == FLAG_FULL:
                        block = _encode_header_only(_decode_block_dict(block))
                        flags = FLAG_HEADER_ONLY
                    yield block, flags

        new_index, data_size = self._write_data_file(
            self._data_tmp, epoch, records(), heights=sorted(snapshot),
        )

        # Phase 2: catch up + swap (write lock, brief).
        with self._write_lock:
            if self._rewrite_seq != snap_rewrite:
                _safe_unlink(self._data_tmp)   # a reorg raced us; retry next tick
                return
            new_heights = sorted(h for h in self._index if h not in snapshot)
            if new_heights:
                appended, data_size = self._append_data_file(
                    self._data_tmp, data_size,
                    ((self._read_record_via_path(self._index[h]), self._index[h][2]) for h in new_heights),
                    heights=new_heights,
                )
                new_index.update(appended)
            self._flush_index(self._index_tmp, epoch, data_size, new_index)
            self._fsync_dir()
            os.replace(self._data_tmp, self._data_path)
            os.replace(self._index_tmp, self._index_path)
            self._fsync_dir()
            self._index = new_index
            self._epoch = epoch
            self._data_size = data_size
        pruned = sum(1 for e in new_index.values() if e[2] == FLAG_HEADER_ONLY)
        logger.info("Compaction complete: %d blocks, %d header-only", len(new_index), pruned)

    # ------------------------------------------------------------------
    # Low-level data / index io
    # ------------------------------------------------------------------

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        mv = memoryview(data)
        while mv:
            mv = mv[os.write(fd, mv):]

    def _read_record(self, fh, entry: tuple[int, int, int]) -> bytes | None:
        """Read+CRC-verify the block at index `entry` using an open file handle."""
        offset, length, flags = entry
        fh.seek(offset)
        blob = fh.read(length + _CRC.size)
        if len(blob) < length + _CRC.size:
            return None
        block = blob[:length]
        (crc,) = _CRC.unpack(blob[length:])
        if zlib.crc32(bytes([flags]) + block) != crc:
            return None
        return block

    def _read_record_via_path(self, entry: tuple[int, int, int]) -> bytes:
        with open(self._data_path, "rb") as fh:
            block = self._read_record(fh, entry)
        if block is None:
            raise OSError("record CRC/length check failed during compaction catch-up")
        return block

    def _write_data_file(self, path: Path, epoch: int, items, heights: list[int]):
        """Write a fresh data file (header + records). Returns (index, data_size)."""
        index: dict[int, tuple[int, int, int]] = {}
        with open(path, "wb") as fh:
            fh.write(_DATA_HDR.pack(_DATA_MAGIC, epoch))
            pos = _DATA_HDR.size
            for height, (block, flags) in zip(heights, items):
                rec = _pack_record(block, flags)
                fh.write(rec)
                index[height] = (pos + _LEN_STRUCT.size + 1, len(block), flags)
                pos += len(rec)
            fh.flush()
            os.fsync(fh.fileno())
        return index, pos

    def _append_data_file(self, path: Path, pos: int, items, heights: list[int]):
        """Append records to an existing tmp data file. Returns (added_index, new_size)."""
        added: dict[int, tuple[int, int, int]] = {}
        with open(path, "ab") as fh:
            for height, (block, flags) in zip(heights, items):
                rec = _pack_record(block, flags)
                fh.write(rec)
                added[height] = (pos + _LEN_STRUCT.size + 1, len(block), flags)
                pos += len(rec)
            fh.flush()
            os.fsync(fh.fileno())
        return added, pos

    def _read_index_file(self):
        """Return (epoch, data_valid_size, index) if chain.index is intact, else None."""
        if not self._index_path.exists():
            return None
        raw = self._index_path.read_bytes()
        if len(raw) < _IDX_HDR.size + _CRC.size:
            return None
        magic, epoch, data_valid_size, count = _IDX_HDR.unpack(raw[:_IDX_HDR.size])
        if magic != _IDX_MAGIC:
            return None
        body = raw[_IDX_HDR.size:len(raw) - _CRC.size]
        (crc,) = _CRC.unpack(raw[len(raw) - _CRC.size:])
        if len(body) != count * INDEX_ENTRY_SIZE:
            return None
        if zlib.crc32(raw[:_IDX_HDR.size] + body) != crc:
            return None
        index: dict[int, tuple[int, int, int]] = {}
        for i in range(count):
            height, offset, length, flags = _INDEX_STRUCT.unpack(
                body[i * INDEX_ENTRY_SIZE:(i + 1) * INDEX_ENTRY_SIZE])
            index[height] = (offset, length, flags)
        return epoch, data_valid_size, index

    def _flush_index(self, path: Path, epoch: int, data_valid_size: int,
                     index: dict[int, tuple[int, int, int]]) -> None:
        hdr = _IDX_HDR.pack(_IDX_MAGIC, epoch, data_valid_size, len(index))
        body = b"".join(
            _INDEX_STRUCT.pack(h, o, l, f) for h, (o, l, f) in sorted(index.items())
        )
        blob = hdr + body + _CRC.pack(zlib.crc32(hdr + body))
        tmpw = Path(str(path) + ".w")
        with open(tmpw, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmpw, path)

    def _fsync_dir(self) -> None:
        try:
            dfd = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, NotImplementedError):
            pass
