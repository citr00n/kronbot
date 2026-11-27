from datetime import date

import httpx
import pytest

from kronbot.sport import SportClient, SportError
from kronbot.state import StateStore


async def test_sport_types_and_filtered_schedule_use_api(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/filters'):
            return httpx.Response(200, json={'error_code': 0, 'result': {'sport_type_id': [{'id': 2, 'value': 'Плавание'}]}})
        return httpx.Response(200, json={'error_code': 0, 'result': [{'lessons': [{'id': 123, 'date': '2030-03-18T12:00:00+03:00', 'section_name': 'Тренировка', 'section_level': 1, 'type_id': 2, 'available': 1, 'signed': False, 'can_sign_in': {'can_sign_in': True}}]}]})
    with StateStore(tmp_path / 'state') as store:
        store.data['session'] = {'sub': 'owner', 'access_token': 'token', 'access_expires_at': 1900001000, 'refresh_token': None, 'refresh_expires_at': None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: 1900000000)
        assert await client.sport_types() == [(2, 'Плавание')]
        result = await client.schedule(date(2030, 3, 17), date(2030, 3, 30), sport_type_id=2)
        assert [lesson.id for lesson in result] == [123]
        assert calls[1].url.params['sport_type_id'] == '2'
        assert len(store.data['sport_request_times']) == 2
        await client.close()


async def test_malformed_sport_type_is_not_used(tmp_path):
    with StateStore(tmp_path / 'state') as store:
        store.data['session'] = {'sub': 'owner', 'access_token': 'token', 'access_expires_at': 1900001000, 'refresh_token': None, 'refresh_expires_at': None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'error_code': 0, 'result': {'sport_type_id': [{'id': True, 'value': 'Плавание'}]}})), now=lambda: 1900000000)
        with pytest.raises(SportError):
            await client.sport_types()
        await client.close()
