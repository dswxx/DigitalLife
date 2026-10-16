"""真的去发 HTTP。**整个后端里唯一没法离线验证的一层，所以故意做得极薄。**

它只干三件事：拼请求、把响应交回去、把异常翻成人话。
"该怎么重试、该怎么解析"全在 `core` 和各自的适配器里——那些都有测试。

静默挂死（连接开着但不再发数据）用 httpx 的 **read timeout** 兜：
它作用在"每次读操作"上，也就是"多久没收到新数据"，正好是我们要的语义。
"""

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
