@echo off
rem Job-Scanner Pipeline (Windows)
cd /d %~dp0
call .venv\Scripts\activate.bat
python scanner.py
python rohtext_holen.py
python vergaben_check.py
python extraktor.py
python bewertung.py
python report.py
pause
