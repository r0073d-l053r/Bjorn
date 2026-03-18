"""init_shared.py - Global singleton for shared state; import shared_data from here."""

from shared import SharedData

# Module-level initialization is thread-safe in CPython: the import lock
# guarantees that this module body executes at most once, even when multiple
# threads import it concurrently (see importlib._bootstrap._ModuleLock).
shared_data = SharedData()
