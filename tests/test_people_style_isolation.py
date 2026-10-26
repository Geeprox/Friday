"""People selector containment and actual opt-in console route regressions.

No browser, screenshots, style-engine substitute, clustering, or native inference.
Selector containment proves People rules cannot match the outside console DOM;
the Node fixture exercises actual navigation/style/view lifetime source.
"""

import importlib.abc
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_homeplane_recognition_consumer import settings

from homeplane.api import create_app
from homeplane.people.catalog import PeopleCatalog
from homeplane.people.demo import create_demo

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "homeplane/people/static/people.css"


class NativeFence(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"cv2", "sherpa_onnx", "miot", "sklearn", "torch", "onnxruntime"}:
            raise AssertionError("native_or_clustering_runtime_forbidden")


@pytest.fixture(autouse=True)
def fence():
    guard = NativeFence()
    sys.meta_path.insert(0, guard)
    yield
    sys.meta_path.remove(guard)


def rules(source):
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    position = 0
    while position < len(source):
        opening = source.find("{", position)
        if opening == -1:
            assert not source[position:].strip()
            return
        header = source[position:opening].strip()
        depth, closing = 1, opening + 1
        while depth:
            assert closing < len(source)
            depth += (source[closing] == "{") - (source[closing] == "}")
            closing += 1
        yield header, source[opening + 1 : closing - 1]
        position = closing


def selectors(header):
    # Commas inside :where()/:has() do not create a new selector.
    return re.split(r",(?![^()]*\))", header)


def assert_contained(source):
    total = 0
    for header, body in rules(source):
        if header.startswith("@media"):
            total += assert_contained(body)
        elif header.startswith("@keyframes"):
            assert header.split()[1].startswith("people-")
            assert all(name in {"from", "to"} for name, _ in rules(body))
        else:
            assert not header.startswith("@"), header
            for selector in selectors(header):
                selector = selector.strip()
                assert re.match(
                    r"^(?:\.people-root(?=[\s.:#>\[]|$)|\.people-demo(?=[\s.:#>\[]|$)|:where\(\.people-root,\.people-demo\))",
                    selector,
                ), selector
                # An owned ancestor cannot reach siblings outside its surface.
                assert not re.search(r"[+~]", selector), selector
            total += 1
    return total


def test_every_css_rule_and_nested_motion_rule_is_people_contained():
    source = CSS.read_text()
    assert assert_contained(source) > 100
    assert ":root" not in source
    assert all(name.startswith("--people-") for name in re.findall(r"--[\w-]+", source))
    assert ".people-demo{margin:0;" in source  # standalone body styling remains
    assert "people-row-enter" in source and "people-inspect-enter" in source


@pytest.mark.parametrize(
    "leak",
    [
        ":root{--people-ink:red}",
        "button{font-size:1px}",
        "@media(prefers-reduced-motion:reduce){*{animation:none!important}}",
        ".people-root ~ .brand{color:red}",
    ],
)
def test_containment_regression_rejects_original_leaks(leak):
    with pytest.raises(AssertionError):
        assert_contained(CSS.read_text() + leak)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
async def test_actual_console_capability_and_asset_mount_require_opt_in(tmp_path, available):
    config = settings(tmp_path)
    connection = sqlite3.connect(":memory:", check_same_thread=False) if available else None
    people = PeopleCatalog(connection) if available else None
    app = create_app(
        db_path=tmp_path / "synthetic-traces.sqlite", settings_service=config, people_catalog=people
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert (await client.head("/")).status_code == 200
            assert ('data-people-enabled="true"' in response.text) is available
            assert (await client.get("/index.html")).text == response.text
            assert "/people-static/people.css" not in response.text
            assert "/static/style.css" in response.text and "/static/icons.css" in response.text
            if not available:
                assert response.text == (ROOT / "homeplane/static/index.html").read_text()
            assets = await client.get("/people-static/people.css")
            assert assets.status_code == (200 if available else 404)
            page = await client.get("/api/people")
            assert page.status_code == (200 if available else 404)
            status = await client.get("/api/settings/camera/recognition")
            assert status.json()["state"] == "disabled"
            assert response.headers["cache-control"] == "no-store"
            if available:
                assert "people_catalog" not in (await client.get("/api/settings")).text


def test_standalone_fixture_still_has_its_scoped_surface():
    import asyncio

    async def read():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_demo()), base_url="http://testserver"
        ) as client:
            response = await client.get("/")
            assert '<body class="people-demo">' in response.text
            assert 'class="people-root"' in response.text
            assert "/people-static/people.css" in response.text
            assert (await client.get("/people-static/people.css")).text == CSS.read_text()

    asyncio.run(read())


def test_actual_javascript_navigation_style_and_destroy_lifetime():
    completed = subprocess.run(
        ["node", str(ROOT / "tests/people_style_isolation_fixture.cjs"), str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    evidence = json.loads(completed.stdout)
    assert evidence == {
        "absent_people_nav": True,
        "enabled_people_nav": True,
        "outside_styles_before_open": 0,
        "scoped_style_while_open": True,
        "outside_styles_after_leave": 0,
        "root_marker_removed": True,
        "late_refresh_ignored": True,
        "embedded_heading_omitted": True,
    }
