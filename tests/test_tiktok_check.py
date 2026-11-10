import asyncio
import json
import time

from live_recorder.domain.enums import LiveStatus
from live_recorder.platforms.tiktok import TikTokChecks, check_tiktok, pauses_tiktok_polling


class Response:
    def __init__(self, status_code: int, body: object | None, *, raw: bytes | None = None) -> None:
        self.status_code = status_code
        self._body = body
        if raw is not None:
            self.content = raw
        elif body is None:
            self.content = b""
        else:
            self.content = json.dumps(body).encode()

    def json(self) -> object:
        if self._body is None:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._body


class Web:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[float, float]] = []
        self.params: list[dict] = []

    async def get(self, **kwargs: object) -> Response:
        started = time.monotonic()
        await asyncio.sleep(0.02)
        self.calls.append((started, time.monotonic()))
        self.params.append(dict(kwargs.get("extra_params") or {}))
        return self.responses.pop(0)


def client(web: Web) -> object:
    return type("Client", (), {"web": web})()


def test_offline_and_live_come_from_room_status():
    offline = asyncio.run(
        check_tiktok("user.name", client=client(Web([Response(200, {"data": {"liveRoom": {"status": 4}}})])))
    )
    live = asyncio.run(
        check_tiktok("@user.name", client=client(Web([Response(200, {"data": {"liveRoom": {"status": 2}}})])))
    )
    assert offline.status == LiveStatus.OFFLINE
    assert live.status == LiveStatus.LIVE


def test_empty_403_is_http_status_not_json_error():
    observation = asyncio.run(check_tiktok("user", client=client(Web([Response(403, None, raw=b"")]))))
    assert observation.status == LiveStatus.ERROR
    assert observation.error == "http_403"
    assert pauses_tiktok_polling(observation.error)
    assert pauses_tiktok_polling("http_503")
    assert not pauses_tiktok_polling("user_not_found")
    assert not pauses_tiktok_polling("http_404")


def test_non_json_body_pauses_polling():
    observation = asyncio.run(
        check_tiktok("user", client=client(Web([Response(200, None, raw=b"<html>")])))
    )
    assert observation.error == "invalid_json"
    assert pauses_tiktok_polling(observation.error)


def test_missing_user_does_not_pause_everyone():
    observation = asyncio.run(
        check_tiktok("user", client=client(Web([Response(200, {"message": "user_not_found"})])))
    )
    assert observation.error == "user_not_found"
    assert not pauses_tiktok_polling(observation.error)


def test_checks_run_one_at_a_time_and_a_block_skips_the_rest():
    web = Web(
        [
            Response(403, None, raw=b""),
            Response(200, {"data": {"liveRoom": {"status": 4}}}),
        ]
    )
    checks = TikTokChecks(gap=0.05, block_seconds=10, client_factory=lambda _user: client(web))

    async def run() -> tuple[object, object]:
        return await asyncio.gather(checks.check("first"), checks.check("second"))

    first, second = asyncio.run(run())
    assert first.error == "http_403"
    assert second.error == "http_403"
    assert len(web.calls) == 1
    assert checks.block_remaining() > 9


def test_successful_checks_keep_a_gap():
    class ByUser(Web):
        def __init__(self) -> None:
            super().__init__([])

        async def get(self, **kwargs: object) -> Response:
            started = time.monotonic()
            await asyncio.sleep(0.02)
            self.calls.append((started, time.monotonic()))
            extra = dict(kwargs.get("extra_params") or {})
            self.params.append(extra)
            status = 4 if extra.get("uniqueId") == "aa" else 2
            return Response(200, {"data": {"liveRoom": {"status": status}}})

    web = ByUser()
    checks = TikTokChecks(gap=0.08, block_seconds=10, client_factory=lambda _user: client(web))

    async def run() -> tuple[object, object]:
        return await asyncio.gather(checks.check("aa"), checks.check("bb"))

    offline, live = asyncio.run(run())
    assert offline.status == LiveStatus.OFFLINE
    assert live.status == LiveStatus.LIVE
    assert web.calls[1][0] >= web.calls[0][1] + 0.07
    assert {item["uniqueId"] for item in web.params} == {"aa", "bb"}
    assert web.params[0]["sourceType"] == 54
