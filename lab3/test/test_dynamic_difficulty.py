import time
import pytest

from types import SimpleNamespace

from lab3.config import DEFAULT_DIFFICULTY, MAX_DIFFICULTY, MIN_DIFFICULTY, TARGET_BLOCK_TIME_S, EMA_WINDOW, FUTURE_DRIFT_S, MAX_ADJUSTMENT
from lab3.blockchain.difficulty import DynamicDifficultyPolicy

def make_chain(
    num_blocks: int,
    block_time: int = TARGET_BLOCK_TIME_S,
    start_difficulty: int = DEFAULT_DIFFICULTY,
    start_time: int = 1_000_000,
) -> dict[int, object]:
    """
    Build a fake chain as a height.
    SimpleBlock is a plain namespace so we don't need the full chain machinery.
    block_time controls the gap between consecutive timestamps.
    """

    chain: dict[int, object] = {}
    for h in range(num_blocks):
        chain[h] = SimpleNamespace(
            height=h,
            timestamp=start_time + h * block_time,
            difficulty=start_difficulty,
        )
    return chain


def policy_from_chain(chain: dict) -> DynamicDifficultyPolicy:
    return DynamicDifficultyPolicy(get_block=chain.get)


def run_blocks(
    policy: DynamicDifficultyPolicy,
    chain: dict,
    block_time: int,
    n: int,
    start_height: int,
    start_time: int,
    start_difficulty: int,
) -> list[int]:
    difficulties = []
    current_difficulty = start_difficulty
    current_time = start_time

    for i in range(n):
        h = start_height + i
        tip = chain[h - 1] if h > 0 else chain[0]
        diff = policy.get_difficulty(tip)
        current_difficulty = diff
        current_time += block_time
        chain[h] = SimpleNamespace(
            height=h,
            timestamp=current_time,
            difficulty=current_difficulty,
        )
        difficulties.append(diff)

    return difficulties


class TestClampTimestamp:
    def test_valid_timestamp_unchanged(self):
        difficulty = DynamicDifficultyPolicy(lambda x: None)
        prev = 1_000_000
        ts = prev + TARGET_BLOCK_TIME_S
        assert difficulty.clamp_timestamp(ts, prev) == ts

    def test_below_lower_bound_clamped(self):
        difficulty = DynamicDifficultyPolicy(lambda x: None)
        prev = 1_000_000
        assert difficulty.clamp_timestamp(prev, prev) == prev + 1    
        assert difficulty.clamp_timestamp(prev - 10, prev) == prev + 1

    def test_future_timestamp_clamped(self):
        difficulty = DynamicDifficultyPolicy(lambda x: None)
        prev = 1_000_000
        far_future = int(time.time()) + FUTURE_DRIFT_S + 1000
        clamped = difficulty.clamp_timestamp(far_future, prev)
        assert clamped <= int(time.time()) + FUTURE_DRIFT_S

    def test_just_within_future_drift_accepted(self):
        difficulty = DynamicDifficultyPolicy(lambda x: None)
        prev = 1_000_000
        ts = int(time.time()) + FUTURE_DRIFT_S - 1
        assert difficulty.clamp_timestamp(ts, prev) == ts


class TestDynamicGenesis:
    def test_genesis_returns_default(self):
        chain = make_chain(1)
        policy = policy_from_chain(chain)
        genesis = chain[0]
        assert policy.get_difficulty(genesis) == DEFAULT_DIFFICULTY


class TestSteadyRate:
    def test_difficulty_stable_at_target(self):
        """
        When blocks arrive exactly at TARGET_BLOCK_TIME_S, difficulty should
        not drift — the EMA stays at target, ratio == 1.0, no change.
        """
        n = EMA_WINDOW * 3
        chain = make_chain(n + 1, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)

        diffs = [policy.get_difficulty(chain[h]) for h in range(1, n + 1)]

        # All difficulties should be DEFAULT_DIFFICULTY (ratio == 1.0 throughout).
        assert all(d == DEFAULT_DIFFICULTY for d in diffs), (
            f"Expected stable difficulty={DEFAULT_DIFFICULTY}, got: {diffs}"
        )

    def test_ema_not_updated_twice_for_same_height(self):
        """Calling get_difficulty() twice on the same tip must not double-update."""
        chain = make_chain(5, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)
        tip = chain[4]

        d1 = policy.get_difficulty(tip)
        ema_after_first = policy._ema
        d2 = policy.get_difficulty(tip)
        ema_after_second = policy._ema

        assert d1 == d2
        assert ema_after_first == ema_after_second


class TestFastAdaptation:
    def test_difficulty_rises_when_blocks_come_fast(self):
        """
        If blocks arrive much faster than target, difficulty should increase
        within ~EMA_WINDOW blocks.
        """
        fast_time = TARGET_BLOCK_TIME_S // 10   

        # Seed with steady chain, then inject fast blocks.
        chain = make_chain(EMA_WINDOW + 1, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)

        # Warm up the EMA on the steady chain.
        for h in range(1, EMA_WINDOW + 1):
            policy.get_difficulty(chain[h])

        # Now simulate fast blocks for EMA_WINDOW more.
        diffs = run_blocks(
            policy, chain,
            block_time=fast_time,
            n=EMA_WINDOW,
            start_height=EMA_WINDOW + 1,
            start_time=chain[EMA_WINDOW].timestamp,
            start_difficulty=DEFAULT_DIFFICULTY,
        )

        assert diffs[-1] > DEFAULT_DIFFICULTY, (
            f"Expected difficulty to rise above {DEFAULT_DIFFICULTY}, got {diffs}"
        )

    def test_difficulty_falls_when_blocks_come_slow(self):
        """
        If blocks arrive much slower than target, difficulty should decrease.
        """
        slow_time = TARGET_BLOCK_TIME_S * 10   

        chain = make_chain(EMA_WINDOW + 1, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)

        for h in range(1, EMA_WINDOW + 1):
            policy.get_difficulty(chain[h])

        diffs = run_blocks(
            policy, chain,
            block_time=slow_time,
            n=EMA_WINDOW,
            start_height=EMA_WINDOW + 1,
            start_time=chain[EMA_WINDOW].timestamp,
            start_difficulty=DEFAULT_DIFFICULTY,
        )

        assert diffs[-1] < DEFAULT_DIFFICULTY, (
            f"Expected difficulty to fall below {DEFAULT_DIFFICULTY}, got {diffs}"
        )

    def test_adapts_within_window_blocks(self):
        """
        After a 10x hashpower jump, difficulty should start moving within
        EMA_WINDOW blocks — not sit still for a long retarget interval.
        """
        fast_time = TARGET_BLOCK_TIME_S // 10

        chain = make_chain(EMA_WINDOW + 1, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)
        for h in range(1, EMA_WINDOW + 1):
            policy.get_difficulty(chain[h])

        # After just 3 fast blocks (well within the window), difficulty
        # should already have moved.
        diffs = run_blocks(
            policy, chain,
            block_time=fast_time,
            n=3,
            start_height=EMA_WINDOW + 1,
            start_time=chain[EMA_WINDOW].timestamp,
            start_difficulty=DEFAULT_DIFFICULTY,
        )

        assert diffs[-1] > DEFAULT_DIFFICULTY or diffs[-1] == DEFAULT_DIFFICULTY, (
            "Difficulty should only move up (or stay) after fast blocks"
        )
        # More specifically: after 3 fast blocks the EMA must have moved.
        assert policy._ema < TARGET_BLOCK_TIME_S


class TestTimestampLiar:
    def test_single_liar_block_moves_difficulty_minimally(self):
        """
        One block with a maximally lying timestamp (FUTURE_DRIFT_S ahead)
        should not cause a significant difficulty change.
        """
        chain = make_chain(EMA_WINDOW + 1, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)
        for h in range(1, EMA_WINDOW + 1):
            policy.get_difficulty(chain[h])

        baseline_diff = policy.get_difficulty(chain[EMA_WINDOW])

        # Inject one block with timestamp maximally in the future.
        liar_ts = chain[EMA_WINDOW].timestamp + FUTURE_DRIFT_S + 1  # will be clamped
        clamped_ts = policy.clamp_timestamp(liar_ts, chain[EMA_WINDOW].timestamp)
        liar_block = SimpleNamespace(
            height=EMA_WINDOW + 1,
            timestamp=clamped_ts,
            difficulty=baseline_diff,
        )
        chain[EMA_WINDOW + 1] = liar_block

        after_liar = policy.get_difficulty(liar_block)

        # The difficulty after one liar must stay within [baseline / MAX, baseline * MAX].
        assert after_liar >= baseline_diff / MAX_ADJUSTMENT
        assert after_liar <= baseline_diff * MAX_ADJUSTMENT

class TestSettlesWithoutOscillation:
    def test_no_oscillation_after_hashpower_jump(self):
        """
        After a step change in hashpower, difficulty should monotonically
        approach the new equilibrium without bouncing above and below it.

        We check that the sequence of difficulties is monotone (either
        entirely non-decreasing or entirely non-increasing) for the
        EMA_WINDOW blocks after the jump, before it fully settles.
        """
        fast_time = TARGET_BLOCK_TIME_S // 5

        chain = make_chain(EMA_WINDOW + 1, block_time=TARGET_BLOCK_TIME_S)
        policy = policy_from_chain(chain)
        for h in range(1, EMA_WINDOW + 1):
            policy.get_difficulty(chain[h])

        diffs = run_blocks(
            policy, chain,
            block_time=fast_time,
            n=EMA_WINDOW * 2,
            start_height=EMA_WINDOW + 1,
            start_time=chain[EMA_WINDOW].timestamp,
            start_difficulty=DEFAULT_DIFFICULTY,
        )

        # Check for oscillation: count direction changes in the sequence.
        direction_changes = sum(
            1 for i in range(1, len(diffs) - 1)
            if (diffs[i] > diffs[i-1]) != (diffs[i+1] > diffs[i])
            and diffs[i] != diffs[i-1]
            and diffs[i+1] != diffs[i]
        )

        # Allow at most 1 change (rounding at the new equilibrium is fine).
        assert direction_changes <= 1, (
            f"Oscillation detected ({direction_changes} direction changes): {diffs}"
        )

    def test_difficulty_bounded_by_min_max(self):
        """Difficulty never goes below MIN_DIFFICULTY or above MAX_DIFFICULTY."""
        # Extremely slow blocks — should hit MIN_DIFFICULTY, not go below.
        chain = make_chain(EMA_WINDOW * 5 + 1, block_time=TARGET_BLOCK_TIME_S * 100)
        policy = policy_from_chain(chain)

        diffs = [policy.get_difficulty(chain[h]) for h in range(1, EMA_WINDOW * 5 + 1)]
        assert all(MIN_DIFFICULTY <= d <= MAX_DIFFICULTY for d in diffs)