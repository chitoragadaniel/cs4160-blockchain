import os
import logging
import asyncio
import argparse
from pathlib import Path

from .blockchain.chain import Blockchain, make_genesis_block
from .blockchain.difficulty import FixedDifficultyPolicy
from .blockchain.mempool import Mempool
from .blockchain.storage import IndexedStorage, _DATA_FILENAME, _INDEX_FILENAME, _DATA_TMP, _INDEX_TMP
from .network.peers import TrustedPeers

from .config import PERSONAL_KEY_FILE, DEFAULT_DIFFICULTY
from .network.blockchain_community import BlockchainCommunity
from .network.registering_community import RegisteringCommunity
 
from ipv8.configuration import ConfigBuilder, Strategy, WalkerDefinition, default_bootstrap_defs
from ipv8_service import IPv8

class _UnsupportedCurveFilter(logging.Filter):
    """Suppress the stream of 'Curve X is not supported' errors from old peers."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Curve" not in msg and "is not supported" not in msg
 
logging.getLogger("RegisteringCommunity").addFilter(_UnsupportedCurveFilter())
logging.basicConfig(level=logging.INFO)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key_path", required=False, help="path to the key file (default = PERSONAL_KEY_FILE)", default=PERSONAL_KEY_FILE)
    parser.add_argument("--register", action="store_false", required=False, help="Whether or not to register to the server")
    parser.add_argument("--start_fresh", action="store_true", required=False, help="Wheter or not to delete current chain storage")
    args = parser.parse_args()

    # Per-key storage dir so multiple nodes on one machine don't share (clobber) a chain.
    storage_path = Path("assignment_3", "wal", Path(args.key_path).stem)
    if args.start_fresh:
        for name in (_DATA_FILENAME, _INDEX_FILENAME, _DATA_TMP, _INDEX_TMP):
            path = Path(storage_path, name)
            if os.path.exists(path):
                print(f"Delete the current chain storage at: {path}")
                os.remove(path)

    # Crash-safe indexed store: O(1) reads, background compaction, header-only pruning.
    storage = IndexedStorage(storage_path)
    # Single genesis only. Heights 1+ must be mined (real PoW) — seeding unmined blocks makes them fail
    blockchain = Blockchain(make_genesis_block(DEFAULT_DIFFICULTY), storage=storage)
    storage.start_compaction_worker()
    mempool = Mempool(max_size=1000)
    trusted_peers = TrustedPeers()
    # Fixed difficulty: DynamicDifficultyPolicy scales the *bit count* multiplicatively, but work
    # is exponential in bits (20 -> 30 bits = 1024x the hashes). A few fast blocks push it to a
    # difficulty no machine can mine, the tip freezes, and the EMA never updates again.
    difficulty_policy = FixedDifficultyPolicy(DEFAULT_DIFFICULTY)
     
 
    builder = ConfigBuilder()
    builder.clear_keys()
    builder.clear_overlays()
    builder.add_key("my peer", "curve25519", str(args.key_path))
    builder.add_overlay(
        "BlockchainCommunity",
        "my peer",
        [WalkerDefinition(Strategy.RandomWalk, 20, {"timeout": 5.0})],
        default_bootstrap_defs,
        {
            "chain": blockchain,
            "mempool": mempool,
            "trusted_peers": trusted_peers,
            "difficulty_policy": difficulty_policy
        },
        [("started",)],
        False,
    )

    if not args.register:
        builder.add_overlay(
            "RegisteringCommunity",
            "my peer",
            [WalkerDefinition(Strategy.RandomWalk, 20, {"timeout": 5.0})],
            default_bootstrap_defs,
            {
                "chain": blockchain,
                "mempool": mempool,
                "trusted_peers": trusted_peers,
                "difficulty_policy": difficulty_policy
            },
            [],
            False,
        )
        extra_communities = {
            "BlockchainCommunity": BlockchainCommunity,
            "RegisteringCommunity": RegisteringCommunity
        }
    else:
        extra_communities = {"BlockchainCommunity": BlockchainCommunity}
        
    ipv8 = IPv8(
        builder.finalize(),
        extra_communities=extra_communities,
    )
 
    # TODO mb choose 1 teammate to register, and the rest can just start blockchain community
    await ipv8.start()
    print("IPv8 started, waiting for server peer...\n")
 
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await ipv8.stop()
 
 
if __name__ == "__main__":
    asyncio.run(main())