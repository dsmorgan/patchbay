"""`python -m patchbay`: the same CLI as the `patchbay` command. The poller
runs each cycle as `python -m patchbay poll`."""

import sys

from .cli import main

sys.exit(main())
