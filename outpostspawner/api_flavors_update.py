import json

from forwardbasespawner.utils import check_custom_scopes
from jupyterhub.apihandlers import APIHandler
from jupyterhub.apihandlers import default_handlers
from jupyterhub.utils import token_authenticated

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
                f"{user_name}:{server_name} - Could not load body into json. Body: {body}"
            )
            return

        _outpost_flavors_cache[outpost_name] = flavors
        self.set_status(200)

    async def get(self):
        global _outpost_flavors_cache
        self.write(json.dumps(_outpost_flavors_cache))
        self.set_status(200)
        return


default_handlers.append((r"/api/outpostflavors/([^/]+)", OutpostFlavorsAPIHandler))
default_handlers.append((r"/api/outpostflavors", OutpostFlavorsAPIHandler))
