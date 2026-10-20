"""基于 httpx 的连接池与流传输。read timeout 表示两次接收数据之间的最长等待秒数。"""

from __future__ import annotations

import httpx

from .core import IDLE_MS, AdapterError, Opened, friendly_network_error, idle_message


class HttpxTransport:
    """一个进程复用一个 client：连接池和 TLS 握手都能省下时间。"""

    def __init__(self, *, idle_ms: int = IDLE_MS):
        self.idle_ms = idle_ms
        self._client = httpx.Client(follow_redirects=False)

    def open_stream(self, *, url: str, headers: dict, body: dict, timeout: float) -> Opened:
        # timeout 参数是"多久没数据算卡住"；连接和写入各给 30 秒足够
        limits = httpx.Timeout(connect=30.0, read=max(1.0, float(timeout)), write=30.0, pool=30.0)
        try:
            req = self._client.build_request("POST", url, headers=headers, json=body, timeout=limits)
            resp = self._client.send(req, stream=True)
        except httpx.TimeoutException as e:
            raise AdapterError(504, idle_message({"label": "平台"}, self.idle_ms / 1000)) from e
        except Exception as e:  # noqa: BLE001
            raise AdapterError(502, friendly_network_error(e, {"label": "平台"})) from e

        if not (200 <= resp.status_code < 300):
            try:
                text = resp.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                text = ""
            finally:
                resp.close()
            return Opened(status=resp.status_code, error_text=text)

        def chunks():
            try:
                for piece in resp.iter_bytes():
                    yield piece
            except httpx.TimeoutException as e:
                raise AdapterError(504, idle_message({"label": "平台"}, self.idle_ms / 1000)) from e
            except Exception as e:  # noqa: BLE001
                raise AdapterError(502, friendly_network_error(e, {"label": "平台"})) from e
            finally:
                resp.close()

        return Opened(status=resp.status_code, chunks=chunks())

    def get_json(self, *, url: str, headers: dict, timeout: float) -> tuple[int, str]:
        try:
            resp = self._client.get(url, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            raise AdapterError(504, f"请求 {url} 超时") from e
        except Exception as e:  # noqa: BLE001
            raise AdapterError(502, friendly_network_error(e, {"label": "平台"})) from e
        return resp.status_code, resp.text

    def close(self) -> None:
        self._client.close()
