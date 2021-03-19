"""
table_info.py: Descriptive information about the tables in the Lisberger lab database.



@author: sruffner
@created: 16mar2021
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

from dataclasses import dataclass
import numpy as np
from datetime import date
from typing import Union, Optional, List, Dict

from common import DocEnum


def table_info_for(table_id: DBTable) -> TableInfo:
    """
    Get information about a specified table in the Lisberger lab database.

    Raises:
        KeyError: If table_id is invalid.
    """
    return _table_info[table_id]


def has_auto_primary_key(table_id: DBTable) -> bool:
    """
    Does the specified table in the Lisberger lab database employ an auto-incrementing primary key?

    Raises:
        KeyError: If table_id is invalid.
    """
    return any([v.type == AttrTypeEnum.AUTO for _, v in _table_info[table_id].attributes.items()])


def auto_primary_key_for(table_id: DBTable) -> Optional[str]:
    """
    Return the ID of the auto-incrementing primary key for the specified table in the Lisberger lab database.
    Args:
        table_id: Database table ID.
    Returns:
        The auto-incrementing primary key ID, or None if the table lacks one.
    Raises:
        KeyError: If table_id is invalid.
    """
    for k, v in _table_info[table_id].attributes.items():
        if v.pkey and v.type == AttrTypeEnum.AUTO:
            return k
    return None


def attributes_of(table_id: DBTable) -> List[str]:
    """
    Get the IDs of the attributes for the specified table in the Lisberger lab database. The list includes an auto-
    incrementing primary key attribute, if one is defined on the table. However, for part tables, it excludes the
    primary key attributes that comprise the parent table's primary key.
    Args:
        table_id: Database table ID.
    Raises:
        KeyError: If table_id is invalid.
    """
    return [k for k in _table_info[table_id].attributes.keys()]


def primary_key_of(table_id: DBTable) -> List[str]:
    """
    Get the IDs of the attributes comprising the primary key of the specified table in the Lisberger lab database.

    Raises:
        KeyError: If table_id is invalid
    """
    return [k for k, v in _table_info[table_id].attributes.items() if v.pkey]


def attribute_info(table_id: DBTable, attr_id: str) -> AttrInfo:
    """
    Get information about an attribute of a table in the Lisberger lab database.

    Raises:
        KeyError: If either argument is invalid.
    """
    return _table_info[table_id].attributes[attr_id]


def validate_numeric_attribute_value(table_id: DBTable, attr_id: str, value: Union[int, float]) -> None:
    """
    Validate the value of a numeric (int or float) attribute of a table in the Lisberger lab database. Currently, the
    only enforced restriction on a numeric attribute is that the suffix for an experiment session must lie in [0..9].

    Args:
        table_id: ID of database table.
        attr_id: Attribute ID.
        value: The numeric value to check.

    Raises:
        ValueError: If the attribute value is out-of-range or otherwise invalid.
    """
    if (table_id == DBTable.SESSION) and (attr_id == 'session_sfx'):
        if (value < 0) or (value > 9):
            raise ValueError("Invalid value for session suffix (must lie in 0..9)")


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
    SESSION_EPHYS = 14, "Part table: Electrophysiology recording metadata for an experiment session"
    SESSION_NEURON = 15, "Part table: Neural units recorded during an experiment session"
    TRIAL_PROTOCOL = 16, "Table of trial protocol definitions"
    TRIAL = 17, "Table of individual trial response data"
    TRIAL_EVENT = 18, "Part table: Marker events recorded in a trial"
    TRIAL_BEHAVIORAL = 19, "Part table: Behavioral responses recorded in a trial"
    TRIAL_NEURONAL = 20, "Part table: Neural unit responses recorded in a trial"

    def is_mapping_table(self) -> bool:
        """ Return True for a cross-reference table. """
        return _table_info[self].is_mapping_table

    def is_part_table(self) -> bool:
        """ Return True for a part table. """
        return not (_table_info[self].parent is None)


AttributeValue = Union[str, int, float, bool, date, np.ndarray, bytes]
""" Database table attribute value type - a union of all the input types supported by the backend server. """


class AttrTypeEnum(DocEnum):
    """
    A enumeration of all attribute types that may appear in a table within the Lisberger lab database.
    """
    TEXT = 1, "A string-valued attribute"
    DATE = 2, "A date in ISO standard format (YYYY-MM-DD)"
    FLOAT = 3, "A float-valued attribute"
    INT = 4, "An integer-valued attribute"
    BOOL = 5, "A boolean valued attribute"
    ENUM = 6, "An enumerated attribute selecting from a small set of string-valued options"
    BLOB = 7, "An opaque binary blob, using a Numpy array or bytes array as a value"
    AUTO = 8, "An auto-incrementing integer-valued key (must be a primary key)"
    FKEY = 9, "A foreign key (parent table and attribute ID within that table must be specified"


@dataclass(frozen=True)
class AttrInfo:
    """
    Information about an attribute defined on a database table. The data class has the following fields. If a field is
    set to None, that field does not apply to the attribute.
        'type' - Attribute type. See AttrTypeEnum.

        'label' - User-facing label.

        'pkey' - True if attribute is part of the table's primary key; else False.

        'fkey_table' - ID of the parent table. None if attribute is not a foreign key.

        'fkey_id' - ID of foreign key attribute as defined in the parent table. None if not a foreign key.

        'options' - List of available options for an 'enum' attribute; else None.

        'col_width' - The suggested column width for the attribute value when displaying table contents in a
        user-facing tabular format like the Dash DataTable. Specified in pixels, eg, '50px'. None if attribute is not
        intended for tabular display.

        'textrange' - The [min, max] number of allowed characters in a valid attribute value; None if not applicable.

        'regex' - For certain 'text' attributes, this is a regular expression that must be satisfied by the attribute
        value. Will be None for all other types and for unrestricted 'text' attributes.

        'regex_hint' - For 'text' attributes validated by a regular expression, this is a user-facing message that
        further describes the domain of valid attribute values. It will be included in the error message when a
        proposed attribute value does not satisfy the regular expression.

        'placeholder' - Brief string intended as placeholder for attribute value in an input widget; it should
        characterize the domain of valid attribute values. Not applicable to all attribute types.
    """
    type: AttrTypeEnum
    label: str
    pkey: bool
    fkey_table: Optional[DBTable] = None
    fkey_id: Optional[str] = None
    options: Optional[List[str]] = None
    col_width: Optional[str] = '0px'
    textrange: Optional[List[int]] = None
    regex: Optional[str] = None
    regex_hint: Optional[str] = None
    placeholder: Optional[str] = None


@dataclass(frozen=True)
class TableInfo:
    """
    Information about a table in the Lisberger lab database. The data class has the following fields:
        'label' - A user-facing label for the database table.

        'row_label' - A generic user-facing label for one entry in the table.

        'parent' - For a part table, this is the ID of the parent table; else None.

        'is_mapping_table' - True for a cross-reference table only. This table maps a 'source' table to a 'destination'
        table. Those tables must have a single auto incrementing primary key attribute, and those two foreign keys
        comprise the attributes of the mapping table itself.

        'allow_form_entry' - True if table contents may be entered by user via a web form. False for tables with
        content generated automatically by the backend server.

        'attributes' - Dictionary of table attribute information, keyed by attribute ID. For part tables, the dictionary
        does not include the attributes comprising the primary key of the parent table.
    """
    label: str
    row_label: str
    parent: Optional[DBTable]
    is_mapping_table: bool
    allow_form_entry: bool
    attributes: Dict[str, AttrInfo]


_table_info: Dict[DBTable, TableInfo] = {
    DBTable.USER: TableInfo(
        'Lab members', 'member', None, False, True,
        attributes={
            'username': AttrInfo(
                AttrTypeEnum.TEXT, 'Username', True, None, None, None, '100px', [3, 20], r"^[a-z]{1}[a-z0-9]{2,19}$",
                'Contains an invalid character or does not start with lowercase a-z',
                'Enter username (unique, lowercase a-z or digit, 3-20 characters)'),
            'full_name': AttrInfo(
                AttrTypeEnum.TEXT, 'Full Name', False, None, None, None, '150px', [3, 50],
                r"^[A-Z][a-zA-Z'-]{3,}(?: [A-Z][a-zA-Z'-]*){0,2}$",
                "Too many names, not capitalized, or contains a character other than [A-Za-z'-]",
                'Enter full name (as it would appear in publication; 50 chars max)'),
            'contact_email': AttrInfo(
                AttrTypeEnum.TEXT, 'Email Address', False, None, None, None, '200px', [7, 80],
                r'^[A-Za-z0-9._+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6}$',
                'Does not appear to be a valid email address', 'Enter email address (80 chars max)'),
            'role': AttrInfo(
                AttrTypeEnum.ENUM, 'Role', False, None, None,
                ["Principal Investigator", "Post Doctoral Researcher", "Graduate Student", "Administrator"], '150px')
        }),

    DBTable.SUBJECT: TableInfo(
        'Experiment subjects', 'subject', None, False, True,
        attributes={
            'subj_id': AttrInfo(
                AttrTypeEnum.TEXT, 'Subject ID', True, None, None, None, '150px', [3, 20], r"^[a-zA-z]{3,20}$",
                'May only contain the letters A-Z (uppercase or lowercase)',
                'Enter subject ID/nickname (unique, 3-20 characters)'),
            'species': AttrInfo(
                AttrTypeEnum.ENUM, 'Species', False, None, None, ["Macaca mulatta", "Homo sapiens"], '150px'),
            'dob': AttrInfo(
                AttrTypeEnum.DATE, 'Date of Birth', False, None, None, None, '150px', [10, 10], None, None,
                'YYYY-MM-DD'),
            'sex': AttrInfo(AttrTypeEnum.ENUM, 'Sex', False, None, None, ['M', 'F', '?'], '150px')
        }),

    DBTable.IMPLANT: TableInfo(
        'Implant history', 'implant record', None, False, True,
        attributes={
            'subj_id': AttrInfo(AttrTypeEnum.FKEY, 'Subject ID', True, DBTable.SUBJECT, 'subj_id'),
            'implant_date': AttrInfo(
                AttrTypeEnum.DATE, 'Surgery Date', True, None, None, None, '100px', [10, 10], None, None, 'YYYY-MM-DD'),
            'st_ap': AttrInfo(
                AttrTypeEnum.FLOAT, 'AP (mm)', False, None, None, None, '75px', [1, 10], None, None,
                'Enter anterior-posterior coordinate of cylinder implant, in millimeters'),
            'st_ml': AttrInfo(
                AttrTypeEnum.FLOAT, 'ML (mm)', False, None, None, None, '75px', [1, 10], None, None,
                'Enter medial-lateral coordinate of cylinder implant, in millimeters'),
            'st_dv': AttrInfo(
                AttrTypeEnum.FLOAT, 'DV (mm)', False, None, None, None, '75px', [1, 10], None, None,
                'Enter dorsal-ventral coordinate of cylinder implant, in millimeters'),
            'ap_angle': AttrInfo(
                AttrTypeEnum.FLOAT, 'AP angle (deg)', False, None, None, None, '75px', [1, 10], None, None,
                'Enter cylinder angle relative to anterior-posterior axis, in degrees CCW'),
            'ml_angle': AttrInfo(
                AttrTypeEnum.FLOAT, 'ML angle (deg)', False, None, None, None, '75px', [1, 10], None, None,
                'Enter cylinder angle relative to medial-dorsal axis, in degrees CCW')
        }),

    DBTable.RIG: TableInfo(
        'Experiment rigs', 'rig', None, False, True,
        attributes={
            'rig_id': AttrInfo(
                AttrTypeEnum.TEXT, 'Rig ID', True, None, None, None, '100px', [1, 10], r'^[A-Z]{1}[\w .-]{0,9}$',
                'Does not start with capital A-Z or contains an invalid character',
                'Enter short rig name, eg "Rm 1A" (unique, 1-10 characters)'),
            'rig_loc': AttrInfo(
                AttrTypeEnum.TEXT, 'Location', False, None, None, None, '500px', [0, 50], r'[\s\S]*', None,
                'Enter rig location (eg, building and room number) [optional, up to 50 chars]')
        }),

    DBTable.BRAIN_AREA: TableInfo(
        'Brain Regions', 'region', None, False, True,
        attributes={
            'ba_id': AttrInfo(AttrTypeEnum.AUTO, 'ID#', True),
            'ba_name': AttrInfo(
                AttrTypeEnum.TEXT, 'Brain Area', False, None, None, None, '550px', [3, 50], r'^[\w .-]{3,50}$',
                'May only contain Unicode word characters, digits, and select punctuation',
                'Enter a concise name or abbreviation for brain area (unique, 3-50 characters)')
        }),

    DBTable.NEURON_TYPE: TableInfo(
        'Neuron Types', 'neuron type', None, False, True,
        attributes={
            'nt_id': AttrInfo(AttrTypeEnum.AUTO, 'ID#', True),
            'nt_name': AttrInfo(
                AttrTypeEnum.TEXT, 'Neuron Type', False, None, None, None, '550px', [3, 50], r'^[\w .-]{3,50}$',
                'May only contain Unicode word characters, digits, and select punctuation',
                'Enter a concise name or abbreviation for neuron cell type (unique, 3-50 characters)')
        }),

    DBTable.BRAIN_AREA_TO_NEURON_TYPE: TableInfo(
        '', '', None, True, False,
        attributes={
            'ba_id': AttrInfo(AttrTypeEnum.FKEY, 'Area ID', True, DBTable.BRAIN_AREA, 'ba_id'),
            'nt_id': AttrInfo(AttrTypeEnum.FKEY, 'Type ID', True, DBTable.NEURON_TYPE, 'nt_id')
        }),

    DBTable.STUDY: TableInfo(
        'Research projects', 'project', None, False, True,
        attributes={
            'study_id': AttrInfo(AttrTypeEnum.AUTO, 'ID#', True),
            'study_title': AttrInfo(
                AttrTypeEnum.TEXT, 'Project Title', False, None, None, None, '200px', [3, 50], r'^[\w .-]{3,50}$',
                'May only contain Unicode word characters, digits, and select punctuation',
                'Enter a concise descriptive project title (unique, 3-50 characters)'),
            'study_lead': AttrInfo(AttrTypeEnum.FKEY, 'Prj Lead', False, DBTable.USER, 'username'),
            'study_desc': AttrInfo(
                AttrTypeEnum.TEXT, 'Description', False, None, None, None, '700px', [0, 2048], r'[\s\S]*', None,
                'Enter a description of the research project (optional, up to 2048 chars)')
        }),

    DBTable.KEYWORD: TableInfo(
        'Research keywords', 'keyword', None, False, True,
        attributes={
            'kw_id': AttrInfo(AttrTypeEnum.AUTO, 'ID#', True),
            'keyword': AttrInfo(
                AttrTypeEnum.TEXT, 'Keyword', False, None, None, None, '550px', [3, 50], r'^[\w .-]{3,50}$',
                'May only contain Unicode word characters, digits, and select punctuation',
                'Enter new, unique keyword or phrase (3-50 characters)')
        }),

    DBTable.PUB: TableInfo(
        'Research publications', 'publication', None, False, True,
        attributes={
            'pub_id': AttrInfo(AttrTypeEnum.AUTO, 'ID#', True),
            'citation': AttrInfo(
                AttrTypeEnum.TEXT, 'Citation', False, None, None, None, '400px', [50, 500], r'^[\s\S]{50,500}$', None,
                'Enter formal citation (50-500 chars)'),
            'doi': AttrInfo(
                AttrTypeEnum.TEXT, 'DOI', False, None, None, None, '100px', [0, 100], r'[\s\S]*', None,
                "Enter publication's digital object ID (optional; 100 chars max)")
        }),

    DBTable.STUDY_TO_KEY: TableInfo(
        '', '', None, True, False,
        attributes={
            'study_id': AttrInfo(AttrTypeEnum.FKEY, 'Study', True, DBTable.STUDY, 'study_id'),
            'kw_id': AttrInfo(AttrTypeEnum.FKEY, 'Keyword', True, DBTable.KEYWORD, 'kw_id')
        }),

    DBTable.STUDY_TO_PUB: TableInfo(
        '', '', None, True, False,
        attributes={
            'study_id': AttrInfo(AttrTypeEnum.FKEY, 'Study', True, DBTable.STUDY, 'study_id'),
            'pub_id': AttrInfo(AttrTypeEnum.FKEY, 'Publication', True, DBTable.PUB, 'pub_id')
        }),

    DBTable.SESSION: TableInfo(
        'Experiment sessions', 'session', None, False, True,
        attributes={
            'experimenter': AttrInfo(AttrTypeEnum.FKEY, 'Experimenter', True, DBTable.USER, 'username'),
            'subj_id': AttrInfo(AttrTypeEnum.FKEY, 'Subject ID', True, DBTable.SUBJECT, 'subj_id'),
            'session_date': AttrInfo(
                AttrTypeEnum.DATE, 'Session Date', True, None, None, None, '100px', [10, 10], None, None, 'YYYY-MM-DD'),
            'session_sfx': AttrInfo(
                AttrTypeEnum.INT, 'Suffix', True, None, None, None, '50px', [1, 1], None, None,
                'Enter an integer in [0..9] to distinguish multiple sessions on the same date'),
            'rig_id': AttrInfo(AttrTypeEnum.FKEY, 'Rig', False, DBTable.RIG, 'rig_id'),
            'study_id': AttrInfo(AttrTypeEnum.FKEY, 'Study', False, DBTable.STUDY, 'study_id'),
            'session_notes': AttrInfo(
                AttrTypeEnum.TEXT, 'Notes', False, None, None, None, '500px', [0, 2048], r'[\s\S]*', None,
                'Enter any notes about this particular session (optional, up to 2048 chars)')
        }),

    DBTable.SESSION_EPHYS: TableInfo(
        'EPhys Recording', 'recording', DBTable.SESSION, False, True,
        attributes={
            'ephys_src': AttrInfo(
                AttrTypeEnum.ENUM, 'Recording Source', False, None, None,
                ['Omniplex', 'Omniplex clips', 'Plexon MAP', 'Maestro Waveform', 'Maestro Spike Ch'], '100px'),
            'probe_type': AttrInfo(
                AttrTypeEnum.ENUM, 'Probe Type', False, None, None, ['single', '32-channel', 'other'], '100px'),
            'sampling_rate': AttrInfo(
                AttrTypeEnum.FLOAT, 'Sample Rate (Hz)', False, None, None, None, '100px', [2, 10], None, None,
                'Enter the electrode sampling rate in Hz'),
            'probe_x': AttrInfo(
                AttrTypeEnum.FLOAT, 'Probe X (mm)', False, None, None, None, '100px', [2, 10], None, None,
                'Enter the X-coordinate of probe within recording cylinder implant (mm)'),
            'probe_y': AttrInfo(
                AttrTypeEnum.FLOAT, 'Probe Y (mm)', False, None, None, None, '100px', [2, 10], None, None,
                'Enter the Y-coordinate of probe within recording cylinder implant (mm)'),
            'probe_depth': AttrInfo(
                AttrTypeEnum.FLOAT, 'Probe Depth (mm)', False, None, None, None, '100px', [2, 10], None, None,
                'Enter insertion depth of probe (mm)'),
            'ba_id': AttrInfo(AttrTypeEnum.FKEY, 'Target Region', False, DBTable.BRAIN_AREA, 'ba_id')
        }),

    DBTable.SESSION_NEURON: TableInfo(
        'Neural Units', 'unit', DBTable.SESSION, False, False,
        attributes={
            'unit_id': AttrInfo(AttrTypeEnum.INT, 'Unit #', True, None, None, None, '50px'),
            'unit_channel': AttrInfo(AttrTypeEnum.TEXT, 'Source Channel', False, None, None, None, '75px'),
            'unit_type': AttrInfo(AttrTypeEnum.FKEY, 'Neuron Type', False, DBTable.NEURON_TYPE, 'nt_id'),
            'unit_rate': AttrInfo(AttrTypeEnum.FLOAT, 'Mean Firing Rate (Hz)', False, None, None, None, '100px'),
            'unit_snr': AttrInfo(AttrTypeEnum.FLOAT, 'SNR', False, None, None, None, '100px'),
            'unit_template': AttrInfo(AttrTypeEnum.BLOB, 'Template Waveform (10ms)', False)
        }),

    # NOTE: The tables below this line do not involve manual user entry and are not displayed in tabular fashion.

    DBTable.TRIAL_PROTOCOL: TableInfo(
        'Trial Protocols', 'protocol', None, False, False,
        attributes={
            'proto_hash': AttrInfo(AttrTypeEnum.TEXT, 'MD5 Hash', True),
            'proto_name': AttrInfo(AttrTypeEnum.TEXT, 'Trial Name', False, None, None, None, '100px'),
            'proto_set': AttrInfo(AttrTypeEnum.TEXT, 'Trial Set', False, None, None, None, '100px'),
            'proto_subset': AttrInfo(AttrTypeEnum.TEXT, 'Trial Subset', False, None, None, None, '100px'),
            'proto_def': AttrInfo(AttrTypeEnum.BLOB, 'Definition', False)
        }),

    DBTable.TRIAL: TableInfo(
        'Session Trials', 'trial', None, False, False,
        attributes={
            'experimenter': AttrInfo(AttrTypeEnum.FKEY, 'Experimenter', True, DBTable.USER, 'username'),
            'subj_id': AttrInfo(AttrTypeEnum.FKEY, 'Subject ID', True, DBTable.SUBJECT, 'subj_id'),
            'session_date': AttrInfo(AttrTypeEnum.FKEY, 'Session Date', True, DBTable.SESSION, 'session_date'),
            'session_sfx': AttrInfo(AttrTypeEnum.FKEY, 'Session Suffix', True, DBTable.SESSION, 'session_sfx'),
            'proto_hash': AttrInfo(AttrTypeEnum.FKEY, 'Protocol ID', True, DBTable.TRIAL_PROTOCOL, 'proto_hash'),
            'trial_idx': AttrInfo(AttrTypeEnum.INT, 'Trial Index', True, None, None, None, '50px'),
            'trial_header': AttrInfo(AttrTypeEnum.BLOB, 'Header', False),
            'trial_filename': AttrInfo(AttrTypeEnum.TEXT, 'File Name', False),
            'trial_dur': AttrInfo(AttrTypeEnum.INT, 'Recorded Duration (ms)', False),
            'trial_record_start': AttrInfo(AttrTypeEnum.INT, 'Record Start (ms)', False),
            'trial_success': AttrInfo(AttrTypeEnum.BOOL, 'Success', False),
            'trial_rewarded': AttrInfo(AttrTypeEnum.BOOL, 'Reward Given', False),
            'trial_rew1': AttrInfo(AttrTypeEnum.INT, 'Reward Pulse 1 (ms)', False),
            'trial_rew2': AttrInfo(AttrTypeEnum.INT, 'Reward Pulse 2 (ms)', False),
            'trial_ts': AttrInfo(AttrTypeEnum.FLOAT, 'Timestamp (s)', False),
            'trial_rvs': AttrInfo(AttrTypeEnum.BLOB, 'RV', False)
        }),

    DBTable.TRIAL_EVENT: TableInfo(
        'Digital Events', 'event', DBTable.TRIAL, False, False,
        attributes={
            'event_ch': AttrInfo(AttrTypeEnum.INT, 'Event Ch#', True),
            'event_times': AttrInfo(AttrTypeEnum.BLOB, 'Event Times (s)', False)
        }),

    DBTable.TRIAL_BEHAVIORAL: TableInfo(
        'Behavioral Responses', 'response', DBTable.TRIAL, False, False,
        attributes={
            'response_id': AttrInfo(
                AttrTypeEnum.ENUM, 'Response ID', True, None, None, ['HEPOS', 'VEPOS', 'HEVEL', 'VEVEL', 'HDVEL']),
            'response_trace': AttrInfo(AttrTypeEnum.BLOB, 'Response Trace', False)
        }),

    DBTable.TRIAL_NEURONAL: TableInfo(
        'Neuronal Responses', 'response', DBTable.TRIAL, False, False,
        attributes={
            'unit_id': AttrInfo(AttrTypeEnum.FKEY, 'Unit #', True, DBTable.SESSION_NEURON, 'unit_id'),
            'spike_times': AttrInfo(AttrTypeEnum.BLOB, 'Spike Train', False)
        })
}
