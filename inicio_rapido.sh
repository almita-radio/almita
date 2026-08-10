#!/bin/bash
# QUICK START - Run this script to begin
# Assumes KStars/Ekos is already running with INDI server

echo "🚀 QUICK START - Testing INDIpy"
echo "=================================="
echo ""

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "⚠️  Virtual environment not found"
    echo "   Run first: ./setup_rpi.sh"
    exit 1
fi

# Activate virtual environment
echo "📦 Activating virtual environment..."
source venv/bin/activate

# Check INDIpy (module 'indi')
if ! python3 -c "import indi" 2>/dev/null; then
    echo "⚠️  INDIpy is not installed correctly"
    echo "   Reinstalling now..."
    pip uninstall indipy -y -q 2>/dev/null
    pip install git+https://github.com/wlatanowicz/indipy.git -q
fi

echo "✓ Environment ready"
echo ""

# Check INDI server
echo "🔍 Checking INDI server..."
if ! pgrep -x "indiserver" > /dev/null; then
    echo "⚠️  INDI server is not running"
    echo ""
    echo "   Please start KStars/Ekos and connect your mount"
    echo ""
    read -p "Have you started KStars/Ekos? (y/n): " response
    if [ "$response" != "y" ] && [ "$response" != "Y" ]; then
        echo "   Start KStars/Ekos and run this script again"
        exit 1
    fi
else
    echo "✓ INDI server is running (KStars/Ekos)"
fi

echo ""
echo "=================================="
echo "What would you like to run?"
echo "=================================="
echo ""
echo "1. Full tests (Testing_INDIpy.py)"
echo "2. Interactive examples (ejemplos_uso.py)"
echo "3. Exit"
echo ""
read -p "Option [1-3]: " option

case $option in
    1)
        echo ""
        echo "🧪 Running full tests..."
        echo "   Connecting to KStars INDI server (localhost:7624)"
        echo ""
        python Testing_INDIpy.py
        ;;
    2)
        echo ""
        echo "📚 Running interactive examples..."
        echo "   Connecting to KStars INDI server (localhost:7624)"
        echo ""
        python ejemplos_uso.py
        ;;
    3)
        echo "👋 Goodbye!"
        exit 0
        ;;
    *)
        echo "Invalid option"
        exit 1
        ;;
esac

echo ""
echo "=================================="
echo "✓ Execution completed"
echo "=================================="

