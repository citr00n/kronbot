import httpx
import pytest

from kronbot.auth import Restricted
from kronbot.sport import SportClient
from kronbot.state import StateStore


async def test_isu_lookup_saves_only_confirmed_number(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": {"isu": 123456, "programs": []}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "opaque-id", "access_token": "token", "access_expires_at": 1900001000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: 1900000000)
        assert await client.account_isu() == "123456"
        assert store.data["session"]["isu"] == "123456"
        assert store.data["sport_request_times"] == []
        assert len(requests) == 1 and requests[0].url.path == "/api/eduPlanNew/programs"
        await client.close()
    with StateStore(tmp_path / "state") as store:
        assert store.data["session"]["isu"] == "123456"


async def test_isu_lookup_persists_restriction(tmp_path):
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "token", "access_expires_at": 1900001000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "300"})), now=lambda: 1900000000)
        with pytest.raises(Restricted):
            await client.account_isu()
        assert store.data["blocked_until"]["my.itmo.ru"] == 1900000300
        await client.close()
