#!/usr/bin/env python3
"""
Diagnostic script to verify INDI clients installation
"""

import sys
import time
from datetime import datetime

def log(message: str, level: str = "INFO"):
    """Print message with timestamp"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    print(f"[{timestamp}] [{level}] {message}")
    sys.stdout.flush()

log("")
log("=" * 80)
log("INDI CLIENTS DIAGNOSTICS FOR PYTHON")
log("=" * 80)
log("")

# System information
log(f"Python: {sys.version}")
log(f"Platform: {sys.platform}")
log("")

# Check indipy
log("Checking INDIpy...")
try:
    import indipy
    version = getattr(indipy, '__version__', 'unknown')
    log(f"✓ INDIpy {version} installed", "INFO")

    # Try to import specific components
    try:
        from indipy.client import IndiClient
        log("  ✓ indipy.client.IndiClient available", "INFO")
    except ImportError as e:
        log(f"  ✗ Error importing IndiClient: {e}", "WARNING")

    # List module attributes
    log("  INDIpy attributes:", "INFO")
    attrs = [attr for attr in dir(indipy) if not attr.startswith('_')]
    for attr in attrs[:10]:  # First 10
        log(f"    - {attr}", "INFO")

except ImportError:
    log("✗ INDIpy NOT installed", "ERROR")
    log("  Install with: pip install indipy", "INFO")

log("")

# Check PyIndi (C-based alternative)
log("Checking PyIndi (pyindi-client)...")
try:
    import PyIndi
    log(f"✓ PyIndi installed", "INFO")
    log("  PyIndi is the official C++-based INDI client", "INFO")
    log("  More mature and stable than indipy", "INFO")
except ImportError:
    log("✗ PyIndi NOT installed", "WARNING")
    log("  Install with: pip install pyindi-client", "INFO")
    log("  RECOMMENDED for production", "INFO")

log("")
log("=" * 80)
log("RECOMMENDATION")
log("=" * 80)
log("")

# Give final recommendation
try:
    import indipy
    try:
        from indipy.client import IndiClient
        log("✓ Your INDIpy installation seems OK", "INFO")
        log("  Run: python Testing_INDIpy.py", "INFO")
    except ImportError:
        log("⚠ INDIpy is installed but has structure issues", "WARNING")
        log("  Possible incompatibility with Python 3.13", "WARNING")
        log("  SOLUTION:", "INFO")
        log("    1. pip uninstall indipy", "INFO")
        log("    2. pip install pyindi-client", "INFO")
        log("    3. Use Testing_PyIndi.py script (if exists)", "INFO")
except ImportError:
    log("To install INDI client:", "INFO")
    log("  pip install pyindi-client  (recommended)", "INFO")
    log("  pip install indipy          (experimental)", "INFO")

log("")
log("=" * 80)

