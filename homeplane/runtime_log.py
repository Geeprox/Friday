"""Bounded operational metadata. Never accept a message, payload or exception text."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import fcntl
import hashlib
import hmac
import inspect
import json
import logging
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.responses import Response

FILE_BYTES = 262144
BACKUPS = 2
MEMORY_ROWS = 256
EXPORT_ROWS = 64
STAGES = frozenset('''service.construct service.start service.stop http.request
embodied.start embodied.stop embodied.command embodied.preview embodied.voice
people.collect people.enroll
infrastructure.reconcile infrastructure.configure
automation.catalog.read automation.catalog.import
desktop.connect desktop.disconnect desktop.close desktop.arl desktop.health
ha.connect ha.auth ha.snapshot ha.read ha.subscribe ha.stream ha.close
ha.inventory ha.stream.gap
flow.bootstrap flow.turn flow.cancel flow.invalidate flow.dispose flow.close
action.plan action.approve action.transition action.reconcile device.call device.readback
arl.status arl.job arl.complete arl.worker
arl.thread nas.connect
archive.privacy archive.playback archive.read archive.stream archive.cancel archive.close
nas.start nas.configure nas.resume nas.disable nas.owner nas.read nas.health nas.close
'''.split())
UPSTREAMS = frozenset({'local', 'ha', 'arl', 'nas'})
RESULTS = frozenset({'ok', 'refused', 'unavailable', 'timeout', 'cancelled', 'error'})
# Membership is the privacy boundary. Syntactically valid strings are NOT trusted codes.
CODES = frozenset('''none unclassified internal_error invalid_local_origin origin_required
unsupported_ttl identity_unavailable offline_dependency stale_input unsupported invalid_readback
unavailable_readback invalid_actuator_binding request_refused
authentication_unavailable subscription_disconnected event_buffer_overflow state_delta_discontinuity
reconcile_not_converged event_identity_mismatch
unauthorized not_found request_budget_exceeded desktop_read_only home_not_connected
ha_authorization_rejected ha_transport_unavailable invalid_config adapter_closed
READ_ONLY_ADAPTER connection_in_progress ha_not_configured configuration_unavailable
not_running not_checked not_connected sdk_missing disconnected runtime_busy runtime_error
deadline_exceeded expired invalid_timeout tool_not_authorized discarded_configuration_changed
flow_session_unavailable flow_operation_unavailable flow_operation_budget flow_operation_finished
flow_operation_in_progress flow_context_changed flow_lane_changed flow_cancelled
flow_live_write_held flow_report_unavailable flow_backend_unavailable
awaiting_explicit_execution send_claim_recorded ha_service_accepted ha_reported_match
rejected_by_ha reported_state_mismatch readback_or_transport_unavailable
request_cancelled_after_send expired_request restart_requires_new_plan authority_changed
dispatch_guard_rejected managed_guard_required source_conflict readback_match_causality_unconfirmed
late_or_mismatched_readback device_authority_missing action_authority_unavailable
nas_auth_failed nas_access_denied nas_host_key_mismatch nas_dependency_missing nas_missing
nas_revision_changed nas_invalid_request nas_budget_exceeded nas_cursor_expired nas_object_unknown
nas_busy nas_source_changed nas_timeout nas_transport_failed nas_unavailable nas_not_configured
nas_credential_unavailable nas_credentials_unavailable nas_connection_required
not_ready logging_unavailable transport_error authentication_rejected
service_unavailable
archive_privacy_unavailable archive_privacy_busy archive_privacy_budget
archive_source_changed_or_unavailable archive_context_changed archive_playback_expired
archive_playback_not_found archive_playback_byte_budget archive_playback_busy
archive_media_interrupted archive_output_budget archive_invalid_media_range
archive_media_range_required archive_preview_cancelled privacy_source_changed
privacy_invalid_job privacy_media_budget
'''.split())
CODES = CODES | frozenset(
    'control_approval_mismatch control_cancelled_after_send control_cancelled_before_dispatch control_cancelled_result_unavailable control_dispatch_guard_unavailable control_domain_unavailable control_operation_in_progress control_operation_unavailable control_plan_unavailable control_port_unavailable control_preview flow_backend_unavailable flow_busy flow_cancel_required flow_cancelled flow_cancelled_result_unavailable flow_closed flow_context_changed flow_current flow_duplicate_turn flow_expired flow_history_budget flow_idempotency_conflict flow_lane_changed flow_live_write_held flow_operation_budget flow_operation_capacity flow_operation_conflict flow_operation_deadline flow_operation_finished flow_operation_in_progress flow_operation_invalidated flow_operation_reference_mismatch flow_operation_unavailable flow_read_scope_changed flow_read_scope_unavailable flow_ready flow_reference_context_changed flow_reference_expired flow_report_unavailable flow_request_budget flow_response_budget flow_response_or_request_budget flow_session_capacity flow_session_disposed flow_session_mismatch flow_session_unavailable flow_turn_budget flow_turn_capacity flow_turn_conflict ha_authorization_rejected ha_brightness ha_cached_state ha_configuration_or_protocol_error ha_credentials_unavailable ha_http_error ha_not_configured ha_read_timeout ha_reported_match ha_service_accepted ha_unreachable ha_version nas_access_denied nas_auth_failed nas_budget_exceeded nas_busy nas_cursor_expired nas_dependency_missing nas_disabled nas_host_key_mismatch nas_invalid_request nas_missing nas_not_configured nas_object_unknown nas_revision_changed nas_source_changed nas_timeout nas_transport_failed nas_unavailable rule_approval_already_consumed rule_approval_mismatch rule_candidate_mismatch rule_cleanup_pending rule_context_changed rule_domain_unavailable rule_exact_approval_required rule_lane_mismatch rule_new_preview_required rule_no_deployment_to_reconcile rule_operation_busy rule_original_text_changed rule_original_text_required rule_plan_unavailable rule_poll_budget rule_ports_instance_mismatch rule_ports_unavailable rule_request_unsupported rule_session_capacity rule_session_unavailable rule_source_scope_unavailable rule_write_authority_unavailable service_outcome_unknown why_bounded_evidence why_candidate_unavailable why_cursor_unavailable why_device_selection_required why_evidence_ref_unavailable why_room_selection_required why_selection_with_new_question'.split()
)
REFUSALS = frozenset({'refused', 'rejected', 'expired', 'unavailable', 'failed', 'unknown_outcome',
                      'cancelled', 'paused', 'backoff', 'not_configured', 'not_connected',
                      'not_running', 'sdk_missing', 'unauthorized', 'disabled'})
SUCCESS_CODES = frozenset({'none', 'awaiting_explicit_execution', 'send_claim_recorded',
                           'ha_service_accepted', 'ha_reported_match', 'readback_match_causality_unconfirmed'})
_journal = contextvars.ContextVar('friday_runtime_journal', default=None)
_correlation = contextvars.ContextVar('friday_runtime_correlation', default=None)
_http = contextvars.ContextVar('friday_runtime_http', default=False)
_operation = contextvars.ContextVar('friday_runtime_operation', default=False)


def fixed_code(value):
    return value if isinstance(value, str) and value in CODES else 'unclassified'


def exception_result(error):
    if isinstance(error, asyncio.CancelledError):
        return 'cancelled', 'flow_cancelled'
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return 'timeout', 'deadline_exceeded'
    if isinstance(error, httpx.HTTPStatusError):
        return 'unavailable', ('ha_authorization_rejected' if error.response.status_code in {401, 403}
                               else 'ha_http_error')
    code = fixed_code(getattr(error, 'code', None))
    if code == 'unclassified':
        # ValueError/RuntimeError domain codes are accepted only by exact membership.
        code = fixed_code(error.args[0] if len(error.args) == 1 else None)
        if code == 'unclassified' and len(error.args) == 1 and error.args[0] == 'HA authentication rejected':
            code = 'ha_authorization_rejected'
    if code == 'unclassified' and isinstance(error, OSError):
        code = 'transport_error'
    if code in {'deadline_exceeded', 'expired', 'expired_request', 'nas_timeout'}:
        return 'timeout', code
    if code in {'flow_cancelled', 'control_cancelled', 'rule_cancelled', 'request_cancelled_after_send'}:
        return 'cancelled', code
    return 'error', code


def result_projection(value):
    if isinstance(value, Response):
        status = value.status_code
        if type(status) is not int or not 100 <= status <= 599:
            return 'error', 'unclassified'
        code = 'internal_error' if status >= 500 else 'request_refused' if status >= 400 else 'none'
        # A returned response is a business outcome too. Inspect only a bounded
        # completed body and retain solely a fixed allowlisted code, never its text.
        body = getattr(value, 'body', None)
        if status >= 400 and isinstance(body, bytes) and len(body) <= 4096:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and 'code' in payload:
                    code = fixed_code(payload['code'])
            except (ValueError, UnicodeError, RecursionError):
                pass
        return ('timeout' if status in {408, 504} else 'unavailable' if status >= 500
                else 'refused' if status >= 400 else 'ok'), code
    if isinstance(value, str) and value in {'rejected_by_ha', 'service_outcome_unknown'}:
        return 'unavailable', fixed_code(value)
    def field(name):
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)
    failure = field('failure')
    code = failure or field('code')
    phase = field('phase') or field('status') or field('connection') or field('state')
    # Malformed upstream fields must not break the business function or be serialized.
    phase = phase if isinstance(phase, str) else None
    code = code if isinstance(code, str) else 'unclassified' if code is not None else None
    if phase == 'cancelled':
        return 'cancelled', fixed_code(code)
    if not failure and phase in {'clarify', 'select', 'preview', 'result', 'planned', 'sent', 'accepted',
                                'reported_match', 'verified', 'ready', 'online', 'connected', 'completed', 'ok'}:
        return 'ok', fixed_code(code) if code is not None else 'none'
    if code is not None and code not in SUCCESS_CODES:
        code = fixed_code(code)
        if code in {'flow_cancelled', 'request_cancelled_after_send'}:
            return 'cancelled', code
        if code in {'expired', 'expired_request', 'deadline_exceeded', 'nas_timeout'}:
            return 'timeout', code
        return ('refused' if phase in {'rejected', 'refused', 'expired'} else 'unavailable'), code
    if phase in REFUSALS or field('ready') is False:
        return 'unavailable', fixed_code(phase) if phase in CODES else 'not_ready'
    return 'ok', fixed_code(code) if code is not None else 'none'


class RuntimeLog:
    """One private three-file journal; memory/export and correlation links are also bounded.

    Disk failures are visible in the diagnostic policy and never change a business outcome.
    Export uses current-process allowlisted records only, never arbitrary persisted lines.
    """
    def __init__(self, directory: Path | None = None, *, session=None):
        self.directory = Path(directory) if directory is not None else None
        self.session = session if (isinstance(session, str) and len(session) == 32
                                   and all(c in '0123456789abcdef' for c in session)) else secrets.token_hex(16)
        self.rows = deque(maxlen=MEMORY_ROWS)
        self.links = OrderedDict()
        self.salt = secrets.token_bytes(32)
        self.lock = threading.RLock()
        self.sequence = 0
        self.disk_available = None if directory is None else True
        self.disk_failures = 0

    def correlate(self, key=None):
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            return secrets.token_hex(16)
        opaque = hmac.new(self.salt, key.encode(), hashlib.sha256).hexdigest()[:32]
        with self.lock:
            return self.links.get(opaque, opaque)

    def link(self, key, correlation):
        if isinstance(key, str) and 1 <= len(key) <= 256:
            opaque = hmac.new(self.salt, key.encode(), hashlib.sha256).hexdigest()[:32]
            with self.lock:
                self.links[opaque] = correlation
                self.links.move_to_end(opaque)
                while len(self.links) > MEMORY_ROWS:
                    self.links.popitem(last=False)

    def _directory_fd(self):
        path = self.directory.absolute()
        # openat/NOFOLLOW on every component prevents ancestor and leaf substitution.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(path.parts[1:]):
                if part in {'.', '..'}:
                    raise OSError('unsafe_directory')
                final = index == len(path.parts[1:]) - 1
                if final:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError('unsafe_directory')
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _file(fd, name, *, create=False):
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        handle = os.open(name, flags, 0o600, dir_fd=fd)
        info = os.fstat(handle)
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1):
            os.close(handle)
            raise OSError('unsafe_file')
        return handle, info.st_size

    def _append(self, raw):
        directory = self._directory_fd()
        try:
            # Separate app instances share a nonblocking directory lock during append/rotation.
            # Contention is reported as a dropped disk write; business execution never waits.
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Validate every occupied rotation slot before mutating any file.
            for name in ('runtime.jsonl', 'runtime.1.jsonl', 'runtime.2.jsonl'):
                try:
                    fd, size = self._file(directory, name)
                except FileNotFoundError:
                    continue
                os.close(fd)
                if size > FILE_BYTES:
                    raise OSError('oversized_file')
            fd, size = self._file(directory, 'runtime.jsonl', create=True)
            if size + len(raw) > FILE_BYTES:
                os.close(fd)
                try:
                    os.unlink('runtime.2.jsonl', dir_fd=directory)
                except FileNotFoundError:
                    pass
                try:
                    os.rename('runtime.1.jsonl', 'runtime.2.jsonl', src_dir_fd=directory, dst_dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.rename('runtime.jsonl', 'runtime.1.jsonl', src_dir_fd=directory, dst_dir_fd=directory)
                fd, _ = self._file(directory, 'runtime.jsonl', create=True)
            try:
                if os.write(fd, raw) != len(raw):
                    raise OSError('partial_write')
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def record(self, stage, event, *, result='ok', code='none', upstream='local', elapsed_ms=0,
               correlation=None, http_status=None):
        if stage not in STAGES or event not in {'start', 'result'} or upstream not in UPSTREAMS:
            return
        correlation = correlation or _correlation.get() or secrets.token_hex(16)
        if not isinstance(correlation, str) or len(correlation) != 32 or any(c not in '0123456789abcdef' for c in correlation):
            correlation = secrets.token_hex(16)
        with self.lock:
            self.sequence += 1
            row = {'schema': 1, 'sequence': self.sequence, 'at': datetime.now(UTC).isoformat(),
                   'session': self.session,
                   'correlation': correlation, 'stage': stage, 'event': event,
                   'result': result if result in RESULTS else 'error', 'code': fixed_code(code),
                   'upstream': upstream, 'elapsed_ms': max(0, min(int(elapsed_ms), 86400000))}
            if isinstance(http_status, int) and 100 <= http_status <= 599:
                row['http_status'] = http_status
            raw = (json.dumps(row, separators=(',', ':')) + '\n').encode()
            self.rows.append(row)
            if self.directory is not None:
                try:
                    self._append(raw)
                    self.disk_available = True
                except OSError:
                    self.disk_available = False
                    self.disk_failures = min(self.disk_failures + 1, 2147483647)

    def export(self):
        with self.lock:
            return {'schema': 1, 'redacted': True, 'automatic_upload': False,
                    'retention': {'files': BACKUPS + 1, 'bytes_per_file': FILE_BYTES,
                                  'max_disk_bytes': FILE_BYTES * (BACKUPS + 1),
                                  'memory_rows': MEMORY_ROWS, 'export_rows': EXPORT_ROWS},
                    'disk_available': self.disk_available, 'disk_failures': self.disk_failures,
                    'code': 'logging_unavailable' if self.disk_available is False else 'none',
                    'basis': 'current_process_operational_metadata_no_upstream_refresh',
                    'events': [dict(row) for row in list(self.rows)[-EXPORT_ROWS:]]}


@contextmanager
def binding(journal, key=None):
    token = _journal.set(journal)
    correlation = _correlation.set(journal.correlate(key) if key else (_correlation.get() or journal.correlate()))
    try:
        yield
    finally:
        _correlation.reset(correlation)
        _journal.reset(token)


@contextmanager
def stage_span(stage, *, upstream='local'):
    """A synchronous span around existing code; no new worker or cancellation boundary."""
    journal = _journal.get()
    started = time.monotonic()
    if journal is not None:
        journal.record(stage, 'start', upstream=upstream)
    try:
        yield
    except BaseException as error:
        if journal is not None:
            result, code = exception_result(error)
            journal.record(stage, 'result', upstream=upstream, result=result, code=code,
                           elapsed_ms=(time.monotonic() - started) * 1000)
        raise
    else:
        if journal is not None:
            journal.record(stage, 'result', upstream=upstream, elapsed_ms=(time.monotonic() - started) * 1000)


def observe(stage, *, upstream='local', key=None, failure_code=None):
    """Preserves signatures, returns, exceptions and task ownership. No payload serialization."""
    assert stage in STAGES and upstream in UPSTREAMS
    def decorate(function):
        signature = inspect.signature(function)
        @contextmanager
        def span(args, kwargs):
            journal = getattr(args[0], 'runtime_log', None) if args else None
            journal = journal or _journal.get()
            if journal is None:
                yield None
                return
            identifier = None
            if key:
                root, _, field = key.partition('.')
                argument = signature.bind(*args, **kwargs).arguments.get(root)
                identifier = getattr(argument, field, None) if field else argument
            with binding(journal, None if _operation.get() else identifier):
                operation = _operation.set(True)
                started = time.monotonic()
                journal.record(stage, 'start', upstream=upstream)
                try:
                    yield journal
                except BaseException as error:
                    result, code = exception_result(error)
                    journal.record(stage, 'result', result=result, code=code, upstream=upstream,
                                   elapsed_ms=(time.monotonic() - started) * 1000)
                    raise
                finally:
                    _operation.reset(operation)
        def finish(journal, value, started, args, kwargs):
            if journal:
                result, code = result_projection(value)
                if failure_code:
                    result, code = 'unavailable', fixed_code(signature.bind(*args, **kwargs).arguments.get(failure_code))
                    if code in {'deadline_exceeded', 'expired', 'expired_request', 'nas_timeout'}:
                        result = 'timeout'
                journal.link(getattr(value, 'action_id', None), _correlation.get())
                journal.record(stage, 'result', result=result, code=code, upstream=upstream,
                               elapsed_ms=(time.monotonic() - started) * 1000)
            return value
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapped(*args, **kwargs):
                with span(args, kwargs) as journal:
                    started = time.monotonic()
                    return finish(journal, await function(*args, **kwargs), started, args, kwargs)
        else:
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                with span(args, kwargs) as journal:
                    started = time.monotonic()
                    return finish(journal, function(*args, **kwargs), started, args, kwargs)
        return wrapped
    return decorate


async def logged_asgi(app, scope, receive, send, journal):
    if scope['type'] not in {'http', 'lifespan'} or (_http.get() and _journal.get() is journal):
        return await app(scope, receive, send)
    with binding(journal):
        token = _http.set(scope['type'] == 'http')
        started = time.monotonic()
        status = None
        response_code = None
        stage = 'http.request'
        if scope['type'] == 'http':
            journal.record(stage, 'start')
        async def incoming():
            nonlocal started, stage
            message = await receive()
            if message['type'] in {'lifespan.startup', 'lifespan.shutdown'}:
                stage = 'service.start' if message['type'] == 'lifespan.startup' else 'service.stop'
                started = time.monotonic()
                journal.record(stage, 'start')
            return message
        async def outgoing(message):
            nonlocal status, response_code
            if message['type'] == 'http.response.start':
                status = message['status']
            elif (message['type'] == 'http.response.body' and status is not None and status >= 400
                  and not message.get('more_body') and len(message.get('body', b'')) <= 4096):
                try:
                    value = json.loads(message.get('body', b''))
                    response_code = fixed_code(value.get('code')) if isinstance(value, dict) else None
                except (ValueError, UnicodeError, RecursionError):
                    pass
            if message['type'] in {'lifespan.startup.complete', 'lifespan.shutdown.complete',
                                    'lifespan.startup.failed', 'lifespan.shutdown.failed'}:
                journal.record(stage, 'result', result='error' if message['type'].endswith('.failed') else 'ok',
                               code='internal_error' if message['type'].endswith('.failed') else 'none',
                               elapsed_ms=(time.monotonic() - started) * 1000)
            await send(message)
        try:
            await app(scope, incoming, outgoing)
            if scope['type'] == 'http':
                journal.record(stage, 'result', result='unavailable' if status is None or status >= 500 else
                               'refused' if status >= 400 else 'ok',
                               code=response_code or ('internal_error' if status is None or status >= 500 else
                               'request_refused' if status >= 400 else 'none'),
                               http_status=status, elapsed_ms=(time.monotonic() - started) * 1000)
        except BaseException as error:
            result, code = exception_result(error)
            journal.record(stage, 'result', result=result, code=code, http_status=status,
                           elapsed_ms=(time.monotonic() - started) * 1000)
            raise
        finally:
            _http.reset(token)


class RuntimeLogMiddleware:
    def __init__(self, app, *, journal):
        self.app, self.journal = app, journal

    async def __call__(self, scope, receive, send):
        await logged_asgi(self.app, scope, receive, send, self.journal)


def asgi_observed(function):
    @functools.wraps(function)
    async def wrapped(self, scope, receive, send):
        async def app(scope, receive, send):
            await function(self, scope, receive, send)
        await logged_asgi(app, scope, receive, send, self.runtime_log)
    return wrapped


class SafeServerHandler(logging.Handler):
    """A server log may contain URLs, bodies and traceback locals: retain none of it."""
    def __init__(self, journal):
        super().__init__()
        self.journal = journal

    def emit(self, record):
        self.journal.record('http.request', 'result', result='error', code='internal_error')
