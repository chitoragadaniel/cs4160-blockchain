# CS4160 Blockchain Engineering

Lab work for the TU Delft course **CS4160 Blockchain Engineering**. Every lab is a
peer-to-peer client built on [py-ipv8](https://github.com/Tribler/py-ipv8) that talks to a
course grading server over an IPv8 community. Each lab builds on the previous one: the
IPv8 identity (key pair) registered in Lab 1 is reused in Labs 2 and 3.

The group labs (2 and 3) were done by a team of three. The code was copied from the shared
group repository
[LeaderSupreme/CS4160_blockchain_engineering](https://github.com/LeaderSupreme/CS4160_blockchain_engineering).

## Labs

| Lab | Topic | Type | Docs |
|---|---|---|---|
| [`lab1/`](lab1) | Proof of Work over IPv8: compute a PoW over email + repo URL and submit it to register an identity | Individual | [`lab1/README.md`](lab1/README.md) |
| [`lab2/`](lab2) | Coordinated group signing: three members sign server nonces and submit bundles within a 10 s budget | Group | [`lab2/assignment_2.md`](lab2/assignment_2.md) |
| [`lab3/`](lab3) | Proof-of-Work blockchain: three nodes mine, gossip and converge on one chain that the server verifies | Group | [`lab3/README.md`](lab3/README.md), [`lab3/assignment_3.md`](lab3/assignment_3.md) |

## Repository layout

```
.
├─ pyproject.toml        # shared Python project + dependencies (managed with uv)
├─ uv.lock
│
├─ lab1/                 # Lab 1: Proof of Work over IPv8
│  ├─ README.md          #   assignment spec
│  ├─ main.py            #   submission constants (email, GitHub repo)
│  └─ requirements.txt
│
├─ lab2/                 # Lab 2: Coordinated group signing
│  ├─ assignment_2.md    #   assignment spec
│  ├─ registering.py     #   group registration with the server
│  ├─ phase_2.py         #   signing client entry point
│  ├─ phase_2_community.py  # IPv8 community for the signing rounds
│  └─ temp.py            #   scratch / experimental client
│
└─ lab3/                 # Lab 3: Proof-of-Work blockchain
   ├─ README.md          #   architecture, run instructions, design notes
   ├─ assignment_3.md    #   assignment spec
   ├─ FUNCTION_OVERVIEW.md  # per-function reference
   ├─ client.py          #   entry point
   ├─ config.py          #   keys, community IDs, message IDs, tunables
   ├─ blockchain/        #   chain, PoW, difficulty, mempool, miner, storage
   ├─ network/           #   IPv8 communities and wire payloads
   ├─ test/              #   pytest unit tests
   └─ wal/               #   per-node chain storage
```

## Setup

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

This installs the shared dependencies (`pyipv8`, `msgpack`, `dotenv`, `pytest`) for all labs.

## Keys and secrets

- Every client authenticates with an IPv8 private key (`*.pem`). This key is your course
  identity: Lab 1 registers it, and Labs 2 and 3 are checked against it. Keep it safe and
  never commit it.
- Lab 2 reads the server community ID, server public key and member index from
  `lab2/.env` (`COMMUNITY_ID_ASS2`, `SERVER_PUBLIC_KEY_ASS2`, `MY_INDEX`).

## Running

See each lab's docs for details. In short:

```bash
# Lab 3: start a blockchain node from the repository root (add --register to register with the server)
uv run python -m lab3.client --key_path path/to/key.pem

# Lab 3 tests
uv run pytest lab3/test -v
```
