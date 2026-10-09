import pytest
import hashlib

from lab3.blockchain.crypto import (
    compute_txs_hash, count_leading_zero_bits, hash_header,
    hash_transaction, mine_block, serialize_header, 
    satisfies_pow, sha256, deserialize_header,
)


# ----------------------
# Header 
# ----------------------
def test_header_size():
    """Header must be exactly 84 bytes."""
    prev_hash = b"\x00" * 32
    txs_hash = b"\xff" * 32
    assert len(serialize_header(prev_hash, txs_hash, 1234, 27, 42)) == 84

def test_header_roundtrip():
    """Deserializing after serialzing should result in the start values"""
    prev_hash = b"\x00" * 32
    txs_hash = b"\xff" * 32
    timestamp, difficulty, nonce = 1234, 20, 73

    packed = serialize_header(prev_hash, txs_hash, timestamp, difficulty, nonce)
    prev, txs, ts, hardness, n = deserialize_header(packed)

    assert prev == prev_hash
    assert txs == txs_hash
    assert ts == timestamp
    assert hardness == difficulty
    assert n == nonce

def test_header_big_endian():
    """Timestamp is stored correctly"""
    prev = b"\x00" * 32
    txs = b"\xff" * 32
    ts = 0x0102030405060708
    raw = serialize_header(prev, txs, ts, 0, 0)
    assert raw[64:72] == bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])


# ----------------------
# hash_transaction
# ----------------------
def test_hash_transaction_length():
    assert len(hash_transaction(b"sender_key", b"data", 12345, b"sig")) == 32

# ----------------------
# txs_hash
# ----------------------
def test_txs_hash_empty():
    """Emtpy block is sha_256(b""), not only zeros"""
    result = compute_txs_hash([])
    assert result == hashlib.sha256(b"").digest()
    assert result != b"\x00" * 32

def test_txs_hash_single():
    """Hashing works for a single transaction"""
    h = b"\xab" * 32
    assert compute_txs_hash([h]) == hashlib.sha256(h).digest()

def test_txs_hash_multiple():
    """Hashing works for a multiple transaction"""
    hashes = [bytes([i]) * 32 for i in range(2)]
    assert compute_txs_hash(hashes) == hashlib.sha256(b"".join(hashes)).digest()


# ----------------------
# Proof of Work
# ----------------------
def test_count_leading_zero_bits_all_zeros():
    """Test all bytes are zero bytes"""
    assert count_leading_zero_bits(b"\x00" * 4) == 32

def test_count_leading_zero_bits_first_byte_nonzero():
    """Test there is no zero byte, but there are leading zeros"""
    assert count_leading_zero_bits(bytes([0b00010000])) == 3

def test_count_leading_zero_byte_then_nonzero():
    """Test leading zero byte, but no zeros after"""
    assert count_leading_zero_bits(b"\x00\x80") == 8 

def test_count_leading_zero_byte_then_zerobits():
    """Test leading zero byte, but some (2) zeros after"""
    assert count_leading_zero_bits(b"\x00\x30") == 10

def test_satisfies_pow_true():
    """Test if difficulty check is correct"""
    h = b"\x00\x00" + b"\xff" * 30
    assert satisfies_pow(h, 16)

def test_satisfies_pow_false():
    """Test if difficulty check fails when not satisfied"""
    h = b"\x00\x01" + b"\x00" * 30
    assert not satisfies_pow(h, 16)  


# ----------------------
# mine_block
# ----------------------
def test_mine_block_finds_valid_nonce():
    """Test if we can mine a block. difficulty is set low to make the test fast"""
    prev = b"\x00" * 32
    txs = sha256(b"")
    ts = 12345
    difficulty = 8 

    nonce, block_hash = mine_block(prev, txs, ts, difficulty)

    assert satisfies_pow(block_hash, difficulty)
    expected = hash_header(prev, txs, ts, difficulty, nonce)
    assert expected == block_hash