#!/usr/bin/env python3
"""Optional native image installer; implementation belongs to Mirror Wall."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mirrorwall.install import main

if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,subprocess.SubprocessError) as problem:
        raise SystemExit("sdcpp installation failed: "+str(problem)) from None
