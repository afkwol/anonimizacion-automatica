@echo off
setlocal

if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo [ERROR] No se encontró el entorno virtual en %~dp0.venv
    echo Ejecuta primero setup_anonimizador.bat para crearlo e instalar dependencias.
    pause
    exit /b 1
)

call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python -m app --gui

endlocal
