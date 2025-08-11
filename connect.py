import sys
from portacode.cli import cli

# Build command arguments
args = ['connect', '--non-interactive', '--debug']

# Add log categories if provided
if len(sys.argv) > 1 and sys.argv[1]:
    args.extend(['--log-categories', sys.argv[1]])

cli(args)