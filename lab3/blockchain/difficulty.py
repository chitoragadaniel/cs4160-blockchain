import time

from typing import Callable

from .chain import Block
from ..config import (
    DEFAULT_DIFFICULTY,
    MAX_DIFFICULTY,
    MIN_DIFFICULTY,
    TARGET_BLOCK_TIME_S,
    FUTURE_DRIFT_S,
    _ALPHA,
    MAX_ADJUSTMENT
)

class DifficultyPolicy:
    """Interface class for difficulty policies. 
    Any class that implements this should implement `get_difficulty(chain) -> int` method.
    """

    def get_difficulty(self, tip: "Block") -> int:
        """Return the difficulty to use for the next block."""
        ... 

class FixedDifficultyPolicy:
    """Difficulty is fixed for the chain, set as a paramater and doesnt change"""

    def __init__(self, difficulty: int = DEFAULT_DIFFICULTY) -> None:
        self.difficulty = difficulty

    def get_difficulty(self, tip: "Block") -> int:
        return self.difficulty

class DynamicDifficultyPolicy:
    """ Adaptive difficulty, exponential moving average.
    get_block param is a Callable that returns the Block at a given height, or None.

    The policy is stateful (it maintains the EMA). Use one instance per
    chain. If you fork-switch, either reuse the same instance (the EMA
    will adapt) or construct a fresh one for the new chain.
    """

    def __init__(self, get_block: Callable[[int], "Block | None"]) -> None:
        self._get_block = get_block
        self._ema: float = float(TARGET_BLOCK_TIME_S)
        self._last_height: int = -1

    def get_difficulty(self, tip: "Block") -> int:
        if tip.height == 0:
            return DEFAULT_DIFFICULTY

        # Update EMA only once per block height
        if tip.height != self._last_height:
            self._update_ema(tip)
            self._last_height = tip.height

        # If the ratio > 1 -> slower than target so decrease difficulty
        # if ther ation < 1 -> faster than target, so increase difficulty
        ratio = TARGET_BLOCK_TIME_S / self._ema
        ratio = max(1.0 / MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, ratio))

        new_diff = tip.difficulty * ratio
        return max(MIN_DIFFICULTY, min(MAX_DIFFICULTY, round(new_diff)))

    def _update_ema(self, tip: "Block") -> None:
        parent = self._get_block(tip.height - 1)
        if parent is None:
            # Current block is an orphan. We don't adjust difficulty.
            return

        observed = float(tip.timestamp - parent.timestamp)
        observed = max(1.0, observed)
        self._ema = _ALPHA * observed + (1.0 - _ALPHA) * self._ema

    def clamp_timestamp(self, timestamp: int, prev_timestamp: int) -> int:
        """
        Clamp a block timestamp.
        lower bound: prev_timestemp + 1, can not be before the parent block
        upper bound: wall_clock + FUTURE_DRIFT_S, shouldn't be (far) from the future
        """
        lo = prev_timestamp + 1
        hi = int(time.time()) + FUTURE_DRIFT_S
        return max(lo, min(hi, timestamp))

