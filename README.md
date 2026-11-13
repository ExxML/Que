# Que
Your live AI programming assistant

Install dependencies with `python -m pip install -r requirements.txt`.

Run `python src/que.py`, then open **Settings** in the overlay:

- **Gemini:** paste a Gemini API key, refresh models, and optionally select **Test key** to verify generation access. The test sends a tiny request and uses API quota. Keys are encrypted with Windows DPAPI for the current Windows account; Que no longer reads `.env`. Existing users should paste their key once in Settings.
- **Claude:** install Claude Code Terminal for Windows, sign in with `claude auth login` using your Claude Pro subscription, then select **Check connection**. Que uses that installed CLI and its subscription login. Native and standard npm installations are supported; npm installations also need Node on PATH. Update Claude Code if it does not support model discovery or the session protocol.
- Choose the provider's **default model** and **default effort**; changes apply and save automatically. Provider switches and pasted API keys are checked automatically before replacing the active configuration. Each provider retains its own defaults. The selectors below the chat input override them for the current chat; Clear chat restores the saved defaults.

Model lists are retrieved from Gemini's Models API and Claude Code's initialization response. Claude effort choices come from the CLI's model capabilities. Gemini's Models API does not advertise per-model effort levels, so Que retrieves Google's published [thinking capability table](https://ai.google.dev/gemini-api/docs/thinking), caching the last successful result. Unknown capabilities show only **Default**, which lets the provider choose. Refresh models or check the connection to refresh these choices.

Claude replies stream through a hidden terminal process. Each Que chat owns a Claude conversation; subsequent turns resume that session at the last accepted reply. Switching providers preserves each provider's separate context. Clear chat resets both contexts, and canceled or failed replies are not committed. Screenshots are passed directly as image content. Claude also accepts PDF and text attachments; Gemini keeps its existing attachment handling. Claude's filesystem and command tools are disabled in Que.

Settings stay inside the existing overlay. The Gemini credential and provider defaults are stored in `src/data/config.json`, excluded from Git. Claude Code manages its own saved transcripts; clearing Que's chat ends its reference to the session but does not erase Claude Code's transcript files. Que starts a new conversation after restarting, matching its existing chat lifecycle.

Run backend and UI regression tests without API calls from PowerShell:
```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s test -p "test_*.py" -v
```

Compile .exe using Nuitka
```bash
pip install nuitka
python -m nuitka --standalone --windows-icon-from-ico="src/assets/que.ico" --windows-console-mode=disable --enable-plugin=pyqt6 --include-data-dir="src/assets=src/assets" --include-data-dir="src/data=src/data" src/que.py
```
> [!NOTE]
> Due to the large google.genai library, the first compilation will take a long time (20-30 minutes using Nuitka).
> Subsequent compilations will be faster due to cached files in the build directory.
