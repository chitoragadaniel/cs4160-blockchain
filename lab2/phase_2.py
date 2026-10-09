import os
import sys
import asyncio
import logging

from typing import List
from pathlib import Path
from dotenv import load_dotenv

from phase_2_community import Lab2Community

from ipv8.configuration import ConfigBuilder, Strategy, WalkerDefinition, BootstrapperDefinition, Bootstrapper, default_bootstrap_defs
from ipv8_service import IPv8
from ipv8.util import run_forever


KEY_FILE  = Path("assignment_1", "key.pem")
GROUP_ID  = "5c0303d6e952c77d"
 
MEMBER_KEYS: List[bytes] = [  # Faizel, Daniel, Ruben
    b'LibNaCLPK:*Q\xc3\xf7\xaa\x87]#NS\x0cL\xa8\xba\xe4pb\xaf\x82\xdd\x1bE\xb2&\xf8\xfc\x81e\xce\xbc\x91\n8\xfcJ\x92\xd5\xccq\xc0\xdf\xd8\x85\xebr\xa1\x06,ve\xe9yQN\xeewe\x9b\x84\xaeLd\xf6V', 
    b'LibNaCLPK:\xee\xe1\xefN\xf0\xb0&\xf4\xb7]#\x10\x9e\x16\x87\xbb\x86%%\xdeG"\xd2\x86\xb2\xb5\xf7:\x04\xee\x078n\xd8\xf8)\xbd\xbb8\x13;\xb5\xd0D\xa0\x95\x94\xb97\xdd>&\x8a\rf\x8f >\xdf\xd4.c\x0c\xa6', 
    b'LibNaCLPK:\x08<\xceB\x06UC\x07\x99\xb7O\xb4\x1c\x1c\xa5\xa2$\xe6,T[\xd0\x1c9\x93u\xfa0\xfb/\xadw\x03\xf9\x07\x98\x9c\x04\x9be\x04,\xa5\xd1\xb5Dqi\x11a\xb6)\xcbyr\xae\xcd,\xc8?\xe0J4\xbd'
]

load_dotenv()
def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        sys.exit(f"ERROR: environment variable {key!r} is not set in .env")
    return value

COMMUNITY_ID      = bytes.fromhex(_require_env("COMMUNITY_ID_ASS2"))
SERVER_PUBLIC_KEY = bytes.fromhex(_require_env("SERVER_PUBLIC_KEY_ASS2"))
MY_INDEX = int(_require_env("MY_INDEX"))
 
async def _run() -> None:
    config = (
        ConfigBuilder()
        .clear_keys()
        .add_key("my_peer", "curve25519", str(KEY_FILE))
        .clear_overlays()
        .add_overlay(
            "Lab2Community",
            "my_peer",
            [WalkerDefinition(Strategy.RandomWalk, 20, {"timeout": 5.0})],
            default_bootstrap_defs,
            {
                "community_id": COMMUNITY_ID,
                "group_id": GROUP_ID,
                "member_keys": MEMBER_KEYS,
                "my_index": MY_INDEX,
                "server_pk": SERVER_PUBLIC_KEY
            }, 
            [("start_rounds",)],
            -1
        )
        .finalize()
    )
 
    ipv8 = IPv8(config, extra_communities={"Lab2Community": Lab2Community})
    await ipv8.start()
    await run_forever() 
 
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_run())
 
