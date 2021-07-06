"""
sgl_auth.py: A DataJoint schema for a database of users authorized to access protected routes in the Lisberger
    lab data portal.

Some pages in the Lisberger lab data portal allow a client to curate information in selected database tables or commit
experimental sessions to the database. These actions must be restricted to authorized users only. The portal app will
use the Flask-Login package to implement "in-app" user login and authentication, relying on session cookies to restrict
access to protected pages in the web portal.

The Flask-Login package does not implement an authenticated users list; that is up to the application. We have decided
to create a small database, 'sgl-auth', on the same MySQL server that hosts the primary database for the web portal.
This database contains a single table, AuthorizedUser, that holds information on all authorized users. We will use
DataJoint to access the authentication database, and this file defines the DataJoint schema for the database.

Run as a script ('docker-compose run backend python -m database.sgl_auth') to perform a variety of operations on the
'sgl-auth' database: insert a new user, change an existing user's access level or password, delete an existing user,
remove all users from the database, or list all users in the database. Since these are sensitive operations, the script
is restricted to an existing user that has 'admin'-level privileges. However, if there is no such user currently in the
database, then anyone can modify the user database.

@created: 29jun2021
@author: sruffner
"""
import os
import re
import sys
from datetime import datetime
from getpass import getpass
from typing import Optional, Union, Dict

import datajoint as dj
import time
import warnings

# On first import, we connect to the database and declare the schema (if it is not already defined in database)
# NOTE: Since the 'sgl-auth' database is on the same server as the lab database 'sgl', and since the database server
# connection is shared, we must be careful not to access this SHARED connection from different threads.
from werkzeug.security import generate_password_hash, check_password_hash

# DataJoint configuration parameters required to connect to the database. In the portal backend server, these are
# found in app.py. THESE NEED TO BE SETUP BEFORE THE CONNECTION ATTEMPT THAT OCCURS BELOW
dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['safemode'] = False
dj.config['enable_python_native_blobs'] = True
if 'MYSQL_ROOT_PASSWORD' not in os.environ:
    print("====> ERROR: The environment variable MYSQL_ROOT_PASSWORD is missing", flush=True)
dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']

while True:
    try:
        db_connection = dj.conn()
        break
    except Exception as connection_error:
        warnings.warn(RuntimeWarning(
            "Unable to connect to the database with error {0}. Trying again in 5s.".format(connection_error)))
        time.sleep(5)
schema = dj.schema('sgl-auth', connection=db_connection)


@schema
class AuthorizedUser(dj.Manual):
    definition = """
    # Authorized users with restricted access to the Lisberger lab data portal
    username : varchar(20)              # Login name
    ---
    password : char(93)                 # Password hash
    access : enum("admin", "curate", "contribute", "readonly")   # level of access to restricted portal functions
    full_name : varchar(50)             # Full name
    contact_email : varchar(80)         # Email address
    title = NULL : varchar(50)          # Position description or title, eg, 'PostDoc, Lisberger Lab'
    organization = NULL : varchar(50)   # University, research center, etc
    registered = CURRENT_TIMESTAMP : timestamp  # date/time that user was registered as an authorized user
    last_login = NULL : timestamp       # date/time of user's last login
    """


_PASSWORD_HASH_METHOD = 'pbkdf2:sha256:10000'
""" Method used to generate hashed passwords that are stored in DB """
_USER_PROFILE_KEYS = {'full_name', 'contact_email', 'title', 'organization'}
""" Set of attributes that are part of an authorized user's editable profile. """
_ACCESS_LEVELS = ['admin', 'curate', 'contribute', 'readonly']


def _insert_user(username: str, password: str, confirm: str, access: str, full_name: str,
                 contact_email: str) -> Optional[str]:
    """
    Create a new user account with restricted access to the Lisberger lab data portal.

    Args:
        username: The username (3-20 lowercase letters or digits, starting with a letter).
        password: Plain-text password (8-32 characters, with at least one digit and one uppercase character). For
            security, the password will be stored in the database in encrypted form.
        confirm: The password again, to verify entry. If this doesn't match the password argument, the operation fails.
        access: Access level assigned to the new user. Must be one of 'admin' > 'curate' > 'contribute' > 'readonly'.
        full_name: The user's full name. Must be 5-50 characters long, but otherwise unchecked for format.
        contact_email: The user's email address. Up to 80 characters long and checked for valid format.
    Returns:
        None if a new user is successfully added; else a brief error description.
    """
    error_msg = _check_user(username, password, confirm, access, full_name, contact_email)
    if error_msg is not None:
        return error_msg

    table: dj.Table = AuthorizedUser()
    row = dict(username=username, password=generate_password_hash(password, method=_PASSWORD_HASH_METHOD),
               access=access, full_name=full_name, contact_email=contact_email)
    try:
        with table.connection.transaction:
            table.insert1(row, replace=False)
    except Exception as e:
        error_msg = f"Failed to register new user {username}: {str(e)}"
    return error_msg


def _check_user(username: str, password: str, confirm: str, access: str, full_name: str,
                contact_email: str) -> Optional[str]:
    """
    Helper method for _insert_user() validates required information for a new user account.

    Returns:
        None if user information is valid; else a brief error description
    """
    error_msg = _validate_password(password, confirm)
    if error_msg is not None:
        return error_msg
    if access not in _ACCESS_LEVELS:
        return f"Invalid access level: {access}"
    if not (5 <= len(full_name) <= 50):
        return "Full name must have 5-50 characters"
    if (len(contact_email) > 80) or \
       (re.fullmatch(r'^[A-Za-z0-9._+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6}$', contact_email) is None):
        return "Email address is too long or otherwise invalid"
    if re.fullmatch(r'^[a-z][a-z0-9]{2,19}$', username) is None:
        return "Invalid username"
    try:
        table: dj.Table = AuthorizedUser()
        pk = dict(username=username)
        if len(table & pk) != 0:
            return "Username is already taken"
    except Exception:
        pass
    return None


def _validate_password(password: str, confirm: str) -> Optional[str]:
    """ Helper method enforces restrictions on a user password."""
    if password != confirm:
        return 'Password mismatch'
    elif not (8 <= len(password) <= 32):
        return "Password must have 8-32 characters"
    elif (re.search(r"[\d]+", password) is None) and (re.search(r"[A-Z]+", password) is None):
        return 'Password must contain at least 1 digit and at least 1 uppercase character'
    return None


def _delete_user(username: Optional[str] = None) -> Optional[str]:
    """
    Remove a user login account for the Lisberger lab data portal.

    Args:
        username: Username of account to be deleted. If None (the default), ALL user accounts are removed!
    Returns:
        None if user account was removed; else a brief error description.
    """
    table: dj.Table = AuthorizedUser()
    error_msg = None
    try:
        with table.connection.transaction:
            if username is None:
                table.delete()
            else:
                (table & dict(username=username)).delete()
    except Exception as e:
        error_msg = "Failed to remove all users" if (username is None) else f"Failed to remove user {username}"
        error_msg += f": {str(e)}"
    return error_msg


def _get_user(username: str) -> Union[str, Dict[str, str]]:
    """
    Retrieve a user account record from the database of users authorized for restricted access to the Lisberger lab
    data portal.

    Args:
        username: Username of the account.
    Returns:
        If successful, returns the user account record as a dictionary of key-value pairs. Otherwise, returns a brief
        error description.
    """
    table: dj.Table = AuthorizedUser()
    pk = dict(username=username)
    error_msg = None
    user_record = None
    try:
        user_record = (table & pk).fetch1()
    except Exception as e:
        error_msg = f'Unrecognized username or other failure: {str(e)}'
    return error_msg if (error_msg is not None) else user_record


def authenticate_user(username: str, password: str, admin_only: bool = False) -> Optional[str]:
    """
    Authenticate the user account with the specified name and password. If the account exists, update the account's
    "last login" timestamp.

    Args:
        username: The username for the account.
        password: The (plaintext) password for the account.
        admin_only: If True, require that the user account have 'admin'-level privileges. Default is False.
    Returns:
        None if account was authenticated; else a brief error description (invalid username, etc.)
    """
    table: dj.Table = AuthorizedUser()
    pk = dict(username=username)
    error_msg = None
    try:
        hashed_password, access = (table & pk).fetch1('password', 'access')
        if not check_password_hash(hashed_password, password):
            error_msg = "Incorrect password"
        if admin_only and (access != 'admin'):
            error_msg = "Admin-level access required"
    except Exception:
        error_msg = 'Unrecognized username'
    if error_msg is None:
        try:
            last_login = datetime.now().isoformat(sep=' ', timespec='seconds')   # 'YYYY-MM-DD HH:MM:SS'
            entry = dict(username=username, last_login=last_login)
            table.update1(entry)
        except Exception:
            pass
    return error_msg


def change_password(username: str, old_password: str, new_password: str) -> Optional[str]:
    """
    Change the password for an existing user account on the Lisberger lab portal.

    Args:
        username: The username for the account.
        old_password: The current (plaintext) password for the account.
        new_password: The new (plaintext) password for the account.
    Returns:
        None if password was successfully changed; else a brief error description.
    """
    error_msg = _validate_password(new_password, new_password)
    if error_msg is not None:
        return f"New password in invalid ({error_msg})"
    table: dj.Table = AuthorizedUser()
    entry = dict(username=username)
    try:
        hashed_password = (table & entry).fetch1('password')
        if not check_password_hash(hashed_password, old_password):
            raise Exception("Current password is incorrect")
        entry['password'] = generate_password_hash(new_password, method=_PASSWORD_HASH_METHOD)
        table.update1(entry)
    except Exception as e:
        error_msg = f"Failed to update password: {str(e)}"
    return error_msg


def _change_access(username: str, access: str) -> Optional[str]:
    """
    Change the restricted access level assigned to an existing user account on the Lisberger lab data portal.

    Args:
        username: The username for the account.
        access: The requested access level. Must be one of 'admin' > 'curate' > 'contribute' > 'readonly'.
    Returns:
        None if operation was successful; else a brief error description.
    """
    if access not in _ACCESS_LEVELS:
        return f"Invalid access level: {access}"
    table: dj.Table = AuthorizedUser()
    entry = dict(username=username, access=access)
    error_msg = None
    try:
        table.update1(entry)
    except Exception as e:
        error_msg = f"Failed to update user access level: {str(e)}"
    return error_msg


def update_user_profile(username: str, full_name: str = None, contact_email: str = None, title: str = None,
                        organization: str = None) -> Optional[str]:
    """
    Update selected information in the user account profile. A user can freely change their full name, email address,
    title, and organization.

    Args:
        username: The username for the account.
        full_name: New value for user's full name (5-50 chars), or None if no change. Default is None.
        contact_email: New value for user's email address (up to 80 chars, checked for validity), or None if no change.
            Default is None.
        title: New value for user's title (0-50 chars) or None if no change. Default is None.
        organization: New value for user's organization (0-50 chars) or None if no change. Default is None.
    Returns:
        None if profile information is updated successfully; else a brief error description.
    """
    # validate profile values supplied and prepare table update entry
    entry = dict(username=username)
    if full_name is not None:
        if not (5 <= len(full_name) <= 50):
            return "Full name must have 5-50 characters"
        else:
            entry['full_name'] = full_name
    if contact_email is not None:
        if (len(contact_email) > 80) or \
           (re.fullmatch(r'^[A-Za-z0-9._+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6}$', contact_email) is None):
            return "Email address is too long or otherwise invalid"
        else:
            entry['contact_email'] = contact_email
    if title is not None:
        if len(title) > 50:
            return "Title can be no longer than 50 characters"
        else:
            entry['title'] = title
    if organization is not None:
        if len(organization) > 50:
            return "Organization name can be no longer than 50 characters"
        else:
            entry['organization'] = organization
    table: dj.Table = AuthorizedUser()
    error_msg = None
    if len(entry) > 1:
        try:
            table.update1(entry)
        except Exception as e:
            error_msg = f"Failed to update user profile: {str(e)}"
    return error_msg


def _print_user_list() -> Optional[str]:
    error_msg = None
    try:
        user_rows = AuthorizedUser().fetch(as_dict=True)
        print(f"   {len(user_rows)} users found:", file=sys.stdout, flush=True)
        header = '{:<20} {:<50} {:<50} {:<10} {:<20} {:<20}'.format('USERNAME', 'NAME/EMAIL ADDRESS', 'TITLE/ORG',
                                                                    'ACCESS', 'LAST LOGIN', 'REGISTERED')
        print(f"   {header}", file=sys.stdout, flush=True)
        for row in user_rows:
            truncated_email = row['contact_email'][0:50]
            last_login = "Never logged in" if (row['last_login'] is None) else str(row['last_login'])
            title = "Unspecified" if (row['title'] is None) else row['title']
            organization = "Unspecified" if (row['organization'] is None) else row['organization']
            blank = "  "
            print(f"   {row['username']:<20} {row['full_name']:<50} {title:<50} {row['access']:<10} {last_login:<20}"
                  f" {str(row['registered']):<20}\n"
                  f"   {blank:<20} {truncated_email:<50} {organization:<50}",
                  file=sys.stdout, flush=True)
    except Exception as e:
        error_msg = f"Failed to retrieve user list: {str(e)}"
    return error_msg


def _print_usage() -> None:
    print("Available commands:\n"
          "   a = Add a new user.\n"
          "   d = Delete an existing user.\n"
          "   r = Remove ALL users.\n"
          "   c = Change a user's access level.\n"
          "   p = Change a user's password.\n"
          "   u = Change user's profile information.\n"
          "   l = List all existing users.\n"
          "   t = Test login.\n"
          "   h = Print this usage message.\n"
          "   x = Exit.\n\n")


def _process_command() -> bool:
    command = input('Enter command (a,d,r,c,p,u,l,t,h,x) > ')
    error_msg = None
    if command == 'a':
        username = input('Enter user name (3-20 lowercase letters or digits, starting with a letter) > ')
        access = input('Enter access level (admin, curate, contribute, readonly) > ')
        full_name = input('Enter full name (eg. "Jane E. Doe", "William Smith, PhD"; 5-50 chars) > ')
        email = input('Enter email address > ')
        password = getpass('Enter password (8-32 characters) > ')
        confirm_password = getpass('Confirm password > ')
        error_msg = _insert_user(username, password, confirm_password, access, full_name, email)
    elif command == 'd':
        username = input('Enter username of user to be removed > ')
        error_msg = _delete_user(username)
    elif command == 'r':
        error_msg = _delete_user()
    elif command == 'c':
        username = input('Enter username > ')
        access = input('Enter access level (admin, curate, contribute, readonly) > ')
        error_msg = _change_access(username, access)
    elif command == 'p':
        username = input('Enter username > ')
        old_password = getpass('Enter current password > ')
        new_password = getpass('Enter new password > ')
        confirm_new = getpass('Confirm new password > ')
        error_msg = \
            "Password mismatch" if (new_password != confirm_new) \
            else change_password(username, old_password, new_password)
    elif command == 'u':
        username = input('Enter username > ')
        user_record = _get_user(username)
        if not isinstance(user_record, dict):
            error_msg = user_record
        else:
            print("*** To update any profile parameter, enter the new value after the prompt. To leave a"
                  "parameter unchanged, simply hit Return.\n")
            full_name = input(f"Full name: {user_record['full_name']} > ")
            contact_email = input(f"Email: {user_record['contact_email']} > ")
            title = input(f"Title: {user_record['title']} > ")
            organization = input(f"Organization: {user_record['organization']} > ")
            error_msg = update_user_profile(username,
                                            full_name=(full_name if len(full_name) > 0 else None),
                                            contact_email=(contact_email if len(contact_email) > 0 else None),
                                            title=(title if len(title) > 0 else None),
                                            organization=(organization if len(organization) > 0 else None))
    elif command == 'l':
        error_msg = _print_user_list()
    elif command == 'h':
        _print_usage()
    elif command == 't':
        username = input('Enter username > ')
        password = getpass('Enter password > ')
        error_msg = authenticate_user(username, password)
    elif command == 'x':
        return True
    else:
        error_msg = f"Unrecognized command: {command}. Try again."

    print(f"ERROR: {error_msg}\n\n" if isinstance(error_msg, str) else "OK.\n\n", file=sys.stdout, flush=True)
    return False


def _admin_account_exists() -> bool:
    table: dj.Table = AuthorizedUser()
    found = False
    try:
        found = bool(table & 'access = "admin"')
    except Exception:
        pass
    return found


if __name__ == '__main__':
    print("sgl_auth.py: Manage database of authorized users having access to restricted sections of the Lisberger lab "
          "data portal...\n", file=sys.stdout, flush=True)
    print(f"The current time is : {datetime.now().isoformat(sep=' ', timespec='seconds')} UTC\n",
          file=sys.stdout, flush=True)

    # we only want users with 'admin'-level privileges to use this script, but there might not be any!
    if _admin_account_exists():
        print("** Please login. Only 'admin'-level users can modify the portal authorized users database.**")
        admin_username = input('Enter username > ')
        admin_password = getpass('Enter password > ')
        e_msg = authenticate_user(admin_username, admin_password, admin_only=True)
        if e_msg is not None:
            print(f"ERROR: {e_msg}... BYE!", file=sys.stdout, flush=True)
            exit(0)
    else:
        print("********************\n"
              " WARNING. There are currently no 'admin'-level users in the portal authorized users database.\n"
              " Please create an admin user account.\n"
              "********************\n\n")

    _print_usage()
    done = False
    while not done:
        done = _process_command()

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
