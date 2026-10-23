@echo off
REM Build the GUI program (dist folder) from gui.py + analyze.py
REM Usage: double-click or run "build_exe.bat" in this folder.
REM Requires: Python 3 on PATH. Takes several minutes.

cd /d "%~dp0"

echo [1/4] Creating build venv...
python -m venv .build_venv || goto :error

echo [2/4] Installing dependencies (this takes a while)...
.build_venv\Scripts\python.exe -m pip install --quiet --upgrade pip || goto :error
.build_venv\Scripts\python.exe -m pip install --quiet -r requirements.txt pyinstaller || goto :error

echo [3/4] Running tests...
.build_venv\Scripts\python.exe test_analyze.py || goto :error

echo [4/4] Building GUI exe...
.build_venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --version-file version_info.txt --name "SsakSsakChordAnalyzer" ^
  --collect-all librosa --collect-all soundfile --collect-all audioread ^
  --hidden-import matplotlib.backends.backend_agg gui.py || goto :error

echo.
echo Build OK: dist\SsakSsakChordAnalyzer.exe
echo (You can rename it and delete .build_venv / build folders to save space.)
pause
exit /b 0

:error
echo.
echo BUILD FAILED. See messages above.
pause
exit /b 1
