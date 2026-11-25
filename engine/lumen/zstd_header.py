"""Bounded access to a single Zstandard frame using the bundled decoder."""
import ctypes
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _decoder():
    parents = Path(__file__).resolve().parents
    if sys.platform == 'darwin':
        if parents[2].name == 'Resources' and parents[3].name == 'Contents':
            candidates = [parents[3] / 'Frameworks/liblumen-zstd.dylib']
        else:
            candidates = ['/opt/homebrew/opt/zstd/lib/libzstd.1.dylib',
                          '/usr/local/opt/zstd/lib/libzstd.1.dylib']
    elif sys.platform.startswith('linux'):
        candidates = ['libzstd.so.1']
    else:
        return None
    for path in candidates:
        try:
            library = ctypes.CDLL(str(path))
            library.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            library.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
            library.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
            library.ZSTD_decompress.restype = ctypes.c_size_t
            return library
        except (OSError, AttributeError):
            continue
    return None


def first_frame(prefix, output_limit):
    """Return only the first complete frame, with a fixed-size output buffer."""
    library = _decoder()
    if library is None or not prefix.startswith(b'\x28\xb5\x2f\xfd'):
        return None
    length = library.ZSTD_findFrameCompressedSize(prefix, len(prefix))
    # The size_t error codes returned by zstd are larger than either buffer.
    if not 0 < length <= len(prefix):
        return None
    output = ctypes.create_string_buffer(output_limit)
    written = library.ZSTD_decompress(output, output_limit, prefix, length)
    return output.raw[:written] if 0 < written <= output_limit else None
