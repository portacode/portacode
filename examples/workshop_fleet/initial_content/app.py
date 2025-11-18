#!/usr/bin/env python3
from pathlib import Path

WELCOME = """Welcome to the Portacode workshop!\n\nEdit this file, run it inside your container, and push your changes once you're ready."""


def main() -> None:
    workspace = Path(__file__).resolve().parent
    notes = workspace / "notes.md"
    print(WELCOME)
    if notes.exists():
        print("\nInstructor notes:")
        print(notes.read_text())
    else:
        print("\nNo notes yet—ask your instructor what to build next!")


if __name__ == "__main__":
    main()
