# SideCar for OpenMoxie

Additive add-ons that run **beside** a stock OpenMoxie install to bring a
discontinued Moxie robot back to life — enabling features the robot **already
ships**, on the owner's own hardware, privacy-first. No forks. No firmware
modification.

> Community work on a discontinued device. **Not** reverse engineering, **not**
> hacking, **not** a firmware mod — the robot runs stock, signed firmware;
> everything here lives at the network/backend layer.

## Add-ons

| Add-on | What it does | Status |
|---|---|---|
| [**vision-sidecar**](vision-sidecar/) | Opens Moxie's already-shipped camera / image-captioning gate so the robot streams frames — a small MQTT companion, **zero OpenMoxie core edits** | ✅ proven live |
| caption-server *(planned)* | Answers the robot's caption POSTs with a local vision model | — |
| tts-voice *(planned)* | A custom local voice via the firmware's CloudTTS path | — |
| multi-speaker *(planned)* | Attend to more than one person; voice ↔ face | — |

Each add-on is a **self-contained folder** with its own README and one-command
installer — use whichever you want, independently.

## Common prerequisites (all add-ons)

- A Moxie on firmware **24.10.803**, already **relocated to OpenMoxie** (the
  relocate-QR feature needs 801+; genuinely older robots must be USB-flashed first).
- A running **OpenMoxie** (Django + mosquitto), native or Docker.

## The idea

See [docs/overview.md](docs/overview.md) — why this is an **add-on layer** around
stock OpenMoxie (not a fork), how it stays privacy-first, and how the pieces combine
(e.g. *seeing* = vision-sidecar **+** a caption server **+** a DNS redirect).

## Credits

Built with **Claude (Anthropic)** and **Grok (xAI)**, directed, tested, and
maintained by the repository owner. Grok did much of the firmware disassembly and
research that mapped the robot's dormant capabilities; Claude did the design,
integration, and packaging. The work was **verified live on a real Moxie**. Issues
and PRs are answered by the maintainer.

## License

MIT — see [LICENSE](LICENSE).
