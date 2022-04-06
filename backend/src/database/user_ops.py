"""
user_ops.py: Operations on the table of users registered with the Lisberger lab portal.

This module handles any operation that adds or removes a registered user from the portal database (User table), and
it also handles login/authentication of a user.

@author: sruffner
@created: 11oct2021
"""
import re
import sys
from datetime import datetime
from getpass import getpass
from typing import Optional, Union, Dict, List

from werkzeug.security import check_password_hash, generate_password_hash

from config.app_logging import get_application_logger
from database.table_info import DBTable, attribute_info
from database.table_ops import update_table_row, fetch_rows, insert_into_table, row_exists, \
    delete_from_table, fetch_one_row, fetch_restrict_proj


PASSWORD_HASH_METHOD = 'pbkdf2:sha256:10000'
""" Method used to generate hashed passwords that are stored in DB """
USER_PROFILE_KEYS = {'full_name', 'contact_email', 'title', 'organization'}
""" Set of attributes that are part of an authorized user's editable profile. """
ACCESS_LEVELS = ['admin', 'commit', 'download']
""" List of all defined access levels. """
ADMIN_ACCESS: str = ACCESS_LEVELS[0]
""" Access level with full administrative privleges on portal. """
DOWNLOAD_ACCESS: str = ACCESS_LEVELS[2]
""" The most restrictive access level only allows user to download data sets from the portal. """
COMMIT_ACCESS: List[str] = ACCESS_LEVELS[:-1]
""" List of access levels that allow user to contribute experiment sessions to the lab database. """


def authenticate_portal_user(username: str, password: str, admin_only: bool = False) -> Optional[str]:
    """
    Authenticate the user account on the Lisberger lab portal with the specified name and password.

    Args:
        username: The username for the account.
        password: The (plaintext) password for the account.
        admin_only: If True, require that the user account have 'admin'-level privileges. Default is False.
    Returns:
        None if account was authenticated; else a brief error description (invalid username, etc.)
    """
    # protect against bad arguments
    if not validate_username(username):
        return f"Invalid username: {username}"
    elif (emsg := validate_password(password)) is not None:
        return emsg

    pk = dict(username=username)
    error_msg = None
    get_application_logger().debug(f"Trying to authenticate {username}")
    res = fetch_restrict_proj([DBTable.USER], [pk], ['password', 'access'])
    if (res is None) or (len(res) != 1):
        error_msg = 'Unrecognized username or database error'
    elif not check_password_hash(res[0]['password'], password):
        error_msg = "Incorrect password"
    elif admin_only and (res[0]['access'] != 'admin'):
        error_msg = "Admin-level access required"

    # when a user is authenticated, update their last login timestamp, but don't fail if this update fails, as
    # this is not crucial.
    if error_msg is None:
        last_login = datetime.now().isoformat(sep=' ', timespec='seconds')  # 'YYYY-MM-DD HH:MM:SS'
        entry = dict(username=username, last_login=last_login)
        update_table_row(DBTable.USER, entry)
    else:
        get_application_logger().debug(f"...authentication failed: {error_msg}")
    return error_msg


def get_portal_user_record(username: str) -> Union[str, Dict[str, str]]:
    """
    Retrieve the specified user account records from the database of users authorized for restricted access to the
    Lisberger lab data portal.

    Args:
        username: Username of the account.
    Returns:
        If successful, returns the user account record. For security reasons, the user's encrypted password is
            removed from the record. Otherwise, returns a brief error description.
    """
    res = fetch_one_row(DBTable.USER, dict(username=username))
    if res is None:
        return f"User account record not found for '{username}', or database error"
    res.pop('password', None)
    return res


def get_all_portal_user_records() -> Union[str, List[Dict[str, str]]]:
    """
    Retrieve all user account records from the database of users authorized for restricted access to the Lisberger
    lab data portal.

    Returns:
        If successful, returns the user account records. For security reasons, the user's encrypted password is
            removed from each record. Returns an empty list if there no registered users OR if an error occurs.
    """
    user_records = fetch_rows(DBTable.USER)
    if user_records is None:
        return []
    for rec in user_records:
        rec.pop('password', None)
    return user_records


def register_new_portal_user(username: str, password: str, access: str, full_name: str,
                             contact_email: str) -> Optional[str]:
    """
    Create a new user account authorized for restricted access to the Lisberger lab data portal.

    Args:
        username: The username (3-20 lowercase letters or digits, starting with a letter).
        password: Plain-text password (8-32 characters, with at least one digit and one uppercase character). For
            security, the password will be stored in the database in encrypted form.
        access: Access level assigned to user. Must be one of 'admin' > 'curate' > 'contribute' > 'readonly'.
        full_name: The user's full name. Must be 5-50 characters long, but otherwise unchecked for format.
        contact_email: The user's email address. Up to 80 characters long and checked for valid format.
    Returns:
        None if successful, else a brief error description.
    """
    error_msg = _check_user(username, password, access, full_name, contact_email)
    if error_msg is None:
        now = datetime.now().isoformat(sep=' ', timespec='seconds')
        row = dict(username=username, password=generate_password_hash(password, method=PASSWORD_HASH_METHOD),
                   access=access, full_name=full_name, contact_email=contact_email, registered=now, pwd_changed=now)
        error_msg = insert_into_table(DBTable.USER, row)
    if error_msg is None:
        get_application_logger().debug(f"Registered new user {username} with access level {access}.")
    else:
        error_msg = f"Failed to register new user {username}: {error_msg}"
        get_application_logger().debug(error_msg)
    return error_msg


def _check_user(username: str, password: str, access: str, full_name: str, email: str) -> Optional[str]:
    """
    Helper method for register_new_portal_user() validates required information for a new user account.

    Returns:
        None if user information is valid; else a brief error description
    """
    error_msg = validate_password(password)
    if error_msg is not None:
        return error_msg
    if access not in ACCESS_LEVELS:
        return f"Invalid access level: {access}"
    if not (5 <= len(full_name) <= 50):
        return "Full name must have 5-50 characters"
    if (len(email) > 80) or \
            (re.fullmatch(r'^[A-Za-z0-9._+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6}$', email) is None):
        return "Email address is too long or otherwise invalid"
    if not validate_username(username):
        return "Invalid username"
    if row_exists(DBTable.USER, dict(username=username)):
        return "Username is already taken"
    return None


def validate_username(username: str) -> bool:
    """
    Validate candidate username for a registered portal user.

    Returns:
        True only is candidate username is 3-20 characters long, starts with a lowercase letter, and contans only
            lowercase letters and digis.
    """
    return not (isinstance(username, str) and (re.fullmatch(r'^[a-z][a-z0-9]{2,19}$', username) is None))


def validate_password(password: str) -> Optional[str]:
    """
    Enforce restrictions on the password for a user registered on the Lisberger lab portal. The password must be
    8-32 characters long and contain at least one digit and at least one capital letter.

    Args:
        password: The (plain-text) password ot validate.
    Returns:
        None if password if valid, else a brief error description
    """
    if not isinstance(password, str):
        return "Invalid password"
    elif not (8 <= len(password) <= 32):
        return "Password must have 8-32 characters"
    elif (re.search(r"[\d]+", password) is None) or (re.search(r"[A-Z]+", password) is None):
        return 'Password must contain at least 1 digit and at least 1 uppercase character'
    return None


def validate_fullname(fullname: str) -> Optional[str]:
    """
    Enforce restrictions on the full name of a user registered on the Lisberger lab portal. The user's full name must
    be 5-50 characters long; have no more than 3 name parts, and contain only the characters A-Z, a-z, plus " ' " or
    " - ".

    Args:
        fullname: The candidate name string.
    Returns:
        None if valid, else a brief error description.
    """
    attr_info = attribute_info(DBTable.USER, 'full_name')
    if not isinstance(fullname, str):
        return "Not a string"
    elif not (5 <= len(fullname) <= 50):
        return "Full name must be at least 5 and no more than 50 characters long"
    elif re.fullmatch(attr_info.regex, fullname) is None:
        return attr_info.regex_hint


def validate_email_address(email: str) -> Optional[str]:
    """
    Enforce restrictions on the email address of a user registered on the Lisberger lab portal. The address must be
    be 7-80 characters long and satisfy a regular expression for typical email addresses. Of course, this does not
    check whether the email address actually exists.

    Args:
        email: The candidate email address string.
    Returns:
        None if valid, else a brief error description.
    """
    attr_info = attribute_info(DBTable.USER, 'contact_email')
    if not isinstance(email, str):
        return "Not a string"
    elif attr_info.textrange and not (attr_info.textrange[0] <= len(email) <= attr_info.textrange[1]):
        return "Email address must be at least 7 and no more than 80 characters long"
    elif re.fullmatch(attr_info.regex, email) is None:
        return attr_info.regex_hint


def remove_portal_user(username: str) -> Optional[str]:
    """
    Permanently remove the specified user account from the Lisberger lab data portal. Note that a user record cannot
    be removed if other tables are dependent on that record through a foreign key relationship (eg, any user that
    has committed experiment sessions cannot be removed).

    Args:
        username: Username for user account to be removed.
    Returns:
        None if successful, else a brief error description
    """
    get_application_logger().debug(f"Trying to remove portal user {username}...")
    return delete_from_table(DBTable.USER, dict(username=username))


def update_portal_user_profile(username: str, full_name: str, email: str, title: str, org: str) -> Optional[str]:
    """
    Update the profile for an existing user account on the Lisberger lab data portal.

    Args:
        username: Username of the account.
        full_name: The user's full name. Must be 5-50 chars long.
        email: The user's email address. Must be a valid email address up to 80 chars long.
        title: The user's title or position description; 0-50 chars long.
        org: The user's organization name; 0-50 chars long.
    Returns:
        None if successful, else a brief error message.
    """
    entry = dict(username=username)
    keys = ['full_name', 'contact_email', 'title', 'organization']
    values = [full_name, email, title, org]
    for i, k in enumerate(keys):
        if isinstance(values[i], str):
            entry[k] = None if (len(values[i]) == 0) else values[i]
    if len(entry) == 1:   # no changes
        return None
    error_msg = update_table_row(DBTable.USER, entry)
    get_application_logger().debug(f"{username} successfully changed profile." if (error_msg is None) else
                                   f"User profile change failed for {username}: {error_msg}")
    return error_msg


def change_portal_user_password(username: str, old_password: str, new_password: str) -> Optional[str]:
    """
    Change the password for an existing user account on the Lisberger lab data portal.

    Args:
        username: Username of the account.
        old_password: The user's current password. Operation fails if this is incorrect.
        new_password: The user's new password. Operation fails if this is not a valid password. No action taken if
            this matches 'old_password'.
    Returns:
        None if successful, else a brief error message.
    """
    if old_password == new_password:
        return "Password is unchanged. Enter a new password."
    error_msg = validate_password(new_password)
    if error_msg is not None:
        return f"New password in invalid ({error_msg})"

    entry = dict(username=username)
    res = fetch_restrict_proj([DBTable.USER], [entry], ['password'])
    if (res is None) or (len(res) != 1):
        error_msg = 'Unrecognized username or database error'
    elif not check_password_hash(res[0]['password'], old_password):
        error_msg = "Incorrect password"
    else:
        entry['password'] = generate_password_hash(new_password, method=PASSWORD_HASH_METHOD)
        entry['pwd_changed'] = datetime.now().isoformat(sep=' ', timespec='seconds')  # 'YYYY-MM-DD HH:MM:SS'
        error_msg = update_table_row(DBTable.USER, entry, log=False)  # we don't record password changes in DB ops log

    get_application_logger().debug(f"{username} successfully changed password." if (error_msg is None) else
                                   f"Password change failed for {username}: {error_msg}")
    return error_msg


def change_portal_user_access_level(username: str, access: str) -> Optional[str]:
    """
    Change the access level assigned to an existing user account on the Lisberger lab data portal.

    Args:
        username: Username of the account.
        access: The desired access level. Must be one of 'admin' > 'commit' > 'download'.

    Returns:
        None if successful, else a brief error description.
    """
    if access not in ACCESS_LEVELS:
        error_msg = f"Invalid access level: {access}"
    else:
        error_msg = update_table_row(DBTable.USER, dict(username=username, access=access))

    get_application_logger().debug(f"Changed {username}'s access level to {access}." if (error_msg is None) else
                                   f"Access level change failed for {username}: {error_msg}")
    return error_msg


def prompt_for_password(username: str) -> Optional[str]:
    """
    Request a password -- from STDIN -- for a portal user account to be added to the laboratory database. The method
    will prompt for the password twice to guard against accidental typos and verify that it meets requirements. If not,
    it will prompt again until an acceptable password is entered. It also gives the user the option to abort the
    request entirely by entering 'q' after the password prompt.

    NOTE: THIS IS A UTILITY METHOD INTENDED FOR USE IN SCRIPTS RUNNING IN A PYTHON CONSOLE. It uses getpass.getpass()
    to get the password input.

    Args:
        username: The username for the new account.
    Returns:
        A valid password for the account, or None if the user elected to abort the script.
    """
    while True:
        new_password = getpass(f"Enter the password for user '{username}', or 'q' to abort > ")
        if new_password == 'q':
            return None
        confirm_new = getpass('Reenter password to confirm > ')
        if confirm_new != new_password:
            print("   Password mismatch... Try again.", file=sys.stdout, flush=True)
        else:
            res = validate_password(new_password)
            if res is None:
                return new_password
            else:
                print(f"   {str(res)}... Try again.", file=sys.stdout, flush=True)
