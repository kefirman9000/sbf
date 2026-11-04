"""Double-click to start SBF with no command window (Windows opens .pyw files with pythonw)."""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
runpy.run_path(os.path.join(HERE, "main.py"), run_name="__main__")
