"""
manager.py: Manage access to the Lisberger lab database and backing repository.

For now, I've only added code managing a log file that tracks all changes to the lab database so that the database
can be regenerated upon a catastrophic failure. However, I may want to refactor things so that it includes everything
in session_builder.py and table_views.py.

The database log file is a Python pickle file that contains a chronological sequence of database entries logging all
changes to the database since inception. Then, to rebuild the database from scratch, we would simply process each entry
in the file one at a time.

For any addition to a manual table, the entry identifies the table and includes the "row" added (as a dictionary). For
any deletion from a table, the entry is similar, except the row need only be identified by the primary key. When an
experiment session is committed, many entries are added to the database, and the session archive and a pickle file
containing pre-processed information are saved to the backing repository. Rather than log every database entry, a
session commit is marked by a simple entry containing the information needed to locate the files in the backing
repository. All the information needed to re-commit the experiment session is in those files -- including session
metadata, trial protocols, neural unit data, and the original recorded data files.

The lab database schema also includes some "mapping" or cross-reference tables such as BrainAreaNeuronType. We restrict
the nature of these tables and the two tables they "associate" -- see MappingView in table_views.py: the associated
tables' primary keys are both single-attribute, auto-incrementing integer keys. The mapping table has a primary key
consisting of the those two foreign keys and nothing else, and the table has no non-primary attributes. A log entry
recording an update to a mapping table consists of the name of the mapping table, an integer identifying an entity in
the source table, and a set of integers (possibly empty) identifying entities in the destination table that are
associated with the entity in the source table.

Summary of the log entry types:
    1) Add row to manual-entry table: {'op': 'add', 'table': str, 'row': Dict}
    2) Delete row from manual-entry table: {'op': 'delete', 'table': str, 'row': Dict}
    3) Mapping table update: {'op': 'mapping', 'table': str, 'src_pk': int, 'dst_pks': Set[int]}
    4) Session commit: {'op': 'session', 'username': str, 'subj_id': str, 'date': 'YYYY-MM-DD', 'suffix': int}

@author: sruffner
@created: 03mar2021
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import os
import pickle
from pathlib import Path
from typing import Optional, Dict, Any, Set
import threading


class DataBaseManager:
    _singleton: Optional[DataBaseManager] = None

    def __new__(cls):
        """
        Constructs the singleton DataBaseManager instance if it does not yet exist; else returns that singleton.
        """
        if cls._singleton is None:
            cls._singleton = super(DataBaseManager, cls).__new__(cls)
            cls._singleton.__init__()
        return cls._singleton

    def __init__(self):
        self._log_lock: threading.Lock = threading.Lock()
        """ Lock object guarding access to the database updates log file. """

    @staticmethod
    def _log_file_path() -> Path:
        """ Get file system path for the database updates log file. """
        return Path(os.environ['DJDEV_ROOT_REPO'], 'logs', 'update_log')

    @staticmethod
    def _ensure_logs_directory_exists() -> None:
        log_dir = Path(os.environ['DJDEV_ROOT_REPO'], 'logs')
        log_dir.mkdir(parents=True, exist_ok=True)

    def log_add_table_row(self, table_name: str, row: Dict[str, Any]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'add', 'table': table_name, 'row': row}, file)
        except Exception as err:
            error_msg = f"Failed to post 'add' entry to database update log: {str(err)}"
        return error_msg

    def log_delete_table_row(self, table_name: str, row: Dict[str, Any]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'delete', 'table': table_name, 'row': row}, file)
        except Exception as err:
            error_msg = f"Failed to post 'delete' entry to database update log: {str(err)}"
        return error_msg

    def log_mapping_table_update(self, table_name: str, src_pk_value: int, dst_pk_values: Set[int]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'mapping', 'table': table_name, 'src_pk': src_pk_value,
                                 'dst_pks': dst_pk_values}, file)
        except Exception as err:
            error_msg = f"Failed to post 'mapping' entry to database update log: {str(err)}"
        return error_msg

    def log_session_commit(self, user: str, subject: str, session_date: str, suffix: int) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'session', 'username': user, 'subj_id': subject, 'date': session_date,
                                 'suffix': suffix}, file)
        except Exception as err:
            error_msg = f"Failed to post 'session' entry to database update log: {str(err)}"
        return error_msg
