"""Entry point so `python -m marketengine <command>` works."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
