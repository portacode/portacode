"""Shared process exit codes used by the Portacode runtime and service managers."""

# Process exited because the gateway rejected device authentication (for example,
# an unknown/deleted device key). Service supervisors should not auto-restart on
# this code to avoid endless auth failure loops.
AUTH_REJECTED_EXIT_CODE = 86

