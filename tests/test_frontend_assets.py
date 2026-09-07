"""Browser branding must resolve through the production SPA on nested routes."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import pytest


class _Icons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and "icon" in attributes.get("rel", "").split():
            self.hrefs.append(attributes.get("href", ""))


@pytest.mark.parametrize("route", ["/", "/recordings/1", "/journeys/1", "/obd/preview"])
async def test_nested_shell_branding_resolves_to_an_actual_icon(client, route):
    from app.main import FRONTEND_DIST

    if not FRONTEND_DIST.is_dir():
        pytest.skip("production frontend build is absent")
    shell = await client.get(route)
    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "no-cache"
    assert "<title>Dashcam Analyser</title>" in shell.text
    icons = _Icons()
    icons.feed(shell.text)
    assert icons.hrefs
    for href in icons.hrefs:
        resolved = urlsplit(urljoin(f"http://test{route}", href))
        assert resolved.netloc == "test"
        icon = await client.get(resolved.path)
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in icon.text
        assert "<html" not in icon.text
