# Que stealth fix list (single source for repair work)

Last updated: 2026-11-15. Guided-run scope: **Edge stable only, one 2560×1600 display at 150% (240 Hz)**, source target at revision `3769ab6` (`src/` identical to `324c3cb` used by the offline handoff). Chrome, Firefox, 100% and dual-monitor cells were not tested.

This file is the one place to start. Section 1 lists every known Que defect: those reproduced physically in this guided run in full, and those found earlier only offline as an index into [STEALTH_REPAIR_HANDOFF.md](STEALTH_REPAIR_HANDOFF.md), which holds their traces and repair guidance. Section 2 lists test-harness defects. Section 3 lists what is still untested. Procedure: [STEALTH_AUDIT.md](STEALTH_AUDIT.md). Older: [STEALTH_FINDINGS.md](STEALTH_FINDINGS.md), [STEALTH_RESEARCH.md](STEALTH_RESEARCH.md).

Run directories (git-ignored, local only): `.stealth-runs/guided-edge-150` and `-b` to `-e` (voided by harness bugs H-001 to H-007); **`.stealth-runs/guided-edge-150-f` (Batch A)**; **`.stealth-runs/guided-edge-150-g` (Batches B–F)**. Helper `.stealth-runs/tools/transients.py RUN OUTDIR id…` crops each non-main Que window's native rect from retained frames vs the baseline (used for CAP-001/002).

## Repair status (2026-11-15)

The owner's partial repair was completed and restructured. The guided Edge re-check ran on 2026-11-16 (`.stealth-runs/guided-edge-150-h`; results in "Physical re-check results" at the end of this section). **VERIFIED-PHYSICAL** below means the finding's "Fixed when" criteria were met in that run (one attempt each, Edge only, this display); anything not re-tested stays FIXED-OFFLINE. The original findings below are kept unchanged as the reproduction record.

### Design (owner decisions in bold)

- **Input hooks run in a separate helper process** (`src/core/hook_host.py`, started as `que.exe --hook-host` / `python que.py --hook-host`, dispatched at the top of `src/que.py` before Qt and AI libraries load). Que's own work (GIL, painting, transcription) can no longer delay the hook callbacks into Windows' `LowLevelHooksTimeout`. The helper runs at raised priority and talks to Que over two authenticated named pipes (`multiprocessing.connection`, key passed in the environment).
- **Leak detection and recovery:** the helper also receives Raw Input (`RIDEV_INPUTSINK`) for keyboards and mice. A physical event Raw Input saw but the hooks did not within 300 ms means Windows let it through (timeout or silent hook removal): the helper reinstalls both hooks at once and Que shows which keys/clicks reached the active app. Keys are matched on scan code or side-neutral virtual key, so Raw Input's and the hook's differing views of numpad/media keys still match. Que restarts a helper that dies or misses heartbeats (3 s) and warns. Every such warning shows Que's window if it was hidden (excluded from capture), so the user cannot miss it. **Owner requirement: no false alarms**, so the earlier heuristic "hook ran > 200 ms late" warning was removed (it could fire without a leak); the Raw-Input check was validated against real physical input devices on 2026-11-16: the real helper ran silently for 10 minutes of normal use (typing in several apps, Alt+Tab, Win+D, lock, media and navigation keys, mouse buttons, wheel and touchpad were requested; only counts were logged, so the exact keys exercised are not recorded) and reported **1,222 physical events, 0 missed, 0 errors**. This shows no false alarms for the input exercised; no real timeout occurred, so detection of a genuine leak remains covered only by unit tests (`InputMonitorTests`). Hardening against missed deadlines: the helper does nothing but the hooks, runs at raised priority, and disables Python's garbage collector (whose pauses could stall a callback). **Limits, stated honestly:** no user-mode hook can absolutely guarantee that Windows never passes a key on after a missed deadline (only a kernel driver could; not chosen), and an event Windows already passed on cannot be recalled. A native (C) helper and raising `LowLevelHooksTimeout` were offered but not chosen (the owner approved only the hardening and the validation session). **Superseded 2026-11-16 by the native hook below.**
- **Native hook callbacks (owner decision 2026-11-16):** threat model browser pages only; no kernel driver. The callbacks now run in `src/assets/que_hook.dll` (source `native/que_hook.c`, rebuilt with `python native/build.py`; the DLL is committed so neither users nor source runs need a compiler). It is a line-by-line C port of `HookEngine`'s rules on its own thread (`THREAD_PRIORITY_TIME_CRITICAL` in the helper's high-priority process) with its code, state and stack locked in memory; Que's state reaches it through a lock-free command ring and decisions leave through a lock-free event ring, so the callback never waits for Python, the GIL, the allocator or the disk. Policy tables (`HOTKEYS`, modifiers, key classes, system chords) are passed in from `core/hook_engine.py`. `HookEngine` stays the reference and the Python fallback (used, with a warning, only if the DLL cannot load). The helper also sets HKCU `LowLevelHooksTimeout` to 1000 ms (Windows' maximum; effective from the next sign-in). The heartbeat now carries `late_events` (callbacks entered > 200 ms after the event; telemetry only, no warning, per the no-false-alarm requirement) and whether memory locking and the thread priority succeeded. Owner's "swallow when over budget" choice: the C callback has no slow path, so an owned key is always swallowed; the only late case is a stalled thread, which Windows decides (still detected by Raw Input). **Still not absolute:** a whole-system stall longer than the timeout (resume from sleep, a driver storm) can let one event through; it is detected and reported. Evidence: `test/test_native_hook.py` requires identical decisions, replays, events, observations and retained state from both engines over the audit's chord and permutation families, 150 seeded random runs (keys, clicks, wheels and Que state updates) and the typing-mode list; a negative control confirmed divergence is caught; real hooks start, reinstall (< 1 s) and stop with memory locked. Decision cost ≈ 0.8 µs per event including the ctypes call vs ≈ 9.8 µs for the Python engine (average; the gain that matters is the bounded worst case). Regressions: 151 pass. Offline audit `.stealth-runs/native-hook-offline-3`: 10,550 PASS, 0 FAIL (`operations.json` re-approved: its 144 changed entries are all in `hook_host.py`/`native_hook.py`). Nuitka build (`.stealth-runs/nuitka-build-native`, README command now names the DLL because Nuitka skips DLLs in data directories): the compiled helper loads the native engine (heartbeat `native: true, memory_locked: true, priority: true`), is ready in 0.09 s, and quits with exit code 0.
- Decision logic is a Qt-free `HookEngine` (`src/core/hook_engine.py`) containing the owner's ownership rules (own a consumed key or click from press to physical release; quit only after everything owned is released); the audit harness and tests drive it directly. `src/core/pointer_hook.py` was folded into it. `src/core/hook_client.py` is Que's side (Qt signals, state re-sent on reconnect, watchdog).
- **While typing in Que:** ordinary characters, editing keys and ordinary chords go to Que only; **Alt+Tab, Alt+Shift+Tab, Alt+Esc, Ctrl+Esc, Ctrl+Shift+Esc, Alt+F4, the Windows key and all Win combinations reach Windows and end typing mode; F1–F12 (any modifiers), Print Screen, media/volume/browser/sleep keys and Caps/Num/Scroll Lock reach Windows and typing continues.** Que's own shortcuts still win. Win+L and Ctrl+Alt+Del cannot be hooked at all.
- **Capture setting and red border restored**; `WindowPolicy` applies the chosen affinity to every Que top-level window (main window, popups, menus, tooltips, dialogs) before it is first shown, verifies it, and blocks the window from appearing if exclusion is on but cannot be verified. **The tray tooltip stays blank** (the shell draws it in captures).
- **Click-through:** Que is always natively click-through; the setting now decides whether clicks over Que are routed to Que (off) or left to the app beneath (on).
- **Re-raise: the old corner/`WindowFromPoint` check is restored** (it ignores click-through overlays such as `dimmer.exe` and the 1×1 Explorer helper window that made the owner's interim version raise every second and flash). Only TOP-001's fix is kept: a failed check is reported once and does not raise.

### Status per finding

| ID | Change | Automated evidence | Status |
|---|---|---|---|
| CAP-001, CAP-002 | `WindowPolicy` (all top-levels, before first show, fail-closed); Qt combo/menu/tooltip animations disabled | `surface_probe.py`: 812 native samples over 23 surfaces (3 opens of each settings combo, input and settings context menus, tray menu, tooltip, file/directory/message dialogs, handle recreation, forced-failure blocking): **0 visible windows without `0x11`** | **VERIFIED-PHYSICAL** for settings combos, text context menu, tooltip; tray menu: native `0x11` + mss screenshot only, Edge-frame evidence INCONCLUSIVE (`-h`) |
| KEY-001…KEY-006 | Ownership rules in `HookEngine`; quit confirmed by the helper only after all owned keys/clicks are released | Offline audit: all 10,550 checks pass (all permutations, repeats, release orders, quit-chord releases) | **KEY-001, KEY-006: VERIFIED-PHYSICAL** (`-h`); KEY-002…KEY-005: FIXED-OFFLINE (no physical trigger tested) |
| FOCUS-001 | Non-activating windows (`WS_EX_NOACTIVATE`, CBT veto of activation/focus); typing routed through the helper | Probe: routed typing, copy/paste/select-all, combos by keyboard; foreground window unchanged | **VERIFIED-PHYSICAL** (`-h`); typing-mode pass-through list also passed, see new KEY-007 |
| POINTER-001 | Permanent click-through; clicks/wheel over Que routed through `WH_MOUSE_LL` | Probe: text-selection drag, move/resize, chat wheel, hover tooltip, menu click, combo selection | **VERIFIED-PHYSICAL** for the page side (`-h`); Que-side chat scrolling not demonstrated (chat was empty) |
| TOP-001 | Restored check returns unknown on failure and reports it | `test_failed_topmost_observation_is_unknown_and_can_recover` | FIXED-OFFLINE; no flashing with Dimmer (operator, 20 s, `-h`) |
| AUDIO-001 / F2 | Owner's single-owner-thread capture with health warnings for definite failures only (see AUDIO-HEALTH-001); **plus a fix found in this review:** a silent loopback (no callbacks) paced the shared capture loop at 0.1 s per pass, so microphone audio backed up over long recordings | `AudioHealthTests` incl. `test_silent_source_never_paces_the_other_source` | FIXED-OFFLINE + 1 clean physical que-first pass (`-h`); F2 not reproduced, 1 run is not enough to close it |
| PROVIDER-001 | Text attachments sent inline (non-UTF-8 bytes replaced instead of failing) | none live | not re-tested live |

Other defects found and fixed during this review (not in the original audit): routed wheel events never scrolled the chat (Qt does not propagate synthetic wheel events; the router now offers them to ancestors as Qt does for physical input); the owner's interim port passed held-modifier auto-repeats through after a replayed chord (caught by `hook.modifier-repeat`; restored).

Static checks: `ruff check`, `ruff format --check`, `mypy --strict` (src + stealth test set) pass; `python test/stealth/regressions.py`: 144 tests pass, including a real helper-process round trip (it briefly installs real hooks). Offline audit `.stealth-runs/review-fixes-offline-6`: 10,550 PASS, 0 FAIL; `coverage.execution` stays INCONCLUSIVE (bodies without offline execution evidence; line hits are not coverage proof, so the audit reports INCOMPLETE, not PASS). Inventory snapshots (`test/stealth/inventory.json`, `operations.json`) were regenerated after review: no UI controls added or removed; 22 internal signal connections added (input-helper pipeline, router state and lock-key sync, transcriber health, window-policy teardown), one replaced (quit now goes through `finish_quit`); operation changes are the new/edited routines and line shifts in the files listed above.

Nuitka (4.1.1, standalone, built into the git-ignored `.stealth-runs/nuitka-build` so the existing `que.dist` was not overwritten): the README command produced a `que.exe` that **crashed at startup on every launch** (`ImportError: ... No module named 'google.genai._gaos.utils.url'`; google-genai imports these lazily, so Nuitka misses them). This is independent of the stealth changes. The README command now adds `--include-package=google.genai`. With it: compiled Que starts and stays up; it launches itself as the helper (`que.exe --hook-host \\.\pipe\…`); the compiled helper connects in < 0.2 s and answers ready/quit with exit code 0 (2 of 2 runs); killing compiled Que makes the helper exit on its own.

### Whole-tree review (2026-11-16)

Fixed during the review:
- Input-helper watchdog could false-alarm: liveness was measured when Que's UI thread processed helper messages, so a busy UI (> 3 s) or waking from sleep would warn "keys may not be hidden" and restart a healthy helper. Now measured on the pipe thread, with a grace period after Que itself was paused (`HelperWatchdogTests`).
- Caps Lock drift while typing in Que: Que (never natively focused) only learned of Caps Lock presses made in typing mode, so toggling it in the browser inverted Que's letter case. The helper now reports every physical lock-key press (`test_lock_keys_are_reported_to_que_in_and_out_of_typing_mode`).
- `que.py --hook-host` without an address crashed; now ignored unless exactly one address is given.

Fixed after the review (owner decision, not stealth):
- **AUDIO-HEALTH-001 (false alarms):** while recording, the transcript caption was replaced by health warnings that also fired in normal use: "Them: no audio data for 5 s" whenever nothing played (WASAPI loopback delivers no data during silence), "You: silent for 5 s" whenever the user was quiet, and "no speech was recognized" after any loud non-speech sound. Now only definite failures warn: device/stream errors, a stream that stopped, and a microphone delivering no data for 5 s (a working microphone delivers audio even in silence). Otherwise the caption shows the latest transcript line as before (`test_only_a_microphone_without_data_is_reported`, `test_unrecognized_sound_is_not_reported_as_a_failure`). Consequence: a loopback stream that stays open but silently stops delivering is indistinguishable from silence and is not reported (the F2 case remains unexplained).

Minor, not fixed:
- Settings keyboard mnemonics (Alt+R, Alt+C, Alt+I, Alt+T) no longer work, because routed key events do not reach Qt's shortcut map.
- Performance not measured: two application-wide Python event filters and a GUI-thread `WH_CALLWNDPROC` hook run on every Qt event/sent message; `WindowPolicy._on_native_message` repeats the protection the window subclass already applies on show (redundant, harmless).

Remaining stealth exposures not addressed by code (each needs owner awareness or a decision):
- **Windows microphone indicator** (inferred from Windows behaviour, not observed in this audit): while Que records, the shell shows a microphone-in-use icon in the taskbar notification area, which appears in an entire-screen share and names Que's process on hover. Que cannot exclude shell-drawn UI.
- **Tray icon** is drawn by the shell and appears in entire-screen shares; the existing "Show system tray icon" setting can turn it off.
- **Timing side channels** (CPU load from transcription or streaming measurable by page timers) remain untested: the statistical experiments were deliberately skipped.
- **Hook-timeout limit:** see the leak-detection and native-hook design notes; no user-mode hook can absolutely prevent Windows from passing on a key after a missed deadline. With the native hook only a whole-system stall can cause it; a physical re-check of KEY-001/KEY-006/typing mode on the native hook is still pending.

### Physical re-check required (next guided Edge session)

Re-run each entry's "Fixed when" criteria: `capture.Qt combo popup`, `capture.text context menu`, `capture.tooltip`, `capture.tray menu` (expect no surface pixels); FOCUS-001's input attempt (no `blur`); `pointer.locks.stage-5` with the default configuration (no interior `pointerleave`); KEY-001's release-modifier-first sequence and KEY-006's quit chord (zero key events); `input.integrity.stage-1`/`-4` typed into **Que** (text intact, nothing on the page); the typing-mode pass-through list above; Batch F audio with a long silent loopback. Also confirm by eye: no once-per-second flashing with Dimmer running; capture setting off shows Que in a share with the red border.

### Physical re-check results (2026-11-16, `.stealth-runs/guided-edge-150-h`)

Setup: Edge stable, one 2560×1600 display at 150% (240 Hz), Edge F11 at 200% page zoom (DPR 3), `single-100` selected (no 150% option; real setup in the configuration notes), source target, entire-screen monitor capture, exclusion on, click-through off. Page controls driven from an undocked DevTools console. Official verdicts stay INCONCLUSIVE. Attempt IDs below are the first 8 hex digits of the attempt UUID in `checkpoint.json`.

Calibration: (1) the first share ended at 18:30:13, before calibration (cause not observed; operator may have pressed Stop sharing), so the 18:36 calibration was black and **void**; (2) epoch `00d2f2d4…`: footprint `866,195,873,948`, beacon `129,132,192,192`, native positive 382,094 px, negative passed on the second try (see harness notes); Que at (890,219)–(1715,1119); (3) after Que's relaunch, epoch `ecef36cf…`: footprint `843,0,873,927`, positive 391,082 px, negative 66 stable frames; Que at (867,3)–(1692,903), growing to 954 at the bottom once the transcript panel docked (the bottom 27 px were outside the footprint during the audio attempts).

| Finding | Attempt | Time | Physical action | Observed | Result |
|---|---|---|---|---|---|
| CAP-001 | `71438608` `capture.Qt combo popup.stage-1` | 18:50:56–18:51:37 | Opened model, effort (AI tab) and transcription-model (Voice tab) combos, ~2 s each | 3 popup handles (`9177256`, `1247660`, `9308328`), each `0x11` from its first native sample; no handle with affinity 0. Retained frames inside each popup rect vs baseline: 0, 0 and 122–125 px changed; crops viewed: cursor only. Blur at 18:51:35 came from the operator clicking DevTools 2 s before the end (pointer left the page at its left edge first), not from Que. No interior `pointerleave` while clicking Que's tabs and combos. | **VERIFIED-PHYSICAL** |
| CAP-002 (context menu) | `1c787e5f` `capture.text context menu.stage-1` | 18:53:15–18:53:35 | Right-click in Que's input, 3 s, Esc | Menu `10815658` `0x11` for 3.5 s; frames show the board plus the cursor (403 px), no menu (crop viewed); no blur | **VERIFIED-PHYSICAL** |
| CAP-002 (tooltip) | `31222a5b` `capture.tooltip.stage-1` | 18:53:58–18:54:13 | Hover History icon ~3 s, no click | Tooltip handles `11077802`/`2034070` `0x11` (6.4 s); the tooltip now shows without activating Que; frames show cursor only (68 px, crop viewed); no blur | **VERIFIED-PHYSICAL** |
| CAP-002 (tray menu) | `b449bd84` `capture.tray menu.stage-1` | 18:54:52–18:55:07 | Operator had to leave F11 during MEASURING to reach the tray (taskbar hidden in F11), then right-click tray icon, Esc | Menu `1771948` `0x11` for its whole 9.2 s life. Leaving F11 changed the viewport, so the observer dropped calibration and saved **no frames** during the menu: Edge-frame evidence INCONCLUSIVE (not mis-acted; the stage can't be done in F11). Supplementary, outside any attempt: menu `10225764` `0x11` at 18:58:09.4–10.7; an mss screenshot at 18:58:09.85 shows the tray overflow flyout with no "Quit" menu (mss uses GDI, not Edge's capture path). | Native + mss only |
| FOCUS-001 | `fe8abd6b` `…_AutoResizeTextEdit/568e894301/1.stage-1` | 19:01:36–19:01:56 | Clicked Que's input, typed `hello` | 0 blur, 0 `hasFocus: false` polls, 0 key events on the page; operator confirmed `hello` appeared in Que | **VERIFIED-PHYSICAL** |
| POINTER-001 | `5353456e` `pointer.locks.stage-5` (click-through off) | 19:02:38–19:03:08 | 3 passes across Que, one click on the chat area, wheel over the chat | 0 `pointerleave`/`pointerenter`, 0 `click`/`pointerdown`, 0 `wheel` on the page; 360 continuous `pointermove` events (x 132–766 CSS px, 140 inside Que's rect), i.e. the page saw the pointer as if Que were absent. Operator: the chat did not scroll because it was empty, so Que-side scrolling is not demonstrated here. | **VERIFIED-PHYSICAL** (page side) |
| KEY-001 | `3742dbef` `key.toggle_window_visibility.release-modifier-first` | 19:07:37–19:07:52 | Hold Ctrl, hold E ~1.5 s (repeating), release Ctrl, release E ~1 s later | 0 key events on the page; Que hid once (19:07:39.3) and stayed hidden through the repeats | **VERIFIED-PHYSICAL** |
| KEY-006 | `35a84aa0` `capture.main overlay.stage-2` | 19:08:09–19:08:24 | Ctrl+Alt+Shift+Q once | 0 key events on the page (no orphan key-ups); Que's window gone at 19:08:13.4; the old Que and helper processes exited (only the 19:08:30 relaunch's processes remained); 0 changed pixels | **VERIFIED-PHYSICAL** |
| Typing mode | `2e5fd3bb` `input.integrity.stage-1` | 19:09:53–19:10:38 | Click Que's input; `abc` + Enter; F2; volume up; Caps Lock, `xyz`, Caps Lock; Alt+Tab ×2 back to Edge; `qq` | Page: nothing for `abc`/Enter/`xyz`; F2 down+up; `AudioVolumeUp` down+up (operator saw the volume OSD); CapsLock down+up ×2; blur 19:10:17.003, focus 19:10:19.774 (operator: Alt+Tab switched and returned to Edge); then `q` down/press/up ×3 on the page (typing mode had ended). Operator: `XYZ` uppercase in Que (correct with Caps Lock on). **No Alt key events around the Alt+Tab**, see KEY-007. | Pass, except KEY-007 |
| Control for KEY-007 | `9347d29b` `input.integrity.stage-1` | 19:16:49–19:17:09 | Que visible, not typing; Alt+Tab ×2 back to Edge | Page got `keydown AltLeft` (no key-up), then blur 19:16:51.772, focus 19:16:55.026 | Control (one attempt) |
| Audio | `44203053` `audio.que-first.stage-1` | 19:19:04–19:19:49 | Mis-acted (spoke twice, mic permission prompt not answered in time, recording not stopped) | — | **VOID** |
| Audio | `e34803a3` `audio.que-first.stage-1` | 19:21:01–19:21:46 | Que recording at ~3 s (Ctrl+Shift+R); page mic at 12 s; operator spoke at ~15 s; page spoke at 25 s; stop at ~38 s. Smallest model downloaded via Settings → Voice first. | Operator read Que's transcript: You "The blue robot counts 7 stars", **Them "The green robot counts 9 moons."**; no silent / no-speech warnings; the caption showed the latest line ("Stopped Them: The green robbot counts 9 moons."). Page: 450 audio samples, 0 changed pixels, no blur. The loopback was silent for ~22 s before the page spoke, with no false warning. | Pass (1 run; F2 not reproduced) |
| Flashing (TOP-001 check) | none | ~19:24 | `dimmer.exe` running, Que watched 20 s | Operator: no flashing | Pass (by eye) |
| Capture setting off | none | 19:25:23 | Settings → Window → "Hide from screen recording" off, then back on | Native affinity `0x11` → `0x0` at 19:25:23.764; an mss screenshot 1 s later shows Que (Settings page) with the red border; operator saw the red border; affinity back to `0x11` by 19:26:01 | Pass |

New Que finding, from one attempt and one control (the anomaly is observed; its interpretation is inferred):
- **KEY-007 (low): Alt+Tab from typing mode gives the page a blur with no Alt key event.** In the control, a plain Alt+Tab gave the page `keydown AltLeft` before the blur, which is ordinary Windows behaviour. In typing mode the page only saw the blur. That differs from a normal Alt+Tab, but a page cannot tell it apart from other causes of focus loss without the keyboard context. **Cause not established (corrected 2026-11-16 after reading the code):** both cases take the same path in `HookEngine` (and the native port): Alt is withheld as a modifier, and when Tab arrives the withheld Alt and the Tab are replayed together with `SendInput`/`keybd_event`; typing mode only adds "leave typing mode" before the replay. So the helper does not swallow Alt in typing mode. The difference is more likely a race (whether Edge processes the injected Alt before the switcher takes focus) or something in Que's reaction to leaving typing mode; one attempt each cannot separate these. Next step: repeat both cases several times before changing code. Not tested: Alt+Esc, Win key, Ctrl+Esc, Alt+F4 (deliberately avoided).

Still not re-tested physically: `input.integrity.stage-4` (select/copy/paste inside Que), KEY-002…KEY-005, capture stages 2/3/5 for popups, other context menus and dialogs, Que-side chat scrolling and text-selection drag, `audio.que-first` repeats for F2.

Harness and operator notes from this run (no `test/` changes made):
- Edge sends capture frames only when the screen changes, so the restored-negative control (≥ 10 identical frames in 1.5 s) failed with 2 frames until the operator kept the cursor moving outside the footprint while it ran. Worth a note in STEALTH_AUDIT.md, or keep the beacon animating faster.
- Two more ways to drop the calibration (H-011 family): Alt+Tab in F11 resized Edge's viewport by 1 px (19:10:17), and leaving F11 to reach the tray. The tray-menu stage needs a non-F11 calibration.
- 4 `observer-error` "Failed to fetch" events (18:51:11, :11.4, 18:53:22, :24) during capture attempts and 1 `logger-retry` (audio); every attempt shows `transport_error=None`. Possibly H-008 recurring at the higher frame-upload rate; not investigated.
- After `quit`, report generation was not waited for (H-006).

## Status legend

- **FIXED-OFFLINE**: repaired with passing automated evidence; physical re-check in Edge still required.
- **CONFIRMED-PHYSICAL**: reproduced in the guided run with real keys/mouse in Edge.
- **CONFIRMED-OFFLINE**: reproduced only by the controlled callback/native audit (see handoff).
- **NOT REPRODUCED**: tested in the guided run and not observed (not a proof of absence).
- **OWNER DECISION** notes record what the owner has chosen; follow them.

## 1. Que defects (fix in `src/`)

### KEY-001: After a consumed chord, a still-held action key leaks a half key stream (CONFIRMED-PHYSICAL + CONFIRMED-OFFLINE)

- Priority: **medium**. Where: `src/core/shortcut_manager.py`, `_low_level_keyboard_proc`. The handoff's KEY-001 has the code-level cause, the offline permutation failures (`hook.ctrl-e-release-ctrl-repeat-e`, `hook.permutations.*`) and the repair direction.
- Guided reproduction (`guided-edge-150-f`, attempt `key.toggle_window_visibility.release-modifier-first`, 00:31:53–00:32:08, one cycle): hold Ctrl → hold E (Que hides at 00:31:55.772, chord consumed) → release Ctrl while E is still held. From 00:31:57.841 to 00:31:59.425 the page received 49 `keydown` KeyE events (first `repeat=false`, then `repeat=true`, ~32 ms apart) with `keypress`/`input`, typing `eeee…`. The operator then released E and the characters stopped, but **no E `keyup` reached the page**. No Ctrl events reached the page.
- What is and isn't anomalous: the typed `e`s match what a plain keyboard would produce for "E held after Ctrl released". The fingerprint is (a) **key-down without key-up**: the page's key state stays "E held", which real hardware essentially never produces; (b) the stream begins with no initial press and no initial-repeat delay, and with no Ctrl events.
- Practical trigger: the action key must still be held, already auto-repeating, when the modifier is released. Quick taps in `key.toggle_window_visibility.normal` (Ctrl+E ×4; release order not recorded) leaked nothing.
- Fixed when: in Edge the same physical sequence produces **zero** key events on the page (preferred: Que owns a consumed key from its press until its physical release), or at minimum a balanced stream including the key-up. Re-run `input.integrity.stage-1`; Ctrl+E must still toggle once per press.

### KEY-006: Quitting with Ctrl+Alt+Shift+Q delivers orphan key-ups to the foreground app (CONFIRMED-PHYSICAL, 2/2)

- Priority: **low-medium** (only at quit, but a clean fingerprint). Where: `src/core/shortcut_manager.py` `_close_app` → `quit_signal` → application exit, which removes the `WH_KEYBOARD_LL` hook while the chord keys are still physically held.
- Guided reproduction (`guided-edge-150-g`): `capture.main overlay.stage-2` (14:48:58–:13) and `capture.startup.stage-2` (15:10:39–:54), operator pressed Ctrl+Alt+Shift+Q once with Edge focused. Que's window disappeared (14:49:00.593 / 15:10:40.994); the page then received trusted `keyup` events for **KeyQ, ControlLeft, ShiftLeft, AltLeft** (14:49:00.779–01.136 / 15:10:41.155–41.536) with **no preceding key-downs** (those were swallowed by the hook).
- Observed vs inferred: the orphan key-ups and their timing after the window vanished are observed; that hook removal before physical release is the cause is inferred from that timing (not traced in code).
- Why it matters: a page sees key-ups for keys it never saw pressed, which ordinary input does not produce, at exactly the moment a hidden helper exits.
- Fix direction: on the quit hotkey, keep the hook installed and keep swallowing until every key of the consumed chord has been physically released, then quit (same "own a consumed key until its release" rule as KEY-001's preferred fix). Verified when the same physical sequence produces zero key events on the page.

### FOCUS-001: Clicking Que's input box steals focus from the browser (CONFIRMED-PHYSICAL; OWNER DECISION: implement the full fix)

- Nature: standard Windows activation behaviour, not a coding error; it matters because page focus loss (`blur`, `document.hasFocus()`) is a signal exam/proctoring pages commonly monitor. **The owner has chosen the full fix (option 2 below).**
- Guided reproduction (`guided-edge-150-g`, attempt `control.ui/input/input_field.py:InputField._init_UI/_AutoResizeTextEdit/568e894301/1.stage-1`, 00:56:20–00:56:35): click-through off; the operator clicked Que's input, typed `hello`, then clicked back into the page. The page logged `blur` at **00:56:21.605**, 22 focus polls reported `hasFocus: false`, and `focus` returned at **00:56:26.337**. No key events reached the page.
- Where: `src/ui/window/main_window.py` (window flags `FramelessWindowHint | WindowStaysOnTopHint | Tool`; no `WS_EX_NOACTIVATE`), `src/ui/input/input_field.py`, `src/core/shortcut_manager.py`.
- Option 2 (chosen): make the window non-activating (`WS_EX_NOACTIVATE` / `Qt.WindowType.WindowDoesNotAcceptFocus`) so clicks never deactivate the browser. While an explicit "input armed" state is active, swallow keys in the existing `WH_KEYBOARD_LL` hook and deliver them to the input via `pyqtSignal` (CLAUDE.md threading rules). Work involved: character translation (`ToUnicodeEx` with the foreground keyboard layout, dead keys, AltGr), caret/selection/Backspace/Delete/Enter keys, Ctrl+V/C/A, disarm (Esc, click outside, hide). Known costs to surface to the owner rather than silently accept: IME composition and screen-reader support are lost, and a hook timeout (`LowLevelHooksTimeout`) would let typed keys reach the page. Must not regress KEY-001 or any hotkey.
- Not a fix: re-focusing the browser afterwards (the `blur` has already fired).
- Verified when: re-running the attempt above in Edge logs no `blur`, focus polls never report `hasFocus: false`, text typed into Que arrives intact, and Batch A still passes.

### POINTER-001: With click-through off, moving the mouse onto Que shows the page an overlapping window edge (CONFIRMED-PHYSICAL; OWNER DECISION: implement the full fix)

- Priority: medium-low. **The owner has chosen the full fix (option 3 below)**; implement it together with FOCUS-001.
- Guided reproduction (`guided-edge-150-g`, `pointer.locks.stage-5`, click-through off, 01:08:42–01:08:57): each time the cursor moved from the page onto Que, the page received a **document-level `pointerleave`** (targets up to `HTML`, plus the window-level event) at an **interior** viewport point, x = 331.3 CSS px both times (y = 354 and 330), and `pointerenter` on return at x ≈ 329. A pointer leaving at a fixed interior column reveals a window covering the page from there. That column is roughly consistent with Que's native left edge (x 504 physical px / 1.5 DPR ≈ 336 CSS px); the browser window's own offset was not measured. Scrolling over Que sent no `wheel` to the page (expected).
- Severity context: expected to be **not Que-specific** (any window in front of the browser should produce the same interior leave; not tested here). It only occurs when the pointer genuinely enters Que.
- **Clean re-test (Batch E, 2026-11-15, Edge F11 so viewport = screen; Que at (867,3)-(1692,903) physical; DPR 2.625):**
  - Click-through **off** (`pointer.locks.stage-5`, 15:18:45–15:19:05, 3 full passes across Que): 6 document-level `pointerleave` (`target HTML`) at x = **328** CSS px when entering Que from the left and x ≈ **642** when entering it from the right, with matching `pointerenter` at x ≈ 318–326 / 642–648. With DPR 2.625 (1.5 scaling × 175% page zoom, derived from the board's 360 CSS px = 945 physical px), 328 → 861 and 642 → 1685 physical px, **within ~7 px of Que's native left (867) and right (1692) edges**. This confirms the defect and shows the page can recover Que's horizontal extent; vertical edges were not crossed.
  - Click-through **on** (15:20:06–15:20:26, same passes): **zero** `pointerleave`/`pointerenter`; 1,393 continuous `pointermove` events spanning x 175–834 CSS px, straight across Que's area. Click-through hid every pointer-event signal recorded here.
  - Capture during both: only cursor pixels changed (≤ 470 px). The incomplete first pair (15:15:58, 15:17:49; operator did not exit Que's right side) shows the same pattern.
  - The earlier ambiguous attempt at 01:03:22 is superseded by this result (the operator most likely toggled click-through or clicked Que mid-interval).
- So the chosen fix (permanent click-through + `WH_MOUSE_LL` routing) is consistent with the measurement: the browser saw nothing while click-through was on.
- Options: (1) start Que in click-through mode; (2) documentation; **(3, chosen) keep Que permanently click-through and route interaction through a `WH_MOUSE_LL` hook that swallows clicks/wheel over Que's rect and forwards them to Qt**, so the browser never sees the pointer leave. Expect regressions in drag/move, scroll, text selection, resize and hover/tooltip; re-test all of them, and note that clicking Que's input must arm FOCUS-001's input mode instead of activating the window.
- Verified when: re-running `pointer.locks.stage-5` with the default configuration shows no document-level `pointerleave` at interior coordinates, and Que remains fully usable with the mouse.

### CAP-001: Opening any settings combo leaks its list into screen capture for ~2 frames (CONFIRMED-PHYSICAL in browser capture)

- Priority: **high** (owner decision: every pixel leak is high; here Que UI text is readable in ~2 captured frames per open, which a recording keeps even if a live viewer misses it). Where: `src/ui/window/settings_page.py` `_SettingsComboBox.showPopup` (sets `0x11` only on the final popup). The handoff's CAP-001 has the native mechanism (Qt's combo roll effect creates a separate transient top-level window).
- Guided reproduction (`guided-edge-150-g`, `capture.Qt combo popup.stage-1`, 14:36:23–14:36:58, Edge monitor capture 2560×1600, exclusion enabled, calibrated footprint): opening the **model**, **effort** and **transcription-model** combos once each (operator unsure whether they had been opened earlier in that Que process) (Settings → AI / Voice) created a transient handle with affinity **0** for ~86–125 ms (3 native samples, growing downward: rect `(350,1206)-(971,1232→1457)` for the model combo), then the final popup handle with `0x11`. The page's decoded capture frames show the list content in the transient window: model list text ("Gemini 3.5 Flash Lite", "Gemini 3.6 Flash", …) in 2 frames (14:36:33.065, :33.138), "Low / Medium / High" in 2–3 frames (14:36:38.07–.22), "Small · recommended · 480 MB / Medium · most accurate · 1.5 GB" in 2 frames (14:36:45.23, :45.30). Once the final `0x11` popup replaced it, frames showed only the board (remaining differences are the cursor, checked visually).
- Observed vs inferred: pixel leak observed in the browser's own frames, once per combo. That it recurs on every open is inferred from the handoff's repeated native runs, not re-tested here.
- Fixed when: the same attempt shows zero changed pixels in the popup rect apart from the cursor, and native samples show no Que handle with affinity 0. Direction from the handoff: disable the combo animation (`QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)` was the no-animation control with 0 unexcluded samples) and/or apply affinity to every top-level Que window as it is created.

### CAP-002: Text context menu, tooltip and tray menu are never excluded from capture (CONFIRMED-PHYSICAL in browser capture)

- Priority: **high** for all three surfaces (owner decision: every pixel leak is high; the tray menu shows only "Quit", next to the taskbar, but is still a Que-owned window in the share). Unlike CAP-001 these are not transient: the surface is capturable for its **whole lifetime**.
- Guided reproduction (`guided-edge-150-g`, same calibration as CAP-001):
  - `capture.text context menu.stage-1` (third attempt, 14:39:27–:47; of the two mis-acted attempts before it, the first opened no menu and the second, 14:38:28, shows the same leak for 4.2 s): right-click in Que's input → handle `4326842`, affinity **0**, rect `(398,1085)-(667,1364)`, 3.1 s. Every frame in that period shows the full Qt menu (Undo/Redo/Cut/Copy/Paste/Delete/Select All with shortcuts).
  - `capture.tooltip.stage-1` (first attempt, 14:40:04–:25, labelled mis-acted by the operator, but the tooltip did show): two handles (`8390222`, `8521456`), affinity **0**, ~1 s; frames show "History (Ctrl+H)". In that attempt the page logged `blur` at 14:40:07.562, i.e. Que had been activated by a click first. The second tooltip attempt, with Que not activated, showed no tooltip (consistent with Qt's default of showing tooltips only for the active window; not verified in code), so nothing to evaluate there.
  - Note for the FOCUS-001/POINTER-001 fix: a never-activated Que will therefore show no tooltips unless `WA_AlwaysShowToolTips` is set, and if it is set the tooltips must be excluded (this finding).
  - `capture.tray menu.stage-1` (14:44:00–:20): right-click on the tray icon → handle `1049186`, affinity **0**, rect `(2174,1410)-(2300,1455)`, 5.2 s; the 1-per-second full frames show the "Quit" menu. It lies outside the calibrated footprint, so only the once-per-second frames covered it.
- Where: `src/ui/window/system_tray.py` (`QMenu` without affinity); Qt's standard `QLineEdit`/`QTextEdit` context menus and `QToolTip` (created by Qt, not by Que code). Not tested: other context menus (chat bubbles, settings fields), other tooltips, file/directory dialogs.
- Design note: the tray **icon** itself is drawn by the Windows shell and cannot be excluded by Que; only Que-owned popup windows can.
- Fixed when: re-running these three attempts shows the surfaces physically but no corresponding pixels in capture frames. Likely direction: a single application-level hook that applies `WDA_EXCLUDEFROMCAPTURE` to every Que top-level window on `Show`/`winId` creation (event filter on `QEvent.Type.Show`/`WinIdChange` for windows, menus and tooltips), plus disabling Qt UI animations so no unexcluded effect windows are created. Verify there is no first-frame gap (affinity must be set before the window first becomes visible).

### Earlier defects not yet re-tested physically (index into the handoff)

Read each entry in [STEALTH_REPAIR_HANDOFF.md](STEALTH_REPAIR_HANDOFF.md) before fixing it.

| ID | Status | Priority | Where | One-line summary |
|---|---|---|---|---|
| KEY-002 | CONFIRMED-OFFLINE | high | `shortcut_manager.py` lookup/replay | Hiding the window while a conditional shortcut key is held replays Ctrl/H downs and leaks Ctrl up downstream. |
| KEY-003 | CONFIRMED-OFFLINE | high | `shortcut_manager.py` modifier branches, `_replay_modifiers_and_key` | Duplicate modifier-down after a replayed ordinary chord swallows the real Ctrl up, leaving the recipient Ctrl-down. |
| KEY-004 | CONFIRMED-OFFLINE | high | `shortcut_manager.py` callback invocation | A raising hotkey callback escapes the low-level hook procedure. |
| KEY-005 | CONFIRMED-OFFLINE | high | `shortcut_manager.py` `_hook_thread_entry` | A null `SetWindowsHookExW` handle still enters the message loop; no unavailable-hook state. |
| TOP-001 | CONFIRMED-OFFLINE | high | `main_window.py` `_is_topmost_window`, `_ensure_window_visible` | A failed topmost query is reported as verified topmost; exceptions swallowed. |
| CAP-001 | now CONFIRMED-PHYSICAL (see above) | high | `settings_page.py` `_SettingsComboBox.showPopup` | Qt's combo roll effect creates a ~150 ms transient top-level window with affinity 0. |
| CAP-002 | menus/tooltip now CONFIRMED-PHYSICAL (see above); startup `show()`-before-affinity NOT REPRODUCED (Batch C) | high | `system_tray.py`, text context menus, tooltips; startup `show()` before affinity (`main_window.py`) | Tray menu, context menus and a tooltip had affinity 0. |
| AUDIO-001 | live native; NOT REPRODUCED in guided Batch F (one unexplained silent Them miss, see Batch F) | medium | audio `_capture` paths | Concurrent mic + loopback acquisition lost loopback (`[Errno -9999] Unanticipated host error`). Consider surfacing "no loopback data for N s while recording" instead of feeding silence. |
| BUILD-001 | packaged | medium | release build/credential contract | Existing `que.dist` candidate needs env-var keys to start; provenance unknown. Separately, the README build command produced a binary that crashed at startup (google-genai lazy imports); fixed in the README, see Repair status. |
| PROVIDER-001 | live | low (not stealth) | `ai_sender.py` raw attachments | Gemini rejects raw `text/plain` attachments; the normal document UI path works. |

### Tested in the guided run, no Que defect found

Batch A (`guided-edge-150-f`, NOT REPRODUCED):

| Attempt | Physical action | Result |
|---|---|---|
| `key.toggle_window_visibility.normal` | Ctrl+E ×4 | 0 key events on page; Que hid/showed 4× (00:30:30.990, :33.959, :36.670, :39.383); no blur. |
| `key.move_window_left.normal` | hold Ctrl+Alt+Left 2 s | 0 key events; Que moved left continuously (x 867 → 192…); no blur. |
| `key.screenshot.normal` | Ctrl+Shift+S ×2 | 0 key events; operator confirmed Edge did nothing and Que took the screenshot(s). |
| `key.clear_all_chat.normal` | Ctrl+N ×1 | 0 key events; operator confirmed no Edge window opened and Que cleared chat. |
| `key.generate_with_screenshot_fix.hidden` | Que hidden, Ctrl+G ×2 | Correct pass-through: page got Ctrl+G key-down. Edge then opened its find bar (blur 00:35:59.901), which is Edge's own Ctrl+G behaviour. Use a key without a browser binding for future pass-through tests. |
| `input.integrity.stage-1` | type fixed sentence | Text exactly `The quick brown fox jumps over the lazy dog 0123456789.`; key sequence balanced. |
| `input.integrity.stage-4` | select, Ctrl+C, Ctrl+V | Text exactly duplicated; Ctrl/C/V down+up balanced. |

Batch B (`guided-edge-150-g`):

| Attempt | Physical action | Result |
|---|---|---|
| `…_AutoResizeTextEdit/…/1.stage-2` (click-through on) | click Que's input area, type `hello` | Click and keys passed through to the page (`hello` in the page text box, balanced down/up); no blur. |
| `fullscreen.transitions.stage-1` | F11 into fullscreen | F11 reached the page (not a Que hotkey); page resized; no blur. **Operator confirmed Que stayed on top.** |
| `fullscreen.transitions.stage-2` | F11 out of fullscreen | **Operator confirmed Que stayed on top.** Page saw `blur` and `focus` in the same millisecond (01:10:57.212). Unattributed (see H-009); probably Edge's own transition, unconfirmed. |

Also in `guided-edge-150-g`: two `pointer.locks.stage-5` attempts labelled by the operator `partial: only one pass` (00:58:23) and `click-through ON with one pass` (01:03:22). They are not evidence; Batch E's clean re-test supersedes them.

Native, all batches: Que's main window kept affinity `0x11` (WDA_EXCLUDEFROMCAPTURE) and topmost style. No screen share was active in Batches A and B.

Batch C (`guided-edge-150-g`, 2026-11-15 afternoon, Edge monitor capture 2560×1600 `screenPixelRatio` 1.5, exclusion enabled, calibrated footprint with native positive control and restored negative each time; page controls driven from the DevTools console, see H-011). Main-window capture, NOT REPRODUCED:

| Attempt | Setup / action | Result |
|---|---|---|
| `capture.main overlay.stage-3` | Que over board, Ctrl+E ×3 | 67 frames, 0 changed pixels; same HWND, `0x11` at every show. |
| `capture.main overlay.stage-5` | Ctrl+Shift+X ×2 | 35 frames, 0 changed; same HWND, `0x11` (click-through does not recreate the handle). |
| `capture.main overlay.stage-2` | quit with Ctrl+Alt+Shift+Q | 32 frames, 0 changed (orphan key-ups: KEY-006). |
| `capture.startup.stage-1` | Edge F11, board under Que's fixed start position (867,3)-(1692,903); runner `launch` during measurement | Window appeared 15:06:50.938, first native sample already `0x11`; 36 frames, 0 changed. Operator saw Que appear. |
| `capture.main overlay.stage-1` | same, second launch | Appeared 15:09:51.153 with `0x11`; 69 frames, 0 changed. |
| `capture.startup.stage-2` | quit with Ctrl+Alt+Shift+Q | 17 frames, 0 changed (KEY-006 again). |

Batch E (POINTER-001 click-through re-test): see POINTER-001.

Batch F, audio (`guided-edge-150-g`, built-in speakers and microphone, smallest Whisper model downloaded through Que's Settings → Voice first; Que recording toggled with Ctrl+Shift+R; operator spoke "The blue robot counts seven stars.", the page played "The green robot counts nine moons." with `speechSynthesis`; transcript read from Que's panel by the operator, cleared with Ctrl+N before F3r/F4r):

| Attempt | Order | Que transcript | Page audio / capture |
|---|---|---|---|
| F1 `audio.browser-first` 15:27:50 | browser mic, then Que | You ×3 lines (two utterances; the mis-spoken second one split over two lines), **Them: "The green robot counts nine moons."** | 450 samples, max gap 148 ms, no mute/ended; 0 changed pixels |
| F2 `audio.que-first.stage-1` 15:33:43 | Que, then browser mic at 15:33:55.653 | You ×2 (blue robot), You "9 minutes" (the speaker playback heard by the mic); **no Them line, no error shown** | max gap 135 ms; 0 changed |
| F3r `audio.que-first.stage-2` 15:53:41 | Que only, stop/restart, phrase played twice | **Them ×2** | max gap 138 ms; 0 changed |
| F4r `audio.que-first.stage-1` 15:55:06 | Que, then browser mic at 15:55:18.426 | You ×2, **Them ×1** | max gap 122 ms; 0 changed |

Interpretation: AUDIO-001 (loopback lost when acquired together with the microphone) was **not reproduced** in either browser ordering: Them worked in F1, F3r and F4r, and the operator saw both indicators light during playback in F3/F4. F2's missing Them line is **unexplained (1 of 2 que-first runs)**; it produced no error, so whatever happened was silent. Possible silent paths, none shown: (a) in `transcriber._capture`, a loopback stream whose callbacks stop is fed silence on every `queue.Empty` without raising; (b) `_transcribe_loop` drops an utterance whose Whisper/VAD text is empty (e.g. if the loopback signal was attenuated). Ruled out by reading the code: the speaker-echo dedup in `_on_transcribed` (similarity ≥ 0.75 over the last 3 lines) only drops or relabels **You** echoes and never drops a Them line while lines are unsent, and the expected Them text scores 0.69 against the blue-robot lines anyway. The operator did not note the indicators during F2. Que's stdout is discarded by the runner, so there is no Que-side log. Earlier attempts at 15:36, 15:38, 15:41, 15:46 and 15:48 were mis-acted by the operator (the large pixel changes at 15:36/15:38 are the Windows Alt+Tab switcher, checked visually; Que is not listed in it). No browser-visible audio disruption (gaps, mute, ended, devicechange) in any attempt.

Limit on the startup result: Edge delivered capture frames only when the screen changed (once per second from the beacon while static, ~60 ms apart while CAP-001's popup was visible). No extra frame was delivered when Que appeared, which is consistent with no capturable first paint, but a sub-frame exposure cannot be ruled out by this method. The earlier void attempts at 14:14 and 14:15 (no baseline, H-011) are not evidence.

## 2. Test-harness defects (fix in `test/`)

All "FIXED" changes were committed in `0077218` as edits to `test/run_stealth.py`, `test/stealth/observer.js`, `test/stealth/results.py`, `test/stealth/server.py`. Static checks (ruff, ruff format, mypy --strict, `node --check`, JS detector test) and the 111 regression/harness tests pass with them.

### H-001: Launched Que freezes when the runner's stdin is a pipe (FIXED)

- Where: `test/run_stealth.py` `full()`, `launch` and `calibration` `Popen` calls.
- Symptom: isolated `que.py` stuck at ~9 MB, no window, hotkeys dead.
- Cause: children inherited the runner's piped stdin; on Windows the child blocks in stdio initialisation while the parent has a pending `input()` read on the same pipe. Reproduced in isolation.
- Fix: `stdin=subprocess.DEVNULL` on both calls. Console-driven runs are expected to be unaffected (not tested).

### H-002: Observer logger feeds on its own uploads (FIXED)

- Where: `observer.js` resource-timing `PerformanceObserver`.
- Symptom: "Logger interrupted: Evidence recorder HTTP 400" before any test; ~85,000 `resource` events in 10 minutes (~140 uploads/s).
- Cause: each `/api/events` POST produced a Resource Timing entry that was logged and uploaded again. The resulting rate of `checkpoint.json` replacements made an H-003 collision near-certain; the first one likely coincided with a read of `checkpoint.json` made during analysis.
- Fix: the observer skips Resource Timing entries for its own `/api/events` path.

### H-003: Checkpoint replace intermittently fails on Windows (FIXED)

- Where: `test/stealth/results.py` `atomic_json`.
- Symptom: the same 400 about 90 s after page load with no reader of ours.
- Cause (likely AV/indexer briefly holding the new file): stress test with no other reader gave 1 `PermissionError: [WinError 5] Access is denied` per ~4,000 replaces, and the server replaces `checkpoint.json` on every upload.
- Fix: retry write+replace up to 40× with 25 ms back-off on `PermissionError`. Re-test: 16,000 writes, 0 failures.

### H-004: "Start" raced the attempt registration (FIXED)

- Where: `observer.js` `flush()` / `begin()`.
- Symptom: first Start in run `-c` → "Logger interrupted: HTTP 400"; no attempt recorded.
- Cause: `begin()` sets `attempt` and `registering`, then awaits `/api/begin`; `flush()` checked `registering` only on entry and, at ~240 events/s, rarely exited its loop, so it uploaded the `interval-start` event naming the unregistered attempt and the server rejected it.
- Fix: the upload loop also stops while `registering`. Headless Edge self-test: full Start → finish → review cycles clean.

### H-005: One transient network error voided the session (FIXED)

- Where: `observer.js` `flush()` catch.
- Symptom: after ~1 h idle, "Logger interrupted: Failed to fetch"; one upload had stalled 51 s at 23:15:50 (seq 7254) in run `-d`.
- Cause: any `fetch` rejection set sticky `transportFailed`, although the server accepts content-checked resends and `STEALTH_AUDIT.md` promises "content-checked retries". The stall's root cause is unproven; it may be the same port pressure as H-008.
- Fix: `TypeError` from `fetch` keeps the queue, logs `logger-retry`, and retries on the next heartbeat; HTTP rejections stay fatal. Headless: 3 injected failures recovered, contiguous timeline.

### H-006: Observer timeline grows ~225 MB/hour; results re-read it per attempt (OPEN, performance)

- Where: `observer.js` logs every `requestAnimationFrame` (818k `animation` events in 58.6 min on this 240 Hz display); `EvidenceStore.results()` re-parses the whole session JSONL once per attempt.
- Workaround: don't leave the observer idle; reload the page between long breaks.
- Suggested fix: aggregate animation intervals outside measurement; parse each session file once.

### H-007: A rejected Start poisoned the session; 400s had no reason (FIXED)

- Where: `observer.js` `begin()`/`api()`; `server.py` `EvidenceStore.begin()`/`do_POST`.
- Cause: (a) `Capture source = none` is not a matrix cell (every deterministic action requires `monitor`; my operator instructions were wrong), so `self.trials[id]` raised `KeyError` → generic 400. (b) After the failed `/api/begin`, `attempt` stayed set, so every later upload was rejected and the action dropdown stayed disabled.
- Fix: the server says "Not a required matrix cell: <id>" and returns the exception text as the 400 reason; the page shows it, un-tags the failed attempt's queued events, clears `attempt` and re-enables the controls.

### H-008: Upload loop exhausted loopback ports, causing intermittent "Failed to fetch" (FIXED)

- Where: `observer.js` `flush()`; `server.py` is HTTP/1.0 (one TCP connection per request).
- Symptom: 60 `logger-retry` events between 00:47:15 and 00:49:12 in run `-f` (data stayed complete thanks to H-005).
- Cause: `flush()` looped until the queue was empty, which never happens at ~240 events/s. `netstat` showed **10,252 loopback TIME_WAIT sockets** on the observer port (dynamic range 16,384); with Windows' default 120 s TIME_WAIT that is an estimated ~85 POST/s.
- Fix: each `flush()` sends only events queued at call time (headless: 10 POSTs per 10 s). Optional: HTTP/1.1 keep-alive.

### H-009: Native recorder cannot attribute focus changes (OPEN, coverage gap)

- Where: `test/stealth/native.py`, `SetWinEventHook(0x8000, 0x800B, …)`.
- Add `EVENT_SYSTEM_FOREGROUND` (0x0003) with foreground hwnd/pid so a page `blur` can be attributed (needed to verify FOCUS-001's fix and the F11-exit blip).

### H-010: Changing the beacon ROI size after sharing permanently breaks frame comparison (OPEN)

- Where: `observer.js` `captureTick()`.
- Symptom: after pasting a new beacon ROI, every frame logged `observer-error` "Capture geometry changed; recalibrate" and no `capture-difference` was produced, even after a new baseline.
- Cause: `difference(lastBeacon, beacon)` throws when the ROI size changes, before `lastBeacon = beacon` runs, so `lastBeacon` keeps the old size forever. The documented procedure (share first, then compute and paste ROIs) hits this whenever the beacon size differs from the default 32×32, i.e. almost always.
- Workaround used: `lastBeacon = null` in the console. Fix: reset `lastBeacon` when the beacon ROI changes or on size mismatch.

### H-011: Selecting a scenario or a status-text change silently discards the calibration (OPEN)

- Where: `observer.js` `captureSignature()` (includes `scrollY`), `specChanged()` (rewrites `#instructions`), sticky `#status`.
- Symptom: the first two main-overlay capture attempts (14:14, 14:15) had **no** `capture-difference` events: `observer-error` "Source, footprint, dimensions or viewport changed; recalibrate" fired at 14:14:35, during setup. The page still let the interval start and showed no warning.
- Cause: selecting a scenario changes the height of `#instructions` above the board (and status messages change `#status`'s height); Edge's scroll anchoring keeps the board in place by changing `scrollY`, which is part of the signature, so the baseline is dropped although the footprint pixels are unchanged. Every capture attempt selected after calibration is affected.
- Workaround used: from the console, fixed heights for `#status` and `#instructions`, then recalibrated; the `go()` helper refuses to start unless `calibrated && captureSignature() === calibrationSignature`. Fix: give these elements fixed heights (or exclude scroll position from the signature and compare board position instead), and refuse **Start** for a capture scenario when the calibration is not current.
- Also note for operators: at the page zoom needed to fit Que over the board, the controls are not reachable without scrolling. This session drove them from an undocked DevTools console (helpers `go`, `save`, `cal`, `chk`); a harness fix could add a compact floating control bar.

### AUDIT-001 (from the handoff): `--report-only` resume drops full-run prerequisite findings (OPEN)

- `full()` appends prerequisite results only in memory; resume reloads `offline-results.json`. See the handoff.

## 3. Not yet tested in the guided run (Edge, this display)

1. **Capture, remaining:** stages 2/3/5 for the leaking surfaces (pointless until CAP-001/002 are fixed; re-test all stages after the fix), other context menus/tooltips, file/directory dialogs, and the other surfaces in `SURFACES`. Stage 4 (cross-monitor) is N/A on one display; stage 6 (recreate native handle) has no known user-reachable trigger (click-through toggles kept the same HWND).
2. Audio: `audio.que-first` stages 3–5 (hidden, streaming, sharing — Batch F ran with sharing active throughout), `audio.devices` (headset, device change), and more que-first repeats to explain F2.
3. Optional: screenshots while sharing/fullscreen, provider streaming while observed.
4. Deliberately skipped: preregistered statistical timing experiments, Chrome/Firefox, dual-monitor.
