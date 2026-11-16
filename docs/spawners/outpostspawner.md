# OutpostSpawner

A JupyterHub spawner that spawns services on remote locations in combination with a JupyterHub Outpost service.

`OutpostSpawner` is a subclass of the [ForwardBaseSpawner](https://github.com/kreuzert/jupyterhub-forwardbasespawner). All options of the `ForwardBaseSpawner` and of JupyterHub's `Spawner` can be used in addition (e.g. `ssh_key`, `ssh_node`, `ssh_enabled`, `public_api_url`, `svc_name_template`, `dns_name_template`, `http_timeout`, `options_form`). The options specific to the `OutpostSpawner` are documented below.

## `request_url`

Type: `str` or `callable` (may be a coroutine)

The URL used to communicate with the JupyterHub Outpost service.

```python
def request_url(spawner, user_options):
    if user_options.get("system", "") == "A":
        return "http://outpost.namespace.svc:8080/services/"
    else:
        return "https://remote-outpost.com/services/"

c.OutpostSpawner.request_url = request_url
```

## `request_headers`

Type: `dict` or `callable` (may be a coroutine)

An optional hook function, or dict, to define the headers used for all requests sent to the JupyterHub Outpost service. They are forwarded directly to the `tornado.httpclient.HTTPRequest` object.

```python
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
```

## `request_kwargs`

Type: `dict` or `callable` (may be a coroutine), default `{}`

An optional hook function, or dict, to define keyword arguments for all requests sent to the JupyterHub Outpost service. They are directly forwarded to the `tornado.httpclient.HTTPRequest` object.

```python
def request_kwargs(spawner, user_options):
    return {
        "request_timeout": 30,
        "connect_timeout": 10,
        "ca_certs": ...,
        "validate_cert": ...,
    }

c.OutpostSpawner.request_kwargs = request_kwargs
```

## `request_kwargs_start`

Type: `dict` or `callable` (may be a coroutine), default `None`

An optional hook function, or dict, to define keyword arguments for the start request sent to the JupyterHub Outpost service. They are directly forwarded to the `tornado.httpclient.HTTPRequest` object. If not defined, `request_kwargs` will be used instead.

## `request_404_poll_keep_running`

Type: `bool`, default `False`

How to handle a 404 response from the Outpost API during a singleuser poll request.

## `request_failed_poll_keep_running`

Type: `bool`, default `True`

How to handle a failed request to the Outpost API during a singleuser poll request.

## `start_async`

Type: `bool` or `callable` (may be a coroutine), default `False`

Whether the start at the Outpost service should run in the background or not.

## `stop_async`

Type: `bool` or `callable` (may be a coroutine), default `False`

Whether the stop at the Outpost service should run in the background or not.

## `custom_poll_interval`

Type: `int` (milliseconds) or `callable` (may be a coroutine), default `0`

An optional hook function to define the poll interval (in milliseconds). This allows to have different intervals for different Outpost services, and can be used to randomize the poll interval for each spawner object.

```python
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
```

## `custom_env`

Type: `dict` or `callable` (may be a coroutine)

An optional hook function, or dict, to add extra environment variables to send to the JupyterHub Outpost service.

```python
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
```

## `custom_user_options`

Type: `dict` or `callable` (may be a coroutine)

An optional hook function, or dict, to add extra `user_options` to send to the JupyterHub Outpost service.

```python
async def custom_user_options(spawner, user_options):
    user_options["image"] = "jupyter/minimal-notebook:latest"
    return user_options

c.OutpostSpawner.custom_user_options = custom_user_options
```

## `custom_misc`

Type: `dict` or `callable` (may be a coroutine)

An optional hook function, or dict, to add extra configurations to send to the JupyterHub Outpost service. This overrides the Spawner configuration set at the Outpost. `key` can be anything you would normally use in your Spawner configuration: `c.OutpostSpawner.<key> = <value>`.

```python
async def custom_misc(spawner, user_options):
    return {
        "image": "jupyter/base-notebook:latest"
    }

c.OutpostSpawner.custom_misc = custom_misc
```

will override the image configured at the Outpost:

```python
c.JupyterHubOutpost.spawner_class = KubeSpawner
c.KubeSpawner.image = "default_image:1.0"
```

and spawn a JupyterLab using the `jupyter/base-notebook:latest` image.

## `custom_misc_disable_default`

Type: `bool`, default `False`

By default, these `misc` options are sent to the Outpost service to override the corresponding values of the Spawner configured at the Outpost. You can disable this behaviour by setting this value to `True`.

Default `custom_misc` options:

```python
extra_labels = await self.get_extra_labels()
custom_misc.update({
  "dns_name_template": self.dns_name_template,
  "pod_name_template": self.svc_name_template,
  "internal_ssl": self.internal_ssl,
  "ip": "0.0.0.0",
  "port": self.port,
  "services_enabled": True,
  "extra_labels": extra_labels
})
```

## `check_allowed`

Type: `callable` (may be a coroutine)

An optional hook function you can implement to double check if the given `user_options` allow a start. If the start is not allowed, it should raise an exception.

```python
def custom_check_allowed(spawner, user_options):
    if not user_options.get("allowed", True):
        raise Exception("This is not allowed")

c.OutpostSpawner.check_allowed = custom_check_allowed
```

## `failed_spawn_request_hook`

Type: `callable` (may be a coroutine)

An optional hook function you can implement to handle a failed start attempt. This will be called if the POST request to the Outpost service was not successful.

```python
def custom_failed_spawn_request_hook(spawner, exception_thrown):
    ...

c.OutpostSpawner.failed_spawn_request_hook = custom_failed_spawn_request_hook
```

## `post_spawn_request_hook`

Type: `callable` (may be a coroutine)

An optional hook function you can implement to handle a successful start attempt. This will be called if the POST request to the Outpost service was successful.

```python
def post_spawn_request_hook(spawner, resp_json):
    ...

c.OutpostSpawner.post_spawn_request_hook = post_spawn_request_hook
```

## `additional_cafile`

Type: `str` or `callable` (may be a coroutine), default `None`

Additional certificate authorities can be added. Required if `JUPYTERHUB_API_URL` is an external URL and `c.JupyterHub.internal_ssl` is `True`.

## `ssl_alt_names`

Type: `list` or `callable` (may be a coroutine), default `[]`

List of SSL alternative names. May be set in config if all spawners should have the same value(s), or set at runtime by a Spawner that knows its names.

## `collect_logs`

Type: `bool`, default `False`

Whether to collect logs when stopping the service.

## `collect_logs_polling`

Type: `bool`, default `False`

Whether to collect logs when polling the service.

## `http_client_defaults`

Type: `dict`, default `{}`

Default keyword arguments for the shared HTTP client used to communicate with the Outposts.
