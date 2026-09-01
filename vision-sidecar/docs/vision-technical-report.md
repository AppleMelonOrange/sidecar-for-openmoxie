# Moxie Vision — Technical Report

Enabling Moxie's built‑in image‑captioning ("describe what you see") from a self‑hosted
OpenMoxie backend. Firmware **v24.10.803** (`v3.6.4-24_12_28-…-v24.10.803-rls-robot`), the
last official build. The capability ships in the firmware and is off by default; it is enabled
entirely with backend settings and messages — the robot is not modified. This documents what
the firmware contains, what gates the camera, and the exact working configuration, with log
evidence.

---

## 0. Result (the working recipe)

1. **Device config JSON** (`/devices/{id}/config`, parsed by the robot as `RobotCloudConfig`):
   add top‑level **`data_sharing: "full"`**; props `image_captioning:"1"`, `ic_by_rb:"1"`,
   `gcp_upload_disable:"0"`.
2. **HTTP‑token reply:** answer the robot's `client-service-http-token` MQTT event with
   `{command:"http_token", http_token:"notoken"}` (`_PROVIDE_HTTP_TOKENS=True`).
3. **Arm message on connect** (protobuf over `/devices/{id}/commands/zmq`, `"full_name:bytes"`):
   `embodied.robotbrain.EnableICModule{run=true}` — **alone**. The originally
   published recipe also sent `embodied.logging.LoggingStateUpdate{upload_policy=FULL(2)}`;
   that message restarts the robot's logging session, which the microphone's
   streaming‑STT channel rides on, so every arm silently broke Moxie's hearing.
   With `data_sharing:"full"` supplying the policy, `EnableICModule` alone opens the
   gate. See "The camera‑breaks‑hearing problem" in
   [enable-vision-step-by-step.md](enable-vision-step-by-step.md).
4. **DNS redirect** `production-ic-worker.embodied.com`, `staging-ic-worker.embodied.com`,
   `client-service-api.embodied.com` → your server.
5. **Caption server** on :443 answering `POST /api/v1/caption` (multipart JPEG in; caption JSON
   out; self‑signed cert OK, robot skips TLS verify) → any vision‑language model.

`data_sharing:"full"` is the decisive field; the rest is supporting. Everything below is why.

---

## 1. Architecture (from the firmware image)

Extracted with `7z` reading the ext4 `.img` directly; the real app is
`system/priv-app/bo-android/bo-android.apk` → native libs in `lib/armeabi-v7a/`.

- Board **RK3288**, Android 9. Face = Unity render → MIPI‑DSI → **TI DLPC3430** DLP projector
  (welded to board). Camera = **OV2710** MIPI‑CSI on the SoC ISP; firmware only ever emits a
  **320×180 JPEG**, never raw video.
- **Two separate OS processes, bridged only by an internal ZMQ bus** (`EventBroadcaster`):
  - `libbo-logger.so` — owns the MQTT link (bundled Paho MQTT C, Google Cloud IoT Core JWT
    topic conventions) and the cloud/logging policy.
  - `libbo-vision.so` — the vision `MainLoop` and captioning.
  They are unlinked; nothing calls directly between them.

## 2. The native capture gate (`libbo-vision.so`, ARM Thumb‑2)

The one and only call into the capture task `ImageToTextLongRunning` sits in
`MainLoop::operator()` (**0x323418**), guarded at **0x32357e–0x3235a0**:

```
if (MainLoop+0x101d != 0  &&  MainLoop+0x101e != 0)   RunWorker(frame)   // strict AND
```

- **+0x101d** written only by `SetImageCaptioning` (**0x322a6c**): set to 1 iff
  `(config image_captioning != 0) AND (MainLoop+0xfc8 != 0)`, else forced to 0.
- **+0xfc8** written **only** by `OnLoggingStateChanged` (**0x322a04**):
  `+0xfc8 = (proto field @ native offset 0x2c == 2)` — code `subs #2; clz; lsr #5`.
- **+0x101e** = the `ic_by_rb` byte, written by `SetICState` (**0x323aa8**) / `OnEnableIC`.

Which field is at offset 0x2c: from `LoggingStateUpdate::Clear` (**0x36d140**) and
`_InternalParse` (**0x36d198**) the layout is 6 string pointers @0x08–0x1f, then
`timestamp`@0x20, `state`@0x28, **`upload_policy`@0x2c** (parser tag `0x38` = field 7 →
`str [r5,#0x2c]`). So the gate byte tests **`upload_policy == FULL(2)`**.

Proto field numbers (from `Embodied.Protos`): `LoggingStateUpdate{state=1, path=2, uuid=3,
timestamp=4, user_uuid=5, session_uuid=6, upload_policy=7, software_version=100, module_name=101}`;
`EnableICModule{timestamp=1, run=2(bool), software_version=100, module_name=101}`.

`EnableICModule` reaches `Service::StateChangeListeners::OnEnableIC` (**0x33bc20**) via a generic
`ProtoEventArgs<T>::SubscriberCallback(string) → Core::Event::Fire` dispatch table shared by ~12
proto types — i.e. it's deliverable over the same `/commands/zmq` `full_name:bytes` channel as STT.

**Sending both arm‑protos still did not turn the camera on** → the block was upstream.

## 3. The real blocker: cloud config / data‑sharing policy (`libbo-logger.so`)

`embodied::logging::cloud::RightPoint` owns the policy. `GetLoggingPolicy` (**0x471ee8**) reads a
string at `RightPoint+0x3ac`: **`"full"`→FULL(2)**, else `"no_data"`→NO_DATA(1) (rodata literal
`full\0no_data\0`), gated by an enable byte `+0x674`. That policy comes from a **cloud config**
the robot expects from the backend; without it, the robot logs:

```
Config requested - No cloud configuration available
Making AUID-pending as cloud_config not yet valid.
Configured for no cloud data management.  Skipping restore.
```

→ policy stays **NO_DATA** → `+0xfc8` never set → gate shut. This is why config flags + protos
alone never worked.

The cloud config is `embodied.logging.RobotCloudConfig` — **the `/config` JSON OpenMoxie already
sends** (it carries audio_volume, screen_brightness, child_pii, settings/DeviceSettings,
timezone_id, wake_button_enabled …). It was missing exactly one field: **`data_sharing`
(field 16, string)**. Adding `data_sharing:"full"` set the policy to FULL.

(Related: `ServiceConfiguration2` from the pairing QR carries `webservice_root` /
`disable_log_upload` / `endpoint_id`; the `IOTEndpoint` enum includes **`OPEN_MOXIE=11`**; the
robot also accepts an `endpoint_update` MQTT command carrying a `cloud_json` field.)

## 4. The upload/token handshake (observed via EBUploader logs)

With `gcp_upload_disable=0`, the robot's `EBUploader` starts and its error progressed as each
piece was supplied — useful confirmation the subsystem was engaging:

```
gcs upload failed: Timed out requesting token over MQTT   (no http_token reply)
  → (after _PROVIDE_HTTP_TOKENS=True) …
gcs upload failed: error performing small-upload: result 6, bad status code 0   (couldn't resolve host)
gcs upload failed: error getting session-url                                     (no client-service backend)
```

These GCS uploads are **telemetry**, separate from the caption path; they don't need to succeed
for captioning. But they confirmed the token event and policy wiring were live.

## 5. The caption POST (`libbo-vision.so`, libcurl — not the C# app)

`ImageToTextClient::GenerateText` (**0x31f20c**) builds a **libcurl** multipart POST to a
**hardcoded** `https://production-ic-worker.embodied.com/api/v1/caption` (staging variant present),
with headers `Authorization: Bearer <hardcoded rodata key>` (prod `<hardcoded-key>`, staging
`<hardcoded-key>`) and form fields `question, prompt, session-id, center-x, center-y, width, height,
is-mentor, model` + the JPEG file. The Bearer value is a **static compiled‑in key, not a runtime
token**. `client-service-http-token` appears in **no** native lib — it's a C#/upload‑handshake
concept, unrelated to the caption auth. Because the URL is hardcoded, DNS redirect is the only
intercept.

## 6. Success (log evidence)

On connect the backend logs the arm messages (this capture predates the drop of
`LoggingStateUpdate` — see §0 step 3), then real frames arrive with the hardcoded key
(varying sizes = a real camera, not a fixed test image):

```
INFO Opening image-captioning gate (LoggingStateUpdate) for d_xxxx…
INFO Enabling IC module (EnableICModule run=True) for d_xxxx…
POST /api/v1/caption (27497B) ctype=multipart/form-data auth=Bearer <hardcoded-key>...
POST /api/v1/caption (28547B) ... (≈1/sec, 22–30 KB each)
  -> caption: I see a person with glasses and a white shirt, sitting at a desk with a computer
              and a wall-mounted speaker.
```

A saved frame was verified to be the actual room. Multipart note: parse the JPEG **by its
`FF D8 … FF D9` markers** — a stock `cgi.FieldStorage` parser rejected the robot's real multipart
(0 of 500 parsed) until switched to marker extraction.

## 7. Application layer

- **"What do you see?"** — a METHOD global‑response queries the vision model with the user's actual
  question against the newest frame.
- **Vision in normal conversation** — `OPENMOXIE_CHAT` pre_process injects the current caption every
  turn (ambient, read free from the caption log) and, for any visual‑sounding turn, asks the vision
  model the user's exact words for a frame‑specific answer; the prompt forbids "camera's blurry"
  deflection; freshness‑gated ≤20 s.
- **Lifecycle** `moxie-ctl.sh` — buddy‑wake is a single `GET /hive/moxie_wake/<pk>`
  (nothing before it); optional brightness/volume and `image_captioning` are
  separate config pushes after that, not part of waking. Teacher‑wake is
  `command=enable` then `GET moxie_wake`. There is **no software sleep call** —
  use the robot's schedule/inactivity or the physical power switch; never fake
  sleep by chaining enable/disable. Stuck puppet: `enable` then `disable` (the
  UI's own Start‑then‑Stop); `puppet_state` is a one‑way robot report and is
  never force‑reset by the server.
- **Super‑resolution** (`mfsr.py`, run with `/opt/homebrew/bin/python3` for cv2) — iterative
  back‑projection sharpens **static** scene detail (960×540 from a 12‑frame burst); moving subjects
  ghost at ~1 fps (needs a millisecond‑apart burst).

## 8. Behaviour & constraints

- **Resolution** 320×180 JPEG, ~22–30 KB. **Rate** ~1/sec (robot self‑paces to the server's reply;
  instant reply → near‑continuous stream).
- Captioning runs only when **awake + buddy mode + engaged**; it **pauses when idle** (no empty‑room
  capture) and does **not** run in puppet/teacher mode.
- The camera never streams video or high‑res; only these gated stills leave the robot. A full
  MQTT payload capture during active face‑search showed **zero** vision/image protos on the wire —
  only symbolic results (face found/lost, QR string, marker id, book name) return as chat
  `input_vars`.
- **STT ("ears") is fragile**: it drops out across server restarts and mode transitions, so frequent
  reloads visibly worsen it; a restart re‑handshakes it.

## 9. Firmware coordinate index

| what | symbol / address |
|---|---|
| capture gate (AND) | `MainLoop::operator()` **0x323418**, test at **0x32357e–0x3235a0** |
| image_captioning flag | `SetImageCaptioning` **0x322a6c** → `MainLoop+0x101d` |
| media‑allowed byte | `OnLoggingStateChanged` **0x322a04** → `MainLoop+0xfc8` (upload_policy==2) |
| ic_by_rb byte | `SetICState` **0x323aa8** → `MainLoop+0x101e` |
| enable‑IC event | `OnEnableIC` **0x33bc20** (reads `run_`) |
| LoggingStateUpdate layout | `Clear` **0x36d140**, `_InternalParse` **0x36d198** (upload_policy@0x2c) |
| logging policy string | `RightPoint::GetLoggingPolicy` **0x471ee8** (`+0x3ac` "full"/"no_data", enable `+0x674`) |
| caption HTTP POST | `ImageToTextClient::GenerateText` **0x31f20c** (libcurl, hardcoded URL+key) |
| cloud config message | `embodied.logging.RobotCloudConfig`, `data_sharing` = field 16 |

*Everything above is enabling a feature already present in v24.10.803 from the backend — no robot
modification.*
