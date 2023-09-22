import asyncio
import inspect
import json
import os
import string
import subprocess
import time
import traceback
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlunparse

import escapism
from jupyterhub.spawner import Spawner
from jupyterhub.utils import maybe_future
from jupyterhub.utils import random_port
from jupyterhub.utils import url_path_join
from kubernetes import client
from kubernetes import config
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
from traitlets import List
from traitlets import Unicode
from traitlets import Union


@lru_cache
def get_name(key):
    """Load value from the k8s ConfigMap given a key."""

    path = f"/home/jovyan/jupyterhub-config/config/{key}"
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    else:
        raise Exception(f"{path} not found!")


class OutpostSpawner(Spawner):
    """
    A JupyterHub spawner that spawn services on remote locations in combination with
    a JupyterHub outpost service.
    """

    # Remote jupyterhub-singleuser servers might require a ssh port forward
    # to be reachable by jupyterhub. This dict will contain this information
    # ssh -i <key> -L <local_host>:<local_port>:<remote_host>:<remote_port> <user>@<node>
    port_forward_info = {}

    # If the jupyterhub-singleuser servers dies while the hub is down, .stop() and
    # .post_stop_hook() will not be called. This attribute will fix this behaviour.
    # We set this to True by default, but set it to false during .start() and .poll().
    # Therefore it will be only True for the first .poll() call during hub initialization
    # https://github.com/jupyterhub/jupyterhub/issues/4519
    # Can be removed after JupyterHub got fixed
    call_during_startup = True

    # This is used to prevent multiple requests during the stop procedure.
    already_stopped = False
    already_post_stop_hooked = False

    @property
    def internal_ssl(self):
        """
        Returns self.custom_internal_ssl result if defined, user.settings.get('internal_ssl', False) otherwise
        """
        if self.custom_internal_ssl:
            ret = self.custom_internal_ssl(self)
        else:
            ret = self.user.settings.get("internal_ssl", False)
        return ret

    custom_internal_ssl = Any(
        help="""
        An optional hook function that you can implement do override the internal_ssl
        value for a spawner. Return value must be boolean.
        
        This maybe a coroutine.
        
        Example::
        
            def custom_internal_ssl(spawner):
                return spawner.name.startswith("ssl-")
        
            c.OutpostSpawner.custom_internal_ssl = custom_internal_ssl
        """,
    ).tag(config=True)

    check_allowed = Any(
        help="""
        An optional hook function that you can implement do double check if the
        given user_options allow a start. If the start is not allowed, it should
        raise an exception.
        
        This maybe a coroutine.
        
        Example::
            
            def custom_check_allowed(spawner):
                if not spawner.user_options.get("allowed", True):
                    raise Exception("This is not allowed")
            
            c.OutpostSpawner.check_allowed = custom_check_allowed
        """,
    ).tag(config=True)

    custom_env = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, that you can implement to add
        extra environment variables send to the JupyterHub outpost service.
        
        This maybe a coroutine.
        
        Example::
        
            async def custom_env(spawner):
                env = {
                    "JUPYTERHUB_STAGE": os.environ.get("JUPYTERHUB_STAGE", ""),
                    "JUPYTERHUB_DOMAIN": os.environ.get("JUPYTERHUB_DOMAIN", ""),
                    "JUPYTERHUB_OPTION1": spawner.user_options.get("option1", "")
                }
                return env
            
            c.OutpostSpawner.custom_env = custom_env
        """,
    ).tag(config=True)

    custom_user_options = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, that you can implement to add
        extra user_options send to the JupyterHub outpost service.
        
        This maybe a coroutine.
        
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
        By default these `misc` options will be send to the Outpost service,
        to override the remotely configured Spawner options. You can disable
        this behaviour by setting this value to true.
        
        Default `custom_misc` options::

            extra_labels = await self.get_extra_labels()
            custom_misc.update({
              "dns_name_template": self.dns_name_template,
              "pod_name_template": self.pod_name_template,
              "internal_ssl": self.interal_ssl,
              "port": self.port,
              "services_enabled": True,
              "extra_labels": extra_labels
            }
        """,
    ).tag(config=True)

    custom_misc = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, that you can implement to add
        extra configuration send to the JupyterHub outpost service.
        This will override the Spawner configuration at the outpost.
        `key` can be anything you would normally use in your Spawner configuration:
        `c.OutpostSpawner.<key> = <value>`
        
        This maybe a coroutine.
        
        Example::
        
            async def custom_misc(spawner):
                return {
                    "image": "jupyter/base-notebook:latest"
                }
            
            c.OutpostSpawner.custom_misc = custom_misc
        """,
    ).tag(config=True)

    extra_labels = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook function, or dict, that you can implement to add
        extra labels to the service created when using port-forward.
        Will also be forwarded to the outpost service (see self.custom_misc_disable_default)
        
        This maybe a coroutine.
        
        Example::

            def extra_labels(spawner):
                labels = {
                    "hub.jupyter.org/username": spawner.user.name,
                    "hub.jupyter.org/servername": spawner.name,
                    "sidecar.istio.io/inject": "false"
                }
                return labels
            
            c.OutpostSpawner.extra_labels = extra_labels
        """,
    ).tag(config=True)

    request_kwargs = Union(
        [Dict(), Callable()],
        default_value={},
        help="""
        An optional hook function, or dict, that you can implement to define
        keyword arguments for all requests send to the JupyterHub outpost service.
        They are directly forwarded to the tornado.httpclient.HTTPRequest object.
                
        Example::
        
            def request_kwargs(spawner):
                return {
                    "request_timeout": 30,
                    "connect_timeout": 10,
                    "ca_certs": ...,
                    "validate_cert": ...,
                }
                
            c.OutpostSpawner.request_kwargs = request_kwargs
        """,
    ).tag(config=True)

    custom_port = Union(
        [Integer(), Callable()],
        default_value=8080,
        help="""
        An optional hook function, or dict, that you can implement to define
        a port depending on the spawner object.
        
        Example::
        
            from jupyterhub.utils import random_potr
            def custom_port(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return 8080
                return random_port()
            
            c.OutpostSpawner.custom_port = custom_port
        """,
    ).tag(config=True)

    poll_interval = Union(
        [Integer(), Callable()],
        default_value=0,
        help="""
        An optional hook function, or dict, that you can implement to define
        the poll interval (in seconds). This allows you to have to different intervals
        for different outpost services. You can use this to randomize the poll interval
        for each spawner object. 
        
        Example::

            import random
            def poll_interval(spawner):
                system = spawner.user_options.get("system", "None")
                if system == "A":
                    base_poll_interval = 30
                    poll_interval_randomizer = 10
                    poll_interval = 1e3 * base_poll_interval + random.randint(
                        0, 1e3 * poll_interval_randomizer
                    )
                else:
                    poll_interval = 0
                return poll_interval
            
            c.OutpostSpawner.poll_interval = poll_interval
        """,
    ).tag(config=True)

    failed_spawn_request_hook = Any(
        help="""
        An optional hook function that you can implement to handle a failed
        start attempt properly. This will be called, if the POST request
        to the outpost service was not successful.
        
        This maybe a coroutine.
        
        Example::

            def custom_failed_spawn_request_hook(Spawner, exception_thrown):
                ...
                return
            
            c.OutpostSpawner.failed_spawn_request_hook = custom_failed_spawn_request_hook
        """
    ).tag(config=True)

    post_spawn_request_hook = Any(
        help="""
        An optional hook function that you can implement to handle a successful
        start attempt properly. This will be called, if the POST request
        to the outpost service was successful.
        
        This maybe a coroutine.
        
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
        How to handle a 404 response from outpost API during singleuser poll request.
        """,
    ).tag(config=True)

    request_failed_poll_keep_running = Bool(
        default_value=True,
        help="""
        How to handle a failed request to outpost API during singleuser poll request.
        """,
    ).tag(config=True)

    request_url = Union(
        [Unicode(), Callable()],
        help="""
        The URL used to communicate with the JupyterHub outpost service. 
        
        This maybe a coroutine.
        
        Example::

            def request_url(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return "http://outpost.namespace.svc:8080/services/"
                else:
                    return "https://remote-outpost.com/services/"
            
            c.OutpostSpawner.request_url = request_url
        """,
    ).tag(config=True)

    request_headers = Union(
        [Dict(), Callable()],
        help="""
        An optional hook function, or dict, that you can implement to define
        the header userd for all requests send to the JupyterHub outpost service.
        They are forwarded directly to the tornado.httpclient.HTTPRequest object.
                
        Example::

            def request_headers(spawner):
                if spawner.user_options.get("system", "") == "A":
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

    ssh_recreate_at_start = Union(
        [Callable(), Bool()],
        default_value=True,
        help="""
        Whether ssh tunnels should be recreated at JupyterHub start or not.
        If you have outsourced the port forwarding to an extra pod, you can
        set this to false. This also means, that running JupyterLabs are not
        affected by JupyterHub restarts.
        
        This maybe a coroutine.
        """,
    ).tag(config=True)

    ssh_enabled = Union(
        [Callable(), Bool()],
        default_value=True,
        help="""
        An optional hook function, or boolean, that you can implement to
        decide whether a ssh port forwarding process should be run after
        the POST request to the JupyterHub outpost service.
        
        Common Use Case: 
        singleuser service was started remotely and is not accessible by
        JupyterHub (e.g. it's running on a different K8s Cluster), but you
        know exactly where it is (e.g. the service address).

        Example::

            def ssh_enabled(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return True
                return False

            c.OutpostSpawner.ssh_enabled = ssh_enabled

        """,
    ).tag(config=True)

    ssh_key = Union(
        [Callable(), Unicode()],
        allow_none=True,
        default_value="/home/jovyan/.ssh/id_rsa",
        help="""
        An optional hook function, or string, that you can implement to
        set the ssh privatekey used for ssh port forwarding.

        This maybe a coroutine.

        Example::

            def ssh_key(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return "/mnt/private_keys/a"
                return "/mnt/private_keys/b"

            c.OutpostSpawner.ssh_key = ssh_key

        """,
    ).tag(config=True)

    ssh_username = Union(
        [Callable(), Unicode()],
        default_value="jupyterhuboutpost",
        help="""
        An optional hook function, or string, that you can implement to
        set the ssh username used for ssh port forwarding.

        This maybe a coroutine.

        Example::

            def ssh_username(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return "jupyterhuboutpost"
                return "ubuntu"

            c.OutpostSpawner.ssh_username = ssh_username

        """,
    ).tag(config=True)

    ssh_node = Union(
        [Callable(), Unicode()],
        allow_none=True,
        default_value=None,
        help="""
        An optional hook function, or string, that you can implement to
        set the ssh node used for ssh port forwarding.

        This maybe a coroutine.

        Example::

            def ssh_node(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return "outpost.namespace.svc"
                else:
                    return "<public_ip>"

            c.OutpostSpawner.ssh_node = ssh_node

        """,
    ).tag(config=True)

    ssh_port = Union(
        [Callable(), Integer()],
        default_value=22,
        help="""
        An optional hook function, or string, that you can implement to
        set the ssh port used for ssh port forwarding.

        This maybe a coroutine.

        Example::

            def ssh_port(spawner):
                if spawner.user_options.get("system", "") == "A":
                    return 22
                else:
                    return 2222

            c.OutpostSpawner.ssh_port = ssh_port

        """,
    ).tag(config=True)

    ssh_custom_forward = Any(
        help="""
        An optional hook function that you can implement to create your own
        ssh port forwarding. This can be used to use an external pod
        for the port forwarding. 
        
        Example::

            from tornado.httpclient import HTTPRequest
            def ssh_custom_forward(spawner, port_forward_info):
                url = "..."
                headers = {
                    ...
                }
                req = HTTPRequest(
                    url=url,
                    method="POST",
                    headers=headers,
                    body=json.dumps(port_forward_info),                    
                )
                await spawner.send_request(
                    req, action="setuptunnel"
                )

            c.OutpostSpawner.ssh_custom_forward = ssh_custom_forward

        """
    ).tag(config=True)

    ssh_custom_forward_remove = Any(
        help="""
        An optional hook function that you can implement to remove your own
        ssh port forwarding. This can be used to use an external pod
        for the port forwarding. 
        
        Example::

            from tornado.httpclient import HTTPRequest
            def ssh_custom_forward_remove(spawner, port_forward_info):
                url = "..."
                headers = {
                    ...
                }
                req = HTTPRequest(
                    url=url,
                    method="DELETE",
                    headers=headers,
                    body=json.dumps(port_forward_info),                    
                )
                await spawner.send_request(
                    req, action="removetunnel"
                )

            c.OutpostSpawner.ssh_custom_forward_remove = ssh_custom_forward_remove

        """
    ).tag(config=True)

    ssh_custom_svc = Any(
        help="""
        An optional hook function that you can implement to create a customized
        kubernetes svc. 
        
        Example::

            def ssh_custom_svc(spawner, port_forward_info):
                ...
                return spawner.pod_name, spawner.port

            c.OutpostSpawner.ssh_custom_svc = ssh_custom_svc

        """
    ).tag(config=True)

    ssh_custom_svc_remove = Any(
        help="""
        An optional hook function that you can implement to remove a customized
        kubernetes svc. 
        
        Example::

            def ssh_custom_svc_remove(spawner, port_forward_info):
                ...
                return spawner.pod_name, spawner.port

            c.OutpostSpawner.ssh_custom_svc_remove = ssh_custom_svc_remove

        """
    ).tag(config=True)

    ssh_forward_options = Union(
        [Dict(default_value={}), Callable()],
        help="""
        An optional hook, or dict, to configure the ssh commands used in the
        spawner.ssh_default_forward function. The default configuration parameters
        (see below) can be overriden.
        
        Default::

            ssh_forward_options_all = {
                "ServerAliveInterval": "15",
                "StrictHostKeyChecking": "accept-new",
                "ControlMaster": "auto",
                "ControlPersist": "yes",
                "Port": str(ssh_port),
                "ControlPath": f"/tmp/control_{ssh_address_or_host}",
                "IdentityFile": ssh_pkey,
            }        
        
        """,
    ).tag(config=True)

    def get_request_kwargs(self):
        """Get the request kwargs

        Returns:
          request_kwargs (dict): Parameters used in HTTPRequest(..., **request_kwargs)

        """
        if callable(self.request_kwargs):
            request_kwargs = self.request_kwargs(self)
        else:
            request_kwargs = self.request_kwargs
        return request_kwargs

    @default("port")
    def get_port(self):
        """Get the port used for the singleuser server

        Returns:
          port (int): port of the newly created singleuser server
        """
        if callable(self.custom_port):
            port = self.custom_port(self)
        else:
            port = self.custom_port
        return port

    @default("property")
    def get_poll_interval(self):
        """Get poll interval.

        Returns:
          poll_interval (float): poll status of singleuser server
                                 every x seconds.
        """
        if callable(self.poll_interval):
            poll_interval = self.poll_interval(self)
        else:
            poll_interval = self.poll_interval
        return poll_interval

    def run_pre_spawn_hook(self):
        if self.already_stopped:
            raise Exception("Server is in the process of stopping, please wait.")
        """Run the pre_spawn_hook if defined"""
        if self.pre_spawn_hook:
            return self.pre_spawn_hook(self)

    def run_post_stop_hook(self):
        if self.already_post_stop_hooked:
            return
        self.already_post_stop_hooked = True

        """Run the post_stop_hook if defined"""
        if self.post_stop_hook is not None:
            try:
                return self.post_stop_hook(self)
            except Exception:
                self.log.exception("post_stop_hook failed with exception: %s", self)

    @default("failed_spawn_request_hook")
    def _failed_spawn_request_hook(self):
        return self._default_failed_spawn_request_hook

    def _default_failed_spawn_request_hook(self, spawner, exception):
        return

    async def run_failed_spawn_request_hook(self, exception):
        await maybe_future(self.failed_spawn_request_hook(self, exception))

    @default("post_spawn_request_hook")
    def _post_spawn_request_hook(self):
        return self._default_post_spawn_request_hook

    def _default_post_spawn_request_hook(self, spawner, resp_json):
        return

    def run_post_spawn_request_hook(self, resp_json):
        return self.post_spawn_request_hook(self, resp_json)

    async def get_request_url(self, attach_name=False):
        """Get request url

        Returns:
          request_url (string): Used to communicate with outpost service
        """
        if callable(self.request_url):
            request_url = await maybe_future(self.request_url(self))
        else:
            request_url = self.request_url
        request_url = request_url.rstrip("/")
        if attach_name:
            request_url = f"{request_url}/{self.name}"
        return request_url

    async def get_request_headers(self):
        """Get request headers

        Returns:
          request_headers (dict): Used in communication with outpost service

        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if callable(self.request_headers):
            request_headers = await maybe_future(self.request_headers(self))
        else:
            request_headers = self.request_headers
        headers.update(request_headers)
        return headers

    async def run_check_allowed(self):
        """Run allowed check.

        May raise an exception, if start is not allowed.
        """
        if callable(self.check_allowed):
            await maybe_future(self.check_allowed(self))

    async def get_custom_env(self):
        """Get customized environment variables

        Returns:
          env (dict): Used in communication with outpost service.
        """
        env = self.get_env()

        # Remove keys that might disturb new JupyterLabs (like PATH, PYTHONPATH)
        for key in set(env.keys()):
            if not (key.startswith("JUPYTER_") or key.startswith("JUPYTERHUB_")):
                self.log.info(f"Remove {key} from env")
                del env[key]

        # Add URL to manage ssh tunnels
        url_parts = ["users", "setuptunnel", self.user.escaped_name]
        if self.name:
            url_parts.append(self.name)
        env["JUPYTERHUB_SETUPTUNNEL_URL"] = url_path_join(*url_parts)

        env["JUPYTERHUB_API_URL"] = self.public_api_url.rstrip("/")

        if callable(self.custom_env):
            custom_env = await maybe_future(self.custom_env(self))
        else:
            custom_env = self.custom_env
        env.update(custom_env)

        env["JUPYTERHUB_USER_ID"] = str(self.user.id)
        env[
            "JUPYTERHUB_ACTIVITY_URL"
        ] = f"{env['JUPYTERHUB_API_URL']}/users/{self.user.name}/activity"

        if self.internal_ssl:
            proto = "https://"
        else:
            proto = "http://"
        env[
            "JUPYTERHUB_SERVICE_URL"
        ] = f"{proto}0.0.0.0:{self.port}/user/{self.user.name}/{self.name}/"

        return env

    async def get_custom_user_options(self):
        """Get customized user_options

        Returns:
          user_options (dict): Used in communication with outpost service.

        """
        user_options = self.user_options
        if callable(self.custom_user_options):
            custom_user_options = await maybe_future(
                self.custom_user_options(self, user_options)
            )
        else:
            custom_user_options = self.custom_user_options
        user_options.update(custom_user_options)
        return user_options

    async def get_custom_misc(self):
        """Get customized outpost configuration

        Returns:
          custom_misc (dict): Used in communication with outpost service
                              to override configuration in remote spawner.

        """
        if callable(self.custom_misc):
            custom_misc = await maybe_future(self.custom_misc(self))
        else:
            custom_misc = self.custom_misc

        if not self.custom_misc_disable_default:
            custom_misc["dns_name_template"] = self.dns_name_template
            custom_misc["pod_name_template"] = self.pod_name_template
            custom_misc["internal_ssl"] = self.internal_ssl
            custom_misc["port"] = self.port
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
            extra_labels = await maybe_future(self.extra_labels(self))
        else:
            extra_labels = self.extra_labels

        return extra_labels

    http_client = Any()

    @default("http_client")
    def _default_http_client(self):
        return AsyncHTTPClient(force_instance=True, defaults=dict(validate_cert=False))

    def get_state(self):
        """get the current state"""
        state = super().get_state()
        state["port_forward_info"] = self.port_forward_info
        state["port"] = self.port
        return state

    def load_state(self, state):
        """load state from the database"""
        super().load_state(state)
        if "port_forward_info" in state:
            self.port_forward_info = state["port_forward_info"]
        if "port" in state:
            self.port = state["port"]

    def clear_state(self):
        """clear any state (called after shutdown)"""
        super().clear_state()
        self._start_future = None
        self.port_forward_info = {}
        self.already_stopped = False
        self.already_post_stop_hooked = False

    def start_polling(self):
        """Start polling periodically for singleuser server's running state.

        Callbacks registered via `add_poll_callback` will fire if/when the server stops.
        Explicit termination via the stop method will not trigger the callbacks.

        """
        poll_interval = self.get_poll_interval()
        if poll_interval <= 0:
            return
        else:
            self.log.debug("Polling service status every %ims", poll_interval)

        self.stop_polling()

        self._poll_callback = PeriodicCallback(self.poll_and_notify, poll_interval)
        self._poll_callback.start()

    async def fetch(self, req, action):
        """Wrapper for tornado.httpclient.AsyncHTTPClient.fetch

        Handles exceptions and responsens of the outpost service.

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
                f"Communication with outpost failed: {e.code} {req.method} {url}: {message}.\nOutpost traceback:\n{traceback}",
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
                f"Communicated {action} with outpost service ( {req.url} ) (request duration: {toc})",
                extra={
                    "uuidcode": self.name,
                    "log_name": self._log_name,
                    "user": self.user.name,
                    "duration": toc,
                },
            )

    async def get_ssh_recreate_at_start(self):
        """Get ssh_recreate_at_start

        Returns:
          ssh_recreate_at_start (bool): Restart ssh tunnels if hub was restarted
        """
        if callable(self.ssh_recreate_at_start):
            ssh_recreate_at_start = await maybe_future(self.ssh_recreate_at_start(self))
        else:
            ssh_recreate_at_start = self.ssh_recreate_at_start
        return ssh_recreate_at_start

    async def get_ssh_port(self):
        """Get ssh port

        Returns:
          ssh_port (int): Used in ssh forward command. Default is 22
        """
        if callable(self.ssh_port):
            ssh_port = await maybe_future(self.ssh_port(self, self.port_forward_info))
        else:
            ssh_port = self.port_forward_info.get("ssh_port", self.ssh_port)
        return ssh_port

    async def get_ssh_username(self):
        """Get ssh username

        Returns:
          ssh_user (string): Used in ssh forward command. Default ist "jupyterhuboutpost"
        """
        if callable(self.ssh_username):
            ssh_user = await maybe_future(
                self.ssh_username(self, self.port_forward_info)
            )
        else:
            ssh_user = self.port_forward_info.get("ssh_username", self.ssh_username)
        return ssh_user

    async def get_ssh_key(self):
        """Get ssh key

        Returns:
          ssh_key (string): Path to ssh privatekey used in ssh forward command"""
        if callable(self.ssh_key):
            ssh_key = await maybe_future(self.ssh_key(self, self.port_forward_info))
        else:
            ssh_key = self.port_forward_info.get("ssh_key", self.ssh_key)
        return ssh_key

    def get_ssh_enabled(self):
        """Get ssh enabled

        Returns:
          ssh_enabled (bool): Create ssh port forwarding after successful POST request
                              to outpost service, if true

        """
        if callable(self.ssh_enabled):
            ssh_enabled = self.ssh_enabled(self)
        else:
            ssh_enabled = self.ssh_enabled
        return ssh_enabled

    async def get_ssh_node(self):
        """Get ssh node

        Returns:
          ssh_node (string): Used in ssh port forwading command
        """

        if callable(self.ssh_node):
            ssh_node = await maybe_future(self.ssh_node(self, self.port_forward_info))
        else:
            ssh_node = self.port_forward_info.get("ssh_node", self.ssh_node)
        return ssh_node

    async def run_ssh_forward(self, create_svc=True):
        """Run the custom_create_port_forward if defined, otherwise run the default one"""
        ssh_enabled = self.get_ssh_enabled()
        if ssh_enabled:
            self.port = random_port()
            try:
                if self.ssh_custom_forward:
                    port_forward = self.ssh_custom_forward(self, self.port_forward_info)
                    if inspect.isawaitable(port_forward):
                        await port_forward
                else:
                    await self.ssh_default_forward()
            except Exception as e:
                raise web.HTTPError(
                    419,
                    log_message=f"Cannot start ssh tunnel for {self.name}: {str(e)}",
                    reason=traceback.format_exc(),
                )
            if create_svc:
                try:
                    if self.ssh_custom_svc:
                        ssh_custom_svc = self.ssh_custom_svc(
                            self, self.port_forward_info
                        )
                        if inspect.isawaitable(ssh_custom_svc):
                            ssh_custom_svc = await ssh_custom_svc
                        return ssh_custom_svc
                    else:
                        return await self.ssh_default_svc()
                except Exception as e:
                    raise web.HTTPError(
                        419,
                        log_message=f"Cannot create svc for {self.name}: {str(e)}",
                        reason=traceback.format_exc(),
                    )

    async def get_forward_cmd(self, extra_args=["-f", "-N", "-n"]):
        """Get base options for ssh port forwarding

        Returns:
          (string, string, list): (ssh_user, ssh_node, base_cmd) to be used in ssh
                                  port forwarding cmd like:
                                  <base_cmd> -L0.0.0.0:port:address:port <ssh_user>@<ssh_node>

        """
        ssh_port = await self.get_ssh_port()
        ssh_username = await self.get_ssh_username()
        ssh_address_or_host = await self.get_ssh_node()
        ssh_pkey = await self.get_ssh_key()

        ssh_forward_options_all = {
            "ServerAliveInterval": "15",
            "StrictHostKeyChecking": "accept-new",
            "ControlMaster": "auto",
            "ControlPersist": "yes",
            "Port": str(ssh_port),
            "ControlPath": f"/tmp/control_{ssh_address_or_host}",
            "IdentityFile": ssh_pkey,
        }
        if callable(self.ssh_forward_options):
            ssh_forward_options = self.ssh_forward_options(
                self, self.self.port_forward_info
            )
            if inspect.isawaitable(ssh_forward_options):
                ssh_forward_options = await ssh_forward_options
            ssh_forward_options_all.update(ssh_forward_options)
        else:
            ssh_forward_options_all.update(self.ssh_forward_options)
            ssh_forward_options_all.update(
                self.port_forward_info.get("ssh_forward_options", {})
            )

        cmd = ["ssh"]
        cmd.extend(extra_args)
        for key, value in ssh_forward_options_all.items():
            cmd.append(f"-o{key}={value}")
        return ssh_username, ssh_address_or_host, cmd

    def subprocess_cmd(self, cmd, timeout=3):
        """Execute bash cmd via subprocess.Popen as user 1000

        Returns:
          returncode (int): returncode of cmd
        """

        def set_uid():
            try:
                os.setuid(1000)
            except:
                pass

        self.log.info(f"SSH cmd: {' '.join(cmd)}")
        p = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, preexec_fn=set_uid
        )
        try:
            p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            p.kill()
            raise e
        return p.returncode

    def split_service_address(self, service_address):
        service_address_port = service_address.removeprefix("https://").removeprefix(
            "http://"
        )
        service_address_short, port = service_address_port.split(":")
        return service_address_short, port

    async def ssh_default_forward_remove(self):
        """Default function to remove previously created port forward."""
        service_address, service_port = self.split_service_address(
            self.port_forward_info.get("service")
        )
        user, node, cmd = await self.get_forward_cmd()
        cancel_cmd = cmd.copy()
        cancel_cmd.extend(
            [
                "-O",
                "cancel",
                f"-L0.0.0.0:{self.port}:{service_address}:{service_port}",
                f"{user}@{node}",
            ]
        )
        self.subprocess_cmd(cancel_cmd)

    async def ssh_default_forward(self):
        """Default function to create port forward.
        Forwards 0.0.0.0:{self.port} to {service_address}:{service_port} within
        the hub container. Uses ssh multiplex feature to reduce open connections

        Returns:
          None
        """
        # check if ssh multiplex connection is up
        user, node, cmd = await self.get_forward_cmd()
        check_cmd = cmd.copy()
        check_cmd.extend(["-O", "check", f"{user}@{node}"])
        returncode = self.subprocess_cmd(check_cmd)

        if returncode != 0:
            # Create multiplex connection
            connect_cmd = cmd.copy()
            connect_cmd.append(f"{user}@{node}")

            # First creation always runs in a timeout. Expect this and check
            # the success with check_cmd again
            try:
                returncode = self.subprocess_cmd(connect_cmd, timeout=1)
            except subprocess.TimeoutExpired as e:
                returncode = self.subprocess_cmd(check_cmd)

            if returncode != 0:
                raise Exception(
                    f"Could not create ssh connection ({connect_cmd}) (Returncode: {returncode} != 0)"
                )

        service_address, service_port = self.split_service_address(
            self.port_forward_info.get("service")
        )
        create_cmd = cmd.copy()
        create_cmd.extend(
            [
                "-O",
                "forward",
                f"-L0.0.0.0:{self.port}:{service_address}:{service_port}",
                f"{user}@{node}",
            ]
        )

        returncode = self.subprocess_cmd(create_cmd)
        if returncode != 0:
            # Maybe there's an old forward still running for this
            cancel_cmd = cmd.copy()
            cancel_cmd.extend(
                [
                    "-O",
                    "cancel",
                    f"-L0.0.0.0:{self.port}:{service_address}:{service_port}",
                    f"{user}@{node}",
                ]
            )
            self.subprocess_cmd(cancel_cmd)

            returncode = self.subprocess_cmd(create_cmd)
            if returncode != 0:
                raise Exception(
                    f"Could not forward port ({create_cmd}) (Returncode: {returncode} != 0)"
                )

    def _k8s_get_client_core(self):
        """Get python kubernetes API client"""
        config.load_incluster_config()
        return client.CoreV1Api()

    async def ssh_default_svc(self):
        """Create Kubernetes Service.
        Selector: the hub container itself
        Port + targetPort: self.port

        Removes existing services with the same name, to create a new one.

        Returns:
          (string, int): (self.pod_name, self.port)
        """
        labels = {"app": get_name("fullname"), "component": "singleuser-server"}
        extra_labels = await self.get_extra_labels()
        labels.update(extra_labels)
        v1 = self._k8s_get_client_core()
        service_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "labels": labels,
                "name": self.pod_name,
                "resourceversion": "v1",
            },
            "spec": {
                "ports": [
                    {
                        "name": "http",
                        "port": self.port,
                        "protocol": "TCP",
                        "targetPort": self.port,
                    }
                ],
                "selector": {"app": get_name("fullname"), "component": "hub"},
            },
        }
        try:
            v1.create_namespaced_service(
                body=service_manifest, namespace=self.namespace
            )
        except client.exceptions.ApiException as e:
            status_code = getattr(e, "status", 500)
            if status_code == 409:
                v1.delete_namespaced_service(
                    name=self.pod_name, namespace=self.namespace
                )
                v1.create_namespaced_service(
                    body=service_manifest, namespace=self.namespace
                )
            else:
                raise e
        return self.pod_name, self.port

    async def ssh_default_svc_remove(self):
        """Remove Kubernetes Service
        Used parameters: self.pod_name and self.namespace

        Returns:
          None
        """
        v1 = self._k8s_get_client_core()
        name = self.pod_name
        v1.delete_namespaced_service(name=name, namespace=self.namespace)

    async def run_ssh_forward_remove(self):
        """Run the custom_create_port_forward if defined, else run the default one"""
        try:
            if self.ssh_custom_forward_remove:
                port_forward_stop = self.ssh_custom_forward_remove(
                    self, self.port_forward_info
                )
                if inspect.isawaitable(port_forward_stop):
                    await port_forward_stop
            else:
                await self.ssh_default_forward_remove()
        except:
            self.log.exception("Could not cancel port forwarding")
        try:
            if self.ssh_custom_svc_remove:
                ssh_custom_svc_remove = self.ssh_custom_svc_remove(
                    self, self.port_forward_info
                )
                if inspect.isawaitable(ssh_custom_svc_remove):
                    ssh_custom_svc_remove = await ssh_custom_svc_remove
            else:
                await self.ssh_default_svc_remove()
        except:
            self.log.exception("Could not delete port forwarding svc")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._start_future = None
        self.pod_name = self._expand_user_properties(self.pod_name_template)
        self.dns_name = self.dns_name_template.format(
            namespace=self.namespace, name=self.pod_name
        )

    public_api_url = Unicode(
        help="""
        Singleuser servers started remotely may have to use a different api_url than
        the default internal one. This will overwrite `JUPYTERHUB_API_URL` in env.
        Default value is the default internal `JUPYTERHUB_API_URL`
        """,
    ).tag(config=True)

    @default("public_api_url")
    def _public_api_url_default(self):
        if self.hub_connect_url is not None:
            hub_api_url = url_path_join(
                self.hub_connect_url, urlparse(self.hub.api_url).path
            )
        else:
            hub_api_url = self.hub.api_url
        return hub_api_url

    dns_name_template = Unicode(
        "{name}.{namespace}.svc.cluster.local",
        config=True,
        help="""
        Template to use to form the dns name for the pod.
        """,
    )

    pod_name_template = Unicode(
        "jupyter-{username}--{servername}",
        config=True,
        help="""
        Template to use to form the name of user's pods.

        `{username}`, `{userid}`, `{servername}`, `{hubnamespace}`,
        `{unescaped_username}`, and `{unescaped_servername}` will be expanded if
        found within strings of this configuration. The username and servername
        come escaped to follow the `DNS label standard
        <https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names>`__.

        Trailing `-` characters are stripped for safe handling of empty server names (user default servers).

        This must be unique within the namespace the pods are being spawned
        in, so if you are running multiple jupyterhubs spawning in the
        same namespace, consider setting this to be something more unique.

        """,
    )

    namespace = Unicode(
        config=True,
        help="""
        Kubernetes namespace to create services in.
        Default::

          ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
          if os.path.exists(ns_path):
              with open(ns_path) as f:
                  return f.read().strip()
          return "default"
        """,
    )

    @default("namespace")
    def _namespace_default(self):
        """
        Set namespace default to current namespace if running in a k8s cluster

        If not in a k8s cluster with service accounts enabled, default to
        `default`
        """
        ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
        if os.path.exists(ns_path):
            with open(ns_path) as f:
                return f.read().strip()
        return "default"

    def _expand_user_properties(self, template):
        # Make sure username and servername match the restrictions for DNS labels
        # Note: '-' is not in safe_chars, as it is being used as escape character
        safe_chars = set(string.ascii_lowercase + string.digits)

        raw_servername = self.name or ""
        safe_servername = escapism.escape(
            raw_servername, safe=safe_chars, escape_char="-"
        ).lower()

        hub_namespace = self._namespace_default()
        if hub_namespace == "default":
            hub_namespace = "user"

        legacy_escaped_username = "".join(
            [s if s in safe_chars else "-" for s in self.user.name.lower()]
        )
        safe_username = escapism.escape(
            self.user.name, safe=safe_chars, escape_char="-"
        ).lower()
        rendered = template.format(
            userid=self.user.id,
            username=safe_username,
            unescaped_username=self.user.name,
            legacy_escape_username=legacy_escaped_username,
            servername=safe_servername,
            unescaped_servername=raw_servername,
            hubnamespace=hub_namespace,
        )
        # strip trailing - delimiter in case of empty servername.
        # k8s object names cannot have trailing -
        return rendered.rstrip("-")

    def start(self):
        # Wrapper around self._start
        # Can be used to cancel start progress while waiting for it's response
        self._start_future = asyncio.ensure_future(self._start())
        return self._start_future

    async def _start(self):
        self.log.info(
            f"Start singleuser server {self.name}",
            extra={
                "uuidcode": self.name,
                "log_name": self._log_name,
                "user": self.user.name,
            },
        )
        self.call_during_startup = False
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

        if self.internal_ssl:
            for key, path in self.cert_paths.items():
                with open(path, "r") as f:
                    request_body["certs"][key] = f.read()
            for key, path in self.internal_trust_bundles.items():
                with open(path, "r") as f:
                    request_body["internal_trust_bundles"][key] = f.read()

        request_header = await self.get_request_headers()
        url = await self.get_request_url()

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
                "Send Request failed",
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
                    "Could not stop service which failed to start.",
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
        if self.internal_ssl:
            proto = "https://"
        else:
            proto = "http://"

        """
        There are 3 possible scenarios for remote singleuser servers:
        1. Reachable by JupyterHub (e.g. outpost service running on same cluster)
        2. Port forwarding required, and we know the service_address (e.g. outpost service running on remote cluster)
        3. Port forwarding required, but we don't know the service_address yet (e.g. start on a batch system)
        """
        ssh_enabled = self.get_ssh_enabled()
        if ssh_enabled:
            # Case 2: Create port forwarding to service_address given by outpost service.

            # Store port_forward_info, required for port forward removal
            self.port_forward_info = resp_json
            local_bind_address, port = await maybe_future(self.run_ssh_forward())
            service_address = local_bind_address.removeprefix("http://").removeprefix(
                "https://"
            )
            ret = f"{proto}{service_address}:{port}"
        else:
            if not resp_json.get("service", ""):
                # Case 3: service_address not known yet.
                # Wait for service at default address. The singleuser server itself
                # has to call the SetupTunnel API with it's actual location.
                # This will trigger the delayed port forwarding.
                ret = f"{proto}{self.pod_name}:{self.port}"
            else:
                # Case 1: No port forward required, just connect to given service_address
                service_address, port = self.split_service_address(
                    resp_json.get("service")
                )
                ret = f"{proto}{service_address}:{port}"

        # Port may have changed in port forwarding or by remote outpost service.
        self.port = int(port)

        self.log.info(f"Expect JupyterLab at {ret}")
        return ret

    async def poll(self):
        if self.already_stopped:
            # avoid loop with stop
            return 0

        if self.call_during_startup:
            # Only true when called in app.check_spawner() during startup
            ssh_enabled = self.get_ssh_enabled()
            ssh_recreate_at_start = await self.get_ssh_recreate_at_start()
            if ssh_enabled and ssh_recreate_at_start:
                await self.run_ssh_forward(create_svc=False)

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
            if type(e).__name__ == "HTTPClientError" and getattr(e, "code", 500) == 404:
                if self.request_404_poll_keep_running:
                    ret = None
            if self.request_failed_poll_keep_running:
                ret = None
        else:
            ret = resp_json.get("status", None)

        if self.call_during_startup:
            # Only true when called in app.check_spawner() during startup
            self.call_during_startup = False
            if ret != None:
                await self.stop(cancel=True)
                await self.run_post_stop_hook()
        return ret

    async def stop(self, now=False, cancel=False, **kwargs):
        if self.already_stopped:
            # We've already sent a request to the outpost.
            # There's no need to do it again.
            return

        # Prevent multiple requests to the outpost
        self.already_stopped = True

        if cancel:
            # If self._start is still running we cancel it here
            await self.cancel_start_function()

        url = await self.get_request_url(attach_name=True)
        headers = await self.get_request_headers()
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

        # We've implemented a cancel feature, which allows us to call
        # Spawner.stop(cancel=True) and stop the spawn process.
        # Used by api_events.py.
        if cancel:
            await self.cancel()

        if self.port_forward_info:
            await self.run_ssh_forward_remove()

    async def cancel_start_function(self):
        # cancel self._start, if it's running
        if self._start_future and type(self._start_future) is asyncio.Task:
            self.log.warning(f"Start future status: {self._start_future._state}")
            if self._start_future._state in ["PENDING"]:
                try:
                    self._start_future.cancel()
                    await maybe_future(self._start_future)
                except asyncio.CancelledError:
                    pass
        else:
            self.log.warning(f"Start future status: {self._start_future}")

    async def cancel(self):
        try:
            # If this function was, it was called directly in self.stop
            # and not via user.stop. So we want to cleanup the user object
            # as well. It will throw an exception, but we expect the asyncio task
            # to be cancelled, because we've cancelled it ourself.
            await self.user.stop(self.name)
        except asyncio.CancelledError:
            pass

        if type(self._spawn_future) is asyncio.Task:
            if self._spawn_future._state in ["PENDING"]:
                try:
                    self._spawn_future.cancel()
                    await maybe_future(self._spawn_future)
                except asyncio.CancelledError:
                    pass
