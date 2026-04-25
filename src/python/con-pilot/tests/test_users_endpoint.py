"""Tests for admin install-key verification endpoint logic."""

import pytest
from fastapi import HTTPException

from con_pilot.users.router import (
    ShowMeResponse,
    VerifyKeyRequest,
    VerifyKeyResponse,
    show_me_endpoint,
    verify_key_endpoint,
)


def test_verify_key_endpoint_accepts_matching_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "con_pilot.users.router._read_install_key",
        lambda: b"expected-key",
    )

    result = verify_key_endpoint(VerifyKeyRequest(key="expected-key"))

    assert result == VerifyKeyResponse(valid=True)


def test_verify_key_endpoint_rejects_non_matching_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "con_pilot.users.router._read_install_key",
        lambda: b"expected-key",
    )

    with pytest.raises(HTTPException) as exc_info:
        verify_key_endpoint(VerifyKeyRequest(key="wrong-key"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid install key"


def test_show_me_endpoint_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CON_PILOT_DEV_SHOW_ME", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        show_me_endpoint()

    assert exc_info.value.status_code == 404


def test_show_me_endpoint_returns_key_and_appdir_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    (appdir / "key").write_text("dev-key", encoding="utf-8")
    (appdir / "usr").mkdir()
    (appdir / "usr" / "bin").mkdir()
    (appdir / "usr" / "bin" / "con-pilot").write_text("binary", encoding="utf-8")

    monkeypatch.setenv("CON_PILOT_DEV_SHOW_ME", "1")
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))

    result = show_me_endpoint()

    assert result == ShowMeResponse(
        enabled=True,
        conductor_home=str(tmp_path / "home"),
        appdir=str(appdir),
        key_file=str(appdir / "key"),
        key_value="dev-key",
        appdir_entries=["key", "usr/", "usr/bin/", "usr/bin/con-pilot"],
    )
