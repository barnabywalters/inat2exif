# Load the real OpenCV once, before test modules install their fallback stubs.
# Re-importing OpenCV's native module later in the same process is not supported.
import cv2  # noqa: F401
