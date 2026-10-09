from __future__ import annotations
import os
import sys
import time
import asyncio
import logging

from dataclasses import dataclass, field
from typing import Dict, Optional, cast

from dotenv import load_dotenv
from ipv8.peer import Peer
from ipv8.community import Community, CommunitySettings
from ipv8.keyvault.keys import PrivateKey
from ipv8.lazy_community import lazy_wrapper
from ipv8.messaging.payload_dataclass import DataClassPayload
from ipv8.configuration import ConfigBuilder
from ipv8.peerdiscovery.network import PeerObserver

class _UnsupportedCurveFilter(logging.Filter):
    """Suppress the stream of 'Curve X is not supported' errors from old peers."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Curve" not in msg and "is not supported" not in msg
 
logging.getLogger("Lab2Community").addFilter(_UnsupportedCurveFilter())
logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger("Lab2")


@dataclass
class ChallengeRequestPayload(DataClassPayload[3]):
    group_id: str

@dataclass
class ChallengeResponsePayload(DataClassPayload[4]):
    nonce: bytes
    round_number: int
    deadline: float

@dataclass
class SignatureBundlePayload(DataClassPayload[5]):
    group_id: str
    round_number: int
    sig1: bytes        # Same number of signatures as submitted in part 1
    sig2: bytes        # In the same order every round.
    sig3: bytes

@dataclass
class RoundResultPayload(DataClassPayload[6]):
    success: bool
    round_number: int
    rounds_completed: int
    message: str

@dataclass
class NonceSigPayload(DataClassPayload[91]):
    """Peer broadcasts nonce + own signature for a round."""
    group_id: str
    round_number: int
    nonce: bytes
    signature: bytes

@dataclass
class ReadyPayload(DataClassPayload[93]):
    """Ready msg, to share we are ready to start the challenges. Only share when the cache is full."""
    group_id: str


@dataclass
class RoundState:
    round_number: int
    nonce: Optional[bytes] = None
    deadline: Optional[float] = None
    sig_cache: Dict[int, bytes] = field(default_factory=dict)
    nonce_event: asyncio.Event = field(default_factory=asyncio.Event)
    sigs_full_event: asyncio.Event = field(default_factory=asyncio.Event)
    advance_event: asyncio.Event = field(default_factory=asyncio.Event)

load_dotenv()
def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        sys.exit(f"ERROR: environment variable {key!r} is not set in .env")
    return value

class Lab2Community(Community, PeerObserver):
    def __init__(self, settings: CommunitySettings) -> None:
        self.community_id = settings.community_id
        super().__init__(settings)
        print(settings.community_id)
        print(settings.group_id)
        print(settings.my_index)
        print(settings.member_keys)
        print(settings.server_pk)
        print(bytes.fromhex(_require_env("SERVER_PUBLIC_KEY_ASS2")))
        self._group_id = settings.group_id
        self._my_index = settings.my_index
        self._member_keys = settings.member_keys
        self._server_pk = settings.server_pk
        self._sk = None

        self._round_states: Dict[int, RoundState] = {
            rn: RoundState(round_number=rn) for rn in (1, 2, 3)
        }
        self._current_round: int = 1
        self._has_won: bool = False

        # Cache the peer objects of our server and the peers, so we don't need to search for them again
        self._server_peer: Optional[Peer] = None
        self._teammate_peers: Dict[int, Peer] = {}  # member index to Peer instance

        self._my_cache_ready: bool = False
        self._teammates_ready: set[int] = set()
        self._all_ready = asyncio.Event()

        self.add_message_handler(ChallengeResponsePayload, self._on_challenge_response)
        self.add_message_handler(RoundResultPayload, self._on_round_result)
        self.add_message_handler(NonceSigPayload, self._on_nonce_sig)
        self.add_message_handler(ReadyPayload, self._on_ready)

        # Make sure we have a private key associated with the public key
        assert self.my_peer.key.has_secret_key()
        self._signing_key: PrivateKey = cast(PrivateKey, self.my_peer.key) 
        logger.info("Lab2Community ready | group=%s | my_index=%d", settings.group_id, settings.my_index)
    
    def on_peer_added(self, peer: Peer) -> None:
        return None

    def on_peer_removed(self, peer: Peer) -> None:
        return None

    def start_rounds(self) -> None:
        asyncio.ensure_future(self.run_all_rounds())

    async def run_all_rounds(self) -> None:
        logger.info("Discovering server + teammates")
        await self._discover_all_peers()

        logger.info("Ready handshake")
        await self._readiness_handshake()

        logger.info("All 3 members ready — starting rounds")
        t0 = time.time()
        for rn in (1, 2, 3):
            await self._do_round(rn)
            self._advance_to(rn + 1)

        logger.info("Rounds done in %.2fs | has_won=%s", time.time() - t0, self._has_won)

    async def _do_round(self, rn: int) -> None:
        state = self._round_states[rn]
        logger.info("[Round %d] start (current=%d, won=%s)", rn, self._current_round, self._has_won)

        # Every node hits the server in parallel; the server returns the same nonce during a live round.
        if state.nonce is None and self._server_peer is not None:
            self.ez_send(self._server_peer, ChallengeRequestPayload(group_id=self._group_id))

        try:
            await asyncio.wait_for(state.nonce_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.error("[Round %d] no nonce received", rn)
            return

        self._sign_and_broadcast(state)

        sigs_task = asyncio.create_task(state.sigs_full_event.wait())
        adv_task = asyncio.create_task(state.advance_event.wait())
        try:
            _, pending = await asyncio.wait(
                {sigs_task, adv_task},
                timeout=3.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        except Exception:
            sigs_task.cancel()
            adv_task.cancel()

        if state.advance_event.is_set():
            logger.info("[Round %d] peer advanced round, skip submit", rn)
            return
        if self._has_won:
            logger.info("[Round %d] already won earlier, skip submit", rn)
            return
        if not state.sigs_full_event.is_set():
            logger.warning("[Round %d] only %d/3 sigs, skipping submit", rn, len(state.sig_cache))
            return

        sigs = [state.sig_cache[i] for i in (0, 1, 2)]
        logger.info("[Round %d] submitting bundle", rn)
        self.ez_send(self._server_peer, SignatureBundlePayload(  # type: ignore
            group_id=self._group_id,
            round_number=rn,
            sig1=sigs[0], sig2=sigs[1], sig3=sigs[2],
        ))

        try:
            await asyncio.wait_for(state.advance_event.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass

    def _sign_and_broadcast(self, state: RoundState) -> None:
        if state.nonce is None or self._my_index in state.sig_cache:
            return
        sig = self.crypto.create_signature(self._signing_key, state.nonce)
        state.sig_cache[self._my_index] = sig
        self._broadcast_nonce_sig(state.round_number, state.nonce, sig)
        self._maybe_set_sigs_full(state)

    def _maybe_set_sigs_full(self, state: RoundState) -> None:
        if len(state.sig_cache) >= 3 and not state.sigs_full_event.is_set():
            state.sigs_full_event.set()

    def _advance_to(self, new_rn: int) -> None:
        if new_rn <= self._current_round:
            return
        for rn in range(self._current_round, new_rn):
            st = self._round_states.get(rn)
            if st is not None and not st.advance_event.is_set():
                st.advance_event.set()
        self._current_round = new_rn

    def _broadcast_nonce_sig(self, rn: int, nonce: bytes, sig: bytes) -> None:
        payload = NonceSigPayload(
            group_id=self._group_id,
            round_number=rn,
            nonce=nonce,
            signature=sig,
        )
        for peer in self._teammate_peers.values():
            self.ez_send(peer, payload)

    @lazy_wrapper(ChallengeResponsePayload)
    def _on_challenge_response(self, peer: Peer, payload: ChallengeResponsePayload) -> None:
        rn = payload.round_number
        state = self._round_states.get(rn)
        logger.info("[Round %d] ChallengeResponse received. Deadline in %.2f s.", rn, payload.deadline - time.time())

        if state is None:
            logger.warning("[Round %d] No state for incoming ChallengeResponse, ignoring. ", rn, payload)
            return

        if rn > self._current_round:
            self._advance_to(rn)

        if state.nonce is None:
            state.nonce = payload.nonce
            state.deadline = payload.deadline
            state.nonce_event.set()
            logger.info("[Round %d] nonce from server (deadline in %.2fs)",
                        rn, payload.deadline - time.time())
            # Relay immediately — peers may not have heard server yet.
            self._sign_and_broadcast(state)

    @lazy_wrapper(NonceSigPayload)
    def _on_nonce_sig(self, peer: Peer, payload: NonceSigPayload) -> None:
        sender_key = peer.public_key.key_to_bin()
        if sender_key not in self._member_keys:
            return
        if payload.group_id != self._group_id:
            return

        rn = payload.round_number
        state = self._round_states.get(rn)
        if state is None:
            return

        if rn > self._current_round:
            self._advance_to(rn)

        if state.nonce is None:
            state.nonce = payload.nonce
            state.nonce_event.set()
            # Sign + rebroadcast so other peer also gets ours fast.
            self._sign_and_broadcast(state)

        sender_idx = self._member_keys.index(sender_key)
        if sender_idx not in state.sig_cache:
            state.sig_cache[sender_idx] = payload.signature
        self._maybe_set_sigs_full(state)

    @lazy_wrapper(RoundResultPayload)
    def _on_round_result(self, peer: Peer, payload: RoundResultPayload) -> None:
        if peer.public_key.key_to_bin() != self._server_pk:
            logger.warning("RoundResult from non-server peer, dropped")
            return

        logger.info("[Round %d] RoundResult: success=%s completed=%d/3 msg=%r",
                    payload.round_number, payload.success,
                    payload.rounds_completed, payload.message)

        if payload.success and payload.round_number >= self._current_round:
            self._has_won = True

        next_rn = payload.rounds_completed + 1
        if next_rn > self._current_round:
            self._advance_to(next_rn)

    @lazy_wrapper(ReadyPayload)
    def _on_ready(self, peer: Peer, payload: ReadyPayload) -> None:
        sender_key = peer.public_key.key_to_bin()
        if sender_key not in self._member_keys:
            logger.warning("ReadyPayload from unregistered peer, ignored.", payload)
            return
        
        if payload.group_id != self._group_id:
            logger.warning("ReadyPayload group id mismatch.", payload)
            return

        self._cache_teammate(peer, sender_key)
        sender_idx = self._member_keys.index(sender_key)
        if sender_idx == self._my_index:
            return

        self._teammates_ready.add(sender_idx)
        logger.info("Teammate %d ready (%d/2)", sender_idx, len(self._teammates_ready))
        self._check_all_ready()

    async def _discover_all_peers(self, timeout: float = 900.0) -> None:
        deadline = time.time() + timeout
        while not self._my_cache_ready:
            if time.time() > deadline:
                raise RuntimeError("Could not discover all peers within %.1f s" % timeout)
            peers = self.get_peers()
            print(len(peers))
            # Search through all peers
            for peer in peers:
                if self._server_peer is None:
                    try:
                        if peer.public_key.key_to_bin() == self._server_pk:
                            self._cache_server(peer)
                            break
                    except Exception:
                        continue

                if len(self._teammate_peers) < 2:
                    try:
                        key = peer.public_key.key_to_bin()
                        if key in self._member_keys and key != self._member_keys[self._my_index]:
                            idx = self._member_keys.index(key)
                            if idx not in self._teammate_peers:
                                self._cache_teammate(peer, key)
                    except Exception:
                        continue

            await asyncio.sleep(5)

    async def _readiness_handshake(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while not self._all_ready.is_set():
            if time.time() > deadline:
                raise RuntimeError("readiness handshake timed out after %.1fs" % timeout)
            self._broadcast_ready()
            try:
                await asyncio.wait_for(asyncio.shield(self._all_ready.wait()), timeout=0.2)
            except asyncio.TimeoutError:
                pass

    def _broadcast_ready(self) -> None:
        payload = ReadyPayload(group_id=self._group_id)
        for peer in self._teammate_peers.values():
            self.ez_send(peer, payload)
        logger.debug("ReadyPayload broadcast to %d teammate(s).", len(self._teammate_peers))

    def _cache_server(self, peer: Peer) -> None:
        if self._server_peer is None:
            self._server_peer = peer
            logger.info("server peer cached: %s", peer.mid.hex())
            self._check_my_cache_ready()

    def _cache_teammate(self, peer: Peer, raw_key: bytes) -> None:
        idx = self._member_keys.index(raw_key)
        if idx not in self._teammate_peers:
            self._teammate_peers[idx] = peer
            logger.info("teammate %d cached: %s", idx, peer.mid.hex())
            self._check_my_cache_ready()

    def _check_my_cache_ready(self) -> None:
        if (not self._my_cache_ready
                and self._server_peer is not None
                and len(self._teammate_peers) == 2):
            self._my_cache_ready = True
            logger.info("local peer cache full — announcing readiness")
            self._check_all_ready()

    def _check_all_ready(self) -> None:
        if (self._my_cache_ready
                and len(self._teammates_ready) == 2
                and not self._all_ready.is_set()):
            self._all_ready.set()
            logger.info("all 3 members ready")
