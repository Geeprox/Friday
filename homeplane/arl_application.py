"""Friday's lifecycle and receipt adapter for the shared ARL application SDK.

This stores request identities and admission receipts, never prompts/results or a
second work queue. Only ARL schedules inference. Construction/registration does
not submit a model task or grant any household capability.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

APP_ID = 'com.friday.home.desktop'
MAX_INPUT_BYTES = 12000
SCENARIOS = (
    dict(id='voice_intent', name='语音意图解析', task_kind='household.voice.intent',
         priority='interactive', package_id='low', deadline_ms=30000, max_output_bytes=8192),
    dict(id='home_planning', name='家庭操作建议', task_kind='household.plan',
         priority='interactive', package_id='high', deadline_ms=30000, max_output_bytes=8192),
)
TERMINAL = {'succeeded', 'failed', 'cancelled', 'needs_review', 'needs_unlock'}


def sdk_compatibility(*, vendor_root=None, expected_manifest_sha256=None):
    """Canonical import-only SDK gate; invoke in a fresh probe interpreter."""
    from .arl_sdk_contract import check_sdk
    return check_sdk(vendor_root=vendor_root, expected_manifest_sha256=expected_manifest_sha256)


class ApplicationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def sdk_session(home):
    """Use owning SDK discovery/handshake/heartbeat, with no RuntimeLauncher."""
    from arl.application_session import ApplicationSession
    from arl.client import Client
    from arl.contracts import Fault
    from .arl_icon import application_icon

    try:
        icon, icon_error = application_icon(), None
    except (OSError, ValueError, KeyError):
        icon, icon_error = None, 'application_icon_unavailable'

    class SupportedSession(ApplicationSession):
        def register(self):
            self.icon_registered = False
            self.icon_error = icon_error
            description = None
            try:
                description = Client(self.client.home, 'settings').describe_runtime()
            except Fault as error:
                # Some hosts expose only readiness to the settings identity.
                # The owning SDK still requires open/challenge/confirm with the
                # declared scenarios; metadata rejection is never a connection.
                if error.code not in {'method_not_authorized', 'method_not_allowed', 'unknown_method', 'unsupported_method'}:
                    raise
            if description is not None and any(description.get('features', {}).get(key) != 1 for key in (
                    'application_registration', 'application_tasks', 'model_scheduler')):
                raise Fault('application_platform_upgrade_required')
            try:
                accepted = super().register()
                self.icon_registered = self.icon is not None
                return accepted
            except Fault as error:
                if error.code == 'application_icon_invalid' and self.client.app_session is not None:
                    # The SDK has confirmed this process before uploading the
                    # icon. Keep that distinct gate; never relabel an ic12
                    # source as icp6 just to satisfy an older host profile.
                    self.icon_error = 'application_icon_profile_unsupported'
                    self.connected, self.error = True, None
                    return list(self.accepted)
                if error.code in {'invalid_arguments', 'method_not_authorized', 'method_not_allowed', 'unknown_method',
                                   'unsupported_method', 'app_identity_unverified', 'unknown_app'}:
                    raise Fault('application_platform_upgrade_required') from None
                raise

    return SupportedSession(home, APP_ID, 'Friday', copy.deepcopy(SCENARIOS),
                            icon_ref='sf:house.fill', version='0.6.0', icon=icon,
                            client_factory=lambda h, a: Client(h, a, max_frame_bytes=65536))


class RequestReceipts:
    """Durable dedupe metadata only; there is no claim/dispatch/replay worker."""
    def __init__(self, directory):
        directory = Path(directory).absolute() / 'arl-application'
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ApplicationError('model_receipt_path_invalid')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise ApplicationError('model_receipt_path_invalid')
        path = directory / 'receipts.sqlite'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ApplicationError('model_receipt_path_invalid')
        finally:
            os.close(fd)
        self.path = path

    def prepare(self, request_id, fingerprint):
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts '
                       '(request TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, admission TEXT, '
                       'finalized INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(receipts)')}
            if 'finalized' not in columns:
                db.execute('ALTER TABLE receipts ADD COLUMN finalized INTEGER NOT NULL DEFAULT 0')
            if 'updated' not in columns:
                db.execute('ALTER TABLE receipts ADD COLUMN updated REAL NOT NULL DEFAULT 0')
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint,admission FROM receipts WHERE request=?', (request_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ApplicationError('model_request_conflict')
                return json.loads(old[1]) if old[1] else None
            if db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] >= 2000:
                # Retire old completed cache entries; ARL owns durable dedupe.
                # Unknown submissions are retained and are never auto-replayed.
                db.execute('DELETE FROM receipts WHERE request IN '
                           '(SELECT request FROM receipts WHERE finalized=1 ORDER BY updated LIMIT 200)')
            if db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] >= 2000:
                raise ApplicationError('model_receipt_capacity')
            db.execute('INSERT INTO receipts(request,fingerprint,updated) VALUES (?,?,?)',
                       (request_id, fingerprint, time.time()))
        return None

    def admit(self, request_id, admission):
        encoded = json.dumps(admission, sort_keys=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            row = db.execute('SELECT admission FROM receipts WHERE request=?', (request_id,)).fetchone()
            if row is None or row[0] is not None and row[0] != encoded:
                raise ApplicationError('model_admission_changed')
            db.execute('UPDATE receipts SET admission=? WHERE request=?', (encoded, request_id))

    def finish(self, request_id):
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('UPDATE receipts SET finalized=1,updated=? WHERE request=?', (time.time(), request_id))


class FridayApplication:
    def __init__(self, home, state_directory, *, session_factory=sdk_session, allow_simulated=False):
        self.home, self.state_directory = home, state_directory
        self.factory, self.allow_simulated = session_factory, allow_simulated
        self.session = None
        self.started = False
        self.error = None
        self._lock = threading.RLock()
        self._active = set()
        self._workers = set()
        self._closed = False

    def start(self):
        with self._lock:
            if self._closed:
                return
            self.started = True
            if self.session is not None:
                return
            try:
                self.session = self.factory(Path(self.home()))
                self.session.start()
                self.error = None
            except ImportError:
                self.session = None
                self.error = 'application_sdk_upgrade_required'
            except Exception as error:
                self.error = self._code(error)

    @staticmethod
    def _code(error):
        code = getattr(error, 'code', 'application_unavailable')
        return code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,79}', code) else 'application_unavailable'

    def status(self):
        with self._lock:
            session = self.session
            connected = bool(not self._closed and session and session.connected
                             and Path(session.client.home) == Path(self.home()))
            error = self.error or (self._code(SimpleError(session.error)) if session and session.error else None)
            return dict(app_id=APP_ID, connected=connected,
                        state='connected' if connected else 'closed' if self._closed else
                        'upgrade_required' if error in {'application_sdk_upgrade_required', 'application_platform_upgrade_required'}
                        else 'registering' if self.started else 'not_started',
                        error=error, model_service_accepted=bool(connected and 'codex.text.plan.v1' in session.accepted),
                        icon=dict(registered=bool(connected and getattr(session, 'icon_registered', False)),
                                  source_resource=(getattr(session, 'icon', None) or {}).get('source_resource'),
                                  sha256=(getattr(session, 'icon', None) or {}).get('sha256'),
                                  error=getattr(session, 'icon_error', None)),
                        identity_basis='same_account_kernel_pid_birth_executable',
                        publisher_attestation=False, device_authority=False,
                        model_data_policy=dict(friday_prompt_storage='none',
                                               arl_prompt_result_storage='durable_queue', retention='arl_managed'),
                        scenarios=copy.deepcopy(SCENARIOS), heartbeat_seconds=10, lease_seconds=30)

    async def close(self):
        with self._lock:
            self._closed = True
            for stop in self._active:
                stop.set()
            session = self.session
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)
        if session:
            await asyncio.to_thread(session.close)

    async def ping(self, *, timeout=8):
        """A current connection.ping receipt, without submitting a work item."""
        from .arl import ARLError
        self.start()
        end = time.monotonic() + timeout
        while not self.status()['connected'] and time.monotonic() < end:
            if self.session is None or self.status()['state'] == 'upgrade_required':
                raise ARLError(self.status()['error'] or 'application_not_registered')
            await asyncio.sleep(.05)
        if not self.status()['connected']:
            raise ARLError('application_not_registered')
        session = self.session
        try:
            receipt = await asyncio.wait_for(asyncio.to_thread(session.client.ping_app_session), timeout=5)
        except Exception as error:
            session.connected = False
            raise ARLError(self._code(error)) from None
        if (not self.status()['connected'] or receipt.get('connected') is not True
                or receipt.get('session_id') != session.client.app_session):
            raise ARLError('app_session_changed')
        return dict(status='sdk_roundtrip_pass', application_registered=True,
                    inference_attempted=False, device_write_attempted=False, hardware_verified=False)

    def _admission(self, job, scenario):
        resolved = job.get('resolved') if isinstance(job, dict) else None
        if (not isinstance(resolved, dict) or set(resolved) != {'model', 'reasoning_effort', 'service_tier', 'priority'}
                or job.get('scenario_id') != scenario['id'] or job.get('package_id') != scenario['package_id']
                or not isinstance(job.get('id'), str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,128}', job['id'])
                or resolved['priority'] != scenario['priority']
                or any(not isinstance(resolved[k], str) or not 1 <= len(resolved[k]) <= 200
                       or any(ord(c) < 32 for c in resolved[k])
                       for k in ('model', 'reasoning_effort', 'service_tier'))):
            raise ApplicationError('model_admission_invalid')
        return dict(id=job['id'], scenario_id=scenario['id'], package_id=scenario['package_id'], resolved=copy.deepcopy(resolved))

    def _result(self, job, admission, scenario, text):
        result = job.get('result')
        resolved = admission['resolved']
        native = job.get('progress', {}).get('native', {})
        if not isinstance(result, dict):
            raise ApplicationError('model_result_invalid')
        preferences = result.get('execution_preferences', {})
        output = result.get('text')
        if (result.get('schema_version') != 1 or type(result.get('simulated')) is not bool
                or result['simulated'] and not self.allow_simulated
                or result.get('model') != resolved['model']
                or preferences.get('reasoning_effort') != resolved['reasoning_effort']
                or preferences.get('service_tier') != resolved['service_tier']
                or preferences.get('source') != ('test_double' if result['simulated'] else 'thread_start_confirmed')
                or not isinstance(output, str) or len(output.encode()) > scenario['max_output_bytes']
                or result.get('request_sha256') != hashlib.sha256(text.encode()).hexdigest()
                or result.get('output_sha256') != hashlib.sha256(output.encode()).hexdigest()
                or not result.get('thread_id') or not result.get('turn_id')
                or native.get('thread_id') != result['thread_id'] or native.get('turn_id') != result['turn_id']
                or native.get('terminal') != 'completed'):
            raise ApplicationError('model_result_unverified')
        return {**result, 'application_admission': copy.deepcopy(admission), 'native_acknowledgements_verified': True,
                'device_authority': False}

    async def complete(self, scenario_id, text, *, request_id, deadline, current=None):
        from .arl import ARLError
        scenario = next((s for s in SCENARIOS if s['id'] == scenario_id), None)
        if (scenario is None or not isinstance(text, str) or not 0 < len(text.encode()) <= MAX_INPUT_BYTES
                or not isinstance(request_id, str) or re.fullmatch(r'[a-z0-9-]{1,128}', request_id) is None
                or not math.isfinite(deadline)):
            raise ARLError('application_request_invalid')
        if current is not None and not callable(current):
            raise ARLError('application_request_invalid')
        deadline = min(deadline, time.monotonic() + scenario['deadline_ms'] / 1000)
        self.start()
        stop = threading.Event()
        home = Path(self.home())
        def check():
            if self._closed or stop.is_set() or time.monotonic() >= deadline:
                raise ARLError('expired')
            if Path(self.home()) != home or current is not None and current() is not True:
                raise ARLError('discarded_model_task_changed')
        def work():
            admission = None
            completed = False
            try:
                check()
                session = self.session
                if session is None:
                    raise ARLError(self.error or 'application_unavailable')
                if Path(session.client.home) != home:
                    raise ARLError('discarded_configuration_changed')
                receipts = RequestReceipts(self.state_directory())
                fingerprint = hashlib.sha256(json.dumps([scenario_id, text], ensure_ascii=False).encode()).hexdigest()
                admission = receipts.prepare(request_id, fingerprint)
                check()
                if admission is None:
                    job = session.submit(scenario_id, text, request_id=request_id)
                    admission = self._admission(job, scenario)
                    receipts.admit(request_id, admission)
                else:
                    self._admission(admission, scenario)
                while True:
                    check()
                    job = session.get(admission['id'])
                    if not isinstance(job, dict) or job.get('id') != admission['id'] or job.get('tool') != 'codex.text.plan.v1':
                        raise ARLError('model_job_changed')
                    if job.get('state') in TERMINAL:
                        break
                    stop.wait(.05)
                check()
                receipts.finish(request_id)
                if job['state'] != 'succeeded':
                    raise ARLError('application_task_' + job['state'])
                result = self._result(job, admission, scenario, text)
                check()
                completed = True
                return result
            except Exception as error:
                if self.session and getattr(error, 'code', None) in {'disconnected', 'app_session_changed', 'app_session_expired'}:
                    self.session.connected = False
                raise ARLError(self._code(error)) from None
            finally:
                if admission and not completed:
                    try:
                        self.session.cancel(admission['id'])
                    except Exception:
                        pass
                with self._lock:
                    self._active.discard(stop)
        with self._lock:
            check()
            if len(self._active) >= 16:
                raise ARLError('application_busy')
            self._active.add(stop)
        worker = asyncio.create_task(asyncio.to_thread(work))
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        worker.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), max(.001, deadline-time.monotonic()))
        except TimeoutError:
            stop.set()
            raise ARLError('deadline_exceeded') from None
        except asyncio.CancelledError:
            stop.set()
            raise


class SimpleError:
    def __init__(self, code):
        self.code = code
