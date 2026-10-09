import asyncio
import logging

from .payloads import RegisterBlockchain, RegisterResponse

from ..config import BLOCKCHAIN_COMMUNITY_ID, GROUP_ID, REGISTRATION_COMMUNITY_ID_B
from ipv8.community import Community
from ipv8.lazy_community import lazy_wrapper
from ipv8.peer import Peer

class _UnsupportedCurveFilter(logging.Filter):
    """Suppress the stream of 'Curve X is not supported' errors from old peers."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Curve" not in msg and "is not supported" not in msg
 
logging.getLogger("RegisteringCommunity").addFilter(_UnsupportedCurveFilter())
logger = logging.getLogger(__name__)

class RegisteringCommunity(Community):
    """Community, only used for registring our community to the server
    Everytime we register, the server will immediatly try to test our blockchain, if some of our nodes are not 
    ready etc, it will retry 3 times, and times out after 5 minutes.
    To try again, we need to register again.
    """
    community_id = REGISTRATION_COMMUNITY_ID_B

    # Longer than the server's ~5-minute attempt window, so a re-registration never cuts a
    # still-running verification attempt short (re-registering resets the attempt batch).
    RE_REGISTER_INTERVAL_S = 420.0

    def __init__(self, settings):
        super().__init__(settings)
        self._trusted_peers = settings.trusted_peers
        self.server_peer = None
        self.submitted = False
        self.passed = False

        self.add_message_handler(RegisterResponse, self.on_response)
        self.register_task("status", self._log_status, interval=10.0, delay=10.0)
 
    def _log_status(self):
        peers = self.get_peers()
        logger.debug(f"[registering status] {len(peers)} peer(s) in community, server_peer={self.server_peer is not None}")
 
        if self.submitted or self.server_peer is not None:
            return
 
        for peer in peers:
            try:
                if self._trusted_peers.is_server(peer):
                    logger.info("Server peer found!")
                    self.server_peer = peer
                    asyncio.ensure_future(self.submit())
                    return
            except Exception:
                continue
 
    def peer_added(self, peer: Peer) -> None:
        logger.debug(f"peer_added() called for: {peer}")
 
        if self._trusted_peers.is_server(peer):
            logger.info("Server peer discovered!")
            self.server_peer = peer
            asyncio.ensure_future(self.submit())
        else:
            logger.debug(f"Found peer that was not server, mid: {peer.mid}")
 
    def peer_removed(self, peer: Peer) -> None:
        if self._trusted_peers.is_server(peer):
            logger.warning("WARNING: Server peer disconnected.")
            self.server_peer = None
 
    async def submit(self):
        """Submit method, to actually send registration to the server"""
        if self.submitted:
            return

        assert self.server_peer, "Server peer was none when registering"
        self.ez_send(self.server_peer, RegisterBlockchain(GROUP_ID, BLOCKCHAIN_COMMUNITY_ID))
        self.submitted = True
 
    @lazy_wrapper(RegisterResponse)
    async def on_response(self, peer: Peer, payload: RegisterResponse):
        """Handle registration response from the server"""
        if not self._trusted_peers.is_server(peer):
            logger.debug(f"Ignoring registration response from non-server peer. payload: {payload}")
            return

        print(f"[Register] Done — success={payload.success} message={payload.message}")
        self.logger.info(f"Registration response from server. success={payload.success}, message={payload.message}")

        if "already passed" in payload.message.lower():
            # The pass is recorded server-side (sticky). Nothing left to do.
            self.passed = True
            self.cancel_pending_task("re_register")
            return

        # Registered, verification attempts are (re)starting.
        # Re-register after the attempt window has fully elapsed.
        self.replace_task("re_register", self._re_register, delay=self.RE_REGISTER_INTERVAL_S)

    def _re_register(self):
        """Allow a fresh RegisterBlockchain to find out whether the group passed (or retry)."""
        if self.passed:
            return

        self.submitted = False
        if self.server_peer is not None:
            asyncio.ensure_future(self.submit())