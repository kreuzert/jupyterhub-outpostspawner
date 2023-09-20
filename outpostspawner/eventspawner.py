import asyncio
import copy
import json
import os
import re
import time
import traceback
from datetime import datetime

from async_generator import aclosing
from jupyterhub.utils import maybe_future
from jupyterhub.utils import url_path_join
from tornado import web
from traitlets import Callable
from traitlets import Dict
from traitlets import Union

from .outpostspawner import OutpostSpawner


class EventOutpostSpawner(OutpostSpawner):
    """
    A JupyterHub OutpostSpawner that focusses on receiving and showing events
    sent by a JupyterHub outpost service or the singleuser server itself
    a JupyterHub outpost service.
    """

    # Keep track if an event with failed=False was yielded
    _cancel_event_yielded = False

    latest_events = []
    events = {}
    yield_wait_seconds = 1

    def get_state(self):
        """get the current state"""
        state = super().get_state()
        if self.events:
            if type(self.events) != dict:
                self.events = {}
            self.events["latest"] = self.latest_events
            # Clear logs older than 24h or empty logs
            events_keys = copy.deepcopy(list(self.events.keys()))
            for key in events_keys:
                value = self.events.get(key, None)
                if value and len(value) > 0 and value[0]:
                    stime = self._get_event_time(value[0])
                    dtime = datetime.strptime(stime, "%Y_%m_%d %H:%M:%S")
                    now = datetime.now()
                    delta = now - dtime
                    if delta.days:
                        del self.events[key]
                else:  # empty logs
                    del self.events[key]
            state["events"] = self.events
        return state

    def load_state(self, state):
        """load state from the database"""
        super().load_state(state)
        if "events" in state:
            self.events = state["events"]
            if "latest" in self.events:
                self.latest_events = self.events["latest"]

    def clear_state(self):
        """clear any state (called after shutdown)"""
        super().clear_state()
        self._cancel_event_yielded = False

    async def _generate_progress(self):
        """Private wrapper of progress generator

        This method is always an async generator and will always yield at least one event.
        """
        if not self._spawn_pending:
            self.log.warning(
                "Spawn not pending, can't generate progress for %s", self._log_name
            )
            return

        # yield {"progress": 0, "message": "Server requested"}

        async with aclosing(self.progress()) as progress:
            async for event in progress:
                yield event

    async def progress(self):
        spawn_future = self._spawn_future
        next_event = 0

        break_while_loop = False
        while True:
            # Ensure we always capture events following the start_future
            # signal has fired.
            if spawn_future.done():
                break_while_loop = True

            len_events = len(self.latest_events)
            if next_event < len_events:
                for i in range(next_event, len_events):
                    yield self.latest_events[i]
                    if self.latest_events[i].get("failed", False) == True:
                        self._cancel_event_yielded = True
                        break_while_loop = True
                next_event = len_events

            if break_while_loop:
                break
            await asyncio.sleep(self.yield_wait_seconds)

    def _events_url(self, server_name=""):
        """API path for status update endpoint for a server with a given name"""
        url_parts = ["users", "progress", "events", self.user.escaped_name]
        if server_name:
            url_parts.append(server_name)
        return f"{self.public_api_url.rstrip('/')}/{url_path_join(*url_parts)}"

    @property
    def events_url(self):
        return self._events_url(self.name)

    filter_events = Callable(
        allow_none=True,
        default_value=None,
        help="""
        Different JupyterHub outpost services may send different events.
        This filter allows you to unify all events. Should always return a dict.
        If the dict should not be shown return an empty dict.
                
        Example::

            def custom_filter_events(spawner, event):
                event["html_message"] = event.get("message", "No message available")
                return event

            c.EventOutpostSpawner.filter_events = custom_filter_events
        """,
    ).tag(config=True)

    def run_filter_events(self, event):
        if self.filter_events:
            event = self.filter_events(self, event)
        return event

    def get_env(self):
        env = super().get_env()
        env["JUPYTERHUB_EVENTS_URL"] = self.events_url
        return env

    def _get_event_time(self, event):
        # Regex for date time
        pattern = re.compile(
            r"([0-9]+(_[0-9]+)+).*[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,3})?"
        )
        message = event["html_message"]
        match = re.search(pattern, message)
        return match.group()

    cancelling_event = Union(
        [Dict(), Callable()],
        default_value={
            "failed": False,
            "ready": False,
            "progress": 99,
            "message": "",
            "html_message": "JupyterLab is cancelling the start.",
        },
        help="""
        Event shown when singleuser server was cancelled.
        Can be a function or a dict.
        
        This may be a coroutine.
        
        Example::

            from datetime import datetime
            async def cancel_click_event(spawner):
                now = datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
                return {
                    "failed": False,
                    "ready": False,
                    "progress": 99,
                    "message": "",
                    "html_message": f"<details><summary>{now}: Cancelling start ...</summary>We're stopping the start process.</details>",
                }
        
            c.EventOutpostSpawner.cancelling_event = cancel_click_event
        """,
    ).tag(config=True)

    async def _start(self):
        """Catch all exceptions and create a useful self.stop_event."""
        try:
            return await super()._start()
        except Exception as e:
            status_code = getattr(e, "status_code", 500)
            reason = getattr(e, "reason", traceback.format_exc()).replace("\n", "<br>")
            log_message = getattr(e, "log_message", "")
            now = datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
            self.stop_event = {
                "failed": True,
                "ready": False,
                "progress": 100,
                "message": "",
                "html_message": f"<details><summary>{now}: JupyterLab start failed ({status_code}). {log_message}</summary>{reason}</details>",
            }
            self.latest_events.append(self.stop_event)
            # Wait up to 5 times yield_wait_seconds, before sending stop event to frontend
            stopwait = time.monotonic() + 5 * self.yield_wait_seconds
            while time.monotonic() < stopwait:
                if self._cancel_event_yielded:
                    break
                await asyncio.sleep(2 * self.yield_wait_seconds)
            raise e

    async def get_cancelling_event(self):
        """Get cancelling event.
        This event will be shown while cancelling/stopping the server

        Returns:
          cancelling_event (dict)
        """
        if callable(self.cancelling_event):
            cancelling_event = await maybe_future(self.cancelling_event(self))
        else:
            cancelling_event = self.cancelling_event
        return cancelling_event

    stop_event = Union(
        [Dict(), Callable()],
        default_value={
            "failed": True,
            "ready": False,
            "progress": 100,
            "message": "",
            "html_message": "JupyterLab was stopped.",
        },
        help="""
        Event shown when single-user server was stopped.
        """,
    ).tag(config=True)

    async def get_stop_event(self):
        if callable(self.stop_event):
            stop_event = await maybe_future(self.stop_event(self))
        else:
            stop_event = self.stop_event
        return stop_event

    async def run_failed_spawn_request_hook(self, exception):
        now = datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
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
            now = datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
            event = {
                "progress": 100,
                "failed": True,
                "html_message": f"<details><summary>{now}: {summary}</summary>{details}</details>",
            }
            return event

        self.stop_event = _get_stop_event
        await super().run_failed_spawn_request_hook(exception)

    def run_pre_spawn_hook(self):
        """Prepare some variables and show the first event"""
        if self.already_stopped:
            raise Exception("Server is in the process of stopping, please wait.")

        # Save latest events with start event time
        if self.latest_events != []:
            try:
                start_event = self.latest_events[0]
                start_event_time = self._get_event_time(start_event)
                self.events[start_event_time] = self.latest_events
            except:
                self.log.info(
                    f"Could not retrieve latest_events. Reset events list for {self._log_name}"
                )
                self.latest_events = []
                self.events = {}
        self.latest_events = []
        if type(self.events) != dict:
            self.events = {}
        self.events["latest"] = self.latest_events

        now = datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
        start_pre_msg = "Sending request to outpost service to start your service."
        start_event = {
            "failed": False,
            "progress": 30,
            "html_message": f"<details><summary>{now}: {start_pre_msg}</summary>\
                &nbsp;&nbsp;Start {self.name}<br>&nbsp;&nbsp;Options:<br><pre>{json.dumps(self.user_options, indent=2)}</pre></details>",
        }
        self.latest_events = [start_event]

        """Run the pre_spawn_hook if defined"""
        if self.pre_spawn_hook:
            return self.pre_spawn_hook(self)

    def run_post_spawn_request_hook(self, resp_json):
        """If communication was successful, we show this to the user"""
        now = datetime.now().strftime("%Y_%m_%d %H:%M:%S.%f")[:-3]
        progress = 50
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

    async def stop(self, now=False, cancel=False, event=None):
        if self.already_stopped:
            return

        if cancel:
            cancelling_event = await self.get_cancelling_event()
            self.latest_events.append(cancelling_event)
            await self.cancel_start_function()

        try:
            # always use cancel=False, and call the function later if neccessary.
            # Otherwise the stop_event would be attached after run_post_stop_hook was called.
            await super().stop(now, cancel=False)
        finally:
            if not event:
                event = await self.get_stop_event()
            elif callable(event):
                event = await maybe_future(event(self))
            self.latest_events.append(event)

        if cancel:
            await self.cancel()
