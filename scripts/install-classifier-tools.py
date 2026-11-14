#!/usr/bin/env python3
"""Install optional Classifier tools using the compute node's shared worker."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loadcoach.classifier_jobs import main

if __name__ == '__main__':
    main()
