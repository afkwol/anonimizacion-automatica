@echo off
setlocal

if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo [ERROR] No se encontró el entorno virtual en %~dp0.venv
    echo Ejecuta primero setup_anonimizador.bat para crearlo e instalar dependencias.
    pause
    exit /b 1
)

call "%~dp0.venv\Scripts\activate.bat"
python "%~dp0anonimizador v.5.py"

endlocal
