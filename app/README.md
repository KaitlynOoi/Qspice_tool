# LTspice → QSpice Tools (packaged app)

This folder bundles your two scripts into **one single-run app**:

- `combined_app.py` — the entry point. One window, one "Run both steps" button.
  It runs, in order, on the same file, automatically:
  1. **Convert .asc → .qsch** (calls `asc_to_qsch_fixed.convert_asc_to_qsch()`, unchanged)
  2. **Fix & Import Missing Models** (calls `fix_and_import_models.process_models()`, unchanged)
  You no longer need to run two separate apps or re-select files in between —
  pick the `.asc` once, click Run, and both steps happen back-to-back in one log.
- `asc_to_qsch_fixed.py`, `fix_and_import_models.py` — your original scripts, untouched
  (the combined app just imports and calls functions from them directly).
- `requirements.txt` — the two things the app needs: `spicelib` (the open-source
  LTspice/QSpice library your scripts already depend on) and `pyinstaller`
  (the tool that bundles Python itself + all libraries into one `.exe`).

Once built, the resulting `.exe` is fully standalone: end users **do not**
need to install Python, spicelib, or anything else. They just double-click it.

## Important: why I can't hand you a finished .exe directly

Building a Windows `.exe` has to happen *on Windows* (PyInstaller doesn't
cross-build). I'm running in a Linux sandbox, so I can't produce the binary
myself. Below are two ways to get it — pick whichever is easier for you.

---

## Option A (recommended, no Windows PC needed): build it in the cloud with GitHub Actions

1. Create a free GitHub account if you don't have one, and create a new
   **public or private repository**.
2. Upload this whole folder (including the hidden `.github` folder) to that
   repository — either by dragging the files into GitHub's web uploader, or
   via `git push` if you're comfortable with git.
3. Go to the repo's **Actions** tab. A workflow called "Build Windows EXE"
   will run automatically (or click **Run workflow** to trigger it manually).
4. When it finishes (~2-3 minutes), open the completed run and download the
   **`LTspiceToQSpiceTools-windows-exe`** artifact — that's your `.exe`, built
   fresh on an actual Windows machine in GitHub's cloud, no install needed on
   your end.

This is free for public repos, and also free for private repos within
GitHub's generous free CI minutes for individual accounts.

---

## Option B: build it yourself on any Windows PC

1. Install Python 3.10+ from python.org on that Windows PC (check "Add to PATH"
   during install).
2. Copy this whole folder onto that PC.
3. Double-click `build_windows_exe.bat`.
4. When it finishes, your app is at `dist\LTspiceToQSpiceTools.exe`.
5. Copy that one `.exe` file anywhere — it's fully self-contained and can be
   given to any Windows user with no setup required on their end.

---

## Notes / things worth knowing

- **Open-source dependency**: both scripts rely on `spicelib` (MIT-licensed,
  on PyPI). PyInstaller bakes it (and Python itself) directly into the .exe,
  so nothing needs to be installed separately by the end user — this satisfies
  your "no install / no calling out" requirement. There's no license
  obligation beyond spicelib's permissive MIT license, but if you plan to
  distribute this to outside users it's good practice to include spicelib's
  license text alongside the app (I can add that for you if you want).
- **Antivirus/SmartScreen**: freshly built, unsigned PyInstaller .exe files are
  sometimes flagged by Windows SmartScreen or antivirus as "unrecognized" —
  this is a known false-positive pattern for PyInstaller apps in general, not
  a sign anything is wrong. Users may need to click "More info" → "Run anyway"
  the first time, or you can code-sign the exe later if you want to avoid that.
- **First run is slightly slower**: onefile PyInstaller apps unpack to a temp
  folder on first launch each session — a second or two delay, totally normal.
- If you'd rather have two separate .exe files instead of one combined menu
  app, that's an easy tweak (just run the build command twice, once per
  script) — let me know and I'll set that up instead.
