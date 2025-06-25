# portacode.connection

Networking primitives used by the Portacode CLI.

* **`client.py`** ― High-level `ConnectionManager` that establishes and
  maintains a persistent WebSocket connection to the Portacode gateway.
  It automatically reconnects after transient failures and integrates with
  an event-loop signal handler so it shuts down cleanly on Ctrl-C.

* **`multiplex.py`** ― A minimal JSON-frame based multiplexer/demultiplexer.
  It lets client-side code open an arbitrary number of virtual, full-duplex
  channels over the single WebSocket connection. Each frame clearly states
  the *channel id* and *payload*. Future commands will rely on this fabric to
  implement rich interactions on top of the same socket.

Both modules purposefully avoid any GUI or terminal logic – that lives in
`portacode.cli` – to keep concerns separated and the codebase easy to test. 