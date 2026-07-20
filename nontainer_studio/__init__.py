"""nontainer-studio: a local AI workbench over nontainer — chat,
versioned workspaces, live app preview, publish."""

import os

# Arrow's default mimalloc pool is not fork-safe, and sandbox workers
# fork from THIS process — a forked worker segfaults in mimalloc's
# thread-init on its first arrow allocation (pandas 3 strings and
# parquet are arrow-backed). pyarrow reads this at import, and
# `import pandas` imports pyarrow, so set it before anything can.
# nontainer's dataframes() preset also sets it; this is the earlier
# belt for the studio process itself.
os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")

__version__ = "0.0.1"
