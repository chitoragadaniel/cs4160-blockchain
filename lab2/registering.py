from ipv8.keyvault.crypto import ECCrypto
from typing import List
import asyncio
import hashlib
import logging
import struct
from pathlib import Path
 
from ipv8.community import Community
from ipv8.configuration import ConfigBuilder, Strategy, WalkerDefinition, default_bootstrap_defs
from ipv8.lazy_community import lazy_wrapper
from ipv8.messaging.lazy_payload import VariablePayload
from ipv8.peer import Peer
from ipv8_service import IPv8

SERVER_PUBLIC_KEY_HEX = "4c69624e61434c504b3a82e33614a342774e084af80835838d6dbdb64a537d3ddb6c1d82011a7f101553cda40cf5fa0e0fc23abd0a9c4f81322282c5b34566f6b8401f5f683031e60c96"
COMMUNITY_ID_HEX = "4c61623247726f75705369676e696e6732303236"
PUBLIC_KEYS = [
    b'LibNaCLPK:*Q\xc3\xf7\xaa\x87]#NS\x0cL\xa8\xba\xe4pb\xaf\x82\xdd\x1bE\xb2&\xf8\xfc\x81e\xce\xbc\x91\n8\xfcJ\x92\xd5\xccq\xc0\xdf\xd8\x85\xebr\xa1\x06,ve\xe9yQN\xeewe\x9b\x84\xaeLd\xf6V', 
    b'LibNaCLPK:\xee\xe1\xefN\xf0\xb0&\xf4\xb7]#\x10\x9e\x16\x87\xbb\x86%%\xdeG"\xd2\x86\xb2\xb5\xf7:\x04\xee\x078n\xd8\xf8)\xbd\xbb8\x13;\xb5\xd0D\xa0\x95\x94\xb97\xdd>&\x8a\rf\x8f >\xdf\xd4.c\x0c\xa6', 
    b'LibNaCLPK:\x08<\xceB\x06UC\x07\x99\xb7O\xb4\x1c\x1c\xa5\xa2$\xe6,T[\xd0\x1c9\x93u\xfa0\xfb/\xadw\x03\xf9\x07\x98\x9c\x04\x9be\x04,\xa5\xd1\xb5Dqi\x11a\xb6)\xcbyr\xae\xcd,\xc8?\xe0J4\xbd'
    ]
PRIVATE_KEYS = ["faizel.pem", "daniel.pem", "ruben.pem"]
KEY_FILE = "assignment_1/key.pem"

class _UnsupportedCurveFilter(logging.Filter):
    """Suppress the stream of 'Curve X is not supported' errors from old peers."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Curve" not in msg and "is not supported" not in msg
 
logging.getLogger("Lab2Community").addFilter(_UnsupportedCurveFilter())
logging.basicConfig(level=logging.DEBUG)

def retrieve_public_keys(private_keys: List[str]) -> List[bytes]:
    # Read stored private key from file
    public_key_bytes = []
    for i in range(len(private_keys)):
        with open(private_keys[i], "rb") as f:
            private_key_bytes = f.read()

        crypto = ECCrypto()

        private_key = crypto.key_from_private_bin(private_key_bytes)

        public_key = private_key.pub()

        public_key_bytes.append(public_key.key_to_bin())
    print(public_key_bytes)
    return(public_key_bytes)

class RegisterPayload(VariablePayload):
    msg_id = 1
    format_list = ["varlenH", "varlenH", "varlenH"]
    names = ["member1_key", "member2_key", "member3_key"]

class ResponsePayload(VariablePayload):
    msg_id = 2
    format_list = ["?", "varlenHutf8", "varlenHutf8"]
    names = ["success", "group_id", "message"]

class Lab2Community(Community):
    community_id = bytes.fromhex(COMMUNITY_ID_HEX)
 
    def __init__(self, settings):
        super().__init__(settings)
        self.server_peer = None
        self.submitted = False
        self.add_message_handler(ResponsePayload, self.on_response)
        self.register_task("status", self._log_status, interval=10.0, delay=10.0)
 
    def _log_status(self):
        peers = self.get_peers()
        print(f"[status] {len(peers)} peer(s) in community, server_peer={self.server_peer is not None}")
 
        if self.submitted or self.server_peer is not None:
            return
 
        for peer in peers:
            try:
                if peer.public_key.key_to_bin() == bytes.fromhex(SERVER_PUBLIC_KEY_HEX):
                    print("Server peer found!")
                    self.server_peer = peer
                    asyncio.ensure_future(self.submit())
                    return
            except Exception:
                continue
 
    def peer_added(self, peer: Peer) -> None:
        print(f"peer_added() called for: {peer}")
        try:
            key = peer.public_key.key_to_bin()
        except Exception as e:
            print(f"  Could not read key: {e}")
            return
 
        print(f"  Key: {key.hex()[:20]}...")
        if key == bytes.fromhex(SERVER_PUBLIC_KEY_HEX):
            print("  -> Server peer discovered!")
            self.server_peer = peer
            asyncio.ensure_future(self.submit())
        else:
            print(f"  -> Non-server peer, skipping.")
 
    def peer_removed(self, peer: Peer) -> None:
        try:
            key = peer.public_key.key_to_bin()
        except Exception:
            return
 
        if key == bytes.fromhex(SERVER_PUBLIC_KEY_HEX):
            print("WARNING: Server peer disconnected.")
            self.server_peer = None
 
    async def submit(self):
        if self.submitted:
            return
        self.submitted = True
 
        public_keys = PUBLIC_KEYS
  
        self.ez_send(self.server_peer, RegisterPayload(public_keys[0], public_keys[1], public_keys[2]))
 
    @lazy_wrapper(ResponsePayload)
    async def on_response(self, peer: Peer, payload: ResponsePayload):
        expected_key = bytes.fromhex(SERVER_PUBLIC_KEY_HEX)
        try:
            sender_key = peer.public_key.key_to_bin()
        except Exception:
            print("WARNING: Could not read public key from response sender — ignoring.")
            return
 
        if sender_key != expected_key:
            print("WARNING: Ignoring response from non-server peer.")
            return
 
        print("\n==============================")
        print("SERVER RESPONSE")
        print("==============================")
        print(f"Success: {payload.success}")
        print(f"group_id: {payload.group_id}")
        print(f"Message: {payload.message}")
        print("==============================\n")
 
        asyncio.get_event_loop().stop()
 
 
# ============================================================
# MAIN
# ============================================================
 
async def main():
    key_path = Path(KEY_FILE)
 
    builder = ConfigBuilder()
    builder.clear_keys()
    builder.clear_overlays()
 
    builder.add_key("my peer", "curve25519", str(key_path))
 
    builder.add_overlay(
        "Lab2Community",
        "my peer",
        [WalkerDefinition(Strategy.RandomWalk, 20, {"timeout": 5.0})],
        default_bootstrap_defs,
        {},
        [],
        -1,
    )
 
    ipv8 = IPv8(
        builder.finalize(),
        extra_communities={"Lab2Community": Lab2Community},
    )
 
    await ipv8.start()
    print("IPv8 started, waiting for server peer...\n")
 
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await ipv8.stop()
 
 
if __name__ == "__main__":
    asyncio.run(main())


