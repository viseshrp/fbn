"""Typed operational failures and their stable process exit codes."""

from __future__ import annotations


class FbnError(Exception):
    """Base class for expected fbn failures."""

    exit_code = 1


class ConfigurationError(FbnError):
    """The supplied configuration is invalid."""

    exit_code = 2


class BrowserUnavailableError(FbnError):
    """The configured browser cannot be installed, found, or started."""

    exit_code = 3


class AuthenticationRequiredError(FbnError):
    """The dedicated browser profile is not authenticated."""

    exit_code = 4


class AccountActionRequiredError(FbnError):
    """Facebook requires attended account action such as a checkpoint."""

    exit_code = 5


class AccessDeniedError(FbnError):
    """The account cannot access the requested group."""

    exit_code = 6


class TransientNavigationError(FbnError):
    """A navigation or rate-limit failure may succeed after a long backoff."""

    exit_code = 7


class LayoutChangedError(FbnError):
    """The page loaded but did not match a supported, safe page state."""

    exit_code = 8


class DeliveryError(FbnError):
    """A notification could not be accepted or delivered."""

    exit_code = 9


class ProfileInUseError(FbnError):
    """Another process owns the dedicated browser profile."""

    exit_code = 10


class MonitorInUseError(FbnError):
    """Another process is running a check against the same state database."""

    exit_code = 12


class BootstrapInterruptedError(FbnError):
    """A termination signal interrupted authentication bootstrap."""

    exit_code = 11


# Source compatibility for code that imported the 0.1 exception names.
FbnException = FbnError
InvalidFrequencyException = ConfigurationError
NoAuthInfoException = AuthenticationRequiredError
