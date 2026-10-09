import logging

from typing import Callable

from .chain import Transaction


logger = logging.getLogger(__name__)

class Mempool:
    """Ordered collection of pending transactions, with a max size.
    When we go over the max size, we evict the lowest priority transactions.
    We can provide a key fn, which must return a priority score (higher is better).
    Options are for example:
        - FIFO (default) (based on tx.timestamp)
        - transaction fee
        - transaction fee / byte
    """

    def __init__(self, max_size: int = 1000, key_fn: Callable[[Transaction], int] | None = None) -> None:
        self.key_fn = key_fn or (lambda tx: tx.timestamp)
        self._pool: dict[bytes, Transaction] = {}
        self.max_size = max_size

    def add(self, tx: Transaction) -> bool:
        """Add a transaction to the pool, if needed evict lowest priority transaction. 
        Returns false if transaction already in pool or if the pool is full and the transaction 
        does not have a high enough priority, true otherwise
        """
        if tx.tx_hash in self._pool:
            logger.debug(f"Mempool: transaction already in pool tx {tx}, ignoring.")
            return False

        if len(self._pool) >= self.max_size:
            worst_tx: Transaction = min(self._pool.values(), key=self.key_fn)
            if self.key_fn(tx) <= self.key_fn(worst_tx):
                logger.debug("Mempool full, and tx not better than worst.")
                return False

            del self._pool[worst_tx.tx_hash]  # Remove lowest priority transaction from pool
            logger.warning(f"Mempool full, dropping worst tx: {worst_tx}")

        self._pool[tx.tx_hash] = tx
        logger.info(f"Mempool: accepted {tx} (pool size={len(self._pool)})")
        return True

    def remove_included(self, tx_hashes: list[bytes]) -> None:
        """Remove transactions that have been included in a confirmed block."""
        for h in tx_hashes:
            self._pool.pop(h, None)


    def get_pending(self, max_count: int = 100) -> list[Transaction]:
        """Return the 'max_count' pending transactions with the heighest priority, or all of them if len <= max_count"""
        return sorted(self._pool.values(), key=self.key_fn, reverse=True)[:max_count]

    def contains(self, tx_hash: bytes) -> bool:
        return tx_hash in self._pool

    def __len__(self) -> int:
        return len(self._pool)

    def __repr__(self) -> str:
        return f"<Mempool size={len(self._pool)}>"