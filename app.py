#!/usr/bin/env python3
"""Entry point — pm2 runs this. Keeps the package importable from the repo root."""

from openai_relay.server import main

if __name__ == "__main__":
    main()
