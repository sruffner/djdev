"""
manager.py: Manage access to the Lisberger lab database and backing repository.

This module is the heart of the backend server for the Lisberger lab database web portal. The Dash-based front-end
relies on this module to access and make any changes to the database and its backing repository.

All database operations go through the singleton DataBaseManager object defined in this module: fetching contents,
adding or removing rows from any database table, uploading and committing an experiment session's worth of data to the
database, recording all changes in log file(s) in the backing repository so that the database can be reconstructed in
the event of a catastrophic failure.

IMPORTANT: Multiple threads will access DataBaseManager methods (the app server uses multiple threads to handle
multiple client requests, even multiple requests from the same client). That means multiple threads could try to
access the DataJoint-administered database at the same time. Experience has shown that the DataJoint framework with
the underlying MySQL database is NOT thread-safe. Multiple threads calling into the same DataBaseManager method resulted
in internal packet communication errors within the PYMYSQL package. For this reason, I've added a Lock to guard any
DataBaseManager code that queries or modifies the database via DataJoint.

==> Session commits.

Committing an experiment session to the database requires a multi-stage, user-interactive procedure. Multiple clients
could start a session commit workflow at the same time, so DataBaseManager manages a limited pool of worker threads
that handle long-running tasks in the commit procedure: extracting files from the uploaded session data ZIP archive,
processing the data files to collect the information that is stored in the database, and finally committing the trial
behavioral and neural data to the database. As a shared resource, it maintains no state that is specific to a particular
session commit in progress. Rather, per-commit state information is maintained by the worker thread handling the commit
process. For details see the worker thread class ProcessArchiveThread.

The commit process will take an extended period of time, and the user could leave and return to the process for any
number of reasons. On the server side, uncommitted session data will be eventually expunged by a daemon. Thus, every
client request to the server must include a "task ID" that identifies the particular commit process to which that
request applies. The task ID is a string 'session-<uuid>', '<uuid>' is a random universally unique identifier assigned
to the commit task, converted to a string in standard form. When the client initiates a new commit task, the server
generates the task ID and creates a "staging" directory -- $DJDEV_ROOT_REPO/staging/<task_id> -- to which the session
data archive is uploaded and then processed.

To commit data from an experiment to the Lisberger lab database, the researcher must compress all of the session data
files into a single ZIP archive. For those experiments that include electrophysiological recordings with the Omniplex
system, the following data files must be present in the archive.

    1) All Maestro trial data files.
    2) A single pickle file (other formats may be supported in the future) containing the results of the researcher's
       own spike-sorting analysis.
    3) One or more Omniplex PL2 files containing the original Omniplex-recorded data from which the sorted spike trains
       were derived. (We hope to support the older Plexon MAP files in the future.)

The pickle file is identified by the extension '.pickle' or '.pkl', and there must be only one such file in the archive.
It must contain a single dictionary with 2 or 3 keys: 'channel' is a List[str] where the N-th element is the name of the
Omniplex analog source channel (wide-band "WB" or narrow-band "SPKC" only!) on which a neural unit was recorded,
'filename' is a List[str] where the N-the element is the name of the PL2 file in which the neural unit was recorded
(this field must be present ONLY if the spike-sorted units were derived from multiple PL2 files, all of which must be in
the archive), and 'spiketimes' is a List[] where the N-th element is a Numpy array containing the spike timestamps for
that neural unit. The float-valued timestamps are in seconds since the start of the Omniplex recording.

Here is a summary of the commit process:

    Stage 1: Session commit not started. Client must send a request to start a commit. In response, the server generates
        a task ID, creates the staging directory for the commit, and spawns a worker thread dedicated to it.
    Stage 2: Upload and pre-processing of session data archive. The "chunked" file upload is handled by a dash-uploader
        component, independent of the worker thread, which merely monitors the upload progress by checking the contents
        of the staging directory. If the upload fails to start or stalls for more than 10 minutes, the worker thread
        deletes the staging directory and terminates. Once the ZIP archive has been uploaded, the worker thread begins
        pre-processing its contents. All Maestro trial data files are examined to find the set of trial protocols
        presented during the experiment session. The pickle file containing information about identified neural units is
        processed. Any and all PL2 files are processed to get the Omniplex start and stop timestamps for every trial
        data file in the archive, and to calculate metrics (SNR, firing rate, 10ms average spike waveform template) for
        each identified neural unit. The results are stored in a separate pickle file, 'preprocessing.pickle', in the
        staging directory. General session information such as session date, subject, etc may be "guessed" by analyzing
        the session data. During pre-processing, the client merely polls the server for progress updates and displays
        new progress messages to the user.
    Stage 3: Review and edit. In this stage, the user reviews the results of the previous stage and provides some
        additional information required to commit the session to the database (info for the Session table and its
        Session.EPhys and Session.Neuron part tables). Client requests retrieve information to be presented on the front
        end, such as trial protocols and identified neural units. On the server side, the worker thread is essentially
        paused waiting for the user's approval to complete the commit process. To proceed to the final stage, any
        missing session metadata must be supplied by the user.
    Stage 4: Commit. In this stage, the worker completes the session commit: (1) the ZIP archive and other supporting
        files are moved from the temporary staging directory to a permanent place within the backing repository; (2) an
        entry for the new session is added to the Session database table (along with appropriate entries in the part
        tables Session.EPhys and Session.Neuron); (3) any new trial protocols are added to the TrialProtocol table; and
        (4) all trials are added to the Trial table (per-trial behavioral and response traces). (5) Lastly, the session
        commit is recorded in the database updates log and the staging directory is removed. In this stage, the client
        merely polls the server for progress updates and displays new progress messages to the user.

In any of the stages 2-4, the client may issue a "cancel" command -- in which case DataBaseManager terminates the worker
thread, deletes the staging directory (or fixes the database and repository if cancelled in the middle of stage 4), and
both server and client return to stage 1.

==> Logging all database changes in the backup repository.

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
the nature of these tables and the two tables they "associate": the associated tables' primary keys are both single
auto-incrementing integer keys. The mapping table has a primary key consisting of the those two foreign keys and nothing
else, and the table has no non-primary attributes.

Summary of the log entry types:
    1) Add row to manual-entry table: {'op': 'add', 'table': DBTable, 'row': Dict}
    2) Delete from manual-entry table: {'op': 'delete', 'table': DBTable, 'restriction': Dict}
    3) Mapping table update: {'op': 'mapping', 'table': DBTable, 'src_pk': int, 'dst_pks': Set[int]}
    4) Session commit: {'op': 'session', 'username': str, 'subj_id': str, 'date': 'YYYY-MM-DD', 'suffix': int}

==> Portal authorized users.

The MySQL server that hosts the lab database also hosts an independent database of authorized portal users. Any
anonymous visitor to the portal will only be able to explore lab data; one must be logged-in with the appropriate
access privileges to commit experimental data, curate information in the lab's manual tables, manage the portal user
database, or download datasets. For details on the access levels, see sgl_auth.py.

Since DataJoint's persistent connection with the MySQL server is not thread-safe, it is important that any queries to
the portal user database be protected by a thread lock. Since DataBaseManager already implements that mechanism, we
decided to use it to implement access to the user database.

@author: sruffner
@created: 03mar2021
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import os
import pickle
import re
import sys
from copy import deepcopy
import dash_bootstrap_components as dbc
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from queue import Queue
from typing import Optional, Dict, Any, Set, List, IO, Tuple, Union
import threading
import time
import shutil
import uuid
import zipfile
from json import JSONDecoder
import numpy as np
import scipy.signal
import datajoint as dj

from common import json_parse, check_date

import database.table_info as ti
from database.table_info import DBTable, AttributeValue, AttrTypeEnum
import database.maestro as maestro
import database.PL2 as PL2
import database.sgl_schema as sgl
import database.sgl_auth as sgl_auth


_table_map: Dict[DBTable, dj.Table] = {
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
    DBTable.SESSION_NEURON: sgl.Session.Neuron(),
    DBTable.TRIAL_PROTOCOL: sgl.TrialProtocol(),
    DBTable.TRIAL: sgl.Trial(),
    DBTable.TRIAL_EVENT: sgl.Trial.Event(),
    DBTable.TRIAL_BEHAVIORAL: sgl.Trial.BehavioralResponse(),
    DBTable.TRIAL_NEURONAL: sgl.Trial.NeuronalResponse()
}
""" Maps enumerated database table ID to the corresponding DataJoint table class in the Lisberger lab schema. """


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
            self._db_lock: threading.Lock = threading.Lock()
            """ Lock object guarding access to the database itself. """
            self._session_commit_lock: threading.Lock = threading.Lock()
            """ Lock object used to queue the session commit tasks. """

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

        THIS METHOD IS INTENDED ONLY FOR USE DURING DEVELOPMENT, so that we can populate some of the manual tables with
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

    def _log_mapping_table_update(self, table_id: DBTable, src_pk_val: int, map_set: Set[int]) -> Optional[str]:
        error_msg: Optional[str] = None
        try:
            with self._log_lock:
                DataBaseManager._ensure_logs_directory_exists()
                with open(DataBaseManager._log_file_path(), 'ab') as file:
                    pickle.dump({'op': 'mapping', 'table': table_id, 'src_pk': src_pk_val, 'dst_pks': map_set}, file)
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

    def authenticate_portal_user(self, username: str, password: str) -> Optional[str]:
        """
        Authenticate the user account on the Lisberger lab portal with the specified name and password.

        Args:
            username: The username for the account.
            password: The (plaintext) password for the account.
        Returns:
            None if account was authenticated; else a brief error description (invalid username, etc.)
        """
        with self._db_lock:
            error_msg = sgl_auth.authenticate_user(username, password)
        return error_msg

    def get_portal_user_record(self, username: str) -> Union[str, Dict[str, str]]:
        """
        Retrieve a user account record from the database of users authorized for restricted access to the Lisberger lab
        data portal.

        Args:
            username: Username of the account.
        Returns:
            If successful, returns the user account record as a dictionary of key-value pairs. For security reasons, the
            user's encrypted password is removed from the record. Otherwise, returns a brief error description.
        """
        with self._db_lock:
            user_record = sgl_auth.get_user(username)
        return user_record

    def entry_form(self, table_id: DBTable, include_attrs: Optional[List[str]] = None,
                   initial_entry: Optional[Dict[str, AttributeValue]] = None,
                   alert_id: Optional[str] = None) -> dbc.Form:
        """
        Generate a Dash Bootstrap form that may be used to gather information from the user to add a new entity (row)
        to the specified database table. Each attribute defining a table entity is represented by a form group
        consisting of a label and an input widget appropriate to the attribute's data type:
            1) 'enum': A Bootstrap Select widget populated with the fixed set of options for that attribute.
            2  'bool' : A Bootstrap Select widget with "Yes" (True) and "No" (False) options.
            2) 'fkey' (foreign key): Similar to 'enum', except that the database is queried for the available choices
            for that foreign key.
            3) 'text' (length > 100): A Bootstrap Textarea widget with 2 or 4 rows (depending on max text length).
            4) Otherwise: A Bootstrap Input widget of type 'number', 'email', or 'text'.

        Selected attributes may be omitted from the form (for tables with an auto-incrementing primary key, that key is
        always omitted because it is not user-specified), and initial values may be specified for each attribute. The
        form optionally includes a Bootstrap Alert component in which an error message can be displayed when the user
        enters an invalid value in the form.

        So that you can use the input widgets on the form in a Dash callback, the 'id' of each widget is set to
        "<attr.id>-input", where <attr.id> is the ID of the table attribute displayed/edited in that widget.

        Args:
            table_id: ID of the database table.
            include_attrs: If None, the form will include all table attributes on the form, with these exceptions:
                auto-incrementing primary key (value controlled by database), any blob-valued attribute (not supported),
                and, for a part table, any attribute that is part of the master table's primary key. Otherwise, only the
                attributes identified in this list  -- that are indeed valid attributes of the table and are not among
                the exceptions above -- are exposed on the form.
            initial_entry: If not None, this dictionary contains initial values for the attributes, keyed by attribute
                ID. If present, it must contain a key-value pair for each table attribute that is included on the form.
            alert_id: If not None, this is the ID assigned to the Alert component included along the bottom of the form;
                otherwise, no Alert component is generated.

        Returns:
            A Dash Bootstrap Form component, as described.

        Raises:
            KeyError: If table ID is invalid or identifies a table that does not support form-based user entry; if
                any attribute ID specified is invalid; or if any attribute ID is missing in initial_entry.
        """
        if not ti.allows_form_entry(table_id):
            raise KeyError(f"Entry form not supported for the database table {str(table_id)}.")
        form_groups = []
        for attr_id in ti.attributes_of(table_id):
            if include_attrs and not (attr_id in include_attrs):
                continue
            attr_info = ti.attribute_info(table_id, attr_id)
            if (attr_info.type == AttrTypeEnum.AUTO) or (attr_info.type == AttrTypeEnum.BLOB):
                continue
            elif attr_info.type == AttrTypeEnum.ENUM:
                entry_widget = dbc.Select(
                    id=f"{attr_id}_input",
                    options=[{"label": opt, "value": opt} for opt in attr_info.options],
                    value=initial_entry[attr_id] if initial_entry else attr_info.options[0]
                )
            elif attr_info.type == AttrTypeEnum.BOOL:
                entry_widget = dbc.Select(
                    id=f"{attr_id}_input",
                    options=[{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                    value=(1 if initial_entry[attr_id] else 0) if initial_entry else 0
                )
            elif attr_info.type == AttrTypeEnum.FKEY:
                entry_widget = dbc.Select(
                    id=f"{attr_id}_input",
                    options=[{"label": opt[0], "value": opt[1]}
                             for opt in self.foreign_key_choices(table_id, attr_id)],
                    value=initial_entry[attr_id] if initial_entry else None
                )
            elif (attr_info.type == AttrTypeEnum.TEXT) and attr_info.textrange and (attr_info.textrange[1] > 100):
                entry_widget = dbc.Textarea(
                    id=f"{attr_id}_input",
                    minLength=attr_info.textrange[0], maxLength=attr_info.textrange[1],
                    rows=2 if attr_info.textrange[1] < 400 else 4,
                    value=initial_entry[attr_id] if initial_entry else "",
                    placeholder=attr_info.placeholder
                )
            else:
                if (attr_info.type == AttrTypeEnum.FLOAT) or (attr_info.type == AttrTypeEnum.INT):
                    input_type = 'number'
                else:
                    input_type = 'email' if 'email' in attr_id else 'text'
                entry_widget = dbc.Input(
                    id=f"{attr_id}_input",
                    type=input_type,
                    minLength=attr_info.textrange[0] if attr_info.textrange else 0,
                    maxLength=attr_info.textrange[1] if attr_info.textrange else 100,
                    value=initial_entry[attr_id] if initial_entry else "",
                    placeholder=attr_info.placeholder
                )

            form_groups.append(dbc.FormGroup(
                [
                    dbc.Label(attr_info.label, width=2),
                    dbc.Col(entry_widget, width=10)
                ],
                row=True,
            ))

        # alert raised when an add operation fails - displays a brief error message. Otherwise hidden.
        if alert_id:
            form_groups.append(dbc.FormGroup(
                dbc.Alert("", id=f"{alert_id}", dismissable=True, duration=10000, fade=True, is_open=False)
            ))

        return dbc.Form(form_groups)

    def foreign_key_choices(self, table_id: DBTable, fkey_id: str) -> List[Tuple[str, AttributeValue]]:
        """
        Retrieve the list of available choices for the specified foreign key attribute in the specified table. To
        support using this list in a user-facing dropdown or list widget, each "choice" is represented by 2-tuple
        (label, fkey_value), where fkey_value is the actual value of the foreign key and label is a unique user-facing
        string identifying that value.

        Args:
            table_id: ID of database table.
            fkey_id: ID of foreign key attribute.

        Returns:
            List of all available value choices for the foreign key table attribute, with companion label as described.
                Sorted alphabetically by the label. Returns an empty list if unable to access the database.

        Raises:
            KeyError: If table_id is invalid, if fkey_id is invalid or is not a foreign key attribute.
        """
        attr_info = ti.attribute_info(table_id, fkey_id)
        if attr_info.type != AttrTypeEnum.FKEY:
            raise KeyError(f"{fkey_id} is not a foreign key attribute!")

        # for select parent tables, we sort on a user-facing attribute rather than the primary key
        if attr_info.fkey_table == DBTable.STUDY:
            studies = sorted(self.fetch_proj(DBTable.STUDY, ['study_id', 'study_title']),
                             key=lambda study: study['study_title'])
            return [] if len(studies) == 0 else [(study['study_title'], study['study_id']) for study in studies]
        elif attr_info.fkey_table == DBTable.BRAIN_AREA:
            regions = sorted(self.fetch_proj(DBTable.BRAIN_AREA, ['ba_id', 'ba_name']),
                             key=lambda region: region['ba_name'])
            return [] if len(regions) == 0 else [(region['ba_name'], region['ba_id']) for region in regions]
        elif attr_info.fkey_table == DBTable.NEURON_TYPE:
            n_types = sorted(self.fetch_proj(DBTable.NEURON_TYPE, ['nt_id', 'nt_name']),
                             key=lambda n_type: n_type['nt_name'])
            return [] if len(n_types) == 0 else [(n_type['ba_name'], n_type['ba_id']) for n_type in n_types]

        fkey_values = sorted(self.fetch_attribute_values(attr_info.fkey_table, attr_info.fkey_id))
        return [(v, v) for v in fkey_values]

    def num_table_rows(self, table_id: DBTable,
                       restriction: Optional[List[Union[str, Dict[str, AttributeValue]]]] = None) -> int:
        """
        Get the current number of entities in the specified database table.

        Args:
            table_id: ID of database table.
            restriction: If not None, then this argument specifies a list of restriction conditions; each condition is
                specified either as a dictionary of attribute ID-value pairs, or as a string (see DataJoint docs). The
                result reflects the number of rows in the table that satisfy ALL conditions. The restriction conditions
                are not checked for validity.
        Returns:
            Number of rows in the table that satisfy the conditions specified (if any). Returns 0 if unable to access
            database, if specified table does not exist, or if a restriction condition is specified that includes an
            attribute not defined on the table (or if the table has no rows satisfying that condition).
        """
        try:
            table: dj.Table = _table_map[table_id]
            query = (table & dj.AndList(restriction)) if isinstance(restriction, list) else table
            with self._db_lock:
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
            table: dj.Table = _table_map[table_id]
            _restriction = None if not restriction else \
                {attr: restriction[attr] for attr in ti.attributes_of(table_id, False) if attr in restriction}
            query = (table & _restriction) if _restriction else table
            with self._db_lock:
                attr_values = list(query.fetch(attr_id))
        except Exception:
            attr_values = []
        return attr_values

    def fetch_proj(self, table_id: DBTable, attributes: List[str], restriction:
                   Optional[List[str], Dict[str, AttributeValue]] = None) -> List[Dict[str, AttributeValue]]:
        """
        Fetch selected attributes (aka, columns) from the specified table.

        Args:
            table_id: ID of database table.
            attributes: Tuple of attribute IDs identifying the subset of table attributes to fetch. The table's
                primary-key attributes will be included in the result, even if they are omitted from this tuple.
            restriction: If not None, then this argument specifies restriction conditions -- either as a dictionary of
                attribute ID-value pairs, or as a list of string conditions (see DataJoint docs). Either way, the result
                reflects the number of rows in the table that satisfy ALL conditions. The restriction conditions are not
                checked for validity.
        Returns:
            The requested table contents. Each element in the list is a dictionary of attribute ID-value pairs. Each
                dictionary will include only primary key attributes plus any other attributes identified in the
                'attributes' argument. Returns an empty list if the table is empty or a database error occurs.
        """
        try:
            table: dj.Table = _table_map[table_id]
            query = (table & restriction) if isinstance(restriction, dict) else \
                ((table & dj.AndList(restriction)) if isinstance(restriction, list) else table)
            with self._db_lock:
                rows = query.proj(*attributes).fetch(as_dict=True)
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
            table: dj.Table = _table_map[table_id]
            query = (table & restriction) if restriction else table
            with self._db_lock:
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
        if not (table_id in _table_map):
            return f"Unrecognized database table ID: {str(table_id)}"
        try:
            table: dj.Table = _table_map[table_id]
            with self._db_lock, table.connection.transaction:
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
        if not (table_id in _table_map):
            return f"Unrecognized database table ID: {str(table_id)}"
        try:
            table: dj.Table = _table_map[table_id]
            with self._db_lock, table.connection.transaction:
                query = (table & restriction) if restriction else table
                query.delete()
                if log:
                    err_msg = self._log_delete_from_table(table_id, restriction)
                    if err_msg:
                        raise Exception(err_msg)
        except Exception as e:
            return f"Delete failed: table={str(table_id)}, restrict={restriction} ===> {str(e)}"
        return None

    def row_exists(self, table_id: DBTable, row_pk: Dict[str, AttributeValue]) -> bool:
        """
        Does the specified entry/row currently exist in the specified table?

        Args:
            table_id: ID of the database table.
            row_pk: This dictionary must contain, at a minimum, the primary key attribute ID-value pairs that uniquely
                identify a single table row. Any other attributes are ignored!

        Returns:
            True if row exists, false otherwise.

        Raises:
            ValueError: If row_pk is missing any of the table's primary key attributes.
        """
        table_pk = ti.primary_key_of(table_id, False)
        try:
            restriction = {key: row_pk[key] for key in table_pk}
            exists = (self.num_table_rows(table_id, [restriction]) == 1)
        except KeyError:
            raise ValueError("Incomplete primary key")
        return exists

    def check_row(self, table_id: DBTable, row: Dict[str, AttributeValue], omit_master: bool = False) -> Optional[str]:
        """
        Check whether or not the proposed row entry is valid and does not yet exist in the specified database table.

        Args:
            table_id: ID of the database table.
            row: The proposed entry. It must contain a valid attribute value for each table attribute -- except for an
                auto-incrementing primary key, and it must not yet exist in the database. SIDE EFFECT: If the entry
                includes a value for the auto primary key, that key-value pair is removed from the dictionary.
            omit_master: If True and this is a part table, attributes in 'row' that are part of the master table's
                primary key are NOT checked, and existence is not checked. This is a way to check a new entry in the
                part table without first inserting the corresponding entry in the master table. Default is False.
        Returns:
            None if operation succeeds, else a user-facing description of the error (bad table ID, missing attribute,
                bad attribute value, entry already exists, database error).
        """
        err_msg = None
        try:
            self._validate_row(table_id, row, omit_master)
        except (Exception, ValueError) as err:
            err_msg = f"Invalid entry: {str(err)}"
        return err_msg

    def _validate_row(self, table_id: DBTable, row: Dict[str, AttributeValue], omit_master: bool = False) -> None:
        """
        Validate an entry (aka, row) that is to be inserted into the database table specified.

        Args:
            table_id: ID of the database table.
            row: The new entry. NOTE: If the table uses an auto-incrementing attribute as its primary key, that
                attribute is removed from the entry, if specified. Its value is set by the database on insert.
            omit_master: If True and the specified table is a part table, attributes in 'row' that are part of the
                parent table's primary key are NOT checked, and existence is not checked. This is a way to check a new
                entry in the part table without first inserting the corresponding entry in the master table. Default is
                False.

        Raises:
            Exception: If the proposed entry already exists in table, or if entry is missing any attribute value.
            ValueError: If any attribute value is invalid.
        """
        # we never check existence when the table uses an auto-incrementing PK!
        if not (ti.has_auto_primary_key(table_id) or omit_master):
            if self.row_exists(table_id, row):
                raise Exception("Attempt to add a new entry with an existing primary key")
        for attr_id in ti.attributes_of(table_id, omit_master):
            attr_info = ti.attribute_info(table_id, attr_id)
            if attr_info.type != AttrTypeEnum.AUTO:
                if attr_id not in row:
                    raise Exception(f"Missing attribute: {attr_id}")
                self._validate_attribute_value(table_id, attr_id, row[attr_id])
            elif attr_id in row:
                row.pop(attr_id, None)

    def _validate_attribute_value(self, table_id: DBTable, attr_id: str, attr_value: AttributeValue) -> None:
        """Validate the proposed value for an attribute in the underlying table.

        Validation of the attribute value depends on the attribute type, AttrTypeEnum:
            TEXT: The value must satisfy any regular expression defined for the attribute (if any), as well as the
                min/max restriction on text length.
            FLOAT: Can be str, int or float, but a string value must be parsable as a float. If string, it must
                satisfy min/max restriction on text length.
            INT: Can be str or int, but a string value must be parsable as an integer. If string, it must satisfy
                min/max restriction on text length.
            DATE: Can be a date or string. A string value must satisfy the format 'YYYY-MM-DD'. The date must be
                after 12/31/1899 and before today.
            BOOL: Can be a number, or boolean. A non-zero number is considered True.
            ENUM: Value must be one of the valid options for the attribute.
            BLOB: The value must be a Numpy array or a bytes array. Its value is not otherwise checked.
            FKEY: The attribute value must identify an existing entity in the parent table.
            AUTO: An auto-incrementing PK. This type of attribute is ignored. Its value is set by the database on
                insert, NOT by the user.

        Args:
            table_id: ID of the database table.
            attr_id: The attribute ID.
            attr_value: The proposed value for the attribute.

        Raises:
            ValueError: If the proposed attribute value is not valid in any way. The error description is intended to
            provide a user-facing description of the problem.
        """
        attr_info = ti.attribute_info(table_id, attr_id)
        if not isinstance(attr_value, (str, bool, int, float, date, np.ndarray, bytes)):
            raise ValueError(f"Attribute value is an unsupported data type: '{attr_info.label}'")
        if attr_info.type == AttrTypeEnum.AUTO:
            return
        if attr_info.pkey:
            if isinstance(attr_value, str) and (attr_value == ""):
                raise ValueError(f"Missing value for primary key attribute: '{attr_info.label}'")
        if attr_info.type == AttrTypeEnum.FKEY:
            if not self.attribute_exists(attr_info.fkey_table, attr_info.fkey_id, attr_value):
                raise ValueError(f"Missing foreign key: '{attr_info.label}' = '{str(attr_value)}'")
        elif attr_info.type == AttrTypeEnum.ENUM:
            if not (attr_value in attr_info.options):
                raise ValueError(f"Invalid option for '{attr_info.label}': '{str(attr_value)}'")
        elif attr_info.type == AttrTypeEnum.DATE:
            if not check_date(attr_value):
                raise ValueError(f"'{attr_info.label}': Date is invalid, earlier than 1900-01-01, or in the future.")
        elif attr_info.type == AttrTypeEnum.FLOAT:
            try:
                num_value = float(attr_value)
            except(TypeError, ValueError):
                raise ValueError(f"'{attr_info.label}' = '{attr_value}' cannot be parsed as a floating-point value")
            ti.validate_numeric_attribute_value(table_id, attr_id, num_value)
        elif attr_info.type == AttrTypeEnum.INT:
            try:
                num_value = int(attr_value)
            except(TypeError, ValueError):
                raise ValueError(f"'{attr_info.label}' = '{attr_value}' cannot be parsed as an integer")
            ti.validate_numeric_attribute_value(table_id, attr_id, num_value)
        elif attr_info.type == AttrTypeEnum.BOOL:
            if not isinstance(attr_value, (int, float, bool)):
                raise ValueError(f"'{attr_info.label}' must be a number or boolean value")
        elif attr_info.type == AttrTypeEnum.BLOB:
            if not isinstance(attr_value, (np.ndarray, bytes)):
                raise ValueError(f"'{attr_info.label}' must be a Numpy array or byte string")
        else:  # 'text' or 'email'
            if not isinstance(attr_value, str):
                raise ValueError(f"'{attr_info.label}': Value must be a string")
            if attr_info.textrange:
                min_len, max_len = attr_info.textrange
                if (len(attr_value) < min_len) | (len(attr_value) > max_len):
                    raise ValueError(f"'{attr_info.label}': Value must be {min_len}-{max_len} characters long.")
            if attr_info.regex:
                if re.fullmatch(attr_info.regex, attr_value) is None:
                    raise ValueError(f"Invalid value for {attr_info.label}: {attr_info.regex_hint}")

    def update_mapping_table(self, map_table_id: DBTable, src_pk_val: int, map_set: Set[int],
                             log: bool = True) -> Optional[str]:
        """
        Update an associative mapping stored in a cross-reference table in the laboratory database, and log the change
        in the repository database updates log. If the log update fails, any changes are rolled back to maintain
        consistency between the database and the backup repository.

        Args:
            map_table_id: ID of the cross-reference table.
            src_pk_val: Integer value identifying a row in the source table for the cross-reference. Must exist in
                database or operation will fail.
            map_set: Set of integer primary key values identifying all rows in the destination table that map to the
                specified source entity. All must exist in the destination table or the operation will fail.
            log: If True, the change is logged in the backup updates log. Default = True.

        Returns:
            None if successful; else a user-facing description of the error.
        """
        error_msg = None
        try:
            if not (map_table_id in _table_map):
                raise Exception(f"Unrecognized database table ID: {str(map_table_id)}")
            if not map_table_id.is_mapping_table():
                raise Exception(f"Table is not a cross-reference table!: {str(map_table_id)}")
            map_table: dj.Table = _table_map[map_table_id]
            src_pk = map_table_id.source_key_for_mapping_table()
            dst_pk = map_table_id.destination_key_for_mapping_table()
            xref_rows = [{src_pk: src_pk_val, dst_pk: value} for value in map_set]
            with self._db_lock, map_table.connection.transaction:
                (map_table & {src_pk: src_pk_val}).delete()
                if len(xref_rows) > 0:
                    map_table.insert(xref_rows)
                if log:
                    err_msg = self._log_mapping_table_update(map_table_id, src_pk_val, map_set)
                    if err_msg:
                        raise Exception(err_msg)
        except Exception as err:
            error_msg = f"Failed to update cross-reference table {str(map_table_id)}: {str(err)}"
        return error_msg

    def trial_protocols_for_neuron(self, neuron_key: Dict[str, AttributeValue], aggregate: bool = False) \
            -> Optional[Dict[str, str]]:
        """
        Get all trial protocols presented to the specified neural unit.

        Args:
            neuron_key: At a minimum, this dictionary must uniquely identify a recorded neural unit in the database.
            aggregate: If True, include ONLY those trial protocols for which the average neural response can be
                computed. By convention, there must be at least 3 reps of the trial protocol for which the neural
                response was recorded, AND the protocol itself either must have NO random variables OR a single segment
                (not necessarily segment 0) of random duration. Default = False.
        Returns:
            A dictionary containing the user-friendly pathname ("set/subset/name") of each trial protocol presented
                while recording the response of the neural unit, keyed by the protocol's MD5 hash digest. If aggregate
                is True, any protocols for which the average neural response CANNOT be computed are excluded; in this
                case, it is possible that the dictionary is empty. The dictionary items are ordered by pathname.
                Returns None if an error occurs.
        """
        try:
            proto_table: dj.Table = _table_map[DBTable.TRIAL_PROTOCOL]
            trial_table: dj.Table = _table_map[DBTable.TRIAL]
            response_table: dj.Table = _table_map[DBTable.TRIAL_NEURONAL]
            neuron_pk = {k: neuron_key[k] for k in ti.primary_key_of(DBTable.SESSION_NEURON, False)}
            with self._db_lock:
                results = (trial_table & (response_table & neuron_pk)).proj(..., '-trial_header').fetch(as_dict=True)
            proto_hashes = {r['proto_hash'] for r in results}
            if aggregate:
                all_reps = [r['proto_hash'] for r in results]
                out = dict()
                for h in proto_hashes:
                    if all_reps.count(h) > 2:
                        proto = self.get_trial_protocol_definition(h)
                        if proto is None:
                            return None
                        elif proto.can_aggregate_responses():
                            out[h] = proto.trial.path_name()
            else:
                restriction = [f"proto_hash = '{h}'" for h in proto_hashes]
                with self._db_lock:
                    protocols = (proto_table & restriction).\
                        proj('proto_name', 'proto_set', 'proto_subset').fetch(as_dict=True)
                out = {p['proto_hash']: f"{p['proto_set']}/{p['proto_subset']}/{p['proto_name']}" for p in protocols}
            sorted_tuples = sorted(out.items(), key=lambda item: item[1])
            return {k: v for k, v in sorted_tuples}
        except Exception:
            return None

    def get_trial_protocol_definition(self, proto_hash: str) -> Optional[maestro.Protocol]:
        """
        Retrieve the definition of the specified Maestro trial protocol definition from the lab database.

        Args:
            proto_hash: The MD5 hash digest that uniquely identifies the requested trial protocol.
        Returns:
            The trial protocol definition. Returns None if an error occurs or the specified protocol not found.
        """
        try:
            proto_table: dj.Table = _table_map[DBTable.TRIAL_PROTOCOL]
            with self._db_lock:
                protocol_entry = (proto_table & {'proto_hash': proto_hash}).fetch1()
            return pickle.loads(protocol_entry['proto_def'])
        except Exception:
            return None

    def trials_for_neuron(self, neuron_key: Dict[str, AttributeValue],
                          proto_hash: Optional[str] = None) -> Optional[List[int]]:
        """
        Get the indices of all trials, or a subset thereof, presented to the specified neural unit.

        Args:
            neuron_key: At a minimum, this dictionary must uniquely identify a recorded neural unit in the database.
            proto_hash: If this identifies a trial protocol in the database, then return only the indices of the trials
                belonging to that protocol. Default = None.
        Returns:
            The list of indices of the relevant trials. The trial index indicates its presentation order during the
                experiment session. The index plus the neural unit's session key is sufficient information to retrieve
                the trial details and response data. Returns an empty list if no relevant trials found. Returns None if
                an error occurs while retrieving the information.
        """
        try:
            trial_table: dj.Table = _table_map[DBTable.TRIAL]
            response_table: dj.Table = _table_map[DBTable.TRIAL_NEURONAL]
            neuron_pk = {k: neuron_key[k] for k in ti.primary_key_of(DBTable.SESSION_NEURON, False)}
            query = (trial_table & (response_table & neuron_pk)).proj('proto_hash')
            with self._db_lock:
                relevant_trials = query.fetch(as_dict=True)
            if isinstance(proto_hash, str):
                return [t['trial_idx'] for t in relevant_trials if t['proto_hash'] == proto_hash]
            else:
                return [t['trial_idx'] for t in relevant_trials]
        except Exception:
            return None

    def data_for_trial(self, trial_key: Dict[str, AttributeValue],
                       unit_ids: Optional[List[int]] = None) -> Optional[TrialData]:
        """
        Retrieve data recorded for a specified trial in the Lisberger lab database.

        Args:
            trial_key: At a minimum, this dictionary must uniquely identify a single trial record in the database.
            unit_ids: Use this argument to request the responses of only selected neural units (identified by their
                integer unit ID). If None, all recorded neural unit responses are retrieved.
        Returns:
            The trial data container. Returns None if trial not found or if an error occurs while retrieving the
                information.
        """
        try:
            trial_table: dj.Table = _table_map[DBTable.TRIAL]
            trial_pk = {k: trial_key[k] for k in ti.primary_key_of(DBTable.TRIAL)}
            trial_behavior: dj.Table = _table_map[DBTable.TRIAL_BEHAVIORAL]
            trial_neuronal: dj.Table = _table_map[DBTable.TRIAL_NEURONAL]

            with self._db_lock:
                trial_info = (trial_table & trial_pk).fetch1()
                behavioral_responses = (trial_behavior & trial_pk).fetch(as_dict=True)
                neuronal_responses = (trial_neuronal & trial_pk).fetch(as_dict=True)
                protocol_table: dj.Table = _table_map[DBTable.TRIAL_PROTOCOL]
                proto_info = (protocol_table & {'proto_hash': trial_info['proto_hash']}).fetch1()

            behavioral_field: Dict[str, np.ndarray] = dict()
            for response in behavioral_responses:
                behavioral_field[response['response_id']] = response['response_trace']
            neuronal_field: Dict[int, np.ndarray] = dict()
            for response in neuronal_responses:
                if (not unit_ids) or (response['unit_id'] in unit_ids):
                    neuronal_field[response['unit_id']] = response['spike_times']
            proto_def: maestro.Protocol = pickle.loads(proto_info['proto_def'])

            trial_data: TrialData = TrialData(
                experimenter=trial_pk['experimenter'],
                subj_id=trial_pk['subj_id'],
                session_date=trial_pk['session_date'],
                session_sfx=trial_pk['session_sfx'],
                trial_idx=trial_pk['trial_idx'],
                protocol=proto_def,
                filename=trial_info['trial_filename'],
                duration_ms=trial_info['trial_dur'],
                record_start_ms=trial_info['trial_record_start'],
                success=trial_info['trial_success'],
                rewarded=trial_info['trial_rewarded'],
                reward1_ms=trial_info['trial_rew1'],
                reward2_ms=trial_info['trial_rew2'],
                vstab_win_len_ms=trial_info['vstab_win_len'],
                timestamp_sec=trial_info['trial_ts'],
                trial_rvs=pickle.loads(trial_info['trial_rvs']),
                behavior=behavioral_field,
                neuronal=neuronal_field
            )
            return trial_data
        except Exception:
            return None

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

    def get_session_info(self, task_id: str) -> Optional[Dict[str, Optional[AttributeValue]]]:
        """
        Get the session information that will be saved in the Session table when the experiment session is eventually
        committed to the lab database. This information is available ONLY during stage 3 of the commit workflow -- after
        pre-processing and before the final commit stage begins. Some session parameters may be inferred by the server
        during pre-processing. On the client side, the user is expected to verify the information and fill in any
        missing parameter values.

        Args:
            task_id: The commit task identifier.
        Returns:
            A dictionary containing the attribute values for a proposed session table entry representing the experiment
                session to be committed, keyed by the attribute IDs. If an attribute value is None, the value must be
                supplied by the user during the stage 3 review. Returns None if the task_id does not identify an
                in-progress commit task, or that task is not currently in stage 3.
        """
        out: Optional[Dict[str, AttributeValue]] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    out = deepcopy(worker.session_info)
        return out

    def get_ephys_info(self, task_id: str) -> Optional[Dict[str, Optional[AttributeValue]]]:
        """
        Get the information about the experiment's electrophysiological recording that is saved in the Session.EPhys
        part table when the experiment session is eventually committed to the lab database. The information is available
        ONLY during stage 3 of the commit workflow -- after pre-processing and before the final commit stage begins.
        Some parameters may be inferred by the server during pre-processing.  the client side, the user is expected to
        verify the information and fill in any missing parameter values.

        Args:
            task_id: The commit task identifier.

        Returns:
             A dictionary containing the attribute values for a proposed Session.EPhys table entry for the experiment
                session to be committed, keyed by the Session.EPhys attribute IDs. If an attribute value is None, the
                value must be supplied by the user during the stage 3 review. Returns None if the task_id does not
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

    def get_protocol_candidate_names(self, task_id: str) -> Optional[List[str]]:
        """
        Get the path names (in the form 'set/subset/trial_name') of all trial protocol candidates detected during
        pre-processing of the session data ZIP archive. Strictly speaking, any protocol for which fewer than 3 trial
        reps were processed -- and which don't match an existing protocol in the database -- are considered "candidates"
        and require user review and verification.

        This information is available ONLY during stage 3 of the commit workflow -- after pre-processing and before the
        final commit stage begins.

        Args:
            task_id: The commit task identifier.

        Returns:
            The list of protocol candidate path names. The list is not sorted, but indicates the order in which the
                protocols were detected in the pre-processing stage. It is unlikely, but theoretically possible, that
                two protocol candidates could have the same path name. A protocol's pathname is prepended with '**' if
                that protocol requires manual user validation. Returns None if the task_id does not identify an
                in-progress commit task or if that task is not currently in stage 3
        """
        out: Optional[List[str]] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    out = [(p.trial.path_name()
                            if (p.user_validated or (p.num_reps > 2) or (p.num_reps == 2 and p.matches_existing))
                            else f"** {p.trial.path_name()}")
                           for p in worker.proto_candidates]
        return out

    def get_protocol_candidate(self, task_id: str, index: int) -> Optional[maestro.ProtocolCandidate]:
        """
        Get the full definition of a trial protocol candidate culled during pre-processing of the session data ZIP
        archive. This information is available ONLY during stage 3 of the commit workflow -- after pre-processing and
        before the final commit stage begins.

        This method, in concert with get_protocol_candidate_names(), provides a mechanism by which the client front-end
        can present a user interface for reviewing each trial protocol candidate.

        Args:
            task_id: The commit task identifier.
            index: The zero-based index of the protocol candidate requested. IMPORTANT: This corresponds to the protocol
                candidate's ordinal position in the list returned by get_protocol_candidate_names().
        Returns:
            The requested trial protocol candidate. Returns None if the task_id does not identify an in-progress commit
                task, if that task is not currently in stage 3, or if 'index' is invalid.
        """
        out: Optional[maestro.ProtocolCandidate] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3 and (0 <= index < len(worker.proto_candidates)):
                    out = worker.proto_candidates[index]
        return out

    def num_protocol_candidates_needing_validation(self, task_id: str) -> Optional[int]:
        """
        Return number of trial protocol candidates culled from the session archive that require validation by the user
        and have not yet been marked as valid (via validate_protocol_candidate()).

        If a protocol candidate is based on a single rep, it is impossible to know if that protocol has any random
        variables. If the candidate is based on two reps and cannot be matched to an existing protocol in the database,
        we are not sufficiently confident we've captured all of the protocol's random variables. In these scenarios, the
        user must manually validate the protocol definition before the session will be committed.

        This method, available only during stage 3 of the commit workflow, indicates how many protocol candidate require
        user validation but have not yet been validated.

        Args:
            task_id: The commit task identifier.
        Returns:
            Number of protocol candidates requiring user validation. Returns None if the task_id does not identify an
            in-progress commit task or if that task is not currently in stage 3.
        """
        out: Optional[int] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    out = sum([1 for p in worker.proto_candidates if not
                               (p.user_validated or (p.num_reps > 2) or (p.num_reps == 2 and p.matches_existing))])
        return out

    def add_random_var_to_protocol_candidate(
            self, task_id: str, index: int, rv: maestro.SegParam) -> Optional[maestro.ProtocolCandidate]:
        """
        Add a random variable to the definition of a trial protocol candidate culled during pre-processing of an
        experiment session.

        When a protocol candidate's definition is based on fewer than 3 trial reps over the course of a session, AND it
        does not match an existing trial protocol in the lab database, the user must validate the definition before
        the protocol and the session can be committed to the database. Part of validation is adding any missing random
        variables that are part of that definition. When only 1 rep is processed, it is impossible to identify any
        random variables; with only 2 reps, it's possible we might miss one.

        Args:
            task_id: The commit task identifier.
            index: The zero-based index of the protocol candidate to update.
            rv: The random variable to be added to the protocol definition.
        Returns:
            The revised protocol candidate definition. Returns None if the task_id does not identify an in-progress
                commit task, if that task is not currently in stage 3, or if the change was unsuccessful.
        """
        out: Optional[maestro.ProtocolCandidate] = None
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3 and (0 <= index < len(worker.proto_candidates)):
                    if worker.proto_candidates[index].add_random_variable(rv):
                        out = worker.proto_candidates[index]
        return out

    def validate_protocol_candidate(self, task_id: str, index: int) -> bool:
        """
        Validate the definition of a trial protocol candidate culled during pre-processing of an experiment session.

        When a protocol candidate's definition is based on fewer than 3 trial reps over the course of a session, AND it
        does not match an existing trial protocol in the lab database, the user must manually add any missing random
        variables in the protocol definition and mark the protocol candidate as valid before the protocol and the
        session can be committed to the database.

        Args:
            task_id: The commit task identifier.
            index: The zero-based index of the protocol candidate to validate.
        Returns:
            True if candidate was marked as validated. Returns False if the task_id does not identify an in-progress
                commit task, if that task is not currently in stage 3, or if the protocol index is invalid.
        """
        out: bool = False
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3 and (0 <= index < len(worker.proto_candidates)):
                    worker.proto_candidates[index].user_validated = True
                    out = True
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
        Update the neuron type ID assigned to one or all neural units identified during pre-processing of the session
        data archive. During stage 3 of the session commit workflow, the user (via the client front-end) must specify
        the neuron type for each identified unit before the session can be committed to the database. The method has no
        effect in any other stage.

        Args:
            task_id: The commit task identifier.
            index: The zero-based index of the neural unit requested. If -1, then the specified neuron type is applied
                to ALL identified units in the session. Otherwise, the operation fails.
            neuron_type: The neuron type ID. This should identify an existing entry in the database's NeuronType table,
                but it is not checked until the session is actually committed to the database in stage 4

        Returns:
            True if successful; False if the task_id does not identify an in-progress commit task, if that task is not
                currently in stage 3, or if the unit index is neither -1 nor identifies a neural unit in the session.
        """
        ok = False
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if (worker.stage == 3) and (index >= -1) and (index < len(worker.units)):
                    if index == -1:
                        for i in range(len(worker.units)):
                            worker.units[i].neuron_type = neuron_type
                    else:
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
            None if successful, else a human-facing error description: Bad task ID, invalid or incomplete session
                metadata (including failure to validate any trial protocol candidates requiring manual validation)
        """
        with self.task_list_lock:
            if task_id in self.running_tasks:
                worker = self.running_tasks[task_id]
                if worker.stage == 3:
                    # validate session and, if applicable, electrophysiology metadata
                    if worker.ephys_info and not ephys_info:
                        error_msg = "Missing electrophysiology metadata for session."
                    else:
                        error_msg = self.check_row(DBTable.SESSION, session_info)
                        if ephys_info and not error_msg:
                            error_msg = self.check_row(DBTable.SESSION_EPHYS, ephys_info, True)
                    # ensure any trial protocol candidates that required validation by user have been validated.
                    if not error_msg:
                        for i, proto_candidate in enumerate(worker.proto_candidates):
                            if (proto_candidate.num_reps < 3) and not \
                                    (proto_candidate.matches_existing or proto_candidate.user_validated):
                                error_msg = f"Trial protocol {i+1} ({proto_candidate.trial.path_name()})" \
                                            f" requires user validation"
                                break
                    # ensure a valid neuron type has been specified for each neural unit
                    if worker.units and (error_msg is None):
                        for i, unit in enumerate(worker.units):
                            if unit.neuron_type is None:
                                error_msg = f"Please select a neuron type for neural unit #{i+1}"
                                break
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

    def finish_commit(self, worker: ProcessArchiveThread) -> Optional[str]:
        """
        This method implements the final stage of the session commit workflow. It is called ONLY on the worker thread
        that was originally spawned to manage the long-running commit task. The final stage is handled within the
        DataBaseManager singleton to (a) ensure that multiple session commit jobs are queued to happen one at a time;
        and (b) to safely share the persistent database connection with other threads servicing requests from other
        clients.

        Committing a pre-processed experiment session to the lab database involves the following steps:

            1) Entries are inserted into the Session, Session.EPhys, and Session.Neuron tables as appropriate, and all
            trial protocols not already in the database are inserted into the TrialProtocol table.

            2) The Trial table and its part tables are populated with data from all the trials presented during the
            session. We post a progress message and check for user cancel roughly once per second during this process.

            3) The ZIP archive is moved to a permanent folder in the lab data repository, and the results from
            pre-processing the session archive, along with session and electrophysiology metadata entered manually by
            the user during the review stage, are saved in a pickle file in the same folder. That folder is
            %REPO_HOME/<username>, where <username> is the experimenter's username in the database. The base filename
            for the .zip and .pickle files is "<subj_id>_<session_date>_<session_sfx>", where <session_date> is in
            ISO format 'YYYY-MM-DD'.

            4) Lastly, the completed session commit is recorded in the database update log. This single log entry (along
            with the archive and pickle file just stored in the data repository) accounts for all of the database
            insertions required to commit the data from the experiment session.

        If an error occurs at any point during the commit, any changes to the database and the file repository are
        unwound before returning.

        Lock objects are used to guard access to the database connection and to this commit procedure itself. If a
        session commit task worker enters the final stage while another is currently executing in this method, the
        former thread will wait until it can acquire the lock to begin its commit. Furthermore, a separate lock must be
        acquired whenever data is inserted into the database.

        Args:
            worker: The worker thread on which the session commit task is executed. This thread object contains all of
                the information needed to update the lab database with the session's worth of data.
        Returns:
            None if successful, in which case the session is fully committed to the database; otherwise an error
                description.
        """
        assert isinstance(worker, ProcessArchiveThread), "Invalid session commit worker"
        assert worker.stage == 4, "Session commit worker must be in stage 4!"

        error_msg: Optional[str] = None
        session_repo_path = DataBaseManager.get_repo_directory_for(worker.session_info['experimenter'])
        base_filename = f"{worker.session_info['subj_id']}_{str(worker.session_info['session_date'])}_" \
                        f"{worker.session_info['session_sfx']}"
        zip_path_in_repo = Path(session_repo_path, f"{base_filename}.zip")
        pickle_path_in_repo = Path(session_repo_path, f"{base_filename}.pickle")
        session_inserted = False
        protocols_added = False
        protocols_to_add: List[Dict[str, Any]] = list()
        protocol_table = sgl.TrialProtocol()
        session_pks = ['experimenter', 'subj_id', 'session_date', 'session_sfx']

        # only one session may be committed to the database at a time!
        worker.msg_q.put_nowait("Waiting in session commit queue...")
        with self._session_commit_lock:
            try:
                # convert all trial protocol candidates to the protocol objects that are added to the database. Then
                # update the individual trial info to include the protocol's unique MD5 hexadecimal digest
                protocols = [maestro.Protocol.from_candidate(c) for c in worker.proto_candidates]
                for _, t_info in worker.trial_info.items():
                    protocol = protocols[t_info.proto_index]
                    t_info.proto_hash = protocol.md5_digest

                # only insert new trial protocols -- keep track of what's inserted so we can rollback on failure
                existing_proto_map = {pk['proto_hash']: 1
                                      for pk in DataBaseManager().fetch_proj(DBTable.TRIAL_PROTOCOL, ['proto_hash'])}
                for protocol in protocols:
                    if protocol.md5_digest not in existing_proto_map:
                        protocol_entry: Dict[str, Any] = dict()
                        protocol_entry['proto_hash'] = protocol.md5_digest
                        protocol_entry['proto_name'] = protocol.trial.name
                        protocol_entry['proto_set'] = \
                            "" if (protocol.trial.set_name is None) else protocol.trial.set_name
                        protocol_entry['proto_subset'] = \
                            "" if (protocol.trial.subset_name is None) else protocol.trial.subset_name
                        protocol_entry['proto_def'] = pickle.dumps(protocol)
                        protocols_to_add.append(protocol_entry)

                worker.msg_q.put_nowait(f"Inserting session entry and any new trial protocols into database...")
                session_table = sgl.Session()
                with self._db_lock, session_table.connection.transaction:
                    session_table.insert1(worker.session_info, replace=False)
                    session_inserted = True
                    if worker.ephys_info is not None:
                        # need primary key of parent table for insertion into part table
                        for pk in session_pks:
                            worker.ephys_info[pk] = worker.session_info[pk]
                        sgl.Session.EPhys().insert1(worker.ephys_info, replace=False)

                        neurons: List[Dict[str, Any]] = list()
                        for i, unit in enumerate(worker.units):
                            neuron = dict()
                            for pk in session_pks:
                                neuron[pk] = worker.session_info[pk]
                            neuron['unit_id'] = i + 1
                            neuron['unit_channel'] = unit.channel
                            neuron['unit_type'] = unit.neuron_type
                            neuron['unit_rate'] = unit.firing_rate
                            neuron['unit_spikes'] = len(unit.spike_times)
                            neuron['unit_snr'] = unit.snr
                            neuron['unit_template'] = unit.template
                            neurons.append(neuron)
                        sgl.Session.Neuron().insert(neurons, replace=False)
                    if len(protocols_to_add) > 0:
                        protocol_table.insert(protocols_to_add, replace=False)
                        protocols_added = True

                if worker.is_cancelled():
                    raise Exception("Operation cancelled.")

                # in order to monitor progress and check for user cancel while populating trials, the Trial table class
                # relies on a TrialProducer delegate to handle the task from within its make() call. Actual insertions
                # happen in insert_trials_for_session(). NOTE: Since auto-populate cycle is wrapped in a transaction,
                # any trial insertions will be unwound if an exception occurs while populating.
                trial_table = sgl.Trial()
                producer = _SessionTrialProducer(worker.zip_path, worker.trial_info, protocols, worker.units,
                                                 worker=worker)
                trial_table.set_trial_producer(producer)
                trial_table.populate()
                trial_table.set_trial_producer(None)

                if worker.is_cancelled():
                    raise Exception("Operation cancelled.")

                worker.msg_q.put_nowait(f"Saving session archive and pre-processing results to data repository...")
                if not session_repo_path.is_dir():
                    session_repo_path.mkdir(parents=True)
                worker.zip_path.replace(zip_path_in_repo)
                results = {'protocols': protocols, 'trials': worker.trial_info, 'units': worker.units,
                           'session': worker.session_info, 'ephys': worker.ephys_info}
                with open(pickle_path_in_repo, 'wb') as file:
                    pickle.dump(results, file)

                # finally, log the session commit
                res = self.log_session_commit(
                    worker.session_info['experimenter'], worker.session_info['subj_id'],
                    str(worker.session_info['session_date']), worker.session_info['session_sfx'])
                if res:
                    raise Exception(res)

            except Exception as err:
                error_msg = f"Error - Failed to commit session:\n  {str(err)}"
            finally:
                # unwind all changes if the commit failed! Note that trials are automatically removed when session is.
                if error_msg:
                    try:
                        with self._db_lock:
                            if session_inserted:
                                (sgl.Session() & worker.session_info).delete()
                            if protocols_added:
                                restriction = [f"proto_hash = '{p['proto_hash']}'" for p in protocols_to_add]
                                (protocol_table & restriction).delete()
                    except Exception:
                        pass  # TODO: We really have to kill the database at this point, because it is inconsistent.
                    zip_path_in_repo.unlink(missing_ok=True)
                    pickle_path_in_repo.unlink(missing_ok=True)

        return error_msg

    def batch_insert_trials(
            self, trials: List[Dict[str, AttributeValue]], behavioral_entries: List[Dict[str, AttributeValue]],
            neuronal_entries: List[Dict[str, AttributeValue]], events: List[Dict[str, AttributeValue]]) -> None:
        """
        Batch-insert trial response data during a session commit. This helper method is called during a session
        commit to insert trial information and response data into the database's Trial table and its part tables.
        The batch insert is guarded by a lock object to ensure that only a single thread uses the database connection
        at one time. The insertions are not logged, since a single log entry for the commit task is recorded once the
        commit is completed.

        This method should be invoked by a _SessionTrialProducer() object in one of two contexts: (1) On a worker thread
        during an interactive session commit workflow (see finish_commit()), or (2) during the administrative script
        that reconstructs the database contents from the update log and archived files in the raw data repository (see
        reconstruct_database_from_log()).

        Args:
            trials: The list of trials to be inserted.
            behavioral_entries: The list of behavioral responses to be inserted.
            neuronal_entries: THe list of neuronal response to be inserted.
            events: The list of TTL event timestamps to be inserted
        """
        trial_table = sgl.Trial()
        behavioral_table = sgl.Trial.BehavioralResponse()
        neuronal_table = sgl.Trial.NeuronalResponse()
        event_table = sgl.Trial.Event()

        # NOTE: We don't use a database transaction here b/c this method is only called within a DataJoint populate()
        # call, which already has started a transaction...
        with self._db_lock:
            trial_table.insert(trials, replace=False)
            if len(behavioral_entries) > 0:
                behavioral_table.insert(behavioral_entries, replace=False)
            if len(neuronal_entries) > 0:
                neuronal_table.insert(neuronal_entries, replace=False)
            if len(events) > 0:
                event_table.insert(events, replace=False)

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

    def reconstruct_database_from_log(self) -> bool:
        """
        FOR ADMIN USE ONLY: THIS METHOD MUST NEVER BE CALLED WHILE THE BACKEND SERVER IS RUNNING AND PROCESSING EXTERNAL
        CLIENT REQUESTS!

        Reconstruct the entire content of the Lisberger lab database from the database update log entries and the
        experiment data archive files stored in the backing repository.

        See file header for a description of the database update log, the folder structure of the backing repository,
        and how reconstruction is possible by "processing" the update log entries in sequence.

        If the database becomes corrupted for whatever reason, this method provides a mechanism for repopulating it
        from scratch without user intervention. The database update log contains an entry for every change made to the
        database: addition or deletion of a row in any of the "manual" tables, an update to a "mapping" table, and a
        session commit. The last is a complex task that involves many additions to the database. It is possible to
        reproduce a session commit without user intervention because the repository stores the original session data
        archive along with a pickle file containing all of the information required to do the commit.

        During normal backend operation, all database changes are recorded in the database update log file. Here we
        are processing the log entries in sequence to restore the database contents. The contents of the repository and
        the update log itself are left unchanged. The database MUST be empty prior to beginning the rebuild; the method
        will fail if it finds any entries in the database tables.

        Returns:
            True if database reconstruction was successful; else False. Progress messages -- and a final error or
                success indicator are printed to the console.
        """
        # ensure database update log exists.
        log_file_path = DataBaseManager._log_file_path()
        if not log_file_path.is_file():
            print(f"ERROR: No database log file found at {str(log_file_path)}", file=sys.stdout, flush=True)
            return False

        print(f"Starting database reconstruction from repository using log file at {str(log_file_path)}...",
              file=sys.stdout, flush=True)

        # first, verify that database is empty
        for table_id in _table_map.keys():
            n = self.num_table_rows(table_id)
            if n != 0:
                print(f"ERROR: Found {n} rows in {ti.table_label(table_id)} table. "
                      f"Database must be empty prior to reconstruction!", file=sys.stdout, flush=True)
                return False

        try:
            num_entries = 0
            with open(log_file_path, 'rb') as file:
                while True:
                    try:
                        entry = pickle.load(file)
                        num_entries += 1
                        print(f"Processing log entry #{num_entries}: \n    {entry}", file=sys.stdout, flush=True)
                        if entry['op'] == 'add':
                            err_msg = self.insert_into_table(entry['table'], entry['row'], log=False)
                        elif entry['op'] == 'delete':
                            err_msg = self.delete_from_table(entry['table'], entry['restriction'], log=False)
                        elif entry['op'] == 'mapping':
                            err_msg = self.update_mapping_table(
                                entry['table'], entry['src_pk'], entry['dst_pks'], log=False)
                        elif entry['op'] == 'session':
                            err_msg = DataBaseManager._reconstruct_session(entry)
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
            return False

        print("Reconstruction completed successfully!", file=sys.stdout, flush=True)
        return True

    @staticmethod
    def _reconstruct_session(log_entry: Dict[str, Union[str, int]]) -> Optional[str]:
        """
        Commit an experiment session during scripted reconstruction of the lab database content from entries in the
        database log file and experiment data archives stored in the backing repository.

        For each experiment session, the original session archive and the pickle file containing the results of
        pre-processing are stored in the folder $REPO/$USER, where $REPO is the data repository root and $USER is the
        experimenter's username in the lab database. The archive file is $SUBJ_$DATE_$SFX.zip and the pickle file is
        $SUBJ_$DATE_$SFX.pickle, where $SUBJ is the experiment subject's ID, $DATE is the session date in the format
        'YYY-MM-DD', and $SFX is the session suffix.

        Since all pre-processing results -- as well as any information entered manually when the session was originally
        committed to the database -- are stored in the pickle file, the commit process requires no user intervention
        and is significantly faster because it does not require processing of a large PL2 file. Still, it could take
        many seconds or even minutes if the session recorded thousands of trials. Progress messages are written to the
        STDOUT console.

        Args:
            log_entry: A database log entry for a session commit. This dictionary must have the form {'op': 'session',
                'username': str, 'subj_id': str, 'date': 'YYYY-MM-DD', 'suffix': int}. See description above.
        Returns:
            An error description if session commit fails; else None
        """
        error_msg = None
        session_repo_path = DataBaseManager.get_repo_directory_for(log_entry['username'])
        base_filename = f"{log_entry['subj_id']}_{str(log_entry['date'])}_{log_entry['suffix']}"
        zip_path_in_repo = Path(session_repo_path, f"{base_filename}.zip")
        pickle_path_in_repo = Path(session_repo_path, f"{base_filename}.pickle")
        session_pks = ['experimenter', 'subj_id', 'session_date', 'session_sfx']
        try:
            # load pre-processing results from pickle file
            print(f"   > Checking session data archives...", file=sys.stdout, flush=True)
            if not (zip_path_in_repo.is_file() and pickle_path_in_repo.is_file()):
                raise Exception("Missing session ZIP archive or pre-processing results file!")
            with open(pickle_path_in_repo, 'rb') as file:
                results = pickle.load(file)
            if not (isinstance(results, dict) or
                    all([(k in results) for k in ['protocols', 'trials', 'units', 'session', 'ephys']])):
                raise Exception("Invalid or incomplete pre-processing results file!")
            protocols: List[maestro.Protocol] = results['protocols']
            trial_info: Dict[str, _TrialInfo] = results['trials']
            units: Optional[List[OmniplexUnit]] = results['units']
            session_info: Dict[str, Optional[AttributeValue]] = results['session']
            ephys_info: Optional[Dict[str, Optional[AttributeValue]]] = results['ephys']

            # only insert new trial protocols
            protocols_to_add: List[Dict[str, Any]] = list()
            existing_proto_map = {pk['proto_hash']: 1
                                  for pk in DataBaseManager().fetch_proj(DBTable.TRIAL_PROTOCOL, ['proto_hash'])}
            for protocol in protocols:
                if protocol.md5_digest not in existing_proto_map:
                    protocol_entry: Dict[str, Any] = dict()
                    protocol_entry['proto_hash'] = protocol.md5_digest
                    protocol_entry['proto_name'] = protocol.trial.name
                    protocol_entry['proto_set'] = "" if (protocol.trial.set_name is None) else protocol.trial.set_name
                    protocol_entry['proto_subset'] = \
                        "" if (protocol.trial.subset_name is None) else protocol.trial.subset_name
                    protocol_entry['proto_def'] = pickle.dumps(protocol)
                    protocols_to_add.append(protocol_entry)

            print(f"   > Inserting session information and new trial protocols into database...",
                  file=sys.stdout, flush=True)
            session_table = sgl.Session()
            with session_table.connection.transaction:
                session_table.insert1(session_info, replace=False)
                if ephys_info is not None:
                    sgl.Session.EPhys().insert1(ephys_info, replace=False)

                if isinstance(units, list) and len(units) > 0:
                    neurons: List[Dict[str, Any]] = list()
                    for i, unit in enumerate(units):
                        neuron = dict()
                        for pk in session_pks:
                            neuron[pk] = session_info[pk]
                        neuron['unit_id'] = i + 1
                        neuron['unit_channel'] = unit.channel
                        neuron['unit_type'] = unit.neuron_type
                        neuron['unit_rate'] = unit.firing_rate
                        neuron['unit_spikes'] = len(unit.spike_times)
                        neuron['unit_snr'] = unit.snr
                        neuron['unit_template'] = unit.template
                        neurons.append(neuron)
                    sgl.Session.Neuron().insert(neurons, replace=False)

                if len(protocols_to_add) > 0:
                    protocol_table = sgl.TrialProtocol()
                    protocol_table.insert(protocols_to_add, replace=False)

            # populate Trial table using a TrialProducer delegate object
            print(f"   > Populating database with trial data...", file=sys.stdout, flush=True)
            trial_table = sgl.Trial()
            producer = _SessionTrialProducer(zip_path_in_repo, trial_info, protocols, units)
            trial_table.set_trial_producer(producer)
            trial_table.populate()
            trial_table.set_trial_producer(None)

            print("   > Session was successfully committed to database.", file=sys.stdout, flush=True)
        except Exception as err:
            error_msg = f"Exception while reconstructing experiment session:\n  {str(err)}"

        return error_msg


class _SessionTrialProducer(sgl.TrialProducer):
    """
    Helper class that populates the Lisberger lab database (the Trial table in sgl_schema.py) when an experiment session
    is committed to the database. It is used in two contexts: (1) during a commit managed by the backend server via
    the ProcessArchiveThread task; (2) during reconstruction of the database contents from the database update log and
    the archive files stored in the backing repository.

    NOTE: Inserting data associated with a single trial can involve many individual database inserts: one for the entry
    into the Trial table itself, one for EACH recorded behavioral response trace inserted into the BehavioralResponse
    part table, one for EACH recorded unit spike train inserted into the NeuronalResponse part table, and one for EACH
    set of event timestamps inserted into the Event part table. To reduce the volume of database calls, we accumulate
    trial entries, behavioral response traces, neural unit spike trains and event timestamp data over "chunks" of 10
    trials at a time. Testing showed that a chunk size of ~25 trials significantly reduced the time it took to insert
    all trial data (versus one database insert at a time), but larger chunk sizes did not further increase performance.

    Usage: Construct the _SessionTrialProducer object, passing the required trial data and the path to the session data
    archive. Set this object as the trial producer on the Trial table: sgl_schema.Trial.set_trial_producer(). Then call
    sgl_schema.Trial.populate() to populate the Trial table with data from all trials recorded during the session.
    """
    def __init__(self, zip_path: Path, trial_info: Dict[str, _TrialInfo], protocols: List[maestro.Protocol],
                 units: Optional[List[OmniplexUnit]], worker: Optional[ProcessArchiveThread] = None):
        """
        Construct the session trial data generator.

        Args:
            zip_path: The path to the session data archive containing all Maestro trial data files.
            trial_info: Dictionary of information about all trials presented during the experiment session, ascertained
                during pre-processing of the session archive. Keyed by trial data filenames.
            protocols: List of all Maestro trial protocols presented during the experiment session.
            units: List of all neural units recorded during the session.
            worker: If not None, this is the worker thread on which the session commit task is performed on the server.
                The method will deliver progress messages to the thread's synchronous queue and check regularly to see
                if the client has cancelled the commit. Otherwise, progress messages are printed to STDOUT and the
                operation is not cancellable. Default = None.
        """
        self.zip_path = zip_path
        self.trial_info = trial_info
        self._protocols = protocols
        self.units = units
        self.worker = worker

    def insert_trials_for_session(self, session_key: Dict[str, Any]) -> None:
        # generate list of trial file names in presentation order. We CANNOT rely on file creation time! If Omniplex
        # system used and all trials were timestamped within the same PL2 file, then order by Omniplex start time.
        # Else, if the file header includes the internal Maestro timestamp (we assume all trials will if the first
        # one does!), use that. Otherwise, order by ascending numeric file suffix (.0001,...).
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
        message = f"{num_inserted} of {num_trials} trials added to database..."
        if self.worker is None:
            print(f"   >    {message}", file=sys.stdout, flush=True)
        else:
            self.worker.msg_q.put_nowait(message)
        t0 = time.time()
        trial1_start_sec: float = 0
        db_mgr = DataBaseManager()

        # we accumulate 25 trials' worth of data at a time to reduce the number of database calls overall. Testing
        # indicated that larger "chunk sizes" did not further improve performance.
        trial_entries: List[Dict[str, AttributeValue]] = list()
        behavioral_entries: List[Dict[str, AttributeValue]] = list()
        neuronal_entries: List[Dict[str, AttributeValue]] = list()
        event_entries: List[Dict[str, AttributeValue]] = list()
        num_trials_chunked = 0
        with zipfile.ZipFile(self.zip_path, 'r') as archive:
            for trial_filename in sorted_filenames:
                data_file = maestro.DataFile.load(archive.read(trial_filename), trial_filename)
                t_info = self.trial_info[trial_filename]

                # compute scale factor to convert Omniplex spike times to Maestro timeline. However, if Maestro
                # trial length according to Omniplex is more than 2ms off, fail.
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
                    proto_hash=t_info.proto_hash,
                    trial_header=pickle.dumps(data_file.header),
                    trial_filename=trial_filename,
                    trial_dur=data_file.header.num_scans_saved - 1,
                    trial_record_start=data_file.trial.record_start(),
                    trial_success=((data_file.header.flags & maestro.FLAG_REWARD_EARNED) != 0),
                    trial_rewarded=((data_file.header.flags & maestro.FLAG_REWARD_GIVEN) != 0),
                    trial_rew1=data_file.header.reward_len1_ms,
                    trial_rew2=data_file.header.reward_len2_ms,
                    vstab_win_len=data_file.header.velocity_stab_window_len_ms
                )

                # compute trial start time relative to start of first trial in session -- if possible
                if sort_strategy == 'index':
                    trial_entry['trial_ts'] = -1
                else:
                    t_sec = t_info.omniplex_start if (
                                sort_strategy == 'omniplex') else t_info.header_timestamp / 1000.0
                    if num_inserted == 0:
                        trial_entry['trial_ts'] = 0
                        trial1_start_sec = t_sec
                    else:
                        trial_entry['trial_ts'] = t_sec - trial1_start_sec

                # for this trial, get the values of the protocol's random variables
                protocol = next((x for x in self._protocols if x.md5_digest == t_info.proto_hash), None)
                if not protocol:
                    raise Exception(
                        f"Internal inconsistency: No trial protocol defined for trial in {trial_filename}")
                rv_values: List[Any] = list()
                for param in protocol.rvs:
                    rv_value = data_file.trial.retrieve_segment_table_parameter_value(param)
                    if not rv_value:
                        raise Exception(
                            f"Internal inconsistency: Invalid RV ({param}) for trial in {trial_filename}")
                    rv_values.append(rv_value)
                trial_entry['trial_rvs'] = pickle.dumps(rv_values)

                trial_entries.append(trial_entry)
                num_trials_chunked += 1

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
                        behavioral_entries.append(response_entry)

                # insert neural unit responses, if any, into Trial.NeuronalResponse. A neural unit may not fire any
                # spikes during a trial, but that could be a valid response. Only exclude a unit if the last spike
                # time is before trial start or the first spike time is after trial end!
                if self.units is not None:
                    for i, unit in enumerate(self.units):
                        spikes = unit.spike_times
                        if (spikes[-1] < t_info.omniplex_start) or (spikes[0] > t_info.omniplex_stop):
                            continue

                        spikes_in_trial = \
                            spikes[(spikes >= t_info.omniplex_start) & (spikes <= t_info.omniplex_stop)]
                        spikes_in_trial = (spikes_in_trial - t_info.omniplex_start) * maestro_omniplex_time_scaling
                        response_entry = dict(
                            session_key,
                            trial_idx=(num_inserted + 1),
                            unit_id=(i + 1),
                            spike_times=spikes_in_trial
                        )
                        neuronal_entries.append(response_entry)

                # insert any recorded marker events into Trial.Event (recorded in Maestro file, not by Omniplex).
                if data_file.events is not None:
                    for di_channel in data_file.events:
                        # convert event times from ms to sec and offset if event recording started after trial began
                        event_times = np.array(
                            data_file.events[di_channel]) * 0.001 + data_file.trial.record_start()
                        event_entry = dict(
                            session_key,
                            trial_idx=(num_inserted + 1),
                            event_ch=di_channel,
                            event_times=event_times
                        )
                        event_entries.append(event_entry)

                # batch insert after accumulating data for 25 trials (or processed the last trial)
                num_inserted += 1
                if (num_trials_chunked == 25) or (num_inserted == num_trials):
                    db_mgr.batch_insert_trials(trial_entries, behavioral_entries, neuronal_entries, event_entries)
                    trial_entries.clear()
                    num_trials_chunked = 0
                    behavioral_entries.clear()
                    neuronal_entries.clear()
                    event_entries.clear()

                # report progress roughly once per second. Also check for cancel if cancel event provided.
                if (time.time() - t0) > 1:
                    message = f"{num_inserted} of {num_trials} trials added to database..."
                    if self.worker is None:
                        print(f"   >    {message}", file=sys.stdout, flush=True)
                    else:
                        self.worker.msg_q.put_nowait(message)
                        if self.worker.is_cancelled():
                            raise Exception("Operation cancelled.")
                    t0 = time.time()


class ProcessArchiveThread(threading.Thread):
    """
    This worker thread handles server-side processing during stages 2-4 of a session commit task:
        1) Wait for upload of session archive ZIP to the staging directory, monitoring its progress once per second. If
        the upload does not start after 10 minutes of waiting, or the upload stalls for more than 10 minutes, report the
        error and terminate.

        2) Once the upload completes, process all Maestro data files in the session archive (in situ -- the files are
        NOT extracted from the ZIP file) and generate a set of "candidate" trial protocols presented over the course of
        the experiment session. If only 1 or 2 reps of a particular protocol candidate are processed, and there is not
        an existing protocol in the database that matches it, then user validation of that protocol candidate will be
        required in stage 3. If an error occurs, report the error and terminate.

        3) Load timing information for each trial. The primary purpose of this step is to process the strobed and event
        data in the Omniplex file(s) in the archive in order to align any neural unit responses with the individual
        trial timelines.

        4) Further process the archive for any neural unit data in the archive. The experimenter must provide their
        own spike sorting results in a single pickle file in the archive. See the module header comments for a
        description of this file. For now, we only support neural units recorded on the Omniplex system, and the archive
        must include the relevant PL2 file(s) for each unit specified in the pickle.

        5) Prepare session metadata for user review.

        6) After pre-processing is complete, the worker enters stage 3, during which the user on the client side reviews
        the results and may make changes to the session metadata. Trial protocols may also require user validation.
        The worker is essentially paused in this stage, waiting for the command to enter stage 4. Any changes to the
        session metadata and trial protocols are validated before the worker can transition to stage 4.

        7) In stage 4, the worker commits the experiment session to the lab database and raw data repository. See
        DataBaseManager.finish_commit() for the details.

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
        self.proto_candidates: Optional[List[maestro.ProtocolCandidate]] = None
        """ The list of trial protocol candidates culled from the session data archive during stage 2 pre-processing.
        Set by worker. Safe for server to access only while worker is paused in stage 3. """
        self._protocols: Optional[List[maestro.Protocol]] = None
        """ The list of trial protocols generated from the trial protocol candidates. Candidates with fewer than 3 reps
        must be manually validated by the user in stage 3, so they are not converted into the final trial protocol
        objects until the session is committed in stage 4. """
        self.trial_info: Optional[Dict[str, _TrialInfo]] = None
        """ Dictionary maps the filename for each Maestro data file in the session archive to timing and trial protocol
        information for the particular trial instance recorded in that file. In particular, this includes the Omniplex
        start and stop timestamps required to align neural responses recorded on the Omniplex system with the behavioral
        responses recorded by Maestro. Set by worker. Safe for server to access only while worker is in stage 3. """
        self.units: Optional[List[OmniplexUnit]] = None
        """ The list of neural units culled from the session data archive during stage 2 pre-processing. Includes the
        information required to prepare an entry in the Session.Neuron part table for each neural unit. Prepared by
        worker during stage 2 pre-processing. Safe for server to access only while worker is paused in stage 3; during
        that stage, the user can assign a neuron type to each neural unit. """
        self.session_info: Optional[Dict[str, Optional[AttributeValue]]] = None
        """ User-supplied information required to add an entry in the Session table in the database, keyed by the table
        table attribute IDs. During pre-processing, the worker thread may initialize some of this information. The 
        client will provide the user-edited version of the dictionary upon initiating the final commit (stage 4). """
        self.ephys_info: Optional[Dict[str, Optional[AttributeValue]]] = None
        """ When a session includes neural unit recordings, this field will contain user-supplied information required
        to add an entry in the Session.EPhys part table in the database, keyed by the attribute IDs in that table. It
        does not include the primary keys that identify the session itself, as these are in self.session_info. During
        stage 2 pre-processing, the worker thread may initialize some of this information. The client will provide the
        user-edited version of the dictionary upon initiating the final commit (stage 4). """

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
            error_msg = DataBaseManager().finish_commit(worker=self)
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

    def is_cancelled(self) -> bool:
        """ Has the session commit task been cancelled? """
        return self._cancel_request.is_set()

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
                self.proto_candidates, file_to_proto = \
                    maestro.ProtocolCandidate.extract_protocols_from_session_data(archive)
                if len(self.proto_candidates) == 0:
                    raise Exception("No trial protocols found in session archive!")
                for filename, proto_index in file_to_proto.items():
                    self.trial_info[filename].proto_index = proto_index
                # if any protocol candidate is based on 2 or more reps, compute the protocol's hash and check to see if
                # that protocol already exists. For any candidate based on a single rep, or on 2 reps but does not match
                # an existing protocol, the user must manually review and validate the protocol candidate before the
                # session is committed to the database.
                db_mgr = DataBaseManager()
                proto_hash_map = \
                    {ph: 1 for ph in db_mgr.fetch_attribute_values(DBTable.TRIAL_PROTOCOL, 'proto_hash')}
                for proto in [p for p in self.proto_candidates if p.num_reps >= 2]:
                    test_proto = maestro.Protocol.from_candidate(proto)
                    if test_proto.md5_digest in proto_hash_map:
                        proto.matches_existing = True
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
                # subject ID from the ZIP archive file name or a Maestro data file name. Some metadata must be supplied
                # by the user.
                self.session_info = dict()
                self.session_info['experimenter'] = None
                subject_choices = db_mgr.fetch_attribute_values(DBTable.SUBJECT, 'subj_id')
                for choice in subject_choices:
                    if choice.lower() in ','.join([self.zip_path.name.lower(), sample_maestro_file_name.lower()]):
                        self.session_info['subj_id'] = choice
                        break
                if 'subj_id' not in self.session_info:
                    self.session_info['subj_id'] = None
                self.session_info['session_date'] = session_date
                self.session_info['session_sfx'] = None
                self.session_info['rig_id'] = None
                self.session_info['study_id'] = None
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
                    self.ephys_info['probe_x'] = None
                    self.ephys_info['probe_y'] = None
                    self.ephys_info['probe_depth'] = None
                    self.ephys_info['ba_id'] = None

        except Exception as err:
            error_msg = f"Error: {str(err)}"

        return error_msg

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

        # prepare neural unit objects. The neuron type is not set here.
        noise = np.median(block_medians) * 1.4826
        out: List[OmniplexUnit] = list()
        for i in range(len(spikes)):
            if num_clips[i] > 0:
                template[i] /= num_clips[i]
            snr = (np.max(template[i]) - np.min(template[i])) / (1.96 * noise)
            firing_rate = float(len(spikes[i])) / (spikes[i][-1] - spikes[i][0])
            template[i] *= to_volts * 1.0e6
            out.append(OmniplexUnit(filename, channel_id, spikes[i], firing_rate, snr, template[i]))
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
    pre-processing phase of the session commit workflow: (1) identify of the trial protocol to which each trial rep
    belongs, and (2) timing information used to determine the order in which trials were presented during the experiment
    and to align spike times of neural units recorded on the Omniplex system with respect to the timeline of the Maestro
    trials in which behavioral response data is recorded.

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
    proto_index: Optional[int] = None
    """ The zero-based index into the list of all trial protocol candidates presented during the session. """
    proto_hash: Optional[str] = None
    """ The MD5 hexadecimal digest uniquely identifying the trial protocol for this particular trial instance. It is
    set only after all trial protocol candidates culled from an experiment session have been validated and converted to
    protocol objects. """


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


@dataclass
class TrialData:
    """
    Data container for the results from a single Maestro trial as retrieved from the Lisberger lab database.
    """
    experimenter: str
    """ Username of lab member performing the experiment in which this trial was recorded. """
    subj_id: str
    """ ID of experiment subject. """
    session_date: date
    """ Date of experiment session during which trial was recorded. """
    session_sfx: int
    """ Session suffix (to distinguish multiple sessions on the same date). """
    trial_idx: int
    """ Trial index -- indicates order of presentation during the experiment session. """
    protocol: maestro.Protocol
    """ The trial protocol presented. """
    filename: str
    """ Original filename of the Maestro data file in which behavioral and other data was recorded. """
    duration_ms: int
    """ Recorded duration of this trial, in milliseconds. """
    record_start_ms: int
    """ Time at which recording began after trial start, in milliseconds (typically 0). """
    success: bool
    """ True if trial was completed successfully. """
    rewarded: bool
    """ True if subject was rewarded (could be false if random reward withholding in effect). """
    reward1_ms: int
    """ Duration of reward pulse #1 in milliseconds. """
    reward2_ms: int
    """ Duration of reward pulse #2 in milliseconds. """
    vstab_win_len_ms: int
    """ Window length for smoothing eye position during velocity stabilization, in milliseconds [1..20]."""
    timestamp_sec: float
    """ Trial start timestamp, in seconds since start of first trial in experiment session (<0 if unknown). """
    trial_rvs: List[Union[int, float]]
    """ List of random variable values, in same order in which random variables are defined in trial protocol. """
    behavior: Dict[str, np.ndarray]
    """ Behavioral responses (in deg or deg/sec) for recorded duration of trial, keyed by channel ID. 1KHz rate. """
    neuronal: Dict[int, np.ndarray]
    """ Neural unit spike trains during trial - spike times in seconds since trial start. Keyed by unit ID. """

    def instantaneous_firing_rate(self, unit_id: int, smooth: bool = False) -> np.ndarray:
        """
        Compute the instantaneous firing rate for a specified neural unit over the course of the trial timeline,
        optionally smoothed with a Gaussian kernel.

        Firing rate R is computed as the reciprocal of inter-spike interval following Lisberger & Pavelko (1986). Let
        the spike times during the trial be [T(1) .. T(N)]. For each t (delta = 1ms) in the interval [T(i)..T(i+1)],
        R(t) = 1/(T(i) - T(i-1)) if t - T(i) < T(i) - T(i-1); else R(t) = 1/(T(i+1) - T(i)). For t < T(1), R(t) = 0.
        For t in [T(N), T(N) + T(N) - T(N-1)], R = 1/(T(N) - T(N-1)). For t > 2*T(N) - T(N-1), R = 0.

        The firing rate trace is optionally smoothed by convolving it with a Gaussian kernel with a width of 2.5ms.

        Args:
            unit_id: Neural unit ID
            smooth: If True, the instantaneous firing rate is smoothed (default = False).
        Returns:
            Instantaneous firing rate per millisecond during trial, in Hz.
        Raises:
            KeyError: If the unit ID is invalid.
        """
        # spike times in seconds, and converted to integer milliseconds (trial timeline DT is 1ms)
        spike_times = self.neuronal[unit_id]
        spikes_ms = np.floor(spike_times*1000.0).astype(int)
        num_spikes = len(spike_times)
        firing_rate = np.zeros(self.duration_ms)
        if num_spikes < 2:
            return firing_rate   # not enough spikes to compute firing rate

        for i in range(num_spikes):
            t = spikes_ms[i]
            if i == 0:
                t_plus = spikes_ms[i+1]
                firing_rate[t:t_plus] = 1.0 / (spike_times[i+1] - spike_times[i])
            elif i == num_spikes - 1:
                t_minus = spikes_ms[i-1]
                t_last = min(2*t - t_minus, self.duration_ms - 1)
                firing_rate[t:t_last+1] = 1.0 / (spike_times[i] - spike_times[i-1])
            else:
                t_minus = spikes_ms[i-1]
                t_plus = spikes_ms[i+1]
                firing_rate[t:t_plus] = 1.0 / (spike_times[i] - spike_times[i-1])
                if 2*t - t_minus < t_plus:
                    firing_rate[2*t - t_minus:t_plus] = 1.0 / (spike_times[i+1] - spike_times[i])

        if smooth:
            width = 2.5  # in milliseconds  -- could make this a parameter to method
            x = np.arange(-10 * width, 10 * width)
            kernel = np.exp(-x**2/2.0) / (width * np.sqrt(2*np.pi))
            kernel = kernel / sum(kernel)
            firing_rate = np.convolve(firing_rate, kernel, mode='same')

        return firing_rate

    def eye_velocity_saccades_removed(
            self, offset: bool = True, t_vel: float = 20, t_vel_max: float = 50, t_acc: float = 1250,
            t_acc_max: float = 2000, pre_ticks: int = 2, post_ticks: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Return the horizontal and vertical eye velocity traces for this trial with any saccade epochs replaced by NaN
        samples. This method ASSUMES a sampling rate of 1KHz!

        Args:
            offset: If True, the eye velocity traces are adjusted for DC offset, if possible. Default = True.
            t_vel: Velocity threshold for a saccade. Default = 20 deg/sec
            t_vel_max: Max velocity threshold for a saccade regardless the current acceleration. Default = 50 deg/sec.
            t_acc: Acceleration threshold for a saccade. Default = 1250 deg/sec^2
            t_acc_max: Max acceleration threshold for a saccade regardless the current velocity. Default = 2000.
            pre_ticks: Number of samples before a detected saccade epoch that are included in that epoch. Default = 2.
            post_ticks: # of samples after a detected saccade epoch that are included in that epoch. Default = 5.
        Returns:
            A 2-tuple (H, V) -- COPIES of the horizontal and vertical eye velocity traces in which any samples falling
                within a detected saccade epoch are replaced with NaN. If either velocity trace was not recorded, it is
                assumed to be 0 for the entire duration of the trial.
        """
        # handle edge cases: only H, only V, or no eye velocity trace available
        if not (('HEVEL' in self.behavior) and ('VEVEL' in self.behavior)):
            return np.zeros(self.duration_ms, dtype=np.float32), np.zeros(self.duration_ms, dtype=np.float32)
        if 'HEVEL' in self.behavior:
            hevel = np.copy(self.behavior['HEVEL'])
            if offset:
                hevel = hevel - self.estimate_velocity_baseline_offset('HEVEL')
        else:
            hevel = np.zeros(self.duration_ms, dtype=np.float32)
        if 'VEVEL' in self.behavior:
            vevel = np.copy(self.behavior['VEVEL'])
            if offset:
                vevel = vevel - self.estimate_velocity_baseline_offset('VEVEL')
        else:
            vevel = np.zeros(self.duration_ms, dtype=np.float32)
        speed = np.sqrt(hevel ** 2 + vevel ** 2)
        acceleration = np.diff(speed) / 0.001   # sampling rate = 1KHz!!
        acceleration = np.append(acceleration, np.nan)
        acceleration = np.abs(acceleration)
        state = "not_saccading"
        onset_indices = []
        offset_indices = []
        stopping_index = 0
        for i in range(len(speed)):
            if (state == "not_saccading") and (((speed[i] > t_vel) and (acceleration[i] > t_acc)) or
                                               (acceleration[i] > t_acc_max) or (speed[i] > t_vel_max)):
                state = "saccading"
                onset_indices.append(max(0, i - pre_ticks))
            elif (state == "saccading") and ((speed[i] < t_vel) or (acceleration[i] < t_acc)):
                stopping_index = i + post_ticks
                state = "stopping"
            elif (state == "stopping") and (speed[i] > t_vel) and (acceleration[i] > t_acc):
                state = "saccading"
            if (state == "stopping") and (i >= stopping_index):
                state = "not_saccading"
                offset_indices.append(i)
        # Make sure we have the same number of samples
        if len(offset_indices) < len(onset_indices):
            offset_indices.append(len(speed))

        for i in range(len(offset_indices)):
            hevel[onset_indices[i]:offset_indices[i]] = np.nan
            vevel[onset_indices[i]:offset_indices[i]] = np.nan

        return hevel, vevel

    def estimate_velocity_baseline_offset(self, response_id: str) -> float:
        """
        Estimate the baseline offset for an eye velocity trace from this trial. This method examines the corresponding
        position traces and looks for a contiguous segment spanning 100 samples (100ms) in which the position varies
        by 0.1 degrees or less AND the velocity varies by 2 deg/s or less -- in which case eye velocity should be close
        to 0 (and not in the tail of a saccade!). If it finds such a segment, the baseline offset in the velocity trace
        is the mean value over the same segment in the original eye velocity trace.

        Args:
            response_id: Must be 'HEVEL', 'VEVEL', or 'HDVEL'.

        Returns:
            Estimated baseline offset in the specified behavioral trace. If the specified behavioral signal, or its
                position counterpart, was not recorded, the offset cannot be estimated and 0 is returned.
        """
        if (response_id.find('VEL') == -1) or not (response_id in self.behavior):
            return 0
        pos_id = 'HEPOS' if response_id.find('H') > -1 else 'VEPOS'
        if not (pos_id in self.behavior):
            return 0
        pos = self.behavior[pos_id]
        vel = self.behavior[response_id]
        start = 0
        delta = 100
        while start + delta <= len(pos):
            chunk_pos = pos[start:start+delta]
            chunk_vel = vel[start:start+delta]
            if (np.nanmax(chunk_pos) - np.nanmin(chunk_pos) <= 0.1) and \
                    (np.nanmax(chunk_vel) - np.nanmin(chunk_vel) <= 2):
                return np.nanmean(chunk_vel)
            start += 1
        return 0
