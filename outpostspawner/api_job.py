import asyncio
import inspect
import json

from jupyterhub.apihandlers import APIHandler
from jupyterhub.apihandlers import default_handlers
from jupyterhub.scopes import needs_scope
from jupyterhub.utils import url_path_join
from tornado import web
from traitlets import Callable
from traitlets import Dict
from traitlets import Integer
from traitlets.config import Configurable

from .misc import generate_random_id


class JobAPIHandlerConfig(Configurable):
    job_server_limit_per_user = Integer(
        default_value=10,
        config=True,
        help="Maximum number of job servers allowed per user at the same time.",
    )

    poll_interval = Integer(
        default_value=10,
        config=True,
        help="Interval (in seconds) at which to poll the job status and logs",
    )

    job_timeout = Integer(
        default_value=3600,
        config=True,
        help="Maximum time (in seconds) to allow a job to run before it is automatically stopped",
    )

    default_user_options = Dict(
        default_value={},
        config=True,
        help="Default options to use when starting a server via the /api/job endpoint",
    )

    prepare_job = Callable(
        default_value=None,
        config=True,
        allow_none=True,
        help="""Optional callback function to be called before spawning the job. The function should be a coroutine and accept two arguments: the JobAPIHandlerConfig instance and the spawner instance. This can be used to perform any necessary setup before the job is spawned, such as preparing resources or validating options.
        """,
    )

    def get_script(self, notebook_dirs=[]):
        script = r"""
set -euo pipefail

if ! command -v papermill >/dev/null 2>&1; then
    python3 -m pip install --user papermill >/dev/null 2>&1 || {
        echo "$(date) - pip install papermill failed" >&2
        exit 1
    }
    PAPERMILL="${HOME}/.local/bin/papermill"
else
    PAPERMILL="$(command -v papermill)"
fi

JHUB_LOG=$(mktemp)
READY_TIMEOUT=60
READY_INTERVAL=1
JOB_PREFIX="${JUPYTERHUB_SERVICE_PREFIX/\/user\//\/job\/}"
JOB_PREFIX="${JOB_PREFIX%/}"
URL="${JUPYTERHUB_API_URL}${JOB_PREFIX}?delete=false"
jupyterhub-singleuser > "$JHUB_LOG" 2>&1 &

elapsed=0
while true; do
    STATUS=$(curl -ks -H "Authorization: token ${JUPYTERHUB_API_TOKEN}" "$URL" | sed 's/.*"status":[ ]*"\([^"]*\)".*/\1/')
    case "$STATUS" in
        spawning)
            sleep "$READY_INTERVAL"
            ;;
        running)
            break
            ;;
        stopped)
            echo "$(date) - Server stopped unexpectedly"
            [ -f $JHUB_LOG ] && cat "$JHUB_LOG" >&2
            exit 1
            ;;
        *)
            sleep "$READY_INTERVAL"
            ;;
    esac
    
    elapsed=$((elapsed + READY_INTERVAL))
    if [ "$elapsed" -ge "$READY_TIMEOUT" ]; then
        echo "$(date) - Timeout waiting for server to be ready" >&2
        [ -f $JHUB_LOG ] && cat "$JHUB_LOG" >&2
        exit 2
    fi
done

echo ""
echo "Papermill Job started"
python3 - <<EOF
import json, subprocess, pathlib, sys, tempfile, os

home_env = os.environ.get("HOME", "/home/jovyan")
papermill = os.environ.get("PAPERMILL", f"{home_env}/.local/bin/papermill")
results = []
global_exit = 0

notebook_dirs = <replace_notebook_dirs>
if not notebook_dirs:
    notebook_dirs = [home_env]
if type(notebook_dirs) == str:
    notebook_dirs = [notebook_dirs]

seen = set()

for dir_str in notebook_dirs:
    dir = pathlib.Path(dir_str)
    base = dir.resolve()
    if not base.exists() or not base.is_dir():
        results.append({
            "notebook": dir_str,
            "exitCode": -1,
            "stdout": f"Directory {dir_str} does not exist or is not a directory",
        })
        global_exit = 1
        continue
    for nb in sorted(
        p for p in dir.rglob("*.ipynb")
        if ".ipynb_checkpoints" not in p.parts
    ):
        try:
            nb_resolved = nb.resolve()
            if nb_resolved in seen:
                continue
            seen.add(nb_resolved)
            
            out_nb = pathlib.Path(tempfile.mkdtemp()) / nb.name
            proc = subprocess.run(
                [papermill, str(nb), str(out_nb)],
                capture_output=True,
                text=True
            )

            if proc.returncode != 0:
                global_exit = 1

            results.append({
                "notebook": str(nb),
                "exitCode": proc.returncode,
                "stdout": proc.stdout + proc.stderr,
            })
        except Exception as e:
            results.append({
                "notebook": str(nb),
                "exitCode": -1,
                "stdout": str(e),
            })

json.dump(
    {
        "exitCode": global_exit,
        "results": results,
    },
    sys.stdout,
    indent=2
)
EOF
echo ""
echo "Papermill Job completed"
"""
        script = script.replace("<replace_notebook_dirs>", json.dumps(notebook_dirs))
        return script


task_references = set()


class JobAPIHandler(APIHandler):
    def merge_user_options(self, user_options, default_options):
        for key, value in default_options.items():
            if key not in user_options:
                user_options[key] = value
            else:
                if isinstance(value, dict) and isinstance(user_options[key], dict):
                    self.merge_user_options(user_options[key], value)
        return user_options

    async def run_job_prepare(self, config, request, spawner):
        if config.prepare_job is not None:
            _job_prepare_future = config.prepare_job(request, spawner)
            if inspect.isawaitable(_job_prepare_future):
                await _job_prepare_future

    @needs_scope("servers")
    async def post(self):
        user = self.current_user
        if not user:
            raise web.HTTPError(403)

        if not self.allow_named_servers and user.running:
            raise web.HTTPError(
                400,
                reason="User already has a running server, and named servers are not allowed.",
            )

        named_server_limit_per_user = await self.get_current_user_named_server_limit()

        if named_server_limit_per_user > 0:
            named_spawners = list(user.all_spawners(include_default=False))
            if named_server_limit_per_user <= len(named_spawners):
                raise web.HTTPError(
                    400,
                    f"User {user.name} already has the maximum of {named_server_limit_per_user} named servers."
                    "  One must be deleted before a new server can be created",
                )

        config = JobAPIHandlerConfig(config=self.config)
        running_jobs = 0
        for spawner in user.all_spawners():
            if getattr(spawner, "_is_job", False):
                if (
                    spawner.active
                    or getattr(spawner, "_job_prepare_status", None) is not None
                ):
                    running_jobs += 1

        if running_jobs >= self.config.job_server_limit_per_user:
            raise web.HTTPError(
                400,
                f"User {user.name} already has the maximum of {config.job_server_limit_per_user} running jobs."
                " One must be completed before a new job can be started",
            )
        try:
            body = json.loads(self.request.body)
        except json.JSONDecodeError:
            body = {}
        user_options = body.get("user_options", {})
        notebook_dirs = body.get("notebook_dirs", [])

        user_options = self.merge_user_options(
            user_options, config.default_user_options
        )
        if "option" not in user_options:
            raise web.HTTPError(400, reason="Missing 'option' in user_options")
        user_options["profile"] = user_options["option"]

        server_name = generate_random_id()

        spawner = user.get_spawner(server_name, replace_failed=True)
        script = config.get_script(notebook_dirs=notebook_dirs)

        job_custom_misc = {"cmd": ["/bin/bash", "-lc"], "args": script}
        spawner_custom_misc = spawner.custom_misc or {}
        spawner_custom_misc.update(job_custom_misc)
        spawner.custom_misc = spawner_custom_misc

        spawner.collect_logs = True
        spawner.collect_logs_polling = True
        spawner._is_job = True
        spawner.user_options = user_options
        spawner.orm_spawner.user_options = user_options
        spawner.custom_poll_interval = config.poll_interval
        self.db.add(spawner.orm_spawner)
        self.db.commit()

        async def spawn_and_cleanup():
            async def _full_stop():
                if server_name in user.spawners:
                    if spawner.active:
                        spawner.log.info(
                            f"{spawner._log_name} - Job stop",
                            extra={"action": "job_stop"},
                        )
                        spawner.stop_polling()
                        await user.stop(server_name)
                    if spawner.orm_spawner is not None:
                        self.db.delete(spawner.orm_spawner)
                    user.spawners.pop(server_name, None)
                    self.db.commit()

            async def _spawn():
                try:
                    await user.spawn(server_name)
                except Exception as e:
                    self.log.error(f"{spawner._log_name} - Error spawning job: {e}")
                    spawner.logs = [f"Error spawning job: {e}"]
                    spawner.exit_code = -1
                    spawner._job_prepare_status = "stopped"
                else:
                    spawner._job_prepare_status = None
                    await asyncio.sleep(config.job_timeout)
                    await _full_stop()

            spawner._job_prepare_status = "preparing"
            try:
                await self.run_job_prepare(config, self.request, spawner)
            except Exception as e:
                self.log.error(f"{spawner._log_name} - Error in job preparation: {e}")
                spawner._job_prepare_status = "stopped"
                if not spawner.logs:
                    spawner.logs = [f"Error in job preparation: {e}"]
                if spawner.exit_code is None or spawner.exit_code == 0:
                    spawner.exit_code = -1
            else:
                await _spawn()

        spawner.log.info(
            f"{spawner._log_name} - Job start",
            extra={
                "action": "job_start",
                "user_options": user_options,
                "notebook_dirs": notebook_dirs,
            },
        )
        task = asyncio.create_task(spawn_and_cleanup())
        task_references.add(task)
        task.add_done_callback(task_references.discard)
        ret = url_path_join(
            f"{self.request.protocol}://{self.request.host}",
            self.hub.base_url,
            "api/job",
            user.name,
            server_name,
        )
        self.write(ret)
        self.set_header("Location", ret)
        self.set_status(201)

    def reduce_logs(self, loglines):
        collect = False
        between_lines = []

        for line in loglines:
            if line == "Papermill Job started":
                collect = True
                continue
            if line == "Papermill Job completed":
                collect = False
                break
            if collect:
                between_lines.append(line)
        return between_lines

    @needs_scope("access:servers")
    async def get(self, user_name, server_name):
        user = self.current_user
        if not user:
            raise web.HTTPError(403)

        if server_name not in user.orm_user.orm_spawners:
            raise web.HTTPError(404)

        spawner = user.get_spawner(server_name)
        if not spawner:
            raise web.HTTPError(404)

        async def _remove_spawner():
            if spawner.active:
                spawner.log.info(
                    f"{spawner._log_name} - Job stop", extra={"action": "job_stop"}
                )
                spawner.stop_polling()
                await user.stop(server_name)
            await user._delete_spawner(spawner)
            if spawner.orm_spawner is not None:
                self.db.delete(spawner.orm_spawner)
            user.spawners.pop(server_name, None)
            self.db.commit()

        logs = []
        exit_code = None
        status = None
        if not spawner.active and spawner._job_prepare_status is not None:
            status = spawner._job_prepare_status
        else:
            status = (
                "stopped"
                if not spawner.active
                else "running"
                if spawner.ready
                else "spawning"
            )
        if status == "running" or status == "stopped":
            logs = spawner.logs
            exit_code = spawner.exit_code
        if len(logs) > 0 and logs[-1] == "Papermill Job completed":
            logs = self.reduce_logs(logs)
            status = "stopped"
            exit_code = 0
        if status == "stopped":
            if (
                self.request.query_arguments.get("delete", [b"true"])[0]
                .decode()
                .lower()
                == "true"
            ):
                task = asyncio.create_task(_remove_spawner())
                task_references.add(task)
                task.add_done_callback(task_references.discard)
        spawner.log.debug(
            f"{spawner._log_name} - Job poll",
            extra={"action": "job_poll", "status": status, "exit_code": exit_code},
        )
        self.write(
            {
                "status": status,
                "logs": logs,
                "exit_code": exit_code,
            }
        )
        self.set_status(200)


default_handlers.append((r"/api/job", JobAPIHandler))
default_handlers.append((r"/api/job/([^/]+)", JobAPIHandler))
default_handlers.append((r"/api/job/([^/]+)/([^/]+)", JobAPIHandler))
