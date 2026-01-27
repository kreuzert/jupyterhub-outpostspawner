_shared_http_client = None


def get_shared_http_client(http_client_defaults={}):
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
