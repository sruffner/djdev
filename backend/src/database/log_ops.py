"""
log_ops.py: Access to the database operations log in the file repository for the Lisberger lab portal.

The database operations log is essentially a record of all operations performed on the database (via user interaction
through the web portal) since the last database "reset". It is a backup to the DB's own backup faciliities. In case of
catastrophic failure, the goal is to be able to repopulate the database from scratch by "playing back" all of the
operations recorded in this log file -- in concert with the session archives that are stored in the backing repository.

The operations log file is located at $WS/logs/database_ops.log, where $WS is the portal workspace directory on a file
system mount accessible to the backend server. For safety's sake, the log file is periodically backed up to the portal
backing repository maintained in an Amazon S3 bucket. The backup occurs in the background and is scheduled to happen
roughly once every 24 hours. Of course, if there are no database changes, the log file is unchanged and a backup is
unnecessary.

The operations log is currently implemented as a single log file that continues to grow over time. In the future, it
may be necessary to divide it into a sequence of log files: database_ops.log.N, where the integer extension indicates
the order in which the files were written.

The database operations log, like the database itself, is a global resource. Since replicas of the portal backend may
be running simultaneously in the cloud-deployed portal application, it is possible that more than one replica (process)
could try to write the log at the same time. In an effort to prevent this, we implement an interprocess lock using a
lock file in the the same directory as the operations log file. This is an ADVISORY, NON-REENTRANT locking scheme. All
access to the operations log file must go through this module.

@author: sruffner
@created: 11oct2021
"""
import pickle
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Dict, Optional, Set, TextIO
from contextlib import contextmanager
from fasteners import InterProcessLock
from rq import Queue

from config.app_logging import get_application_logger
from config.config import get_config
from database import repo
from database.table_info import DBTable, AttributeValue
from sglportalapi.util import size_with_units

_LOG_DIR_NAME: str = 'logs'
_LOG_FILE_NAME: str = 'database_ops.log'

_LOG_FILE_DIR: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME)
""" Directory containing the database operations log file. """
_LOG_FILE_PATH: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME, _LOG_FILE_NAME)
""" The location of the database operations log file in the portal's file system-based backing repository. """
_LOG_LOCK_PATH: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME, '.lock')
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
        schedule_log_backup_if_necessary()
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
        schedule_log_backup_if_necessary()
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
        schedule_log_backup_if_necessary()
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
        schedule_log_backup_if_necessary()
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
        schedule_log_backup_if_necessary()
    except Exception as err:
        error_msg = f"Failed to post 'session' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def schedule_log_backup_if_necessary(soon: bool = False) -> None:
    """
    Schedule a background job to push a copy of the database operations log from the portal workspace to the backing
    repository.

    The database operations log is located in the portal workspace directory, on a file system mount accessible to the
    backend server process. The file contains the entire history of operations on the portal database and is essential
    if we ever need to reconstruct the database. The file is backed up regularly to the portal's backing repository,
    which also stores the ZIP archives for experiment sessions that have been committed to the database. That repository
    is maintained in an Amazon S3 bucket provisioned by the Lisberger lab.

    Call this method to schedule a database log backup job. If a job is already scheduled, no action is taken.

    Args:
        soon: If True, the backup is scheduled to take place one minute from "now". Otherwise, it is scheduled to
            happen in 24 hours. Default = False. If the log has never been backed up, this argument is ignored and a
            backup is scheduled for 1 minute from now.
    """
    job_queue = Queue(connection=get_config().redis_conn)
    if len(job_queue.scheduled_job_registry) == 0:
        if 0 == repo.file_size(f"/{_LOG_DIR_NAME}/{_LOG_FILE_NAME}"):
            soon = True
        delta = timedelta(minutes=1) if soon else timedelta(hours=24)
        job_queue.enqueue_in(time_delta=delta, func=backup_log_to_repo)
        get_application_logger().info(f"Scheduled database ops log backup {'1 min' if soon else '24 hr'} from now.")


def backup_log_to_repo() -> None:
    """
    Push a copy of the current database operations log in the portal workspace to the backing repository on S3.

    This method is intended to be called on a background process independent from the Dash/Flask backend server.
    If the current size of the operations log in the portal workspace exceeds the size of its backup copy in the portal
    repository, the method copies the log to a temporary file (in case other processes are updating the log file
    at the same time), then uploads that temporary file to the repository, replacing the old backup copy of the log.
    """
    # we need to get the current size N of the log file while holding the interprocess lock. After releasing the lock,
    # another server replica could append entries to the log file, but that's OK. We only copy the first N bytes.
    _ensure_logs_directory_exists()
    log_path = log_file_path()
    curr_size = 0
    try:
        with WithTimeout(_LOG_LOCK_PATH, 1):
            curr_size = log_path.stat().st_size
    except Exception:
        pass

    key = f"/{_LOG_DIR_NAME}/{_LOG_FILE_NAME}"
    if curr_size <= repo.file_size(key):
        get_application_logger().info(f"No need to backup {_LOG_FILE_NAME}.")
        return

    tmp_file_path = Path(_LOG_FILE_DIR, f"tmp_{str(uuid.uuid4())}.log")
    try:
        with open(log_path, 'rb') as src, open(tmp_file_path, 'wb') as dst:
            data = src.read(curr_size)
            dst.write(data)
        if not repo.upload_file(tmp_file_path, key):
            get_application_logger().error(
                "Failed to upload current database ops log to portal repository; check system logs.")
        else:
            get_application_logger().info(f"Backed up current database operations log "
                                          f"({size_with_units(curr_size)}) to portal repository.")
    except Exception:
        get_application_logger().error(f"Database operations log backup failed.", exc_info=True)
    finally:
        try:
            tmp_file_path.unlink(missing_ok=True)
        except Exception:
            pass


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
