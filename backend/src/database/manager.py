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
    1) Add row to manual-entry table: {'op': 'add', 'table': DBTable, 'row': Dict}
    2) Delete from manual-entry table: {'op': 'delete', 'table': DBTable, 'restriction': Dict}
    3) Mapping table update: {'op': 'mapping', 'table': DBTable, 'map_rows': Dict}
    4) Session commit: {'op': 'session', 'username': str, 'subj_id': str, 'date': 'YYYY-MM-DD', 'suffix': int}

@author: sruffner
@created: 03mar2021
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import os
import pickle
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from queue import Queue
from typing import Optional, Dict, Any, Set, Union, List, IO, Tuple
import threading
import time
import shutil
import uuid
import zipfile
from json import JSONDecoder
import numpy as np
import scipy.signal
import datajoint as dj

from common import json_parse, DocEnum, check_date
import database.maestro as maestro
import database.PL2 as PL2
import database.sgl_schema as sgl


class DBTable(DocEnum):
    """
    An enumeration of all tables (except part tables) in the Lisberger laboratory database schema (sgl_schema.py).
    """
    USER = 1, "Table of laboratory members"
    SUBJECT = 2, "Table of experiment subjects"
    IMPLANT = 3, "Table of implants on experiment subjects"
    RIG = 4, "Table of experiment rigs"
    BRAIN_AREA = 5, "Table of brain regions"
    NEURON_TYPE = 6, "Table of neuron types"
    BRAIN_AREA_TO_NEURON_TYPE = 7, "Cross-reference table: Brain region to neuron type"
    STUDY = 8, "Table of research projects/studies"
    KEYWORD = 9, "Table of research keywords"
    PUB = 10, "Table of research publications"
    STUDY_TO_KEY = 11, "Cross-reference table: Research study to keyword"
    STUDY_TO_PUB = 12, "Cross-reference table: Research study to publication"
    SESSION = 13, "Table of experiment sessions"
    SESSION_EPHYS = 13, "Part table of electrophysiology metadata for experiment sessions"
    TRIAL_PROTOCOL = 14, "Table of trial protocol definitions"
    TRIAL = 15, "Table of individual trial response data"

    def is_mapping_table(self) -> bool:
        """ Return True for a cross-reference table. """
        return self in (DBTable.BRAIN_AREA_TO_NEURON_TYPE, DBTable.STUDY_TO_KEY, DBTable.STUDY_TO_PUB)


AttributeValue = Union[str, int, float, bool, date, np.ndarray, bytes]
""" Database table attribute value type - a union of all the input types supported by the backend server. """


class DataBaseManager:
    _singleton: Optional[DataBaseManager] = None
    """ The singleton instance of the DataBaseManager object. """
    _MAX_WORKERS: int = 8
    """ Maximum number of running session commit jobs supported. """

    def __new__(cls):
        """
        Constructs the singleton DataBaseManager instance if it does not yet exist; else returns that singleton.
        """
        if cls._singleton is None:
            cls._singleton = super(DataBaseManager, cls).__new__(cls)
            cls._singleton.__init__()
        return cls._singleton

    def __init__(self):
        # must do it this way to ensure we don't overwrite the class members!
        if not hasattr(self, 'running_tasks'):
            self.running_tasks: Dict[str, ProcessArchiveThread] = dict()
            """ Session commit jobs currently in progress on backend server, keyed by unique task ID. """
            self.task_list_lock: threading.Lock = threading.Lock()
            """ Lock object guarding access to the set of running session commit jobs. """
            self._log_lock: threading.Lock = threading.Lock()
            """ Lock object guarding access to the database updates log file. """
            self._table_class_map = {
                DBTable.USER: sgl.User(),
                DBTable.SUBJECT: sgl.Subject(),
                DBTable.IMPLANT: sgl.SubjectImplant(),
                DBTable.RIG: sgl.Rig(),
                DBTable.BRAIN_AREA: sgl.BrainArea(),
                DBTable.NEURON_TYPE: sgl.NeuronType(),
                DBTable.BRAIN_AREA_TO_NEURON_TYPE: sgl.BrainAreaNeuronType(),
                DBTable.STUDY: sgl.Study(),
                DBTable.KEYWORD: sgl.Keyword(),
                DBTable.PUB: sgl.Publication(),
                DBTable.STUDY_TO_KEY: sgl.StudyKeyword(),
                DBTable.STUDY_TO_PUB: sgl.StudyPublication(),
                DBTable.SESSION: sgl.Session(),
                DBTable.SESSION_EPHYS: sgl.Session.EPhys(),
                DBTable.TRIAL_PROTOCOL: sgl.TrialProtocol(),
                DBTable.TRIAL: sgl.Trial()
            }
            """ Maps enumerated database table ID to the corresponding DataJoint table object."""

    def on_startup(self) -> Optional[str]:
        """
        Perform any operations that must take place when the backend server starts up.

        Currently, this method will 'seed' the database if it is empty and a JSON seed file is found. Otherwise, it does
        nothing.

        Returns:
            None if successful, else an error description. On failure, the database is unusable and the backend
            server should cease operation.
        """
        return self._seed_database_if_empty()

    def _seed_database_if_empty(self) -> Optional[str]:
        """
        Seed the lab database if it is empty and a JSON seed file 'seed_data.txt' is available in the backend's code
        base at './assets/seed_data.txt'. The database is assumed to be empty if the "Users" table is empty.

        The seed file contains a sequence of JSON objects separated by whitespace (a linefeed or CRLF pair). Each object
        defines an entity to be added to the lab database. It has the following format:
            { "table": "<table name>", "entry": {<entry definition>}}
        The <table name> must exactly match one of nine manual tables in the SGL database schema: "User", "Subject",
        "SubjectImplant", "Rig", "BrainArea", "NeuronType", "Study", "Publication", and "Keyword".
        The <entry definition> is the set of attribute name-value pairs defining the new table "row". For example, to
        add a new user:
            { "table": "User", "entry": {"username": "sruffner", "full_name": "Scott A Ruffner",
            "contact_email": "sruffner@srscicomp.com", "role": "Administrator"}

        THIS METHOD IS INTENDED ONLY FOR USE DURING DEVELOPMENT, so that we can populate some the manual tables with
        some entries after dropping and recreating the database.

        The entries listed in the seed file are assumed to be presented in a valid order. For example, a subject is
        added to the Subject table before any implants for that subject are added to the SubjectImplant table. Also,
        each entry's attribute name-value pairs are assumed to be valid. If not, the add operation may fail.

        Returns:
            None if successful (or database was not empty, or no seed file found); else an error description.
        """
        if self.num_table_rows(DBTable.USER) > 0:
            return None

        name_to_table_id = {
            "User": DBTable.USER, "Subject": DBTable.SUBJECT, "SubjectImplant": DBTable.IMPLANT,
            "Rig": DBTable.RIG, "BrainArea": DBTable.BRAIN_AREA, "NeuronType": DBTable.NEURON_TYPE,
            "Study": DBTable.STUDY, "Publication": DBTable.PUB, "Keyword": DBTable.KEYWORD
        }
        json_decoder = JSONDecoder()
        try:
            with open('./assets/seed_data.txt', 'r') as file_obj:
                for add_dict in json_parse(file_obj, json_decoder):
                    if isinstance(add_dict, dict) and ("table" in add_dict) and ("entry" in add_dict) \
                            and (add_dict["table"] in name_to_table_id):
                        err_msg = self.insert_into_table(name_to_table_id[add_dict["table"]], add_dict["entry"])
                        if err_msg:
                            raise Exception(err_msg)
        except Exception as e:
            return f"Failed to seed database: {e}"
        return None

    @staticmethod
    def _log_file_path() -> Path:
        """ Get file system path for the database updates log file. """
        return Path(os.environ['DJDEV_ROOT_REPO'], 'logs', 'update_log')

    @staticmethod
    def _ensure_logs_directory_exists() -> None:
        log_dir = Path(os.environ['DJDEV_ROOT_REPO'], 'logs')
        log_dir.mkdir(parents=True, exist_ok=True)

    def _log_add_table_row(self, table_id: DBTable, row: Dict[str, AttributeValue]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'add', 'table': table_id, 'row': row}, file)
        except Exception as err:
            error_msg = f"Failed to post 'add' entry to database update log: {str(err)}"
        return error_msg

    def _log_delete_from_table(self, table_id: DBTable,
                               restriction: Optional[Dict[str, AttributeValue]]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'delete', 'table': table_id, 'restriction': restriction}, file)
        except Exception as err:
            error_msg = f"Failed to post 'delete' entry to database update log: {str(err)}"
        return error_msg

    def _log_mapping_table_update(self, table_id: DBTable, map_rows: List[Dict[str, int]]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'mapping', 'table': table_id, 'map_rows': map_rows}, file)
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

    def num_table_rows(self, table_id: DBTable, restriction: Optional[Dict[str, AttributeValue]] = None) -> int:
        """
        Get the current number of entities in the specified database table.

        Args:
            table_id: ID of database table.
            restriction: If not None, this dictionary restricts the query to those rows of the table that contain the
                attribute ID-value pairs specified in the dictionary.

        Returns:
            Number of rows in table. Returns 0 if unable to access database, if specified table does not exist, or if
            a restriction condition is specified that includes an attribute not defined on the table (or if the table
            has no rows satisfying that condition).
        """
        try:
            table: dj.Table = self._table_class_map[table_id]
            query = (table & restriction) if restriction else table
            n = len(query)
        except Exception:
            n = 0
        return n

    def attribute_exists(self, table_id: DBTable, attr_id: str, attr_value: AttributeValue) -> bool:
        """
        Does the specified attribute have the specified value in any row in the specified table?
        Args:
            table_id: ID of database table.
            attr_id: Attribute ID.
            attr_value: Attribute value.

        Returns:
            False if attribute value not found in table (or if an error occurs).
        """
        existing_values = self.fetch_attribute_values(table_id, attr_id)
        return attr_value in existing_values

    def fetch_attribute_values(self, table_id: DBTable, attr_id: str,
                               restriction: Optional[Dict[str, AttributeValue]] = None) -> List[AttributeValue]:
        """
        Fetch all existing values of the specified attribute within the specified database table, optionally restricted
        to a defined subset of the table's rows.

        Args:
            table_id: ID of database table
            attr_id:  Attribute ID.
            restriction: The attribute ID-value pairs in this dictionary define a restricted subset of rows within the
                table from which to retrieve the attribute's value. Default value is None -- in which case the attribute
                value is retrieved for every row in the table.

        Returns:
            List of values found for the specified attribute (one value per row in the table, or table subset). The list
                is not sorted. Returns an empty list if specified table does not exist or if a database error occurs.
        """
        try:
            table: dj.Table = self._table_class_map[table_id]
            query = (table & restriction) if restriction else table
            attr_values = query.fetch(attr_id)
        except Exception:
            attr_values = []
        return attr_values

    def fetch_proj(self, table_id: DBTable, attributes: List[str]) -> List[Dict[str, AttributeValue]]:
        """
        Fetch selected attributes (aka, columns) from the specified table.

        Args:
            table_id: ID of database table.
            attributes: Tuple of attribute IDs identifying the subset of table attributes to fetch. The table's
                primary-key attributes will be included in the result, even if they are omitted from this tuple.

        Returns:
            The requested table contents. Each element in the list is a dictionary of attribute ID-value pairs. Each
                dictionary will include only primary key attributes plus any other attributes identified in the
                'attributes' argument. Returns an empty list if the table is empty or a database error occurs.
        """
        try:
            table: dj.Table = self._table_class_map[table_id]
            rows = table.proj(*attributes).fetch(as_dict=True)
        except Exception:
            rows = []
        return rows

    def fetch_rows(self, table_id: DBTable, restriction: Optional[Dict[str, AttributeValue]] = None) -> \
            List[Dict[str, AttributeValue]]:
        """
        Fetch all or a subset of the rows in the specified database table.

        Args:
            table_id: ID of the database table.
            restriction: The attribute ID-value pairs in this dictionary specify a restriction that any row returned in
                the result must satisfy. Default value is None -- thereby retrieving all rows in the table.

        Returns:
            The requested rows. Each element in the list is a dictionary of attribute ID-value pairs representing a
                single table row. Returns an empty list if the table is empty, has no rows satisfying the restriction
                (if any), or a database error occurs.
        """
        try:
            table: dj.Table = self._table_class_map[table_id]
            query = (table & restriction) if restriction else table
            rows = query.fetch(as_dict=True)
        except Exception:
            rows = []
        return rows

    def insert_into_table(self, table_id: DBTable, row: Dict[str, AttributeValue], log: bool = True) -> Optional[str]:
        """
        Insert an entry into a specified table in the laboratory database, and log the change in the repository
        database updates log. If the log update fails after insertion, the insertion is rolled back to maintain
        consistency between the database and the backup repository.

        Args:
            table_id: ID of database table.
            row: The new entry.
            log: If True, the insertion is logged in the backup updates log. Default = True.

        Returns:
            None if successful; else a user-facing description of the error (missing attribute, bad attribute value,
            entry already exists, database error).
        """
        if not (table_id in self._table_class_map):
            return f"Unrecognized database table ID: {str(table_id)}"
        try:
            table: dj.Table = self._table_class_map[table_id]
            with table.connection.transaction:
                table.insert1(row, replace=False)
                if log:
                    err_msg = self._log_add_table_row(table_id, row)
                    if err_msg:
                        raise Exception(err_msg)
        except Exception as e:
            return f"Insert failed: table={str(table_id)}, value={row} ===> {str(e)}"
        return None

    def delete_from_table(self, table_id: DBTable, restriction: Optional[Dict[str, AttributeValue]] = None,
                          log: bool = True) -> Optional[str]:
        """
        Delete one or more rows from a specified table in the laboratory database, and log the change in the repository
        database updates log. If the log update fails after deletion, the deletion is rolled back to maintain
        consistency between the database and the backup repository.

        Args:
            table_id: ID of database table
            restriction:  A dictionary of attribute ID-value pairs that describes the row or rows to delete. If this is
                None, the entire contents of the table are deleted!
            log: If True, the deletion is logged in the backup updates log. Default = True.

        Returns:
            None if successful; else a user-facing description of the error (bad table or attribute ID, database error).
        """
        if not (table_id in self._table_class_map):
            return f"Unrecognized database table ID: {str(table_id)}"
        try:
            table: dj.Table = self._table_class_map[table_id]
            with table.connection.transaction:
                query = (table & restriction) if restriction else table
                query.delete(verbose=False)
                if log:
                    err_msg = self._log_delete_from_table(table_id, restriction)
                    if err_msg:
                        raise Exception(err_msg)
        except Exception as e:
            return f"Delete failed: table={str(table_id)}, restrict={restriction} ===> {str(e)}"
        return None

    def update_xref_table(self, map_table_id: DBTable, src_pk: str, src_pk_val: int,
                          dst_pk: str, map_set: Set[int], log: bool = True) -> Optional[str]:
        """
        Update an associative mapping stored in a cross-reference table in the laboratory database, and log the change
        in the repository database updates log. If the log update fails, any changes are rolled back to maintain
        consistency between the database and the backup repository.

        Args:
            map_table_id: ID of the cross-reference table.
            src_pk: Source table's primary key attribute ID
            src_pk_val: Integer value identifying a row in the source table for the cross-reference. Must exist in
                database or operation will fail.
            dst_pk: Destination table's primary key attribute ID
            map_set: Set of integer values identifying all rows in the destination table that map to the specified
                source entity. All must exist in the destination table or the operation will fail.
            log: If True, the change is logged in the backup updates log. Default = True.

        Returns:
            None if successful; else a user-facing description of the error.
        """
        error_msg = None
        try:
            if not (map_table_id in self._table_class_map):
                raise Exception(f"Unrecognized database table ID: {str(map_table_id)}")
            if not map_table_id.is_mapping_table():
                raise Exception(f"Table is not a cross-reference table!: {str(map_table_id)}")
            map_table: dj.Table = self._table_class_map[map_table_id]
            xref_rows = [{src_pk: src_pk_val, dst_pk: value} for value in map_set]
            with map_table.connection.transaction:
                (map_table & {src_pk: src_pk_val}).delete(verbose=False)
                map_table.insert(xref_rows)
                if log:
                    err_msg = self._log_mapping_table_update(map_table_id, xref_rows)
                    if err_msg:
                        raise Exception(err_msg)
        except Exception as err:
            error_msg = f"Failed to update cross-reference table {str(map_table_id)}: {str(err)}"
        return error_msg

    def initiate_session_commit(self) -> Tuple[bool, str]:
        """
        Initiate a session commit task on the lab database server. The method creates the temporary staging directory
        for the task, spawns a background thread to perform the work, and returns a unique ID assigned to the task. The
        client must supply this task ID in all future requests involving the commit task.

        Returns:
            A 2-element tuple. The first element is True only if a new commit task was successfully started on the
                server. If so, the second element is the assigned task ID; if not, it is a brief user-facing error
                description (too many commits in progress, unable to create staging directory, etc).
        """
        with self.task_list_lock:
            if len(self.running_tasks) >= DataBaseManager._MAX_WORKERS:
                return False, "Server is too busy; try again later"

            task_id = f"session-{str(uuid.uuid4())}"
            staging_dir = DataBaseManager.get_staging_directory_for(task_id)
            try:
                staging_dir.mkdir(parents=True, exist_ok=False)
            except Exception as err:
                return False, f"Failed to create staging directory on server: {str(err)}"

            worker = ProcessArchiveThread(task_id)
            self.running_tasks[task_id] = worker
            worker.start()
            return True, task_id

    @staticmethod
    def get_staging_directory_for(task_id: str) -> Path:
        """ Construct the file system path of the temporary staging directory for an in-progress session commit task
        with the task ID specified. """
        return Path(os.environ['DJDEV_ROOT_REPO'], 'staging', task_id)

    @staticmethod
    def get_repo_directory_for(username: str) -> Path:
        """ Construct the file system path of the directory in the lab repository in which session data committed by the
        specified user are stored. """
        return Path(os.environ['DJDEV_ROOT_REPO'], username)

    def get_commit_task_stage(self, task_id: str) -> Tuple[int, int]:
        """
        Get the current stage for a session commit task in progress on the server.

        Args:
            task_id: The commit task identifier.

        Returns:
            A 2-tuple (stage, substage) listing the current task stage and substage. There are 4 stages: 1 = no commit
                task exists for ID specified; 2 = uploading and pre-processing ZIP archive; 3 = paused waiting on user
                confirmation of pre-processed results; 4 = committing session to database. The substage is applicable
                only in stage 2 and may have the following values: 0 = waiting for ZIP upload to start; 1 = upload in
                progress; 2 = pre-processing ZIP archive. In all other stages, substage is always 0.
        """
        stage: int = 1
        substage: int = 0
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                stage = worker.stage
        if stage == 2:
            staging_dir = DataBaseManager.get_staging_directory_for(task_id)
            if staging_dir.is_dir():
                for child in staging_dir.iterdir():
                    if child.is_dir() and (len(child.name) > 3) and (child.name[-3:].lower() == 'zip'):
                        substage = 1
                        break
                    if child.is_file() and (len(child.name) > 4) and (child.name[-4:].lower() == '.zip'):
                        substage = 2
                        break
        return stage, substage

    def progress_update(self, task_id: str) -> Tuple[int, str, Optional[bool]]:
        """
        Retrieve any new progress message for the specified session commit task. Progress messages are regularly
        updated in stages 2 and 4 of a commit task. In stage 3, the background worker is paused while the user reviews
        results on the client-side front-end, so this method is not applicable in that stage.

        Args:
            task_id: The commit task identifier.

        Returns:
            A 3-tuple listing the current stage for the specified commit task, the most recent progress message from
                that task, and a result indicator: None if task still in progress, False if task terminated on an error,
                and True if task completed successfully (end of stage 4). If task_id does not identify an in-progress
                commit task, returns (1, "", None). If the commit task has failed, the last entry in the message list
                will be an error description.
        """
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                while worker.msg_q.qsize() > 0:
                    worker.latest_message = worker.msg_q.get_nowait()
                if worker.is_alive() and worker.latest_message.startswith("Error"):
                    worker.join()
                return worker.stage, worker.latest_message, worker.result
            else:
                return 1, "", None

    def get_session_info(self, task_id: str) -> Optional[Dict[str, AttributeValue]]:
        """
        Get the session information that will be saved in the Session table when the experiment session is eventually
        committed to the lab database. This information is available ONLY during stage 3 of the commit workflow -- after
        pre-processing and before the final commit stage begins. The session information is initialized with reasonable
        attribute values during pre-processing. On the client side, the user is expected to review and correct it in
        stage 3.

        Args:
            task_id: The commit task identifier.

        Returns:
            A dictionary containing the attribute values for a proposed session table entry representing the experiment
                session to be committed, keyed by the attribute IDs. Returns None if the task_id does not
                identify an in-progress commit task, or that task is not currently in stage 3.
        """
        out: Optional[Dict[str, AttributeValue]] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    out = deepcopy(worker.session_info)
        return out

    def get_ephys_info(self, task_id: str) -> Optional[Dict[str, AttributeValue]]:
        """
        Get the information about the experiment's electrophysiological recording that is saved in the Session.EPhys
        part table when the experiment session is eventually committed to the lab database. The information is available
        ONLY during stage 3 of the commit workflow -- after pre-processing and before the final commit stage begins. The
        information is initialized with reasonable attribute values during pre-processing. On the client side, the user
        is expected to review and correct it in stage 3.

        Args:
            task_id: The commit task identifier.

        Returns:
             A dictionary containing the attribute values for a proposed Session.EPhys table entry for the experiment
                session to be committed, keyed by the Session.EPhys attribute IDs. Returns None if the task_id does not
                identify an in-progress commit task, if that task is not currently in stage 3, or if the experiment
                did not include electrophysiological recordings.
        """
        out: Optional[Dict[str, AttributeValue]] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if (worker.stage == 3) and (worker.ephys_info is not None):
                    out = deepcopy(worker.ephys_info)
        return out

    def get_trial_protocol_paths(self, task_id: str) -> Optional[Dict[str, str]]:
        """
        Get the path names and md5 digests for all trial protocols culled during pre-processing of the session data ZIP
        archive. This information is available ONLY during stage 3 of the commit workflow -- after pre-processing and
        before the final commit stage begins. The returned list is intended for display in a dropdown-style web
        component so that the end-user can select a particular protocol for display.

        Args:
            task_id: The commit task identifier.

        Returns:
            A dictionary in which the keys are the md5 digests of the trial protocols and the values are the
                corresponding protocol path names. Each path name is the concatenation of the trial set name (if
                available), subset name (if available), and trial name for the protocol (using '/' as a path separator).
                The dictionary items are sorted in ascending order by pathname. Returns None if the task_id does not
                identify an in-progress commit task, or that task is not currently in stage 3.
        """
        out: Optional[Dict[str, str]] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    out = dict()
                    for protocol in worker.protocols:
                        out[protocol.md5_digest] = protocol.trial.path_name()

        # sort alphabetically by protocol path name (the values of the dictionary
        if out:
            sorted_tuples = sorted(out.items(), key=lambda item: item[1])
            out = {k: v for k, v in sorted_tuples}
        return out

    def get_trial_protocol(self, task_id: str, md5_digest: str) -> Optional[maestro.Protocol]:
        """
        Get the full definition of a trial protocol culled during pre-processing of the session data ZIP archive. This
        information is available ONLY during stage 3 of the commit workflow -- after pre-processing and before the final
        commit stage begins.

        This method, in concert with get_trial_protocol_paths(), provides a mechanism by which the client front-end can
        present a user interface for reviewing the trial protocols.

        Args:
            task_id: The commit task identifier.
            md5_digest: The MD5 digest uniquely identifying the protocol requested.

        Returns:
            The requested trial protocol object. Returns None if the task_id does not identify an in-progress commit
                task, if that task is not currently in stage 3, or if the md5_digest does not identify one of the trial
                protocols found in the pre-processing step.
        """
        out: Optional[maestro.Protocol] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    for protocol in worker.protocols:
                        if protocol.md5_digest == md5_digest:
                            out = protocol
                            break
        return out

    def get_num_neural_units(self, task_id: str) -> Optional[int]:
        """
        Get the number of neural units identified during pre-processing of the session data ZIP archive. This
        information is available ONLY during stage 3 of the commit workflow -- after pre-processing and before the final
        commit stage begins.

        Args:
            task_id: The commit task identifier.

        Returns:
            The number of neural units found in the session data archive. Returns None if the task_id does not identify
            an in-progress commit task, or if that task is not currently in stage 3.
        """
        out: Optional[int] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    out = len(worker.units)
        return out

    def get_neural_unit_metrics(self, task_id: str, index: int) -> Optional[OmniplexUnit]:
        """
        Get the metrics for a neural unit identified during pre-processing of the session data ZIP archive. This
        information is available ONLY during stage 3 of the commit workflow -- after pre-processing and before the final
        commit stage begins.

        This method, in concert with get_num_neural_units(), provides a mechanism by which the client front-end can
        present a user interface for reviewing the neural units found during pre-processing.

        Args:
            task_id: The commit task identifier.
            index: The zero-based index of the neural unit requested.

        Returns:
            The requested neural unit. Returns None if the task_id does not identify an in-progress commit
                task, if that task is not currently in stage 3, or if the unit index is invalid.
        """
        out: Optional[OmniplexUnit] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if (worker.stage == 3) and (index >= 0) and (index < len(worker.units)):
                    out = worker.units[index]
        return out

    def set_neural_unit_type(self, task_id: str, index: int, neuron_type: int) -> bool:
        """
        Update the neuron type ID assigned to a neural unit identified during pre-processing of the session data
        archive. During stage 3 of the session commit workflow, the user (via the client front-end) has the opportunity
        to specify the neuron type for each identified unit. The method has no effect in any other stage.

        Args:
            task_id: The commit task identifier.
            index: The zero-based index of the neural unit requested.
            neuron_type: The neuron type ID. This should identify an existing entry in the database's NeuronType table,
                but it is not checked until the session is actually committed to the database in stage 4

        Returns:
            True if successful; False if the task_id does not identify an in-progress commit task, if that task is not
                currently in stage 3, or if the unit index is invalid.
        """
        ok = False
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if (worker.stage == 3) and (index >= 0) and (index < len(worker.units)):
                    worker.units[index].neuron_type = neuron_type
                    ok = True
        return ok

    def start_commit(self, task_id: str, session_info: Dict[str, AttributeValue],
                     ephys_info: Optional[Dict[str, AttributeValue]]) -> Optional[str]:
        """
        Start the final stage in the session commit workflow, actually committing the session data to the lab database.
        This method should only be called in stage 3, while the worker is paused waiting for the user to review the
        results of the pre-processing stage and make corrections/additions to the session metadata.

        If the supplied session metadata is valid, the background worker is signaled to complete the commit.

        Args:
            task_id: The commit task identifier.
            session_info: A dictionary containing the attribute values for a proposed Session table entry representing
                the experiment session to be committed, keyed by the Session attribute IDs.
            ephys_info: A dictionary containing the attribute values for a proposed Session.EPhys table entry for the
                experiment session to be committed, keyed by the Session.EPhys attribute IDs. None if the experiment
                session is behavioral-only (no electrophysiology).

        Returns:
            None if successful, else a human-facing error description: Bad task ID, invalid session or ephys info.
        """
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    # validate session and, if applicable, electrophysiology metadata
                    if worker.ephys_info and not ephys_info:
                        error_msg = "Missing electrophysiology metadata for session."
                    else:
                        error_msg = DataBaseManager._check_session_metadata(session_info, ephys_info)
                    if not error_msg:
                        for k in worker.session_info.keys():
                            worker.session_info[k] = session_info[k]
                        if worker.ephys_info:
                            for k in worker.ephys_info.keys():
                                worker.ephys_info[k] = ephys_info[k]
                        worker.finish()
                else:
                    error_msg = f"Commit is in progress or is not ready for final stage."
            else:
                error_msg = f"In-progress commit task {task_id} not found on server. Start over."
        return error_msg

    @staticmethod
    def _check_session_metadata(session: Dict[str, AttributeValue],
                                ephys: Optional[Dict[str, AttributeValue]]) -> Optional[str]:
        """
        Validate the proposed metadata for an experiment session to be committed to the database.
        TODO: HACK - We can't use code in table_views.py, because it results in a circular import error

        Args:
            session: The proposed row entry in the Session database table (dictionary of attribute ID-value pairs).
            ephys: The proposed row entry in the Session.EPhys part table. None for behavior-only sessions.

        Returns:
            None if metadata is valid, else a brief user-facing error description.
        """
        for key in ['experimenter', 'subj_id', 'session_date', 'session_sfx', 'rig_id', 'study_id', 'session_notes']:
            if key not in session:
                return f"Invalid entry: Missing attribute '{key}'"
        if not DataBaseManager().attribute_exists(DBTable.USER, 'username', session['experimenter']):
            return f"Invalid entry: Missing foreign key: 'experimenter' = {session['experimenter']}"
        if not DataBaseManager().attribute_exists(DBTable.SUBJECT, 'subj_id', session['subj_id']):
            return f"Invalid entry: Missing foreign key: 'subj_id' = {session['subj_id']}"
        if not DataBaseManager().attribute_exists(DBTable.RIG, 'rig_id', session['rig_id']):
            return f"Invalid entry: Missing foreign key: 'rig_id' = {session['rig_id']}"
        try:
            session['study_id'] = int(session['study_id'])
        except (TypeError, ValueError):
            return "Invalid entry: Foreign key 'study_id' must be an integer"
        if not DataBaseManager().attribute_exists(DBTable.STUDY, 'study_id', session['study_id']):
            return f"Invalid entry: Missing foreign key: 'study_id' = {session['study_id']}"
        if not check_date(session['session_date']):
            return f"Invalid entry: 'session_date' = {session['session_date']}: Date is invalid, earlier" \
                   f" than 1900-01-01, or in the future."
        try:
            num_value = int(session['session_sfx'])
            if num_value < 0 or num_value > 9:
                return "Invalid entry: 'session_sfx' must lie in [0..9]"
        except(TypeError, ValueError):
            return f"Invalid entry: 'session_sfx' = '{str(session['session_sfx'])}' cannot be parsed as an integer"

        session_pk = {key: session[key] for key in ['experimenter', 'subj_id', 'session_date', 'session_sfx']}
        if DataBaseManager().num_table_rows(DBTable.SESSION, restriction=session_pk) != 0:
            return "Invalid entry: Duplicate session key!"

        if not ephys:
            return None
        for key in ['ephys_src', 'probe_type', 'sampling_rate', 'probe_x', 'probe_y', 'probe_depth', 'ba_id']:
            if key not in ephys:
                return f"Invalid entry: Missing attribute '{key}'"
        ephys_sources = ['Omniplex', 'Omniplex clips', 'Plexon MAP', 'Maestro Waveform', 'Maestro Spike Ch']
        if ephys['ephys_src'] not in ephys_sources:
            return f"Invalid entry: Unsupported option for 'ephys_src': '{str(ephys['ephys_src'])}'"
        if ephys['probe_type'] not in ['single', '32-channel', 'other']:
            return f"Invalid entry: Unsupported option for 'probe_type': '{str(ephys['probe_type'])}'"
        for key in ['sampling_rate', 'probe_x', 'probe_y', 'probe_depth']:
            try:
                float(ephys[key])
            except(TypeError, ValueError):
                return f"Invalid entry: '{key}' = '{ephys[key]}' cannot be parsed as a floating-point value"
        try:
            ephys['ba_id'] = int(ephys['ba_id'])
        except (TypeError, ValueError):
            return "Invalid entry: Foreign key 'ba_id' must be an integer"
        if not DataBaseManager().attribute_exists(DBTable.BRAIN_AREA, 'ba_id', ephys['ba_id']):
            return f"Invalid entry: Missing foreign key: 'ba_id' = {ephys['ba_id']}"
        return None

    def cancel(self, task_id: str) -> bool:
        """
        Cancel a session commit task in progress or remove a completed task. If the relevant background task is still
        running, it is cancelled gracefully and the temporary staging directory for the commit task is removed. If the
        task has already finished -- successfully or not --, the server keeps the task object until the client confirms
        the task should be removed (so the client can, for example, display an error message in the event the task
        failed).

        NOTE: If the background thread is still running, this method issues the cancel request but does NOT wait for the
        thread to terminate. If the worker thread should fail on an error before detecting the cancel signal, then the
        staging directory will not be removed.

        Args:
            task_id: The commit task identifier.

        Returns:
            True if the identified commit task was removed from the server, false if task_id does not identify an
            existing commit task.

        """
        with self.task_list_lock:
            worker: Optional[ProcessArchiveThread] = self.running_tasks.pop(task_id, None)

        if not worker:
            return False

        # when worker terminates on an error, it does not remove the staging directory.
        if worker.is_alive():
            worker.cancel()
        elif not worker.result:
            DataBaseManager.delete_directory_tree(DataBaseManager.get_staging_directory_for(task_id))

    @staticmethod
    def delete_directory_tree(staging_dir: Path) -> None:
        try:
            if staging_dir.exists():
                shutil.rmtree(str(staging_dir))
        except OSError:
            # TODO: Need to log this error to an admin log so it can be addressed
            pass


class ProcessArchiveThread(threading.Thread, sgl.TrialProducer):
    """
    This worker thread handles server-side processing during stages 2-4 of a session commit task:
        1) Wait for upload of session archive ZIP to the staging directory, monitoring its progress once per second. If
        the upload does not start after 10 minutes of waiting, or the upload stalls for more than 10 minutes, report the
        error and terminate.

        2) Once the upload completes, process all Maestro data files in the session archive (in situ -- the files are
        NOT extracted from the ZIP file) and generate the list of distinct trial protocols presented over the course of
        the experiment session. If an error occurs, report the error and terminate.

        3) Load timing information for each trial. The primary purpose of this step is to process the strobed and event
        data in the Omniplex file(s) in the archive in order to align any neural unit responses with the individual
        trial timelines.

        4) Further process the archive for any neural unit data in the archive. The experimenter must provide their
        own spike sorting results in a single pickle file in the archive. See the module header comments for a
        description of this file. For now, we only support neural units recorded on the Omniplex system, and the archive
        must include the relevant PL2 file(s) for each unit specified in the pickle.

        5) Using the processing results and information already stored in the lab database, choose values for metadata
        attributes that will be stored in the Session table and its part tables, Session.Ephys and Session.Neuron.

        6) Store the results as a dictionary {'protocols': ..., 'timings': ..., 'units': ...} in the pickle file
        'preprocessing.pickle' within the staging directory.

        7) After pre-processing is complete, the worker enters stage 3, during which the user on the client side reviews
        the results and may make changes to the session metadata. The worker is essentially paused in this stage,
        waiting for the command to enter stage 4. Any changes to the session metadata are validated before the worker
        can transition to stage 4.

        8) In stage 4, the worker commits the experiment session to the lab database and raw data repository. First the
        ZIP archive and any other supporting files are moved from the staging directory to their permanent place in the
        data repository. Then the database is updated with the new Session object, a Session.EPhys entry and one or more
        Session.Neuron entries if the session included neural response data, and a TrialProtocol entry for each trial
        protocol not already in the database. Lastly, the Trial table in the database is populated with a new entry for
        each Maestro trial presented during the session.

    To communicate progress to the main thread, the worker will post a message to a synchronous queue. A message is
    posted whenever there's a significant progress transition. It is incumbent on the thread that launched the worker to
    monitor this queue. If an error occurs, the error description is the last message posted to the queue, and that
    message starts with the string "Error".

    To cancel the session commit, call cancel(). This method sets a flag to inform the worker thread and returns
    immediately. The worker thread will stop its work in progress and remove the staging directory in its entirety.
    """
    def __init__(self, task_id: str):
        super(ProcessArchiveThread, self).__init__(name=f"ProcessArchive-{task_id}")
        self.staging_dir: Path = DataBaseManager.get_staging_directory_for(task_id)
        """ The temporary staging directory for the session commit task. ZIP archive gets uploaded here. """
        self.zip_path: Optional[Path] = None
        """ Once session ZIP archive is uploaded, this is its file system location in the staging directory. """
        self.msg_q = Queue()
        """ A synchronous queue by which worker sends progress messages to main server thread. """
        self.latest_message: str = ""
        """ The most recent progress message received on the synchronous queue. Not touched by worker thread."""
        self.result: Optional[bool] = None
        """ Flag set to True/False to indicate success/failure upon termination. """
        self._cancel_request = threading.Event()
        """ Event object set by the server to cancel the session commit. Checked regularly by the worker thread. """
        self._finish_request = threading.Event()
        """ Event object set by the server to signal the worker to complete the session commit (stage 4). After 
        pre-processing the session archive (stage 2), the worker waits on this event to be signaled, waking up once
        per second to check whether the server has signaled a cancel request."""
        self.stage: int = 2
        """ The current processing stage in the session commit workflow. Set by worker; read-only to server. """
        self.protocols: Optional[List[maestro.Protocol]] = None
        """ The list of trial protocols culled from the session data archive during stage 2 pre-processing. Set by
        worker. Safe for server to access only while worker is paused in stage 3. """
        self.trial_info: Optional[Dict[str, _TrialInfo]] = None
        """ Dictionary maps the filename for each Maestro data file in the session archive to timing and trial protocol
        information for the particular trial instance recorded in that file. In particular, this includes the Omniplex
        start and stop timestamps required to align neural responses recorded on the Omniplex system with the behavioral
        responses recorded by Maestro. Set by worker. Safe for server to access only while worker is in stage 3. """
        self.units: Optional[List[OmniplexUnit]] = None
        """ The list of neural units culled from the session data archive during stage 2 pre-processing. Includes the
        information required to prepare an entry in the Session.Neuron part table for each neural unit. Prepared by
        worker during stage 2 pre-processing. Safe for server to access only while worker is paused in stage 3; during
        that stage, the user will need to assign a neuron type to each neural unit. """
        self.session_info: Optional[Dict[str, AttributeValue]] = None
        """ User-supplied information required to add an entry in the Session table in the database, keyed by the 
        Session table attribute IDs. During pre-processing, the worker thread will initialize this information. The
        client will provide the user-edited version of the dictionary upon initiating the final commit (stage 4). """
        self.ephys_info: Optional[Dict[str, AttributeValue]] = None
        """ When a session includes neural unit recordings, this field will contain user-supplied information required
        to add an entry in the Session.EPhys part table in the database, keyed by the attribute IDs in that table. It
        does not include the primary keys that identify the session itself, as these are in self.session_info. During
        stage 2 pre-processing, the worker thread will initialize this information. The client will provide the user-
        edited version of the dictionary upon initiating the final commit (stage 4). """

    def run(self):
        cancelled = False
        self.msg_q.put_nowait("Awaiting upload...")

        # Wait for upload to begin, then monitor progress until ZIP file is present in staging directory. Fail if upload
        # does not start within 10 minutes or, once started, if it stalls for longer than 10 minutes. NOTE: If upload is
        # fast enough, the ZIP file could be present before even detecting that the upload started!
        t0 = time.time()
        upload_path: Optional[Path] = None
        n_parts_uploaded = 0
        while (not self.zip_path) and (not cancelled):
            time.sleep(1)
            if self._cancel_request.is_set():
                cancelled = True
            elif not upload_path:
                if time.time() - t0 > 600:
                    self.msg_q.put_nowait("Error: Upload failed to start for more than 10 minutes")
                    self.result = False
                    return
                for child in self.staging_dir.iterdir():
                    if child.is_dir() and child.name.endswith('zip'):
                        upload_path = child
                        t0 = time.time()
                        self.msg_q.put_nowait("Upload started...")
                    elif child.is_file() and child.name.endswith('.zip'):
                        self.zip_path = child
            else:
                try:
                    n_chunks = len([f for f in upload_path.iterdir() if f.is_file()])
                    if n_chunks == n_parts_uploaded:
                        if time.time() - t0 > 600:
                            self.msg_q.put_nowait("Error: Upload has stalled for more than 10 minutes.")
                            self.result = False
                            return
                    else:
                        n_parts_uploaded = n_chunks
                        t0 = time.time()
                        self.msg_q.put_nowait(f"Uploading archive - {n_parts_uploaded} parts received")
                except Exception as err:
                    # check to see if the upload has finished - in which case the temporary upload folder will have been
                    # replaced by a ZIP file (causing exception in code above)
                    for child in self.staging_dir.iterdir():
                        if child.is_file() and child.name.endswith('.zip'):
                            self.zip_path = child
                            self.msg_q.put_nowait(f"Upload completed: {child.name}")
                            break
                    if not self.zip_path:
                        self.msg_q.put_nowait(f"Error: Archive upload failed, ZIP file missing ({err})")
                        self.result = False
                        return

        # Pre-process archive contents...
        cancelled = self._cancel_request.is_set()
        if not cancelled:
            error_msg = self._preprocess_session_archive()
            if error_msg:
                self.msg_q.put_nowait(error_msg)
                self.result = False
                return
            else:
                self.msg_q.put_nowait("Finished pre-processing session data archive.")
            cancelled = self._cancel_request.is_set()

        # Stage 3 - worker paused waiting for signal to begin final commit phase (stage 4)
        finish = False
        if not cancelled:
            self.stage = 3
            while not (cancelled or finish):
                finish = self._finish_request.wait(1.0)
                cancelled = self._cancel_request.is_set()

        # Stage 4 - complete the session commit. If cancelled in this stage, any partially commited data is unwound.
        if finish and not cancelled:
            self.stage = 4
            error_msg = self._finish_commit()
            if error_msg:
                self.msg_q.put_nowait(error_msg)
                self.result = False
                return

        self.result = False if cancelled else True
        DataBaseManager.delete_directory_tree(self.staging_dir)
        self.msg_q.put_nowait("Success!" if not cancelled else "User has cancelled session commit")

    def cancel(self) -> None:
        """ Cancel the session commit task handled by this worker thread. """
        self._cancel_request.set()

    def finish(self) -> None:
        """ Wake up the worker thread to complete the session commit task. Has no effect if the worker is not
        waiting in stage 3 of the task workflow. """
        if self.stage == 3:
            self._finish_request.set()

    def _preprocess_session_archive(self) -> Optional[str]:
        """
        This method pre-processes the uploaded session data ZIP archive, scanning the archive contents and extracting
        information that will be needed when the session is actually committed to the lab database: (1) the unique trial
        protocols presented during the session; (2) timing information for all trial reps, in particular, the start and
        stop timestamps for the trial in the Omniplex timeline (for electrophysiological experiments using the Omniplex
        system); and (3) metrics for all neural units recorded in the session. It also initializes metadata that will
        be added to the database (Session and Session.EPhys tables) when the session is committed.

        Pre-processing a large (>1GB) session can take many minutes, so progress messages are delivered over the
        thread's synchronous message queue. The method also checks regularly for a cancel request.

        Returns:
            None if successful; otherwise an error description. Returns None if the session commit is cancelled.
        """
        error_msg: Optional[str] = None
        try:
            self.trial_info = dict()
            self.units = list()
            with zipfile.ZipFile(self.zip_path, 'r') as archive:
                self.msg_q.put_nowait("Scanning archive contents...")
                data_file_name_pattern = re.compile('.[0-9][0-9][0-9][0-9]+$')
                archive_list = archive.infolist()
                pl2s_archived: List[zipfile.ZipInfo] = list()
                units_zip_info: Optional[zipfile.ZipInfo] = None
                session_date: Optional[date] = None
                sample_maestro_file_name: str = ""
                for info in archive_list:
                    if (len(info.filename) > 3) and (info.filename[-3:].lower() == 'pl2'):
                        pl2s_archived.append(info)
                    elif data_file_name_pattern.search(info.filename) is not None:
                        sample_maestro_file_name = info.filename
                        header = maestro.DataFileHeader.parse_header(archive.read(info))
                        file_index = int(info.filename[-4:])
                        header_timestamp = header.timestamp_ms if header.version >= 21 else None
                        if session_date is None:
                            session_date = header.date_recorded
                        elif session_date != header.date_recorded:
                            raise Exception("Recorded date must be the same for all trial files in archive!")
                        duration = float(header.num_scans_saved - 1) / 1000.0  # Trial mode scan rate is fixed at 1KHz
                        self.trial_info[info.filename] = _TrialInfo(file_index, duration, header_timestamp)
                    elif ((len(info.filename) > 7) and (info.filename[-7:].lower() == '.pickle')) or \
                            ((len(info.filename) > 4) and (info.filename[-4:].lower() == '.pkl')):
                        if units_zip_info is None:
                            units_zip_info = info
                        else:
                            raise Exception("Found more than one spikes data file in session data archive!")
                if (units_zip_info is not None) and (len(pl2s_archived) == 0):
                    raise Exception("Missing Omniplex file(s) for spike-sorted unit data!")
                if self._cancel_request.is_set():
                    return None

                self.msg_q.put_nowait("Processing archive for trial protocols...")
                self.protocols, file_to_proto_hash = maestro.Protocol.extract_protocols_from_session_data(archive)
                if len(self.protocols) == 0:
                    raise Exception("No trial protocols found in session archive!")
                for filename, proto_hash in file_to_proto_hash.items():
                    self.trial_info[filename].proto_hash = proto_hash
                if self._cancel_request.is_set():
                    return None

                unit_data: Optional[Dict[str, List[Any]]] = None
                if units_zip_info is not None:
                    self.msg_q.put_nowait(f"Loading neural units data file {units_zip_info.filename}...")
                    unit_data = pickle.loads(archive.read(units_zip_info))
                    pl2_filenames = [x.filename for x in pl2s_archived]
                    if not _validate_neural_unit_data(unit_data, pl2_filenames):
                        raise Exception(f"Invalid format for neural units file: {units_zip_info.filename}")
                    # if 'filename' field missing, assume all units recorded in same Omniplex file
                    if 'filename' not in unit_data:
                        unit_data['filename'] = [pl2_filenames[0]] * len(unit_data['channel'])
                    if self._cancel_request.is_set():
                        return None

                if units_zip_info is not None:
                    for pl2_zip_info in pl2s_archived:
                        save_path = self._chunked_extract_from_archive(archive, pl2_zip_info, self.zip_path.parent)
                        if save_path is None:
                            return None
                        self._process_omniplex_file(save_path, unit_data)
                        if self._cancel_request.is_set():
                            return None
                    # if there is unit data, we require metrics for each unit specified in the neural units data file,
                    # and there must be Omniplex timestamps for all trials
                    if len(self.units) < len(unit_data['channel']):
                        raise Exception(
                            f"Missing analog data for at least one unit defined in {units_zip_info.filename}")
                    for key in self.trial_info.keys():
                        if self.trial_info[key].omniplex_start is None:
                            raise Exception(f"Missing Omniplex start/stop timestamps for {key}")

                # initialize session metadata. We get the session date from the Maestro trials, and we may get the
                # subject ID from the ZIP archive file name or a Maestro data file name. If there already exists a
                # session or session on the same date for the same subject and experimenter, adjust the session suffix
                # accordingly.
                # TODO: This is kind of a HACK because we have to know a lot about the underlying tables here
                self.session_info = dict()
                user_choices = DataBaseManager().fetch_attribute_values(DBTable.USER, 'username')
                self.session_info['experimenter'] = user_choices[0]
                subject_choices = DataBaseManager().fetch_attribute_values(DBTable.SUBJECT, 'subj_id')
                for choice in subject_choices:
                    if choice.lower() in ','.join([self.zip_path.name.lower(), sample_maestro_file_name.lower()]):
                        self.session_info['subj_id'] = choice
                        break
                if 'subj_id' not in self.session_info:
                    self.session_info['subj_id'] = subject_choices[0]
                self.session_info['session_date'] = session_date
                restriction = {k: self.session_info[k] for k in ['experimenter', 'subj_id', 'session_date']}
                session_suffix = DataBaseManager().num_table_rows(DBTable.SESSION, restriction) + 1
                self.session_info['session_sfx'] = session_suffix if session_suffix < 10 else 1
                rig_choices = DataBaseManager().fetch_attribute_values(DBTable.RIG, 'rig_id')
                self.session_info['rig_id'] = rig_choices[0]
                study_choices = DataBaseManager().fetch_attribute_values(DBTable.STUDY, 'study_id')
                self.session_info['study_id'] = study_choices[0]
                self.session_info['session_notes'] = ""

                # if neural units were recorded, initialize metadata about session's electrophysiological recording.
                # User edits this metadata in stage 3. We only support Omniplex system right now, and we infer sampling
                # rate from the length of a unit's spike template waveform, which spans 10ms.
                if len(self.units) > 0:
                    channel_ids = {unit.channel for unit in self.units}
                    self.ephys_info = dict()
                    self.ephys_info['ephys_src'] = 'Omniplex'
                    self.ephys_info['probe_type'] = 'single' if len(channel_ids) == 1 else '32-channel'
                    self.ephys_info['sampling_rate'] = len(self.units[0].template) / 0.01
                    self.ephys_info['probe_x'] = 0
                    self.ephys_info['probe_y'] = 0
                    self.ephys_info['probe_depth'] = 0
                    brain_area_choices = DataBaseManager().fetch_attribute_values(DBTable.BRAIN_AREA, 'ba_id')
                    self.ephys_info['ba_id'] = brain_area_choices[0]

        except Exception as err:
            error_msg = f"Error: {str(err)}"

        return error_msg

    def _finish_commit(self) -> Optional[str]:
        """
        This method implements the final stage of the session commit workflow:

            1) Entries are inserted into the Session, Session.EPhys, and Session.Neuron tables as appropriate, and all
            trial protocols not already in the database are inserted into the TrialProtocol table.

            2) The Trial table and its part tables are populated with data from all the trials presented during the
            session. This is done in 50-trial chunks to regularly check for user cancel and update progress.

            3) The ZIP archive is moved to a permanent folder in the lab data repository, and the results from
            pre-processing the session archive, along with session and electrophysiology metadata entered manually by
            the user during the review stage, are saved in a pickle file in the same folder. That folder is
            %REPO_HOME/<username>, where <username> is the experimenter's username in the database. The base filename
            for the .zip and .pickle files is "<subj_id>_<session_date>_<session_sfx>", where <session_date> is in
            ISO format 'YYYY-MM-DD'.

        If an error occurs at any point during the commit, any changes to the database and the file repository are
        unwound before returning.

        Returns:
            None if successful, in which case the session is fully committed to the database; otherwise an error
            description.
        """
        error_msg: Optional[str] = None
        session_repo_path = DataBaseManager.get_repo_directory_for(self.session_info['experimenter'])
        base_filename = f"{self.session_info['subj_id']}_{str(self.session_info['session_date'])}_" \
                        f"{self.session_info['session_sfx']}"
        zip_path_in_repo = Path(session_repo_path, f"{base_filename}.zip")
        pickle_path_in_repo = Path(session_repo_path, f"{base_filename}.pickle")
        session_inserted = False
        protocols_added = False
        protocols_to_add: List[Dict[str, Any]] = list()
        protocol_table = sgl.TrialProtocol()
        session_pks = ['experimenter', 'subj_id', 'session_date', 'session_sfx']
        try:
            # IMPORTANT: We access the database tables directly here and do NOT go through DataBaseManager, as we don't
            # want any of the additions logged. A single 'session commit' log entry, along with the files stored in the
            # repository, is sufficient to reproduce everything that happens in this commit job.
            self.msg_q.put_nowait(f"Inserting session entry and any new trial protocols into database...")
            session_table = sgl.Session()
            with session_table.connection.transaction:
                session_table.insert1(self.session_info, replace=False)
                session_inserted = True
                if self.ephys_info is not None:
                    # need primary key of parent table for insertion into part table
                    for pk in session_pks:
                        self.ephys_info[pk] = self.session_info[pk]
                    sgl.Session.EPhys().insert1(self.ephys_info, replace=False)

                    neurons: List[Dict[str, Any]] = list()
                    for i, unit in enumerate(self.units):
                        neuron = dict()
                        for pk in session_pks:
                            neuron[pk] = self.session_info[pk]
                        neuron['unit_id'] = i + 1
                        neuron['unit_channel'] = unit.channel
                        neuron['unit_type'] = unit.neuron_type
                        neuron['unit_rate'] = unit.firing_rate
                        neuron['unit_snr'] = unit.snr
                        neuron['unit_template'] = unit.template
                        neurons.append(neuron)
                    sgl.Session.Neuron().insert(neurons, replace=False)

            if self._cancel_request.is_set():
                raise Exception("Operation cancelled.")

            existing_proto_keys = [pk['proto_hash'] for pk in protocol_table.fetch('KEY')]
            for protocol in self.protocols:
                if protocol.md5_digest not in existing_proto_keys:
                    protocol_entry: Dict[str, Any] = dict()
                    protocol_entry['proto_hash'] = protocol.md5_digest
                    protocol_entry['proto_name'] = protocol.trial.name
                    protocol_entry['proto_set'] = "" if (protocol.trial.set_name is None) else protocol.trial.set_name
                    protocol_entry['proto_subset'] = \
                        "" if (protocol.trial.subset_name is None) else protocol.trial.subset_name
                    protocol_entry['proto_def'] = pickle.dumps(protocol)
                    protocols_to_add.append(protocol_entry)
            if len(protocols_to_add) > 0:
                protocol_table.insert(protocols_to_add, replace=False)
                protocols_added = True

            if self._cancel_request.is_set():
                raise Exception("Operation cancelled.")

            # in order to monitor progress and check for user cancel while populating trials, the Trial table class
            # relies on a TrialProducer delegate to handle the task from within its make() call. The actual insertions
            # happen in insert_trials_for_session(). NOTE: Since the auto-populate cycle is wrapped in a transaction,
            # any trial insertions will be unwound if an exception occurs while populating.
            trial_table = sgl.Trial()
            trial_table.set_trial_producer(self)
            trial_table.populate()
            trial_table.set_trial_producer(None)

            if self._cancel_request.is_set():
                raise Exception("Operation cancelled.")

            self.msg_q.put_nowait(f"Saving session archive and pre-processing results to data repository...")
            if not session_repo_path.is_dir():
                session_repo_path.mkdir(parents=True)
            self.zip_path.replace(zip_path_in_repo)
            results = {'protocols': self.protocols, 'trials': self.trial_info, 'units': self.units,
                       'session': self.session_info, 'ephys': self.ephys_info}
            with open(pickle_path_in_repo, 'wb') as file:
                pickle.dump(results, file)

            # finally, log the session commit
            res = DataBaseManager().log_session_commit(
                self.session_info['experimenter'], self.session_info['subj_id'],
                str(self.session_info['session_date']), self.session_info['session_sfx'])
            if res:
                raise Exception(res)

        except Exception as err:
            error_msg = f"Error - Failed to commit session:\n  {str(err)}"
        finally:
            # unwind all changes if the commit failed! Note that trials are automatically removed when session is.
            if error_msg:
                try:
                    if session_inserted:
                        (sgl.Session() & self.session_info).delete(verbose=False)
                    if protocols_added:
                        restriction = [f"proto_hash = '{p['proto_hash']}'" for p in protocols_to_add]
                        (protocol_table & restriction).delete(verbose=False)
                except Exception:
                    pass   # TODO: We really have to kill the database at this point, because it is inconsistent.
                zip_path_in_repo.unlink(missing_ok=True)
                pickle_path_in_repo.unlink(missing_ok=True)

        return error_msg

    def insert_trials_for_session(self, session_key: Dict[str, Any]) -> None:
        # generate list of trial file names in presentation order. We CANNOT rely on file creation time! If Omniplex
        # system used and all trials were timestamped within the same PL2 file, then order by Omniplex start time. Else,
        # if the file header includes the internal Maestro timestamp (we assume all trials will if the first one does!),
        # use that. Otherwise, order by ascending numeric file suffix (.0001,...).
        sort_strategy = None
        sorted_filenames = None
        if isinstance(self.units, list) and (len(self.units) > 0):
            pl2_file_set = {unit.source_file for unit in self.units}
            if len(pl2_file_set) == 1:
                sort_strategy = 'omniplex'
                sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].omniplex_start)
        if not sort_strategy:
            if self.trial_info[next(iter(self.trial_info))].header_timestamp:
                sort_strategy = 'timestamp'
                sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].header_timestamp)
            else:
                sort_strategy = 'index'
                sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].file_index)

        num_trials = len(sorted_filenames)
        num_inserted = 0
        self.msg_q.put_nowait(f"Inserting trials into database... {num_inserted} of {num_trials}")
        t0 = time.time()
        trial1_start_sec: float = 0
        with zipfile.ZipFile(self.zip_path, 'r') as archive:
            for trial_filename in sorted_filenames:
                data_file = maestro.DataFile.load(archive.read(trial_filename), trial_filename)
                t_info = self.trial_info[trial_filename]

                # compute scale factor to convert Omniplex spike times to Maestro timeline. However, if Maestro trial
                # length according to Omniplex is more than 2ms off, fail.
                maestro_omniplex_time_scaling = 1.0
                if t_info.omniplex_start is not None:
                    trial_length = (data_file.trial.record_start() + data_file.header.num_scans_saved - 1) / 1000.0
                    omniplex_length = t_info.omniplex_stop - t_info.omniplex_start
                    if abs(trial_length - omniplex_length) > 0.002:
                        raise Exception(f"Trial duration on Omniplex does not match Maestro trial "
                                        f"duration: {trial_filename}")
                    maestro_omniplex_time_scaling = trial_length / omniplex_length

                trial_entry: Dict[str, Any] = dict(
                    session_key,
                    trial_idx=(num_inserted + 1),
                    trial_header=pickle.dumps(data_file.header),
                    trial_filename=trial_filename,
                    trial_dur=data_file.header.num_scans_saved - 1,
                    trial_record_start=data_file.trial.record_start(),
                    trial_success=((data_file.header.flags & maestro.FLAG_REWARD_EARNED) != 0),
                    trial_rewarded=((data_file.header.flags & maestro.FLAG_REWARD_GIVEN) != 0),
                    trial_rew1=data_file.header.reward_len1_ms,
                    trial_rew2=data_file.header.reward_len2_ms,
                    proto_hash=t_info.proto_hash
                    )

                # compute trial start time relative to start of first trial in session -- if possible
                if sort_strategy == 'index':
                    trial_entry['trial_ts'] = -1
                else:
                    t_sec = t_info.omniplex_start if (sort_strategy == 'omniplex') else t_info.header_timestamp/1000.0
                    if num_inserted == 0:
                        trial_entry['trial_ts'] = 0
                        trial1_start_sec = t_sec
                    else:
                        trial_entry['trial_ts'] = t_sec - trial1_start_sec

                # for this trial, get the values of the protocol's random variables
                protocol = next((x for x in self.protocols if x.md5_digest == t_info.proto_hash), None)
                if not protocol:
                    raise Exception(f"Internal inconsistency: No trial protocol defined for trial in {trial_filename}")
                rv_values: List[Any] = list()
                for param in protocol.diffs:
                    rv_value = data_file.trial.retrieve_segment_table_parameter_value(param)
                    if not rv_value:
                        raise Exception(f"Internal inconsistency: Invalid RV ({param}) for trial in {trial_filename}")
                    rv_values.append(rv_value)
                trial_entry['trial_rvs'] = pickle.dumps(rv_values)

                sgl.Trial().insert1(trial_entry, replace=False)

                # insert recorded behavioral responses into part table Trial.BehavioralResponse
                for response_id in maestro.BEHAVIOR_TO_CHANNEL.keys():
                    ai_channel = maestro.BEHAVIOR_TO_CHANNEL[response_id]
                    if ai_channel in data_file.ai_data:
                        scale = maestro.ADC_TO_DEG if (response_id.find('POS') > -1) else maestro.ADC_TO_DPS
                        response = np.array(data_file.ai_data[ai_channel]) * scale
                        response_entry = dict(
                            session_key,
                            trial_idx=(num_inserted + 1),
                            response_id=response_id,
                            response_trace=response
                        )
                        sgl.Trial.BehavioralResponse().insert1(response_entry, replace=False)

                # insert neural unit responses, if any, into Trial.NeuronalResponse. A neural unit may not fire any
                # spikes during a trial, but that could be a valid response. Only exclude a unit if the last spike time
                # is before trial start or the first spike time is after trial end!
                if self.units is not None:
                    for i, unit in enumerate(self.units):
                        spikes = unit.spike_times
                        if (spikes[-1] < t_info.omniplex_start) or (spikes[0] > t_info.omniplex_stop):
                            continue

                        spikes_in_trial = spikes[(spikes >= t_info.omniplex_start) & (spikes <= t_info.omniplex_stop)]
                        spikes_in_trial = (spikes_in_trial - t_info.omniplex_start) * maestro_omniplex_time_scaling
                        response_entry = dict(
                            session_key,
                            trial_idx=(num_inserted + 1),
                            unit_id=(i + 1),
                            spike_times=spikes_in_trial
                        )
                        sgl.Trial.NeuronalResponse().insert1(response_entry, replace=False)

                # insert any recorded marker pulse events into Trial.Event (recorded in Maestro file, not by Omniplex).
                if data_file.events is not None:
                    for di_channel in data_file.events:
                        # convert event times from ms to sec and offset if event recording started after trial began
                        event_times = np.array(data_file.events[di_channel]) * 0.001 + data_file.trial.record_start()
                        event_entry = dict(
                            session_key,
                            trial_idx=(num_inserted + 1),
                            event_ch=di_channel,
                            event_times=event_times
                        )
                        sgl.Trial.Event().insert1(event_entry, replace=False)

                num_inserted += 1
                if (time.time() - t0) > 1:
                    self.msg_q.put_nowait(f"Inserting trials into database... {num_inserted} of {num_trials}")
                    if self._cancel_request.is_set():
                        raise Exception("Operation cancelled.")
                    t0 = time.time()

    def _chunked_extract_from_archive(self, archive: zipfile.ZipFile, pl2_info: zipfile.ZipInfo,
                                      destination: Path) -> Optional[Path]:
        """
        Extract a potentially very large file from a session data archive. If the uncompressed size of the file is under
        300MB, it is extracted in one go using ZipFile.extract(). Otherwise, it is extracted in 100MB chunks so that
        progress can be reported during the extraction and so that the operation can be cancelled prior to completion.

        Args:
            archive: The source ZIP archive.
            pl2_info: The file to be extracted.
            destination: The target directory to which the file is extracted.

        Returns:
            File system path for the extracted file, or None if the extraction was cancelled
        """
        chunk_size = 100 * 1024 * 1024
        size_in_mb: float = pl2_info.file_size / (1024 * 1024)
        if pl2_info.file_size < 300:
            self.msg_q.put_nowait(f"Extracting Omniplex file {pl2_info.filename} (size={size_in_mb:.1f} MB)")
            save_path = Path(archive.extract(pl2_info, str(destination)))
            return None if self._cancel_request.is_set() else save_path

        # Large file extract in chunks
        save_path = Path(destination, pl2_info.filename)
        self.msg_q.put_nowait(f"Extracting Omniplex file {pl2_info.filename}: 0 of {size_in_mb:.1f} MB ...")
        bytes_written: int = 0
        with archive.open(pl2_info, 'r') as source, open(save_path, 'wb') as target:
            while True:
                buffer = source.read(chunk_size)
                if len(buffer) == 0:
                    return save_path
                target.write(buffer)
                bytes_written += len(buffer)
                written_mb: float = bytes_written / (1024 * 1024)
                self.msg_q.put_nowait(f"Extracting Omniplex file {pl2_info.filename}: {written_mb:.1f} of "
                                      f"{size_in_mb:.1f} MB ...")
                if self._cancel_request.is_set():
                    return None

    def _process_omniplex_file(self, path: Path, unit_data: Dict[str, List[Any]]) -> None:
        """
        Process the Omniplex file for information needed when committing an electrophysiological recording session to
        the lab database.

        First, the method analyses the "Strobed" and "EVT02" event channels to find the Omniplex-recorded start and stop
        timestamps for each Maestro trial presented. These timestamps are essential in order to align neural unit
        spike times derived from the Omniplex recording with behavioral responses recorded in each individual Maestro
        trial data file.

        Second, the method calculate selected metrics for each identified neural unit (mean firing rate, signal-to-noise
        ratio, and the average spike template waveform) using the unit spike times (in "Omniplex time") and the original
        Omniplex analog data stream(s) from which those spike times were "sorted". These metrics are ultimately stored
        in the lab database. For details, see _prepare_neural_units()

        Args:
            path: The path to the Omniplex PL2 file to be processed.
            unit_data: The identified neural unit data, including channel ID, PL2 source file, and the spike timestamps
                in seconds since the Omniplex recording started. For a full description of this dictionary, see
                _validate_neural_unit_data().

        Raises:
            Exception: If an error occurs while loading and processing data in the Omniplex file.
        """
        with open(path, 'rb') as fp:
            self.msg_q.put_nowait(f"Processing trial timing information in Omniplex file {path.name}...")
            info = PL2.load_file_information(fp)
            timings_dict = _get_trial_timing_from_pl2_file(fp, info)
            for key in (timings_dict.keys() & self.trial_info.keys()):
                t_info = self.trial_info[key]
                t_info.omniplex_start, t_info.omniplex_stop = timings_dict[key]
            if self._cancel_request.is_set():
                return

            # which units are recorded in this PL2 file
            units_in_file = [i for i, filename in enumerate(unit_data['filename']) if filename == path.name]

            # multiple units may be recorded on the same analog channel, but we only want to load and process a given
            # analog channel once because the recordings can be very long!
            channel_ids = {unit_data['channel'][unit_idx] for unit_idx in units_in_file}
            for channel_id in sorted(channel_ids):
                spikes = [unit_data['spiketimes'][unit_idx] for unit_idx in units_in_file
                          if unit_data['channel'][unit_idx] == channel_id]
                units_found = self._prepare_neural_units(channel_id, spikes, path.name, fp, info)
                if units_found is None:
                    return
                self.units.extend(units_found)

    def _prepare_neural_units(self, channel_id: str, spikes: List[np.ndarray], filename: str,
                              fp: IO, info: Dict[str, Any]) -> Optional[List[OmniplexUnit]]:
        """
        Helper method processes the Omniplex analog data channel on which identified neural units were recorded and
        calculates selected metrics for those units: firing rate, SNR, average spike template waveform.

        On calculating the template waveform and SNR for each neural unit: The channel ID in the pickle file must start
        with "WB" (wide band data) or "SPKC" (narrow band data). Wide band data is preferred because the filtering
        parameters for SPKC can be changed during an Omniplex session and are not stored in the PL2 file. If the
        specified channel ID is "SPKC<num>", where <num> is a 2-digit number, the method first looks for the wide-band
        channel "WB<num>". If that is available, the analog trace is bandpass-filtered between 300-8000Hz using a
        second-order Butterworth filter via the SciPy package. If not, the analog trace on "SPKC<num>" is used as is
        (it should already have been filtered).

        To calculate the template waveform, the method averages 10-ms "clips" in the filtered trace that start 1ms prior
        to each spike timestamp. To calculate SNR, the method first estimates the standard deviation of the background
        noise as 1.4826 * median absolute deviation (MAD) of the data trace. The MAD = median(abs(x-X)) = median(abs(x))
        because X = median(x) is approximately 0 since the trace x has been bandpass-filtered, removing any DC offset.
        Then: SNR = (max(template) - min(template)) / 1.96 * std_background_noise.

        Since Omniplex recordings can be very long, and a wide-band analog trace must be bandpass-filtered once it is
        loaded from the PL2 file, this method would consume a lot of memory if the entire trace is loaded at once.
        Furthermore, loading a huge array can take many seconds, which prevents ProcessArchiveThread from making regular
        progress updates or promptly detecting the "cancel" event. For these reasons, the method uses a "chunked"
        approach to the calculations, reading and processing one block (65535 samples each, except the last block) at a
        time.

        It is possible for a single extracellular electrode to record activity from multiple neural units at the same
        time. It would be wasteful to re-process the same analog channel for each neural unit "sorted" from that
        channel, especially since the background noise calculation will be the same for all. Hence, the "spikes"
        argument is a list containing a spike timestamps array for each distinct unit recorded on the channel

        Args:
            channel_id: ID of the Omniplex analog data channel: wide-band "WBnn" or narrow-band "SPKCnn"
            spikes: List of Numpy arrays; each array holds the spike timestamps (in seconds during Omniplex recording)
                for a distinct neural unit recorded on the specified analog channel. It is assumed that each array
                contains at least two spike times.
            filename: The source PL2 filename.
            fp: The PL2 file object. The file must be open and is NOT closed on return.
            info: Dictionary containing "table of contents" for the PL2 file (see PL2.load_file_information).

        Returns:
            A list of neural unit objects containing the spike times array, firing rate, and other metrics calculated
            from the original analog data. Returns None if the commit task was cancelled.

        Raises:
            Exception: If an error occurs while processing the analog data channel.
        """
        self.msg_q.put_nowait(f"Calculating firing rate and other metrics for {len(spikes)} neural unit(s) on "
                              f"Omniplex channel {channel_id} ...")
        # if narrow band channel SPKC<num> specified, use wide band channel WB<num> instead IF it is available
        is_wide_band = (len(channel_id) > 2) and (channel_id[0:2].lower() == 'wb')
        is_narrow_band = (len(channel_id) > 4) and (channel_id[0:4].lower() == 'spkc')
        if not (is_wide_band or is_narrow_band):
            raise Exception(f"Bad Omniplex channel ID: {channel_id}")
        ch_index = -1
        try:
            if is_narrow_band:
                ch_index = [ch['name'] for ch in info['analog_channels']].index(channel_id)
                alt_id = "WB" + channel_id[-2:]
                ch_index = [ch['name'] for ch in info['analog_channels']].index(alt_id)
                is_wide_band = True
            else:
                ch_index = [ch['name'] for ch in info['analog_channels']].index(channel_id)
        except ValueError:
            pass
        if ch_index == -1:
            raise Exception(f"Did not find Omniplex analog channel data for channel ID: {channel_id}")

        num_blocks = len(info["analog_channels"][ch_index]["block_num_items"])
        samples_per_sec: float = info['analog_channels'][ch_index]['samples_per_second']
        to_volts: float = info['analog_channels'][ch_index]['coeff_to_convert_to_units']
        samples_in_template = int(samples_per_sec * 0.01)
        block_medians = np.zeros(num_blocks)
        num_clips = [0] * len(spikes)
        spike_idx = [0] * len(spikes)
        template = [np.zeros(samples_in_template) for _ in spikes]
        num_spikes = [len(spike_times) for spike_times in spikes]
        sample_idx = 0
        block_idx = 0

        # prepare bandpass filter in case analog signal is wide-band. The filter delays are initialized with zero-vector
        # initial condition and the delays are updated as each block is filtered...
        [b, a] = scipy.signal.butter(2, [2 * 300 / samples_per_sec, 2 * 8000 / samples_per_sec], btype='bandpass')
        filter_ic = scipy.signal.lfiltic(b, a, np.zeros(max(len(b), len(a))-1))

        prev_block: Optional[np.ndarray] = None
        t0 = time.time()
        while block_idx < num_blocks:
            # read in next block of samples and bandpass-filter it if signal is wide-band
            curr_block = PL2.load_analog_channel_block(fp, ch_index, block_idx, info)
            if is_wide_band:
                curr_block, filter_ic = scipy.signal.lfilter(b, a, curr_block, axis=-1, zi=filter_ic)

            # save block median for later SNR calculation
            block_medians[block_idx] = np.median(np.abs(curr_block))

            # for each distinct neural unit, accumulate all spike template clips that are fully contained in the current
            # block OR straddle the previous and current block
            num_samples_in_block = len(curr_block)
            for i in range(len(spikes)):
                while spike_idx[i] < num_spikes[i]:
                    # clip start and end indices with respect to the current block
                    start = int((spikes[i][spike_idx[i]] - 0.001) * samples_per_sec) - sample_idx
                    end = start + samples_in_template
                    if end >= num_samples_in_block:
                        break  # no more spikes fully contained in current block
                    elif start >= 0:
                        template[i] = np.add(template[i], curr_block[start:end])
                        num_clips[i] += 1
                    elif isinstance(prev_block, np.ndarray):
                        template[i] = np.add(template[i], np.concatenate((prev_block[start:], curr_block[0:end])))
                        num_clips[i] += 1
                    spike_idx[i] += 1

            # get ready for next block; check for cancel signal and update progress roughly once per second
            prev_block = curr_block
            sample_idx += num_samples_in_block
            block_idx += 1
            if (time.time() - t0) > 1:
                if self._cancel_request.is_set():
                    return None
                self.msg_q.put_nowait(f"Calculating firing rate and other metrics for {len(spikes)} neural unit(s) on "
                                      f"Omniplex channel {channel_id} ...{100.0*block_idx/num_blocks:.1f}%")
                t0 = time.time()

        # prepare neural unit objects. Assign a default neuron type to each -- preferably the "unknown" type if it is
        # defined in database
        neuron_types = DataBaseManager().fetch_rows(DBTable.NEURON_TYPE)
        initial_nt_id = neuron_types[0]['nt_id']
        for nt in neuron_types:
            if nt['nt_name'].lower() == 'unknown':
                initial_nt_id = nt['nt_id']
                break
        noise = np.median(block_medians) * 1.4826
        out: List[OmniplexUnit] = list()
        for i in range(len(spikes)):
            if num_clips[i] > 0:
                template[i] /= num_clips[i]
            snr = (np.max(template[i]) - np.min(template[i])) / (1.96 * noise)
            firing_rate = float(len(spikes[i])) / (spikes[i][-1] - spikes[i][0])
            template[i] *= to_volts * 1.0e6
            out.append(OmniplexUnit(filename, channel_id, spikes[i], firing_rate, snr, template[i], initial_nt_id))
        return out


def _validate_neural_unit_data(unit_data: Dict[str, List[Any]], pl2_filenames: List[str]) -> bool:
    """
    Helper method validates the object loaded from a single dedicated pickle file in the session ZIP archive that
    lists all identified neurons and their spike times.

    When researchers prepare the ZIP archive containing all data files for an experiment session including neural
    unit recordings, they must provide a single Python pickle file with the results of their spike-sorting analysis of
    all units recorded during the session. This pickle file contains a dictionary with 2-3 fields: 'channel',
    'spiketimes', and (optionally) 'filename'. The last field is required ONLY if there is more than one Omniplex PL2
    file in the archive. Each field is a list of length N, where N is the number of neural units. The 'channel' key
    holds the Omniplex-specific channel ID for the analog channel on which the unit was recorded, the 'filename' key
    holds the name of the Omniplex PL2 file within the ZIP archive, and the 'spiketimes' key holds the spike times (in
    seconds since the Omniplex recording started) for each unit, as a Numpy array.

    Args:
        unit_data: The dictionary loaded from the neural units pickle file
        pl2_filenames: List of all Omniplex PL2 files found in the session archive.

    Returns:
        True if unit_data is validly formatted as described above, false otherwise.
    """
    ok = isinstance(unit_data, dict) and ('channel' in unit_data) and ('spiketimes' in unit_data)
    if ok:
        ok = isinstance(unit_data['channel'], list) and isinstance(unit_data['spiketimes'], list) and \
             len(unit_data['channel']) == len(unit_data['spiketimes']) and \
             all(isinstance(x, str) for x in unit_data['channel']) and \
             all(isinstance(x, np.ndarray) for x in unit_data['spiketimes'])
    if ok:
        if 'filename' in unit_data:
            ok = isinstance(unit_data['filename'], list) and (
                        len(unit_data['filename']) == len(unit_data['channel'])) \
                 and all(x in pl2_filenames for x in unit_data['filename'])
        else:
            ok = (len(pl2_filenames) == 1)
    return ok


def _get_trial_timing_from_pl2_file(fp: IO, info: Optional[Dict[str, Any]] = None) -> Dict[str, Tuple[float, float]]:
    """
    Analyze the strobed character events and the XS2 events in the PL2 file's event streams in order to find the
    file names of all Maestro data files successfully saved during the Omniplex recording session, along with the
    timestamps marking the start and end of each trial presented. This information is needed to align neural unit
    responses recorded on the Omniplex with the individual trial timelines.

    For each Maestro trial that is successfully saved, Maestro delivers a sequence of ASCII characters along with pulses
    on XS2 ("EVT02" channel on Omniplex): a "trial start" character code 0x02, followed by null-terminated trial name
    and null-terminated filename, a pulse on XS2 immediately after the trial commences, a second pulse on XS2
    immediately after the trial ends, then a 0x06 character to indicate the file was saved, and finally a "trial stop"
    character code 0x03.

    This method loads and parses the relevant event data channels to extract, for each successfully saved data file,
    the filename, and the timestamps of the two XS2 pulses bracketing the trial duration.

    Args:
        fp: The PL2 file object. It must be open and is NOT closed upon return.
        info: Header and footer information from the PL2 file, for navigating a potentially multi-GB file. If None,
            the method will read in that information first.
    Returns:
        A dictionary mapping the name of each saved data file to a 2-tuple (start, stop) containing the start and stop
        timestamps of the corresponding Maestro trial in seconds since the start of the Omniplex recording. The
        dictionary will be empty if the expected event channel data is not found in the PL2 file.

    Raises:
        A generic exception if a problem is detected while analyzing the Omniplex strobed character and event channels.
        The exception message is the error description.
    """
    result: Dict[str, Tuple[float, float]] = dict()
    if info is None:
        info = PL2.load_file_information(fp)
    timestamp_frequency = info['timestamp_frequency']  # To convert timestamps from raw tick counts to seconds

    # get strobed character data and convert to uint8. Timestamps are in raw tick counts. We'll scale to seconds later.
    strobed_index = [ch['name'] for ch in info['event_channels']].index('Strobed')
    strobed_data = PL2.load_event_channel(fp, strobed_index, info)
    if strobed_data is None:
        return result
    for i in range(len(strobed_data["strobed"])):
        strobed_data["strobed"][i] &= 0xFF
    strobed_data["strobed"] = strobed_data["strobed"].astype("uint8")

    # get timestamps for all pulses on XS2
    event2_index = [ch['name'] for ch in info['event_channels']].index('EVT02')
    event2_ts = PL2.load_event_channel(fp, event2_index, info)['timestamps']
    if event2_ts is None:
        return result
    event2_ts = event2_ts.astype('int64')

    # get filename and XS2 start and stop timestamps for each data file successfully saved (character code 0x06). This
    # code uses Numpy array operations to (hopefully) speed up the process
    start_code_mask = strobed_data["strobed"] == 0x02
    stop_code_mask = strobed_data["strobed"] == 0x03
    null_code_mask = strobed_data["strobed"] == 0x00
    start_code_indices = np.where(start_code_mask)[0]

    # helper function used to find, eg, the stop code character after a start code character
    def find_next(mask: np.ndarray, after: int, code_desc: str):
        for _i in range(after + 1, len(mask)):
            if mask[_i]:
                return _i
        raise Exception(f"Missing {code_desc} in Omniplex strobed character data")

    for start_code_index in start_code_indices:
        first_null_index = find_next(null_code_mask, start_code_index, 'null terminator')
        second_null_index = find_next(null_code_mask, first_null_index+1, 'null terminator')
        stop_code_index = find_next(stop_code_mask, second_null_index, 'trial stop character')
        file_name = "".join([chr(code) for code in strobed_data['strobed'][first_null_index + 1:second_null_index]])
        file_was_saved = (any(strobed_data["strobed"][second_null_index + 1:stop_code_index] == 0x06))
        if file_was_saved:
            start_code_ts = int(strobed_data['timestamps'][start_code_index])
            stop_code_ts = int(strobed_data['timestamps'][stop_code_index])
            xs2_indices = np.where((event2_ts >= start_code_ts) & (event2_ts < stop_code_ts))[0]
            if len(xs2_indices) < 2:
                raise Exception(f"Missing trial start or stop pulse on XS2 for saved file: {file_name}")
            xs2_start_ts = event2_ts[xs2_indices[0]]
            xs2_stop_ts = event2_ts[xs2_indices[-1]]
            if (xs2_start_ts - start_code_ts)/timestamp_frequency > 0.100:
                raise Exception(f"XS2 start pulse is more than 100ms after start code for saved file: {file_name}")
            result[file_name] = (float(xs2_start_ts)/timestamp_frequency, float(xs2_stop_ts)/timestamp_frequency)

    return result


@dataclass
class _TrialInfo:
    """
    A data container to accumulate information about each trial presented during an experiment session during the
    pre-processing phase of the session commit workflow: (1) the MD5 hash digest that uniquely identifies the trial
    protocol presented (see maestro.Protocol), and (2) timing information used to determine the order in which trials
    were presented during the experiment and to align spike times of neural units recorded on the Omniplex system with
    respect to the timeline of the Maestro trials in which behavioral response data is recorded.

    Behavior-only experiments have no Omniplex data. For these sessions, we rely only on the internal timestamps to
    determine the trial order. If those timestamps are unavailable, then we rely on the file indices. When the Omniplex
    data is available, it is the start/stop times as recorded on the Omniplex that determine both the trial presentation
    order and the conversion of neural unit spike times to the individual Maestro trial timelines.
    """
    file_index: int
    """ The trial data file's 4-digit numeric string extension converted to an integer."""
    duration: float
    """ The trial duration in seconds, as culled from the data file header."""
    header_timestamp: Optional[int]
    """ The internal timestamp found in the data file header, in ms since Maestro started. Will be None for data files
    prior to version 21. """
    omniplex_start: Optional[float] = None
    """ The Omniplex timestamp for the XS2 pulse delivered at the start of the trial, in seconds since the Omniplex 
    recording began. Will be None for behavior-only experiment sessions."""
    omniplex_stop: Optional[float] = None
    """ The Omniplex timestamp for the XS2 pulse delivered at the end of the trial, in seconds since the Omniplex
    recording began. Will be None for behavior-only experiment sessions. """
    proto_hash: Optional[str] = None
    """ The MD5 hash digest identifying the trial protocol presented."""


@dataclass
class OmniplexUnit:
    """
    Data object containing information that will be stored in the Session.Neuron part table in the lab database for each
    identified neural unit in an Omniplex recording session. The Omniplex source filename, channel ID, and spike
    timestamps for each unit are extracted from the spike-sort results file that must be included in the session data
    archive when committing an experiment session to the database. Other metrics are computed from the original Omniplex
    analog data stream from which the unit spike times were "sorted".
    """
    source_file: str
    """ The name of the Omniplex PL2 file containing the analog data for the neural unit. """
    channel: str
    """ The Omniplex source channel name, which consists of the tag 'WB' (wide-band channel) or 'SPKC' (narrow-band
    channel) followed by a 2-digit number."""
    spike_times: np.ndarray
    """ 1D Numpy array holding the sorted spike times in seconds since the start of the Omniplex recording."""
    firing_rate: float
    """ Mean firing rate in Hz (computed from spike times array). """
    snr: float
    """ Signal-to-noise ratio (computed from spike times array and original analog data stream. """
    template: np.ndarray
    """ Average spike template waveform (computed by averaging 10-ms clips of filtered analog channel stream starting
    1ms before each timestamp in the spike times array). Units = micro-volts. """
    neuron_type: Optional[int] = None
    """ ID of the neuron type associated with this unit (value of primary key in NeuronType table). """
