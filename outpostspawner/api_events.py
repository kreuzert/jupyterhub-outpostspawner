import asyncio
import datetime
import json

from jupyterhub.apihandlers import default_handlers
from jupyterhub.apihandlers.base import APIHandler
from jupyterhub.scopes import needs_scope
from tornado import web

user_cancel_message = (
    "Start cancelled by user.</summary>You clicked the cancel button.</details>"
)


class SpawnEventsAPIHandler(APIHandler):
    def check_xsrf_cookie(self):
        pass

    @needs_scope("read:servers")
    async def get(self, user_name, server_name=""):
        user = self.find_user(user_name)
        if user is None:
            # no such user
            raise web.HTTPError(404)
        if server_name not in user.spawners:
            # user has no such server
            raise web.HTTPError(404)
        spawner = user.spawners[server_name]
        data = {
            "events": spawner.latest_events,
            "active": spawner.active,
            "ready": spawner.ready,
        }
        self.write(json.dumps(data))

    @needs_scope("access:servers")
    async def post(self, user_name, server_name=""):
        self.set_header("Cache-Control", "no-cache")
        if server_name is None:
            server_name = ""
        user = self.find_user(user_name)
        if user is None:
            # no such user
            raise web.HTTPError(404)
        if server_name not in user.spawners:
            # user has no such server
            raise web.HTTPError(404)
        body = self.request.body.decode("utf8")
        event = json.loads(body) if body else {}

        user = self.find_user(user_name)
        spawner = user.spawners[server_name]
        uuidcode = server_name

        # Do not do anything if stop or cancel is already pending
        if spawner.pending == "stop" or spawner.already_stopped:
            self.set_status(204)
            return

        if event and event.get("failed", False):
            if event.get("html_message", "").endswith(user_cancel_message):
                self.log.debug(
                    "APICall: SpawnUpdate",
                    extra={
                        "uuidcode": uuidcode,
                        "log_name": f"{user_name}:{server_name}",
                        "user": user_name,
                        "action": "cancel",
                        "event": event,
                    },
                )
                # Add correct timestamp to event, at the moment it will be used.
                async def stop_event(spawner):
                    now = datetime.datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
                    return {
                        "failed": True,
                        "ready": False,
                        "progress": 100,
                        "message": "",
                        "html_message": f"<details><summary>{now}: {user_cancel_message}",
                    }

                asyncio.create_task(spawner.stop(cancel=True, event=stop_event))
            else:
                self.log.debug(
                    "APICall: SpawnUpdate",
                    extra={
                        "uuidcode": uuidcode,
                        "log_name": f"{user_name}:{server_name}",
                        "user": user_name,
                        "action": "failed",
                        "event": event,
                    },
                )
                asyncio.create_task(spawner.stop(cancel=True, event=event))
            self.set_header("Content-Type", "text/plain")
            self.set_status(204)
            return

        try:
            event = spawner.run_filter_events(event)
        except:
            self.log.exception("Could not filter exception")
            event = {}
        else:
            if event is None:
                event = {}

        if event.get("html_message", ""):
            # Add timestamp
            now = datetime.datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
            if event["html_message"].startswith("<details><summary>"):
                event[
                    "html_message"
                ] = f"<details><summary>{now}: {event['html_message'][len('<details><summary>'):]}"
            else:
                event["html_message"] = f"{now}: {event['html_message']}"

        if not event or spawner._stop_pending:
            self.set_header("Content-Type", "text/plain")
            self.write("Bad Request")
            self.set_status(400)
            return
        else:
            self.log.debug(
                "APICall: SpawnUpdate",
                extra={
                    "uuidcode": uuidcode,
                    "log_name": f"{user_name}:{server_name}",
                    "user": user_name,
                    "action": "spawnupdate",
                    "event": event.get("html_message", event.get("message", event)),
                },
            )
            spawner = user.spawners[server_name]
            if hasattr(spawner, "latest_events"):
                spawner.latest_events.append(event)
            self.set_header("Content-Type", "text/plain")
            self.set_status(204)
            return


default_handlers.append((r"/api/users/progress/events/([^/]+)", SpawnEventsAPIHandler))
default_handlers.append(
    (r"/api/users/progress/events/([^/]+)/([^/]+)", SpawnEventsAPIHandler)
)
