import asyncio
import logging

from ..blockchain import crypto
from ..blockchain.chain import Block, Blockchain, Transaction
from ..blockchain.difficulty import DifficultyPolicy
from ..blockchain.mempool import Mempool
from ..blockchain.miner import Miner
from .payloads import AnnounceBlock, BlockResponse, BlockResponseInner, ChainHeightResponse, GetBlock, GetChainHeight, MempoolTransaction, RequestBlock, SubmitTransaction, SubmitTransactionResponse
from .peers import TrustedPeers
from ..config import BLOCKCHAIN_COMMUNITY_ID

from ipv8.community import Community, lazy_wrapper
from ipv8.peer import Peer

class _UnsupportedCurveFilter(logging.Filter):
    """Suppress the stream of 'Curve X is not supported' errors from old peers."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Curve" not in msg and "is not supported" not in msg
 
logging.getLogger("BlockchainCommunity").addFilter(_UnsupportedCurveFilter())
logger = logging.getLogger(__name__)

class BlockchainCommunity(Community):
    """The community that actually runs our blockchain"""
    community_id = BLOCKCHAIN_COMMUNITY_ID
    CHAIN_SYNC_INTERVAL_S = 10.0
    CHAIN_SYNC_START_DELAY_S = 5.0
 
    def __init__(self, settings):
        super().__init__(settings)
        
        # Inject all dependencies via the settings
        self._chain: Blockchain = settings.chain
        self._mempool: Mempool = settings.mempool
        self._trusted_peers: TrustedPeers = settings.trusted_peers
        self._difficulty_policy: DifficultyPolicy = settings.difficulty_policy

        # Thead safe miner, which can run in the background
        self._miner = Miner(
            mempool=self._mempool,
            difficulty_policy=self._difficulty_policy,
            on_block_mined=self._on_block_mined_thread_safe
        )
        
        # Msg handlers
        self.add_message_handler(SubmitTransaction, self.on_submit_transaction)
        self.add_message_handler(GetChainHeight, self.on_get_chain_height)
        self.add_message_handler(ChainHeightResponse, self.on_chain_height_response)
        self.add_message_handler(GetBlock, self.on_get_block)

        self.add_message_handler(AnnounceBlock, self.on_announce_block)
        self.add_message_handler(MempoolTransaction, self.on_mempool_transaction)
        self.add_message_handler(RequestBlock, self.on_request_block)
        self.add_message_handler(BlockResponseInner, self.on_block_response_internal)

        self._seen_tx_hashes: set[bytes] = set()
        self._chain_sync_request_id = 0
        self._sync_targets: dict[bytes, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def started(self) -> None:
        # Capture the running loop here (main thread). Miner threads can't get it themselves.
        self._loop = asyncio.get_running_loop()
        self._miner.start(self._chain.tip)
        self.register_task(
            "chain_sync",
            self._sync_chains,
            interval=self.CHAIN_SYNC_INTERVAL_S,
            delay=self.CHAIN_SYNC_START_DELAY_S,
        )
 

    def peer_added(self, peer: Peer) -> None:
        if self._trusted_peers.is_server(peer):
            logger.info("Found the server")
        elif self._is_sync_peer(peer):
            if self._trusted_peers.is_teammate(peer):
                logger.info(f"Found a teammate, mid: {peer.mid}")
            else:
                logger.info(f"Found a non-server blockchain peer, mid: {peer.mid}")
            self._request_chain_height(peer)
            self._share_mempool_with(peer)
        else:
            logger.debug(f"Peer joined the community, neiter server not teammate. mid: {peer.mid}")

 
    def peer_removed(self, peer: Peer) -> None:
        if self._trusted_peers.is_server(peer):
            logger.warning("Server left the community")
        elif self._trusted_peers.is_teammate(peer):
            logger.warning(f"Teammate left the community, mid: {peer.mid}")
        else:
            logger.debug(f"Peer left the community. Was neither server nor teammate. mid: {peer.mid}")


    # --------------------------------------
    # Server communication
    # --------------------------------------
    @lazy_wrapper(SubmitTransaction)
    def on_submit_transaction(self, peer: Peer, payload: SubmitTransaction) -> None:
        """A peer submits a transaction to the mempool
        We need to:
            - Verify the signature
            - Add the transaction to the mempool
            - Share the transaction with teammates
            - Respond
        """
        logger.info(f"Peer: {peer}, submitted transaction: {payload}")

        tx_hash = self._transaction_hash_from_payload(payload)
        tx = self._transaction_from_payload(payload, f"Submit transaction from peer: {peer}")
        if tx is None:
            self.logger.warning(f"Submittransaction from peer: {peer}, had invalid signature")
            self.ez_send(
                peer, 
                SubmitTransactionResponse(
                    success=False,
                    tx_hash=tx_hash,
                    message="invalid signature"
                )
            )
            return
        
        accepted, added = self._add_transaction_to_mempool(tx)
        if not accepted:
            self.logger.warning(f"Failed to add tx: {tx} to the mempool: {self._mempool}")
            self.ez_send(
                peer,
                SubmitTransactionResponse(
                    success=False,
                    tx_hash=tx.tx_hash, 
                    message="Failed to add to mempool. Pool is full and priority is too low."
                )
            )
            return

        self.ez_send(
            peer, 
            SubmitTransactionResponse(
                success=True,
                tx_hash=tx.tx_hash, 
                message="Added transaction to pool" if added else "Transaction already known"
            )
        )
        if added:
            self._broadcast_transaction(tx)
        self.logger.debug(f"Submittransaction from peer: {peer}, was successfully added")


    # --------------------------------------
    # Mempool sync
    # --------------------------------------
    def _transaction_hash_from_payload(self, payload) -> bytes:
        """Compute the transaction hash, or return zeroes if the payload is malformed."""
        try:
            return crypto.hash_transaction(payload.sender_key, payload.data, payload.timestamp, payload.signature)
        except (OverflowError, ValueError):
            return b"\x00" * 32

    def _transaction_from_payload(self, payload, source: str) -> Transaction | None:
        """Verify a transaction payload and convert it into a Transaction."""
        if payload.timestamp < 0:
            logger.warning(f"{source}: transaction timestamp is negative")
            return None

        try:
            sender_key = self.crypto.key_from_public_bin(payload.sender_key)
            timestamp_bytes = payload.timestamp.to_bytes(8, "big")
            transaction_data = payload.sender_key + payload.data + timestamp_bytes
            if not self.crypto.is_valid_signature(sender_key, transaction_data, payload.signature):
                return None

            return Transaction(
                sender_key=payload.sender_key,
                data=payload.data,
                timestamp=payload.timestamp,
                signature=payload.signature,
                tx_hash=crypto.hash_transaction(payload.sender_key, payload.data, payload.timestamp, payload.signature),
            )
        except Exception as exc:
            logger.warning(f"{source}: failed to decode or verify transaction: {exc}")
            return None

    def _add_transaction_to_mempool(self, tx: Transaction) -> tuple[bool, bool]:
        """Add a transaction if new. Returns (accepted, added_now)."""
        if tx.tx_hash in self._seen_tx_hashes or self._mempool.contains(tx.tx_hash):
            self._seen_tx_hashes.add(tx.tx_hash)
            return True, False

        if not self._mempool.add(tx):
            return False, False

        self._seen_tx_hashes.add(tx.tx_hash)
        self._miner.mine(self._chain.tip)
        return True, True

    @staticmethod
    def _transaction_has_body(tx: Transaction) -> bool:
        """Header-sync placeholders only carry tx_hash; mempool txs carry sender/signature."""
        return bool(tx.sender_key) and bool(tx.signature)

    def _broadcast_transaction(self, tx: Transaction, exclude_peer: Peer | None = None) -> None:
        """Share a transaction with all teammate peers except the optional sender."""
        payload = MempoolTransaction(
            sender_key=tx.sender_key,
            data=tx.data,
            timestamp=tx.timestamp,
            signature=tx.signature,
        )
        sent = 0
        for peer in self._teammate_peers():
            if exclude_peer is not None and peer.mid == exclude_peer.mid:
                continue

            self.ez_send(peer, payload)
            sent += 1

        logger.debug(f"Broadcast transaction {tx.tx_hash.hex()[:12]}... to {sent} teammate peer(s)")

    def _share_mempool_with(self, peer: Peer) -> None:
        """Send all currently pending transactions to a newly discovered teammate."""
        pending = self._mempool.get_pending(max_count=self._mempool.max_size)
        for tx in pending:
            self.ez_send(
                peer,
                MempoolTransaction(
                    sender_key=tx.sender_key,
                    data=tx.data,
                    timestamp=tx.timestamp,
                    signature=tx.signature,
                )
            )

        logger.debug(f"Shared {len(pending)} pending transaction(s) with peer {peer}")

    @lazy_wrapper(MempoolTransaction)
    def on_mempool_transaction(self, peer: Peer, payload: MempoolTransaction) -> None:
        """Accept a pending transaction gossiped by a teammate and relay it once."""
        if not self._is_sync_peer(peer):
            logger.debug(f"Ignoring mempool transaction from non-sync peer: {peer}")
            return

        tx = self._transaction_from_payload(payload, f"Mempool transaction from peer: {peer}")
        if tx is None:
            logger.warning(f"Rejected invalid mempool transaction from peer: {peer}")
            return

        was_known = tx.tx_hash in self._seen_tx_hashes or self._mempool.contains(tx.tx_hash)
        accepted, added = self._add_transaction_to_mempool(tx)
        if not accepted:
            logger.debug(f"Rejected gossiped transaction {tx} because the mempool is full")
            return

        if added and not was_known:
            logger.info(f"Accepted gossiped transaction {tx} from peer {peer}")
            self._broadcast_transaction(tx, exclude_peer=peer)


    # --------------------------------------
    # Chain sync
    # --------------------------------------
    def _is_sync_peer(self, peer: Peer) -> bool:
        """Peers in this blockchain community, except the grading server, can help sync."""
        return not self._trusted_peers.is_server(peer)

    def _teammate_peers(self) -> list[Peer]:
        """Return peers that can participate in internal block and mempool sync."""
        peers = [peer for peer in self.get_peers() if self._is_sync_peer(peer)]
        trusted_count = sum(1 for peer in peers if self._trusted_peers.is_teammate(peer))
        if peers and trusted_count != len(peers):
            logger.debug(
                f"Using {len(peers)} non-server sync peer(s); "
                f"{trusted_count} matched configured teammate key(s)."
            )
        return peers

    def _sync_chains(self) -> None:
        """Periodically ask teammates for their chain height."""
        peers = self._teammate_peers()
        if not peers:
            logger.debug("Chain sync skipped, no teammate peers available")
            return

        for peer in peers:
            self._request_chain_height(peer)

    def _request_chain_height(self, peer: Peer) -> None:
        """Request the current chain height from one teammate."""
        self._chain_sync_request_id += 1
        self.ez_send(peer, GetChainHeight(self._chain_sync_request_id))
        logger.debug(f"Requested chain height from peer {peer}, request_id={self._chain_sync_request_id}")

    def _request_next_missing_block(self, peer: Peer) -> None:
        """Request the next block needed to catch up to a peer."""
        target_height = self._sync_targets.get(peer.mid)
        if target_height is None:
            return

        next_height = self._chain.height + 1
        if next_height > target_height:
            self._sync_targets.pop(peer.mid, None)
            logger.debug(f"Finished syncing with peer {peer}, local height={self._chain.height}")
            return

        logger.info(f"Requesting missing block {next_height}/{target_height} from peer {peer}")
        self.ez_send(peer, RequestBlock(next_height))

    @lazy_wrapper(ChainHeightResponse)
    def on_chain_height_response(self, peer: Peer, payload: ChainHeightResponse) -> None:
        """Handle a teammate's response to our periodic height request."""
        if not self._is_sync_peer(peer):
            logger.debug(f"Ignoring chain height response from non-sync peer: {peer}")
            return

        if payload.height > self._chain.height:
            logger.info(
                f"Peer {peer} is ahead. peer_height={payload.height}, "
                f"local_height={self._chain.height}"
            )
            self._sync_targets[peer.mid] = payload.height
            self._request_next_missing_block(peer)
            return

        if payload.height < self._chain.height:
            logger.debug(
                f"Peer {peer} is behind. peer_height={payload.height}, "
                f"local_height={self._chain.height}. Announcing our tip."
            )
            self.ez_send(peer, AnnounceBlock(self._chain.height, self._chain.tip.block_hash))
            return

        if payload.tip_hash != self._chain.tip.block_hash:
            logger.info(
                f"Peer {peer} has a different tip at height {payload.height}. "
                "Requesting the peer's tip block to inspect the fork."
            )
            self.ez_send(peer, RequestBlock(payload.height))


    @lazy_wrapper(GetChainHeight)
    def on_get_chain_height(self, peer: Peer, payload: GetChainHeight) -> None:
        """Handle a request for the current chain height"""
        current_tip = self._chain.tip
        self.logger.debug(f"Handeling get chain height request from peer: {peer}. Current tip: {current_tip}. Current height: {current_tip.height}")
        self.ez_send(
            peer,
            ChainHeightResponse(
                request_id=payload.request_id,
                height=current_tip.height,
                tip_hash=current_tip.block_hash
            )
        )
        if self._is_sync_peer(peer):
            self._share_mempool_with(peer)


    @lazy_wrapper(GetBlock)
    def on_get_block(self, peer: Peer, payload: GetBlock) -> None:
        """Handle a request for a block on a specific height"""
        block = self._chain.get_block(payload.height)
        if not block:
            self.logger.warning(f"Couldnt find a block a requested height: {payload.height}")
            return  # TODO no way to send failure right? mb other peers will have this height already
        
        tx_hashes = b"".join([transaction.tx_hash for transaction in block.transactions])
        self.ez_send(
            peer,
            BlockResponse(
                height=block.height,
                prev_hash=block.prev_hash,
                txs_hash=block.txs_hash,
                timestamp=block.timestamp,
                difficulty=block.difficulty,
                nonce=block.nonce,
                block_hash=block.block_hash,
                tx_hashes=tx_hashes
            )
        )
        self.logger.debug(f"Successfully returned requested block at height: {payload.height} to peer: {peer}")

        
    
    # --------------------------------------
    # Internal communication
    # --------------------------------------
    @lazy_wrapper(AnnounceBlock)
    def on_announce_block(self, peer: Peer, payload: AnnounceBlock) -> None:
        """Handle a block announcement. If we dont have this block yet, we will request more info for it"""
        if payload.height > self._chain.height:
            logger.info(f"Received block announcement from peer: {peer}, with height: {payload.height}, this is bigger then our own height: {self._chain.height}")
            self.ez_send(peer, RequestBlock(payload.height))
            return

        #TODO if this, or lower height, probably should be in forks (should we store it? we still assume longest chain is valid)
        elif payload.height == self._chain.height and not self._chain.contains(payload.block_hash):
            logger.info(f"Received block announcement from peer: {peer}, at same height: {payload.height}, but unkown hash. Possible fork.") 
            self.ez_send(peer, RequestBlock(payload.height))
            return
        
        else:
            logger.debug(f"Received block announcement from peer {peer}, with height {payload.height}, our height is: {self._chain.height}, ignoring announcmeent.")

    @lazy_wrapper(RequestBlock)
    def on_request_block(self, peer: Peer, payload: RequestBlock) -> None:
        """Handle an internal block request from a teammate. Same lookup as on_get_block, but we
        reply with BlockResponseInner so the requester's on_block_response_internal handler fires."""
        block = self._chain.get_block(payload.height)
        if not block:
            self.logger.warning(f"Couldnt find a block a requested height: {payload.height}")
            return  # TODO no way to send failure right? mb other peers will have this height already

        tx_hashes = b"".join([transaction.tx_hash for transaction in block.transactions])
        self.ez_send(
            peer,
            BlockResponseInner(
                height=block.height,
                prev_hash=block.prev_hash,
                txs_hash=block.txs_hash,
                timestamp=block.timestamp,
                difficulty=block.difficulty,
                nonce=block.nonce,
                block_hash=block.block_hash,
                tx_hashes=tx_hashes
            )
        )
        self.logger.info(f"Successfully returned requested block at height: {payload.height} to peer: {peer}")

    def _block_from_internal_response(self, payload: BlockResponseInner) -> Block | None:
        """Decode and validate a block received from a teammate."""
        if len(payload.tx_hashes) % 32 != 0:
            logger.warning(
                f"Internal block response: can't decode tx hashes. "
                f"Nr bytes not divisible by 32, len: {len(payload.tx_hashes)}"
            )
            return None

        tx_hashes = [payload.tx_hashes[i:i+32] for i in range(0, len(payload.tx_hashes), 32)]

        txs_hash_check = crypto.compute_txs_hash(tx_hashes)
        if txs_hash_check != payload.txs_hash:
            logger.warning("Internal block response: txs_hash mismatch, rejecting block")
            return None

        header_hash_check = crypto.hash_header(payload.prev_hash, payload.txs_hash, payload.timestamp, payload.difficulty, payload.nonce)
        if header_hash_check != payload.block_hash:
            logger.warning("Internal block response: blockhash didnt match header hash, rejecting block")
            return None

        transactions = tuple(
            Transaction(sender_key=b"", data=b"", timestamp=0, signature=b"", tx_hash=tx_hash)
            for tx_hash in tx_hashes
        )
        block = Block(
            block_hash=payload.block_hash,
            prev_hash=payload.prev_hash,
            txs_hash=payload.txs_hash,
            timestamp=payload.timestamp,
            difficulty=payload.difficulty,
            nonce=payload.nonce,
            height=payload.height,
            transactions=transactions,
        )

        if not block.is_valid():
            logger.warning("Internal block response: invalid block, rejecting it")
            return None

        return block

    @lazy_wrapper(BlockResponseInner)
    def on_block_response_internal(self, peer: Peer, payload: BlockResponseInner) -> None:
        """Handle Block response inner. We verify the block, and possibly add it to our chain."""
        if not self._is_sync_peer(peer):
            logger.debug(f"Ignoring internal block response from non-sync peer: {peer}")
            return

        block = self._block_from_internal_response(payload)
        if block is None:
            return

        result = self._chain.add_block(block)

        if result.is_orphan:
            # Parent unknown: the chain has parked this block. Ask for the previous height so we
            # can backfill towards a block we already have. (height-based backfill for now.)
            logger.info(f"Internal block response from peer {peer}: parked orphan at height {block.height}, "
                        f"requesting parent at height {block.height - 1}")
            self.ez_send(peer, RequestBlock(block.height - 1))
            return

        if result.extended_tip:
            self.logger.debug(f"Internal block response from peer {peer}: tip changed (added={result.added})")
            self._reconcile_after_chain_update(result)

        if result.added or self._chain.knows(block.block_hash):
            self._request_next_missing_block(peer)

    def _reconcile_after_chain_update(self, result) -> None:
        """After a block was added, keep the mempool and miner consistent with the new best chain."""
        if not result.extended_tip:
            return  # block landed on a side branch; main chain unchanged

        # Transactions that left the main chain in a reorg go back to the mempool...
        for block in result.reverted:
            for tx in block.transactions:
                if self._transaction_has_body(tx):
                    self._mempool.add(tx)
        # ...and transactions now confirmed on the main chain are removed.
        for block in result.applied:
            self._seen_tx_hashes.update(block.tx_hashes)
            self._mempool.remove_included(block.tx_hashes)

        # Mine from the new tip.
        self._miner.mine(self._chain.tip)


    def _announce_block(self, block: Block) -> None:
        """Broadcast the block to all sync peers (the server never needs our internal messages)"""
        payload = AnnounceBlock(block.height, block.block_hash)
        for peer in self._teammate_peers():
            self.ez_send(peer, payload)
                
            
    # --------------------------------------
    # Mining
    # --------------------------------------
    def _on_block_mined_thread_safe(self, block: Block) -> None:
        """Called from the mining thread, when a block is mined. schedule the result in the event loop to make it thread safe."""
        self._loop.call_soon_threadsafe(self._on_block_mined, block)
    
    def _on_block_mined(self, block: Block) -> None:
        """Callback method that handles the actual logic of a mined block"""
        result = self._chain.add_block(block)
        if not result.added:
            # Could be that during mining we already got this tip from a peer, or the tip moved on.
            self.logger.warning(f"Mined block was rejected from the chain. block: {block}")
            self._miner.mine(self._chain.tip)  # start mining a new block, from the current tip
            return

        # The block was successfully added.
        # - reconcile the mempool against the (possibly reorged) main chain
        # - announce the block to all peers
        # - keep mining from the current tip
        logger.info(f"Block added to chain: {block}")
        self._reconcile_after_chain_update(result)
        if not result.extended_tip:
            # Our block landed on a side branch (an equal-height peer block won the tie-break),
            # so reconcile skipped the miner restart. Restart here, otherwise every worker
            # stays paused and this node stops mining for good.
            self._miner.mine(self._chain.tip)
        self._announce_block(block)
