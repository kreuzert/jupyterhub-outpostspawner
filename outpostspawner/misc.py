import asyncio

_shared_http_client = None
_shared_semaphore = None


def _get_shared_http_client(http_client_defaults={}):
    global _shared_http_client
    if _shared_http_client is None:
        try:
            from tornado.curl_httpclient import CurlAsyncHTTPClient

            _shared_http_client = CurlAsyncHTTPClient(defaults=http_client_defaults)
        except ImportError:
            from tornado.httpclient import AsyncHTTPClient

            _shared_http_client = AsyncHTTPClient(
                force_instance=True, defaults=http_client_defaults
            )
    return _shared_http_client

def _get_shared_semaphore(concurrent_limit):
    global _shared_semaphore
    if _shared_semaphore is None:
        _shared_semaphore = asyncio.Semaphore(concurrent_limit)
    return _shared_semaphore

async def shared_fetch(req, concurrent_limit, http_client_defaults={}):
    semaphore = _get_shared_semaphore(concurrent_limit)
    async with semaphore:
        return await _get_shared_http_client(http_client_defaults).fetch(req)