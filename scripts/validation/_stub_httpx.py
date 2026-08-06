"""Minimal stand-in for the `httpx` package — just enough surface for
`whale_alpha.utils.http_retry` to import and run for real. Not a mock of
behavior under test: `AsyncClient`/`Response` here are structural stand-ins
constructed and driven entirely by the validation script, while every line
of retry/backoff/circuit-breaker/cache logic executed is the actual,
unmodified `whale_alpha/utils/http_retry.py` shipped in this patch.
"""
import types, sys

httpx = types.ModuleType("httpx")

class AsyncClient:
    pass

class Response:
    def __init__(self, status_code, headers=None, json_data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        return self._json


class TimeoutException(Exception):
    pass


class TransportError(Exception):
    pass


httpx.AsyncClient = AsyncClient
httpx.Response = Response
httpx.TimeoutException = TimeoutException
httpx.TransportError = TransportError
sys.modules["httpx"] = httpx
