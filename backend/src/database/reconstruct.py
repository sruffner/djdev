"""
reconstruct.py: A script that reconstructs the lab database by processing all entries in the database update log.

For now, a single pickle file in the database backup repository, $REPO_HOME/logs/update_log, contains the entire history
of operations on the database. The database can be reconstructed from scratch by executing the operations stored in the
log file. Of course, to execute a session commit job, the requisite files must also be located in the data repository.

It is ESSENTIAL that the database be empty when the script is called -- that is it's assumed state just before the
first operation in update_log. The operations should be logged in chronological order, and this script simply executes
each operation in the log in the same order, thereby reconstructing the database content.

Very little user intervention is required, as the update log and the raw data repository stores everything that is
needed to repopulate the database. There is one exception, however: User passwords are, for security reasons, NEVER
included in update log entries. Therefore, in order to process a log entry that registers a new user on the portal, the
script must request an initial password for that user's account. After a successful reconstruction, the portal
administrator should contact all users and require that they change their password.

If the database is corrupted, return it to its empty state with the reset.py script, then reconstruct its content from
the database update log by running this script. Obviously, this script could take quite some time to run if the database
contains data from a large number of experiment sessions.

If the database reconstruction fails, manual reconstruction will be required. In this scenario, the log output from this
script may be useful

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.reconstruct'. Once the script completes, resume the normal
backend service with 'docker-compose restart backend'.

@author: sruffner
@created: 19may2021
"""
import pickle
import sys
import os
from getpass import getpass

import datajoint as dj

# DataJoint configuration parameters required to connect to the database. In the portal backend server, these are
# found in app.py. THESE NEED TO BE SETUP BEFORE THE CONNECTION ATTEMPT THAT OCCURS WHEN IMPORTING sgl_schema.py
# via the from...import statement that follows
from werkzeug.security import generate_password_hash

from database.table_info import DBTable

dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['safemode'] = False
dj.config['enable_python_native_blobs'] = True
if 'MYSQL_ROOT_PASSWORD' not in os.environ:
    print("====> ERROR: The environment variable MYSQL_ROOT_PASSWORD is missing!", flush=True)
dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']

from database.manager import DataBaseManager, PASSWORD_HASH_METHOD


def _prompt_for_password(username: str) -> str:
    """
    Request a password for a portal user account to be added during reconstruction of the laboratory database. The
    method will prompt for the password twice to guard against accidental typos and verify that it meets requirements.
    If not, it will prompt again until an acceptable password is entered.

    Args:
        username: The username for the new account.
    Returns:
        A valid password for the account.
    """
    while True:
        new_password = getpass(f"Enter the password for user '{username}' > ")
        confirm_new = getpass('Reenter password to confirm > ')
        if confirm_new != new_password:
            print("   Password mismatch... Try again.", file=sys.stdout, flush=True)
        else:
            res = db_mgr.validate_password(new_password)
            if res is None:
                return new_password
            else:
                print(f"   {str(res)}... Try again.", file=sys.stdout, flush=True)


if __name__ == '__main__':
    print("reconstruct.py: Rebuild Lisberger lab database from the update log history and raw data repository...\n\n",
          file=sys.stdout, flush=True)

    db_mgr = DataBaseManager()

    # ensure database update log exists and verify that database is empty
    log_file_path = DataBaseManager.log_file_path()
    if not log_file_path.is_file():
        print(f"ERROR: No database log file found at {str(log_file_path)}\n\n...BYE!", file=sys.stdout, flush=True)
        exit(0)
    err_msg = db_mgr.database_empty()
    if err_msg is not None:
        print(f"ERROR: {err_msg}. Database must be empty prior to reconstruction!\n\n...BYE!", file=sys.stdout,
              flush=True)
        exit(0)

    print(f"Starting database reconstruction from repository using log file at {str(log_file_path)}...",
          file=sys.stdout, flush=True)

    try:
        num_entries = 0
        with open(log_file_path, 'rb') as file:
            while True:
                try:
                    entry = pickle.load(file)
                    num_entries += 1
                    print(f"Processing log entry #{num_entries}: \n    {entry}", file=sys.stdout, flush=True)
                    if entry['op'] == 'add':
                        # SPECIAL CASE: When adding a user account, we must prompt for an initial password
                        if entry['table'] == DBTable.USER:
                            print(f" *** You must specify a valid initial password for each user added to database...",
                                  file=sys.stdout, flush=True)
                            password = _prompt_for_password(entry['row']['username'])
                            entry['row']['password'] = generate_password_hash(password, method=PASSWORD_HASH_METHOD)
                        err_msg = db_mgr.insert_into_table(entry['table'], entry['row'], log=False)
                    elif entry['op'] == 'delete':
                        err_msg = db_mgr.delete_from_table(entry['table'], entry['restriction'], log=False)
                    elif entry['op'] == 'update':
                        err_msg = db_mgr.update_table_row(entry['table'], entry['row'], log=False)
                    elif entry['op'] == 'mapping':
                        err_msg = db_mgr.update_mapping_table(
                            entry['table'], entry['src_pk'], entry['dst_pks'], log=False)
                    elif entry['op'] == 'session':
                        err_msg = DataBaseManager.reconstruct_session(entry)
                    else:
                        err_msg = f"Invalid log entry"

                    if err_msg is not None:
                        raise Exception(err_msg)
                except EOFError:
                    break
    except Exception as e:
        print(f"ERROR: Exception occurred while reconstructing lab database: {str(e)}", file=sys.stdout, flush=True)
        print("Manual reconstruction of database content required. Consult this script's progress log to "
              "assist in that reconstruction.", file=sys.stdout, flush=True)
        exit(0)

    print("Reconstruction completed successfully!", file=sys.stdout, flush=True)

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
