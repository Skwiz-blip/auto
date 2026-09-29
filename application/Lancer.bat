@echo off
rem Ouvre l'application de contrôle des dossiers marchands (sans fenêtre console).
cd /d "%~dp0"
start "" pythonw app.py
