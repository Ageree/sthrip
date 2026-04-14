import sys
from typing import Any, Dict, Optional

import httpx

from sthrip.cli.output import (
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_NETWORK_ERROR,
)


class CliError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_API_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


class StrhipClient:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        timeout: int,
        debug: bool,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._debug = debug
        self._http = httpx.Client(timeout=timeout)

    def _headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = self._headers(idempotency_key)

        if self._debug:
            print(f"DEBUG {method} {url}", file=sys.stderr)
            if json:
                print(f"DEBUG body: {json}", file=sys.stderr)

        try:
            resp = self._http.request(
                method, url, json=json, params=params, headers=headers,
            )
        except httpx.TimeoutException as e:
            raise CliError(f"Request timed out: {e}", EXIT_NETWORK_ERROR)
        except httpx.ConnectError as e:
            raise CliError(f"Connection failed: {e}", EXIT_NETWORK_ERROR)
        except httpx.HTTPError as e:
            raise CliError(f"Network error: {e}", EXIT_NETWORK_ERROR)

        if self._debug:
            print(f"DEBUG status: {resp.status_code}", file=sys.stderr)

        if resp.status_code in (401, 403):
            detail = self._extract_detail(resp)
            raise CliError(detail, EXIT_AUTH_ERROR)

        if resp.status_code >= 400:
            detail = self._extract_detail(resp)
            raise CliError(detail, EXIT_API_ERROR)

        return resp.json()

    @staticmethod
    def _extract_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            return body.get("detail", body.get("error", resp.text))
        except (ValueError, KeyError):
            return resp.text

    def get(
        self, path: str, params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST", path, json=json, idempotency_key=idempotency_key,
        )

    def patch(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._request("PATCH", path, json=json)
