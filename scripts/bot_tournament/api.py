# HTTP client for bot tournament API calls, including login and token refresh.
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import time
from typing import Any

import requests

from .errors import ApiError

logger = logging.getLogger(__name__)


def decode_jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    return exp if isinstance(exp, int) else None


class AuthSession:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.email = email
        self.password = password
        self.token_exp: int | None = None

    def bearer_token(self) -> str:
        if self.token_expiring_soon():
            self.refresh()
        if not self.token:
            self.refresh()
        if not self.token:
            raise ApiError("No bearer token available")
        return self.token

    def token_expiring_soon(self) -> bool:
        if not self.token:
            return True
        if self.token_exp is None:
            return False
        return self.token_exp <= int(time.time()) + 120

    def refresh(self) -> None:
        url = f"{self.base_url}/api/v1/auth/token"
        try:
            response = requests.post(
                url,
                json={"email": self.email, "password": self.password},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Could not call {url}: {exc}") from exc

        if not response.ok:
            raise ApiError(
                f"HTTP {response.status_code} from {url}: {response.text.strip()}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(f"Response from {url} was not JSON") from exc

        if not payload.get("success"):
            message = payload.get("data", {}).get("message", "unknown API error")
            raise ApiError(f"API error from {url}: {message}")

        token = payload.get("data", {}).get("token")
        if not isinstance(token, str):
            raise ApiError(f"API response from {url} did not include data.token")

        self.token = token
        self.token_exp = decode_jwt_exp(token)
        if self.token_exp:
            expires_at = dt.datetime.fromtimestamp(self.token_exp, dt.UTC)
            logger.info("refreshed token; expires at %s", expires_at.isoformat())
        else:
            logger.info("refreshed token")


def is_expired_signature(response: requests.Response, payload: dict[str, Any] | None) -> bool:
    if response.status_code == 401:
        return True
    if not payload:
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("message") == "ExpiredSignature" or data.get("error") == "ExpiredSignature"


def api_request(
    method: str,
    auth: AuthSession,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{auth.base_url}{path}"
    for attempt in range(2):
        try:
            response = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {auth.bearer_token()}"},
                json=body,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Could not call {url}: {exc}") from exc

        payload = None
        try:
            payload = response.json()
        except ValueError:
            pass

        if is_expired_signature(response, payload) and attempt == 0:
            auth.refresh()
            continue

        if not response.ok:
            raise ApiError(
                f"HTTP {response.status_code} from {url}: {response.text.strip()}"
            )

        if payload is None:
            raise ApiError(f"Response from {url} was not JSON")

        if not payload.get("success"):
            data_payload = payload.get("data", {})
            if isinstance(data_payload, dict):
                error = (
                    data_payload.get("error")
                    or data_payload.get("message")
                    or "unknown API error"
                )
            else:
                error = "unknown API error"
            raise ApiError(f"API error from {url}: {error}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ApiError(f"API response from {url} did not include data")

        return data

    raise ApiError(f"Could not refresh token for {url}")


def get_tournament(auth: AuthSession, tournament_id: str) -> dict[str, Any]:
    data = api_request("GET", auth, f"/api/v1/bot/tournament/{tournament_id}")
    tournament = data.get("tournament")
    if not isinstance(tournament, dict):
        raise ApiError("API response did not include data.tournament")
    return tournament


def get_tournament_chat(auth: AuthSession, tournament_id: str) -> list[dict[str, Any]]:
    data = api_request("GET", auth, f"/api/v1/bot/tournament/{tournament_id}/chat")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ApiError("API response did not include data.messages as a list")
    return [message for message in messages if isinstance(message, dict)]


def get_pending_games(auth: AuthSession) -> list[dict[str, Any]]:
    data = api_request("GET", auth, "/api/v1/bot/games/pending")
    games = data.get("games")
    if not isinstance(games, list):
        raise ApiError("API response did not include data.games as a list")
    return [game for game in games if isinstance(game, dict)]


def post_tournament_chat(auth: AuthSession, tournament_id: str, message: str) -> None:
    api_request(
        "POST",
        auth,
        f"/api/v1/bot/tournament/{tournament_id}/chat",
        {"message": message},
    )


def start_game(auth: AuthSession, game_id: str) -> dict[str, Any]:
    return api_request(
        "POST",
        auth,
        "/api/v1/bot/games/control",
        {"game_id": game_id, "control": "start"},
    )


def play_move(auth: AuthSession, game_id: str, move: str) -> dict[str, Any]:
    return api_request(
        "POST",
        auth,
        "/api/v1/bot/games/play",
        {"game_id": game_id, "piece_pos": move},
    )
