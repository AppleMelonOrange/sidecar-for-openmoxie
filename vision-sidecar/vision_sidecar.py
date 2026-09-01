#!/usr/bin/env python3
"""OpenMoxie vision sidecar — additive MQTT companion, no OpenMoxie source edits.

WHY this process exists
-----------------------
Stock OpenMoxie never opens the robot's native image-captioning gate. The
firmware already ships the camera/caption path; it stays shut until:

1. HiveConfiguration serves data_sharing="full" plus three IC props
   (see apply_vision_config.py — that is the DB-row substitute for editing
   DEFAULT_ROBOT_CONFIG / DEFAULT_ROBOT_SETTINGS in robot_data.py).
2. On robot MQTT connect, ONE ZMQ arm proto is published:
   EnableICModule{run=True} — the "polite arm". Earlier versions also sent
   LoggingStateUpdate{state=2, upload_policy=2}; that message restarts the
   robot's logging session, which the microphone's streaming-STT channel is
   armed on, so every arm silently broke Moxie's hearing. See
   send_arm_protos and docs/enable-vision-step-by-step.md
   ("The camera-breaks-hearing problem").
3. Optionally, the robot's client-service-http-token event is answered with
   a dummy JSON http_token command. Whether (3) is load-bearing is an OPEN
   QUESTION this sidecar exists to test; default is ON to match the working
   core-patched recipe.

This process does (2) and (3) by sitting on the SAME mosquitto broker as
stock OpenMoxie and reacting to mosquitto's own $SYS/broker/log/# connect
lines. It does not patch moxie_server.py.

INVARIANTS
----------
- Never edit OpenMoxie core. If a behavior cannot be done from this process,
  stop; do not reach into site/hive/mqtt/.
- Wire format of ZMQ commands MUST match moxie_server.send_zmq_to_bot:
  ASCII proto full_name + b':' + SerializeToString(), published to
  /devices/{device_id}/commands/zmq. No length prefix, no extra framing.
- HTTP-token reply MUST match moxie_server.send_command_to_bot_json:
  topic /devices/{device_id}/commands/http_token, payload = json.dumps(
  {'command': 'http_token', 'http_token': 'notoken'}) as a str (paho encodes).
- Connect device_id comes from regex on the $SYS log PAYLOAD text, never
  from the topic. Patterns are copied verbatim from moxie_server.py.
- A malformed MQTT message must log and continue; this process must not exit.
- No live broker/robot in unit tests; handlers are callable with fakes.

See: docs/ENABLE_VISION_ON_MOXIE.md, docs/PROJECT_OVERVIEW.md
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import ssl
import sys
import threading
import time

import paho.mqtt.client as mqtt

# Generated pb2 modules register themselves as embodied.* (see
# BuildTopDescriptorsAndMessages(..., 'embodied.logging.LoggingStateUpdate_pb2', ...)).
# They have no intra-package imports, but putting protos/ on sys.path lets us
# import them under that same embodied.* name. Do this BEFORE the import.
_PROTOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'protos')
if _PROTOS_DIR not in sys.path:
    sys.path.insert(0, _PROTOS_DIR)

from embodied.logging.LoggingStateUpdate_pb2 import LoggingStateUpdate  # noqa: E402
from embodied.robotbrain.EnableICModule_pb2 import EnableICModule  # noqa: E402

logger = logging.getLogger('vision_sidecar')

# Verbatim from site/hive/mqtt/moxie_server.py (MoxieServer.__init__).
# Connect: match.group(2) is the device_id (d_<hex-and-hyphens>).
# Disconnect: match.group(1) is the device_id. Device id is NEVER in the topic
# for this feed — only in the payload text.
CONNECT_PATTERN = r"connected from (.*) as (d_[a-f0-9-]+)"
DISCONNECT_PATTERN = r"Client (d_[a-f0-9-]+) (closed its connection|disconnected)"

ARM_DEBOUNCE_S = 10.0
DEFAULT_HTTP_TOKEN = 'notoken'

# PowerStatePB rides the same /devices/{id}/events/zmq stream the sidecar
# already subscribes to, framed "full.proto.name:" + bytes. Wire (firmware
# dig, corrected 2026-08-26): field 1 = timestamp varint, field 2 = state
# varint (1 Config, 2 Startup, 3 RUNNING, 4 LIGHT_SLEEP, 5 SUSPEND),
# field 3 = prev_state varint.
POWERSTATE_PREFIX = b'embodied.power.PowerStatePB:'
POWER_RUNNING = 3

# Episode-bounded stale-frame re-arm (opt-in via --frames-glob).
# WHY: the camera can stall mid-session (frames stop, robot stays awake).
# The in-core config-push watchdog (moxie_server._check_vision_alive) froze
# conversations ~2 min (public Issue #1) and cannot arm since 3fcedde
# (arming moved here). Revival is this sidecar's job now.
#
# FLOOD-SAFETY (2026-08-28 incident): a ~10s arm heartbeat crash-looped
# the robot's XMOS audio subsystem and triggered watchdog reboots. Hard
# bound: at most 2 arms per stall episode. An episode's counter resets
# ONLY when frames are observed actually flowing again — never on a
# timer alone. File-stat polling on a timer is fine (that's just reading
# a file); messages sent to the robot are strictly event-driven.
STALE_AFTER_S = 120
SECOND_ARM_AFTER_S = 300
CHECK_PERIOD_S = 30


def parse_powerstate_state(payload):
    """Return PowerStatePB.state (field 2) from a zmq event payload, or None.

    Minimal defensive varint walk — all three fields are wiretype-0
    varints; anything unexpected returns None. Never raises.
    """
    try:
        if not payload.startswith(POWERSTATE_PREFIX):
            return None
        buf = payload[len(POWERSTATE_PREFIX):]
        i, n = 0, len(buf)
        while i < n:
            tag = buf[i]
            i += 1
            field, wt = tag >> 3, tag & 7
            if wt != 0:
                return None
            val, shift = 0, 0
            while i < n:
                b = buf[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            else:
                return None
            if field == 2:
                return val
        return None
    except Exception:
        return None

# Exact JSON object core publishes via send_command_to_bot_json.
# json.dumps preserves insertion order (Py3.7+); do NOT sort_keys — the wire
# shape is this dumps() result as a str, matching core byte-for-byte in intent.
HTTP_TOKEN_COMMAND = 'http_token'


def now_ms():
    """Milliseconds since epoch. Same formula as moxie_server.now_ms."""
    return time.time_ns() // 1_000_000


def extract_connect_device_id(line):
    """Return device_id from a mosquitto $SYS log line, or None.

    Uses CONNECT_PATTERN verbatim; group(2) is the d_… id.
    """
    match = re.search(CONNECT_PATTERN, line)
    if match:
        return match.group(2)
    return None


def extract_disconnect_device_id(line):
    """Return device_id from a mosquitto disconnect log line, or None."""
    match = re.search(DISCONNECT_PATTERN, line)
    if match:
        return match.group(1)
    return None


def build_logging_state_update(timestamp_ms=None):
    """LoggingStateUpdate that opens the native IC gate (state=2, upload_policy=2).

    Both enums set to 2 because firmware OnLoggingStateChanged gates on whichever
    enum lands at native offset 0x2c (state or upload_policy). 2 == STOP / FULL.
    See moxie_server.send_logging_state_full.

    ★ NO LONGER SENT by send_arm_protos. LoggingStateUpdate restarts the
    robot's logging session — the same session the microphone's streaming-STT
    channel is armed on — so sending it kills live hearing ("BoAudio errored
    out"). Kept only for the wire-format test and as documentation of the
    old gate mechanism. Do not wire it back into any arm path.
    """
    lsu = LoggingStateUpdate()
    lsu.state = 2
    lsu.upload_policy = 2
    lsu.timestamp = now_ms() if timestamp_ms is None else timestamp_ms
    return lsu


def build_enable_ic_module(run=True, timestamp_ms=None):
    """EnableICModule that asserts the second native IC-run byte (run=True)."""
    eic = EnableICModule()
    eic.run = run
    eic.timestamp = now_ms() if timestamp_ms is None else timestamp_ms
    return eic


def frame_zmq_payload(msgobject):
    """Exact send_zmq_to_bot framing: full_name + ':' + serialized bytes.

    Verbatim from moxie_server.send_zmq_to_bot:
        payload = (msgobject.DESCRIPTOR.full_name + ":").encode('utf-8') + msgobject.SerializeToString()
    """
    return (msgobject.DESCRIPTOR.full_name + ':').encode('utf-8') + msgobject.SerializeToString()


def zmq_command_topic(device_id):
    return f'/devices/{device_id}/commands/zmq'


def http_token_command_topic(device_id):
    """Outgoing command topic. Mirrors send_command_to_bot_json(device_id, 'http_token', ...)."""
    return f'/devices/{device_id}/commands/{HTTP_TOKEN_COMMAND}'


def http_token_command_payload(token=DEFAULT_HTTP_TOKEN):
    """JSON *string* (not bytes). Core passes json.dumps(...) straight to publish."""
    return json.dumps({'command': 'http_token', 'http_token': token})


def parse_frames_glob(raw):
    """Split a comma-separated glob string into non-empty patterns.

    Empty / None → [] (feature OFF). A list/tuple is accepted so tests can
    pass patterns without joining.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return [p.strip() for p in str(raw).split(',') if p.strip()]


def newest_frame_age(patterns):
    """Seconds since the newest file matching any glob, or None.

    Never raises. No matches / missing dir / empty patterns → None
    (mirrors core moxie_server._newest_frame_age: no caption server here
    means do nothing at all, not even a log line).
    """
    if not patterns:
        return None
    newest = 0.0
    try:
        for pattern in patterns:
            try:
                matches = glob.glob(pattern)
            except (OSError, ValueError):
                continue
            for path in matches:
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue  # rotated away under us
                if mtime > newest:
                    newest = mtime
    except Exception:
        return None
    if newest == 0.0:
        return None
    return time.time() - newest


def _payload_text(payload):
    if isinstance(payload, bytes):
        return payload.decode('utf-8')
    return payload


class VisionSidecar:
    """MQTT sidecar: arm IC on connect, optionally answer http-token events.

    `client` is any object with publish(topic, payload=...) — the real paho
    client at runtime, a fake in tests. Handlers never talk to the network
    except through that object.
    """

    def __init__(
        self,
        client=None,
        http_token_enabled=True,
        http_token_value=DEFAULT_HTTP_TOKEN,
        resend_interval=0.0,
        debounce_s=ARM_DEBOUNCE_S,
        time_fn=None,
        broker_host='localhost',
        broker_port=8883,
        frames_glob='',
        newest_frame_age_fn=None,
    ):
        self._client = client
        self.http_token_enabled = http_token_enabled
        self.http_token_value = http_token_value
        self.resend_interval = float(resend_interval)
        self.debounce_s = float(debounce_s)
        self._now = time_fn or time.monotonic
        self.broker_host = broker_host
        self.broker_port = int(broker_port)
        # Separate debounce clocks: arm vs http-token must not suppress each other.
        self._arm_last = {}
        self._http_token_last = {}
        # Armed-at-least-once set: resend-interval walks this, independent of debounce.
        self._armed_devices = set()
        # Last PowerStatePB.state per device (3=RUNNING, 4=LIGHT_SLEEP,
        # 5=SUSPEND). Sleep CLOSES the capture gate (observed live
        # 2026-08-28: wake-from-sleep left the camera blind — same MQTT
        # session, no connect line, device still latched-armed). A
        # transition INTO RUNNING is a WAKE EVENT and triggers exactly one
        # re-arm. Event, not heartbeat.
        self._power_state = {}
        self._tried_unknown_user = False
        self._stop = threading.Event()
        # Stale-frame re-arm is opt-in: empty frames_glob means the feature
        # is OFF (no thread, behavior identical to before this existed).
        self._frames_glob_patterns = parse_frames_glob(frames_glob)
        # Per-device episode state: {device_id: {'arms': int, 'ts': float,
        # 'gave_up_logged': bool}}. Counter resets ONLY on observed frame
        # flow — never on elapsed wall-clock alone. See _stale_check_tick.
        self._stale_episodes = {}
        self._stale_check_thread = None
        self._stale_check_started = False
        # Injectable seam so tests drive age with a fake clock / no FS.
        # Default never raises (see newest_frame_age).
        self._newest_frame_age_fn = (
            newest_frame_age_fn if newest_frame_age_fn is not None
            else self._newest_frame_age_from_globs
        )

    # ----- publish helpers (the only place that talks to the client) -----

    def send_arm_protos(self, device_id, ignore_debounce=False, latch=True):
        """Publish EnableICModule (the "polite arm"). Returns True if sent.

        Debounce is per-device, 10s, skipped when ignore_debounce=True.
        latch=False sends the protos but does NOT mark the device armed —
        used by the $SYS connect-line trigger, whose arm can land TOO EARLY
        in a cold boot (2026-08-28: the boot-connect arm fired before the
        native vision subsystem was listening; with once-only latching there
        was no retry and the camera stayed dead all session). The arm that
        LATCHES is the first-event arm: events flowing prove the perception
        subsystems are up and able to receive. Max 2 arms per connection
        epoch (connect best-effort + first-event latch) — never a heartbeat.
        """
        now = self._now()
        if not ignore_debounce:
            last = self._arm_last.get(device_id)
            if last is not None and (now - last) < self.debounce_s:
                logger.info('VISION-ARM debounce skip dev=%s', device_id)
                return False
        ts = now_ms()
        # POLITE ARM (live-verified 2026-08-31): EnableICModule ALONE. The old
        # LoggingStateUpdate{FULL} restarted the robot's logging session, which
        # the mic's zmqSTT stream is armed on -> "BoAudio errored out" -> the
        # ear went deaf on every arm. data_sharing:"full" in the served device
        # config already supplies the policy byte, so this single message opens
        # the camera with zero audio damage. Verified live: 0 BoAudio crashes,
        # 45-min continuous conversation with 100% hearing while streaming.
        # NEVER re-add the LoggingStateUpdate publish here.
        eic = build_enable_ic_module(run=True, timestamp_ms=ts)
        topic = zmq_command_topic(device_id)
        self._client.publish(topic, payload=frame_zmq_payload(eic))
        self._arm_last[device_id] = now
        if latch:
            self._armed_devices.add(device_id)
        logger.info('VISION-ARM dev=%s sent EnableICModule (polite arm)%s', device_id,
                    '' if latch else ' (best-effort, unlatched)')
        return True

    def send_http_token(self, device_id):
        """Publish the dummy http_token command. Independent 10s debounce."""
        now = self._now()
        last = self._http_token_last.get(device_id)
        if last is not None and (now - last) < self.debounce_s:
            logger.info('VISION-HTTP-TOKEN debounce skip dev=%s', device_id)
            return False
        topic = http_token_command_topic(device_id)
        payload = http_token_command_payload(self.http_token_value)
        # Pass the dumps() str through, matching core — do not .encode().
        self._client.publish(topic, payload=payload)
        self._http_token_last[device_id] = now
        logger.info('VISION-HTTP-TOKEN dev=%s', device_id)
        return True

    # ----- MQTT callbacks -----

    def on_connect(self, client, userdata, flags, rc):
        try:
            if rc == 0:
                logger.info('MQTT connected rc=0 to %s:%s', self.broker_host, self.broker_port)
                client.subscribe('$SYS/broker/log/#')
                # Always subscribe device events — NOT just for http-token.
                # The $SYS connect line only fires on a FRESH broker session
                # (robot boot). A sidecar that (re)starts while the robot is
                # already connected, or a robot that wakes from suspend keeping
                # its session, never emits a new connect line — so we would
                # never arm it. Any live device emits events constantly; arming
                # on first event (debounced) discovers already-connected robots
                # and seeds the resend set. (2026-08-28 fix.)
                client.subscribe('/devices/+/events/#')
                return
            logger.error(
                'MQTT connect failed rc=%s. If not-authorized, the broker ACL '
                'may need an operator-added user that can read $SYS/broker/log/# '
                'and /devices/+/events/# and write /devices/+/commands/#. '
                'Trying username=unknown with no password as a fallback.',
                rc,
            )
            if not self._tried_unknown_user:
                self._tried_unknown_user = True
                client.username_pw_set(username='unknown', password=None)
                try:
                    client.reconnect()
                except Exception:
                    logger.exception('reconnect after setting username=unknown failed')
        except Exception:
            logger.exception('on_connect handler error (ignored)')

    def on_message(self, client, userdata, msg):
        try:
            self.handle_message(msg)
        except Exception:
            logger.exception('Error handling mqtt message (ignored, sidecar stays up)')

    def handle_message(self, msg):
        """Route one MQTT message. Safe to call from tests with a fake msg.

        Topic split mirrors moxie_server.on_message:
          $SYS/broker/log/N  -> ['$SYS','broker','log','N']  fromdevice=dec[2]=='log'
          /devices/{id}/events/{eventname} -> dec[2]=device_id, dec[3]=='events', dec[4]=eventname
        For the $SYS feed we regex-search the payload of EVERY message on the
        subscription (device id is in the text, not the topic). OpenMoxie also
        filters basetype=='N'; we still regex-search all log lines because a
        connect string is harmless to scan on I/D/E/W too.
        """
        dec = msg.topic.split('/')
        if len(dec) >= 3 and dec[0] == '$SYS' and dec[1] == 'broker' and dec[2] == 'log':
            self._on_sys_log(msg)
            return
        if len(dec) >= 5 and dec[3] == 'events':
            device_id = dec[2]
            eventname = dec[4]
            self._on_device_event(device_id, eventname, msg)

    def _on_sys_log(self, msg):
        line = _payload_text(msg.payload)
        device_id = extract_connect_device_id(line)
        if device_id:
            # Best-effort early arm (may land before the vision subsystem is
            # up during a cold boot) — does NOT latch; the first-event arm
            # is the one that counts. See send_arm_protos docstring.
            self.send_arm_protos(device_id, ignore_debounce=False, latch=False)
            return
        gone = extract_disconnect_device_id(line)
        if gone:
            # Clear the armed flag so the device's NEXT connection (fresh
            # boot = gate closed) gets exactly one new arm from the event
            # trigger. See the incident note in _on_device_event.
            self._armed_devices.discard(gone)
            self._arm_last.pop(gone, None)
            logger.info('VISION-DISCONNECT dev=%s (armed flag cleared for re-arm on reconnect)', gone)

    def _on_device_event(self, device_id, eventname, msg):
        # Any event proves this device is connected NOW. Arm it ONCE —
        # only if we have not already armed it this connection epoch.
        # ★ 2026-08-28 INCIDENT: the first version armed on every event with
        # only the 10s debounce, i.e. a re-arm every ~10s forever. Each arm
        # then carried LoggingStateUpdate(state=STOP)+EnableICModule; at that
        # cadence it crash-looped the robot's XMOS audio subsystem ("Audio
        # startup" every ~10s) and the watchdog escalated to FULL SYSTEM
        # REBOOTS (boot logo). Arming must be an event (once per
        # connection), never a heartbeat. The disconnect handler clears
        # the device so the next connection arms once again.
        if (device_id and device_id.startswith('d_')
                and device_id not in self._armed_devices):
            # ignore_debounce: at boot the best-effort connect arm fires
            # seconds before the first event — the debounce must not
            # swallow the one arm that latches.
            self.send_arm_protos(device_id, ignore_debounce=True, latch=True)
        # WAKE detection: sleep closes the capture gate; a PowerState
        # transition INTO RUNNING is the one-shot re-arm trigger. First
        # frame ever seen (prev None) never arms — the discovery arm above
        # owns that case.
        if eventname == 'zmq' and device_id and device_id.startswith('d_'):
            try:
                raw = msg.payload if isinstance(msg.payload, (bytes, bytearray)) else bytes(str(msg.payload), 'utf-8')
                state = parse_powerstate_state(bytes(raw))
                if state is not None:
                    prev = self._power_state.get(device_id)
                    self._power_state[device_id] = state
                    if state == POWER_RUNNING and prev is not None and prev != POWER_RUNNING:
                        # WAKE: same two-step as cold boot. Arming at the
                        # transition INSTANT proved too early (2026-08-29
                        # 00:16 live: two wake-arms sent, gate never opened,
                        # camera blind; an event-timed re-arm opened it in
                        # seconds). So: best-effort arm now (unlatched) and
                        # UNLATCH the device — the next perception event
                        # (vision subsystem provably up) sends the arm that
                        # counts via the discovery path.
                        logger.info('VISION-WAKE dev=%s power %s->RUNNING; best-effort arm + unlatch for event re-arm', device_id, prev)
                        self.send_arm_protos(device_id, ignore_debounce=True, latch=False)
                        self._armed_devices.discard(device_id)
            except Exception:
                logger.exception('powerstate wake check failed (ignored)')
        # Incoming JSON body is irrelevant for this branch — core does not read it.
        if not self.http_token_enabled:
            return
        if eventname == 'client-service-http-token':
            self.send_http_token(device_id)

    # ----- run loop -----

    def _resend_loop(self):
        """Daemon thread: every N seconds re-arm every armed-at-least-once device.

        Independent of the connect debounce — this is the explicit fallback
        in case a $SYS connect line was missed.
        """
        interval = self.resend_interval
        while not self._stop.wait(interval):
            for device_id in list(self._armed_devices):
                try:
                    self.send_arm_protos(device_id, ignore_debounce=True)
                except Exception:
                    logger.exception('resend arm failed for %s', device_id)

    def _newest_frame_age_from_globs(self):
        """Real newest-frame-age: glob configured patterns, stat mtimes.

        Never raises. No dir / no matching files → None (do nothing).
        """
        return newest_frame_age(self._frames_glob_patterns)

    def _stale_check_tick(self):
        """Apply one stale-frame check. File-stat is on a timer; arms are not.

        HARD INVARIANT: total arms sent per episode is ALWAYS <= 2 no matter
        how long the stall lasts; the ONLY way an episode's arm counter
        resets to 0 is observing frames actually flowing again
        (age <= STALE_AFTER_S from the real glob/mtime check) — a
        long-elapsed clock alone must NEVER reset it. Enforced by
        TestStaleFrameRearm.test_bound_holds_over_unbounded_stall and
        test_recovery_resets_only_on_real_frame_flow.

        Known accepted imprecision: frames also legitimately pause when
        nobody is engaging the robot (idle) and the sidecar cannot see
        engagement state, so these 2 bounded arms will also fire during
        ordinary idle periods. Harmless for now (send_arm_protos is
        idempotent and capped at 2/episode).
        TODO: replace this idle false-positive with the real gate-closed
        signal once the firmware lane identifies it — do not "fix" it now
        by weakening the flood bound.
        """
        try:
            age = self._newest_frame_age_fn()
        except Exception:
            return
        if age is None:
            return
        if age <= STALE_AFTER_S:
            # Frames flowing: reset EVERY tracked episode. Nothing is sent.
            for ep in self._stale_episodes.values():
                ep['arms'] = 0
                ep['ts'] = 0.0
                ep['gave_up_logged'] = False
            return
        # Stall episode in progress.
        devices = set(self._armed_devices) | set(self._power_state.keys())
        now = self._now()
        for device_id in devices:
            # Unknown / not in dict / any state other than RUNNING → never arm.
            if self._power_state.get(device_id) != POWER_RUNNING:
                continue
            episode = self._stale_episodes.get(device_id)
            if episode is None:
                episode = {'arms': 0, 'ts': 0.0, 'gave_up_logged': False}
                self._stale_episodes[device_id] = episode
            if episode['arms'] == 0:
                self.send_arm_protos(device_id, ignore_debounce=True, latch=True)
                episode['arms'] = 1
                episode['ts'] = now
                logger.info('VISION-STALE-REARM dev=%s attempt=1 age=%s', device_id, age)
            elif episode['arms'] == 1 and (now - episode['ts']) >= SECOND_ARM_AFTER_S:
                self.send_arm_protos(device_id, ignore_debounce=True, latch=True)
                episode['arms'] = 2
                episode['ts'] = now
                logger.info('VISION-STALE-REARM dev=%s attempt=2 age=%s', device_id, age)
            elif episode['arms'] >= 2:
                if not episode['gave_up_logged']:
                    logger.info('VISION-STALE-GIVEUP dev=%s', device_id)
                    episode['gave_up_logged'] = True
            # arms==1 but not yet SECOND_ARM_AFTER_S: do nothing, no log.

    def _stale_check_loop(self):
        """Daemon thread: poll frame mtimes every CHECK_PERIOD_S.

        The poll itself is a timer (reading files is fine). Any message
        sent to the robot is event-driven inside _stale_check_tick —
        never a heartbeat.
        """
        while not self._stop.wait(CHECK_PERIOD_S):
            try:
                self._stale_check_tick()
            except Exception:
                logger.exception('stale-check tick failed (ignored)')

    def _connect_broker(self):
        """Anonymous first, then username 'unknown' with no password.

        Local mosquitto is commonly allow_anonymous; some ACL configs are not.
        Documented in README: operator may need to add a broker user.
        """
        host, port = self.broker_host, self.broker_port
        try:
            logger.info('Connecting anonymously to %s:%s', host, port)
            self._client.connect(host, port, 60)
            return
        except Exception:
            logger.exception('Anonymous MQTT connect failed; retrying as username=unknown (no password)')
        self._tried_unknown_user = True
        self._client.username_pw_set(username='unknown', password=None)
        logger.info('Connecting as username=unknown (no password) to %s:%s', host, port)
        self._client.connect(host, port, 60)

    def run(self):
        """Subscribe + loop_forever. paho reconnects on its own after loss."""
        self._client.on_connect = self.on_connect
        self._client.on_message = self.on_message
        self._connect_broker()
        if self.resend_interval > 0:
            t = threading.Thread(
                target=self._resend_loop, name='vision-sidecar-resend', daemon=True
            )
            t.start()
            logger.info('Resend thread on, interval=%ss', self.resend_interval)
        # Stale-frame re-arm thread: ONLY if frames_glob was configured.
        # Empty glob → no thread, 100% unchanged from before this existed.
        if self._frames_glob_patterns:
            t = threading.Thread(
                target=self._stale_check_loop,
                name='vision-sidecar-stale-check',
                daemon=True,
            )
            self._stale_check_thread = t
            self._stale_check_started = True
            t.start()
            logger.info(
                'Stale-frame re-arm thread on (episode-bounded, max 2 arms/episode; glob=%s)',
                ','.join(self._frames_glob_patterns),
            )
        logger.info('Entering loop_forever (paho automatic reconnect)')
        self._client.loop_forever()


def _make_paho_client(client_id=None):
    """paho 1.x and 2.x: prefer VERSION1 callbacks to match OpenMoxie signatures.

    client_id is made UNIQUE per process (pid suffix). A FIXED id collides on
    the broker whenever the sidecar restarts (KeepAlive relaunch, or an operator
    re-run) — the old session lingers for the keepalive window and MQTT kicks
    one client for the other, producing a reconnect loop (observed 2026-08-28).
    A per-process id cannot collide with its own predecessor.
    """
    if client_id is None:
        client_id = 'openmoxie-vision-sidecar-%d' % os.getpid()
    kwargs = dict(client_id=client_id, transport='tcp')
    callback_api = getattr(mqtt, 'CallbackAPIVersion', None)
    if callback_api is not None:
        kwargs['callback_api_version'] = callback_api.VERSION1
    # paho 2.x: reconnect_on_failure defaults True; pass it when supported.
    try:
        client = mqtt.Client(reconnect_on_failure=True, **kwargs)
    except TypeError:
        client = mqtt.Client(**kwargs)
    # TLS is REQUIRED: OpenMoxie's mosquitto has a TLS listener on 8883
    # (local/mosquitto-openmoxie.conf) and its own client calls tls_set()
    # (moxie_server.py). Without this a plaintext socket never completes the
    # handshake — no CONNACK, on_connect never fires, the sidecar hangs silent
    # (observed 2026-08-27 smoke test). cert_reqs=CERT_NONE mirrors core's
    # not-cert-required branch: encrypt, don't verify the self-signed cert.
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    return client


def _env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Additive OpenMoxie vision sidecar (MQTT arm + optional http-token).'
    )
    parser.add_argument(
        '--broker-host',
        default=os.environ.get('VISION_SIDECAR_BROKER_HOST', 'localhost'),
        help='MQTT broker host (env VISION_SIDECAR_BROKER_HOST). Default: localhost',
    )
    parser.add_argument(
        '--broker-port',
        type=int,
        default=int(os.environ.get('VISION_SIDECAR_BROKER_PORT', '8883')),
        help='MQTT broker port (env VISION_SIDECAR_BROKER_PORT). Default: 8883',
    )
    # BooleanOptionalAction gives --http-token / --no-http-token. Default ON.
    default_http = _env_bool('VISION_SIDECAR_HTTP_TOKEN', default=True)
    parser.add_argument(
        '--http-token',
        dest='http_token',
        action=argparse.BooleanOptionalAction,
        default=default_http,
        help='Answer client-service-http-token events (default: on; env VISION_SIDECAR_HTTP_TOKEN).',
    )
    parser.add_argument(
        '--resend-interval',
        type=float,
        default=float(os.environ.get('VISION_SIDECAR_RESEND_INTERVAL', '0')),
        help='Seconds between fallback re-arms of every armed-at-least-once device. '
        '0 = only arm on a $SYS connect line (env VISION_SIDECAR_RESEND_INTERVAL).',
    )
    parser.add_argument(
        '--frames-glob',
        default=os.environ.get('VISION_SIDECAR_FRAMES_GLOB', ''),
        help='Comma-separated glob patterns whose file mtimes track received '
        'frames (any glob whose file mtimes track received frames; e.g. '
        '/path/fast_*.jpg,/path/frame_*.jpg). Empty string (default) disables '
        'the episode-bounded stale-frame re-arm entirely — opt-in only '
        '(env VISION_SIDECAR_FRAMES_GLOB).',
    )
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stdout,
    )
    args = parse_args(argv)
    client = _make_paho_client()
    sidecar = VisionSidecar(
        client=client,
        http_token_enabled=bool(args.http_token),
        resend_interval=args.resend_interval,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        frames_glob=args.frames_glob,
    )
    logger.info(
        'Starting vision-sidecar host=%s port=%s http_token=%s resend_interval=%s frames_glob=%s',
        args.broker_host,
        args.broker_port,
        args.http_token,
        args.resend_interval,
        args.frames_glob,
    )
    sidecar.run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info('Interrupted, exiting')
