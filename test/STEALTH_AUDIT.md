# Browser observability audit

This is an evidence-based audit, not a guarantee that Que is undetectable. The suite reports `PASS`, `FAIL`, `INCONCLUSIVE`, and `UNSUPPORTED` for the exact tested configuration. Required unsupported or missing coverage prevents a complete pass. Known defects are real failures, not expected-failure tests.

For a hands-on manual session (runner, observer setup, calibration, every physical test, analysis with `test/stealth/manual.py`), follow the [manual testing walkthrough](STEALTH_MANUAL_TESTING.md).

Production code is unchanged. Ordinary regression tests and compliance checks have separate outcomes and CI jobs. See [findings](STEALTH_FINDINGS.md) and the [research and investigation ledger](STEALTH_RESEARCH.md).

## Run

Use the repository environment with `requirements.txt` and Node.js 22 or newer installed on Windows (Node runs the JavaScript detector self-tests). The project’s existing Nuitka **standalone** distribution is supported; release evidence must use the exact executable and adjacent DLLs being released.

```powershell
python test/run_stealth.py --mode offline
python test/run_stealth.py --mode full --target source --browser all
python test/run_stealth.py --mode full --target source --browser all --cross-origin-isolated
python test/run_stealth.py --mode full --target C:\build\que.dist\que.exe --browser all
python test/run_stealth.py --resume .stealth-runs\20261114-120000
python test/run_stealth.py --resume .stealth-runs\20261114-120000 --report-only
```

Use `--output <new-directory>` for a named run. `--browser chrome`, `edge`, or `firefox` limits the reported scope; it does not establish coverage of the other browsers. Link a packaged run to its source compliance evidence with `--source-audit <offline-run-directory>`. The link records source hashes and report hash; it does not prove that the executable was built from those sources. Retain the build provenance separately.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | All required checks in the selected scope passed. |
| 1 | One or more genuine failures; the report can also contain coverage gaps. |
| 2 | Incomplete coverage, including required unsupported APIs or hardware. |
| 3 | Runner/setup error; no successful audit conclusion. |

Offline mode executes the real hook callback with `KBDLLHOOKSTRUCT` values and mocked downstream Win32 APIs/actions. It never installs a keyboard hook. It also instantiates the real Qt UI in an isolated, offline subprocess, checks the reviewed action inventory, and runs all existing regression and harness tests with live connections denied. Windows DPAPI tests need access to the normal Windows account profile; sandbox denial is a prerequisite problem, not an application defect.

## Full desktop procedure

1. Use an unlocked Windows 11 workstation. Close other Que instances. Update the installed stable Chrome, Edge and Firefox, and record the versions/channel. Prepare a single 100% monitor setup and a dual 100%/150% setup with the secondary monitor left of the primary. Record keyboard layouts/IMEs and speaker/headset devices.
2. Start full mode. The runner creates ordinary headed browser instances with **new dedicated profiles**. No disabled browser security, fake devices, synthetic physical keys, or production hook patches are used. Browser versions, source/executable hashes and native environment information are retained.
3. The observer opens before Que. Grant the permissions needed by the selected scenario. Set up capture before entering `launch` in the runner for first-paint/startup tests. `launch` starts only the disposable application copy. Configure the actual browser/window/display state and confirm it in the page; choosing a dropdown does not change Windows or the browser state.
4. Select an action and its configuration/repeat. Read its completion contract. Use synthetic chat text/documents and the supplied phrases in `fixtures/`. Full runs require real Gemini/Claude requests, microphone and WASAPI recording, transcription downloads, and results; they do not silently substitute mocks for unavailable services.
5. Start the five-second setup countdown, restore browser focus, and perform the physical action only once the page says **MEASURING**. Setup events are retained, with a separate measurement interval. Do not switch to the terminal during measurement. Action-caused focus loss, fullscreen exits and pointer-lock loss remain visible findings. Deliberate browser fullscreen transitions also appear in the timeline; distinguish the requested transition from an unsolicited exit in the review.
6. After the timer ends, create hash-bound receipts for the completed action and physical configuration (below), enter their references and each required event reference, then review the timeline/frames and record exposures. A quiet timeline without action evidence cannot pass. Repeat each deterministic configuration three times. All missing matrix cells remain incomplete, including unsupported Que-window capture sources.
7. Between intervals, `snapshot` saves screenshot/model hashes and current display information; use those paths in completion evidence. `report` writes current reports. Quit Que normally to measure clean shutdown; `launch` can then start another lifecycle. `quit` ends the runner and stops processes it still directly owns. Close any remaining windows of the dedicated test profiles before resuming.

Runner commands: `launch`, `calibration`, `snapshot`, `report`, `dummy-key`, `live-key`, `stop`, `quit`. `stop` explains how to perform a normal measured application shutdown; it does not inject a quit shortcut.

The initial run copies only the saved encrypted Gemini credential, or uses `QUE_AUDIT_GEMINI_KEY` if explicitly set. It never copies the user's history/screenshots or browser profiles. Child environments allow necessary Windows/account/CLI settings and isolate temporary files, local caches, and the Claude working directory. Claude retains its existing subscription login; fresh synthetic conversations may still be retained in Claude's normal account transcript storage. No existing Claude sessions are resumed by the fixture.

For credential-reveal scenarios, quit the disposable application, run `dummy-key`, then `launch`. The browser recorder refuses the key-reveal control scenario outside that mode. Use `live-key` while Que is stopped to restore the live-service credential for later provider checks. Known plaintext credentials are redacted from JSON event/checkpoint text. **Only dummy values may be exposed visually**; PNGs are original browser frames, not OCR-redacted images.

Standalone directories are copied with adjacent runtime files and without `data/`. A standalone directory is recognized by `.dist` or an adjacent `python*.dll`. An individual executable elsewhere is staged as a single executable. Onefile/custom builds can derive runtime paths from their extraction directory: their data path and build layout require explicit desktop verification before treating their evidence as release validation. Temporary files are redirected into the run. Never infer onefile path isolation merely from an executable hash.

## Capture calibration and interpretation

The observer uses real `getDisplayMedia()` and logs labels, settings, decoded-frame timestamps, pixel differences and saved PNGs. Set the footprint ROI in **captured video pixels**, covering Que plus its borders/shadow. Set a separate beacon ROI over the changing square. These coordinates differ from logical desktop coordinates on scaled displays.

Hide Que and save an unobstructed baseline. Enter `calibration` in the runner to display an ordinary native window, move it over the footprint, and measure the positive control. Close it and confirm restoration of the baseline. Recalibrate after changing sources, geometry or scaling. An unchanged/black stream or wrong source cannot satisfy calibration. The changing beacon detects frozen streams. For a tab/window source that isolates unrelated native windows, toggle the page positive control over the same footprint, measure it, remove it, and confirm restoration. Monitor capture requires the native control. The evaluator requires retained baseline/positive/restored-negative images, ten unchanged negative frames, a matching calibration epoch and actual source metadata. A failed control remains incomplete; investigate the mapping and source instead of accepting it.

The detector examines every delivered decoded-frame callback and records **actual** frame intervals/presented counts. It detects any changed pixel, including a one-channel-level change. Changed frames are queued for retention; unchanged examples are saved about once per second. Uploads do not block scheduling the next callback. Backpressure, fallback animation callbacks, stale calibration and missing frame artifacts make negative coverage incomplete. Events between samples can be missed; no startup/transient invisibility guarantee follows. Compare changed pixels, black replacement, borders, shadows and missing underlying content. Compression/resizing or unrelated content changes can cause differences: retain and review those findings instead of declaring them Que identification. Capture exclusion disabled is both a positive exposure demonstration and a failure of an absolute invisibility claim in that configuration.

Native window affinity, z-order events and 50 ms target rectangle samples are separate diagnostics. `native/summary.json` gives actual gaps and potential overlap/recovery durations. Overlap does not prove physical occlusion: transparency and cloaking can matter. Physical confirmation is mandatory, because an excluded screenshot cannot establish whether Que was physically visible. The one-second raising timer receives no exception.

## Audio, providers, and permission boundaries

Run both audio acquisition orders and speaker/headset configurations. The page records device enumeration/change, acquisition errors, mute/unmute/end, Web Audio sample energy/time and local-peer WebRTC statistics. Confirm actual **You** and **Them** transcript segments using the supplied distinct phrases. An indicator alone is insufficient. Record cold/cached loading, model downloads, silence, playback, device loss, errors, hidden recording, shutdown, transcript interactions and provider submission as described in each action.

Provider contracts require live discovery/checks, text/screenshots/documents/transcripts, streaming/cancellation, switching, history and shutdown. Offline failure tests exercise authentication, quota, server, timeout, connection, screenshot and model failure paths without exhausting live quotas. Native socket inventory is limited to Que and its descendants. The page's reachable endpoint is a harness-owned calibration; Resource Timing, service-worker observations and WebRTC statistics are page-scoped, not system-wide packet capture. Public-origin local-network policy and any additional owned service endpoint must be recorded as separate deployment-specific coverage; loopback calibration does not establish that permission boundary for every website.

Clipboard, synthetic-directory, camera and microphone demonstrations are permission-boundary limitations. Copy/read only synthetic data. APIs such as directory access or keyboard lock that are unavailable in a browser remain `UNSUPPORTED`, never successful tests. Camera evidence requires a camera physically seeing the screen.

## Statistical experiments

`statistics-schedule.json` preregisters two independently randomized batches of 20 baseline/action pairs for each selected browser and condition: absent sham, idle, screenshotting, recording, transcribing, streaming, unrelated worker CPU load and unrelated loopback network load.

The page lists each `stat.<browser>-<condition>.<batch>.<pair>.<phase>` interval in its prescribed order. Baseline is Que absent. The action phase is the named state. Set up outside measurement, keep the condition active for at least 15 seconds, and record action-completion evidence. These experiments explicitly use the single-monitor 100%, normal-window configuration; other statistical configurations require additional registered experiments and are not inferred.

The analyzer derives animation, event-loop, worker, canvas, network, GPU, audio-delivery, bounded storage and memory timings from recorded intervals. Enable the appropriate probes before statistical measurement; missing metric samples remain incomplete. It refuses missing/reversed/unreviewed intervals and insufficient samples; typed-in summary numbers cannot substitute. It evaluates interval means, p95 values and standard deviations, then reports paired effects, standardized effects where defined, Bonferroni-corrected bootstrap intervals and reproduced direction. The preregistered equivalence bound is ±1 ms for these timing metrics/reducers. A negative requires both intervals inside that bound, an effective generic load control and a stable absent/sham comparison. This does not exclude smaller effects, SSD contention when the fixture is cached, other working sets, other configurations or classification attacks. Ineffective controls and inadequate sensitivity remain incomplete. Existing statistical failures are retained with immutable summary artifacts after retries. A reproducible timing difference is a potential inference channel, not proof that a site identified Que. Native diagnostics and transport latency must not be presented as JavaScript visibility into other processes.

## Evidence and interrupted runs

Each run has JSON, JUnit and standalone HTML reports, `guide.html` reproduction contracts, the manifest, source/executable hashes, browser-only JSONL timelines/PNGs, and a separate `native/` directory. Profiles, encrypted settings and provider/model caches stay under `isolated/` or `private/` and are not CI artifacts. Full evidence can contain browser device labels and deliberately exposed synthetic content.

Sequence numbers, content-checked retries, persisted hash chains, durable append/acknowledgement and atomic checkpoints preserve transport evidence. Altered duplicate events, changed/truncated persisted timelines, unknown attempts and clock reversals are rejected. These hashes detect accidental alteration; they do not authenticate evidence against an actor who can rewrite the entire run directory. Resume recovers flushed events and marks unfinished intervals incomplete. Reruns create new attempts; prior failures remain. Missing actions, logger errors, browser closure, unacknowledged batches, dead observers and frozen capture cannot produce a pass. Resume checks staged application and harness hashes; after code changes, start a new run and retain the old evidence. `--report-only` evaluates saved evidence without launching browsers/services.

## Inventory and maintenance

`stealth/manifest.py` and `stealth/cases.py` define the hotkeys, individual control/surface/workflow stages, failure/background sites, pairwise state combinations, selected high-risk triples, and the display/fullscreen/capture matrix. Each stage needs its own evidence. Pairwise and selected triple acquisition orders have separate identifiers. Every deterministic desktop action requires calibrated monitor capture; dedicated capture-surface cases additionally require all selected source types. Statistical intervals remain a separately bounded timing experiment. Acquisition-order and concurrency coverage is not inferred from one completed stage. `stealth/inventory.json` snapshots source constructors/connections, including custom interactive widgets and module-level dialogs. `stealth/qt-controls.json` records actual instantiated controls with synthetic history, chat, screenshots, transcripts and dropdowns. A source or runtime inventory difference fails compliance and requires a reviewed scenario decision. Do not blindly refresh snapshots to silence a failure.

`stealth/operations.json` additionally inventories routines, conditional branches, failure handlers and scheduled work with semantic hashes. The offline runner records actual source line hits and emits `native/execution-gaps.json`; unexecuted entry/body lines leave coverage INCONCLUSIVE. Subprocess fixtures run through `stealth/traced.py` (the native surface probe and the Qt inventory) count too. `stealth/known-execution-gaps.json` lists the accepted gaps, and `coverage.ratchet` fails on any new unexecuted path and on any known gap that tests now execute, so the list only shrinks: remove covered gaps with `python test/stealth/approve.py prune-gaps RUN`, and approve reviewed operation changes one identity at a time with `python test/stealth/approve.py operations IDENTITY ...`. Matching an inventory, or merely executing a line, does not establish browser protection.

The matrix is intentionally large (over 190,000 declared desktop cells before statistical intervals). These are required contracts, not executed tests. Use resumable sessions and narrow browser selections for development; selected subsets do not establish the complete multi-browser contract. Some source types/APIs are inherently unavailable, so the complete absolute contract may remain incomplete or fail.

```powershell
python test/stealth/regressions.py
ruff check src test native
ruff format --check src test native
mypy --strict src test native
Get-ChildItem test/stealth/*.js | ForEach-Object { node --check $_.FullName }
node test/stealth/detector_test.js
foreach ($browser in "chrome", "edge") {
    python test/stealth/browser_smoke.py --browser $browser --output .stealth-runs/$browser-smoke
    python test/stealth/browser_smoke.py --browser $browser --cross-origin-isolated --output .stealth-runs/$browser-isolated-smoke
    python test/stealth/resource_smoke.py --browser $browser --output .stealth-runs/$browser-resource-smoke
}
```

Headless smoke checks verify observer startup/HTTP delivery only. They never count as physical keyboard, capture, audio or topmost validation. `.github/workflows/stealth-audit.yml` runs every automated check above plus the offline compliance run on every push, and publishes deterministic compliance failures without `continue-on-error` and leaves the existing static-analysis workflow intact. Interactive validation belongs on an unlocked workstation, not headless CI.


## Evidence receipts

Free-text completion/proof claims no longer pass. Use `event:<sequence>` only for an observed API event compatible with that role. For native actions, completion, configuration, transcripts and physical visibility, retain raw evidence inside the run and create a receipt after reviewing it:

```powershell
python test/stealth/receipt.py .stealth-runs/RUN ATTEMPT-UUID --role completion --role physical_action --role state_transition --artifact native/action-video.mp4:physical-video --before "Overlay hidden" --after "Overlay visibly shown by the physical chord" --observed-ms WALL-MS-IN-INTERVAL --reviewer "Reviewer name"
```

Paste the returned `receipt:relative/path.json#sha256` into the relevant fields. `checkpoint.json` contains attempt IDs and exact wall-clock intervals. Receipts bind the action ID, attempt, configuration, observation time, reviewer, before/after states, and raw artifact hashes. Do not cite a receipt for another action or infer success from a recording indicator. Transcript roles require an actual `transcript` artifact. Physical-visibility roles require `physical-video` or `physical-photo`; an excluded screenshot or affinity flag cannot substitute.

For `physical_configuration`, include both a physical image/video and a `native-display-snapshot` from the runner's `snapshot` command. Its display timestamp must be within 60 seconds of the observed configuration, and its actual monitor count, DPI and left-secondary placement must match the selected cell. The reviewer must also establish placement, browser mode, input layout, devices and selected settings from the raw evidence. The tool validates references and consistency; human review remains necessary to interpret physical recordings. A receipt is not an automatic visual oracle.

Observed focus/fullscreen/pointer-lock effects have no blanket workflow exemption. Deliberate browser transitions remain labeled observations of that interval and must not be described as proof of Que identification. Likewise, pixel changes are retained effects requiring attribution; noisy capture cannot be made to pass by increasing a tolerance.

## Additional probes and public-origin experiment

The [research ledger](STEALTH_RESEARCH.md) records primary sources, executable probes, controls, hypotheses and remaining limitations. Idle detection needs at least 65-second intervals. GPU/audio/storage/memory probes add their own workloads: enable identical probes during matched baseline/action intervals. Storage preparation happens before measurement, defaults to a 64 MiB owned OPFS fixture, and stops/removes only its own file through the dedicated button. Directory access is confined by a marker to the disposable fixtures/data directory; its hashes cover the first 64 KiB, not arbitrary large model contents.

Use separate run directories for ordinary and `--cross-origin-isolated` conditions. The latter sends standard COOP/COEP/CORP headers, checks the observed isolation state and enables normal browser isolation behavior without security-disabling flags. It does not reproduce all high-resolution timer or cache attacks.

For an actual permissioned HTTPS origin:

```powershell
python test/stealth/network_origin.py --origin https://YOUR-PERMISSIONED-HOST --output .stealth-runs/RUN/native/owned-network.jsonl
```

Host `test/network-origin-test.html` on that origin through your normal approved workflow. Enter only the printed owned control URL. Run endpoint-present/absent controls, permission states and Que conditions, download the page observations, and retain them with the native endpoint log. The endpoint returns only a synthetic marker, never application/native evidence. Fetch and WebSocket are separate observations. This artifact is ready for deployment; no external site is published by the audit runner.


The optional real-worker smoke test uses the browser debugger to click only the observer's own storage/memory controls, with a 1 MiB owned file, and verifies cleanup. Those actions are explicitly synthetic harness operations and never count as physical Que actions:

```powershell
python test/stealth/resource_smoke.py --browser chrome --output .stealth-runs/chrome-resource-smoke
python test/stealth/resource_smoke.py --browser edge --output .stealth-runs/edge-resource-smoke
python test/stealth/browser_smoke.py --browser edge --cross-origin-isolated --output .stealth-runs/edge-isolated-smoke
```

Core observer readiness no longer waits for service-worker registration. Network/provider scenarios separately require actual service-worker, resource-timing and reachable-endpoint observations; an unavailable or stalled worker cannot silently satisfy those roles.

The page keeps only a bounded recent event window in memory so long sessions do not grow the observer heap indefinitely. Downloads explicitly report evicted in-memory events; the acknowledged append-only browser JSONL remains the complete timeline. Non-text clipboard data and granted directory metadata are boundary exposures even without a plaintext payload. Pixel comparison includes alpha where present.
