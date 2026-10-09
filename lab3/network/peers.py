import logging

from ipv8.peer import Peer

from ..config import SERVER_PUBLIC_KEY_B, TEAMMATE_PUBLIC_KEYS_HEX

logger = logging.getLogger(__name__)


class TrustedPeers:
    """Util class for checking for trusted peers (members and server)"""

    def __init__(self) -> None:
        self._server_key: bytes = SERVER_PUBLIC_KEY_B
        self._teammate_keys: list[bytes] = [bytes.fromhex(key) for key in TEAMMATE_PUBLIC_KEYS_HEX]

    def is_server_b(self, peer_key: bytes) -> bool:
        return peer_key == self._server_key

    def is_teammate_b(self, peer_key: bytes) -> bool:
        return peer_key in self._teammate_keys

    def is_trusted_b(self, peer_key: bytes) -> bool:
        """Checks if peer is either a teammate, or the known server"""
        return self.is_server_b(peer_key) or self.is_teammate_b(peer_key)

    def is_server(self, peer: Peer) -> bool:
        return peer.public_key.key_to_bin() == self._server_key

    def is_teammate(self, peer: Peer) -> bool:
        return peer.public_key.key_to_bin() in self._teammate_keys

    def is_trusted(self, peer: Peer) -> bool:
        """Checks if peer is either a teammate, or the known server"""
        return self.is_server(peer) or self.is_teammate(peer)