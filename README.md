# Que
Your live AI programming assistant

Install dependencies with `python -m pip install -r requirements.txt`.

The thinking toggle selects high effort when enabled. When disabled, it uses minimal
effort for Gemini 3 Flash, low effort for Gemini 2.5 Flash, and the default off setting
for Gemini 2.5 Flash Lite. Interactions supports
[thinking levels](https://ai.google.dev/gemini-api/docs/thought-signatures?hl=en),
not the previous zero-token thinking budget.

Run backend regression tests without API calls from PowerShell:
```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s test -p test_interactions.py -v
```

Compile .exe using Nuitka
```bash
pip install nuitka
python -m nuitka --standalone --windows-icon-from-ico="src/assets/que.ico" --windows-console-mode=disable --enable-plugin=pyqt6 --include-data-dir="src/assets=src/assets" --include-data-dir="src/data=src/data" src/que.py
```
> [!NOTE]
> Due to the large google.genai library, the first compilation will take a long time (20-30 minutes using Nuitka).
> Subsequent compilations will be faster due to cached files in the build directory.
