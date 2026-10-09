import time

from lab3.blockchain.chain import Block, Blockchain, Transaction, make_genesis_block
from lab3.blockchain.crypto import compute_txs_hash, hash_transaction, mine_block, sha256

def make_valid_block(chain: Blockchain, difficulty: int = 4, ts: int | None = None) -> Block:
    """Helper method to make a valid (empty) block. Pass `ts` to force a distinct block hash
    (two empty blocks with the same parent and timestamp would otherwise be identical)."""
    tip = chain.tip
    txs_hash = compute_txs_hash([])
    ts = int(time.time()) if ts is None else ts
    nonce, block_hash = mine_block(tip.block_hash, txs_hash, ts, difficulty)

    return Block(
        height=tip.height + 1,
        prev_hash=tip.block_hash,
        txs_hash=txs_hash,
        timestamp=ts,
        difficulty=difficulty,
        nonce=nonce,
        block_hash=block_hash,
        transactions=(),
    )


# --------------------------------------
# Genesis (test invariants)
# --------------------------------------
def test_genesis_height_zero():
    genesis = make_genesis_block()
    assert genesis.height == 0

def test_genesis_prev_hash_zero():
    genesis = make_genesis_block()
    assert genesis.prev_hash == b"\x00" * 32

def test_genesis_txs_hash_empty():
    genesis = make_genesis_block()
    assert genesis.txs_hash == sha256(b"")

def test_genesis_deterministic():
    """Genesis block should always be the same"""
    genesis = make_genesis_block()
    genesis = make_genesis_block()
    assert genesis.block_hash == genesis.block_hash


# --------------------------------------
# Block validation
# --------------------------------------
def test_block_verify_pow_valid():
    chain = Blockchain(make_genesis_block())
    block = make_valid_block(chain)
    assert block.verify_pow()

def test_block_verify_pow_wrong_hash():
    """Make sure pow fails, with a bad block hash"""
    chain = Blockchain(make_genesis_block())
    block = make_valid_block(chain)
    bad_block = Block(
        height=block.height,
        prev_hash=block.prev_hash,
        txs_hash=block.txs_hash,
        timestamp=block.timestamp,
        difficulty=block.difficulty,
        nonce=block.nonce,
        block_hash=b"\xff" * 32,
        transactions=(),
    )
    assert not bad_block.verify_pow()

def test_block_verify_txs_hash_empty():
    """Check that an empty block has correct txs hash"""
    chain = Blockchain(make_genesis_block())
    block = make_valid_block(chain)
    assert block.verify_txs_hash()

def test_block_verify_txs_hash_with_tx():
    """Check if hashses are also correct, when transaction sare non empty"""
    tx_hash = hash_transaction(b"key", b"data", 1234, b"sig")
    txs_hash = compute_txs_hash([tx_hash])
    tip = make_genesis_block()
    ts = int(time.time())
    nonce, block_hash = mine_block(tip.block_hash, txs_hash, ts, 4)

    tx = Transaction(
        sender_key=b"key",
        data=b"data",
        timestamp=1000,
        signature=b"sig",
        tx_hash=tx_hash,
    )
    block = Block(
        height=1,
        prev_hash=tip.block_hash,
        txs_hash=txs_hash,
        timestamp=ts,
        difficulty=4,
        nonce=nonce,
        block_hash=block_hash,
        transactions=(tx,),
    )
    assert block.verify_txs_hash()


# --------------------------------------
# Blockchain
# --------------------------------------
def test_add_block_extends_chain():
    """Check if we can correctly add a valid block to the chain"""
    chain = Blockchain(make_genesis_block())
    block = make_valid_block(chain)
    assert chain.add_block(block)
    assert chain.height == 1
    assert chain.tip == block

def test_add_block_wrong_height_rejected():
    """Check that a block with the wrong height is rejected, and chain is not updated"""
    chain = Blockchain(make_genesis_block())
    tip = chain.tip
    block = make_valid_block(chain)
    bad_block = Block(
        height=2,
        prev_hash=block.prev_hash,
        txs_hash=block.txs_hash,
        timestamp=block.timestamp,
        difficulty=block.difficulty,
        nonce=block.nonce,
        block_hash=block.block_hash,
        transactions=(),
    )
    assert not chain.add_block(bad_block)
    assert chain.height == 0
    assert chain.tip == tip

def test_add_block_wrong_prev_hash_rejected():
    """Check block is rejected if prev_hash is wrong"""
    chain = Blockchain(make_genesis_block())
    tip = chain.tip
    txs_hash = sha256(b"")
    ts = int(time.time())
    bad_prev = b"\xaa" * 32  
    nonce, block_hash = mine_block(bad_prev, txs_hash, ts, 4)
    block = Block(
        height=1,
        prev_hash=bad_prev,
        txs_hash=txs_hash,
        timestamp=ts,
        difficulty=4,
        nonce=nonce,
        block_hash=block_hash,
        transactions=(),
    )
    assert not chain.add_block(block)

def test_chain_get_block():
    chain = Blockchain(make_genesis_block())
    assert chain.get_block(0) is not None
    assert chain.get_block(1) is None

def test_fork_switch_longer_chain_wins():
    """A competing branch that becomes strictly taller should take over the main chain"""
    chain = Blockchain(make_genesis_block())
    b1 = make_valid_block(chain)
    chain.add_block(b1)

    # build a competing fork, that split from genesis (distinct timestamps -> distinct hashes)
    genesis = chain.get_block(0)
    assert genesis
    fork_chain = Blockchain(genesis)
    f1 = make_valid_block(fork_chain, ts=b1.timestamp + 1000)
    fork_chain.add_block(f1)
    f2 = make_valid_block(fork_chain, ts=b1.timestamp + 2000)
    fork_chain.add_block(f2)

    # f1 ties our height (1): deterministic tie-break -> the smaller block hash wins
    r1 = chain.add_block(f1)
    assert r1.added
    winner = min(b1, f1, key=lambda b: b.block_hash)
    assert chain.tip == winner
    assert r1.extended_tip == (winner is f1)

    # f2 makes the fork height 2 > our 1 -> reorg onto it
    r2 = chain.add_block(f2)
    assert r2.added and r2.extended_tip
    assert chain.height == 2
    assert chain.tip == f2
    # b1 is no longer on the main chain (f1/f2 branch took over)
    assert chain.get_block(1) == f1

def test_fork_switch_shorter_chain_ignored():
    """A competing branch shorter than the current chain must not take over"""
    chain = Blockchain(make_genesis_block())
    b1 = make_valid_block(chain)
    chain.add_block(b1)
    b2 = make_valid_block(chain)
    chain.add_block(b2)

    # forked chain with lenght 1
    genesis = chain.get_block(0)
    assert genesis
    fork_chain = Blockchain(genesis)
    f1 = make_valid_block(fork_chain, ts=b1.timestamp + 1000)
    fork_chain.add_block(f1)

    r = chain.add_block(f1)
    assert r.added and not r.extended_tip   # stored, but no reorg
    assert chain.height == 2
    assert chain.tip == b2


def test_orphan_adopted_when_parent_arrives():
    """A block whose parent we don't have yet is parked, then adopted once the parent connects"""
    chain = Blockchain(make_genesis_block())
    # mine a real parent/child pair on a throwaway chain (same deterministic genesis)
    builder = Blockchain(make_genesis_block())
    b1 = make_valid_block(builder)
    builder.add_block(b1)
    b2 = make_valid_block(builder)
    builder.add_block(b2)

    # deliver the child first: parent unknown -> parked as orphan, tip unchanged
    r_child = chain.add_block(b2)
    assert not r_child.added
    assert r_child.is_orphan
    assert r_child.missing_parent == b1.block_hash
    assert chain.height == 0

    # now the parent arrives: it connects AND pulls in the parked child
    r_parent = chain.add_block(b1)
    assert r_parent.added and r_parent.extended_tip
    assert chain.height == 2
    assert chain.tip == b2


def test_orphan_pool_bounded():
    """The orphan pool must not grow without bound (DoS guard)"""
    chain = Blockchain(make_genesis_block(), max_orphans=1)

    builder = Blockchain(make_genesis_block())
    b1 = make_valid_block(builder)
    builder.add_block(b1)
    b2 = make_valid_block(builder)                          # parent b1 is unknown to chain -> orphan

    other = Blockchain(make_genesis_block())
    x1 = make_valid_block(other, ts=b1.timestamp + 1000)    # distinct from b1 -> distinct subtree
    other.add_block(x1)
    x2 = make_valid_block(other, ts=b1.timestamp + 2000)    # parent x1 is unknown to chain -> orphan

    chain.add_block(b2)                                     # parked, pool now full (size 1)
    assert chain.knows(b2.block_hash)

    chain.add_block(x2)                                     # pool full -> dropped
    assert not chain.knows(x2.block_hash)