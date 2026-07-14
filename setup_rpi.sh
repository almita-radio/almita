#!/bin/bash
# setup_rpi.sh - Quick setup script for Raspberry Pi 5
# Configures Python environment for INDIpy
# REQUIREMENTS: KStars/Ekos with INDI server already running

echo "================================================================================"
echo "INDIpy SETUP ON RASPBERRY PI 5"
echo "================================================================================"
echo ""
echo "This script assumes you already have:"
echo "  ✓ KStars/Ekos installed"
echo "  ✓ INDI server running"
echo "  ✓ Python 3.13.5 (or higher)"
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "⚠️  Do not run this script as root (without sudo)"
    exit 1
fi

# Check Python
echo "📦 Checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed"
    echo "   Install with: sudo apt-get install python3 python3-pip python3-venv"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
echo "✓ Python $PYTHON_VERSION detected"
echo ""

# Create virtual environment
echo "📦 Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✓ Virtual environment created at ./venv"
else
    echo "✓ Virtual environment already exists"
fi
echo ""

echo "📦 Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip -q

# Install main requirements
pip install -r requirements.txt -q

# Force INDIpy from Git (tested with Python 3.13)
pip uninstall indipy -y -q 2>/dev/null
pip install git+https://github.com/wlatanowicz/indipy.git -q

# Verify installation
if python3 -c "import indi" 2>/dev/null; then
    echo "✓ INDIpy installed correctly from Git"
    echo "   (module: indi, version: dev)"
else
    echo "❌ Could not install INDIpy"
    echo ""
    echo "   Run ./debug_install.sh for more information"
    exit 1
fi
echo ""

# Make scripts executable
echo "📦 Configuring script permissions..."
chmod +x Testing_INDIpy.py
chmod +x ejemplos_uso.py
chmod +x inicio_rapido.sh
chmod +x check_indi.py
chmod +x debug_install.sh
echo "✓ Scripts configured"
echo ""

echo "================================================================================"
echo "✓ SETUP COMPLETED"
echo "================================================================================"
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Make sure KStars/Ekos is running with your mount connected"
echo ""
echo "2. Run test script:"
echo "   python Testing_INDIpy.py"
echo ""
echo "3. Or use the quick start script:"
echo "   ./inicio_rapido.sh"
echo ""
echo "NOTES:"
echo "• The script will connect to KStars INDI server (localhost:7624)"
echo "• If your INDI server is on another port, use: --port NUMBER"
echo "• If your mount has a specific name, use: --device \"NAME\""
echo ""
echo "DOCUMENTATION:"
echo "• README.md - Complete usage guide"
echo "• RESUMEN_EJECUTIVO.md - Explanation of point 4 (SYNC)"
echo ""
echo "================================================================================"

