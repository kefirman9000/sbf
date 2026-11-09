"""Double-click to start SBF with no command window (Windows opens .pyw files with pythonw)."""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__":
    os.chdir(HERE)
sys.path.insert(0, HERE)
if __name__ == "__main__":  # (studies running on other cores start this file too, and must not open the app)
    runpy.run_path(os.path.join(HERE, "main.py"), run_name="__main__")
