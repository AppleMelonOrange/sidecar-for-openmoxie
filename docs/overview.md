# SideCar for OpenMoxie — the idea

## Why this exists

Embodied shut down and took Moxie's cloud with it. `OpenMoxie` (jbeghtol/openmoxie)
revived the core — it speaks the robot's protocol and runs chat + speech-to-text
locally — a huge amount of careful work that everything here builds on. It focuses on
that core experience; some of the firmware's heavier, data-sensitive capabilities —
the **camera** most of all — sit outside its scope. These add-ons extend OpenMoxie to
enable them.

## The decision: an add-on layer, not a fork

Each add-on runs **beside** stock OpenMoxie — extra processes and config, no source
edits. Reasons:

- **Ride OpenMoxie's updates** instead of drifting away from them.
- **Keep the pieces swappable** and independently installable.
- **Build around the hard part.** OpenMoxie already solves the un-reproducible bit
  (the robot's protocol). Everything added here — a vision model, speech, a voice —
  is commodity and swappable. This is integration, not reinvention.

## Privacy-first

The design principle that makes this defensible on a device made for kids:

- **Sensitive signals stay on your hardware** — camera frames, microphone audio,
  and the vision/speech models all run locally, in your home.
- **Only de-identified text leaves** — if a language model handles conversation, it
  sees *"the child said X; I can see a book,"* never the raw camera or audio. (The
  language model can be fully local too, for zero egress.)

This is better than the original product, which sent low-res camera frames to
Embodied's cloud — and unlike a hosted service, it doesn't recreate the single
operator that Moxie died with, nor put a stranger in charge of children's data.

## How the pieces combine

Some features need more than one add-on. **Seeing**, for example, needs:

- **vision-sidecar** — opens the firmware's camera gate (in this repo),
- **a caption server** — answers the robot's image POSTs with a local vision model,
- **a DNS redirect** — points the robot's hardcoded caption hostname at that server.
  This is a **router setting**; no software can do it for someone.

## Honest scope

This is for the remaining Moxie owners who want what the robot could always do. It
won't reach thousands, and a truly one-click *full* experience (bundling the caption
server + a vision model + the DNS redirect into an appliance) is a bigger,
box-shaped project. Per add-on, **one command** is the bar we hold.
