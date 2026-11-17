# Que stealth repair handoff

Date: 2026-11-14. Source revision: `324c3cb00356ec7625ca586a0c56c75fdff6232a`.

> [!NOTE]
> Historical record of the pre-repair audit. The defects below were later repaired; line numbers and the FAIL verdict refer to the source revision above. Current status: [STEALTH_FIX_LIST.md](STEALTH_FIX_LIST.md).

**Compliance is FAIL. Full physical desktop validation is INCOMPLETE.** The user requested unattended checks and preparation of the remaining guided run. No production code was changed. Do not interpret the successful harness checks, live backend operations, native affinity values, or unattempted scenarios as website invisibility.

Start with this document and [the machine-readable handoff](STEALTH_REPAIR_FINDINGS.json). [The audit procedure](STEALTH_AUDIT.md) defines acceptance and evidence requirements. [Earlier findings](STEALTH_FINDINGS.md) retain the previous investigations. [The research ledger](STEALTH_RESEARCH.md) covers additional browser detection methods.

## Executed checks and boundaries

| Execution | Observed result | Evidence directory under `.stealth-runs/` |
| --- | --- | --- |
| Complete offline callback audit and regression discovery | **3,737 PASS / 6,813 FAIL / 1 INCONCLUSIVE**; 111 regression/harness tests passed. Failing variants are mostly the same hook defects, not 6,813 distinct bugs. | `handoff-offline/` |
| Full source runner, all selected browsers, actual isolated Que startup | Initial report: **3,738 PASS / 6,813 FAIL / 202,577 INCONCLUSIVE**. Chrome and Edge observers became ready. **Zero measured desktop attempts** were submitted. | `handoff-full-source/` |
| Full packaged runner, linked source audit | Initial report: **3,737 PASS / 6,813 FAIL / 202,578 INCONCLUSIVE**. Source callback results were linked, not executed inside the binary. Initial candidate startup produced no observed window. | `handoff-full-package/` |
| Chrome and Edge harness checks | Six checks passed: ordinary observer, isolated observer, and real OPFS/memory worker operations for each browser. These use headless/synthetic harness actions. | `handoff-automation/` |
| Owned endpoint protocol | HTTP/CORS, WebSocket marker, foreign-origin rejection and native-path rejection passed. No public HTTPS-origin browser permission experiment was performed. | `handoff-automation/network-checks.json` |
| Static checks | Ruff lint, formatting, strict mypy over 60 Python files, JavaScript syntax and actual pixel/frame detector checks passed. | `handoff-automation/` |
| Live Gemini and Claude | Discovery, text streaming and cancellation worked. Normal extracted-document + synthetic image + supplied transcript worked for both. Raw `.txt` attachment rejection is separately documented below. | `handoff-full-source/native/live-*.json` |
| Real audio and model operations | Microphone capture produced callbacks; simultaneous loopback acquisition failed twice. Cold `tiny.en` download, cold/cached loading and transcription of two synthetic WAV fixtures worked. | `handoff-full-source/native/live-audio*.json` |
| Synthetic Qt surface actions on the real desktop | Main-window affinity survived the sampled click-through and hide/show transitions. Native popup/menu/tooltip handles with affinity zero were observed. Combo transition reproduced three times. | `handoff-full-source/native/synthetic-surfaces.json`, `popup-*.json` |

The installed browser executable versions were Chrome `157.0.8103.51` and Edge `157.0.4341.46`; no browser update/channel certification was performed. Firefox was absent from the runner's supported installation locations. Windows build was 26200, with one 2560×1600 display at 144 DPI (150%). Neither required display setup was available. Eleven audio device entries included WASAPI microphone, output and loopback endpoints; enumeration alone did not establish successful acquisition.

The source native collector retained 1,295 samples, a maximum observed interval of 69 ms, and 707 samples containing Que's window. Its main-window affinity was `0x11`. A rectangle-overlap episode lasted at least 44,164 ms through the end of collection. **This is a candidate overlap, not confirmed physical occlusion**: unrelated transparent/cloaked windows can overlap. No camera or physical visibility review was available. Forced harness cleanup is not a graceful-shutdown test.

## Reproduced source defects: fix these first

### KEY-001 — A consumed hotkey leaks repeated action-key downs after modifiers change

Priority: high. Location: [shortcut_manager.py](../src/core/shortcut_manager.py), `_low_level_keyboard_proc`, lines 235–278. Compliance IDs include `hook.ctrl-e-release-ctrl-repeat-e` and `hook.permutations.*`.

Minimal controlled reproduction: Ctrl down → E down → Ctrl up → repeated E down → E up. The initial chord is consumed. The repeated E down reaches `CallNextHookEx`; E up is still consumed. Browser-visible delivery is therefore plausible, but this run used the real callback with mocked downstream APIs, not physical browser input.

Cause supported by code and trace: each non-modifier event performs a new modifier/visibility lookup. `suppressed_vk_codes` is consulted for release, but does not protect repeated downs after lookup stops matching. With remaining buffered modifiers, the no-match branch can also replay modifiers plus the action key. All 6,573 failing mixed-modifier permutation cases had downstream delivery and an unbalanced delivered stream.

Repair direction: preserve the decision for a consumed physical key until its release, independently of later modifier changes. Preserve repeat-enabled action semantics while the original chord is active, and ensure no-repeat callbacks remain single-shot. Do not indiscriminately clear held/modifier state or replay consumed presses as a recovery strategy.

Acceptance: all generic/left/right and mixed modifier press/release permutations, partial releases, held repeats, extended/system-key paths and ordinary-input controls pass unchanged. Then reproduce physically in each browser while screenshots, streaming, recording and fullscreen transitions are active. No delivered down without its matching up; no consumed-key replay.

### KEY-002 — Hiding the window releases a held conditional shortcut downstream

Priority: high. Same callback, especially lookup at lines 238–242 and replay at 262–268. ID: `hook.visibility`.

Reproduction: Ctrl down → H down → change visibility to hidden while H remains held → repeated H down → H up → Ctrl up. The trace contains replayed Ctrl/H downs and a delivered Ctrl up after the original H chord was consumed.

Cause: current visibility determines whether a repeat still matches, even after the application already claimed the key. Fix per-press suppression ownership, not a special exemption for hide/show. Acceptance includes hide/show, minimize, panel transitions and callback-triggered visibility changes while keys remain held. Keep hidden-window ordinary shortcut pass-through intact.

### KEY-003 — Duplicate modifier-down after replay leaves the recipient's modifier held

Priority: high for input integrity; physical occurrence remains unverified. Location: modifier branches at lines 214–231 and `_replay_modifiers_and_key` at 295–321. ID: `hook.modifier-repeat`.

Reproduction: Ctrl down → ordinary A down/up → another Ctrl down → Ctrl up. The ordinary chord replays Ctrl down to the recipient. The duplicate down recreates a suppressed modifier record, so the real Ctrl up is swallowed. The recipient remains logically Ctrl-down.

Track physical-held, buffered and already-delivered modifier state separately enough to balance the recipient's stream. Test duplicate downs, both sides held, shared modifiers, interrupted chords, ordinary typing and selection/copy/paste. Do not remove the duplicate-event test because the present physical trigger is unconfirmed.

### KEY-004 — Callback exceptions escape the low-level hook procedure

Priority: high. Location: callback invocation at line 253. ID: `hook.callback-exception`.

A matched callback raising the fixture `RuntimeError` escapes `_low_level_keyboard_proc`. The test invokes the Python callback directly; actual ctypes dispatch/Windows consequences were not established. Contain failures at the hook boundary, retain deterministic suppression/release bookkeeping, and surface errors through the established Qt signal path. Verify that later input remains balanced and that the hook does not silently stop functioning. Do not turn action failures into a passing action-completion result.

### KEY-005 — A null hook handle still enters the message loop

Priority: high. Location: `_hook_thread_entry`, lines 323–345. ID: `hook.installation.False`.

With `SetWindowsHookExW` returning null, the implementation calls `GetMessageW` anyway. Check the installation result, capture the native error immediately, and expose an unavailable-hook state. Preserve successful install/unhook and thread-shutdown behavior. Add actual installation/removal and sustained-load confirmation; the current controlled test does not simulate Windows silently removing a timed-out hook.

### TOP-001 — Failed observation is reported as verified topmost state

Priority: high for truthful protection state. Location: [main_window.py](../src/ui/window/main_window.py), `_is_topmost_window`, lines 730–770, and `_ensure_window_visible`, lines 722–728. ID: `failure.topmost-query-error`.

The real method returns `True` after `frameGeometry()` raises. Its caller also swallows exceptions. A query failure must not assert successful observation. Preserve an explicit unknown/error path and test recovery. Review primary-screen scaling, coordinate conversion on secondary/negative-coordinate monitors, corner-only sampling and the one-second timer separately. These are additional investigation targets, not newly proven occlusions. Acceptance still requires independent native ordering plus physical visibility evidence; capture-excluded screenshots cannot supply it.

Representative callback traces and the topmost exception trace are embedded in [the prior JSON findings](STEALTH_FINDINGS.json); complete parameterized traces are `handoff-offline/native/hook-traces.json`. Every issue above remains a failing compliance assertion.

## Newly measured capture-affinity exposure candidates

### CAP-001 — Combo opening creates an unexcluded transient window

Priority: high. Location: [settings_page.py](../src/ui/window/settings_page.py), `_SettingsComboBox.showPopup`, lines 865–871. Applies to the model, effort and transcription-model combos; test each actual surface.

The minimal native experiment imports the production `_SettingsComboBox`, opens it on the real Windows desktop, records the actual affinity-setting call, and samples visible top-level widgets during the transition. Qt version was **6.9.0**. In all three default-effect runs, `SetWindowDisplayAffinity(final_popup, 0x11)` returned 1 with native error 0, but a separate visible handle reported affinity **0** during opening:

| Run | Samples with an unexcluded visible handle | Span from first to last such sample |
| --- | --- | --- |
| `popup-affinity.json` | 32 | 151.40 ms |
| `popup-repeat-2.json` | 33 | 152.03 ms |
| `popup-repeat-3.json` | 32 | 148.91 ms |
| `popup-no-animation-control.json` | 0 | No such sample |

The final popup remained excluded. The last row is a **modified diagnostic control**, not a passing production configuration and not a reason to discard the default-effect failures.

Likely mechanism, supported by the control and the matching Qt source: `QComboBox::showPopup()` starts a 150 ms scrolling effect; `QRollEffect` creates a separate top-level widget and paints a captured popup pixmap. Applying affinity only to the final popup does not cover that transient handle. See the primary [Qt 6.9.0 combo source](https://raw.githubusercontent.com/qt/qtbase/v6.9.0/src/widgets/widgets/qcombobox.cpp) and [effect source](https://raw.githubusercontent.com/qt/qtbase/v6.9.0/src/widgets/widgets/qeffects.cpp).

Reproducer retained in `.stealth-runs/handoff-automation/popup_affinity.py`:

```powershell
.venv\Scripts\python.exe .stealth-runs/handoff-automation/popup_affinity.py .stealth-runs/handoff-full-source --label new-popup-reproduction
```

Use a fresh label to preserve old evidence. The synthetic invocation and native state are proven; **captured browser pixels are not yet proven**. Next: capture an already calibrated monitor before opening each real combo, at the delivered frame cadence, with three repeats and source-matched controls. Investigate a narrowly scoped animation policy or covering every transient surface before first paint. Preserve selection, keyboard navigation, scrolling, placement, sizing, themes and capture-included behavior. Retest menus/tooltips too; a steady-state affinity check is insufficient.

The retained reproducer also enforces a native assertion: an unexcluded visible window exits 1 as `FAIL_NATIVE_AFFINITY`; missing/unsuccessful final-popup observations exit 2. The assertion-enabled rerun is retained as `popup-assertion-reproduction.json`. The animation-disabled branch is explicitly `CONTROL_ONLY`, never a production stealth pass.

### CAP-002 — Menus and tooltip/transient surfaces require explicit investigation

`synthetic-surfaces.json` records affinity 0 for standard text context-menu and tray-menu handles and a tooltip-related transient. The tray menu is created in [system_tray.py](../src/ui/window/system_tray.py) without an affinity assignment. The test used the actual menu objects with synthetic popup calls; it did not activate physical tray UI or capture a browser frame. Some observed widgets were transient animations, so do not infer that every final popup remains unexcluded.

Trace each native handle from creation through destruction, including framework-created effects, native dialogs and handle replacement. Capture these surfaces with an exposed positive control. Also investigate startup `show()` before capture exclusion at `main_window.py:396–398`. Click-through toggles and hide/show retained the same excluded main-window handle in the sampled native experiment; that bounded observation does not close other platforms, transient frames or handle-recreation paths.

## Live operation findings and environmental investigations

### AUDIO-001 — Simultaneous Que acquisition lost the system-output source

Two instrumented runs called the unchanged `_capture` paths for microphone and loopback concurrently. Both sources produced completion sentinels, but only the microphone opened successfully. The retry retained the exact error: `Them audio is unavailable: [Errno -9999] Unanticipated host error` (nonfatal). The microphone produced 30 callbacks / 2,304,000 bytes in the retry. No raw live audio was retained.

Evidence: `native/live-audio.json`, `live-audio-capture-retry.json`, their logs, and `loopback-control.json`. The first script reused a mutable error list when writing later stages, so its empty final error list is not evidence of error-free acquisition; the separately retained retry freezes the error data and is authoritative for the error text.

An independent raw PyAudio loopback control opened successfully without Que: silent playback delivered no callbacks; owned zero-valued output delivered 19 callbacks / 729,600 bytes. Thus the device exists and can work. Root cause remains unresolved: investigate acquisition order, simultaneous PortAudio instances, host/device lifecycle and load. Do not label the device universally unavailable or invent a browser disruption from this result. Test both acquisition orders, repeated sessions, inactive/active output, speakers/headset, device changes and browser co-capture. Retain the microphone-only degraded state accurately in the UI.

Cold `tiny.en` download and loading succeeded; the same cached model object was reused. Synthetic WAVs produced both labeled transcript lines. Those WAVs were generated to files, not played through speakers and not captured from real speech. They prove the synthetic transcription path only.

### BUILD-001 — Existing candidate uses a different credential/startup contract

Candidate: `que.dist/que.exe`, SHA-256 `66aa3007f8fa43e12fdeb5d579aa0b80a5447b959f3d856bb52e3651b7f161cb`. Its build/source provenance is unknown.

The default staged run produced no target window. A separate captured-output retry exited 1 with `ValueError: Missing key inputs argument!` during compiled `AISender.__init__`; its line mapping differs from current source. Supplying the existing test credential only through `GOOGLE_API_KEY` and `GEMINI_API_KEY` in that child's environment allowed startup: 29 visible-window samples over 30 seconds, affinity `0x11`, topmost style present. Logs were redacted. Evidence: `native/startup-investigation.json`, `startup-stderr.log`, `startup-env-investigation.json`.

This establishes a staging/credential compatibility problem for this candidate, not a new current-source credential defect. Do not copy the user's whole `.env` or data directory into tests. Build the intended release from a recorded revision, document its credential contract, and repeat actual capture/startup tests. Native startup with an environment-key workaround is not release stealth validation.

### PROVIDER-001 — Raw text attachment is rejected, normal document flow succeeds

Gemini discovery, a connection generation request, streaming and cancellation succeeded. Directly passing `document.txt` in `AISender`'s attachment list returned HTTP 400: `text/plain` is unsupported as document content. The resumed-request wrapper then described the failure as possibly unavailable conversation state. Claude accepted that raw attachment.

The actual document UI calls `extract_text` and saves context, rather than submitting a raw `.txt` document attachment. That normal path succeeded for both providers alongside a synthetic red image and transcript keyword: both returned `ORANGE-TELESCOPE-42, red, BICYCLE`. Therefore do **not** report ordinary text-document upload as broken on this evidence. Only change raw attachment handling if that path is intended/supported; improve error attribution if appropriate. Synthetic images are not evidence that screenshot capture worked. Responses, model selections, completion/cancellation observations and errors are in `native/live-gemini.json`, `live-claude.json`, `live-context.json`.

### AUDIT-001 — Report-only resume drops a full-run prerequisite finding

The source full report was copied to `report-before-resume.json` before running `--resume ... --report-only`. Resume retained all 6,813 failures and the overall FAIL, but dropped `prerequisite.firefox`, reducing INCONCLUSIVE from 202,577 to 202,576. Firefox was still unavailable; no test resolved that prerequisite.

Cause: `full()` appends setup findings to its in-memory `base`, while `offline-results.json` retains only the earlier offline results. Report-only resume reloads that earlier list and does not rebuild browser prerequisites. Location: `test/run_stealth.py`, `full()` prerequisite appends and `main()` resume loading. Preserve full-run prerequisite/setup findings durably, without duplicate IDs on repeated resumes, and add a regression comparing the before/after set of results. Keep original reports and failing/incomplete outcomes. The current run did not become a false complete pass, but losing the explanation is still an audit defect.

## Remaining guided work and acceptance

Both full sessions are retained and resumable; neither contains a measured desktop attempt. Source run: `.stealth-runs/handoff-full-source`; candidate run: `.stealth-runs/handoff-full-package`. Their guides contain scenario IDs, prerequisites, completion proof and cleanup. The operator must use real keys, permissions and physical observation. No synthetic action above should be imported as a physical-action receipt.

```powershell
.venv\Scripts\python.exe test/run_stealth.py --resume .stealth-runs/handoff-full-source
.venv\Scripts\python.exe test/run_stealth.py --mode full --target source --browser all --cross-origin-isolated --output .stealth-runs/NEW-ISOLATED-RUN
```

If source/harness code changes, create a fresh run; preserve these results. Do not refresh inventories merely to silence drift. The current isolated source has synthetic context documents, WAV/image fixtures and a downloaded test model; use a new run for cold-start/model-download conditions. Provider test conversations may remain in their normal service account storage.

Prioritize the keyboard reproducers and combo/menu transient captures, then startup/first paint, fullscreen/focus/pointer lock, all surfaces/dialogs, audio acquisition orders, screenshots, provider failures/cancellation, concurrent actions, sustained hook stress and shutdown. Supply Firefox, single-monitor 100% and dual 100%/150% with the secondary left, physical camera/video evidence, and the exact release build. Test both capture settings and every supported capture source. Repeat deterministic cells three times; perform the preregistered paired statistical batches with effective controls. Public HTTPS-origin local-network policy, permission boundaries, hardware-specific contention and timing/classification experiments remain unmeasured.

The manifest declares 676 contracts and 199,935 deterministic desktop cells, before statistical intervals. These are obligations, not executed tests. The offline execution tracer still reports **329 unexecuted source entry/body paths**, with the largest groups in `main_window.py` (55), `history_sidebar.py` (35), `chat_area.py` (32), `claude_bridge.py` (29), and settings/hooks (27 each). Supplemental live/native probes were not merged into that line-coverage report. Executing a body alone does not establish its browser-observability contract.

After repairs, run the complete regression/static checks and unchanged compliance assertions, then the corresponding physical cells and release matrix. Do not weaken pixel thresholds, suppress intentional leaks, hide observer errors, accept textual action claims, or convert known failures into expected passes. Platform/API limits and granted clipboard/filesystem/camera access remain explicit limitations even after implementation defects are repaired.

## Evidence transfer

The machine-readable handoff records file hashes, run totals, supplemental findings, resume behavior and the evidence bundle. The bundle includes reports, raw native/browser logs, synthetic fixtures and diagnostic scripts. It excludes encrypted settings, credentials, browser profiles, caches, downloaded model weights and application binaries. Full evidence remains locally under the named run directories. The compact checked-in findings preserve the conclusions even if ignored run directories are later unavailable.
