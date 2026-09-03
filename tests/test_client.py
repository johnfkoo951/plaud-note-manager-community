import httpx
import pytest

import core.auth_status as auth_status_mod
from core.client import PlaudAPIError, PlaudClient
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
