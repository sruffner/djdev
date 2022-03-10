"""
log_ops.py: Access to the database operations log in the file repository for the Lisberger lab portal.

The database operations log is essentially a record of all operations performed on the database (via user interaction
through the web portal) since the last database "reset". It is a backup to the DB's own backup faciliities. In case of
catastrophic failure, the goal is to be able to repopulate the database from scratch by "playing back" all of the
operations recorded in this log file -- in concert with the session archives that are stored in the backing repository.

The operations log file is located at $REPO_HOME/logs/update_log, where $REPO_HOME is the root directory for the file
repository that backs the portal.

The database operations log, like the database itself, is a global resource. Since replicas of the portal backend may
be running simultaneously in the cloud-deployed portal application, it is possible that more than one replica (process)
could try to write the log at the same time. In an effort to prevent this, we implement an interprocess lock using a
lock file in the the same directory as the operations log file. This is an ADVISORY, NON-REENTRANT locking scheme. All
access to the operations log file must go through this module.

@author: sruffner
@created: 11oct2021
"""
import os
import pickle
import sys
from pathlib import Path
from typing import Dict, Optional, Set, TextIO
from contextlib import contextmanager
from fasteners import InterProcessLock

from config.config import get_application_logger
from database.table_info import DBTable, AttributeValue


_LOG_FILE_DIR: Path = Path(os.environ['DJDEV_ROOT_REPO'], 'logs')
""" Directory containing the database operations log file. """
_LOG_FILE_PATH: Path = Path(os.environ['DJDEV_ROOT_REPO'], 'logs', 'update_log')
""" The location of the database operations log file in the portal's file system-based backing repository. """
_LOG_LOCK_PATH: Path = Path(os.environ['DJDEV_ROOT_REPO'], 'logs', '.lock')
""" Lock file for advisory interprocess lock to mediate exclusive access to the database operations log. """


class FailedToAcquireLockException(Exception):
    """ Exception thrown if unable to acquire an advisory interprocess lock. """
    pass


class WithTimeout(InterProcessLock):
    """
    An extension of fasteners.InterProcessLock that lets you specify a timeout for acquiring the lock.

    Usage: WithTimeout('path.to.lockfile').locked(timeout_value_in_seconds)
    """
    @contextmanager
    def locked(self, timeout):
        ok = self.acquire(timeout=timeout)
        if not ok:
            raise FailedToAcquireLockException()
        try:
            yield
        finally:
            self.release()


def _ensure_logs_directory_exists() -> None:
    _LOG_FILE_DIR.mkdir(parents=True, exist_ok=True)


def log_file_path() -> Path:
    """ The file system path to the database operations log file in the portal backup repository. """
    return _LOG_FILE_PATH


def log_add_table_row(table_id: DBTable, row: Dict[str, AttributeValue]) -> Optional[str]:
    """
    Record the addition of a single row to the specified database table.

    Args:
        table_id: The table ID
        row: The row added.
    Returns:
        A brief error description on failure; else None.
    """
    error_msg: Optional[str] = None
    try:
        _ensure_logs_directory_exists()
        # NEVER store user's encrypted password in the log!
        if table_id == DBTable.USER:
            row.pop('password', None)
        with WithTimeout(_LOG_LOCK_PATH, 1):
            with open(_LOG_FILE_PATH, 'ab') as file:
                pickle.dump({'op': 'add', 'table': table_id, 'row': row}, file)
    except Exception as err:
        error_msg = f"Failed to post 'add' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def log_delete_from_table(table_id: DBTable, restriction: Optional[Dict[str, AttributeValue]]) -> Optional[str]:
    """
    Record the deletion of a single row from the specified database table.

    Args:
        table_id: The table ID
        restriction: A dictionary specifying the primary key of the single row that was deleted.
    Returns:
        A brief error description on failure; else None.
    """
    error_msg: Optional[str] = None
    try:
        _ensure_logs_directory_exists()
        with WithTimeout(_LOG_LOCK_PATH, 1):
            with open(_LOG_FILE_PATH, 'ab') as file:
                pickle.dump({'op': 'delete', 'table': table_id, 'restriction': restriction}, file)
    except Exception as err:
        error_msg = f"Failed to post 'delete' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def log_update_table_row(table_id: DBTable, row: Dict[str, AttributeValue]) -> Optional[str]:
    """
    Record the update of a single row in the specified database table.

    Args:
        table_id: The table ID
        row: The updated row's contents.
    Returns:
        A brief error description on failure; else None.
    """
    error_msg: Optional[str] = None
    try:
        _ensure_logs_directory_exists()
        # NEVER store user's encrypted password in the log!
        if table_id == DBTable.USER:
            row.pop('password', None)
            if len(row) == 1:
                return None
        with WithTimeout(_LOG_LOCK_PATH, 1):
            with open(_LOG_FILE_PATH, 'ab') as file:
                pickle.dump({'op': 'update', 'table': table_id, 'row': row}, file)
    except Exception as err:
        error_msg = f"Failed to post 'update' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def log_mapping_table_update(table_id: DBTable, src_pk_val: int, map_set: Set[int]) -> Optional[str]:
    """
    Record an update to the specified mapping table in the database.

    Args:
        table_id: ID of the mapping table.
        src_pk_val: Integer value identifying a row in the source table for the cross-reference.
        map_set: Set of integer primary key values identifying all rows in the destination table that map to the
            specified source entity.
    Returns:
        A brief error description on failure; else None.
    """
    error_msg: Optional[str] = None
    try:
        _ensure_logs_directory_exists()
        with WithTimeout(_LOG_LOCK_PATH, 1):
            with open(_LOG_FILE_PATH, 'ab') as file:
                pickle.dump({'op': 'mapping', 'table': table_id, 'src_pk': src_pk_val, 'dst_pks': map_set}, file)
    except Exception as err:
        error_msg = f"Failed to post 'mapping' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def log_session_commit(user: str, subject: str, session_date: str, suffix: int) -> Optional[str]:
    """
    Record the commit of an entire experiment session to the database. A typical commit involves hundreds or even
    thousands of database insertions. The arguments define the primary key of the experiment session. All other data
    associated with that session is backed up in the portal's file repository.

    Args:
        user: User ID of the experimenter.
        subject: ID of the experiment subject.
        session_date: Recording date for the session, in the format 'YYYY-MM-DD'
        suffix: Session suffix (in case multiple sessions were conducted on the same day with the same subject).
    Returns:
        A brief error description on failure; else None.
    """
    error_msg: Optional[str] = None
    try:
        _ensure_logs_directory_exists()
        with WithTimeout(_LOG_LOCK_PATH, 1):
            with open(_LOG_FILE_PATH, 'ab') as file:
                pickle.dump({'op': 'session', 'username': user, 'subj_id': subject, 'date': session_date,
                             'suffix': suffix}, file)
    except Exception as err:
        error_msg = f"Failed to post 'session' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def dump_log(out: Optional[TextIO] = sys.stdout) -> None:
    """
    Dump the entire contents of the database operations log to a text file stream.

    NOTE: THIS IS AN ADMINISTRATIVE FUNCTION that should never be called when the portal application is online. The
    method does NOT acquire an advisory interprocess lock before reading the operations log file.

    Args:
        out: The target text stream. Defaults to STDOUT.
    """
    log_path = log_file_path()
    if not log_path.is_file():
        print(f"=====> Error: No log file found at {str(log_path)}", file=out, flush=True)
        return

    print("\n****** Database operations log history ******\n", file=out, flush=True)
    try:
        num_entries = 0
        with open(log_path, 'rb') as file:
            while True:
                try:
                    entry = pickle.load(file)
                    num_entries += 1
                    print(f"{num_entries:04}:  {entry}", file=out)
                except EOFError:
                    break
    except Exception as e:
        err_msg = f"Error occurred while dumping database operations log: {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
        print(f"=====> {err_msg}", file=sys.stdout, flush=True)
    print("\n****** END Database operations log history ******\n")
