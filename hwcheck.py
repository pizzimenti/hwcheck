#!/usr/bin/env python3
"""hwcheck: an on-screen hardware check for a freshly provisioned linux laptop.

    python3 hwcheck.py --plan P [--resume | --new] [--port N] [--timeout S]

prints the page's url, then waits until every check is answered (or --timeout).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from hwchecklib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
