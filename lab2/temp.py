import asyncio
import logging

from typing import cast
from pathlib import Path
 
from ipv8.keyvault.crypto import ECCrypto
from ipv8.peerdiscovery.network import PeerObserver
from ipv8.community import Community
from ipv8.configuration import ConfigBuilder, Strategy, WalkerDefinition, default_bootstrap_defs
from ipv8.lazy_community import lazy_wrapper
from ipv8.messaging.lazy_payload import VariablePayload, vp_compile
from ipv8.peer import Peer
from ipv8.keyvault.keys import PrivateKey
from ipv8_service import IPv8

class _UnsupportedCurveFilter(logging.Filter):
    """Suppress the stream of 'Curve X is not supported' errors from old peers."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Curve" not in msg and "is not supported" not in msg
 
logging.getLogger("Lab2Community").addFilter(_UnsupportedCurveFilter())
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("Lab2Community")

KEY_FILE = "assignment_1/key.pem"
SERVER_PUBLIC_KEY_HEX = "4c69624e61434c504b3a82e33614a342774e084af80835838d6dbdb64a537d3ddb6c1d82011a7f101553cda40cf5fa0e0fc23abd0a9c4f81322282c5b34566f6b8401f5f683031e60c96"
COMMUNITY_ID_HEX = "4c61623247726f75705369676e696e6732303236"


PUBLIC_KEYS = [
    b'LibNaCLPK:*Q\xc3\xf7\xaa\x87]#NS\x0cL\xa8\xba\xe4pb\xaf\x82\xdd\x1bE\xb2&\xf8\xfc\x81e\xce\xbc\x91\n8\xfcJ\x92\xd5\xccq\xc0\xdf\xd8\x85\xebr\xa1\x06,ve\xe9yQN\xeewe\x9b\x84\xaeLd\xf6V', 
    b'LibNaCLPK:\xee\xe1\xefN\xf0\xb0&\xf4\xb7]#\x10\x9e\x16\x87\xbb\x86%%\xdeG"\xd2\x86\xb2\xb5\xf7:\x04\xee\x078n\xd8\xf8)\xbd\xbb8\x13;\xb5\xd0D\xa0\x95\x94\xb97\xdd>&\x8a\rf\x8f >\xdf\xd4.c\x0c\xa6', 
    b'LibNaCLPK:\x08<\xceB\x06UC\x07\x99\xb7O\xb4\x1c\x1c\xa5\xa2$\xe6,T[\xd0\x1c9\x93u\xfa0\xfb/\xadw\x03\xf9\x07\x98\x9c\x04\x9be\x04,\xa5\xd1\xb5Dqi\x11a\xb6)\xcbyr\xae\xcd,\xc8?\xe0J4\xbd'
    ]
NR_TO_MEMBER = {
    0: "4c69624e61434c504b3a2a51c3f7aa875d234e530c4ca8bae47062af82dd1b45b226f8fc8165cebc910a38fc4a92d5cc71c0dfd885eb72a1062c7665e979514eee77659b84ae4c64f656",
    1: "4c69624e61434c504b3aeee1ef4ef0b026f4b75d23109e1687bb862525de4722d286b2b5f73a04ee07386ed8f829bdbb38133bb5d044a09594b937dd3e268a0d668f203edfd42e630ca6",
    2: "4c69624e61434c504b3a083cce420655430799b74fb41c1ca5a224e62c545bd01c399375fa30fb2fad7703f907989c049b65042ca5d1b54471691161b629cb7972aecd2cc83fe04a34bd",
}
MEMBER_TO_NR = {
    "4c69624e61434c504b3a2a51c3f7aa875d234e530c4ca8bae47062af82dd1b45b226f8fc8165cebc910a38fc4a92d5cc71c0dfd885eb72a1062c7665e979514eee77659b84ae4c64f656": 0,
    "4c69624e61434c504b3aeee1ef4ef0b026f4b75d23109e1687bb862525de4722d286b2b5f73a04ee07386ed8f829bdbb38133bb5d044a09594b937dd3e268a0d668f203edfd42e630ca6": 1,
    "4c69624e61434c504b3a083cce420655430799b74fb41c1ca5a224e62c545bd01c399375fa30fb2fad7703f907989c049b65042ca5d1b54471691161b629cb7972aecd2cc83fe04a34bd": 2
}

@vp_compile
class RegisterPayload(VariablePayload):
    msg_id = 1
    format_list = ["varlenH", "varlenH", "varlenH"]
    names = ["member1_key", "member2_key", "member3_key"]

@vp_compile
class ResponsePayload(VariablePayload):
    msg_id = 2
    format_list = ["?", "varlenHutf8", "varlenHutf8"]
    names = ["success", "group_id", "message"]

@vp_compile
class ChallengeRequest(VariablePayload):
    msg_id=3
    format_list = ["varlenHutf8"]
    names = ["group_id"]

@vp_compile
class ChallengeResponse(VariablePayload):
    msg_id=4
    format_list = ["varlenH", "q", "d" ]
    names = ["nonce", "round_number", "deadline"]

@vp_compile
class BundleSubmission(VariablePayload):
    msg_id=5
    format_list = ["varlenHutf8", "q", "varlenH", "varlenH", "varlenH"]
    names = ["group_id", "round_number", "sig1", "sig2", "sig3"]

@vp_compile
class RoundResult(VariablePayload):
    msg_id=6
    format_list = ["?", "q", "q", "varlenHutf8"]
    names = ["success", "round_number", "rounds_completed", "message"]

@vp_compile
class ReadyPayload(VariablePayload):
    """Ready msg, to share we are ready to start the challenges. Only share when the cache is full."""
    msg_id = 99
    format_list = ["varlenHutf8"]
    names = ["group_id"]

@vp_compile
class ChallengeInternalNonceDist(VariablePayload):
    msg_id = 98
    format_list = ["varlenHutf8", "varlenH", "q"]
    names = ["group_id", "nonce", "round_nr"]

@vp_compile
class ChallengeInternalResponse(VariablePayload):
    msg_id = 97
    format_list = ["varlenHutf8", "varlenH", "q", "q"]
    names = ["group_id", "sig", "round_nr", "idx"]

@vp_compile
class InternalStartNextRoundNotice(VariablePayload):
    msg_id = 96
    format_list = ["varlenHutf8", "q"]
    names = ["group_id", "new_round_nr"]


class Lab2Community(Community, PeerObserver):
    community_id = bytes.fromhex(COMMUNITY_ID_HEX)
 
    def __init__(self, settings):
        super().__init__(settings)

        self._group_id = "b54c463cf3ba5295" 
        self._my_index = settings.my_index
        self._member_to_nr= settings.member_to_nr
        self._nr_to_member = settings.nr_to_member
        self._server_pk = settings.server_pk
        self._sk = None

        # discovery
        self.server_peer = None
        self._nr_to_peer = {}
        self.all_present = False
        
        # ready handshake
        self.teammates_ready = {}
        self.all_ready = False
        self._groups_updated_team_ready = {}

        # my round
        self._signed_nonces = {}

        # handlers
        self.add_message_handler(ReadyPayload, self._on_ready)
        self.add_message_handler(ResponsePayload, self._on_response_payload)
        self.add_message_handler(ChallengeResponse, self._on_challenge_response)
        self.add_message_handler(ChallengeInternalResponse, self._on_internal_sig_response)
        self.add_message_handler(ChallengeInternalNonceDist, self._on_internal_nonce_dist)
        self.add_message_handler(RoundResult, self._on_round_result)
        self.add_message_handler(InternalStartNextRoundNotice, self._on_next_round_notice)
    
    def started(self):
        print("-- STARTED COMMUNITY --")
        print(f"Own mid: {self.my_peer.mid.hex()}")
        print(f"My key end: {self.my_peer.public_key.key_to_bin().hex()[-20:]}"
        if peer in self.teammates_ready.keys():
            return

        self.teammates_ready[peer] = self._member_to_nr[sender_key]
        print(f"Teammate {self.teammates_ready[peer]} ready ({len(self.teammates_ready)}/3)")

        if len(self.teammates_ready.keys()) == 3:
            print("EVERYONE READY, nr 1 will start challenge"))
        print(f"my idx: {self._my_index}")
        self.network.add_peer_observer(self)
        self._nr_to_peer[self._my_index] = self.my_peer
        self._sk: PrivateKey = cast(PrivateKey, self.my_peer.key) 

    def on_peer_removed(self, peer: Peer) -> None:
        peer_pk = peer.public_key.key_to_bin().hex()
        print(f"PEER REMOVED: {peer}, with pk: {peer_pk[-20:]}")
        return

    def on_peer_added(self, peer: Peer):
        if self.all_present:
            return

        peer_pk = peer.public_key.key_to_bin().hex()
        print(f"FOUND PEER: {peer}, with pk:\n{peer_pk[-20:]}")

        if is_server(peer):
            print("SERVER FOUND\n")
            self.server_peer = peer
            print("REGISTERING GROUP")
            # self.ez_send(self.server_peer, RegisterPayload(PUBLIC_KEYS[0], PUBLIC_KEYS[1], PUBLIC_KEYS[2]))
        
        if peer_pk in self._member_to_nr.keys():
            nr = self._member_to_nr[peer_pk]
            print(f"Found group mate: {nr}\n")
            self._nr_to_peer[nr] = peer
        
        if self.server_peer and len(self._nr_to_peer.keys()) == 3 and self._group_id:
            self.all_present = True
            self.teammates_ready[self.my_peer] = self._my_index
            print(f"FOUND EVERYONE, and group id: {self._group_id}, continuing to protocol")
            self._broadcast_ready()

    @lazy_wrapper(ReadyPayload)
    def _on_ready(self, peer: Peer, payload: ReadyPayload) -> None:
        sender_key = peer.public_key.key_to_bin().hex()
        if sender_key not in self._member_to_nr.keys():
            print(f"ReadyPayload from unregistered peer {peer},\n", payload)
            return
        
        if peer in self.teammates_ready.keys():
            return

        self.teammates_ready[peer] = self._member_to_nr[sender_key]
        print(f"Teammate {self.teammates_ready[peer]} ready ({len(self.teammates_ready)}/3)")

        if len(self.teammates_ready.keys()) == 3:
            print("EVERYONE READY, nr 1 will start challenge")
            if self._my_index == 0:
                self._start_challenge_rounds()


    @lazy_wrapper(ResponsePayload)
    def _on_response_payload(self, peer: Peer, payload: ResponsePayload) -> None:
        print(f"RECEIVED REGISTRATION RESPONSE FROM THE SERVER: {peer},\npayload: {payload}")
        self._group_id = payload.group_id
        if self.server_peer and len(self._nr_to_peer.keys()) == 3 and self._group_id:
            self.all_present = True
            print(f"FOUND EVERYONE, and groupid: {self._group_id}, continuing to protocol")
            self._broadcast_ready()


    def _broadcast_ready(self) -> None:
        payload = ReadyPayload(group_id=self._group_id)
        for peer in self._nr_to_peer.values():
            self.ez_send(peer, payload)

        print(f"ReadyPayload broadcast to {len(self.teammates_ready.keys())} teammate(s). teammates: ", self.teammates_ready.values())
    
    def _start_challenge_rounds(self):
        print(f"Sending Challenge of round: {self._my_index} to server")
        assert self.server_peer, "self.server_peer was NONE"
        self.ez_send(self.server_peer, ChallengeRequest(self._group_id))

    @lazy_wrapper(ChallengeResponse)
    def _on_challenge_response(self, peer: Peer, payload: ChallengeResponse) -> None:
        if not peer is self.server_peer:
            print(f"GOT CHALLENGE RESPONSE FROM NON SERVER.\npeer: {peer},\npayload:{payload}\n")
            return
        
        nonce = payload.nonce
        for nr, peer in self._nr_to_peer.items():
            if nr == self._my_index:
                continue
            
            self.ez_send(peer, ChallengeInternalNonceDist(self._group_id, nonce, self._my_index))

        assert self._sk, "OWN SECRET KEY WAS NONE"
        self._signed_nonces[self._my_index] = self.crypto.create_signature(self._sk, nonce)
        
    @lazy_wrapper(ChallengeInternalResponse)
    def _on_internal_sig_response(self, peer: Peer, payload: ChallengeInternalResponse) -> None:
        print(f"RECEIVED SIGNED NOCE BACK FROM PEER: {peer},\npayload: {payload}")

        self._signed_nonces[payload.idx] = payload.sig      
        if len(self._signed_nonces) == 3:
            print("RECEIVED ALL NONCES, SENDING TO SERVER")
            assert self.server_peer, "SERVER PEER WAS NONE"
            self.ez_send(self.server_peer, BundleSubmission(self._group_id, self._my_index + 1, self._signed_nonces[0], self._signed_nonces[1], self._signed_nonces[2]))
            print("Send back singed nonce")
        else:
            print(f"DIDNT RECEIVE ALL SIGNED NONCE YET. CURRENTLY KNOW: {self._signed_nonces.keys()}")

    @lazy_wrapper(ChallengeInternalNonceDist)
    def _on_internal_nonce_dist(self, peer: Peer, payload: ChallengeInternalNonceDist) -> None:
        print(f"RECEIVED NONCE FROM PEER: {peer},\npayload: {payload}")
        assert self._sk, "PRIVATE KEY WAS NONE"
        signed_nonce = self.crypto.create_signature(self._sk, payload.nonce)
        self.ez_send(peer, ChallengeInternalResponse(self._group_id, signed_nonce, payload.round_nr, self._my_index))
        print("responsed")

    @lazy_wrapper(RoundResult)
    def _on_round_result(self, peer: Peer, payload: RoundResult) -> None:
        print(f"RECEIVED ROUND RESPONSE FORM SERVER: {peer},\npayload: {payload}")

        if payload.success:
            if self._my_index < 2:
                print("LETTING NEXT NOTE KNOW TO START NEW ROUND")
                self.ez_send(self._nr_to_peer[self._my_index+1], InternalStartNextRoundNotice(self._group_id, self._my_index+1))
                return
            if self._my_index == 2:
                print("THIS WAS LAST ROUND")
                return
        else:
            print("ABORTING ROUND FAILED")

    @lazy_wrapper(InternalStartNextRoundNotice)
    def _on_next_round_notice(self, peer: Peer, payload: InternalStartNextRoundNotice) -> None:
        print(f"RECEIVED NEXT ROUND MSG FROM PEER: {peer},\npayload: {payload}")
        if payload.new_round_nr == self._my_index:
            self._start_challenge_rounds()
            
        
        
        
def is_server(peer: Peer):
    return peer.public_key.key_to_bin().hex() == SERVER_PUBLIC_KEY_HEX

async def main():
    key_path = Path(KEY_FILE)
 
    builder = ConfigBuilder()
    builder.clear_keys()
    builder.clear_overlays()
    builder.add_key("my peer", "curve25519", str(key_path))
    builder.add_overlay(
        "Lab2Community",
        "my peer",
        [WalkerDefinition(Strategy.RandomWalk, 30, {"timeout": 5.0})],
        default_bootstrap_defs,
        {
            "community_id": COMMUNITY_ID_HEX,
            "nr_to_member": NR_TO_MEMBER,
            "member_to_nr": MEMBER_TO_NR,
            "my_index": 2,
            "server_pk": SERVER_PUBLIC_KEY_HEX 
        },
        [("started",)],
        False,
    )
 
    ipv8 = IPv8(builder.finalize(), extra_communities={"Lab2Community": Lab2Community})
    await ipv8.start()
    print("IPv8 started, waiting for server peer...\n")
 
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await ipv8.stop()
 
 
if __name__ == "__main__":
    asyncio.run(main())


