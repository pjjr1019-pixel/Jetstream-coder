"""Loopback-only operator console and control API."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from pathlib import Path
from typing import Any
from aiohttp import web

from .security import OperatorCredential

from .config import save_config, validate_config
from .permissions import PermissionManager
from .runtime_paths import pad_data, root as isolated_root

MAX_BODY = 128 * 1024
LOG = logging.getLogger("musebridge.operator")


class OperatorServer:
    def __init__(self, bridge, config_path: Path, port: int = 8766):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Operator port must be between 0 and 65535")
        self.bridge = bridge
        self.config_path = config_path
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.server: web.AppRunner | None = None
        self.credential = OperatorCredential(bridge.data_dir / "operator.secret")
        self._control_lock = asyncio.Lock()
        self.shutdown_requested = asyncio.Event()
        self.restart_requested = False

    async def start(self) -> None:
        if self.server is not None:
            return
        app = web.Application(client_max_size=MAX_BODY, middlewares=[self._guard])
        app.add_routes([web.get("/", self._index), web.post("/api/pair", self._pair),
                        web.get("/api/state", self._state), web.post("/api/action", self._perform_http)])
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self.server = runner
        self.port = runner.addresses[0][1]
        self.origin = f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.cleanup()
            self.server = None

    def authenticated(self, request) -> bool:
        authorization = request.headers.get("Authorization", "")
        return (authorization.startswith("Bearer ") and self.credential.accepts(authorization[7:])) or self.credential.accepts_session(request.cookies.get("musebridge_operator", ""))

    @web.middleware
    async def _guard(self, request, handler):
        if request.headers.get("Host") != f"127.0.0.1:{self.port}":
            return web.json_response({"error": "Invalid local host", "code": "invalid_host"}, status=400)
        if request.method not in ("GET", "HEAD") and request.headers.get("Origin") != self.origin:
            return web.json_response({"error": "Same-origin local request required", "code": "invalid_origin"}, status=403)
        if request.path.startswith("/api/") and request.path != "/api/pair" and not self.authenticated(request):
            return web.json_response({"error": "Pair this console using musebridge console", "code": "operator_auth_required"}, status=401)
        try:
            response = await handler(request)
        except (ValueError, KeyError, TypeError) as exc:
            response = web.json_response({"error": str(exc)[:500], "code": getattr(exc, "code", "invalid_request")}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason, "code": "http_error"}, status=exc.status)
        except Exception:
            LOG.exception("Operator action failed")
            response = web.json_response({"error": "Operator action failed", "code": "internal_error"}, status=500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
        return response

    async def _index(self, request):
        return web.Response(text=self._page(), content_type="text/html")

    async def _pair(self, request):
        value = await request.json()
        if not isinstance(value, dict) or not self.credential.accepts(value.get("token", "")):
            return web.json_response({"error": "Invalid pairing credential", "code": "pairing_failed"}, status=401)
        response = web.json_response({"ok": True})
        response.set_cookie("musebridge_operator", self.credential.issue_session(), httponly=True,
                            samesite="Strict", max_age=8*3600, path="/")
        return response

    async def _state(self, request):
        if "after" in request.query:
            await self.bridge.wait_for_state(int(request.query["after"]))
        state = self.bridge.operator_state(include_audit=False)
        if request.query.get("compact") != "1":
            state["audit"] = await asyncio.to_thread(self.bridge.audit.recent, 200, request.query.get("q", ""))
        return web.json_response(state)

    async def _perform_http(self, request):
        value = await request.json()
        result = await self._action(value)
        self.bridge.state_changed()
        return web.json_response({"ok": True, "result": result})

    async def _action(self, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("action"), str):
            raise ValueError("Invalid operator action")
        if value["action"] in {"offline_actions_enabled", "start", "stop", "kill", "configure", "desktop_control", "clear_stop", "revoke_day_authorization", "reconnect", "stop_all", "session", "rotate_bridge_token", "stream_arm_local", "stream_release_local"}:
            async with self._control_lock:
                return await self._perform_action(value)
        return await self._perform_action(value)

    async def _perform_action(self, value: dict[str, Any]) -> Any:
        action = value["action"]
        if isolated_root() is not None and (action.startswith('embedded_') or action.startswith('stream_')):
            raise ValueError('JettPad connection and streaming controls are unavailable in Deskhand')
        if action in {'clear_stop', 'executor_submit_local', 'executor_capture_local', 'assistant_window_list_local',
                      'offline_read', 'offline_app'} or (action == 'desktop_control' and value.get('enabled') is True):
            from .host_stop import ensure_host_allows
            ensure_host_allows()
        if action=='embedded_options_local':
            if set(value)!={'action'}:raise ValueError('Unsupported connection setup fields')
            from companion_data import paired_certificates
            return {'paired_fingerprints':sorted((await asyncio.to_thread(paired_certificates)).keys())}
        if action in ('embedded_status_local','embedded_begin_local','embedded_stop_local'):
            lifecycle=getattr(self.bridge,'embedded_lifecycle',None)
            if lifecycle is None:raise ValueError('Built-in connection setup is unavailable')
            if action=='embedded_status_local':
                if set(value)!={'action'}:raise ValueError('Unsupported connection status fields')
                return lifecycle.status_local()
            if action=='embedded_stop_local':
                if set(value)!={'action'}:raise ValueError('Unsupported connection stop fields')
                return await lifecycle.stop_local()
            if set(value)!={'action','binding','local_approval'} or value['local_approval'] is not True:
                raise ValueError('Explicit local connection approval is required')
            return await lifecycle.begin_local(value['binding'],locally_approved=True)
        if action=='stream_status_local':
            if set(value)!={'action'}:raise ValueError('Unsupported stream status fields')
            if not self.bridge.stream_stop.status()['available']:return self.bridge.stream_stop.status()
            from sunshine.host.local_adapter import status
            return status(self.bridge)
        if action=='stream_arm_local':
            if not self.bridge.stream_stop.status()['available']:raise ValueError('Streaming safety is unavailable')
            from sunshine.host.local_adapter import arm
            result=await asyncio.to_thread(arm,self.bridge,value)
            await self.bridge._audit('stream.arm_local','verified staged Sunshine',True,'Local privacy acknowledgement; no grants changed')
            return result
        if action=='stream_release_local':
            if set(value)!={'action'}:raise ValueError('Unsupported stream release fields')
            self.bridge.stream_stop.release_local()
            await self.bridge._audit('stream.off_local','verified staged Sunshine',True,'Local stream off; no grants changed')
            return self.bridge.stream_stop.status()
        if action=='offline_read':
            if set(value)!={'action','request_id'}:raise ValueError('Unsupported offline read fields')
            from .local_reads import LocalReadBroker
            import os
            return await asyncio.to_thread(LocalReadBroker(self.bridge,pad_data()).execute,value['request_id'])
        if action=='offline_actions_enabled':
            if set(value)!={'action','enabled'} or type(value['enabled']) is not bool:raise ValueError('enabled must be boolean')
            from .local_actions import LocalAppBroker
            import os,hashlib
            broker=LocalAppBroker(self.bridge,pad_data())
            policy=broker.policy();policy['enabled']=value['enabled']
            raw=json.dumps(policy,indent=2).encode('utf-8');path=self.bridge.data_dir/'offline-actions.json'
            temp=path.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(path)
            path.with_suffix('.sha256').write_text(hashlib.sha256(raw).hexdigest(),encoding='ascii')
            await self.bridge._audit('offline.actions.toggle','local operator',True,str(value['enabled']))
            return {'enabled':value['enabled']}
        if action == 'offline_app':
            if set(value)!={'action','request_id'}:raise ValueError('Unsupported offline action fields')
            self.bridge.permissions.check_session('desktop')
            from .local_actions import LocalAppBroker
            import os
            broker=LocalAppBroker(self.bridge,pad_data())
            return await asyncio.to_thread(broker.execute,value['request_id'])
        if action == 'away_mode':
            if set(value)-{'action','active','window_id'}:raise ValueError('Invalid Away payload')
            return await self.bridge.away.set(value.get('active'),value.get('window_id'))
        if action == "get_capabilities":
            # Read-only: identical builder used by the authenticated bridge.
            return self.bridge.capabilities()
        if action == 'assistant_window_list_local':
            if set(value) != {'action'}:raise ValueError('Window listing takes no parameters')
            self.bridge.permissions.check_session('desktop')
            self.bridge.desktop._ensure_authority('desktop_control')
            result=await self.bridge._local_operation(self.bridge._desktop_lock,self.bridge.desktop.list_windows)
            for row in result['windows']:
                await self.bridge._audit('window.list',row['process_name'],True,json.dumps({k:row.get(k) for k in ('window_id','title','protected','protection_reason')}))
            self.bridge.permissions.check_session('desktop')
            self.bridge.desktop._ensure_authority('desktop_control')
            return result
        if action == 'executor_submit_local':
            # This remains behind the authenticated operator API. The same
            # coordinator applies the original STOP, scope and session gates.
            if set(value) != {'action','request_id','task'}:raise ValueError('Use a request ID and task')
            import uuid
            request_id = value['request_id']
            try:
                if str(uuid.UUID(request_id)) != request_id:raise ValueError()
            except (ValueError,TypeError,AttributeError):raise ValueError('Invalid request ID') from None
            return await asyncio.to_thread(self.bridge.executor.submit,request_id,value['task'])
        if action == 'executor_capture_local':
            if set(value) != {'action','id','step'}:raise ValueError('Use a job ID and screenshot step')
            from .executor_capture_local import read_capture
            return await asyncio.to_thread(read_capture,self.bridge,value['id'],value['step'])
        if action=='executor_status':
            if set(value)=={'action'}:return self.bridge.executor.status()
            if set(value)!={'action','id'}:raise ValueError('Use an executor job id')
            return self.bridge.executor.get(value['id'])
        if action=='executor_cancel':
            if set(value)!={'action','id'}:raise ValueError('Use an executor job id')
            return self.bridge.executor.cancel(value['id'])
        if action=='revoke_day_authorization':
            if set(value)!={'action'}:raise ValueError('Local revoke accepts no fields')
            self.bridge.desktop.revoke(reason='local_day_authorization_revoke')
            save_config(self.config_path,self.bridge.config)
            await self.bridge._audit('control.day_authorization','local operator',True,'revoked locally')
            await self.bridge.journal_grants('local_day_authorization_revoke')
            return self.bridge.operator_state()
        if action=='clear_stop':
            # STOP latch removed (Phil 2026-11-09): there is no latch to clear.
            # Harmless no-op: report the latch as absent; write no STOP state.
            # The host STOP event is still validated by _perform_action before
            # this branch runs.
            await self.bridge._audit('desktop.control','local clear STOP (no-op; STOP latch removed)',True)
            await self.bridge.journal_grants('local_clear_stop')
            return self.bridge.operator_state()
        if action in {"shutdown", "restart"}:
            self.restart_requested = action == "restart"
            asyncio.get_running_loop().call_later(.1, self.shutdown_requested.set)
            return {"shutdown_requested": True, "restart_requested": self.restart_requested}
        if action == "stop_all":
            await self.bridge.emergency_stop()
            return {"stopped": True}
        if action == "session":
            self.bridge.permissions.set_session(value.get("profile"), value.get("minutes", 60))
            self.bridge.desktop.revoke()
            await self.bridge.journal_grants("local_session_profile_set_with_expiry")
            running = list(self.bridge._request_tasks)
            for task in running: task.cancel()
            await self.bridge.file_worker.close()
            await self.bridge.tasks.close()
            if running: await asyncio.gather(*running, return_exceptions=True)
            await self.bridge._audit("permissions.session", str(value.get("profile")), True)
            return {"profile": self.bridge.permissions.session_profile, "expires_at": self.bridge.permissions.session_expires_at}
        if action == "rotate_bridge_token":
            import os
            from .config import generate_token
            if value.get("confirmation") != "ROTATE MUSE TOKEN":
                raise ValueError("Type ROTATE MUSE TOKEN to disconnect the old credential")
            if os.environ.get("MUSEBRIDGE_TOKEN"):
                raise ValueError("Remove the environment token override before rotating the stored credential")
            running = self.bridge.server is not None
            await self.bridge.stop()
            self.bridge.token = self.bridge.config["token"] = generate_token()
            save_config(self.config_path, self.bridge.config)
            if running:
                await self.bridge.start()
            await self.bridge._audit("auth.rotate", "remote credential", True)
            return {"rotated": True, "re_pair_required": True}
        if action == "ping_muse":
            return await self.bridge.ping_muse()
        if action == "start":
            await self.bridge.set_enabled(True)
            save_config(self.config_path, self.bridge.config)
            return self.bridge.operator_state()
        if action in ("stop", "kill"):
            try:
                await self.bridge.set_enabled(False)
            finally:
                save_config(self.config_path, self.bridge.config)
            return self.bridge.operator_state()
        if action == "desktop_control":
            enabled = value.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("enabled must be boolean")
            if enabled and (not self.bridge.config["features"]["desktop_control"] or not self.bridge.connected):
                raise ValueError("Enable the feature and connect Muse before desktop control")
            if enabled:
                self.bridge.remote_control.revoked(reason='local_clear_stop',preserve_day=True)
                if self.bridge.desktop.indicator:self.bridge.desktop.indicator.set_remote(False)
                resume=value.get("resume_on_reconnect",True)
                if type(resume) is not bool:raise ValueError("resume_on_reconnect must be boolean")
                # B2-E (B-11): honour the caller's choice — previously the
                # value was validated and then ignored (always True), so an
                # operator could never decline resume-on-reconnect.
                self.bridge.config["desktop_resume_approved"]=bool(resume)
                self.bridge.desktop.connected=self.bridge.connected
                self.bridge.desktop.enable()
                self.bridge.executor.resume_admissions()
            else:
                self.bridge.config['features']['desktop_control']=False
                self.bridge.config['desktop_resume_approved']=False
                self.bridge.desktop.revoke(preserve_approval=True,reason='local_operator_disabled')
            save_config(self.config_path,self.bridge.config)
            await self.bridge._audit("desktop.control", "operator toggle", True,
                                     "enabled" if enabled else "disabled")
            await self.bridge.journal_grants("local_operator_approved" if enabled else "local_operator_revoked")
            return {"enabled": enabled}
        if action == "configure":
            return await self._configure(value)
        if action == "export_audit":
            return {"json": self.bridge.audit.export()}
        if action == "connection_details":
            if self.bridge.server is None:
                raise ValueError("Connect MuseBridge before copying connection details")
            bind = self.bridge.config["bind"]
            host = f"[{bind}]" if ":" in bind else bind
            # B2-E (B-14): use the actual bound port when configured port is
            # 0 (OS-assigned); connection details are only served post-start.
            port = getattr(self.bridge, "listener_port", None) or self.bridge.config.get("port", 8765)
            return {
                "url": f"ws://{host}:{port}/v1/bridge",
                "token": self.bridge.token,
            }
        if action == "reconnect":
            if self.bridge.server is None:
                raise ValueError("Start the  bridge before reconnecting")
            client, self.bridge.client = self.bridge.client, None
            self.bridge.desktop.disconnect()
            if client is not None:
                await client.close(1000, "Reconnect requested by local operator")
            return {"reconnecting": True}
        raise ValueError("Unsupported operator action")

    async def _configure(self, value: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "enabled", "bind", "port", "tier", "scopes", "secret_overrides",
            "features", "desktop_apps", "rate_limits", "max_task_budget_secs",
            "whole_drive_warning_accepted",
            "chat_timeout_secs", "retain_chat_text", "desktop_app_paths", "require_frame_id", "execution_mode", "peer_name", "peer_addresses",
        }
        if set(value) - (fields | {"action", "confirmation", "confirmations"}):
            raise ValueError("Unsupported configuration fields")
        candidate = dict(self.bridge.config)
        for key in fields.intersection(value):
            candidate[key] = value[key]
        if candidate.get("tier") == 2:
            if (self.bridge.config.get("tier") != 2
                    and value.get("confirmation") != "ENABLE WHOLE DRIVE"):
                raise ValueError("Type ENABLE WHOLE DRIVE to grant whole-drive access")
            candidate["whole_drive_warning_accepted"] = True
        confirmations = value.get("confirmations", {})
        if not isinstance(confirmations, dict):
            raise ValueError("Secret override confirmations must be an object")
        for key, setting in candidate.get("secret_overrides", {}).items():
            if setting is True and self.bridge.config.get("secret_overrides", {}).get(key) is not True:
                if confirmations.get(key) != f"EXPOSE {key.upper()}":
                    raise ValueError(f"Secret override {key} needs its own explicit confirmation")
        previous_exec_roots = self.bridge.permissions.exec_scope_roots()
        candidate_exec_roots = PermissionManager(candidate, self.config_path).exec_scope_roots()
        expanded_exec_scope = any(
            not any(
                root == previous or root.is_relative_to(previous)
                for previous in previous_exec_roots
            )
            for root in candidate_exec_roots
        )
        if (candidate["tier"] != 2 and expanded_exec_scope
                and confirmations.get("exec_scope") != "ENABLE UNSANDBOXED EXECUTION"):
            raise ValueError("Adding an exec scope needs explicit local confirmation")
        for feature, phrase in (
            ("exec", "ENABLE UNSANDBOXED EXECUTION"),
            ("screenshots", "ENABLE SCREENSHOTS"),
            ("desktop_control", "ENABLE DESKTOP CONTROL"),
        ):
            if (candidate.get("features", {}).get(feature) is True
                    and self.bridge.config.get("features", {}).get(feature) is not True
                    and confirmations.get(feature) != phrase):
                raise ValueError(f"Enabling {feature} needs explicit local confirmation")
        if (set(candidate.get("desktop_apps", [])) - set(self.bridge.config.get("desktop_apps", []))
                and confirmations.get("desktop_apps") != "TRUST SCREEN CONTENT"):
            raise ValueError("Adding trusted capture apps needs explicit content-trust confirmation")
        if set(candidate.get("desktop_app_paths", [])) - set(self.bridge.config.get("desktop_app_paths", [])):
            if confirmations.get("desktop_apps") != "TRUST SCREEN CONTENT":
                raise ValueError("Adding trusted capture paths needs explicit content-trust confirmation")
        if candidate.get("require_frame_id") is False and self.bridge.config.get("require_frame_id") is not False:
            if confirmations.get("legacy_input") != "ALLOW LEGACY INPUT":
                raise ValueError("Disabling frame IDs needs explicit local confirmation")
        if candidate.get("execution_mode") == "host" and self.bridge.config.get("execution_mode") != "host":
            if confirmations.get("exec") != "ENABLE UNSANDBOXED EXECUTION":
                raise ValueError("Host execution needs explicit local acknowledgement")
        # B2-E (B-11): enabling desktop_control here is itself a local
        # gesture (it needs the ENABLE DESKTOP CONTROL confirmation above),
        # so it records resume approval; disabling it clears resume approval.
        if candidate.get("features", {}).get("desktop_control") is True and self.bridge.config.get("features", {}).get("desktop_control") is not True:
            candidate["desktop_resume_approved"] = True
        if candidate.get("features", {}).get("desktop_control") is not True:
            candidate["desktop_resume_approved"] = False
        candidate = validate_config(candidate)
        was_enabled = self.bridge.server is not None
        old = self.bridge.config
        should_run = candidate["enabled"] and candidate["tier"] > 0
        endpoint_changed = (
            candidate.get("bind") != old.get("bind")
            or candidate.get("port") != old.get("port")
        )
        permissions_changed = any(candidate.get(key) != old.get(key) for key in
            ("tier", "scopes", "features", "secret_overrides", "desktop_apps", "desktop_app_paths", "peer_addresses"))
        needs_stop = was_enabled and (not should_run or endpoint_changed or permissions_changed)
        if needs_stop:
            await self.bridge.stop()
        try:
            with self.bridge.desktop._lock:
                # STOP latch removed: no STOP state exists to preserve.
                self.bridge.config = candidate
                self.bridge.desktop.config = candidate
            self.bridge.permissions.config = candidate
            self.bridge.runner.config = candidate
            self.bridge.desktop.config = candidate
            self.bridge.store.retain_chat_text = candidate.get("retain_chat_text", False)
            if not candidate["features"]["desktop_control"]:
                self.bridge.desktop.revoke(preserve_approval=True,reason='local_operator_disabled')
            if should_run and self.bridge.server is None:
                await self.bridge.start()
            save_config(self.config_path, candidate)
            await self.bridge.journal_grants("local_configuration_saved")
            self.bridge.last_error = None
            changed = [
                key for key in (
                    "tier", "scopes", "features", "secret_overrides", "bind", "port",
                    "rate_limits", "desktop_apps", "max_task_budget_secs",
                    "chat_timeout_secs", "retain_chat_text", "desktop_app_paths", "require_frame_id", "execution_mode", "peer_name", "peer_addresses",
                )
                if old.get(key) != candidate.get(key)
            ]
            if changed:
                await self.bridge._audit("permissions.changed", ",".join(changed), True)
        except Exception as exc:
            self.bridge.last_error = f"Configuration failed ({type(exc).__name__})"
            if self.bridge.server is not None:
                await self.bridge.stop()
            self.bridge.config = old
            self.bridge.permissions.config = old
            self.bridge.runner.config = old
            self.bridge.desktop.config = old
            self.bridge.store.retain_chat_text = old.get("retain_chat_text", False)
            if was_enabled and old.get("enabled") and old.get("tier") > 0 and self.bridge.server is None:
                try:
                    await self.bridge.start()
                except Exception as restore_error:
                    self.bridge.last_error = f"Bridge restart failed ({type(restore_error).__name__})"
                    LOG.error("Could not restore bridge after a rejected configuration change: %s",
                              type(restore_error).__name__)
            LOG.error("Could not apply local operator configuration (%s)", type(exc).__name__)
            if isinstance(exc, OSError) and exc.errno == errno.EADDRNOTAVAIL:
                message = (
                    f"{candidate.get('bind')} is not assigned to this PC. Enter this "
                    "PC's Tailscale IP, not Muse's IP."
                )
            elif isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
                message = (
                    f"Port {candidate.get('port')} is already in use. Close the other "
                    "service or choose a different port."
                )
            elif isinstance(exc, OSError):
                message = (
                    f"Could not open {candidate.get('bind')}:{candidate.get('port')}. "
                    "Check that the address belongs to this PC and the port is available."
                )
            else:
                message = (
                    "Configuration was not applied; check the local bind address and settings"
                )
            raise ValueError(message) from None
        return self.bridge.operator_state()

    @staticmethod
    def _page() -> str:
        return "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Jett Pad</title><style>body{color:#F5F0E6;background:#0A0A0A;font:16px/1.6 system-ui,sans-serif;max-width:680px;margin:12vh auto;padding:24px}h1{color:#C9A227;font-size:28px;line-height:1.3}p{background:#141414;padding:24px;border-radius:12px}</style></head>" \
               "<body><h1>PC controls are inside Jett Pad</h1>" \
               "<p>Open Jett Pad and choose Settings. This background service has no separate chat or control window.</p>" \
               "</body></html>"
