import asyncio
import json
import os

from forwardbasespawner.utils import check_custom_scopes
from jupyterhub.apihandlers import APIHandler
from jupyterhub.apihandlers import default_handlers
from jupyterhub.utils import token_authenticated
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPRequest

_outpost_flavors_cache = {}


class OutpostFlavorsAPIHandler(APIHandler):
    required_scopes = ["custom:outpostflavors:set"]

    def check_xsrf_cookie(self):
        pass

    @token_authenticated
    async def post(self, outpost_name):
        check_custom_scopes(self)
        global _outpost_flavors_cache

        body = self.request.body.decode("utf8")
        try:
            flavors = json.loads(body) if body else {}
        except:
            self.set_status(400)
            self.log.exception(
                f"{outpost_name} - Could not load body into json. Body: {body}"
            )
            return

        _outpost_flavors_cache[outpost_name] = flavors
        self.set_status(200)

    async def get(self):
        global _outpost_flavors_cache

        try:
            initial_system_names = os.environ.get(
                "OUTPOST_FLAVOR_INITIAL_SYSTEM_NAMES", ""
            )
            initial_system_urls = os.environ.get(
                "OUTPOST_FLAVOR_INITIAL_SYSTEM_URLS", ""
            )
            initial_system_tokens = os.environ.get(
                "OUTPOST_FLAVOR_INITIAL_SYSTEM_TOKENS", ""
            )

            # If initial checks are configured
            if initial_system_names and initial_system_urls:
                initial_system_names_list_all = initial_system_names.split(";")
                initial_system_urls_list_all = initial_system_urls.split(";")
                initial_system_tokens_list_all = initial_system_tokens.split(";")

                initial_system_names_list = []
                initial_system_urls_list = []
                initial_system_tokens_list = []
                i = 0
                # Only check for initial checks, when they're not yet part of _outpost_flavors_cache
                for system_name in initial_system_names_list_all:
                    if system_name not in _outpost_flavors_cache.keys():
                        initial_system_names_list.append(system_name)
                        initial_system_urls_list.append(initial_system_urls_list_all[i])
                        initial_system_tokens_list.append(
                            initial_system_tokens_list_all[i]
                        )
                    i += 1

                # If systems are left without successful initial check, try to reach the Outpost
                if initial_system_names_list:
                    self.log.info(
                        f"OutpostFlavors - Connect to {initial_system_names_list} / {initial_system_urls_list}"
                    )

                    urls_tokens = list(
                        zip(initial_system_urls_list, initial_system_tokens_list)
                    )
                    http_client = AsyncHTTPClient(
                        force_instance=True, defaults=dict(validate_cert=False)
                    )
                    tasks = []
                    for url_token in urls_tokens:
                        req = HTTPRequest(
                            url_token[0],
                            headers={"Authorization": f"Basic {url_token[1]}"},
                        )
                        tasks.append(http_client.fetch(req, raise_error=False))
                    results = await asyncio.gather(*tasks)
                    names_results = list(zip(initial_system_names_list, results))
                    for name_result in names_results:
                        if name_result[1].code == 200:
                            try:
                                self.log.info(
                                    f"OutpostFlavors - {name_result[0]} successful"
                                )
                                result_json = json.loads(name_result[1].body)
                                _outpost_flavors_cache[name_result[0]] = result_json
                            except:
                                self.log.exception(
                                    f"OutpostFlavors - {name_result[0]} Could not load result into json"
                                )
                        else:
                            self.log.warning(
                                f"OutpostFlavors - {name_result[0]} - Answered with {name_result[1].code}"
                            )
        except:
            self.log.exception("OutpostFlavors failed, return empty dict")

        self.write(json.dumps(_outpost_flavors_cache))
        self.set_status(200)
        return


default_handlers.append((r"/api/outpostflavors/([^/]+)", OutpostFlavorsAPIHandler))
default_handlers.append((r"/api/outpostflavors", OutpostFlavorsAPIHandler))
