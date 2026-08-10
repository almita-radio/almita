#!/bin/bash
# Script de depuración para problemas de instalación de indipy

echo "================================================================================"
echo "DEPURACIÓN DE INSTALACIÓN DE INDIpy"
echo "================================================================================"
echo ""

# Verificar que estamos en el venv
if [ -z "$VIRTUAL_ENV" ]; then
    echo "❌ Entorno virtual NO activado"
    echo ""
    echo "   Actívalo con:"
    echo "   source venv/bin/activate"
    echo ""
    exit 1
else
    echo "✓ Entorno virtual activado: $VIRTUAL_ENV"
fi
echo ""

# Verificar Python
echo "📦 Python en uso:"
which python3
python3 --version
echo ""

# Verificar pip
echo "📦 Pip en uso:"
which pip
pip --version
echo ""

# Listar paquetes instalados
echo "📦 Paquetes instalados relacionados con INDI:"
pip list | grep -i indi
echo ""

# Verificar ubicación de instalación
echo "📦 Ubicación de instalación:"
python3 -c "import sys; print('Python paths:'); [print('  -', p) for p in sys.path]" 2>&1
echo ""

# Intentar importar indi (versión Git)
echo "📦 Intentando importar 'indi' (versión Git)..."
python3 -c "import indi; print('✓ indi importado correctamente'); print('Ubicación:', indi.__file__)" 2>&1
IMPORT_RESULT=$?

echo ""
echo "================================================================================"

if [ $IMPORT_RESULT -eq 0 ]; then
    echo "✓ INDIpy funciona correctamente"
    echo ""
    echo "Puedes ejecutar:"
    echo "  python Testing_INDIpy.py"
else
    echo "✗ INDIpy NO se puede importar"
    echo ""
    echo "SOLUCIÓN RECOMENDADA:"
    echo ""
    echo "Reinstalar desde repositorio Git:"
    echo "  pip uninstall indipy -y"
    echo "  pip install git+https://github.com/wlatanowicz/indipy.git"
    echo ""
    echo "O recrear entorno desde cero:"
    echo "  deactivate"
    echo "  rm -rf venv"
    echo "  ./setup_rpi.sh"
fi

echo "================================================================================"

