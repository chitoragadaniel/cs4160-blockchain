from lab3.blockchain.mempool import Mempool
from lab3.blockchain.chain import Transaction

def make_tx(timestamp: int) -> Transaction:
    """Make a Transaction with a unique tx_hash derived from timestamp."""
    tx_hash = timestamp.to_bytes(32, "big")
    return Transaction(
        sender_key=b"key",
        data=b"data",
        timestamp=timestamp,
        signature=b"sig",
        tx_hash=tx_hash,
    )

# ----------------------
# Mempool
# ----------------------
def test_mempool_add_basic():
    pool = Mempool(max_size=10)
    tx = make_tx(0)

    assert pool.add(tx) is True
    assert len(pool) == 1
    assert pool.contains(tx.tx_hash)

def test_mempool_duplicate_rejected():
    pool = Mempool(max_size=10)
    tx = make_tx(0)

    assert pool.add(tx) is True
    assert pool.add(tx) is False
    assert len(pool) == 1

def test_mempool_fifo_ordering():
    """Make sure the ordering is correct"""
    pool = Mempool(key_fn = lambda tx: tx.timestamp)
    tx1 = make_tx(0)
    tx2 = make_tx(1)

    pool.add(tx1)
    pool.add(tx2)

    pending = pool.get_pending(max_count=10)
    assert len(pending) == 2
    assert pending[0] == tx2  # Later tx is first

def test_mempool_eviction_replaces_worst():
    """Check if evection works, when the new tx is better than the worst old one"""
    pool = Mempool(max_size=2, key_fn=lambda tx: tx.timestamp)
    tx1 = make_tx(0)
    tx2 = make_tx(1)

    pool.add(tx1)
    pool.add(tx2)

    tx3 = make_tx(3)
    assert pool.add(tx3) is True
    assert not pool.contains(tx1.tx_hash)
    assert len(pool) == 2

def test_mempool_eviction_rejects_worse_tx():
    """Check that if the new transaction is worse than the current worse, and the pool is full it is rejected"""
    pool = Mempool(max_size=2, key_fn=lambda tx: tx.timestamp)
    tx1 = make_tx(1)
    tx2 = make_tx(2)

    pool.add(tx1)
    pool.add(tx2)

    tx3 = make_tx(0)
    assert pool.add(tx3) is False
    assert not pool.contains(tx3.tx_hash)
    assert len(pool) == 2

def test_mempool_test_pending_correct_ordering():
    pool = Mempool(max_size=10, key_fn=lambda tx: tx.timestamp)

    for i in range(5):
        tx = make_tx(i)
        pool.add(tx)

    for i in range(10, 5, -1):
        tx = make_tx(i)
        pool.add(tx)

    pending = pool.get_pending(max_count=5)
    assert len(pending) == 5
    assert pending[0].timestamp >= pending[1].timestamp