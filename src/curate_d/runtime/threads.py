"""Pin native library thread pools to one thread.

Numerical and inference libraries default to one thread per core. A worker already runs
several items at once through its queue concurrency, so each item's library spinning up
its own pool puts far more threads than cores on the CPU and runs slower than serial.
Parallelism is expressed at the queue level instead, and each library gets one thread.

The variables are read when a library first loads its thread pool, so this must run before
anything imports numpy, scipy, Pillow's SIMD paths or an inference runtime: the worker's
`__main__` calls it before importing the application.
"""

import os
from collections.abc import MutableMapping
from typing import Final

# OpenMP (numpy/scipy builds, ONNX Runtime), Tesseract's OpenMP pool, OpenBLAS, MKL,
# Apple Accelerate, numexpr.
THREAD_VARIABLES: Final = (
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def pin_library_threads(environment: MutableMapping[str, str] = os.environ) -> None:
    """Set every library thread-count variable to one.

    Overrides a value already set: a deployment that raised one of these would be asking
    for exactly the oversubscription this exists to prevent.
    """
    for variable in THREAD_VARIABLES:
        environment[variable] = "1"
