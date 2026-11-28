"""Bounded Zstandard metadata decoding using the bundled decoder."""
import ctypes
import sys
from functools import lru_cache
from pathlib import Path


_MAGIC = b'\x28\xb5\x2f\xfd'
_READ_CHUNK = 16 * 1024
_WINDOW_LOG_MAX_PARAMETER = 100  # ZSTD_d_windowLogMax in zstd.h.
_WINDOW_LOG_LIMIT = 23  # 8 MiB.


class _Buffer(ctypes.Structure):
    # ZSTD_inBuffer and ZSTD_outBuffer have the same layout.
    _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


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
    if library is None or not prefix.startswith(_MAGIC):
        return None
    length = library.ZSTD_findFrameCompressedSize(prefix, len(prefix))
    # The size_t error codes returned by zstd are larger than either buffer.
    if not 0 < length <= len(prefix):
        return None
    output = ctypes.create_string_buffer(output_limit)
    written = library.ZSTD_decompress(output, output_limit, prefix, length)
    return output.raw[:written] if 0 < written <= output_limit else None


@lru_cache(maxsize=1)
def _stream_decoder():
    library = _decoder()
    if library is None:
        return None
    address = ctypes.c_void_p
    size = ctypes.c_size_t
    buffer = ctypes.POINTER(_Buffer)
    signatures = (
        ('ZSTD_createDCtx', (), address),
        ('ZSTD_freeDCtx', (address,), size),
        ('ZSTD_DCtx_setParameter', (address, ctypes.c_int, ctypes.c_int), size),
        ('ZSTD_decompressStream', (address, buffer, buffer), size),
        ('ZSTD_isError', (size,), ctypes.c_uint),
    )
    try:
        for name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
    except AttributeError:
        return None
    return library


def first_line(stream, input_limit, output_limit):
    """Return the first newline-terminated line in the first frame and bytes read."""
    library = _stream_decoder()
    if library is None or input_limit < len(_MAGIC) or output_limit <= 0:
        return None, 0
    context = library.ZSTD_createDCtx()
    if not context:
        return None, 0
    consumed = 0
    try:
        setting = library.ZSTD_DCtx_setParameter(context, _WINDOW_LOG_MAX_PARAMETER, _WINDOW_LOG_LIMIT)
        if library.ZSTD_isError(setting):
            return None, 0
        data = ctypes.create_string_buffer(output_limit)
        output = _Buffer(ctypes.addressof(data), len(data), 0)
        while consumed < input_limit and output.pos < output.size:
            chunk = stream.read(min(_READ_CHUNK, input_limit - consumed))
            if not chunk or (consumed == 0 and not chunk.startswith(_MAGIC)):
                return None, consumed + len(chunk)
            consumed += len(chunk)
            source = ctypes.cast(ctypes.c_char_p(chunk), ctypes.c_void_p)
            incoming = _Buffer(source, len(chunk), 0)
            while incoming.pos < incoming.size and output.pos < output.size:
                start, position = output.pos, incoming.pos
                status = library.ZSTD_decompressStream(context, ctypes.byref(output), ctypes.byref(incoming))
                if library.ZSTD_isError(status):
                    return None, consumed
                newline = data[start:output.pos].find(b'\n')
                if newline >= 0:
                    return data[:start + newline + 1], consumed
                if status == 0 or (output.pos == start and incoming.pos == position):
                    return None, consumed
        return None, consumed
    finally:
        library.ZSTD_freeDCtx(context)
