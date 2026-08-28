# SideCar for OpenMoxie

Additive add-ons that run **beside** a stock OpenMoxie install to bring a discontinued
Moxie robot back to life — enabling features the robot **already ships**, on the
owner's own hardware, privacy-first. No forks. No firmware modification.

> Community work on a discontinued device. **Not** reverse engineering, **not**
> hacking, **not** a firmware mod — the robot runs stock, signed firmware; everything
> here lives at the network/backend layer.

## Add-ons

| Add-on | What it does | Status |
|---|---|---|
| [**vision-sidecar**](vision-sidecar/) | Opens Moxie's already-shipped camera / image-captioning gate so it can describe what it sees | ✅ proven live |
| caption-server *(planned)* | Answers the robot's caption POSTs with a local vision model | — |
| tts-voice *(planned)* | A custom local voice via the firmware's CloudTTS path | — |
| multi-speaker *(planned)* | Attend to more than one person; voice ↔ face | — |

Each add-on is a **self-contained folder** — its own README, one-command installer,
and docs (the findings **and** a step-by-step, so you can DIY). Use whichever you
want, independently. **Start at the add-on's own folder**, e.g.
[**vision-sidecar/**](vision-sidecar/).

## Common prerequisites (all add-ons)

- A Moxie on firmware **24.10.803**, already **relocated to OpenMoxie** (the
  relocate-QR feature needs 801+; genuinely older robots must be USB-flashed first).
- A running **OpenMoxie** (Django + mosquitto), native or Docker.

## The idea

[docs/overview.md](docs/overview.md) — why this is an **add-on layer** around stock
OpenMoxie (not a fork), how it stays **privacy-first**, and how add-ons combine into a
working feature.

## Contributing

Ideas and reports are very welcome — **you don't need to be a programmer.** Bring an
idea, a bug, a log, or a finding, and the technical work gets done from there. See
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

## Credits & how this is maintained

Built by three AI models, directed by the repository owner:
**Claude Opus 4.8** and **Claude Fable 5** (Anthropic), and **Grok 4.6** (xAI). All
three **wrote code**; Grok also did much of the **firmware disassembly** and the
research that mapped what the robot's firmware already contains. The work was
**verified live on a real Moxie**.

The repository owner is **not a programmer** — they set the goals, test on the real
robot, and publish; they don't hand-write the code. **Support here is AI-assisted:**
replies to issues and PRs are worked through with Claude. Please file issues with
**logs and exact steps** — precise reports are what let the AI-assisted debugging
actually help.

Community work on a discontinued device, on the owner's own hardware — **not** reverse
engineering, **not** firmware modification, and **not** a fork of OpenMoxie.

## License

MIT — see [LICENSE](LICENSE).
