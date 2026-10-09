from dataclasses import dataclass
from ipv8.messaging.payload_dataclass import DataClassPayload
from ipv8.messaging.lazy_payload import VariablePayload, vp_compile

from ..config import (
    MSG_BLOCK_RESPONSE_INNER, MSG_REGISTER_BLOCKCHAIN, MSG_REGISTER_RESPONSE, MSG_REQUEST_BLOCK, 
    MSG_SUBMIT_TX, MSG_SUBMIT_TX_RESPONSE, MSG_GET_CHAIN_HEIGHT,
    MSG_CHAIN_HEIGHT_RESPONSE, MSG_GET_BLOCK, MSG_BLOCK_RESPONSE, MSG_ANNOUNCE_BLOCK,
    MSG_MEMPOOL_TRANSACTION
)

# --------------------------------------
# Registration
# --------------------------------------
@vp_compile
@dataclass
class RegisterBlockchain(DataClassPayload[MSG_REGISTER_BLOCKCHAIN]):
    group_id: str
    community_id: bytes

    format_list = ["varlenHutf8", "varlenH"]
    names = ["group_id", "community_id"]

@vp_compile
@dataclass
class RegisterResponse(DataClassPayload[MSG_REGISTER_RESPONSE]):
    success: bool
    message: str

    format_list = ["?", "varlenHutf8"]
    names = ["success", "message"]


# --------------------------------------
# Blockchain
# --------------------------------------
@vp_compile
@dataclass
class SubmitTransaction(DataClassPayload[MSG_SUBMIT_TX]):
    """Submit transaction to mempool"""
    sender_key: bytes
    data: bytes
    timestamp: int
    signature: bytes

    format_list = ["varlenH", "varlenH", "q", "varlenH"]
    names = ["sender_key", "data", "timestamp", "signature"]


@vp_compile
@dataclass
class SubmitTransactionResponse(DataClassPayload[MSG_SUBMIT_TX_RESPONSE]):
    """Confirmation of transaction submittion"""
    success: bool
    tx_hash: bytes
    message: str

    format_list = ["?", "varlenH", "varlenHutf8"]
    names = ["success", "tx_hash", "message"]


@vp_compile
@dataclass
class GetChainHeight(DataClassPayload[MSG_GET_CHAIN_HEIGHT]):
    """Request current chain height"""
    request_id: int

    format_list = ["q"]
    names = ["request_id"]

@vp_compile
@dataclass
class ChainHeightResponse(DataClassPayload[MSG_CHAIN_HEIGHT_RESPONSE]):
    """Response with current chain height, and hash of current tip"""
    request_id: int
    height: int
    tip_hash: bytes

    format_list = ["q", "q", "varlenH"]
    names = ["request_id", "height", "tip_hash"]


@vp_compile
@dataclass
class GetBlock(DataClassPayload[MSG_GET_BLOCK]):
    """Request for a block on a given height"""
    height: int

    format_list = ["q"]
    names = ["height"]

@vp_compile
@dataclass
class BlockResponse(DataClassPayload[MSG_BLOCK_RESPONSE]):
    """Respond with requested block on the height provided in the request"""
    height: int
    prev_hash: bytes
    txs_hash: bytes
    timestamp: int
    difficulty: int
    nonce: int
    block_hash: bytes
    tx_hashes: bytes    # flat concatenated 32 byte hashes

    format_list = ["q", "varlenH", "varlenH", "q", "q", "q", "varlenH", "varlenH"]
    names = ["height", "prev_hash", "txs_hash", "timestamp", "difficulty", "nonce", "block_hash", "tx_hashes"]

# --------------------------------------
# Inner Communication
# --------------------------------------
@vp_compile
@dataclass
class AnnounceBlock(DataClassPayload[MSG_ANNOUNCE_BLOCK]):
    """Announce you just mined or received a block at this height, include the block hash"""
    height: int
    block_hash: bytes

    format_list = ["q", "varlenH"]
    names = ["height", "block_hash"]

@vp_compile
@dataclass
class MempoolTransaction(DataClassPayload[MSG_MEMPOOL_TRANSACTION]):
    """Share a pending transaction with teammates."""
    sender_key: bytes
    data: bytes
    timestamp: int
    signature: bytes

    format_list = ["varlenH", "varlenH", "q", "varlenH"]
    names = ["sender_key", "data", "timestamp", "signature"]

@vp_compile
@dataclass
class RequestBlock(DataClassPayload[MSG_REQUEST_BLOCK]):
    """Request a block on a specific height from peers"""
    height: int

    format_list = ["q"]
    names = ["height"]

@vp_compile
@dataclass
class BlockResponseInner(DataClassPayload[MSG_BLOCK_RESPONSE_INNER]):
    """Repondss with requested block"""
    height: int
    prev_hash: bytes
    txs_hash: bytes
    timestamp: int
    difficulty: int
    nonce: int
    block_hash: bytes
    tx_hashes: bytes    # flat concatenated 32 byte hashes

    format_list = ["q", "varlenH", "varlenH", "q", "q", "q", "varlenH", "varlenH"]
    names = ["height", "prev_hash", "txs_hash", "timestamp", "difficulty", "nonce", "block_hash", "tx_hashes"]
