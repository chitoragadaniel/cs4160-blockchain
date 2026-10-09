import hashlib
import struct

# --------------------------------------
# Header functions
# --------------------------------------
# --------------------------------------
# Block header layout (84 bytes total)
# --------------------------------------
# Offset  Size  Field
#   0     32    prev_hash
#  32     32    txs_hash
#  64      8    timestamp  (unix seconds)
#  72      4    difficulty (nr leading zeros)
#  76      8    nonce      
# --------------------------------------

HEADER_STRUCT = struct.Struct(">32s32sQIQ") 
assert HEADER_STRUCT.size == 84
def serialize_header(prev_hash: bytes, txs_hash: bytes, timestamp: int, difficulty: int, nonce: int) -> bytes:
    """Serialize a block header into 84-byte"""
    return HEADER_STRUCT.pack(prev_hash, txs_hash, timestamp, difficulty, nonce)


def deserialize_header(raw: bytes) -> tuple[bytes, bytes, int, int, int]:
    """Deserialize 84 bytes header to ordered ruple, returns (prev_hash, txs_hash, timestamp, difficulty, nonce)"""
    return HEADER_STRUCT.unpack(raw)


def hash_header(prev_hash: bytes, txs_hash: bytes, timestamp: int, difficulty: int, nonce: int) -> bytes:
    """Returns sha-256 of the packed header."""
    return sha256(serialize_header(prev_hash, txs_hash, timestamp, difficulty, nonce))

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

# --------------------------------------
# Transaction functions
# --------------------------------------
# --------------------------------------
# Transaction layout (134+ bytes total)
# --------------------------------------
# Offset  Size   Field
#   0      32    sender_key
#  32       8    timestamp   (unix seconds)
#  40      64    signature
# 104      32    tx_hash
# 134      ...   data        (arbitrary payload, is just the rest of the bytes)
# --------------------------------------
TX_STRUCT = struct.Struct(">32sQ64s32s")
assert TX_STRUCT.size == 136

def serialize_transaction(sender_key: bytes, timestamp: int, signature: bytes, tx_hash: bytes, data: bytes) -> bytes:
    """Serialize an entire transaction, we add the unbounded data bytes at the end"""
    return TX_STRUCT.pack(sender_key, timestamp, signature, tx_hash) + data

def deserialize_transaction(raw: bytes) -> tuple[bytes, int, bytes, bytes, bytes]:
    """Deserialize a transaction"""
    sender_key, timestamp, signature, tx_hash = TX_STRUCT.unpack(raw[:TX_STRUCT.size])
    data = raw[TX_STRUCT.size:]
    return sender_key, timestamp, signature, tx_hash, data

def hash_transaction(sender_key: bytes, data: bytes, timestamp: int, signature: bytes) -> bytes:
    """Hash a transaction, converts int timestamp to 8 byte binary representation"""
    timestamp_bytes = timestamp.to_bytes(8, "big")
    return sha256(sender_key + data + timestamp_bytes + signature)


def compute_txs_hash(tx_hashes: list[bytes]) -> bytes:
    """Computes txs_hash (hash over transaction hashes) -> SHA256(tx_hash_1 || ... || tx_hash_n) 
    Note that an empty block uses SHA256(b""), and is not just 32 0 bytes.
    """
    return sha256(b"".join(tx_hashes))


# --------------------------------------
# Proof of Work
# --------------------------------------
# We precompute LEADING_ZEROS_BIT_TABLE, because it will probably be called a bunch
LEADING_ZEROS_BIT_TABLE = [8 - i.bit_length() for i in range(256)]
def count_leading_zero_bits(data: bytes) -> int:
    """Get the nr of leading zero bits in the byte string"""
    count = 0
    for byte in data:
        if byte == 0:
            count += 8
        else:
            return count + LEADING_ZEROS_BIT_TABLE[byte]

    return count

def satisfies_pow(block_hash: bytes, difficulty: int) -> bool:
    """Check if block_hash has at least `difficulty` leading zero bits, returns true if this is the case"""
    return count_leading_zero_bits(block_hash) >= difficulty

def mine_block(prev_hash: bytes, txs_hash: bytes, timestamp: int, difficulty: int, start_nonce: int = 0, count = 2**64, step: int = 1) -> tuple[int | None, bytes | None]:
    """Search for a nonce that satisfies the PoW requirement, returns (nonce, block_hash), or (None, None) if not found in range [start_nonce, start_nonce + count)"""
    for nonce in range(start_nonce, start_nonce + count, step):
        candidate = serialize_header(prev_hash, txs_hash, timestamp, difficulty, nonce)
        block_hash = sha256(candidate)

        if satisfies_pow(block_hash, difficulty):
            return nonce, block_hash
        nonce += 1

    return (None, None)