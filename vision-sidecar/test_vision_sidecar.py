#!/usr/bin/env python3
"""Unit tests for vision-sidecar. No live broker, no live Django DB.

Runnable as:
  /path/to/openmoxie/venv/bin/python -m unittest vision-sidecar/test_vision_sidecar.py -v
  /path/to/openmoxie/venv/bin/python -m pytest vision-sidecar/test_vision_sidecar.py -v
(pytest is optional; these are unittest.TestCase.)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import vision_sidecar as vs  # noqa: E402
from embodied.logging.LoggingStateUpdate_pb2 import LoggingStateUpdate  # noqa: E402
from embodied.robotbrain.EnableICModule_pb2 import EnableICModule  # noqa: E402


# Synthetic device id — UUID-shaped so the regex has something realistic to
# chew on. Not a real robot id.
SYNTH_DEVICE_ID = 'd_00000000-0000-4000-8000-000000000001'

# Realistic mosquitto $SYS/broker/log/N line (notice). Timestamp prefix +
# "New client connected from <ip:port> as <clientid> (p2, c1, k60)."
SYNTH_CONNECT_LINE = (
    '1740000000: New client connected from 127.0.0.1:54321 as '
    + SYNTH_DEVICE_ID
    + ' (p2, c1, k60).'
)


class FakeClient:
    """Records publish() calls. No network.

    connect / loop_forever / username_pw_set are no-ops so tests can call
    VisionSidecar.run() without blocking on a real broker.
    """

    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None):
        self.published.append((topic, payload))

    def connect(self, host, port, keepalive=60):
        return 0

    def username_pw_set(self, username=None, password=None):
        pass

    def loop_forever(self):
        return


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        if isinstance(payload, bytes):
            self.payload = payload
        else:
            self.payload = payload.encode('utf-8')


class TestConnectRegex(unittest.TestCase):
    def test_extracts_device_id_from_broker_log_line(self):
        got = vs.extract_connect_device_id(SYNTH_CONNECT_LINE)
        self.assertEqual(got, SYNTH_DEVICE_ID)

    def test_non_connect_line_returns_none(self):
        line = '1740000000: Sending CONNACK to some-other-client (0, 0)'
        self.assertIsNone(vs.extract_connect_device_id(line))


class TestArmProtoFraming(unittest.TestCase):
    def test_logging_state_update_frame_and_roundtrip(self):
        # Wire-format check only. LoggingStateUpdate is NEVER sent by the arm
        # path anymore (it kills the mic's STT stream — see send_arm_protos);
        # the builder is kept as documentation of the old gate mechanism.
        ts = 1_740_000_000_000
        lsu = vs.build_logging_state_update(timestamp_ms=ts)
        raw = vs.frame_zmq_payload(lsu)
        prefix = b'embodied.logging.LoggingStateUpdate:'
        self.assertTrue(raw.startswith(prefix), raw[:80])
        self.assertEqual(raw, prefix + lsu.SerializeToString())
        parsed = LoggingStateUpdate()
        parsed.ParseFromString(raw[len(prefix):])
        self.assertEqual(parsed.state, 2)
        self.assertEqual(parsed.upload_policy, 2)
        self.assertEqual(parsed.timestamp, ts)

    def test_enable_ic_module_frame_and_roundtrip(self):
        ts = 1_740_000_000_001
        eic = vs.build_enable_ic_module(run=True, timestamp_ms=ts)
        raw = vs.frame_zmq_payload(eic)
        prefix = b'embodied.robotbrain.EnableICModule:'
        self.assertTrue(raw.startswith(prefix), raw[:80])
        self.assertEqual(raw, prefix + eic.SerializeToString())
        parsed = EnableICModule()
        parsed.ParseFromString(raw[len(prefix):])
        self.assertEqual(parsed.run, True)
        self.assertEqual(parsed.timestamp, ts)


class TestArmDebounce(unittest.TestCase):
    def test_second_arm_within_10s_does_not_publish(self):
        fake = FakeClient()
        clock = {'t': 100.0}

        sidecar = vs.VisionSidecar(
            client=fake,
            http_token_enabled=False,
            debounce_s=10.0,
            time_fn=lambda: clock['t'],
        )
        topic = f'$SYS/broker/log/N'
        msg = FakeMsg(topic, SYNTH_CONNECT_LINE)

        sidecar.handle_message(msg)
        self.assertEqual(len(fake.published), 1, fake.published)
        sidecar.handle_message(msg)
        self.assertEqual(
            len(fake.published),
            1,
            'second connect within 10s must not send again; got %r' % (fake.published,),
        )
        # The single publish is EnableICModule on the zmq command topic — the
        # polite arm. A LoggingStateUpdate here would break the robot's hearing.
        zmq_topic = f'/devices/{SYNTH_DEVICE_ID}/commands/zmq'
        self.assertEqual(fake.published[0][0], zmq_topic)
        self.assertTrue(
            fake.published[0][1].startswith(b'embodied.robotbrain.EnableICModule:')
        )
        for _t, payload in fake.published:
            self.assertFalse(
                payload.startswith(b'embodied.logging.LoggingStateUpdate:'),
                'LoggingStateUpdate must never be published by an arm',
            )

    def test_arm_after_debounce_window_sends_again(self):
        fake = FakeClient()
        clock = {'t': 100.0}
        sidecar = vs.VisionSidecar(
            client=fake,
            http_token_enabled=False,
            debounce_s=10.0,
            time_fn=lambda: clock['t'],
        )
        msg = FakeMsg('$SYS/broker/log/N', SYNTH_CONNECT_LINE)
        sidecar.handle_message(msg)
        clock['t'] = 110.1
        sidecar.handle_message(msg)
        self.assertEqual(len(fake.published), 2)


class TestHttpTokenReply(unittest.TestCase):
    def test_http_token_topic_and_payload_match_core(self):
        fake = FakeClient()
        sidecar = vs.VisionSidecar(client=fake, http_token_enabled=True)
        event_topic = f'/devices/{SYNTH_DEVICE_ID}/events/client-service-http-token'
        # Body is irrelevant — core does not read it for this branch.
        msg = FakeMsg(event_topic, json.dumps({'ignored': True}))
        sidecar.handle_message(msg)

        # Any event now also arms (1 proto, the polite arm) — device discovery
        # for an already-connected robot. The http_token reply must be present
        # and byte-correct among the publishes.
        token_pubs = [
            (t, p) for (t, p) in fake.published
            if t == f'/devices/{SYNTH_DEVICE_ID}/commands/http_token'
        ]
        self.assertEqual(len(token_pubs), 1, fake.published)
        topic, payload = token_pubs[0]
        expected = json.dumps({'command': 'http_token', 'http_token': 'notoken'})
        self.assertEqual(payload, expected)
        self.assertIsInstance(payload, str)
        # Exactly one arm proto accompanies it (arm-on-event discovery).
        arm_pubs = [
            (t, p) for (t, p) in fake.published
            if t == f'/devices/{SYNTH_DEVICE_ID}/commands/zmq'
        ]
        self.assertEqual(len(arm_pubs), 1, fake.published)

    def test_http_token_disabled_still_arms_but_sends_no_token(self):
        # http-token OFF: an event must NOT publish a token, but MUST still
        # arm (arm-on-event device discovery is independent of the token flag).
        fake = FakeClient()
        sidecar = vs.VisionSidecar(client=fake, http_token_enabled=False)
        event_topic = f'/devices/{SYNTH_DEVICE_ID}/events/client-service-http-token'
        sidecar.handle_message(FakeMsg(event_topic, '{}'))
        topics = [t for (t, _p) in fake.published]
        self.assertNotIn(f'/devices/{SYNTH_DEVICE_ID}/commands/http_token', topics)
        self.assertEqual(
            topics.count(f'/devices/{SYNTH_DEVICE_ID}/commands/zmq'), 1, fake.published)

    def test_http_token_debounce_is_independent_of_arm(self):
        fake = FakeClient()
        clock = {'t': 50.0}
        sidecar = vs.VisionSidecar(
            client=fake,
            http_token_enabled=True,
            debounce_s=10.0,
            time_fn=lambda: clock['t'],
        )
        sidecar.handle_message(FakeMsg('$SYS/broker/log/N', SYNTH_CONNECT_LINE))
        self.assertEqual(len(fake.published), 1)  # best-effort connect arm (unlatched)
        sidecar.handle_message(
            FakeMsg(
                f'/devices/{SYNTH_DEVICE_ID}/events/client-service-http-token',
                '{}',
            )
        )
        # First event after the connect: the LATCHING arm (1 proto, ignores the
        # connect arm's debounce — boot sequence) + the http-token reply.
        self.assertEqual(len(fake.published), 3)
        topic, payload = fake.published[2]
        self.assertEqual(topic, f'/devices/{SYNTH_DEVICE_ID}/commands/http_token')
        self.assertEqual(
            payload, json.dumps({'command': 'http_token', 'http_token': 'notoken'})
        )

    def test_events_arm_once_per_connection_epoch(self):
        """2026-08-28 incident regression: events must NEVER become an arm
        heartbeat. First event from an unarmed device arms (1 proto);
        subsequent events arm NOTHING, even far outside the 10s debounce —
        the repeated arm at ~10s cadence (which then still carried
        LoggingStateUpdate) crash-looped the robot's XMOS audio and caused
        watchdog reboots.
        A disconnect line clears the flag so the NEXT connection arms once."""
        fake = FakeClient()
        clock = {'t': 100.0}
        sidecar = vs.VisionSidecar(
            client=fake, http_token_enabled=False,
            debounce_s=10.0, time_fn=lambda: clock['t'],
        )
        evt = f'/devices/{SYNTH_DEVICE_ID}/events/whatever'
        sidecar.handle_message(FakeMsg(evt, '{}'))
        self.assertEqual(len(fake.published), 1)  # armed once
        for step in (30.0, 120.0, 3600.0):  # far beyond any debounce
            clock['t'] = 100.0 + step
            sidecar.handle_message(FakeMsg(evt, '{}'))
        self.assertEqual(len(fake.published), 1)  # STILL exactly one arm
        # disconnect -> flag cleared -> next event arms exactly once again
        disc = f'Client {SYNTH_DEVICE_ID} closed its connection'
        sidecar.handle_message(FakeMsg('$SYS/broker/log/N', disc))
        clock['t'] = 4000.0
        sidecar.handle_message(FakeMsg(evt, '{}'))
        self.assertEqual(len(fake.published), 2)

    def test_wake_transition_rearms_once(self):
        """Sleep closes the capture gate (2026-08-28 live: blind after nap).
        PowerState transition INTO RUNNING = one re-arm. First-seen frame
        never wake-arms; sleep-entry never arms; repeat RUNNING never arms."""
        def ps(state, prev=0):
            body = bytes([0x08, 0x01, 0x10, state, 0x18, prev])
            return vs.POWERSTATE_PREFIX + body
        fake = FakeClient()
        clock = {'t': 100.0}
        sidecar = vs.VisionSidecar(
            client=fake, http_token_enabled=False,
            debounce_s=10.0, time_fn=lambda: clock['t'],
        )
        zmq_topic = f'/devices/{SYNTH_DEVICE_ID}/events/zmq'
        # first frame: RUNNING — discovery arm fires (unarmed device), but
        # NO wake arm (prev unknown): exactly 1 proto
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 1)
        # robot goes to sleep: no arm
        clock['t'] = 200.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(5)))
        self.assertEqual(len(fake.published), 1)
        # WAKE: SUSPEND -> RUNNING => best-effort arm now (1 proto) and
        # the device is UNLATCHED — the discovery check already ran for
        # this message, so the latching arm fires on the NEXT event, when
        # the vision subsystem has had strictly more time to come up.
        # (2026-08-29 00:16 live: arming only at the transition instant was
        # too early — gate never opened; the event-timed arm opened it.)
        clock['t'] = 300.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 2)
        # next event after wake: the LATCHING discovery arm (+1)
        clock['t'] = 400.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 3)
        # steady RUNNING frames after that: nothing further
        clock['t'] = 500.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 3)
        # parser: non-powerstate zmq payload is ignored quietly
        sidecar.handle_message(FakeMsg(zmq_topic, b'embodied.other.ThingPB:\x08\x01'))
        self.assertEqual(len(fake.published), 3)


class TestMergeVisionConfig(unittest.TestCase):
    """Pure-function tests of apply_vision_config.merge — no Django."""

    def test_preserves_unrelated_keys(self):
        # Import without running main() (django.setup lives under main).
        import apply_vision_config as avc

        config = {'pairing_status': 'paired', 'audio_volume': '0.6'}
        settings = {'props': {'touch_wake': '1', 'local_stt': 'on'}}
        new_c, new_s = avc.merge_vision_config(config, settings)
        self.assertEqual(new_c['pairing_status'], 'paired')
        self.assertEqual(new_c['audio_volume'], '0.6')
        self.assertEqual(new_c['data_sharing'], 'full')
        self.assertEqual(new_s['props']['touch_wake'], '1')
        self.assertEqual(new_s['props']['local_stt'], 'on')
        self.assertEqual(new_s['props']['image_captioning'], '1')
        self.assertEqual(new_s['props']['ic_by_rb'], '1')
        self.assertEqual(new_s['props']['gcp_upload_disable'], '0')
        # Originals not mutated.
        self.assertNotIn('data_sharing', config)
        self.assertNotIn('image_captioning', settings['props'])

    def test_idempotent_on_already_set(self):
        import apply_vision_config as avc

        config = {'data_sharing': 'full'}
        settings = {
            'props': {
                'image_captioning': '1',
                'ic_by_rb': '1',
                'gcp_upload_disable': '0',
            }
        }
        new_c, new_s = avc.merge_vision_config(config, settings)
        self.assertEqual(new_c, config)
        self.assertEqual(new_s, settings)


# ---------------------------------------------------------------------------
# Episode-bounded stale-frame re-arm
#
# Tests 1-5 call _stale_check_tick() directly: the flood bound is a pure
# state-machine invariant (age + clock + power), and driving the tick
# method with a fake clock + injected age fn proves it without a 30s
# sleep or a real frames dir. Test 6 (and the feature-on counterpart)
# exercise run()'s thread-start gating, which is the other half of
# "opt-in, otherwise 100% unchanged".
# ---------------------------------------------------------------------------

STALE_THREAD_NAME = 'vision-sidecar-stale-check'
POWER_SUSPEND = 5  # firmware PowerStatePB.state; sidecar only names POWER_RUNNING


def _zmq_count(fake, device_id=SYNTH_DEVICE_ID):
    topic = f'/devices/{device_id}/commands/zmq'
    return sum(1 for (t, _p) in fake.published if t == topic)


class _LogList(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestStaleFrameRearm(unittest.TestCase):
    """Flood-safety: at most 2 arms per stall episode, reset only on real frame flow."""

    def _sidecar(self, age_fn, clock, frames_glob='/x/fast_*.jpg'):
        fake = FakeClient()
        sidecar = vs.VisionSidecar(
            client=fake,
            http_token_enabled=False,
            debounce_s=10.0,
            time_fn=lambda: clock['t'],
            frames_glob=frames_glob,
            newest_frame_age_fn=age_fn,
        )
        return sidecar, fake

    def _running(self, sidecar, device_id=SYNTH_DEVICE_ID):
        sidecar._armed_devices.add(device_id)
        sidecar._power_state[device_id] = vs.POWER_RUNNING

    def _capture_logs(self):
        handler = _LogList()
        log = logging.getLogger('vision_sidecar')
        prev_level = log.level
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        self.addCleanup(lambda: (log.removeHandler(handler), log.setLevel(prev_level)))
        return handler

    def test_bound_holds_over_unbounded_stall(self):
        """THE BOUND: 100 ticks, always-stale, hours between ticks → exactly 2 arms.

        Always-stale age of 999999, fake clock +3600s per tick, device RUNNING.
        Exactly 2 raw zmq publishes (1 proto × 2 arms), exactly one
        VISION-STALE-GIVEUP, and nothing further across the remaining ticks.
        A long-elapsed clock alone must NEVER reset the episode.
        """
        clock = {'t': 0.0}
        sidecar, fake = self._sidecar(lambda: 999999, clock)
        self._running(sidecar)
        logs = self._capture_logs()

        for _ in range(100):
            sidecar._stale_check_tick()
            clock['t'] += 3600.0

        self.assertEqual(
            _zmq_count(fake),
            2,
            'expected exactly 2 arms (2 zmq publishes) over 100 stale hours; got %r'
            % (fake.published,),
        )
        giveups = [
            m for m in logs.messages
            if 'VISION-STALE-GIVEUP' in m and SYNTH_DEVICE_ID in m
        ]
        self.assertEqual(
            len(giveups),
            1,
            'expected exactly one GIVEUP log; got %r' % (logs.messages,),
        )
        rearms = [m for m in logs.messages if 'VISION-STALE-REARM' in m]
        self.assertEqual(len(rearms), 2, rearms)
        self.assertEqual(sidecar._stale_episodes[SYNTH_DEVICE_ID]['arms'], 2)

    def test_recovery_resets_only_on_real_frame_flow(self):
        """Episode resets only when age drops; elapsed wall-clock alone does not.

        Sub-case: after 2 arms, a tick with huge elapsed time but age STILL
        stale must not reset and must not send a 3rd arm. Then one recovery
        tick (age=10) resets arms to 0. Then stall again → exactly 2 more
        arms (2 more publishes), proving the reset re-enabled the bound.
        """
        clock = {'t': 0.0}
        age = {'v': 999999}
        sidecar, fake = self._sidecar(lambda: age['v'], clock)
        self._running(sidecar)

        sidecar._stale_check_tick()          # arm 1
        clock['t'] += 3600.0
        sidecar._stale_check_tick()          # arm 2
        self.assertEqual(_zmq_count(fake), 2)
        self.assertEqual(sidecar._stale_episodes[SYNTH_DEVICE_ID]['arms'], 2)

        # Huge elapsed time, still stale: must NOT reset, must NOT send a 3rd arm.
        clock['t'] += 10_000_000.0
        sidecar._stale_check_tick()
        self.assertEqual(
            _zmq_count(fake),
            2,
            'elapsed wall-clock alone must not send a 3rd arm; got %r' % (fake.published,),
        )
        self.assertEqual(
            sidecar._stale_episodes[SYNTH_DEVICE_ID]['arms'],
            2,
            'elapsed wall-clock alone must not reset the episode',
        )

        # Real frame flow: age <= STALE_AFTER_S.
        age['v'] = 10
        sidecar._stale_check_tick()
        self.assertEqual(sidecar._stale_episodes[SYNTH_DEVICE_ID]['arms'], 0)
        self.assertFalse(sidecar._stale_episodes[SYNTH_DEVICE_ID]['gave_up_logged'])
        self.assertEqual(_zmq_count(fake), 2, 'recovery tick must send nothing')

        # Stall again: bound re-enabled, exactly 2 more arms.
        age['v'] = 999999
        sidecar._stale_check_tick()          # arm 1 of new episode
        clock['t'] += 3600.0
        sidecar._stale_check_tick()          # arm 2 of new episode
        self.assertEqual(_zmq_count(fake), 4)
        self.assertEqual(sidecar._stale_episodes[SYNTH_DEVICE_ID]['arms'], 2)

    def test_frames_flowing_sends_nothing(self):
        """Age always well under STALE_AFTER_S → zero publishes across many ticks."""
        clock = {'t': 0.0}
        sidecar, fake = self._sidecar(lambda: 10, clock)
        self._running(sidecar)
        for _ in range(50):
            sidecar._stale_check_tick()
            clock['t'] += 3600.0
        self.assertEqual(len(fake.published), 0, fake.published)

    def test_power_gating_suspend_and_unknown(self):
        """SUSPEND and unknown power → zero arms; RUNNING mid-episode still bound to 2."""
        clock = {'t': 0.0}
        sidecar, fake = self._sidecar(lambda: 999999, clock)

        # Unknown / absent: device in the armed set but not in _power_state.
        sidecar._armed_devices.add(SYNTH_DEVICE_ID)
        for _ in range(5):
            sidecar._stale_check_tick()
            clock['t'] += 3600.0
        self.assertEqual(len(fake.published), 0, fake.published)

        # SUSPEND: known, but not RUNNING.
        sidecar._power_state[SYNTH_DEVICE_ID] = POWER_SUSPEND
        for _ in range(5):
            sidecar._stale_check_tick()
            clock['t'] += 3600.0
        self.assertEqual(len(fake.published), 0, fake.published)

        # Flip to RUNNING mid-episode: arms begin, still bounded to exactly 2
        # even though many stale ticks preceded the flip.
        sidecar._power_state[SYNTH_DEVICE_ID] = vs.POWER_RUNNING
        for _ in range(20):
            sidecar._stale_check_tick()
            clock['t'] += 3600.0
        self.assertEqual(
            _zmq_count(fake),
            2,
            'after RUNNING flip, still exactly 2 arms; got %r' % (fake.published,),
        )

    def test_age_none_is_silent(self):
        """No caption server / no frames dir: zero publishes and zero STALE logs."""
        clock = {'t': 0.0}
        sidecar, fake = self._sidecar(lambda: None, clock)
        self._running(sidecar)
        logs = self._capture_logs()
        for _ in range(20):
            sidecar._stale_check_tick()
            clock['t'] += 3600.0
        self.assertEqual(len(fake.published), 0, fake.published)
        stale_logs = [
            m for m in logs.messages
            if 'VISION-STALE-REARM' in m or 'VISION-STALE-GIVEUP' in m
        ]
        self.assertEqual(stale_logs, [], 'age=None must not log-spam; got %r' % (logs.messages,))

    def test_feature_off_does_not_start_thread(self):
        """Empty/default frames_glob: run() must not start the stale-check thread."""
        fake = FakeClient()
        sidecar = vs.VisionSidecar(
            client=fake,
            http_token_enabled=False,
            frames_glob='',
        )
        sidecar.run()
        self.assertFalse(
            getattr(sidecar, '_stale_check_started', False),
            'stale-check thread must not start when frames_glob is empty',
        )
        self.assertIsNone(getattr(sidecar, '_stale_check_thread', None))
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn(STALE_THREAD_NAME, names)

    def test_feature_on_starts_stale_thread(self):
        """Non-empty frames_glob: run() starts the named daemon thread.

        Completes the gating proof for test_feature_off_does_not_start_thread.
        Stop the thread immediately so it cannot tick during other tests.
        """
        fake = FakeClient()
        sidecar = vs.VisionSidecar(
            client=fake,
            http_token_enabled=False,
            frames_glob='/x/fast_*.jpg',
            newest_frame_age_fn=lambda: None,
        )
        sidecar.run()
        try:
            self.assertTrue(sidecar._stale_check_started)
            self.assertIsNotNone(sidecar._stale_check_thread)
            self.assertEqual(sidecar._stale_check_thread.name, STALE_THREAD_NAME)
            self.assertTrue(sidecar._stale_check_thread.daemon)
            names = [t.name for t in threading.enumerate()]
            self.assertIn(STALE_THREAD_NAME, names)
        finally:
            sidecar._stop.set()
            sidecar._stale_check_thread.join(timeout=1.0)


class TestConfigPushRearm(unittest.TestCase):
    """Issue #3: a /devices/{id}/config push (server restart / admin save)
    must schedule ONE delayed best-effort arm, unlatch, and let the next
    perception event send the latching arm — never more than 2 arms per push."""

    def _zmq_arms(self, fake):
        return [t for t, _ in fake.published if t.endswith('/commands/zmq')]

    def test_config_push_rearms_after_delay_then_latches_once(self):
        fake = FakeClient()
        sidecar = vs.VisionSidecar(client=fake, http_token_enabled=False, debounce_s=0.0)
        dev = 'd_test-config'
        old_delay = vs.CONFIG_REARM_DELAY_S
        vs.CONFIG_REARM_DELAY_S = 0.2
        try:
            sidecar.handle_message(FakeMsg(f'/devices/{dev}/events/perception', '{}'))
            self.assertEqual(len(self._zmq_arms(fake)), 1)          # first-event latch
            sidecar.handle_message(FakeMsg(f'/devices/{dev}/config', '{}'))
            sidecar.handle_message(FakeMsg(f'/devices/{dev}/events/perception', '{}'))
            self.assertEqual(len(self._zmq_arms(fake)), 1)          # nothing before the delay
            sidecar._config_timers[dev].join(timeout=2.0)
            self.assertEqual(len(self._zmq_arms(fake)), 2)          # delayed best-effort arm
            self.assertNotIn(dev, sidecar._armed_devices)           # unlatched
            sidecar.handle_message(FakeMsg(f'/devices/{dev}/events/perception', '{}'))
            self.assertEqual(len(self._zmq_arms(fake)), 3)          # next event latches
            sidecar.handle_message(FakeMsg(f'/devices/{dev}/events/perception', '{}'))
            self.assertEqual(len(self._zmq_arms(fake)), 3)          # and no more
        finally:
            vs.CONFIG_REARM_DELAY_S = old_delay

    def test_config_push_for_unarmed_device_is_ignored(self):
        fake = FakeClient()
        sidecar = vs.VisionSidecar(client=fake, http_token_enabled=False, debounce_s=0.0)
        sidecar.handle_message(FakeMsg('/devices/d_never-armed/config', '{}'))
        self.assertEqual(sidecar._config_timers, {})
        self.assertEqual(self._zmq_arms(fake), [])


if __name__ == '__main__':
    unittest.main()
