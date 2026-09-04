# Non-voice input → Moxie reacts (feasibility, proven live)

**Status: proven on a stock robot (2026-09-04).** This folder holds the *findings* —
how to make Moxie react to something that is **not speech**, what works, and what to
watch for — plus a short demo video. It is not yet an installable add-on.

## What this is

Out of the box, Moxie only reacts to **voice**: you talk, he answers. This finding
shows how an outside program — a game, an app, a sensor, anything that can report an
event — can make Moxie **speak and show an expression within about a second, without
anyone talking to him**, while he keeps listening so you can still talk to him in
between.

The demo uses a **piano-practice app** as the example: it reports "good passage" or
"wrong note", and Moxie cheers or encourages. But the app is just the *usage*; the
mechanism works for any event source.

📹 **Demo:** [`media/nonvoice_input_demo_take7.mov`](media/nonvoice_input_demo_take7.mov) (≈1 min).
What you'll see: a Mac voice says "Moxie, piano time"; Moxie opens the session; the app
sends three remarks with no one speaking (happy, concerned, surprised — watch his face);
the Mac voice asks Moxie two questions mid-session and gets real, unscripted answers; the
app ends the session and Moxie returns to normal chat.
Line-by-line script: [`media/nonvoice_input_demo_script.md`](media/nonvoice_input_demo_script.md).

> Community work on a discontinued device, on the owner's own hardware. Stock, signed
> firmware; everything here is a custom module on the **OpenMoxie backend**, using its
> public Remote Module API (`doc/RemoteModuleAPI.md`, `doc/ContentModules.md`). No
> firmware modification, no fork.

## The problem

Moxie is built around **conversation turns**: he says a line, then listens for you.
Nothing in that model lets an outside program "push" a line to him while he is idle —
and naive attempts leave him either silent or saying "Hmm" over and over while no
longer hearing anything.

## The answer (from the OpenMoxie developer)

Asked publicly, the OpenMoxie developer, **u/OpenMoxie on Reddit**, described the
intended pattern
([the reply](https://www.reddit.com/r/MoxieRobot/comments/1w57qi6/comment/p7fat1t/)):
build a custom module with Python plugins that *"produce silent outputs + monologue
timer event hooks, so robot will keep hitting your code with timer event inputs — a
timer input can then make a web call to the 'game' to get context and determine if you
want to emit output or continue silently waiting. If you want to emit output, load some
context in and let the AI produce a response. Just be sure to keep adding the monologue
timer event hooks so you get more callbacks after moxie delivers a real line."*

The demo below is that pattern, verified live.

## How it works

In plain words: Moxie is given a **tiny alarm clock** that rings about once a second.
Each time it rings, our code asks the event source "anything new?". Usually the answer
is no, and Moxie stays *quietly listening* (the tricky part — see below). When there is
something, Moxie says it, with a matching expression. If **you** speak, the normal
conversation takes over for that one turn, then the alarm clock is set again. When the
source says it's over, Moxie says goodbye and goes back to normal chat.

**Technical details — the module**

One `SinglePromptChat` module with its own `module_id` (so the normal chat module is
untouched) plus a global launcher phrase. Everything happens in the module's
`pre_process` hook:

1. **Set the alarm on every tick** — `volley.add_execution_action('eb_wait_monologue')`
   + `volley.update_subscriptions(['eb-wait-complete'])`. The timer only starts after
   Moxie's current line finishes; it comes back as `speech == 'eb-wait-complete'`.
2. **Poll the event source** — in the demo it is a file the Mac writes to; a real
   integration makes a web call here.
3. **Nothing new → a truly silent output, re-arm.**
4. **Something new → speak it, re-arm.** Emotion is optional: the event carries
   `mood:intensity|text`; the text goes through OpenMoxie's own automarkup engine with
   `mood_and_intensity=(mood, 0..1)`, and Moxie's **face** shows the mood.
5. **You spoke → `return False`.** The module's own LLM prompt answers as usual; the
   loop re-arms afterwards. The microphone is open between ticks.
6. **Source sends `END` → speak the close line and `launch` straight back to chat.** A
   hard tick cap and a kill-file are safety backstops, not the normal exit.


### The one detail that matters: what "silent" means

Plainly: telling Moxie to "say nothing" does **not** make him quiet. He treats an empty
line as a line, fills it with a little "Hmm" — and while he is making that sound his
microphone is off. Do that once a second and you get a robot that hums and cannot hear.
The fix is to send him a line that contains *instructions but no words*: he processes
it, has nothing to say, and keeps listening.

**Technical details — silent output**

An empty output (`set_output('', '')`) is not silent: the framework stores markup only
when non-empty and auto-builds markup from the text when it is missing, so an empty
line reaches the robot as an empty utterance and the robot fills it with "Hmm".
**Truly silent = non-empty markup with no speech.** We send the `AUTO_GESTURE_NONE`
behaviour-tree mark that OpenMoxie's automarkup already appends to every normal line,
minus the words. The robot parses it every turn anyway; nothing to speak, no "Hmm",
mic stays open.


## What was measured (stock firmware, normal chat mode)

| | |
|---|---|
| Quiet check-in period | ≈ 0.8 s (13 quiet ticks in a row = 10.6 s of witnessed silence, zero "Hmm") |
| Event → Moxie starts speaking | 0.8 – 1.5 s (best 0.3 s) |
| Question asked mid-session → Moxie's answer starts | ≈ 7 s (two in the video, both answered) |
| Per-event mood (happy / concerned / surprise / curious) | face changes; the voice does not |
| Session end via the source's `END` event | goodbye line, back in chat, no dead-end screen |
| Hearing afterwards | normal |

## Things to watch for

- **End the session by launching back to chat**, not with a plain "exit" — the plain
  exit drops Moxie into the firmware's "chat a bit longer?" screen and then a built-in
  activity. (API: `add_response_action('launch', module_id=<chat>, content_id=...)`,
  not `exit_module`.)
- **If the same AI brain also runs normal chat**, it keeps chat's habits (addressee or
  credit tags). Give this module its own `post_process` that strips them, or Moxie will
  read the tags aloud.
- **Don't talk while Moxie is talking** — his own playback masks the microphone and your
  words are dropped. Wait until he finishes.
- **No web-server restart needed to deploy**: run your loader, then
  `curl -sL http://localhost:8001/hive/reload_database`. Launching the module is a
  fresh entry.
- **Every loop needs brakes**: a hard tick cap, a kill-file checked every tick, and a
  clean exit. Keep it in its own module so "Moxie, do something else" still works as a
  second brake.

## Trade-offs

- Checking in faster means quicker reactions but less room for you to get a word in
  between ticks. ≈0.8 s felt right.
- Every *spoken* reaction is a real turn (microphone off while he speaks), so keep spoken
  events paced; quiet ticks cost nothing.

## Next step

Replace the file read with a real event source's web call (the developer's design), and
package it as an installable add-on like [`vision-sidecar/`](../vision-sidecar/).
