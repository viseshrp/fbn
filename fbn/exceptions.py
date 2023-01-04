"""
All exceptions used in the code base are defined here.
"""


class FbnException(Exception):
    """
    Base exception. All other exceptions
    inherit from here.
    """


class NoAuthInfoException(FbnException):
    """
    Raised when the provided Heroku API key is wrong
    """


class InvalidFrequencyException(FbnException):
    """
    Raised when the provided Heroku API key is wrong
    """
