import asyncio
import copy
import json
import os

from forwardbasespawner.utils import check_custom_scopes
from jupyterhub.apihandlers import APIHandler
from jupyterhub.apihandlers import default_handlers
from jupyterhub.utils import token_authenticated
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPRequest

from .misc import Thread

_outpost_flavors_cache = {}


async def async_get_flavors(log, user=None):
    global _outpost_flavors_cache

    try:
        initial_system_names = os.environ.get("OUTPOST_FLAVOR_INITIAL_SYSTEM_NAMES", "")
        initial_system_urls = os.environ.get("OUTPOST_FLAVOR_INITIAL_SYSTEM_URLS", "")
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
                    initial_system_tokens_list.append(initial_system_tokens_list_all[i])
                i += 1

            # If systems are left without successful initial check, try to reach the Outpost
            if initial_system_names_list:
                log.info(
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
                            log.info(f"OutpostFlavors - {name_result[0]} successful")
                            result_json = json.loads(name_result[1].body)
                            _outpost_flavors_cache[name_result[0]] = result_json
                        except:
                            log.exception(
                                f"OutpostFlavors - {name_result[0]} Could not load result into json"
                            )
                    else:
                        log.warning(
                            f"OutpostFlavors - {name_result[0]} - Answered with {name_result[1].code}"
                        )
    except:
        log.exception("OutpostFlavors failed, return empty dict")

    # If it's an user authenticated request, we override the available flavors, if
    # there's a dict with available flavors in auth_state.
    # One can add this in Authenticator.post_auth_hook, to allow user-specific
    # flavors for each Outpost.
    if user:
        auth_state = await user.get_auth_state()
        if auth_state:
            user_specific_flavors = auth_state.get("outpost_flavors", {})
            if user_specific_flavors:
                # Do not override global default flavors cache
                user_specific_ret = copy.deepcopy(_outpost_flavors_cache)
                for system_name, system_flavors in user_specific_flavors.items():
                    if type(system_flavors) == bool and not system_flavors:
                        # System is not allowed for this user
                        if system_name in user_specific_ret.keys():
                            del user_specific_ret[system_name]
                    elif type(system_flavors) == dict:
                        # Replace the default flavor dict with the user specific one
                        # but keep the "current" value
                        user_specific_ret[system_name] = system_flavors
                        for key, value in system_flavors.items():
                            specific_current = value.get("current", 0)
                            user_specific_ret[system_name][key]["current"] = (
                                _outpost_flavors_cache.get(system_name, {})
                                .get(key, {})
                                .get("current", specific_current)
                            )
                return user_specific_ret
    return _outpost_flavors_cache


def sync_get_flavors(log, user):
    loop = asyncio.new_event_loop()

    def t_get_flavors(loop, log, user):
        asyncio.set_event_loop(loop)
        ret = loop.run_until_complete(async_get_flavors(log, user))
        log.info(ret)
        return ret

    t = Thread(target=t_get_flavors, args=(loop, log, user))
    t.start()
    ret = t.join()
    log.info(ret)
    return ret


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
        ret = await async_get_flavors(self.log, self.current_user)
        self.write(json.dumps(ret))
        self.set_status(200)
        return


default_handlers.append((r"/api/outpostflavors/([^/]+)", OutpostFlavorsAPIHandler))
default_handlers.append((r"/api/outpostflavors", OutpostFlavorsAPIHandler))
