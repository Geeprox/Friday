"""Serve only protected bytes while rechecking the original source binding."""

import argparse
import json
import os
import sys

from .archive_worker import open_beneath, root_handle, run_range, signature


def serve(cache, source_root, request, output):
    if set(request) != {"path", "signature", "start", "length", "source_path", "source_signature"}:
        raise ValueError("privacy_invalid_job")
    root = root_handle(source_root)
    original = None
    try:
        original = open_beneath(root, request["source_path"])

        def check():
            current = open_beneath(root, request["source_path"])
            try:
                if (
                    signature(os.fstat(current)) != request["source_signature"]
                    or signature(os.fstat(original)) != request["source_signature"]
                ):
                    raise ValueError("privacy_source_changed")
            finally:
                os.close(current)

        class ProtectedOutput:
            def write(self, body):
                check()
                return output.write(body)

            def flush(self):
                return output.flush()

        check()
        return run_range(
            cache, {k: request[k] for k in ("path", "signature", "start", "length")}, ProtectedOutput()
        )
    finally:
        if original is not None:
            os.close(original)
        os.close(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", required=True)
    parser.add_argument("--source-root", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError("privacy_invalid_job")
        okay = serve(args.cache, args.source_root, json.loads(raw), sys.stdout.buffer)
    except BaseException:
        # After a binary header only EOF is legal; no exception strings enter media.
        okay = False
    raise SystemExit(0 if okay else 2)


if __name__ == "__main__":
    main()
