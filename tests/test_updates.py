import httpx
import pytest

from kronbot import updates


@pytest.mark.parametrize(
    ("current", "versions", "expected", "prerelease"),
    [
        ("1.9", {"1.10": False}, "1.10", False),
        ("1.0.0", {"1.0": False}, None, False),
        ("1.0", {"2.0rc1": False, "1.1": False}, "1.1", False),
        ("1.0.dev1", {"1.0a1": False}, "1.0a1", True),
        ("1.0a1", {"1.0": False}, "1.0", False),
        ("1.0", {"2.0": True, "1.1+local": False}, None, False),
    ],
)
def test_versions(monkeypatch, current, versions, expected, prerelease):
    monkeypatch.setattr(updates.metadata, "version", lambda name: current)
    client = httpx.Client
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"releases": {v: [{"yanked": y}] for v, y in versions.items()}})
    monkeypatch.setattr(updates.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    result = updates.check_update()
    assert len(requests) == 1 and str(requests[0].url) == "https://pypi.org/pypi/kronbot/json"
    if expected is None:
        assert result is None
    else:
        assert expected in result
        assert "uv tool upgrade" in result and "pipx upgrade" in result
        assert f"https://github.com/citr00n/kronbot/releases/tag/v{expected}" in result
        assert ("--prerelease allow" in result and "--pip-args=--pre" in result) == prerelease


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="bad"), httpx.Response(200, json={"releases": []})])
def test_bad_response_is_silent(monkeypatch, response):
    monkeypatch.setattr(updates.metadata, "version", lambda name: "1.0")
    client = httpx.Client
    monkeypatch.setattr(updates.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(lambda request: response), **kwargs))
    assert updates.check_update() is None


def test_unknown_version_does_not_connect(monkeypatch):
    def unknown(name):
        raise updates.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(updates.metadata, "version", unknown)
    def forbidden(**kwargs):
        pytest.fail("unexpected network")
    monkeypatch.setattr(updates.httpx, "Client", forbidden)
    assert updates.check_update() is None


def test_network_error_is_silent(monkeypatch):
    monkeypatch.setattr(updates.metadata, "version", lambda name: "1.0")
    client = httpx.Client
    def handler(request):
        raise httpx.ConnectError("offline", request=request)
    monkeypatch.setattr(updates.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    assert updates.check_update() is None
