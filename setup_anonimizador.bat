@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No se encontro Python en PATH.
    echo Instala Python 3.10 o superior y volve a ejecutar este script.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creando entorno virtual...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)

echo [INFO] Actualizando pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] No se pudo actualizar pip.
    pause
    exit /b 1
)

echo [INFO] Instalando dependencias del proyecto...
python -m pip install -e .[dev]
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion del proyecto.
    pause
    exit /b 1
)

echo.
echo [OK] Entorno listo.
echo - Para abrir la interfaz grafica: run_anonimizador.bat
echo - Para correr tests: python -m pytest -q
echo.
pause
exit /b 0
