"""`python -m lr` == the CLI entry point (see lr.cli)."""
import sys

from .cli import main

if __name__ == '__main__':
    sys.exit(main())
