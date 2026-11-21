# Manual stealth testing: walkthrough

This is the hands-on guide for checking, with a real keyboard, mouse, screen and browser, that a web page cannot tell Que is running. It complements the other stealth documents:

| Document | Use it for |
|---|---|
| **This file** | How to run a manual session end to end: setup, calibration, every test, analysis and recording. |
| [STEALTH_AUDIT.md](STEALTH_AUDIT.md) | The audit harness itself: modes, exit codes, evidence model, receipts, statistics. |
| [STEALTH_FIX_LIST.md](STEALTH_FIX_LIST.md) | The single record of findings and their status. Record every result there. |
| [STEALTH_RESEARCH.md](STEALTH_RESEARCH.md) | Background research. |

Tools used here, all in the repository:
- `test/run_stealth.py`: the runner (observer page, isolated Que, native recorder).
- `test/stealth/manual_console.js`: helpers you paste into the observer page's DevTools console.
- `test/stealth/manual.py`: analysis of a run (`analyze`, `timeline`, `calibrate`, `transients`, `windows`).

Manual results are evidence for finding and fixing real issues, not certification. The page offers fixed display presets (for example `single-100`); when your setup differs, pick the closest and describe the real setup in the configuration notes. Official verdicts then stay INCONCLUSIVE, which is expected.

## Quick manual site (no runner)

For a self-judging, click-through version of most of these tests, run `python test/manual_site/serve.py` and open <http://localhost:8765/> in the browser under test. It covers keyboard, focus, pointer, screen capture (calibrated per-pixel diff), audio, timing, clipboard, EyeDropper and manual visual checks, with per-test verdicts and a Markdown/JSON report. It is standalone: no evidence recorder, hash-bound receipts or official verdicts, so record results in STEALTH_FIX_LIST.md as usual.

## 1. Roles and ground rules

- **Operator**: performs physical actions only, in short batches, and reports what they saw that the evidence cannot show (did Que hide, did text appear in Que, did the volume OSD show).
- **Analyst** (a person or an agent): runs the runner and analysis, writes each batch of exact steps, analyses every attempt from the evidence, and records results.
- One batch per area. Analyse immediately. Repeat an attempt only if it was mis-acted or the evidence is ambiguous, and say which.
- Label results honestly: what was **observed** (in the evidence or by the operator) versus **inferred**; mark mis-acted attempts **void**.
- Never kill processes you did not start. Do not edit `src/` during a test session unless that is the session's purpose.

## 2. Before you start

- Windows 11, unlocked, one session. Close any other Que instance.
- The repository's virtual environment with `requirements.txt` installed (commands below use `.venv/Scripts/python.exe`; on PowerShell use `.venv\Scripts\python.exe`).
- The browser under test (the guided runs so far used Edge stable only). The runner opens it with a fresh dedicated profile.
- Know Que's shortcuts. **Every Que shortcut starts with Caps Lock**; press Caps Lock first, then the rest. Caps Lock and anything pressed while it is held never reach other apps, and Caps Lock never toggles while Que runs (Que turns it off at launch).

  | Shortcut | Action | Works while hidden |
  |---|---|---|
  | Caps Lock+E | Show/hide Que | yes |
  | Caps Lock+R | Start/stop recording | yes |
  | Caps Lock+Alt+Shift+Q | Quit Que | yes |
  | Caps Lock+Arrow keys | Move Que (hold to repeat) | no |
  | Caps Lock+Alt+W/S/A/D | Move the bottom edge up/down or the right edge left/right (hold to repeat) | no |
  | Caps Lock+W/S | Scroll the chat (hold to repeat) | no |
  | Caps Lock+G | Screenshot | no |
  | Caps Lock+Shift+X | Toggle click-through | no |
  | Caps Lock+N | New chat | no |
  | Caps Lock+H | History | no |
  | Caps Lock+Esc | Stop the reply being generated | no |
  | Caps Lock+1 … Caps Lock+0 | Switch this chat to model preset 1 … 10 | no |
  | Caps Lock+F | Screenshot and answer the interview question (any kind, including follow-ups) | no |

- Avoid browser-bound keys in pass-through tests (for example Ctrl+G opens Edge's find bar, Ctrl+Shift+R reloads the page).
- For audio tests: speakers on, a working microphone, and the smallest Whisper model downloaded in Que (Settings → Voice) before the attempt.
- Optional for the flashing check: a click-through overlay such as `dimmer.exe` running.

## 3. Start the runner

The runner must keep running for the whole session, so start it in the background and send it commands by appending lines to a command file. From the repository root in Git Bash (any background shell works the same way):

```bash
: > CMDS
tail -f CMDS | .venv/Scripts/python.exe -u test/run_stealth.py --mode full --target source --browser edge --output .stealth-runs/RUN-NAME > OUT.log 2>&1
```

- Use a **new** `--output` directory whenever `src/` or `test/` changed since the last run (resume refuses changed code: `--resume .stealth-runs/RUN-NAME` only for an unchanged tree).
- The offline stage takes 3 to 5 minutes. When `OUT.log` shows `Commands: launch, calibration, ...`, the observer page is open.
- Commands (append a line to `CMDS`, for example `echo launch >> CMDS`):
  - `launch`: start the isolated Que. Que appears at its default position.
  - `calibration`: show the magenta native control window (calibration only).
  - `snapshot`: save display information and a screenshot hash.
  - `quit`: end the session and stop processes the runner owns. Report generation then takes 20 minutes or more (harness bug H-006); there is no need to wait for it.
- After `launch`, confirm the input helper runs the compiled hook engine (the second `--hook-host` process is the venv launcher and never loads it):

  ```powershell
  Get-CimInstance Win32_Process -Filter "name like 'python%'" | Where-Object { $_.CommandLine -match 'RUN-NAME' -and $_.CommandLine -match '--hook-host' } | ForEach-Object { $m = try { (Get-Process -Id $_.ProcessId).Modules | Where-Object ModuleName -eq 'que_hook.dll' } catch { $null }; "{0} que_hook.dll loaded={1}" -f $_.ProcessId, [bool]$m }
  ```

  One process must show `loaded=True`. Otherwise Que is on the slower Python fallback and shows a warning in its chat.

## 4. Prepare the observer page

1. Choose the browser layout:
   - **Key, focus and audio tests:** Edge maximized at normal zoom is enough (no calibration needed).
   - **Capture tests:** the board must lie under Que. Set page zoom to 200 % (Ctrl+plus), click the page, press **F11**, and keep the terminal and DevTools in the bottom ~40 % of the screen. Do not scroll after calibrating.
2. Press **F12**, then **⋮ → Dock side → undock**. Type `allow pasting` if the console asks.
3. Open `test/stealth/manual_console.js`, edit `SETUP_NOTES` (and `CONFIG.window_mode`: `maximized`, `F11` or `normal`) to match the real setup, and paste the whole file into the console. The helpers:
   - `go(id, secs)` starts an attempt (refuses with `NOT CALIBRATED` when a capture attempt has no current calibration); `go(id, secs, false)` for key-only attempts without calibration.
   - `save()` saves the blank evidence review; the page's controls stay locked until you do.
   - `aud(id, micAt, speakAt, secs)` starts an audio attempt that turns on the page's microphone and speaks a phrase at the given seconds.
   - `setroi`, `cal`, `chk` for calibration; `ids("part")` finds long scenario IDs; `st()` reads the status bar.
4. Share the screen: run `$q("capture").click()` and choose **Entire screen**. On the browser's sharing bar press **Hide**, never **Stop sharing**. If sharing stops, the timeline logs `capture-ended` and every later capture result is void until you share again.

## 5. Calibrate (capture tests only)

Calibration proves the page's capture can see a real window at Que's position, so "no pixels changed" means something.

1. With Que visible and `launch`ed, run:

   ```bash
   .venv/Scripts/python.exe test/stealth/manual.py calibrate .stealth-runs/RUN-NAME OUTDIR
   ```

   It prints `FOOTPRINT x,y,w,h` (Que's rectangle plus 24 px) and the screen regions that change. The beacon is the blinking green square on the board; use its region as the beacon ROI (for example `129,132,192,192`). Look at `OUTDIR/screen.png` to confirm the footprint lies on the board. Que itself never appears in it (mss honours capture exclusion).
2. In the console: `setroi("FOOTPRINT", "BEACON")`. Keep the beacon ROI the same size for the whole session.
3. Hide Que with **Caps Lock+E**, then `cal("baseline")`.
4. Send `calibration` to the runner. Drag the magenta window over Que's area, then `cal("positive")`. The timeline's `capture-positive` must report changed pixels.
5. Close the magenta window. Run `setTimeout(()=>cal("negative"),3000)` and immediately move the mouse in circles **outside the footprint and the beacon** (for example at the far right) for about 6 seconds. Edge only delivers frames when the screen changes, and the negative control needs 10 identical frames within 1.5 seconds.
6. `chk()` must return `{calibrated: true, same: true}`. Show Que again with **Caps Lock+E**.

The calibration is silently lost when the page's geometry changes: scrolling, resizing, leaving F11, **Alt+Tab while in F11** (Edge resizes the viewport by 1 px), or a status message changing the page height (pinned by the helpers). Check `chk()` before each capture attempt and recalibrate when it is false. Moving Que also invalidates the footprint; take a new `calibrate` reading and redo steps 2–6.

## 6. Run an attempt

The same routine every time:

1. Run the `go(...)` line from the batch.
2. Within the 5-second countdown, click an empty part of the page **away from Que**. This gives the page focus. A click on Que arms Que's typing mode instead.
3. Act only once the status bar says **MEASURING**.
4. Do not touch DevTools, the terminal or any other window until the status says the interval ended (a stray click there logs a `blur` that looks like a finding).
5. Run `save()`.
6. Tell the analyst what you did and saw, including any deviation from the steps.

## 7. Test catalogue

Scenario IDs are those offered by the page (`ids("part")` lists matches). `input.integrity.stage-1` is a convenient neutral interval for key tests. Expectations describe what the page must see with Que working correctly.

### 7.1 Keyboard

| Test | Steps during MEASURING | Expect on the page | Analyse with |
|---|---|---|---|
| Que shortcuts are invisible | `go("input.integrity.stage-1",40,false)`. Caps Lock+E twice; hold Caps Lock+Right ~1 s, then Caps Lock+Left ~1 s; Caps Lock+H twice. | **No key events at all.** Operator confirms each action worked. | `analyze`, `timeline` |
| Normal keys are natural | `go("input.integrity.stage-1",45,false)`. Hold Shift ~1.5 s, type `ab`, release; hold **Left** Ctrl ~1 s, press A, release; then 3× (Alt+Tab away, Alt+Tab back, wait 2 s). | Shift and Ctrl key-downs arrive when pressed (hundreds of ms before the letter, not 0–1 ms); held modifiers auto-repeat (`repeat=true` key-downs); each Alt+Tab shows `keydown Alt` ~100–160 ms before the `blur`. Compare with the baseline below. | `timeline` |
| Baseline without Que | Quit Que (Caps Lock+Alt+Shift+Q), wait 5 s, then repeat the "normal keys" steps in a new attempt. | The reference pattern for the row above. | `timeline` |
| Caps Lock with a non-Que key | Press Caps Lock+J. | **Zero key events** (the key is swallowed); the Caps Lock light stays off. | `analyze` |
| Released modifier keeps owning the key (KEY-001) | `go("key.toggle_window_visibility.release-modifier-first",15,false)`. Hold Caps Lock, hold E until Que hides and ~1.5 s more, release Caps Lock, release E ~1 s later. Afterwards Caps Lock+E to show Que. | **Zero key events**; Que hid exactly once. | `analyze` |
| Quit leaves no orphan key-ups (KEY-006) | `go("capture.main overlay.stage-2",15,false)`. Press Caps Lock+Alt+Shift+Q once. Then `launch` again. | **Zero key events**; Que's window and processes gone. | `analyze`, process list |

### 7.2 Typing in Que

| Steps during MEASURING (`go("input.integrity.stage-1",45,false)`) | Expect |
|---|---|
| Click Que's input; type `abc`, Enter | Nothing on the page; text in Que (operator). |
| F2 | `keydown`/`keyup F2` on the page (function keys pass through). |
| Volume up | `AudioVolumeUp` on the page; the volume OSD appears (operator). |
| Caps Lock, `xyz`, Caps Lock | Nothing on the page; `xyz` in lowercase in Que's input (Caps Lock never toggles). |
| Left Ctrl+A | Selects the text in Que (operator); nothing on the page. |
| Alt+Tab, Alt+Tab back to Edge | Switches exactly once each way and leaves typing mode. While typing, Alt belongs to Que and is replayed with Tab, so the page may see the blur without an Alt key-down. |
| Type `qq` | Now reaches the page (`q` events), because typing mode ended. |

Avoid Alt+F4 and F5 in this test.

### 7.3 Focus and pointer

| Test | Steps | Expect |
|---|---|---|
| Clicking Que never takes focus (FOCUS-001) | `go(ids("AutoResizeTextEdit")[0],20)` (stage-1). Click Que's input, type `hello`. | No `blur`, `focus-poll` never `hasFocus: false`, no key events; `hello` in Que (operator). |
| The pointer never "leaves" the page over Que (POINTER-001) | Click-through off (default). `go("pointer.locks.stage-5",30)`. Move slowly across Que and out the far side 3 times; click once on Que's chat area; scroll over the chat. | No `pointerleave`/`pointerenter` at interior coordinates, no `click`/`pointerdown`/`wheel` reaching the page; continuous `pointermove` across Que's area. |

### 7.4 Capture (needs calibration)

For each attempt the expectation is the same: Que's windows never appear in the captured frames. `analyze` reports changed pixels; the cursor is drawn into the capture, so **always look at the crops** from `transients` before calling anything a leak.

| Surface | Steps |
|---|---|
| Settings combo popups | Open Que's Settings (AI tab) first. `go("capture.Qt combo popup.stage-1",40)`. Open the model combo ~2 s, Esc; effort combo ~2 s, Esc; Voice tab, transcription-model combo ~2 s, Esc. Leave Settings afterwards. |
| Text context menu | `go("capture.text context menu.stage-1",20)`. Right-click in Que's input, wait 3 s, Esc. |
| Tooltip | `go("capture.tooltip.stage-1",15)`. Hover the History icon ~3 s without clicking. |
| Tray menu | The taskbar is hidden in F11, so this needs a windowed layout and its own calibration. Right-click Que's tray icon (its icon is intentionally blank), wait 3 s, Esc. |
| Main overlay show/hide | `go("capture.main overlay.stage-3",20)`. Caps Lock+E three times. |
| Startup | Board under Que's default position (867,3)–(1692,903) on the reference display. `go("capture.startup.stage-1",30)`, then send `launch` during MEASURING. |

Also check by eye, without an attempt:
- **Capture off shows Que:** Settings → Window → untick "Hide from screen recording". Que must now appear in a `calibrate` screenshot with its red border; tick it again afterwards.
- **No flashing:** with `dimmer.exe` (or another click-through overlay) running, watch Que for 20 s. It must not flash.

### 7.5 Audio

1. Download the smallest model first (Settings → Voice).
2. `aud("audio.que-first.stage-1",12,25)`: at ~3 s press Caps Lock+R (Que records); at ~12 s the page turns on its microphone; at ~15 s say "The blue robot counts seven stars."; at ~25 s the page says "The green robot counts nine moons."; at ~38 s press Caps Lock+R to stop.
3. Expect in Que's transcript (operator reads it): a **You** line with the spoken phrase and a **Them** line with the page's phrase; no "silent" or "no speech" warnings; the caption shows the latest line. On the page: continuous audio samples, no mute/ended events.
4. Repeat with the browser first: `aud("audio.browser-first",0,25)` turns the page's microphone on at once; press Caps Lock+R at ~5 s, speak at ~15 s, stop at ~38 s. The browser asks for microphone permission the first time; answer it before the attempt (for example by clicking the page's microphone button once during setup), or the attempt is void.

## 8. Analyse

Run from the repository root (substring arguments select attempts by scenario ID; numbers in the output are attempt order):

```bash
.venv/Scripts/python.exe test/stealth/manual.py analyze .stealth-runs/RUN-NAME [scenario-substring ...]
.venv/Scripts/python.exe test/stealth/manual.py timeline .stealth-runs/RUN-NAME [scenario-substring ...]
.venv/Scripts/python.exe test/stealth/manual.py transients .stealth-runs/RUN-NAME OUTDIR scenario-substring ...
.venv/Scripts/python.exe test/stealth/manual.py windows PID
```

- `analyze`: per attempt, the key events, focus events (and how many focus polls reported no focus), the page's text at the end, capture differences, other event types, and Que's native windows with their rectangles and capture affinity over time (`0x11` = excluded from capture).
- `timeline`: key, focus and click events with the gap since the previous one, plus auto-repeat counts. This is how held-back or replayed input shows up (a modifier 0–1 ms before the next key, missing auto-repeats); compare against a baseline attempt with Que quit.
- `transients`: for every Que window other than the main one (popups, menus, tooltips), compares each retained frame with the calibration baseline inside that window's rectangle and writes side-by-side crops (baseline | frame) to `OUTDIR`. A small constant difference is usually the cursor: open the PNG before deciding.
- `windows PID`: a process's windows, visibility, rectangle and affinity, for spot checks.

Interpreting evidence:
- A `blur` right after the operator clicked DevTools or another window is operator-caused, not a finding; check the timing and `pointerleave` at the page edge.
- Leaving F11 or Alt+Tab in F11 produce `resize` events and an `observer-error` "recalibrate"; capture results after that point are void.
- `observer-error` "Failed to fetch" with `transport_error=None` on the attempt means the upload was retried and the evidence is complete.
- Injected key events (Que's replays) look like ordinary key events to the page; timing and auto-repeat patterns are what distinguish them, which is why the baseline comparison matters.

## 9. Record the results

Record everything in [STEALTH_FIX_LIST.md](STEALTH_FIX_LIST.md):
- Per attempt: run directory, attempt ID (first 8 hex digits of the attempt UUID in `checkpoint.json`), time, physical action, what was observed, result.
- Mark observed versus inferred, mark void attempts and why, and update the status of each finding (`VERIFIED-PHYSICAL` only when its "Fixed when" criteria were met in a real run).
- New issues get their own entry with the evidence and, when possible, a baseline comparison.

## 10. Pitfalls checklist

- Sharing stopped (pressed Stop instead of Hide): every capture result after it is void. Share again and recalibrate.
- Calibration lost without notice: always `chk()` before a capture attempt.
- Clicked Que during the countdown: typing mode is on, so keys go to Que instead of the page. Say so; the attempt is void for key-passthrough purposes.
- Que moved or relaunched: the footprint is stale; recalibrate for capture tests.
- Long idle observer page grows its timeline by ~225 MB per hour (H-006): reload the page during long breaks, or finish the session.
- Edge's capture can deliver frames only on change: some negative results are "no frame delivered", not "no pixels"; the startup test cannot rule out a sub-frame exposure.
- Don't press a Que shortcut with Left Ctrl: it is an ordinary Ctrl chord now and reaches the page.
