"""nontainer-studio: a local AI workbench over nontainer — chat,
versioned workspaces, live app preview, publish."""

import os

# Arrow's default mimalloc pool is not fork-safe: a forked child
# segfaults in mimalloc's thread-init on its first arrow allocation
# (pandas 3 strings and parquet are arrow-backed). pyarrow reads this
# at import, and `import pandas` imports pyarrow, so set it before
# anything can.
#
# Sandbox workers no longer fork from THIS process — sandtrap 0.3 forks
# them from a forkserver broker — but the pin matters more than it did,
# not less. `_python_config` sets preload_grants, which imports the
# granted stack (pyarrow included) INTO that broker, and the broker
# inherits the environment of the process that starts it. This one.
# nontainer's dataframes() preset pins it too; this is the earlier belt.
os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")

__version__ = "0.0.1"
