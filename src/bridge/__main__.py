"""So ``python -m bridge`` is the command in the systemd unit."""

from .main import main

raise SystemExit(main())
