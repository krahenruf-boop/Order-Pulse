"""macOS input-idle duration only; never captures input contents."""
import ctypes
import math
import sys


def input_idle_seconds():
    if sys.platform != 'darwin':
        return None
    try:
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        read = cg.CGEventSourceSecondsSinceLastEventType
        read.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        read.restype = ctypes.c_double
        seconds = read(0, 0xffffffff)  # Combined session, any input event.
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (OSError, AttributeError):
        return None


if __name__ == '__main__':
    import json
    print(json.dumps(input_idle_seconds()))
