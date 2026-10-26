"""Retired direct-model entrypoint; runtime execution belongs to ARL."""
import sys


def main():
    print("Direct model execution retired. Use .venv/bin/python -m scripts.check_arl; "
          "task benchmark requires an advertised ARL contract.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
