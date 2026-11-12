"""Keep the reviewed privacy AST while allowing the declared console port.

The only accepted substitution is the exact localhost redirect: its authority
and destination now come from the validated console port. Normalize that one
statement back to its reviewed 8740 form; every other statement and the media
quality callback retain their original fingerprints. Runtime port/origin tests
live in test_console_port_wiring.py and test_homeplane_settings.py.
"""

import ast
import copy
import hashlib

_DECLARED_REDIRECT = ast.parse('''
if (
    request.method == "GET"
    and request.url.path == "/"
    and request.headers.get("host") == localhost_authority
):
    return RedirectResponse(
        console_origin + '/',
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
''').body[0]

_REVIEWED_REDIRECT = ast.parse('''
if (
    request.method == "GET"
    and request.url.path == "/"
    and request.headers.get("host") == "localhost:8740"
):
    return RedirectResponse(
        "http://127.0.0.1:8740/",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
''').body[0]


def _dump(node):
    return ast.dump(node, include_attributes=False).replace(', type_params=[]', '')


def privacy_fingerprint(source, name):
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    if name == 'local_boundary' and _dump(node.body[0]) == _dump(_DECLARED_REDIRECT):
        node = copy.deepcopy(node)
        node.body[0] = _REVIEWED_REDIRECT
    return hashlib.sha256(_dump(node).encode()).hexdigest()
