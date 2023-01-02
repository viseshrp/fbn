"""
All exceptions used in the code base are defined here.
"""


class FbNotifyException(Exception):
    """
    Base exception. All other exceptions
    inherit from here.
    """


class NoAuthInfoException(FbNotifyException):
    """
    Raised when the provided Heroku API key is wrong
    """
