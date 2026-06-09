import os
import sys

# Run the suite straight from the source tree (no install needed):
#   python -m unittest discover -s tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
