"""
log_ops.py: Functions pertainign to the Lisberger lab portal's database operations log and API requests log.

**Database Operations Log:**

The database operations log is essentially a record of all operations performed on the database -- other than read-only
retrievals -- since the last database "reset". It is a backup to the DB's own backup faciliities. In case of
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

**Each log entry is serialized using a custom JSON encoder to handle data types that cannot be serialized by the
standard JSON module.** However, since JSON is not a framed protocol, we cannot use json.dump() to append each new log
entry to the log file. Instead, each JSONified entry is encoded as a byte sequence, and that bytes object is appended to
the log file, preceded by its length. These low-level details are handled internally within this module.

The database operations log, like the database itself, is a global resource. Since replicas of the portal backend may
be running simultaneously in the cloud-deployed portal application, it is possible that more than one replica (process)
could try to write the log at the same time. In an effort to prevent this, we implement an interprocess lock using a
lock file in the the same directory as the operations log file. This is an ADVISORY, NON-REENTRANT locking scheme. All
access to the operations log file must go through this module.

**API Requests Log:**

The portal implements a number of API 'endpoints' by which a client can retrieve information from the underlying
portal database outside the context of a web browser. A clientside Python package is available for download that handles
the details of sending requests to and unpacking the responses from these endpoints. This is the preferred method by
which registered portal users can retrieve selected data sets for scripted analysis. All API endpoint requests,
including requests to download the clientside package, are recorded in the API Requests Log, also implemented as a
single log file that grows over time.

The log file is stored in the same folder as the database operations log: $WS/logs/api_requests.log. The same locking
scheme (but using a different lock file) is used to guard access to the log file, and the requests log is backed up to
S3 as part of the same background task that backs up the database operations log file.

Authoer: saruffner
"""
import base64
import json
import struct
import sys
import uuid
from datetime import timedelta, date, datetime
from pathlib import Path
from typing import Dict, Optional, Set, TextIO, Any, List
from contextlib import contextmanager

import numpy as np
from fasteners import InterProcessLock
from rq import Queue

from config.app_logging import get_application_logger
from config.config import get_config
from database import repo
from database.table_info import DBTable, AttributeValue
from sglportalapi.util import size_with_units

_LOG_DIR_NAME: str = 'logs'
""" Name of portal workspace directory for portal logs. """
_LOG_FILE_NAME: str = 'database_ops.log'
""" Filename for the database operations log. """
_API_LOG_FILE_NAME: str = 'api_requests.log'
""" Filename for the API requests log. """

_LOG_FILE_DIR: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME)
""" Directory containing the database operations and API requests log files. """
_LOG_FILE_PATH: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME, _LOG_FILE_NAME)
""" The location of the database operations log file in the portal's file system-based backing repository. """
_LOG_LOCK_PATH: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME, '.lock')
""" Lock file for advisory interprocess lock to mediate exclusive access to the database operations log. """
_API_LOG_FILE_PATH: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME, _API_LOG_FILE_NAME)
""" The location of the API requests log file in the portal's file system-based backing repository. """
_API_LOG_LOCK_PATH: Path = Path(get_config().workspace_dir, _LOG_DIR_NAME, '.api-lock')
""" Lock file for advisory interprocess lock to mediate exclusive access to the API requests log. """


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


class _LogEntryJSONEncoder(json.JSONEncoder):
    """
    JSONEncoder subclass customized to jsonify any entry written to the database operations log or the API requests log.
    It handles the serialization/deserialization of those object types that standard JSON cannot handle but that can
    appear in a log entry:
     - A DBTable enum.
     - 1D Numpy float array.
     - `datetime.date` or `datetime.datetime` objects.
     - A `bytes` object.
    """
    def default(self, obj):
        if isinstance(obj, DBTable):
            return {'_DBTable': obj.value}
        elif isinstance(obj, np.ndarray) and obj.ndim == 1:
            return {'_nparray_b64': base64.b64encode(obj.tobytes()).decode('utf-8')}
        elif isinstance(obj, date):
            return {'_date_iso': obj.isoformat()}
        elif isinstance(obj, datetime):
            return {'_datetime_iso': obj.isoformat()}
        elif isinstance(obj, bytes):
            return {'_bytes_b64': base64.b64encode(obj).decode('utf-8')}
        return super(_LogEntryJSONEncoder, self).default(obj)

    @staticmethod
    def decoder_hook(dict_obj):
        if isinstance(dict_obj, dict) and (len(dict_obj.keys()) == 1):
            if '_DBTable' in dict_obj:
                return DBTable(dict_obj['_DBTable'])
            elif '_nparray_b64' in dict_obj:
                return np.frombuffer(base64.b64decode(dict_obj['_nparray_b64']))
            elif '_date_iso' in dict_obj:
                return date.fromisoformat(dict_obj['_date_iso'])
            elif '_datetime_iso' in dict_obj:
                return datetime.fromisoformat(dict_obj['_datetime_iso'])
            elif '_bytes_b64' in dict_obj:
                return base64.b64decode(dict_obj['_bytes_b64'])
        return dict_obj


def _ensure_logs_directory_exists() -> None:
    _LOG_FILE_DIR.mkdir(parents=True, exist_ok=True)


def log_file_path() -> Path:
    """ The file system path to the database operations log file in the portal backup repository. """
    return _LOG_FILE_PATH


def _append_log_entry(entry: Dict[str, Any], is_api_log: bool = False) -> None:
    """
    Helper method that appends a new log entry to the database operations log file or the API requests log in the portal
    workspace. It handles the details of JSONifying the entry, converting the resulting JSON to a byte sequence,
    acquiring an interprocess lock on the dedicated log file, and then appending the byte sequence -- preceded by its
    length -- to that file.

    After appending the log entry, it will schedule a backup of the operations log file to portal's backup repository
    in S3 (if needed).

    Args:
        entry: The new entry.
        is_api_log: True to append entry to API request log, else database operations log. Default = False.
    Raises:
        Exception: If an error occurs while serializing the entry to the database operations log file.
    """
    _ensure_logs_directory_exists()
    raw_bytes = json.dumps(entry, cls=_LogEntryJSONEncoder).encode()
    lock_path = _API_LOG_LOCK_PATH if is_api_log else _LOG_LOCK_PATH
    log_path = _API_LOG_FILE_PATH if is_api_log else _LOG_FILE_PATH
    with WithTimeout(lock_path, 1):
        with open(log_path, 'ab') as f:
            f.write(struct.pack('<i', len(raw_bytes)))
            f.write(raw_bytes)
    schedule_log_backup_if_necessary()


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
        # NEVER store user's encrypted password in the log!
        if table_id == DBTable.USER:
            row.pop('password', None)
        _append_log_entry(dict(op='add', table=table_id, row=row))
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
        _append_log_entry(dict(op='delete', table=table_id, restriction=restriction))
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
        # NEVER store user's encrypted password in the log!
        if table_id == DBTable.USER:
            row.pop('password', None)
            if len(row) == 1:
                return None
        _append_log_entry(dict(op='update', table=table_id, row=row))
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
        _append_log_entry(dict(op='mapping', table=table_id, src_pk=src_pk_val, dst_pks=[k for k in map_set]))
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
        _append_log_entry(dict(op='session', username=user, subj_id=subject, date=session_date, suffix=suffix))
    except Exception as err:
        error_msg = f"Failed to post 'session' entry to database update log: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


def log_api_request(route: str, username: str, **kwargs) -> None:
    """
    Log a request to one of the portal's API endpoints, or a request to download the API client-side Python package.
    Only registered portal users have permission to download the package and use it to send API requests to retrieve
    portal data outside the context of a web browser. To protect the provenance of that data, all API requests are
    logged in a dedicated file within the portal workspace.

    The method prepares a dictionary containing the route name, requester's username, the request parameters, and a
    timestamp, then appends that dictionary to the dedicated log file. As a fallback, if the write fails for any reason,
    the API log entry is written to the application message log.

    All request parameters must be JSON-ifiiable. The internal JSONEncoder that processes the log entries does support
    several additional object types not handled by the standard encoder: 1D Numpy arrays, a `date` or `datetime` object,
    a `bytes` object, and a `DBTable` enum.

    Args:
        route: The API endpoint route name
        username: Username of the registered portal user that initiated the API request.
        **kwargs: The request parameters (if any).
    """
    entry = dict(route=route, username=username, ts=datetime.now().isoformat())
    if isinstance(kwargs, dict):
        for k, v in kwargs.items():
            entry[k] = v
    try:
        _append_log_entry(entry, is_api_log=True)
    except Exception as err:
        error_msg = f"Failed to append entry in API requests log: {str(err)}."
        get_application_logger().error(error_msg, exc_info=True)
        get_application_logger().info(f"Unlogged API request: {str(entry)}")


def schedule_log_backup_if_necessary(soon: bool = False) -> None:
    """
    Schedule a background job to push copies of the database operations log and API requests log from the portal
    workspace to the backing repository.

    The two dedicated log files are located in the portal workspace directory, on a file system mount accessible to the
    backend server process. The database operations log contains the entire history of operations on the portal database
    and is essential if we ever need to reconstruct the database. The API requests log keeps a record of all requests
    received by the portal's API endpoints, as well as any request to download the clientside Python package by which
    users can programmatically access those endpoints; this log is important for data provenance reasons.

    Both are backed up regularly to the portal's backing repository, which also stores the ZIP archives for experiment
    sessions that have been committed to the database. That repository is maintained in an Amazon S3 bucket provisioned
    by the Lisberger lab.

    Call this method to schedule a log backup job. If a job is already scheduled, no action is taken.

    Args:
        soon: If True, the backup is scheduled to take place one minute from "now". Otherwise, it is scheduled to
            happen in 24 hours. Default = False. If either log has never been backed up, this argument is ignored and a
            backup is scheduled for 1 minute from now.
    """
    job_queue = Queue(connection=get_config().redis_conn)
    if len(job_queue.scheduled_job_registry) == 0:
        if (not soon) and (0 == repo.file_size(f"/{_LOG_DIR_NAME}/{_LOG_FILE_NAME}")):
            soon = True
        if (not soon) and (0 == repo.file_size(f"/{_LOG_DIR_NAME}/{_API_LOG_FILE_NAME}")):
            soon = True
        delta = timedelta(minutes=1) if soon else timedelta(hours=24)
        job_queue.enqueue_in(time_delta=delta, func=backup_log_to_repo)
        get_application_logger().info(f"Scheduled logs backup {'1 min' if soon else '24 hr'} from now.")


def backup_log_to_repo() -> None:
    """
    Push a copy of the current database operations log and the current API requests log in the portal workspace to the
    backing repository on S3.

    This method is intended to be called on a background process independent from the Dash/Flask backend server.

    If the current size of either log in the portal workspace exceeds the size of its backup copy in the portal
    repository, the method copies the log to a temporary file (in case other processes are updating the log file
    at the same time), then uploads that temporary file to the repository, replacing the old backup copy of the log.
    """
    # we need to get the current size N of each log file while holding the corresponding interprocess lock. After
    # releasing the lock, another server replica could append entries to a log file, but that's OK. We only copy what
    # was there when we checked.
    _ensure_logs_directory_exists()
    for lock_path, log_path in [(_LOG_LOCK_PATH, _LOG_FILE_PATH), (_API_LOG_LOCK_PATH, _API_LOG_FILE_PATH)]:
        curr_size = 0
        try:
            with WithTimeout(lock_path, 1):
                curr_size = log_path.stat().st_size
        except Exception:
            pass

        key = f"/{_LOG_DIR_NAME}/{log_path.name}"
        if curr_size > repo.file_size(key):
            tmp_file_path = Path(_LOG_FILE_DIR, f"tmp_{str(uuid.uuid4())}.log")
            try:
                with open(log_path, 'rb') as src, open(tmp_file_path, 'wb') as dst:
                    data = src.read(curr_size)
                    dst.write(data)
                if not repo.upload_file(tmp_file_path, key):
                    get_application_logger().error(
                        f"Failed to backup {log_path.name} to portal repository; check system logs.")
                else:
                    get_application_logger().info(f"Backed up {log_path.name} "
                                                  f"({size_with_units(curr_size)}) to portal repository.")
            except Exception:
                get_application_logger().error(f"Internal error while backing up {log_path.name}.", exc_info=True)
            finally:
                try:
                    tmp_file_path.unlink(missing_ok=True)
                except Exception:
                    pass


def read_log_entries(is_api_log: bool = False) -> List[Dict[str, Any]]:
    """
    Read in all entries from one of two dedicated log files maintained in the portal workspace: the database operations
    history and the API requests log.

    It is safe to call this method when the portal application is online.

    Args:
        is_api_log: True to read the API requests log, False for the database operations log. Default = False.
    Returns:
        List of all entries read from the log file.
    Raises:
        OSError: If log file not found in portal workspace directory, or if any error occurs while reading the file.
        EOFError: If end-of-file is reached in the middle of a log entry.
        JSONDecodError: If an error occurs while parsing any entry.
    """
    log_path = _API_LOG_FILE_PATH if is_api_log else _LOG_FILE_PATH
    lock_path = _API_LOG_LOCK_PATH if is_api_log else _LOG_LOCK_PATH
    if not log_path.is_file():
        raise Exception(f"No {'API requests' if is_api_log else 'database operations'} log found at {str(log_path)}")

    # get current size of log file while we hold the access lock...
    curr_size = 0
    try:
        with WithTimeout(lock_path, 1):
            curr_size = log_path.stat().st_size
    except Exception:
        pass
    if curr_size <= 0:
        return []

    # then copy that number of bytes to a temp file and read entries from temp file. The copy should not be affected by
    # a simultaneous append by another process...
    entries: List[Dict[str, Any]] = list()
    tmp_file_path = Path(_LOG_FILE_DIR, f"tmp_{str(uuid.uuid4())}.log")
    try:
        with open(log_path, 'rb') as src, open(tmp_file_path, 'wb') as dst:
            data = src.read(curr_size)
            dst.write(data)

        int_sz = struct.calcsize('<i')
        with open(tmp_file_path, 'rb') as f:
            while True:
                size_bytes = f.read(int_sz)
                if len(size_bytes) == 0:
                    break
                elif len(size_bytes) != int_sz:
                    raise EOFError('Hit EOF in the middle of a log entry')
                entry_size, = struct.unpack('<i', size_bytes)
                raw_entry = f.read(entry_size)
                if len(raw_entry) != entry_size:
                    raise EOFError('Hit EOF in the middle of a log entry')
                entry = json.loads(raw_entry, object_hook=_LogEntryJSONEncoder.decoder_hook)
                entries.append(entry)

        return entries
    finally:
        try:
            tmp_file_path.unlink(missing_ok=True)
        except Exception:
            pass


def dump_log(out: Optional[TextIO] = sys.stdout, is_api_log: bool = False) -> None:
    """
    Dump the entire contents of the database operations log or the API requests log to a text file stream.

    NOTE: THIS IS AN ADMINISTRATIVE FUNCTION that should never be called when the portal application is online. The
    method does NOT acquire an advisory interprocess lock before reading the log file.

    Args:
        out: The target text stream. Defaults to STDOUT.
        is_api_log: True to dump the API requests log, False for the database operations log. Default = False.
    """
    entries: List[Dict[str, Any]]
    try:
        entries = read_log_entries(is_api_log)
    except Exception as e:
        err_msg = f"Error while dumping {'API requests' if is_api_log else 'database operations'} log: {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
        print(f"=====> {err_msg}", file=out, flush=True)
        return

    log_name = "API Requests" if is_api_log else "Database Operations"
    print(f"\n****** {log_name} log history ******\n", file=out, flush=True)
    for i, entry in enumerate(entries):
        print(f"{i:04}: {entry}", file=out)
    print(f"\n****** END {log_name} log history ******\n", file=out, flush=True)
