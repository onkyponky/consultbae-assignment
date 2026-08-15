"""Put `src/` on the import path for the test run.

`ingest.py` imports its siblings directly (`from models import ...`) because
it is run as `python src/ingest.py`. Tests import the same way so there is
one import style in the repo rather than two.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
