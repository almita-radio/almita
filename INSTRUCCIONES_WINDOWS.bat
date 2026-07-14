@echo off
REM NOTA: Este proyecto está diseñado para Linux (Raspberry Pi)
REM Para usar en Windows, necesitas WSL (Windows Subsystem for Linux)

echo ===============================================================================
echo Script de Pruebas INDIpy - INSTRUCCIONES PARA WINDOWS
echo ===============================================================================
echo.
echo Este proyecto está diseñado para ejecutarse en:
echo   • Raspberry Pi 5
echo   • Linux (Ubuntu, Debian, etc.)
echo   • macOS (con Homebrew)
echo.
echo ===============================================================================
echo OPCIÓN 1: Usar WSL (Windows Subsystem for Linux) - RECOMENDADO
echo ===============================================================================
echo.
echo 1. Instalar WSL2:
echo    wsl --install -d Ubuntu
echo.
echo 2. Abrir terminal WSL y navegar a este directorio:
echo    cd /mnt/c/Users/YourUser/path/to/Testing INDIpy
echo.
echo 3. Ejecutar configuración:
echo    chmod +x setup_rpi.sh
echo    ./setup_rpi.sh
echo.
echo 4. Ejecutar pruebas:
echo    source venv/bin/activate
echo    python Testing_INDIpy.py
echo.
echo ===============================================================================
echo OPCIÓN 2: Usar conexión remota a Raspberry Pi
echo ===============================================================================
echo.
echo 1. Copiar archivos a Raspberry Pi:
echo    scp -r "Testing INDIpy" pi@192.168.1.xxx:/home/pi/
echo.
echo 2. Conectar por SSH:
echo    ssh pi@192.168.1.xxx
echo.
echo 3. En Raspberry Pi:
echo    cd Testing\ INDIpy
echo    ./setup_rpi.sh
echo    ./inicio_rapido.sh
echo.
echo ===============================================================================
echo OPCIÓN 3: Instalar servidor INDI en Windows (AVANZADO)
echo ===============================================================================
echo.
echo Esta opción es compleja y no recomendada. Mejor usar WSL.
echo.
echo ===============================================================================
echo ¿NECESITAS AYUDA?
echo ===============================================================================
echo.
echo Lee la documentación completa:
echo   • README.md - Instalación y uso
echo   • RESUMEN_EJECUTIVO.md - Entender el proyecto
echo   • INDICE.md - Lista de todos los archivos
echo.
echo ===============================================================================
pause
