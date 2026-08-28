#!/usr/bin/env python3
"""Unit tests for vision-sidecar. No live broker, no live Django DB.

Runnable as:
  /path/to/openmoxie/venv/bin/python -m unittest vision-sidecar/test_vision_sidecar.py -v
  /path/to/openmoxie/venv/bin/python -m pytest vision-sidecar/test_vision_sidecar.py -v
(pytest is optional; these are unittest.TestCase.)
"""
from __future__ import annotations

import json
import os
import sys
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
    """Records publish() calls. No network."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None):
        self.published.append((topic, payload))


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
        self.assertEqual(len(fake.published), 2, fake.published)
        sidecar.handle_message(msg)
        self.assertEqual(
            len(fake.published),
            2,
            'second connect within 10s must not send again; got %r' % (fake.published,),
        )
        # Both publishes are the zmq command topic, LoggingStateUpdate then EnableICModule.
        zmq_topic = f'/devices/{SYNTH_DEVICE_ID}/commands/zmq'
        self.assertEqual(fake.published[0][0], zmq_topic)
        self.assertEqual(fake.published[1][0], zmq_topic)
        self.assertTrue(
            fake.published[0][1].startswith(b'embodied.logging.LoggingStateUpdate:')
        )
        self.assertTrue(
            fake.published[1][1].startswith(b'embodied.robotbrain.EnableICModule:')
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
        self.assertEqual(len(fake.published), 4)


class TestHttpTokenReply(unittest.TestCase):
    def test_http_token_topic_and_payload_match_core(self):
        fake = FakeClient()
        sidecar = vs.VisionSidecar(client=fake, http_token_enabled=True)
        event_topic = f'/devices/{SYNTH_DEVICE_ID}/events/client-service-http-token'
        # Body is irrelevant — core does not read it for this branch.
        msg = FakeMsg(event_topic, json.dumps({'ignored': True}))
        sidecar.handle_message(msg)

        # Any event now also arms (2 protos) — device discovery for an
        # already-connected robot. The http_token reply must be present and
        # byte-correct among the publishes.
        token_pubs = [
            (t, p) for (t, p) in fake.published
            if t == f'/devices/{SYNTH_DEVICE_ID}/commands/http_token'
        ]
        self.assertEqual(len(token_pubs), 1, fake.published)
        topic, payload = token_pubs[0]
        expected = json.dumps({'command': 'http_token', 'http_token': 'notoken'})
        self.assertEqual(payload, expected)
        self.assertIsInstance(payload, str)
        # Exactly the two arm protos accompany it (arm-on-event discovery).
        arm_pubs = [
            (t, p) for (t, p) in fake.published
            if t == f'/devices/{SYNTH_DEVICE_ID}/commands/zmq'
        ]
        self.assertEqual(len(arm_pubs), 2, fake.published)

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
            topics.count(f'/devices/{SYNTH_DEVICE_ID}/commands/zmq'), 2, fake.published)

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
        self.assertEqual(len(fake.published), 2)  # best-effort connect arm (unlatched)
        sidecar.handle_message(
            FakeMsg(
                f'/devices/{SYNTH_DEVICE_ID}/events/client-service-http-token',
                '{}',
            )
        )
        # First event after the connect: the LATCHING arm (2, ignores the
        # connect arm's debounce — boot sequence) + the http-token reply.
        self.assertEqual(len(fake.published), 5)
        topic, payload = fake.published[4]
        self.assertEqual(topic, f'/devices/{SYNTH_DEVICE_ID}/commands/http_token')
        self.assertEqual(
            payload, json.dumps({'command': 'http_token', 'http_token': 'notoken'})
        )

    def test_events_arm_once_per_connection_epoch(self):
        """2026-08-28 incident regression: events must NEVER become an arm
        heartbeat. First event from an unarmed device arms (2 protos);
        subsequent events arm NOTHING, even far outside the 10s debounce —
        the repeated LoggingStateUpdate(STOP)+EnableICModule at ~10s cadence
        crash-looped the robot's XMOS audio and caused watchdog reboots.
        A disconnect line clears the flag so the NEXT connection arms once."""
        fake = FakeClient()
        clock = {'t': 100.0}
        sidecar = vs.VisionSidecar(
            client=fake, http_token_enabled=False,
            debounce_s=10.0, time_fn=lambda: clock['t'],
        )
        evt = f'/devices/{SYNTH_DEVICE_ID}/events/whatever'
        sidecar.handle_message(FakeMsg(evt, '{}'))
        self.assertEqual(len(fake.published), 2)  # armed once
        for step in (30.0, 120.0, 3600.0):  # far beyond any debounce
            clock['t'] = 100.0 + step
            sidecar.handle_message(FakeMsg(evt, '{}'))
        self.assertEqual(len(fake.published), 2)  # STILL exactly one arm
        # disconnect -> flag cleared -> next event arms exactly once again
        disc = f'Client {SYNTH_DEVICE_ID} closed its connection'
        sidecar.handle_message(FakeMsg('$SYS/broker/log/N', disc))
        clock['t'] = 4000.0
        sidecar.handle_message(FakeMsg(evt, '{}'))
        self.assertEqual(len(fake.published), 4)

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
        # NO wake arm (prev unknown): exactly 2 protos
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 2)
        # robot goes to sleep: no arm
        clock['t'] = 200.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(5)))
        self.assertEqual(len(fake.published), 2)
        # WAKE: SUSPEND -> RUNNING => exactly one re-arm (2 protos)
        clock['t'] = 300.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 4)
        # steady RUNNING frames: nothing
        clock['t'] = 400.0
        sidecar.handle_message(FakeMsg(zmq_topic, ps(3)))
        self.assertEqual(len(fake.published), 4)
        # parser: non-powerstate zmq payload is ignored quietly
        sidecar.handle_message(FakeMsg(zmq_topic, b'embodied.other.ThingPB:\x08\x01'))
        self.assertEqual(len(fake.published), 4)


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


if __name__ == '__main__':
    unittest.main()
