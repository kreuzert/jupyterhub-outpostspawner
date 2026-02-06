import asyncio
import os
import random
import string

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

def _get_shared_semaphore():
    global _shared_semaphore
    if _shared_semaphore is None:
        concurrent_limit = int(os.environ.get("OUTPOSTSPAWNER_HTTP_CLIENT_CONCURRENT_LIMIT", 10))
        _shared_semaphore = asyncio.Semaphore(concurrent_limit)
    return _shared_semaphore

async def shared_fetch(req, http_client_defaults={}, raise_error=True):
    semaphore = _get_shared_semaphore()
    async with semaphore:
        return await _get_shared_http_client(http_client_defaults).fetch(req, raise_error=raise_error)

def generate_random_id():
    chars = string.ascii_lowercase
    all_chars = string.ascii_lowercase + string.digits

    # Start with a random lowercase letter
    result = random.choice(chars)

    # Add 31 more characters from lowercase letters and numbers
    result += "".join(random.choice(all_chars) for _ in range(31))

    return result