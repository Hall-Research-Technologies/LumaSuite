"""The LumaSuite automated test package.

Importing this package installs the hardware fence and points the application
at a scratch data directory. Both happen here, at package import, so that any
runner -- `run_tests.py`, `python -m unittest`, discovery, an IDE, a mutation
harness -- gets them without having to know they exist.
"""

from . import _fence

# Before any test imports the application: a test must never be able to read or
# write the operator's real device cache, and the application resolves this once
# at import. The scratch folder is pre-seeded with empty state, because an unseeded
# one would have the application migrate the developer's real runtime files into it.
_fence.isolate_data_dir()

_fence.install()
