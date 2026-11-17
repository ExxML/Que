# Observability findings — 2026-11-14

## Deep review results

The current compliance result is **FAIL**, with **3,737 PASS, 6,813 FAIL and 1 INCONCLUSIVE** offline checks. The failing count is mostly repeated hook-release permutations, not thousands of distinct bugs. Broader desktop/release coverage remains **INCOMPLETE**.

- **111 regression/harness tests passed**, including deliberate leaks, forged/free-text proof, altered/truncated evidence, wrong configurations, small pixel changes, non-text clipboard content, metadata-only directory exposure and substituted/missing camera artifacts.
- Ruff lint, formatting and strict mypy passed for production plus the suite (60 Python files). JavaScript syntax and the actual detector's single-pixel/alpha/unthrottled-frame self-tests passed.
- Chrome and Edge loaded the observer; ordinary and COOP/COEP isolation conditions were checked. Real OPFS/memory worker tests produced samples and removed their owned 1 MiB fixture in both browsers. These are headless harness tests, not physical Que detection trials.
- The synthetic network endpoint passed HTTP/CORS/WebSocket and foreign-origin/native-path rejection checks. Actual permissioned HTTPS-origin browser behavior remains unmeasured.
- **329 source entry/branch/failure bodies remain unexecuted** by the combined offline fixtures. They are listed in `native/execution-gaps.json` and keep coverage incomplete. The inventory contains 323 routines, 310 conditional branches, 35 exception handlers and 24 background-call sites. Inventory membership is not execution or protection evidence.
- The manifest now has **676 contracts and 199,935 declared desktop cells**, before statistical intervals. These are outstanding test obligations, not passed tests. Pairwise acquisition orders and selected triple orders are separate cases; every deterministic desktop action requires calibrated capture.

New native diagnostic defect: **`failure.topmost-query-error`** invokes the unchanged `_is_topmost_window()` with a geometry-query exception and observes `True`. Failed observation is being reported as topmost success. The retained trace is `native/failure-traces.json`; this does **not** establish physical occlusion or a website-visible leak. Existing hook leaks remain FAIL, now exercised with mixed generic/left/right modifiers and independent partial-release orders.

The review closed these false-pass paths: free-text action claims, modifier-only leakage, ignored small pixel changes, missing source-matched calibration artifacts, failed probes, unverified configuration, changed duplicate/persisted events, broad action checklists, missing acquisition orders and underpowered/uncalibrated statistical negatives. Camera evidence must contain actual camera frames. Every retained action frame is checked against its file hash during evaluation. Receipts retain raw artifact hashes and reviewer attribution; physical recordings still require real human interpretation.

Additional research produced runnable idle, pressure, EyeDropper, display-change, AudioWorklet, GPU, bounded storage/memory and clipboard-format probes; see the [primary-source research ledger](STEALTH_RESEARCH.md). No new research hypothesis is reported as proven Que identification.

An initial Edge isolated-context smoke found that readiness waited on service-worker registration. Core startup now proceeds independently, while network/provider scenarios separately require actual worker/resource/endpoint observations. The failed output is retained alongside the successful retry. A temporary encoding issue in an earlier local test run was corrected; no production code or unrelated regression content changed.

Current offline artifacts: `.stealth-runs/final-review/`, with browser/worker/protocol smoke evidence in the paths recorded in the JSON findings (removed; see git history). The final report hash and representative new traces are retained there. Resume/report-only verified unchanged harness and staged-source hashes and preserved FAIL. Source files under `src/` are unchanged. No physical keyboard/capture, live-provider/audio, mixed-DPI, public-origin policy, or exact-release validation is claimed. The existing `que.dist/que.exe` candidate was located and hashed; its release provenance is unverified.


The suite was implemented without changing `src/`. This initial audit **fails compliance**. It does not establish physical browser invisibility or release validation. See [run instructions](STEALTH_AUDIT.md).

Source revision: `324c3cb00356ec7625ca586a0c56c75fdff6232a`. The tested hook file SHA-256 is `4b46954c37bf5a7c56c6a1bd9c21b1ebba993c25095a4f10b7616bce5fde04ac`.

## Earlier verification before the deep review

| Check | Result |
| --- | --- |
| Controlled offline compliance audit | **517 PASS, 239 FAIL**, no incomplete offline checks; exit code 1. |
| Existing regression tests plus harness/failure self-tests | **89 passed**, with live connections blocked. |
| Ruff lint and formatting | Passed for production source and the new Python suite. |
| Strict mypy | Passed for 52 checked Python files, including production and the new suite. |
| Observer/worker/service-worker JavaScript syntax | Passed. |
| Real Chrome headless observer smoke | Passed; 1,029 recorded events, no observer/logger errors. |
| Real Edge headless observer smoke | Passed; 1,012 recorded events, no observer/logger errors. |
| Native window collector smoke | Recorded 16 samples without window-collector errors; this did not validate Que visibility. |
| Native process/socket collector smoke | Observed the audit helper and its descendants using normal Windows diagnostic access. |
| Resume/report-only | Reopened the completed offline run and preserved its failing outcome. |
| Production diff | No changes under `src/`. |

The 239 failures are parameterized variants of the failure classes below, **not 239 distinct defects**. Hook tests execute the production callback with real Win32 event structures and mocked downstream APIs/actions. They are native diagnostics, not evidence that a website can inspect Windows processes or hooks. Headless browser checks establish observer startup and evidence delivery only.

The workstation reports Windows build 26200, one 2560×1600 monitor at 144 DPI (150%), and Python 3.10.11. Chrome and Edge were found; Firefox was not found in the supported installation locations. Neither required display configuration was physically exercised.

## Reproduced callback defects

| Finding | Reproduction and observed outcome |
| --- | --- |
| Repeated action key leaks after early modifier release | Ctrl down → E down → Ctrl up → repeated E down → E up. The repeated E down reaches `CallNextHookEx`; the matching E up remains suppressed. This also reproduces across other active hotkeys/modifier/message variants. See `hook.ctrl-e-release-ctrl-repeat-e`. |
| Visibility changes can release a still-held conditional shortcut | Ctrl down → H down → make the window hidden → repeated H down. The modifier and H are replayed downstream even though the original H press was suppressed. See `hook.visibility`. |
| Controlled duplicate modifier-down can leave downstream modifier state unbalanced | Ctrl down → ordinary A down/up (replayed) → another Ctrl down → Ctrl up. The final Ctrl up is suppressed, although the earlier Ctrl down was delivered. See `hook.modifier-repeat`. This is a controlled duplicate-event case; physical occurrence is not established. |
| Callback exceptions are not contained by the Python hook callback | A matched action callback raising a fixture exception escapes `_low_level_keyboard_proc`. Native ctypes dispatch behavior and physical consequences still require desktop confirmation. See `hook.callback-exception`. |
| Hook installation failure is not surfaced before the message loop | With `SetWindowsHookExW` returning null, `_hook_thread_entry` still enters `GetMessageW` without an explicit failure path. See `hook.installation.False`. |

These remain failing compliance assertions. No expected-failure markers or production fixes were added.

## Suspected exposures awaiting desktop confirmation

`MainWindow._init_UI()` calls `show()` before applying capture exclusion ([source](../src/ui/window/main_window.py)). That ordering creates a first-paint question; it is **not proof** that the compositor delivers an exposed frame. Test with capture already active before launch and retain actual frame intervals.

Popup/dialog/tooltip/tray/Claude-process capture exclusion, action-caused focus or pointer interruptions, fullscreen exits, handle replacement and topmost recovery remain unmeasured. Native affinity and style flags alone cannot settle those questions. The current one-second raising timer may permit observable occlusion; physical visibility and independent window ordering must be correlated.

## Platform limitations and remaining coverage

Windows capture affinity is not a universal content-protection guarantee. Clipboard read after copying, granted access to synthetic files, and a camera seeing the display are explicit permission-boundary demonstrations in the manifest. They must remain visible limitations rather than being described as successful invisibility tests. Page Resource Timing and service-worker observations are not system-wide traffic monitors.

No full physical-key runs, real capture-permission experiments, real microphone/loopback recording, transcription downloads, live Gemini/Claude requests, statistical desktop batches, mixed-DPI/multi-monitor trials, or exact packaged-release trials were performed for these initial findings. Their outcomes are unknown. Firefox, required 100%/mixed-DPI setups and the exact release executable remain validation prerequisites.

The current suite requires hash-bound action/configuration artifacts and actual API observations for those checks; unsupported sources and unperformed cells stay incomplete. It now provides a DOM positive control for browser-isolated sources and retains native calibration for monitor capture. The earlier native-control-only limitation is superseded; the physical exposure experiments are still required. Loopback latency/socket calibration does not establish public-origin local-network permissions or external-network contention. Single-file/custom Nuitka extraction paths explicitly remain incomplete; the project's standalone distribution is the supported release-validation layout.

Earlier evidence remains in `.stealth-runs/final-verified/` (ignored by Git); the deep-review evidence is listed above. The CI compliance job is intentionally expected to be red for this source revision and publishes its genuine failures separately from regression results.
