"""Check an earmark email against the mail contract (``contract: 1``).

This launcher is written in old syntax on purpose: an interpreter older than Python 3.9 must
reach the guard below and print a clear message, not die on a SyntaxError. The checker itself
is ``earmark_mail_contract`` in this folder, written to Python 3.9 and standard library only.

Run it as ``python3 -I check_mail.py message|files|rules ...``. Exit codes: 0 every rule that
ran passed, 1 a rule failed, 2 a prerequisite is missing, 64 usage error.
"""

import sys

if sys.version_info < (3, 9):  # noqa: UP036 -- the guard is the point; see the docstring
    sys.stderr.write(
        "check_mail.py needs Python 3.9 or newer; this is "
        + ".".join(str(n) for n in sys.version_info[:2])
        + "\n"
    )
    sys.exit(2)
# -I ignores PYTHONDONTWRITEBYTECODE, and a .pyc must not land in the plugin cache.
sys.dont_write_bytecode = True
from pathlib import Path  # noqa: E402 -- after the guard, which old interpreters must reach

# -I leaves the script's folder off sys.path. The folder holds only the two checker files.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earmark_mail_contract  # noqa: E402 -- after the guard and the path

sys.exit(earmark_mail_contract.main(sys.argv[1:]))
