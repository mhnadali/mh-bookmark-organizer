import sys

from .cli import main

if __name__ == "__main__":  # worker processes re-import this module; they must not run the command line again
    sys.exit(main())
