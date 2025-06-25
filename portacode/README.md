# Portacode Python package

This directory contains the implementation of the `portacode` Python package.  
Most users will interact with the package via the `portacode` command-line tool.

## Important modules

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Implements the Click-based command-line interface. |
| `data.py` | Determines and manages the cross-platform user-data directory. |
| `keypair.py` | Generates, stores and fingerprints the RSA key-pair. |
| `connection.client` | Maintains a resilient WebSocket connection to the Portacode gateway. |
| `connection.multiplex` | A tiny multiplexer that lets you open unlimited virtual channels over the single WebSocket connection. |

Each sub-package contains its own README for easier discovery. 