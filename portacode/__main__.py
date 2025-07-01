from __future__ import annotations

"""Allows ``python -m portacode …`` to behave the same as the *portacode* CLI.

Running::

    python -m portacode [ARGS]

is therefore equivalent to::

    portacode [ARGS]
"""

from .cli import cli

if __name__ == "__main__":  # pragma: no cover
    cli() 