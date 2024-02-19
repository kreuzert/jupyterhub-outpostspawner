import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlunparse

import escapism
from forwardbasespawner import ForwardBaseSpawner
from jupyterhub.utils import maybe_future
from tornado import web
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPClientError
from tornado.httpclient import HTTPRequest
from tornado.ioloop import PeriodicCallback
from traitlets import Any
from traitlets import Bool
from traitlets import Callable
from traitlets import default
from traitlets import Dict
from traitlets import Integer
from traitlets import Unicode
from traitlets import Union


class OutpostSpawner(ForwardBaseSpawner):
    """
    A JupyterHub spawner that spawns services on remote locations in combination with
    a JupyterHub Outpost service.
    """

    check_allowed = Any(
        help="""
        An optional hook function you can implement to double check if the
        given user_options allow a start. If the start is not allowed, it should
        raise an exception.
        
        This may be a coroutine.
        
        Example::
            
            def custom_check_allowed(spawner, user_options):
                if not user_options.get("allowed", True):
                    raise Exception("This is not allowed")
            
            c.OutpostSpawner.check_allowed = custom_check_allowed
        """,
    ).tag(config=True)

    custom_env = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, you can implement to add
        extra environment variables to send to the JupyterHub Outpost service.
        
        This may be a coroutine.
        
        Example::
        
            async def custom_env(spawner, user_options, jupyterhub_api_url):
                system = user_options.get("system", "")
                env = {
                    "JUPYTERHUB_STAGE": os.environ.get("JUPYTERHUB_STAGE", ""),
                    "JUPYTERHUB_DOMAIN": os.environ.get("JUPYTERHUB_DOMAIN", ""),
                    "JUPYTERHUB_OPTION1": user_options.get("option1", "")
                }
                if system:
                    env["JUPYTERHUB_FLAVORS_UPDATE_URL"] = f"{jupyterhub_api_url.rstrip('/')}/outpostflavors/{system}"
                return env
            
            c.OutpostSpawner.custom_env = custom_env
        """,
    ).tag(config=True)

    custom_user_options = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, you can implement to add
        extra user_options to send to the JupyterHub Outpost service.
        
        This may be a coroutine.
        
        Example::
        
            async def custom_user_options(spawner, user_options):
                user_options["image"] = "jupyter/minimal-notebook:latest"
                return user_options
            
            c.OutpostSpawner.custom_user_options = custom_user_options
        """,
    ).tag(config=True)

    custom_misc_disable_default = Bool(
        default_value=False,
        help="""
        By default, these `misc` options will be send to the Outpost service
        to override the corresponding values of the Spawner configured at the 
        Outpost. You can disable this behaviour by setting this value to true.
        
        Default `custom_misc` options::

            extra_labels = await self.get_extra_labels()
            custom_misc.update({
              "dns_name_template": self.dns_name_template,
              "pod_name_template": self.svc_name_template,
              "internal_ssl": self.internal_ssl,
              "ip": "0.0.0.0",
              "port": self.port,
              "services_enabled": True,
              "extra_labels": extra_labels
            }
        """,
    ).tag(config=True)

    custom_misc = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, you can implement to add
        extra configurations to send to the JupyterHub Outpost service.
        This will override the Spawner configuration set at the Outpost.
        `key` can be anything you would normally use in your Spawner configuration:
        `c.OutpostSpawner.<key> = <value>`
        
        This may be a coroutine.
        
        Example::
        
            async def custom_misc(spawner, user_options):
                return {
                    "image": "jupyter/base-notebook:latest"
                }
            
            c.OutpostSpawner.custom_misc = custom_misc

        will override the image configured at the Outpost::

            c.JupyterHubOutpost.spawner_class = KubeSpawner
            c.KubeSpawner.image = "default_image:1.0"

        and spawn a JupyterLab using the `jupyter/base-notebook:latest` image.
        """,
    ).tag(config=True)

    request_kwargs = Union(
        [Dict(), Callable()],
        default_value={},
        help="""
        An optional hook function, or dict, you can implement to define
        keyword arguments for all requests sent to the JupyterHub Outpost service.
        They are directly forwarded to the tornado.httpclient.HTTPRequest object.
                
        Example::
        
            def request_kwargs(spawner, user_options):
                return {
                    "request_timeout": 30,
                    "connect_timeout": 10,
                    "ca_certs": ...,
                    "validate_cert": ...,
                }
                
            c.OutpostSpawner.request_kwargs = request_kwargs
        """,
    ).tag(config=True)

    custom_poll_interval = Union(
        [Integer(), Callable()],
        default_value=0,
        help="""
        An optional hook function, or dict, you can implement to define
        the poll interval (in milliseconds). This allows you to have to different intervals
        for different Outpost services. You can use this to randomize the poll interval
        for each spawner object. 
        
        Example::

            import random
            def custom_poll_interval(spawner, user_options):
                system = user_options.get("system", "None")
                if system == "A":
                    base_poll_interval = 30
                    poll_interval_randomizer = 10
                    poll_interval = 1e3 * base_poll_interval + random.randint(
                        0, 1e3 * poll_interval_randomizer
                    )
                else:
                    poll_interval = 0
                return poll_interval
            
            c.OutpostSpawner.custom_poll_interval = custom_poll_interval
        """,
    ).tag(config=True)

    failed_spawn_request_hook = Any(
        help="""
        An optional hook function you can implement to handle a failed
        start attempt properly. This will be called if the POST request
        to the Outpost service was not successful.
        
        This may be a coroutine.
        
        Example::

            def custom_failed_spawn_request_hook(Spawner, exception_thrown):
                ...
                return
            
            c.OutpostSpawner.failed_spawn_request_hook = custom_failed_spawn_request_hook
        """
    ).tag(config=True)

    post_spawn_request_hook = Any(
        help="""
        An optional hook function you can implement to handle a successful
        start attempt properly. This will be called if the POST request
        to the Outpost service was successful.
        
        This may be a coroutine.
        
        Example::
        
            def post_spawn_request_hook(Spawner, resp_json):
                ...
                return
            
            c.OutpostSpawner.post_spawn_request_hook = post_spawn_request_hook
        """
    ).tag(config=True)

    request_404_poll_keep_running = Bool(
        default_value=False,
        help="""        
        How to handle a 404 response from Outpost API during a singleuser poll request.
        """,
    ).tag(config=True)

    request_failed_poll_keep_running = Bool(
        default_value=True,
        help="""
        How to handle a failed request to Outpost API during a singleuser poll request.
        """,
    ).tag(config=True)

    request_url = Union(
        [Unicode(), Callable()],
        help="""
        The URL used to communicate with the JupyterHub Outpost service. 
        
        This may be a coroutine.
        
        Example::

            def request_url(spawner, user_options):
                if user_options.get("system", "") == "A":
                    return "http://outpost.namespace.svc:8080/services/"
                else:
                    return "https://remote-outpost.com/services/"
            
            c.OutpostSpawner.request_url = request_url
        """,
    ).tag(config=True)

    start_async = Bool(
        default_value=False,
        help="""
        Whether the start at the Outpost service should run in the background or not.
        """,
    ).tag(config=True)

    stop_async = Bool(
        default_value=False,
        help="""
        Whether the stop at the Outpost service should run in the background or not.
        """,
    ).tag(config=True)

    request_headers = Union(
        [Dict(), Callable()],
        help="""
        An optional hook function, or dict, you can implement to define
        the header used for all requests sent to the JupyterHub Outpost service.
        They are forwarded directly to the tornado.httpclient.HTTPRequest object.
                
        Example::

            def request_headers(spawner, user_options):
                if user_options.get("system", "") == "A":
                    auth = os.environ.get("SYSTEM_A_AUTHENTICATION")
                else:
                    auth = os.environ.get("SYSTEM_B_AUTHENTICATION")
                return {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Basic {auth}"
                }
            
            c.OutpostSpawner.request_headers = request_headers
        """,
    ).tag(config=True)

    def get_request_kwargs(self):
        """Get the request kwargs

        Returns:
          request_kwargs (dict): Parameters used in HTTPRequest(..., **request_kwargs)

        """
        if callable(self.request_kwargs):
            request_kwargs = self.request_kwargs(self, self.user_options)
        else:
            request_kwargs = self.request_kwargs
        return request_kwargs

    @property
    def poll_interval(self):
        """Get poll interval.

        Returns:
          poll_interval (float): poll status of singleuser server
                                 every x seconds.
        """
        if callable(self.custom_poll_interval):
            poll_interval = self.custom_poll_interval(self, self.user_options)
        elif self.custom_poll_interval:
            poll_interval = self.custom_poll_interval
        else:
            poll_interval = 1e3 * 30
        return poll_interval

    def start_polling(self):
        # Override start_polling function. We want to use milliseconds
        # instead of seconds.
        """Start polling periodically for single-user server's running state.

        Callbacks registered via `add_poll_callback` will fire if/when the server stops.
        Explicit termination via the stop method will not trigger the callbacks.
        """
        if self.poll_interval <= 0:
            self.log.debug(f"{self._log_name} - Not polling subprocess")
            return
        elif self.poll_interval < 1000:
            self.log.warning(
                f"{self._log_name} - Current poll interval ( {self.poll_interval} ) is lower than 1000. Assume that the configured value is in seconds. Multiply poll_interval by 1000."
            )
            poll_interval = self.poll_interval * 1000
        else:
            poll_interval = self.poll_interval

        self.log.debug(
            f"{self._log_name} - Polling subprocess every %i ms", poll_interval
        )

        self.stop_polling()

        self._poll_callback = PeriodicCallback(self.poll_and_notify, poll_interval)
        self._poll_callback.start()

    def run_pre_spawn_hook(self):
        """Prepare some variables and show the first event"""
        if self.already_stopped:
            raise Exception("Server is in the process of stopping, please wait.")

        ret = super().run_pre_spawn_hook()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        start_pre_msg = "Sending request to Outpost service to start your service."
        start_event = {
            "failed": False,
            "progress": 10,
            "html_message": f"<details><summary>{now}: {start_pre_msg}</summary>\
                &nbsp;&nbsp;Start {self.name}<br>&nbsp;&nbsp;Options:<br><pre>{json.dumps(self.user_options, indent=2)}</pre></details>",
        }
        self.latest_events = [start_event]

        return ret

    @default("failed_spawn_request_hook")
    def _failed_spawn_request_hook(self):
        return self._default_failed_spawn_request_hook

    def _default_failed_spawn_request_hook(self, spawner, exception):
        return

    async def run_failed_spawn_request_hook(self, exception):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        event = {
            "progress": 99,
            "failed": False,
            "html_message": f"<details><summary>{now}: JupyterLab start failed. Deleting related resources...</summary>This may take a few seconds.</details>",
        }
        self.latest_events.append(event)
        # Ensure that we're waiting 2*yield_wait_seconds, so that
        # events will be shown to the spawn-pending page.
        await asyncio.sleep(2 * self.yield_wait_seconds)

        # If it's an exception with status code 419 it was thrown
        # by OutpostSpawner itself. This allows us to show the
        # actual reason for the failed start.
        summary = "Unknown Error"
        details = ""
        if getattr(exception, "status_code", 0) == 419:
            summary = getattr(exception, "log_message", summary)
            details = getattr(exception, "reason", details)
            try:
                details = json.loads(details.decode())
            except:
                pass
        else:
            details = str(exception)

        async def _get_stop_event(spawner):
            """Setting self.stop_event to a function will show us the correct
            datetime, when stop_event is shown to the user."""
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            event = {
                "progress": 100,
                "failed": True,
                "html_message": f"<details><summary>{now}: {summary}</summary>{details}</details>",
            }
            return event

        self.stop_event = _get_stop_event
        await maybe_future(self.failed_spawn_request_hook(self, exception))

    @default("post_spawn_request_hook")
    def _post_spawn_request_hook(self):
        return self._default_post_spawn_request_hook

    def _default_post_spawn_request_hook(self, spawner, resp_json):
        return

    def run_post_spawn_request_hook(self, resp_json):
        """If communication was successful, we show this to the user"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        progress = 20
        if (
            self.latest_events
            and type(self.latest_events) == list
            and len(self.latest_events) > 0
        ):
            progress = self.latest_events[-1].get("progress")
        submitted_event = {
            "failed": False,
            "ready": False,
            "progress": progress,
            "html_message": f"<details><summary>{now}: Outpost communication successful.</summary>You will receive further information about the service status from the service itself.</details>",
        }
        self.latest_events.append(submitted_event)
        return self.post_spawn_request_hook(self, resp_json)

    async def get_request_url(self, attach_name=False):
        """Get request url

        Returns:
          request_url (string): Used to communicate with Outpost service
        """
        if callable(self.request_url):
            request_url = await maybe_future(self.request_url(self, self.user_options))
        else:
            request_url = self.request_url
        request_url = request_url.rstrip("/")
        if attach_name:
            request_url = f"{request_url}/{self.name}/{self.start_id}"
        return request_url

    async def get_request_headers(self):
        """Get request headers

        Returns:
          request_headers (dict): Used in communication with Outpost service

        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if callable(self.request_headers):
            request_headers = await maybe_future(
                self.request_headers(self, self.user_options)
            )
        else:
            request_headers = self.request_headers
        headers.update(request_headers)
        return headers

    async def run_check_allowed(self):
        """Run allowed check.

        May raise an exception if start is not allowed.
        """
        if callable(self.check_allowed):
            await maybe_future(self.check_allowed(self, self.user_options))

    async def get_custom_env(self):
        """Get customized environment variables

        Returns:
          env (dict): Used in communication with Outpost service.
        """
        env = self.get_env()

        # Remove keys that might disturb new JupyterLabs (like PATH, PYTHONPATH)
        for key in set(env.keys()):
            if not (key.startswith("JUPYTER_") or key.startswith("JUPYTERHUB_")):
                self.log.debug(f"{self._log_name} - Remove {key} from env")
                del env[key]

        if callable(self.custom_env):
            custom_env = await maybe_future(
                self.custom_env(self, self.user_options, env["JUPYTERHUB_API_URL"])
            )
        else:
            custom_env = self.custom_env
        env.update(custom_env)

        env["JUPYTERHUB_USER_ID"] = str(self.user.id)
        return env

    async def get_custom_user_options(self):
        """Get customized user_options

        Returns:
          user_options (dict): Used in communication with Outpost service.

        """
        user_options = deepcopy(self.user_options)
        if callable(self.custom_user_options):
            custom_user_options = await maybe_future(
                self.custom_user_options(self, user_options)
            )
        else:
            custom_user_options = self.custom_user_options
        user_options.update(custom_user_options)
        user_options["start_id"] = self.start_id
        return user_options

    async def get_custom_misc(self):
        """Get customized outpost configuration

        Returns:
          custom_misc (dict): Used in communication with Outpost service
                              to override configuration in remote spawner.

        """
        if callable(self.custom_misc):
            custom_misc = await maybe_future(self.custom_misc(self, self.user_options))
        else:
            custom_misc = self.custom_misc

        if not self.custom_misc_disable_default:
            custom_misc["dns_name_template"] = self.dns_name_template
            custom_misc["pod_name_template"] = self.svc_name_template
            custom_misc["internal_ssl"] = self.internal_ssl
            custom_misc["port"] = self.port
            custom_misc["ip"] = "0.0.0.0"
            custom_misc["services_enabled"] = True
            custom_misc["extra_labels"] = await self.get_extra_labels()

        return custom_misc

    async def get_extra_labels(self):
        """Get extra labels

        Returns:
          extra_labels (dict): Used in custom_misc and in default svc.
                               Labels are used in svc and remote pod.
        """
        if callable(self.extra_labels):
            extra_labels = await maybe_future(
                self.extra_labels(self, self.user_options)
            )
        else:
            extra_labels = self.extra_labels

        return extra_labels

    http_client = Any()

    @default("http_client")
    def _default_http_client(self):
        return AsyncHTTPClient(force_instance=True, defaults=dict(validate_cert=False))

    async def fetch(self, req, action):
        """Wrapper for tornado.httpclient.AsyncHTTPClient.fetch

        Handles exceptions and responsens of the Outpost service.

        Returns:
          dict or None

        """
        try:
            resp = await self.http_client.fetch(req)
        except HTTPClientError as e:
            if e.response:
                # Log failed response message for debugging purposes
                message = e.response.body.decode("utf8", "replace")
                traceback = ""
                try:
                    # guess json, reformat for readability
                    json_message = json.loads(message)
                except ValueError:
                    # not json
                    pass
                else:
                    if e.code == 419:
                        args_list = json_message.get("args", [])
                        if type(args_list) != list or len(args_list) == 0:
                            args_list = ["Unknown error"]
                        else:
                            args_list = [str(s) for s in args_list]
                        message = f"{json_message.get('module')}{json_message.get('class')}: {' - '.join(args_list)}"
                        traceback = json_message.get("traceback", "")
                    else:
                        # reformat json log message for readability
                        message = json.dumps(json_message, sort_keys=True, indent=1)
            else:
                # didn't get a response, e.g. connection error
                message = str(e)
                traceback = ""
            url = urlunparse(urlparse(req.url)._replace(query=""))
            self.log.exception(
                f"{self._log_name} - Communication with outpost failed: {e.code} {req.method} {url}: {message}.\nOutpost traceback:\n{traceback}",
                extra={
                    "uuidcode": self.name,
                    "log_name": self._log_name,
                    "user": self.user.name,
                    "action": action,
                },
            )
            raise web.HTTPError(
                419,
                log_message=f"{action} request to {req.url} failed: {e.code}",
                reason=message,
            )
        except Exception as e:
            raise web.HTTPError(
                419, log_message=f"{action} request to {req.url} failed", reason=str(e)
            )
        try:
            body = getattr(resp, "body", b"{}").decode("utf8", "replace")
            return json.loads(body)
        except:
            return None

    async def send_request(self, req, action, raise_exception=True):
        """Wrapper to monitor the time used for any request.

        Returns:
          dict or None
        """
        tic = time.monotonic()
        try:
            resp = await self.fetch(req, action)
        except Exception as tic_e:
            if raise_exception:
                raise tic_e
            else:
                return {}
        else:
            return resp
        finally:
            toc = str(time.monotonic() - tic)
            self.log.info(
                f"{self._log_name} - Communicated {action} with Outpost service ( {req.url} ) (request duration: {toc})",
                extra={
                    "uuidcode": self.name,
                    "log_name": self._log_name,
                    "user": self.user.name,
                    "duration": toc,
                },
            )

    async def _start(self):
        self.log.info(
            f"{self._log_name} - Start singleuser server",
            extra={
                "uuidcode": self.name,
                "log_name": self._log_name,
                "user": self.user.name,
            },
        )
        await self.run_check_allowed()
        env = await self.get_custom_env()
        user_options = await self.get_custom_user_options()
        misc = await self.get_custom_misc()
        name = self.name

        request_body = {
            "name": name,
            "env": env,
            "user_options": user_options,
            "misc": misc,
            "certs": {},
            "internal_trust_bundles": {},
        }

        auth_state = await self.user.get_auth_state()
        if auth_state:
            user_specific_flavors = auth_state.get("outpost_flavors", {})
            if user_specific_flavors:
                auth = {
                    "access_token": auth_state.get("access_token", ""),
                    "name": auth_state.get("name", ""),
                    "groups": auth_state.get("groups", []),
                }
                request_body["authentication"] = auth

        if self.internal_ssl:
            for key, path in self.cert_paths.items():
                with open(path, "r") as f:
                    request_body["certs"][key] = f.read()
            for key, path in self.internal_trust_bundles.items():
                with open(path, "r") as f:
                    request_body["internal_trust_bundles"][key] = f.read()

        request_header = await self.get_request_headers()
        url = await self.get_request_url()
        ssh_during_startup = self.get_ssh_during_startup()
        if self.start_async and not ssh_during_startup:
            request_header["execution-type"] = "async"

        req = HTTPRequest(
            url=url,
            method="POST",
            headers=request_header,
            body=json.dumps(request_body),
            **self.get_request_kwargs(),
        )

        try:
            resp_json = await self.send_request(req, action="start")
        except Exception as e:
            # If JupyterHub could not start the service, additional
            # actions may be required.
            self.log.exception(
                f"{self._log_name} - Send Request failed",
                extra={
                    "uuidcode": self.name,
                    "log_name": self._log_name,
                    "user": self.user.name,
                },
            )
            await maybe_future(self.run_failed_spawn_request_hook(e))

            try:
                await self.stop()
            except:
                self.log.exception(
                    f"{self._log_name} - Could not stop service which failed to start.",
                    extra={
                        "uuidcode": self.name,
                        "log_name": self._log_name,
                        "user": self.user.name,
                    },
                )
            # We already stopped everything we can stop at this stage.
            # With the raised exception JupyterHub will try to cancel again.
            # We can skip these stop attempts. Failed Spawners will be
            # available again faster.
            self.already_stopped = True
            self.already_post_stop_hooked = True

            raise e

        await maybe_future(self.run_post_spawn_request_hook(resp_json))

        return resp_json.get("service", "")

    async def _poll(self):
        url = await self.get_request_url(attach_name=True)
        headers = await self.get_request_headers()
        req = HTTPRequest(
            url=url,
            method="GET",
            headers=headers,
            **self.get_request_kwargs(),
        )

        try:
            resp_json = await self.send_request(req, action="poll")
        except Exception as e:
            ret = 0
            if (
                type(e).__name__ == "HTTPError"
                and getattr(e, "status_code", 500) == 419
                and getattr(e, "log_message", "500").endswith("404")
            ):
                if self.request_404_poll_keep_running:
                    ret = None
            elif self.request_failed_poll_keep_running:
                ret = None
            self.log.exception(
                f"{self._log_name} - Could not poll current status - Return {ret}"
            )
        else:
            ret = resp_json.get("status", None)

        return ret

    async def _stop(self, now=False, cancel=False, event=None, **kwargs):
        url = await self.get_request_url(attach_name=True)
        headers = await self.get_request_headers()
        if self.stop_async:
            headers["execution-type"] = "async"
        req = HTTPRequest(
            url=url,
            method="DELETE",
            headers=headers,
            **self.get_request_kwargs(),
        )

        await self.send_request(req, action="stop", raise_exception=False)

        if self.cert_paths:
            Path(self.cert_paths["keyfile"]).unlink(missing_ok=True)
            Path(self.cert_paths["certfile"]).unlink(missing_ok=True)
            try:
                Path(self.cert_paths["certfile"]).parent.rmdir()
            except:
                pass
