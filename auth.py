"""OAuth2-авторизация в Скорозвон + персистентное хранение токена."""
import json
import logging
import os
import time
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

TOKEN_LIFETIME_SECONDS = 2 * 60 * 60  # 2 часа
REFRESH_BEFORE_SECONDS = 5 * 60        # обновлять заранее


class SkorozvonAuth:
    """Поддерживает живой access_token. Сам обновляет его при необходимости."""

    def __init__(self) -> None:
        self._token: Optional[dict] = self._load_token()

    # ---------- публичное API ----------

    def get_access_token(self) -> str:
        if not self._token or self._is_expiring():
            if self._token and self._token.get("refresh_token"):
                try:
                    self._refresh()
                except Exception as exc:
                    log.warning("Refresh не удался (%s), логинимся заново", exc)
                    self._login()
            else:
                self._login()
        return self._token["access_token"]

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_access_token()}"}

    # ---------- внутреннее ----------

    def _is_expiring(self) -> bool:
        if not self._token:
            return True
        expires_at = self._token.get("expires_at", 0)
        return time.time() >= expires_at - REFRESH_BEFORE_SECONDS

    def _login(self) -> None:
        log.info("Скорозвон: логин по password grant")
        resp = requests.post(
            f"{config.SKOROZVON_BASE_URL}/oauth/token",
            files={
                "grant_type": (None, "password"),
                "username": (None, config.SKOROZVON_USERNAME),
                "api_key": (None, config.SKOROZVON_API_KEY),
                "client_id": (None, config.SKOROZVON_CLIENT_ID),
                "client_secret": (None, config.SKOROZVON_CLIENT_SECRET),
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._store_token(resp.json())

    def _refresh(self) -> None:
        log.info("Скорозвон: refresh token")
        resp = requests.post(
            f"{config.SKOROZVON_BASE_URL}/oauth/token",
            files={
                "grant_type": (None, "refresh_token"),
                "refresh_token": (None, self._token["refresh_token"]),
                "client_id": (None, config.SKOROZVON_CLIENT_ID),
                "client_secret": (None, config.SKOROZVON_CLIENT_SECRET),
            },
            headers={"Authorization": f"Bearer {self._token['access_token']}"},
            timeout=30,
        )
        resp.raise_for_status()
        self._store_token(resp.json())

    def _store_token(self, payload: dict) -> None:
        if "access_token" not in payload:
            raise ValueError(f"Ответ не содержит access_token: {list(payload.keys())}")
        payload["expires_at"] = time.time() + TOKEN_LIFETIME_SECONDS
        self._token = payload
        try:
            with open(config.TOKEN_STORE_PATH, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError as exc:
            log.warning("Не удалось сохранить токен: %s", exc)

    def _load_token(self) -> Optional[dict]:
        if not os.path.exists(config.TOKEN_STORE_PATH):
            return None
        try:
            with open(config.TOKEN_STORE_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
