@echo off
rem Raccourci : ouvre l'application de contrôle (sans fenêtre console).
cd /d "%~dp0application"
start "" pythonw app.py
