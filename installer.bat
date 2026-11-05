@echo off
rem Installe les bibliotheques necessaires (une seule fois).
cd /d "%~dp0"
python -m pip install -r requirements.txt
pause
