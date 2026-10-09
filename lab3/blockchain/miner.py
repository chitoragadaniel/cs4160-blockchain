import time
import logging
import threading

from typing import Callable

from . import crypto
from .chain import Block
from .difficulty import DifficultyPolicy
from .mempool import Mempool

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class Miner:
    """Mining class that is stateless with respect to the chain.
    It is thread safe, so we can run it as co-routine.
    When it mines a block, the callback is used.

    Coordination model:
      - All worker threads mine on the same tip (interleaved nonce ranges).
      - The first thread to find a valid block claims it (`_found`), emits it once, and
        clears `_resume` so every worker pauses.
      - Workers stay paused until `mine()` supplies the next tip (bumping the generation and
        setting `_resume`). `mine()` is always called after a block lands (self-mined or synced
        from a peer), so the pause can never deadlock.
    This stops the threads from busy-spinning and re-emitting duplicate blocks during the brief
    gap between finding a block and the chain adopting it.
    """

    def __init__(self, mempool: Mempool, difficulty_policy: DifficultyPolicy, on_block_mined: Callable[[Block], None], num_threads = 4) -> None:
        self._mempool = mempool
        self._difficulty_policy = difficulty_policy
        self._on_block_mined = on_block_mined

        self.interrupt = threading.Event()   # break an in-progress search when the tip changes
        self._stop_event = threading.Event()
        self._resume = threading.Event()      # workers mine while set, pause while cleared
        self._resume.set()

        self._tip: Block | None = None
        self._tip_lock = threading.Lock()
        self._tip_generation = 0

        self._num_threads = num_threads
        self._workers = []
        self._found_lock = threading.Lock()
        self._found = False

    def start(self, tip = None) -> None:
        """Start the mining process, or continue if already started"""
        logger.debug("Miner start called")
        if any(thread.is_alive() for thread in self._workers):
            return

        self._stop_event.clear()
        self.interrupt.clear()
        self._resume.set()
        self._found = False
        self._workers = []
        if tip is not None:
            self._tip = tip
        self._tip_generation = 0

        for i in range(self._num_threads):
            thread = threading.Thread(target=self._mine_loop, args=(i,), name=f"miner-{i}", daemon=True)
            thread.start()
            self._workers.append(thread)

        logger.info(f"{self._num_threads} Miner(s) started")

    def stop(self) -> None:
        """Completely stop the mining"""
        self._stop_event.set()
        self.interrupt.set()
        self._resume.set()  # wake any paused workers so they can see the stop flag and exit

        for thread in self._workers:
            thread.join(timeout=5)

        self._workers.clear()
        logger.info("Miner stopped")

    def mine(self, tip: Block) -> None:
        """Mine from the provided tip. Interrupts any in-progress search and resumes paused workers."""
        assert tip is not None, "passed None to mine function"
        with self._tip_lock:
            self._tip = tip
            self._tip_generation += 1
        with self._found_lock:
            self._found = False
        self.interrupt.set()
        self._resume.set()
        logger.info(f"Starting to mine at tip: {tip}")

    def _mine_loop(self, worker_id) -> None:
        """Keep mining the current tip until stopped. Pauses between blocks until a new tip is set."""
        while not self._stop_event.is_set():
            # Block until there is work to do (a tip set, and not paused after a found block).
            self._resume.wait()
            if self._stop_event.is_set():
                return
            with self._tip_lock:
                tip = self._tip
                generation = self._tip_generation
            if tip is None:
                # No tip yet; avoid a hot spin until mine()/start() provides one.
                self._resume.clear()
                continue
            self.interrupt.clear()
            self._mine_one_block(tip, worker_id, generation)

    def _mine_one_block(self, tip: Block, worker_id: int, generation: int) -> None:
        """Attempt to mine a block"""
        difficulty = self._difficulty_policy.get_difficulty(tip)
        pending = self._mempool.get_pending()

        # Always mine, even with an empty mempool: the server needs the test transaction buried
        # under at least 3 blocks, so the chain must keep growing after the tx is included.

        # Get all things needed to mine a new block with the pending transactions in the pool
        tx_hashes = [tx.tx_hash for tx in pending]
        txs_hash = crypto.compute_txs_hash(tx_hashes)
        timestamp = int(time.time())
        prev_hash = tip.block_hash
        logger.info(f"Thread {worker_id} Mining block height={tip.height} difficulty={difficulty} nr_txs={len(pending)}")

        # We interleave the search space for multi threaded mining.
        nonce = worker_id
        check_interval = 10_000  # interval to check if the thread should be interrupted
        while not self._stop_event.is_set():
            found_nonce, block_hash = crypto.mine_block(prev_hash, txs_hash, timestamp, difficulty, nonce, check_interval, step=self._num_threads)

            if found_nonce is not None and block_hash is not None:
                with self._found_lock:
                    if self._found:
                        return  # another worker already claimed a block for this tip
                    self._found = True
                # Pause all workers until the chain adopts this block and mine() sets the next tip.
                self._resume.clear()
                self.interrupt.set()

                block = Block(
                    height=tip.height + 1,
                    prev_hash=prev_hash,
                    txs_hash=txs_hash,
                    timestamp=timestamp,
                    difficulty=difficulty,
                    nonce=found_nonce,
                    block_hash=block_hash,
                    transactions=tuple(pending),
                )

                logger.info(f"Thread {worker_id} Mined a block! on tip: {tip}, minded block: {block}")
                self._on_block_mined(block)
                return

            nonce += check_interval * self._num_threads
            with self._tip_lock:
                if generation != self._tip_generation:
                    logger.debug("Mining interrupted by newer tip")
                    return
            if self.interrupt.is_set():
                logger.debug("Mining interrupted")
                return
