#!/bin/bash

# Portacode connection script with optional log category filtering
#
# Usage examples:
#   ./connect.sh                                    # Normal debug mode (all logs)
#   ./connect.sh connection,git                     # Only connection and git logs
#   ./connect.sh project_state                      # Only project state logs (useful for debugging project sync issues)
#   ./connect.sh filesystem,git                     # Only filesystem and git logs
#   ./connect.sh list                               # Show available log categories
#
# Available categories: connection, auth, websocket, terminal, project_state, filesystem, git, handlers, mux, system, debug

# Pass log categories as first argument to connect.py
python connect.py "$1"