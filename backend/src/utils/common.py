"""
common.py: A collection of miscellaneous general-purpose utility functions and classes.

@author: sruffner
@created: 04mar2021
"""
from datetime import date
from enum import Enum
from functools import partial
from json import JSONDecoder
from typing import Optional, Any, Union


def check_date(date_obj: Union[str, date]) -> bool:
    """
    Validate a date that appears in the laboratory database. If the date is a string, it must exactly match the ISO
    format 'YYYY-DD-MM', with a 4-digit year, 2-digit day, and 2-digit month. The year must be 1900 or greater, and the
    date cannot be in the future.

    Args:
        date_obj: The date to test -- either a date object or in string form

    Returns:
        (bool) True if the date object satisfies the requirements described.
    """
    if not isinstance(date_obj, (str, date)):
        return False
    ok = False
    try:
        if isinstance(date_obj, str):
            date_obj = date.fromisoformat(date_obj)
        ok = ((date_obj.year > 1899) and (date_obj < date.today()))
    except(TypeError, ValueError):
        pass
    return ok


def json_parse(file_obj, decoder: JSONDecoder = JSONDecoder(), buffer_size: int = 2048,
               delimiters: Optional[str] = None) -> Optional[Any]:
    """
    Generator function that parses zero or more JSON entities from a text IO stream.

    Args:
        file_obj: The text IO stream.
        decoder (JSONDecoder): Optional supplied JSON decoder used to parse JSON objects from the text stream. If none
            is supplied, one will be created for use by the generator
        buffer_size (int_: Desired buffer size for reading from the text stream; defaults to 2048
        delimiters (Optional[str]): String containing the set of characters separating consecutive JSON objects in the
            source text stream. If omitted, it is assumed that whitespace separates the objects

    Returns:
        Any: Python representation of the JSON entity (array, object) returned, or None if no objects remain.
    """
    remainder = ''
    for chunk in iter(partial(file_obj.read, buffer_size), ''):
        remainder += chunk
        while remainder:
            try:
                stripped = remainder.strip(delimiters)
                result, index = decoder.raw_decode(stripped)
                yield result
                remainder = stripped[index:]
            except ValueError:
                # Not enough data to decode, read more
                break


class DocEnum(Enum):
    """
    Convenience subclass to simplify documenting the individual members of an Enum.
    """
    def __new__(cls, value, doc=None):
        self = object.__new__(cls)  # calling super().__new__(value) here would fail
        self._value_ = value
        if doc is not None:
            self.__doc__ = doc
        return self
