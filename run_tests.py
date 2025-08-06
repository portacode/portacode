#!/usr/bin/env python3
"""
Convenience script to run the testing framework.
This provides an alternative to using the CLI module directly.
"""

import sys
import os

# Add the current directory to Python path so we can import the testing framework
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    from testing_framework.cli import cli
    cli()