# Meeting and recording capture audit

This suite acquires real remote-participant media or actual recording files. It is separate from the existing browser-observability audit. An offline unit-test pass, a successful Windows affinity query, and a sender preview do **not** establish meeting coverage.

The initial matrix includes Zoom desktop/web, Meet Chrome/Edge, Teams desktop/Chrome/Edge, Discord desktop/Chrome, Webex desktop/Chrome, OBS, Snipping Tool, Game Bar, and local Chrome/Edge screen sharing. It enumerates monitor/window/tab sources where applicable, explicit Zoom capture modes, supported OBS capture methods, every selected current display, and three repetitions. Cloud recording, physical-keyboard validation, acoustic microphone-content validation, system inspection, and packaged-release certification remain separate.

## Installation and first run

Run from the repository root in the existing Python environment:

```powershell
python -m pip install -r test/requirements-capture.txt
Copy-Item test/capture/example.json .stealth-runs/capture-config.json
python test/run_capture_audit.py preflight --config .stealth-runs/capture-config.json --output .stealth-runs/capture-preflight
```

Create `.stealth-runs` first if necessary. Preflight does not join meetings, launch recording, or change client settings. The checked-in template deliberately has no room URLs or invented source-selection recipes. Missing setup returns exit code **2** and lists the missing prerequisites. Supplying an application name alone does not mean it was tested.

Set up dedicated test meetings/channels and two test participants. Use an unlocked Windows desktop and installed supported clients. Do not run live tests while using this desktop for other work: the suite changes foreground windows, shares the fixture, exercises Que surfaces, plays a calibration tone, and records received audio/video. Use synthetic content and keep cameras off.

Create separate browser directories for sender and receiver. Each must contain an empty `.que-capture-profile` marker. Sign into test accounts using these dedicated profiles; never point the configuration at a personal browser profile. The runner uses installed Chrome/Edge, so no Playwright browser download is required. Authentication renewal, lobby permissions, and app installations remain initial/external prerequisites.

The test-only FFmpeg package supplies a decoder if `ffmpeg` is absent from PATH. An explicit `ffmpeg` path overrides it. A separately installed `ffprobe` adds stream metadata; unavailable ffprobe is recorded and does not replace actual decoded timestamps.

```powershell
python test/run_capture_audit.py run --config .stealth-runs/capture-config.json --apps meet-chrome --output .stealth-runs/capture-meet
python test/run_capture_audit.py run --config .stealth-runs/capture-config.json --apps meet-chrome --resume .stealth-runs/capture-meet
python test/run_capture_audit.py report --config .stealth-runs/capture-config.json --output .stealth-runs/capture-meet
```

Omit `--apps` for the entire matrix. Resume skips cells whose acquisition completed and reruns interrupted or incomplete ones. Empty `monitors` selects all currently attached displays. `repeats: 1` is useful for development and is explicitly incomplete relative to the required three repetitions. Source/configuration/harness/display changes require a new run. Report-only analysis never starts a browser or a meeting. Resuming retains old attempts and failures.

## Local Chrome/Edge screen sharing (no accounts)

`chrome-local` and `edge-local` need no meeting, account, profile, or recipe; `{}` is a complete configuration. Chromium's own `getDisplayMedia` shares the entire screen, the fixture window, or the fixture tab, and the captured track is recorded directly. Command-line auto-selection (`--auto-select-desktop-capture-source`, `--auto-select-tab-capture-source-by-title`) replaces the source picker, so the runner does not report picker, share-restart, or source-change results for these apps. This covers the capture stack browser meeting clients share. It does not cover their WebRTC encoding, transport, or receiver rendering, so it does not replace the meeting cells.

```powershell
python test/run_capture_audit.py run --config .stealth-runs/capture-config.json --apps chrome-local,edge-local --output .stealth-runs/capture-local
```

With several displays, Chromium names them `Screen 1`, `Screen 2`, and so on. The adapter picks `Screen <index + 1>` and refuses to record if the shared resolution differs from the configured display. For entire-screen shares it dismisses Chromium's floating "is sharing your screen" bar (its **Hide** link) before recording, because the bar sits and repaints inside the captured screen. Browser meeting recipes need the same step. Auto-selected screen and window shares carry no audio track, so only tab shares report the audio check.

## App setup recipes

Meeting adapters use Playwright for web clients and Windows UI Automation for desktop clients and native source pickers. Client labels, lobby screens, and media selectors differ by version and account policy. Initial setup therefore includes recording the correct selectors for the installed client. A changed selector fails acquisition; there is no coordinate guessing or silent skip.

Browser actions use either a CSS `selector` or an exact accessible `role` and `name`. Supported actions are `click` (default), `fill` with `text`, `check` with `checked`, `expect`, and bounded `wait` with `ms`. For example:

```json
{"role":"button","name":"Join now","action":"click"}
```

Native actions have a `window` title regex and optional `control` properties accepted by UI Automation (`title`, `auto_id`, `control_type`). Actions are `click`, `keys` with `keys`, `select` with `value`, `expect`, `wait`, and `inventory`. `inventory` retains **all** descendants, including unnamed entries, before selection. For example:

```json
[
  {"window":".*Share.*","action":"inventory"},
  {"window":".*Share.*","control":{"title":"Que capture audit fixture","control_type":"ListItem"}},
  {"window":".*Share.*","control":{"title":"Share","control_type":"Button"}}
]
```

Verify these properties against the actual UI; this example is a recipe shape, not a universal vendor selector. A desktop client can be inspected locally using `pywinauto.Desktop(backend="uia").window(title_re="...").print_control_identifiers()`.

Each meeting configuration supplies:

- `meeting_url`, distinct `sender_profile`/`receiver_profile`, and `sender_join`/`receiver_join` recipes. Desktop senders instead supply `executable`, optional `arguments`, native `sender_join`, and native `leave` to leave the dedicated test call.
- `share`: a map from `monitor`, `window`, or `tab` to actions that open the source picker. `picker`: matching native recipes that inventory, select the exact source, and confirm. Monitor selection must match the configured display.
- `receiver_selector`: a selector matching exactly one actual received share video/canvas. `receiver_watch` can open or pin the share before recording. Selecting the wrong stream cannot pass the source-nonce calibration. Receiver background throttling is disabled only to keep its observer active; the sender uses ordinary capture selection.
- `inspect.picker` and `inspect.shell`: native recipes that open the relevant picker or shell view, inventory it, and dismiss it. The ordinary fixture must be listed both with Que hidden and present. Changes, including anonymous entries, remain inconclusive unless they identify Que. Retain individual shell views as separate inventory steps to cover Alt-Tab, task view, taskbar, and tray.
- `modes`: for each nondefault Zoom capture mode, supply `set` and `restore` native recipes. Restoration must reproduce the initial settings.
- `operations.share-restart` and `operations.source-change`: transition recipes with `browser` and/or `native` actions. Recorders accept native action lists. These are observed through the receiver stream; missing recipes remain incomplete. A transition that loses the recorded media track remains incomplete instead of passing on stale footage.
- `local_recording` for Zoom/Webex desktop: `start`/`stop` native recipes and a `recordings` directory. The finalized local file is decoded separately from receiver footage. Ambiguous new files are rejected.

For OBS, enable its local WebSocket server, configure the port, and put its password in the environment variable named by `password_env` or in the repository's ignored `.env` file (`QUE_CAPTURE_OBS_PASSWORD=...`). The value is read only by the OBS adapter and never exported to browsers or fixtures. The runner refuses an OBS instance already streaming or recording. `sources` is keyed by `source.method.monitor`, for example `monitor.auto.0`, with the exact OBS `kind` and `settings` for that backend. Obtain these from the installed OBS source properties/WebSocket API; backend numeric values and monitor identifiers must not be inferred across versions. `python test/capture/obs_sources.py` prints a ready `sources` block from the running OBS: it lists the real method and display values through a temporary, disabled scene that it then removes. Window sources match the fixture by title. The audit creates a temporary scene, restores the previous scene/output path, and never removes user scenes.

The OBS adapter fits the source to the canvas and waits for OBS to finish writing before it hashes the recording and restores the previous scene and recording folder. Each restore step runs even if another fails. The original values are also saved to the attempt's `private/obs-restore.json`. It refuses to start while scenes named `Que-capture-audit-*` remain from an earlier interrupted run. On hybrid-GPU laptops OBS's DXGI and BitBlt methods can record black; those cells stay incomplete with "never showed the calibrated fixture". The OBS audio check needs the Audio Output Capture source on the device that plays the fixture's tone.

Snipping Tool and Game Bar use native `start`/`stop` recipes and their `recordings` directory. Close existing recordings before setup; the runner requires one unique new finalized MP4 and preserves the original file. Snipping Tool must select the full fixture region; Game Bar targets the fixture application window.

## Evidence and interpretation

Every path records a random source nonce, continuously changing visual counter, baseline, held-out negative, black rectangle, translucent rectangle, thin border, text control, and restored negative. Compression thresholds are trained only on the initial baseline. Each monitor-captured Que surface also has an intentional exposure with protection disabled. In-source controls handle tab/window isolation. Capture settings in the user's normal Que instance are not changed.

The fixture drives production widgets on the Qt thread, with native capture policy and visibility timers active. Provider responses/model inference are synthetic and the physical-input helper is not started. Real microphone/WASAPI acquisition is exercised separately from transcription. The suite records visible action receipts and checks the requested popup/panel actually opened. It does not add an automation endpoint to production Que.

Decoded frames use their actual presentation timestamps without frame-rate conversion. The runner parks the mouse cursor in the fixture header, outside the measured region. Measurement starts one tile below the header, whose identity bits change every frame. Calibration widens each tile's bounds with baselines shifted by up to two pixels, and a 2x2 box filter suppresses single-pixel codec ringing. The fixed margin (`NOISE_MARGIN` in `detector.py`) was set from live Chrome/Edge captures: post-calibration noise reached about 9 levels, the weakest positive control about 31, and exposed Que surfaces at least 108. Setup frames before the first nonce-matched baseline are counted separately; Que's surfaces are created only after calibration. Reports reject wrong, frozen, cropped, low-resolution, uncalibrated, or interrupted measured streams. Canvas-based receivers are explicitly incomplete for every-delivered-frame coverage. Compression differences exceeding the calibrated envelope are findings, not proof of readable Que text. No result establishes invisibility between observed frames or in an untested capture backend.

Reports include JSON, HTML, JUnit, raw recordings, original action receipts, picker inventories, detector measurements, and worst-frame PNGs. All paths are under the run directory. Private profiles, account URLs, and detailed vendor exceptions stay in private setup/error locations; do not publish those directories. Evidence hashes catch accidental alteration, not an actor able to rewrite the entire audit. Earlier failures remain failures after retries or missing evidence.

Exit codes: **0** all required selected checks pass; **1** observed failures; **2** incomplete/unsupported required coverage; **3** runner/configuration/report error. Missing accounts, recipes, audio controls, source transitions, or shell evidence cannot become successful skips. The capture-visible option is used only as an intentional positive control.

## Development checks

```powershell
python -m unittest discover -s test -p test_capture_audit.py -v
python test/capture/smoke.py --output .stealth-runs/capture-fixture-smoke
python test/stealth/regressions.py
ruff check src test native
ruff format --check src test native
mypy --strict src test native
node --check test/capture/receiver.js
```

The explicit fixture smoke opens real native Qt surfaces but makes no capture claim. Ordinary regression discovery runs detector/configuration/lifecycle tests without joining live services. Tests also encode/decode actual H.264 when the optional test dependencies are installed. Use live runs only on the prepared unlocked desktop; hosted headless CI cannot establish Zoom, Meet, or Teams receiver coverage.
