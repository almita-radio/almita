# scripts/

Auxiliary and historical tools. The production entry points (`almita_*.py`) and the modules they import stay in the repository
root; run everything from the **repository root** (for example `python scripts/validation/science_acceptance_measure.py --help`).

| Directory | Contents |
|---|---|
| `analysis/` | offline spectrum tools: `analyze_spectra.py`, `plot_sky_map.py` |
| `calibration/` | `calibration_inventory.py` |
| `hardware/` | `check_indi.py` |
| `mount/` | `mount_slew_training.py` |
| `validation/` | `science_acceptance_measure.py` (SCIENCE acceptance measurements) |
| `legacy/` | first-era installation helpers (`setup_rpi.sh`, `inicio_rapido.sh`, `debug_install.sh`, Windows notes, Visual Studio project); they reference scripts that no longer exist |

These scripts only import from the standard library and third-party packages, or (`science_acceptance_measure.py`) add the repo root
to `sys.path` themselves. Scripts that import modules from the repo root were deliberately not moved.
