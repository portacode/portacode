# Portacode

Portacode is a modular Python package that provides a command-line interface for connecting your local machine to the Portacode cloud gateway.

```
$ pip install portacode
$ portacode connect
```

The first release only ships the `connect` command which:

1. Creates an RSA public/private key-pair (if not already present) in a platform-specific data directory.
2. Guides you through adding the public key to your Portacode account.
3. Establishes and maintains a resilient WebSocket connection to `wss://device.portacode.com/gateway`.

Future releases will add more sub-commands and build upon the multiplexing channel infrastructure already included in this version.

## Project layout

```
portacode/          ‑ Top-level package
├── cli.py          ‑ Click-based CLI entry-point
├── data.py         ‑ Cross-platform data-directory helpers
├── keypair.py      ‑ RSA key generation & storage
├── connection/     ‑ Networking & multiplexing logic
│   ├── client.py   ‑ WebSocket client with auto-reconnect
│   └── multiplex.py- Virtual channel multiplexer
└── …
```

See the README files inside each sub-module for more details. 