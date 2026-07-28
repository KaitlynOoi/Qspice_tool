@echo off
REM Run this on a Windows PC that has Python installed.
REM It installs the needed packages and builds a single-file .exe
REM that end users can run with no Python/install required.

echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Building LTspiceToQSpiceTools.exe ...
pyinstaller --onefile --noconsole --name "LTspiceToQSpiceTools" --collect-all spicelib combined_app.py

echo.
echo Done. Find your .exe in the "dist" folder:
echo   dist\LTspiceToQSpiceTools.exe
pause
