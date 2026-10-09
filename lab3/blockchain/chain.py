import msgpack
import logging
from dataclasses import dataclass, field
from functools import lru_cache

from .storage import BlockStorage, InMemoryStorage
from ..config import DEFAULT_DIFFICULTY
from .crypto import compute_txs_hash, hash_header, mine_block, satisfies_pow, sha256

logger = logging.getLogger(__name__)

# --------------------------------------
# Transaction
# --------------------------------------
@dataclass(frozen=True)
class Transaction:
    sender_key: bytes
    data: bytes
    timestamp: int
    signature: bytes
    tx_hash: bytes

    def to_dict(self) -> dict:
        """Serialise a Transaction to a plain dict of primitive types."""
        return {
            "sender_key": self.sender_key,
            "data":       self.data,
            "timestamp":  self.timestamp,
            "signature":  self.signature,
            "tx_hash":    self.tx_hash,
        }

    def __repr__(self) -> str:
        return f"<Tx {self.tx_hash.hex()[:12]}...>"


# --------------------------------------
# Block
# --------------------------------------
@dataclass(frozen=True)
class Block:
    block_hash: bytes   # 32 bytes (sha-256 of header)

    # header (84 bytes)
    prev_hash: bytes    # 32 bytes
    txs_hash: bytes     # 32 bytes
    timestamp: int      # unix seconds (8 bytes)
    difficulty: int     # 4 bytes
    nonce: int          # 8 bytes

    # content
    height: int
    transactions: tuple[Transaction, ...]

    def to_dict(self) -> dict:
        """Serialise a Block to a plain dict of primitive types."""
        return {
            "height":       self.height,
            "prev_hash":    self.prev_hash,
            "txs_hash":     self.txs_hash,
            "timestamp":    self.timestamp,
            "difficulty":   self.difficulty,
            "nonce":        self.nonce,
            "block_hash":   self.block_hash,
            "transactions": [tx.to_dict() for tx in self.transactions],
        }

    
    @staticmethod
    def _dict_to_block(d: dict) -> "Block":
        """Deserialise a plain dict back to a Block."""
        txs = tuple(
            Transaction(
                sender_key=t["sender_key"],
                data=t["data"],
                timestamp=t["timestamp"],
                signature=t["signature"],
                tx_hash=t["tx_hash"],
            )
            for t in d.get("transactions", [])
        )
        return Block(
            height=d["height"],
            prev_hash=d["prev_hash"],
            txs_hash=d["txs_hash"],
            timestamp=d["timestamp"],
            difficulty=d["difficulty"],
            nonce=d["nonce"],
            block_hash=d["block_hash"],
            transactions=txs,
        )

    @staticmethod
    def _decode(data: bytes) -> "Block":
        d =  msgpack.unpackb(data, raw=False)
        return Block._dict_to_block(d)

    def _encode(self) -> bytes:
        d = self.to_dict()
        return msgpack.packb(d, use_bin_type=True) # type: ignore

    def verify_header(self) -> bool:
        """Checks the header is internally consistent: block_hash is the hash of the header,
        and that hash satisfies the PoW difficulty. Does NOT need the transaction bodies."""
        expected = hash_header(self.prev_hash, self.txs_hash, self.timestamp, self.difficulty, self.nonce)
        return expected == self.block_hash and satisfies_pow(self.block_hash, self.difficulty)

    def verify_pow(self) -> bool:
        """Alias kept for clarity / tests. Same as verify_header."""
        return self.verify_header()

    def verify_txs_hash(self) -> bool:
        """Checks if the txs_hash is the correct hash of all corresponding transaction hashes"""
        return compute_txs_hash([tx.tx_hash for tx in self.transactions]) == self.txs_hash

    @property
    def has_body(self) -> bool:
        """True if we actually hold the transaction bodies for this block (or it is genuinely empty).
        Header-only blocks received during sync have no bodies but a non-empty txs_hash."""
        return bool(self.transactions) or self.txs_hash == compute_txs_hash([])

    def is_valid(self) -> bool:
        """Check if this block is valid for insertion. It is valid when:
            - the header is consistent (block_hash hashes the header, satisfies PoW)
            - if we have the transaction bodies, they hash to txs_hash
        A header-only block (synced from a peer, bodies not yet fetched) is accepted on the
        header alone; the body is verified separately once the transactions are available.
        """
        if not self.verify_header():
            return False
        if self.has_body:
            return self.verify_txs_hash()
        return True

    @property
    def tx_hashes(self) -> list[bytes]:
        """For convenience, make the transaction hashes easy retrievable"""
        return [tx.tx_hash for tx in self.transactions]

    def __repr__(self) -> str:
        return (
            f"<Block h={self.height} hash={self.block_hash.hex()[:8]}... "
            f"nr_txs={len(self.transactions)} difficulty={self.difficulty}>"
        )


@lru_cache(maxsize=None)
def make_genesis_block(difficulty: int = DEFAULT_DIFFICULTY, prev_hash = b"\x00" * 32, height=0) -> Block:
    """The chain needs a genesis block. It needs to be identical on all nodes, so we hardcode it to make it easy.

    genesis block:
      prev_hash  = 32 zero bytes  (doesnt have a parent)
      txs_hash   = SHA256(b"")    (doesnt have any transactions)
      timestamp  = 0              (doesnt have normal timestamp)
      difficulty = DIFFICULTY
      nonce      = first nonce (from 0) whose hash satisfies the PoW
      height     = 0              (it is the first block of the chain)
    """
    txs_hash = sha256(b"")
    nonce, block_hash = mine_block(prev_hash, txs_hash, 0, difficulty)
    return Block(
        height=height,
        prev_hash=prev_hash,
        txs_hash=txs_hash,
        timestamp=0,
        difficulty=difficulty,
        nonce=nonce,
        block_hash=block_hash,
        transactions=(),
    )


# --------------------------------------
# Add result
# --------------------------------------
@dataclass
class AddResult:
    """Outcome of Blockchain.add_block, so the caller can reconcile its mempool / mining.

    added         : the block was new, valid, and connected to the tree
    extended_tip  : the best (main) chain tip changed as a result (a plain extend or a reorg)
    is_orphan     : the block was valid but its parent is unknown; it is parked until the parent arrives
    missing_parent: the parent hash we are waiting for (only set when is_orphan)
    reverted      : blocks that left the main chain in a reorg (their txs should go back to the mempool)
    applied       : blocks that joined the main chain (their txs should be removed from the mempool)
    """
    added: bool = False
    extended_tip: bool = False
    is_orphan: bool = False
    missing_parent: bytes | None = None
    reverted: list[Block] = field(default_factory=list)
    applied: list[Block] = field(default_factory=list)

    def __bool__(self) -> bool:
        # Keeps `if chain.add_block(b):` / `assert chain.add_block(b)` reading as "was it added".
        return self.added


def _chain_score(block: "Block") -> tuple[int, bytes]:
    """Deterministic best-chain ordering key: taller wins, and on a height tie the smaller
    block hash wins. The hash is inverted so plain `max()`/`>` prefer the smaller hash. Because
    every node applies the same rule, equal-height forks converge on the same tip."""
    inverted_hash = bytes(255 - b for b in block.block_hash)
    return (block.height, inverted_hash)


# --------------------------------------
# Blockchain
# --------------------------------------
class Blockchain:
    """A block tree with longest-chain selection.

    All connected blocks (reachable from genesis) live in `_blocks`, keyed by block_hash and
    linked to their parent through `prev_hash`. The current best chain is materialised in `_main`
    (height -> block) so height lookups stay O(1). When a newly connected block gives a branch
    that is strictly taller than the current tip we reorg the main chain onto it.

    Blocks whose parent we do not have yet are parked in `_orphans`, keyed by the parent hash they
    are waiting for. When that parent finally connects, the waiting children are adopted (recursively).
    The orphan pool is bounded to avoid a memory-exhaustion DoS from bogus high-height blocks.
    """

    def __init__(self, genesisses: list[Block] | Block, max_orphans: int = 1000, storage: BlockStorage | None = None) -> None:
        # Accept either a single genesis block or a pre-seeded list of consecutive start blocks.
        if isinstance(genesisses, Block):
            genesisses = [genesisses]
        # parent_hash we are missing -> {block_hash -> block}
        self._orphans: dict[bytes, dict[bytes, Block]] = {}
        self._orphan_hashes: set[bytes] = set()
        self._max_orphans = max_orphans

        self._storage = storage if storage is not None else InMemoryStorage()
        loaded_data = self._storage.load()
        if loaded_data and len(loaded_data) > 0:
            decoded_loaded_data = [Block._decode(b) for b in loaded_data]
            self._chain = {b.height: b for b in decoded_loaded_data}
            self._blocks: dict[bytes, Block] = {b.block_hash: b for b in decoded_loaded_data} 
            self._genesis = self._chain[0]
        else:
            self._chain = {i: b for i, b in enumerate(genesisses)}
            self._blocks: dict[bytes, Block] = {b.block_hash: b for b in genesisses}
            self._genesis = genesisses[0]
            logger.info(f"Started fresh blockchain, we have {len(self._chain)} start nodes, keys are: {list(self._chain.keys())}. Current height: {self.height}")
            for b in genesisses:
                self._storage.append(b._encode())

    @property
    def height(self) -> int:
        """Get the height of the current chain"""
        return max(self._chain.keys())

    @property
    def tip(self) -> Block:
        """Get the current tip of the chain"""
        return self._chain[self.height]

    def get_block(self, height: int) -> Block | None:
        """Get the block on the specified height, None if it doesnt exist"""
        return self._chain.get(height, None)

    def get_block_by_hash(self, block_hash: bytes) -> Block | None:
        """Get any known (connected) block by its hash, None if unknown"""
        return self._blocks.get(block_hash, None)

    def contains(self, block_hash: bytes) -> bool:
        """Checks if the provided block hash is present in the current chain"""
        block = self.get_block_by_hash(block_hash)
        return block is not None and self._chain.get(block.height) is block

    def knows(self, block_hash: bytes) -> bool:
        """Checks if we have seen this block at all, either connected or parked as an orphan"""
        return block_hash in self._blocks or block_hash in self._orphan_hashes

    def add_block(self, block: Block) -> AddResult:
        """Add a block to the tree, reorging the main chain if the block yields a taller branch.

        Returns an AddResult describing what happened (added / reorg / orphan), so the caller can
        keep its mempool and miner in sync.
        """
        if not block.is_valid():
            logger.warning("Rejected invalid block %s", block)
            return AddResult()

        if self.knows(block.block_hash):
            # Already have it, but a sibling branch we hold may now out-score our tip
            # Re-run selection so nodes converge deterministically regardless of the order blocks arrived in.
            logger.debug("Block already known, re-evaluating best tip %s", block)
            return self._select_best_tip()

        parent = self._blocks.get(block.prev_hash)
        if parent is None:
            # We can't connect it yet, park it until its parent shows up.
            self._add_orphan(block)
            return AddResult(is_orphan=True, missing_parent=block.prev_hash)

        if block.height != parent.height + 1:
            logger.debug("Block height mismatch: parent at %d, block claims %d", parent.height, block.height)
            return AddResult()

        # Store it and pull in any orphans that were waiting on it (recursively).
        self._store_and_cascade(block)

        result = self._select_best_tip()
        result.added = True
        return result

    def _select_best_tip(self) -> AddResult:
        """Pick the best tip among all connected blocks and reorg
        the main chain onto it if it out-scores the current tip. """
        result = AddResult()
        best = max(self._blocks.values(), key=_chain_score)
        if _chain_score(best) > _chain_score(self.tip):
            result.extended_tip = True
            if best.prev_hash == self.tip.block_hash and best.height == self.height + 1:
                # Fast path: best directly extends the current tip -> append, no full rewrite.
                self._chain[best.height] = best
                result.applied = [best]
                self._storage.append(best._encode())
            else:
                # Genuine reorg / tie-break switch: rebuild the main path and rewrite the WAL.
                result.reverted, result.applied = self._reorg(best)
        return result

    # --------------------------------------
    # Internal helpers
    # --------------------------------------
    def _add_orphan(self, block: Block) -> None:
        """Park a valid-but-unconnected block, bounded so peers can't blow up our memory."""
        if len(self._orphan_hashes) >= self._max_orphans:
            logger.warning("Orphan pool full (%d), dropping %s", self._max_orphans, block)
            return
        self._orphans.setdefault(block.prev_hash, {})[block.block_hash] = block
        self._orphan_hashes.add(block.block_hash)
        logger.debug("Parked orphan %s waiting on parent %s", block, block.prev_hash.hex()[:8])

    def _store_and_cascade(self, block: Block) -> list[Block]:
        """Store `block`, then adopt any orphans waiting on it, and their children, etc."""
        newly: list[Block] = []
        stack = [block]
        while stack:
            current = stack.pop()
            self._blocks[current.block_hash] = current
            newly.append(current)

            waiting = self._orphans.pop(current.block_hash, {})
            for child in waiting.values():
                self._orphan_hashes.discard(child.block_hash)
                if child.height == current.height + 1:
                    stack.append(child)
                else:
                    logger.debug("Dropping orphan %s, height inconsistent with parent %s", child, current)
        return newly

    def _reorg(self, new_tip: Block) -> tuple[list[Block], list[Block]]:
        """Rebuild the main chain to run from genesis up to `new_tip`.
        Returns (reverted, applied): blocks that left and joined the main chain.
        """
        # Walk back from the new tip to genesis to get the new main path.
        # Every stored block must connect to genesis (invariant of _store_and_cascade), so
        # we index _blocks directly: a missing parent is a broken invariant -> fail loud (KeyError).
        path: list[Block] = []
        cursor = new_tip
        while cursor.block_hash != self._genesis.block_hash:
            path.append(cursor)
            cursor = self._blocks[cursor.prev_hash]
        path.append(cursor)  # genesis
        path.reverse()
        new_chain = {b.height: b for b in path}

        old_chain = self._chain
        reverted = [old_chain[h] for h in sorted(old_chain)
                    if h not in new_chain or new_chain[h].block_hash != old_chain[h].block_hash]
        applied = [new_chain[h] for h in sorted(new_chain)
                   if h not in old_chain or old_chain[h].block_hash != new_chain[h].block_hash]

        self._chain = new_chain
        self._storage.replace_all([b._encode() for b in self._chain.values()])
        logger.info("Main chain now height %d tip %s (reverted %d, applied %d)",
                    new_tip.height, new_tip, len(reverted), len(applied))
        return reverted, applied

    def __repr__(self) -> str:
        return f"<Blockchain height={self.height} tip={self.tip} known={len(self._blocks)} orphans={len(self._orphan_hashes)}>"
