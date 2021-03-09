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
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Set, Union, List
import threading
from json import JSONDecoder
import numpy as np
import datajoint as dj

from common import json_parse, DocEnum
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


AttributeValue = Union[str, int, float, date, np.ndarray, bytes]
""" Database attribute value type. """


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

    def _log_session_commit(self, user: str, subject: str, session_date: str, suffix: int) -> Optional[str]:
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
            rows = table.proj(attributes).fetch(as_dict=True)
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

    def insert_into_table(self, table_id: DBTable, row: Dict[str, AttributeValue]) -> Optional[str]:
        """
        Insert an entry into a specified table in the laboratory database, and log the change in the repository
        database updates log. If the log update fails after insertion, the insertion is rolled back to maintain
        consistency between the database and the backup repository.

        Args:
            table_id: ID of database table.
            row: The new entry.

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
                err_msg = self._log_add_table_row(table_id, row)
                if err_msg:
                    raise Exception(err_msg)
        except Exception as e:
            return f"Insert failed: table={str(table_id)}, value={row} ===> {str(e)}"
        return None

    def delete_from_table(self, table_id: DBTable,
                          restriction: Optional[Dict[str, AttributeValue]] = None) -> Optional[str]:
        """
        Delete one or more rows from a specified table in the laboratory database, and log the change in the repository
        database updates log. If the log update fails after deletion, the deletion is rolled back to maintain
        consistency between the database and the backup repository.

        Args:
            table_id: ID of database table
            restriction:  A dictionary of attribute ID-value pairs that describes the row or rows to delete. If this is
                None, the entire contents of the table are deleted!

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
                err_msg = self._log_delete_from_table(table_id, restriction)
                if err_msg:
                    raise Exception(err_msg)
        except Exception as e:
            return f"Delete failed: table={str(table_id)}, restrict={restriction} ===> {str(e)}"
        return None

    def update_xref_table(self, map_table_id: DBTable, src_pk: str, src_pk_val: int,
                          dst_pk: str, map_set: Set[int]) -> Optional[str]:
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
                err_msg = self._log_mapping_table_update(map_table_id, xref_rows)
                if err_msg:
                    raise Exception(err_msg)
        except Exception as err:
            error_msg = f"Failed to update cross-reference table {str(map_table_id)}: {str(err)}"
        return error_msg
