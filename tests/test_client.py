import httpx
import pytest

import core.auth_status as auth_status_mod
from core.client import PlaudAPIError, PlaudClient, TempAudioSource
from core.config import PlaudConfig


def test_client_omits_legacy_user_header_when_not_captured() -> None:
    cfg = PlaudConfig(authorization="Bearer test", x_device_id="device")

    assert "x-pld-user" not in cfg.headers()


def test_client_wraps_network_errors_without_raw_httpx_traceback() -> None:
    cfg = PlaudConfig(
        authorization="Bearer test",
        x_device_id="device",
        x_pld_tag="tag",
        x_pld_user="user",
    )
    client = PlaudClient(cfg)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    client._client = httpx.Client(
        base_url=cfg.base_url,
        transport=httpx.MockTransport(boom),
    )

    with pytest.raises(PlaudAPIError) as excinfo:
        client.list_folders()

    msg = str(excinfo.value)
    assert msg.startswith("Plaud network error for GET api-apne1.plaud.ai/filetag/")
    assert "network down" in msg
    assert "Check your internet connection or Plaud session" in msg


def test_one_shot_folder_patch_does_not_retry_transport_failure() -> None:
    cfg = PlaudConfig(authorization="Bearer test", x_device_id="device")
    client = PlaudClient(cfg)
    requests: list[httpx.Request] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("late response", request=request)

    client._client.close()
    client._client = httpx.Client(
        base_url=cfg.base_url,
        headers=cfg.headers(),
        transport=httpx.MockTransport(timeout),
    )

    with pytest.raises(PlaudAPIError) as excinfo:
        client.set_file_folders_once("f1", ["folder"])

    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/file/f1"
    assert excinfo.value.status_code is None
    assert excinfo.value.api_status is None


def test_one_shot_folder_patch_does_not_retry_gateway_timeout() -> None:
    cfg = PlaudConfig(authorization="Bearer test", x_device_id="device")
    client = PlaudClient(cfg)
    requests: list[httpx.Request] = []

    def gateway_timeout(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(504, request=request, json={"status": 504})

    client._client.close()
    client._client = httpx.Client(
        base_url=cfg.base_url,
        headers=cfg.headers(),
        transport=httpx.MockTransport(gateway_timeout),
    )

    with pytest.raises(PlaudAPIError) as excinfo:
        client.set_file_folders_once("f1", [])

    assert len(requests) == 1
    assert excinfo.value.status_code == 504


def test_client_classifies_http_200_business_status_minus_419_as_auth_rejection() -> None:
    cfg = PlaudConfig(authorization="Bearer expired", x_device_id="device")
    client = PlaudClient(cfg)

    def expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"status": -419, "msg": "expired"})

    client._client = httpx.Client(
        base_url=cfg.base_url,
        headers=cfg.headers(),
        transport=httpx.MockTransport(expired),
    )

    with pytest.raises(PlaudAPIError) as excinfo:
        client.list_files(limit=1)

    assert excinfo.value.api_status == -419
    assert excinfo.value.is_auth_rejection is True
    assert auth_status_mod.auth_rejected_at() is not None


def test_temp_audio_source_prefers_mp3_and_temp_url_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PlaudClient(PlaudConfig(authorization="Bearer test", x_device_id="device"))
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda _path: {
            "temp_url": "https://audio.example/recording.mp3",
            "temp_url_opus": "https://audio.example/recording.opus",
        },
    )

    source = client.temp_audio_source("f1")

    assert source == TempAudioSource(
        url="https://audio.example/recording.mp3",
        filename="recording.mp3",
        content_type="audio/mpeg",
    )
    assert client.temp_url("f1") == source.url


def test_temp_audio_source_labels_opus_fallback_truthfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PlaudClient(PlaudConfig(authorization="Bearer test", x_device_id="device"))
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda _path: {"temp_url_opus": "https://audio.example/recording.opus"},
    )

    assert client.temp_audio_source("f1") == TempAudioSource(
        url="https://audio.example/recording.opus",
        filename="recording.opus",
        content_type="audio/ogg",
    )
