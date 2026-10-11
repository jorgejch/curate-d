"""`python -m curate_d.worker`."""

import sys

from curate_d.runtime.threads import pin_library_threads

# Before the application is imported: numpy, scipy and the image decoders size their
# thread pools from the environment when they first load.
pin_library_threads()

from curate_d.worker.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
