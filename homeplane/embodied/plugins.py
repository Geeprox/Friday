"""Explicit product extension point. Add reviewed recognizers and bindings here.

No filesystem plugin scan, dynamic source import or HTTP code upload.
"""


def configure(registry, bindings, games, home):
    # Example: register body.bow -> exercise.rep, then bind it to a reviewed
    # game callback. Home callbacks must use `home.command`/the Friday bridge.
    # Existing OK/Yeah, motion and curtain transitions keep their state machines.
    return None
