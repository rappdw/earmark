"""Receive an earmark email: recognise it, check it, de-duplicate it and file its note.

This launcher is written in old syntax on purpose: an interpreter older than Python 3.9 must
reach the guard below and print a clear message, not die on a SyntaxError. The work is in
``earmark_receive`` in this folder, written to Python 3.9 and standard library only.

Run it as ``python3 -I receive.py preflight|classify|file|rewrite|move|annotate|record ...``.
Exit codes: 0 ok, 1 refused or failed, 2 a prerequisite is missing, 3 the workspace is not
ready, 64 usage error.
"""

import sys

if sys.version_info < (3, 9):  # noqa: UP036 -- the guard is the point; see the docstring
    sys.stderr.write(
        "receive.py needs Python 3.9 or newer; this is "
        + ".".join(str(n) for n in sys.version_info[:2])
        + "\n"
    )
    sys.exit(2)
# -I ignores PYTHONDONTWRITEBYTECODE, and a .pyc must not land in the plugin cache.
sys.dont_write_bytecode = True
from pathlib import Path  # noqa: E402 -- after the guard, which old interpreters must reach

# -I leaves the script's folder off sys.path. The folder holds only the skill's scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earmark_receive  # noqa: E402 -- after the guard and the path

sys.exit(earmark_receive.main(sys.argv[1:]))
