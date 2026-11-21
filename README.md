# Que
Your live AI programming assistant

Install dependencies with `python -m pip install -r requirements.txt`.

Run `python src/que.py`, then open **Settings** in the overlay:

- **Gemini:** paste a Gemini API key, refresh models, and optionally select **Test key** to verify generation access. The test sends a tiny request and uses API quota. Keys are encrypted with Windows DPAPI for the current Windows account; Que no longer reads `.env`. Existing users should paste their key once in Settings.
- **Claude:** install Claude Code Terminal for Windows, sign in with `claude auth login` using your Claude Pro subscription, then select **Check connection**. Que uses that installed CLI and its subscription login. Native and standard npm installations are supported; npm installations also need Node on PATH. Update Claude Code if it does not support model discovery or the session protocol.
- Choose the provider's **default model** and **default effort**; changes apply and save automatically. Provider switches and pasted API keys are checked automatically before replacing the active configuration. Each provider retains its own defaults. The selectors below the chat input override them for the current chat; New chat restores the saved defaults.
- Optionally set up to 10 **presets** under the defaults, each a model and effort, such as a fast model for short answers and a high effort for detailed ones. **Caps Lock+1** … **Caps Lock+0** switch the current chat to that preset, as if chosen in the selectors below the input. Each provider keeps its own presets.

Model lists are retrieved from Gemini's Models API and Claude Code's initialization response. Claude effort choices come from the CLI's model capabilities. Gemini's Models API does not advertise per-model effort levels, so Que retrieves Google's published [thinking capability table](https://ai.google.dev/gemini-api/docs/thinking), caching the last successful result. Unknown capabilities show only **Default**, which lets the provider choose. Refresh models or check the connection to refresh these choices.

Claude replies stream through a hidden terminal process. Each Que chat owns a Claude conversation; subsequent turns resume that session at the last accepted reply. Switching providers preserves each provider's separate context. New chat resets both contexts, and canceled or failed replies are not committed. Screenshots are passed directly as image content. Claude also accepts PDF and text attachments; Gemini keeps its existing attachment handling. Claude's filesystem and command tools are disabled in Que.

Settings stay inside the existing overlay. The Gemini credential and provider defaults are stored in `src/data/config.json`, excluded from Git. Claude Code manages its own saved transcripts; clearing Que's chat ends its reference to the session but does not erase Claude Code's transcript files. Que starts a new conversation after restarting, matching its existing chat lifecycle.

**Interview shortcut:** **Caps Lock+C** takes a screenshot and sends an interview prompt along with any new audio transcript. It answers whatever was just asked in the format that suits it: a behavioural, about-you, resume or concept answer, a brainteaser worked through out loud, a coding walkthrough (clarify, brute force, optimize, code, dry run, complexity), a system design walkthrough, or, for a follow-up, hint, pushback or code fix on the current question, only what changed. Anything you've typed in Que's input is sent as a note (the question itself, a hint, or the kind of question, like "system design") and cleared. The chat shows only the shortcut's label. **Caps Lock+G** takes a screenshot on its own, to attach to a typed message. **Caps Lock+Esc** or the ■ button at the right of the input row stops a reply while it is generating; the text so far stays, marked "Stopped.", and like a failed reply the turn is not saved to history or sent as context, so its transcript is sent again with the next message.

**History:** open the sidebar with the ☰ button or **Caps Lock+H**. Each completed reply is saved to `src/data/chat_history.json` (plain JSON, excluded from Git) under the conversation's provider, shown as a **Gemini** or **Claude** label. Selecting a conversation restores its transcript and resumes its server-side context, switching the active provider for this session if needed (the saved default provider is unchanged). **Caps Lock+N** / the **+** (New chat) button starts a new chat and keeps the previous one in history. Delete a conversation with its × button (click twice) or the Delete key (press twice). Screenshots are not saved in history, and Gemini may expire old server-side conversations, in which case continuing shows an error and you can start a new chat.

Run backend and UI regression tests without API calls from PowerShell:
```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s test -p "test_*.py" -v
```

Run the separate browser-observability audit with `python test/run_stealth.py --mode offline`. For hands-on checks with a real keyboard, screen and browser, follow the [manual stealth testing walkthrough](test/STEALTH_MANUAL_TESTING.md). See the [audit guide](test/STEALTH_AUDIT.md) for guided browser and packaged-build runs, and the [initial findings](test/STEALTH_FINDINGS.md) for reproduced defects and untested coverage. Compliance failures remain failures; this suite does not certify universal invisibility.

For automated meeting and recording checks, see the [capture audit setup](test/CAPTURE_AUDIT.md). The separate runner validates actual participant media and recording files; test accounts, dedicated rooms, and client-specific setup are required. Missing live coverage is reported as incomplete. Local Chrome/Edge screen sharing (`--apps chrome-local,edge-local`) needs no accounts.

**Manual stealth test site:** to check by hand that a web page cannot tell Que is running, start the local test site and open it in the browser you want to test:
```powershell
python test/manual_site/serve.py
```
Open <http://localhost:8765/> (use `--port` to change it), launch Que, and drag it over the **Stage** (the checkered area on the right). Work through the tabs (Keyboard, Focus & pointer, Capture, Audio, Timing & system, Manual). For each test: press **Start**, click the page title so the page has focus, do the listed steps while it shows **MEASURING**, then answer the question about whether Que acted. Every Que shortcut starts with **Caps Lock**. Keep DevTools closed, and do not scroll, resize or zoom during capture tests; calibrate once in the Capture tab first (share the entire screen, then follow its five steps). The **Report** tab exports results as Markdown or JSON. A "No leak seen" verdict is evidence for that run only, not proof of invisibility. See the [manual stealth testing walkthrough](test/STEALTH_MANUAL_TESTING.md) for the runner-based procedure.

Que's keyboard and mouse hooks run in a small compiled library, `src/assets/que_hook.dll`, built from `native/que_hook.c` and committed with it, so running Que needs no compiler. After changing `native/que_hook.c`, rebuild it with MSVC (Visual Studio Build Tools, C++) or gcc and commit both files. Nuitka skips DLLs in data directories, so the build command below names it explicitly.
```bash
python native/build.py
```

Compile .exe using Nuitka
```bash
pip install nuitka
python -m nuitka --standalone --windows-icon-from-ico="src/assets/que.ico" --windows-console-mode=disable --enable-plugin=pyqt6 --include-package=google.genai --include-package=av --include-data-dir="src/assets=assets" --include-data-files="src/assets/que_hook.dll=assets/que_hook.dll" src/que.py
```
> [!NOTE]
> Due to the large google.genai library, the first compilation will take a long time (20-30 minutes using Nuitka).
> Subsequent compilations will be faster due to cached files in the build directory.
