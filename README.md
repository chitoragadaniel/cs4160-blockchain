# Blockchain Engineering

## Assignment 1

### How to run?

#### Proof of Work

To mine the nonce run the command below. Once a nonce is found it is saved to "nonce.txt".
To find the nonce we need to find 28 leading zeros, which means 3 full zero bytes, and the fourth byte should satisfy: 0XF0 (11110000) & byte == 0.
The hash is constructed like: `SHA256( email_utf8 || "\n" || github_url_utf8 || "\n" || nonce_as_8_byte_big_endian )`

```bash
uv run python pow.py --email <my_mail>@student.tudelft.nl --url https://github.com/<my_public_repo>
```

To submit to the server:

```bash
uv run python client.py --email <my_mail>@student.tudelft.nl --url https://github.com/<my_public_repo>
```

#### Client

The client is the one that actually handles all IPv8, and networking stuff. We can front load the CPU work of the mining beforehand.
There are two main classes, `Lab1Community`, and `Lab1Client`. The first one is subclass of the `community` class from the IPv8 library, and kind of owns the protocol. It handles peers joining and leaving the network etc.
The second one, is the orchestrator for our submission, it does the start-up, tear down, timeouts etc.

```bash
    python client.py --email <my_mail>@student.tudelft.nl --url https://github.com/<my_public_repo>
```
