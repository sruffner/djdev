"""
user_admin.py: A script to manage the list of users authorized for restricted access to the Lisberger lab data portal.

Some pages in the Lisberger lab data portal allow a client to curate information in selected database tables or commit
experimental sessions to the database. These actions must be restricted to authorized users only. The portal app will
use the Flask-Login package to implement "in-app" user login and authentication, relying on session cookies to restrict
access to protected pages in the web portal.

The Flask-Login package does not implement an authenticated users list; that is up to the application. The User table
in the lab database (sgl_schema.py) serves this purpose. Each user account includes an attribute that stores the
user's encrypted password. While most user management-related activities can be accessed via the portal's web interface,
those same activities are accessible by this script.

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.user_admin'. Then follow the input prompt to perform a variety
of operations on the list of registered portal users: insert a new user, change an existing user's access level or
password, update a user's profile, delete an existing user (forbidden if there exist dependencies on that user in other
database tables), or list all users in the database. Since these are sensitive operations, the script is restricted to
an existing user that has 'admin'-level privileges. However, if there is no such user currently in the database, then
the script will require you create an initial 'admin' account before performing any other operations.

@created: 27jul2021
@author: sruffner
"""
import sys
from datetime import datetime
from getpass import getpass
from typing import Optional

from config.config import get_config

# configure DataJoint and connect to MySQL server. Must abort if connection is not established!
cfg = get_config()
if not cfg.init_database_connection():
    raise RuntimeError('Unable to connect to database!')

# We have to put this import AFTER configuring DJ and connecting to the database, since it will trigger a DB query
from database.manager import DataBaseManager, ACCESS_LEVELS
from database.table_info import DBTable


def _print_user_list() -> Optional[str]:
    error_msg = None
    try:
        user_rows = DataBaseManager().fetch_rows(DBTable.USER)
        print(f"   {len(user_rows)} users found:", file=sys.stdout, flush=True)
        header = '{:<20} {:<50} {:<50} {:<10} {:<20} {:<20} {:<20}'.format(
            'USERNAME', 'NAME/EMAIL ADDRESS', 'TITLE/ORG', 'ACCESS', 'REGISTERED', 'LAST LOGIN', 'LAST PWD')
        print(f"   {header}", file=sys.stdout, flush=True)
        for row in user_rows:
            truncated_email = row['contact_email'][0:50]
            last_login = "Never logged in" if (row['last_login'] is None) else str(row['last_login'])
            title = "Unspecified" if (row['title'] is None) else row['title']
            organization = "Unspecified" if (row['organization'] is None) else row['organization']
            blank = "  "
            print(f"   {row['username']:<20} {row['full_name']:<50} {title:<50} {row['access']:<10}"
                  f" {str(row['registered']):<20} {last_login:<20} {str(row['pwd_changed']):<20}\n"
                  f"   {blank:<20} {truncated_email:<50} {organization:<50}",
                  file=sys.stdout, flush=True)
    except Exception as e:
        error_msg = f"Failed to retrieve user list: {str(e)}"
    return error_msg


def _print_usage() -> None:
    print("Available commands:\n"
          "   a = Add a new user.\n"
          "   d = Delete an existing user.\n"
          "   c = Change a user's access level.\n"
          "   p = Change a user's password.\n"
          "   u = Change user's profile information.\n"
          "   l = List all existing users.\n"
          "   t = Test login.\n"
          "   h = Print this usage message.\n"
          "   x = Exit.\n\n")


def _process_command() -> bool:
    db_mgr = DataBaseManager()
    command = input('Enter command (a,d,c,p,u,l,t,h,x) > ')
    error_msg = None
    if command == 'a':
        username = input('Enter user name (3-20 lowercase letters or digits, starting with a letter) > ')
        access = input(f"Enter access level ({', '.join(ACCESS_LEVELS)}) > ")
        full_name = input('Enter full name (eg. "Jane E. Doe", "William Smith, PhD"; 5-50 chars) > ')
        email = input('Enter email address > ')
        password = getpass('Enter password (8-32 characters) > ')
        confirm_password = getpass('Confirm password > ')
        if password != confirm_password:
            error_msg = "Password mismatch"
        else:
            error_msg = db_mgr.register_new_portal_user(username, password, access, full_name, email)
    elif command == 'd':
        username = input('Enter username of user to be removed > ')
        error_msg = db_mgr.remove_portal_user(username)
    elif command == 'c':
        username = input('Enter username > ')
        access = input(f"Enter access level ({', '.join(ACCESS_LEVELS)}) > ")
        error_msg = db_mgr.change_portal_user_access_level(username, access)
    elif command == 'p':
        username = input('Enter username > ')
        old_password = getpass('Enter current password > ')
        new_password = getpass('Enter new password > ')
        confirm_new = getpass('Confirm new password > ')
        error_msg = \
            "Password mismatch" if (new_password != confirm_new) \
            else db_mgr.change_portal_user_password(username, old_password, new_password)
    elif command == 'u':
        username = input('Enter username > ')
        user_record = db_mgr.get_portal_user_record(username)
        if not isinstance(user_record, dict):
            error_msg = user_record
        else:
            print("*** To update any profile parameter, enter the new value after the prompt. To leave a"
                  "parameter unchanged, simply hit Return.\n")
            full_name = input(f"Full name: {user_record['full_name']} > ")
            contact_email = input(f"Email: {user_record['contact_email']} > ")
            title = input(f"Title: {user_record['title']} > ")
            organization = input(f"Organization: {user_record['organization']} > ")
            error_msg = db_mgr.update_portal_user_profile(
                username, full_name=(full_name if len(full_name) > 0 else None),
                email=(contact_email if len(contact_email) > 0 else None), title=(title if len(title) > 0 else None),
                org=(organization if len(organization) > 0 else None))
    elif command == 'l':
        error_msg = _print_user_list()
    elif command == 'h':
        _print_usage()
    elif command == 't':
        username = input('Enter username > ')
        password = getpass('Enter password > ')
        error_msg = db_mgr.authenticate_portal_user(username, password)
    elif command == 'x':
        return True
    else:
        error_msg = f"Unrecognized command: {command}. Try again."

    print(f"ERROR: {error_msg}\n\n" if isinstance(error_msg, str) else "OK.\n\n", file=sys.stdout, flush=True)
    return False


def _admin_account_exists() -> bool:
    return len(DataBaseManager().fetch_rows(DBTable.USER, dict(access='admin'))) > 0


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
        e_msg = DataBaseManager().authenticate_portal_user(admin_username, admin_password, admin_only=True)
        if e_msg is not None:
            print(f"ERROR: {e_msg}... BYE!", file=sys.stdout, flush=True)
            exit(0)
    else:
        print("********************\n"
              " WARNING. There are currently no 'admin'-level users in the portal authorized users database.\n"
              " Please create an admin user account NOW.\n"
              "********************\n\n")
        while True:
            admin_username = input('Enter admin user name (3-20 lowercase letters or digits, starting with letter) > ')
            admin_full_name = input('Enter full name (eg. "Jane E. Doe", "William Smith, PhD"; 5-50 chars) > ')
            admin_email = input('Enter email address > ')
            admin_password = getpass('Enter password (8-32 characters, at least 1 digit and uppercase letter) > ')
            admin_confirm_password = getpass('Confirm password > ')
            if admin_password != admin_confirm_password:
                e_msg = "Password mismatch"
            else:
                e_msg = DataBaseManager().register_new_portal_user(
                    admin_username, admin_password, 'admin', admin_full_name, admin_email)
            if e_msg is None:
                break
            else:
                response = input(f"ERROR: {e_msg}\n\n Try again? ('y') > ")
                if response != 'y':
                    print("\n\nBYE!", file=sys.stdout, flush=True)
                    exit(0)

    _print_usage()
    done = False
    while not done:
        done = _process_command()

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
