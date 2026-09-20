"""Typed errors for ve_direct."""


class VeDirectError(Exception):
    """Base error."""


class ChecksumError(VeDirectError):
    """Frame failed checksum validation."""


class FrameError(VeDirectError):
    """Malformed frame."""


class TimeoutError(VeDirectError):
    """No valid frame within timeout."""


class RegisterError(VeDirectError):
    """Unknown register or bad access (e.g. write to read-only)."""


class AmbiguousRegisterError(RegisterError, KeyError):
    """Register ID is aliased across multiple device flavors; flavor or exact name required."""


class TransportError(VeDirectError):
    """Serial/transport failure (port missing, pyserial not installed, ...)."""
