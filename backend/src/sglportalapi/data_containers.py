"""
data_containers.py: Definition of the data objects that can be retrieved from the Lisberger lab portal API.

This module includes classes and methods shared by the server-side and client-side implementations of the Lisberger
lab portal API.

The supported API route endpoint paths are defined here, as well as methods for serializing and deserializing the
response to/from any endpoint.

Endpoint responses may encapsulate any of four kinds of objects: experiment session metadata, neural unit metadata,
Maestro trial protocol definitions, and individual trial reps. Three of the four objects are defined in this module:
`SessionInfo`, `NeuronInfo`, and `TrialRep`. The `Protocol` class in `sglportalapi.maestro` encapsulates a trial
protocol definition.

Author: saruffner
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import base64
import functools
import json
import struct
from datetime import date
from enum import IntFlag
from typing import Dict, Any, Optional, Tuple, List, Union, Final, Type

import numpy as np

from sglportalapi.maestro import Protocol

API_VERSION: int = 2
""" The current version number for the portal database access API. """


class MetadataTable:
    """
    A client-side container encapsulating the contents of one of the "general information" metadata tables in the portal
    database. These small tables contain information used to describe, categorize, and search for experimental datasets:
     - `SUBJECTS`: Experiment subjects.
     - `IMPLANTS`: Implant surgeries for all subjects.
     - `RIGS`: Experiment rigs.
     - `STUDIES`: Research projects in the lab.
     - `NEURON_TYPES`: The types of neurons studied in the lab.
     - `BRAIN_AREAS`: The brain regions studied in the lab.

    MetadataTable is a convenience class for serializing/deserializing the contents of these tables for retrieval from
    a dedicated portal API endpoint and textual display on the clientside. It does not expose the schema of the
    underlying database table.
    """
    SUBJECTS: Final[str] = 'Experiment Subjects'
    """ Information about laboratory subjects. """
    IMPLANTS: Final[str] = 'Subject Implants'
    """ Surgical implant history across all experiment subjects. """
    RIGS: Final[str] = 'Experiment Rigs'
    """ List of rigs in which laboratory experiments are conducted. """
    STUDIES: Final[str] = 'Research Projects'
    """ Current or past lab research projects. """
    NEURON_TYPES: Final[str] = 'Neuron Types'
    """ List of neuron types studied in the laboratory. """
    BRAIN_AREAS: Final[str] = 'Brain Areas'
    """ List of brain regions studied in the laboratory. """

    _META_TABLE_NAMES: List[str] = [SUBJECTS, IMPLANTS, RIGS, STUDIES, NEURON_TYPES, BRAIN_AREAS]
    """ List of all supported API routes. """

    @classmethod
    def is_supported_table_name(cls, name: str) -> bool:
        """
        Does the specified name identify one of the small metadata tables retrievable via the portal API?

        Args:
            name: The table name.
        Returns:
            True if name identifies a metadata table; else False.
        """
        return name in cls._META_TABLE_NAMES

    def __init__(self, info: Dict[str, Any]):
        """
        Construct a MetadataTable.

        Args:
            info: A dictionary with 3 keys: 'name' (str) is the table name; 'columns' (List[str]) holds the table column
                headings; and 'rows' (List[List[str]]) are the corresponding table rows.
        Raises:
            ValueError: If dictionary argument is missing any required keys, or any key value is deemed invalid.
        """
        try:
            if not MetadataTable.is_supported_table_name(info['name']):
                raise ValueError(f"Unrecognized metadata table name {info['name']}")
            if not all([isinstance(k, str) for k in info['columns']]):
                raise ValueError(f"Missing or invalid column heading")
            num_cols = len(info['columns'])
            for row in info['rows']:
                if (len(row) != num_cols) or not all([isinstance(k, str) for k in row]):
                    raise ValueError(f"Invalid row")
        except Exception as e:
            raise ValueError(f"Invalid metadata table initialization dict: {str(e)}")
        self._info = info

    @staticmethod
    def from_database_rows(name: str, table_rows: List[Dict[str, Any]]) -> MetadataTable:
        """
        Construct a MetadataTable object representing the content of a portal database table. **For internal use only
        by the portal API endpoint that handles requests for metadata table contents.**

        Args:
            name: The name of a table in the portal database.
            table_rows: List of all rows retrieved from the database table.
        Raises:
            ValueError: If `table_rows` is empty or contains entries inconsistent with the schema of the underlying
                database table, or if `name` is not recognized.
        """
        if not MetadataTable.is_supported_table_name(name):
            raise ValueError('Table name/ID not recognized')

        try:
            if name == MetadataTable.SUBJECTS:
                columns = ['Subject', 'Species', 'Date of Birth', 'Sex']
                rows = [[r['subj_id'], r['species'], str(r['dob']), r['sex']] for r in table_rows]
            elif name == MetadataTable.IMPLANTS:
                columns = ['Subject', 'Date', 'Stereotaxic Coordinates (AP,ML,DV in mm); (AP \u03b8, ML \u03b8)']
                rows = [[r['subj_id'], str(r['implant_date']),
                         f"({r['st_ap']}, {r['st_ml']}, {r['st_dv']}); "
                         f"({r['ap_angle']:.2f}, {r['ml_angle']:.2f})"] for r in table_rows]
            elif name == MetadataTable.RIGS:
                columns = ['Rig ID', 'Rig Location']
                rows = [[r['rig_id'], r['rig_loc']] for r in table_rows]
            elif name == MetadataTable.BRAIN_AREAS:
                columns = ['Brain Area']
                rows = [[r['ba_name']] for r in table_rows]
            elif name == MetadataTable.NEURON_TYPES:
                columns = ['Neuron Type']
                rows = [[r['nt_name']] for r in table_rows]
            else:  # MetadataTable.STUDIES
                columns = ['Study Title', 'Lead', 'Description']
                rows = [[r['study_title'], r['study_lead'], r['study_desc']] for r in table_rows]
            return MetadataTable(dict(name=name, columns=columns, rows=rows))
        except Exception:
            raise ValueError("Invalid or unexpected database table content")

    @property
    def table_name(self) -> str:
        """ The metadata table name, succinctly describing its purpose in the laboratory database. """
        return self._info['name']

    @property
    def column_headings(self) -> List[str]:
        """ The column headings for the metadata table. """
        return self._info['columns'].copy()

    @property
    def rows(self) -> List[List[str]]:
        """ The row contents of the metadata table. Each row contains a value for each table column. """
        return [r.copy() for r in self._info['rows']]

    def to_bytes(self) -> bytes:
        """ Serialize this object to a byte sequence. """
        out = dict(name=self._info['name'], columns=self._info['columns'], rows=self._info['rows'])
        return json.dumps(out).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> MetadataTable:
        """ Reconstruct MetadataTable from a byte sequence previously generated by `to_bytes()`. """
        info: Dict[str, Any] = json.loads(raw.decode())
        return MetadataTable(info)


class SessionInfo:
    """
    Information about an experiment session stored in the Lisberger lab portal database.
    """
    __REQUIRED_TYPES: Dict[str, type] = dict(
        experimenter=str, subj_id=str, session_date=str, session_sfx=int, rig_id=str, study_id=int, study_title=str,
        session_notes=str, num_trials=int, num_units=int

    )
    __EPHYS_TYPES: Dict[str, type] = dict(
        ephys_src=str, probe_type=str, sampling_rate=float, probe_x=float, probe_y=float,
        probe_depth=float, ba_id=int, brain_area=str
    )

    @staticmethod
    def _validate_init_arg(info: Dict[str, Any]) -> None:
        """
        Validates the dictionary defining session information. For a behavior-only session, all keys related to the
        electrophysiological recording are set to None.

        Args:
            info: A dictionary containing session information.
        Raises:
            ValueError: If any key is missing or any value is invalid.
        """
        try:
            if not isinstance(info, dict):
                raise ValueError(f'Expected a dictionary, got {type(info)}')
            if not all([(type(info[k]) == t) for k, t in SessionInfo.__REQUIRED_TYPES.items()]):
                raise ValueError('Invalid data type for one or more fields in session information dict')
            if info['num_units'] > 0:
                if not all([(type(info[k]) == t) for k, t in SessionInfo.__EPHYS_TYPES.items()]):
                    raise ValueError('Invalid data type for one or more fields in session information dict')
            else:
                for k in SessionInfo.__EPHYS_TYPES.keys():
                    info[k] = None
        except KeyError:
            raise ValueError('One or more missing fields in session information dict')

        try:
            date.fromisoformat(info['session_date'])
        except Exception:
            raise ValueError("Invalid date string for key 'session_date'")

    def __init__(self, info: Dict[str, Any]):
        """
        Experiment session information object. Use read-only properties to access the information.

        Args:
            info: Dictionary containing session information as gleaned from portal database.
        Raises:
            ValueError: If dictionary argument is missing any required keys, or any key value is deemed invalid.
        """
        SessionInfo._validate_init_arg(info)
        self._info = info

    def __str__(self):
        return str(self._info)

    @property
    def primary_key(self) -> Dict[str, Any]:
        """
        The experiment session's primary key within the releveant table in the Lisberger lab portal database. Note
        that the session recording date is an ISO-formatted string 'YYYY-MM-DD' rather than a Python date object.
        """
        return dict(experimenter=self.experimenter, subj_id=self.subject, session_date=self._info['session_date'],
                    session_sfx=self.suffix)

    @property
    def experimenter(self) -> str:
        """ The registered username of the researcher that performed the experiment. """
        return self._info['experimenter']

    @property
    def subject(self) -> str:
        """ ID of the subject for the experiment. """
        return self._info['subj_id']

    @property
    def recording_date(self) -> date:
        """ The date on which the experiment session was recorded. """
        return date.fromisoformat(self._info['session_date'])

    @property
    def iso_recording_date(self) -> str:
        """ The date on which the experiment session was recorded, as a string is ISO format: 'YYYY-MM-DD'. """
        return self._info['session_date']

    @property
    def suffix(self) -> int:
        """ Experiment session suffix (to distinguish multiple sessions on the same recording date). """
        return self._info['session_sfx']

    @property
    def number_of_trials(self) -> int:
        """ Total number of Maestro trials recorded during the experiment. """
        return self._info['num_trials']

    @property
    def number_of_units(self) -> int:
        """ Number of distinct neural units recorded during the experiment. 0 for behavioral-only sessions. """
        return self._info['num_units']

    @property
    def rig(self) -> str:
        """ ID of rig on which experiment session was recorded."""
        return self._info['rig_id']

    @property
    def study(self) -> str:
        """ Title of the larger research study to which this experiment session belongs. """
        return self._info['study_title']

    def notes(self) -> str:
        """ Specific notes regarding this experiment session, as supplied by the experimenter or committer. """
        return self._info['session_notes']

    @property
    def brain_area(self) -> Optional[str]:
        """ Brain area in which neural units were recorded during the experiment. None for behavior-only session. """
        return self._info['brain_area']

    @property
    def ephys_source(self) -> Optional[str]:
        """ Name of electrophysiological recording source for the experiment. None for behavior-only session. """
        return self._info['ephys_src']

    @property
    def ephys_probe_type(self) -> Optional[str]:
        """ Type of probe used for electrophysiological recording during experiment. None for behavior-only session. """
        return self._info['probe_type']

    @property
    def ephys_sampling_rate(self) -> Optional[float]:
        """ Sampling rate for electrophysiological recordings during experiment. None for behavior-only session. """
        return self._info['sampling_rate']

    @property
    def ephys_probe_location(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        A tuple (x, y, depth) specifying the electrophysiological probe location within the recording cylinder implant
        and its insertion depth in mm. (None, None, None) for behavior-only session.
        """
        return self._info['probe_x'], self._info['probe_y'], self._info['probe_depth']

    def to_bytes(self) -> bytes:
        """ Serialize this object to a byte sequence. """
        return json.dumps(self._info).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> SessionInfo:
        """ Reconstruct SessionInfo from a byte sequence previously generated by `to_bytes()`. """
        return SessionInfo(json.loads(raw.decode()))

    def session_table_entry(self) -> Dict:
        """ Database table entry for this experiment session. Internal use only. """
        return {k: self._info[k] for k in ['experimenter', 'subj_id', 'session_date', 'session_sfx', 'rig_id',
                                           'study_id', 'session_notes', 'num_trials', 'num_units']}

    def ephys_table_entry(self) -> Dict:
        """ Database table entry for this experiment session's electrophysiology parameters. Internal use only. """
        if self.number_of_units <= 0:
            return dict()
        else:
            return {k: self._info[k] for k in ['experimenter', 'subj_id', 'session_date', 'session_sfx', 'ephys_src',
                                               'probe_type', 'sampling_rate', 'probe_x', 'probe_y', 'probe_depth',
                                               'ba_id']}


class NeuronInfo:
    """
    Information about a recorded neural unit with response data stored in the Lisberger lab portal database.
    """
    __CONTENT_TYPES: Dict[str, type] = dict(
        experimenter=str, subj_id=str, session_date=str, session_sfx=int, unit_id=int, unit_channel=str,
        unit_rate=float, unit_spikes=int, unit_snr=float, unit_template=np.ndarray, neuron_type=str
    )
    """ 
    Defines the keys and corresponding value types for the serialized dictionary sent 'over the wire' that contains 
    information about a recorded neural unit.
    """

    @staticmethod
    def _validate_init_arg(info: Dict[str, Any]) -> None:
        """
        Validates the dictionary defining information about a neural unit.

        Args:
            info: A dictionary containing session information.
        Raises:
            ValueError: If any key is missing or any value is invalid.
        """
        if not isinstance(info, dict):
            raise ValueError(f'Expected a dictionary, got {type(info)}')
        all_keys = info.keys()
        if not set(NeuronInfo.__CONTENT_TYPES.keys()).issubset(all_keys):
            raise ValueError('One or more missing fields in neuron information dict')
        if not all([(type(info[k]) == NeuronInfo.__CONTENT_TYPES[k]) for k in all_keys]):
            raise ValueError('Invalid data type for one or more fields in the neuron information dict')
        try:
            date.fromisoformat(info['session_date'])
        except Exception:
            raise ValueError("Invalid date string for key 'session_date'")

    def __init__(self, info: Dict[str, Any]):
        """
        Summary information for a recorded neural unit stored in the Lisberger lab portal database. Use read-only
        properties to access the information.

        Args:
            info: Dictionary containing neural unit information as gleaned from portal database.
        Raises:
            ValueError: If dictionary argument is missing any required keys, or any key value is deemed invalid.
        """
        NeuronInfo._validate_init_arg(info)
        self._info = info

    def __str__(self):
        # we do it manually here so we don't show the entire unit template array
        out = list()
        out.append("{")
        for k, v in self._info.items():
            out.append(f"'{k}':")
            if isinstance(v, list):
                out.append(f"[list, N={len(v)}],")
            elif isinstance(v, np.ndarray):
                out.append(f"{np.array2string(v, threshold=4)},")
            else:
                out.append(f"{str(v)},")
        out.append("}")
        return " ".join(out)

    @property
    def primary_key(self) -> Dict[str, Any]:
        """
        The neural unit's unique primary key within the relevant table in the Lisberger lab portal database. Note
        that the session recording date is an ISO-formatted string 'YYYY-MM-DD' rather than a Python date object.
        """
        return dict(experimenter=self.experimenter, subj_id=self.subject, session_date=self._info['session_date'],
                    session_suffix=self.suffix, unit_id=self.id)

    @property
    def experimenter(self) -> str:
        """ The registered username of the researcher that performed the experiment during which unit was recorded. """
        return self._info['experimenter']

    @property
    def subject(self) -> str:
        """ ID of the subject in which the neural unit was recorded. """
        return self._info['subj_id']

    @property
    def recording_date(self) -> date:
        """ The date on which the neural unit was recorded. """
        return date.fromisoformat(self._info['session_date'])

    @property
    def suffix(self) -> int:
        """ Suffix for experiment session during which the neural unit was recorded. """
        return self._info['session_sfx']

    @property
    def id(self) -> int:
        """ ID of neural unit within the recording session. """
        return self._info['unit_id']

    @property
    def channel(self) -> str:
        """ Name of source channel on which this neurol unit was recorded. """
        return self._info['unit_channel']

    @property
    def mean_firing_rate(self) -> float:
        """ The neural unit's mean firing rate (Hz) over the course of the electrophysiological recording. """
        return self._info['unit_rate']

    @property
    def number_of_spikes(self) -> int:
        """ Total number of spikes detected from unit over the course of the electrophysiological recording. """
        return self._info['unit_spikes']

    @property
    def snr(self) -> float:
        """ Estimated signal-to-noise ratio for neural unit. """
        return self._info['unit_snr']

    @property
    def spike_template_waveform(self) -> np.ndarray:
        """ The neural unit's spike template waveform (10ms duration at recorded sampling rate). """
        return np.array(self._info['unit_template'])

    @property
    def neuron_type(self) -> str:
        """ Neuron type assigned to this neural unit. """
        return self._info['neuron_type']

    def to_bytes(self) -> bytes:
        """ Serialize this object to a byte sequence. """
        return json.dumps(self._info, cls=_CustomJSONEncoder).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> NeuronInfo:
        """ Reconstruct NeuronInfo from a byte sequence previously generated by `to_bytes()`. """
        return NeuronInfo(json.loads(raw.decode(), object_hook=_CustomJSONEncoder.decoder_hook))


class RequestedData(IntFlag):
    """
    Enumeration of the different types of trial data that may be requested from the portal and encapsulated within a
    :py:class:`sglportalapi.data_containers.TrialRep` object:
     - BEHAVIORAL: Behavioral response traces (horizontal and vertical eye position, velocity).
     - NEURONAL: Neural unit spike trains.
     - EVENTS: Digital input marker event times during trial (including eye-blink epochs if available).
     - ALL: All behavioral, neuronal, and event data.
    """
    BEHAVIORAL = 1 << 0
    """ Behavioral response traces (H,V eye position, velocity). """
    NEURONAL = 1 << 1
    """ Neural unit spike trains. """
    EVENTS = 1 << 2
    """ Digital input marker event times during trial (including eye-blink epochs if available). """
    ALL = 7
    """ All behavioral, neuronal and event data. """


class TrialRep:
    """
    A container for behavioral and neuronal response data recorded during a single presentation of a Maestro trial
    committed to the Lisberger lab portal database, along with various metadata about the trial presented.
    """
    __CONTENT_TYPES: Dict[str, type] = dict(
        experimenter=str, subj_id=str, session_date=str, session_sfx=int, trial_idx=int, protocol=Protocol,
        trial_rvs=list, trial_filename=str, trial_dur=int, trial_record_start=int, trial_success=bool,
        trial_rewarded=bool, trial_rew1=int, trial_rew2=int, vstab_win_len=int, trial_ts=float, hgpos=np.ndarray,
        vepos=np.ndarray, hevel=np.ndarray, vevel=np.ndarray, events=dict, spike_trains=dict
    )
    """ 
    Dictionary defines the keys and corresponding value types for the serializeed dictionary sent 'over the wire' that
    contains the response data and other information for a single trial rep.
    """

    @staticmethod
    def _validate_init_arg(info: Dict[str, Any]) -> None:
        """
        Validates the dictionary containing response data and other information for a single Maestro trial rep.

        Args:
            info: A dictionary containing session information.
        Raises:
            ValueError - If any key is missing or any value is invalid.
        """
        all_keys = info.keys()
        if not set(TrialRep.__CONTENT_TYPES.keys()).issubset(all_keys):
            raise ValueError('One or more missing fields in trial rep')
        for k in all_keys:
            ok = (type(info[k]) == TrialRep.__CONTENT_TYPES[k]) or \
                 ((k in ['hgpos', 'vepos', 'hevel', 'vevel']) and (info[k] is None))
            if not ok:
                raise ValueError(f"Invalid data type for field '{k}' [{str(type(info[k]))}] in trial rep definition")
        if not all([isinstance(v, (int, float)) for v in info['trial_rvs']]):
            raise ValueError('Invalid key "trial_rvs"')
        if not all([isinstance(v, np.ndarray) for v in info['events'].values()]):
            raise ValueError('Invalid key "events"')
        if not all([isinstance(v, (np.ndarray, type(None))) for v in info['spike_trains'].values()]):
            raise ValueError('Invalid key "spike_trains"')

        try:
            date.fromisoformat(info['session_date'])
        except Exception:
            raise ValueError("Invalid date string for key 'session_date'")

    def __init__(self, info: Dict[str, Any]):
        """
        Recorded response data and other information about a single Maestro trial presented during an experiment
        session that is stored in the Lisberger lab portal database.

        **Intended for internal use only on the portal server to prepare trial data sets for transfer to the requesting
        client. On the client-side, use the read-only properties to access response traces and other trial metadata.**

        Args:
            info: Dictionary containing the trial data as gleaned from portal database.
        Raises:
            ValueError: If dictionary argument is missing any required keys, or any key value is deemed invalid.
        """
        TrialRep._validate_init_arg(info)
        self._info = info
        # these 'computed' data are only prepared when requested. They are never serialized.
        self._fix1_pos: Optional[np.ndarray] = None
        self._fix2_pos: Optional[np.ndarray] = None
        self._fix1_on_epochs: Optional[List[int]] = None
        self._fix2_on_epochs: Optional[List[int]] = None

    def __str__(self):
        # customize string rep to prettify output a bit and not show all of Numpy arrays
        out = list()
        out.append("{\n")
        for k, v in self._info.items():
            out.append(f"  '{k}': ")
            if isinstance(v, np.ndarray):
                out.append(f"{np.array2string(v, threshold=4)},\n")
            elif isinstance(v, Protocol):
                p: Protocol = v
                out.append(f"{p.trial.path_name} [#segs={len(p.trial.segments)}, #tgts={len(p.trial.targets)}],\n")
            elif isinstance(v, dict):
                out.append("{\n")
                for k2, v2 in v.items():
                    out.append(f"    '{k2}': ")
                    if isinstance(v2, np.ndarray):
                        out.append(f"{np.array2string(v2, threshold=4)},\n")
                    else:
                        out.append(f"{str(v2)},\n")
                out.append("  }\n")
            else:
                out.append(f"{str(v)},\n")
        out.append("}\n")
        return " ".join(out)

    @property
    def experimenter(self) -> str:
        """ Registered username of the researcher that performed the experiment during which trial was presented. """
        return self._info['experimenter']

    @property
    def subject(self) -> str:
        """ ID of the experiment subject to which the trial was presented. """
        return self._info['subj_id']

    @property
    def recording_date(self) -> date:
        """ The date on which the trial was presented. """
        return date.fromisoformat(self._info['session_date'])

    @property
    def suffix(self) -> int:
        """ Suffix for experiment session during which the trial was presented. """
        return self._info['session_sfx']

    @property
    def index(self) -> int:
        """ The trial index, which typically indicates the order in which trials were presented during the session. """
        return self._info['trial_idx']

    @property
    def protocol(self) -> Protocol:
        """
        The Maestro trial protocol to which this trial rep belongs. Defines target trajectories and other important
        features of the trial presentation.

        NOTE: This is the definition of the trial protocol, not this specific trial rep. When the protocol includes any
        random variables (such as a random segment duration), then each trial rep will be different IAW the particular
        values assigned to those random variables.
        """
        return self._info['protocol']

    @property
    def rv_values(self) -> List[Union[int, float]]:
        """
        The actual values assigned to any random variables (RV) defined in the trial protocol to generate this specific
        trial rep. This information is needed, for example, to accurately calculate target trajectories for this rep.
        Of course, if the trial protocol has no defined RVs, then every rep of that protocol is the same.

        Returns:
            The list of value assigned to any random variables to generate this rep of the trial protocol. Order in
                list matches the order in which the RVs are defined in the protocol. Will be empty if the protocol lacks
                any random variables.
        """
        return self._info['trial_rvs']

    @property
    def filename(self) -> str:
        """ Original filename of Maestro data file in which response data and other information was saved. """
        return self._info['trial_filename']

    @property
    def duration(self) -> int:
        """ Recorded duration of this trial, in milliseconds. """
        return self._info['trial_dur']

    @property
    def segment_durations(self) -> List[int]:
        """
        The segment durations for this particular trial rep, in milliseconds. A segment's duration will vary from one
        trial presentation to the next if its duration is controlled by a random variable.
        """
        return self.protocol.segment_durations_for_rep(self.rv_values)

    @property
    def record_start(self) -> int:
        """ Time at which recording began after trial start, in milliseconds (typicallly 0). """
        return self._info['trial_record_start']

    @property
    def success(self) -> bool:
        """ True if trial was completed successfully by the subject. """
        return self._info['trial_success']

    @property
    def rewarded(self) -> bool:
        """ True if subject was rewarded at trial's end (could be false if random reward withholding in effect). """
        return self._info['trial_rewarded']

    @property
    def reward1_dur(self) -> int:
        """ Duration of reward pulse #1 in milliseconds. """
        return self._info['trial_rew1']

    @property
    def reward2_dur(self) -> int:
        """ Duration of reward pulse #2 in milliseconds. """
        return self._info['trial_rew2']

    @property
    def vstab_window_length(self) -> int:
        """ Sliding window length for smoothing eye position during velocity stabilization, in milliseconds. """
        return self._info['vstab_win_len']

    @property
    def timestamp(self) -> float:
        """ Trial start timestamp, in seconds since start of first trial in experiment session (<0 if unknown). """
        return self._info['trial_ts']

    @property
    def hgpos(self) -> Optional[np.ndarray]:
        """
        Subject's horizontal gaze position during trial, in degrees (1-ms sampling period). None if gaze position
        was not recorded (rare), **OR if behavioral response traces were not requested**.
        """
        return self._info['hgpos']

    @property
    def vepos(self) -> Optional[np.ndarray]:
        """
        Subject's vertical eye position trajectory during trial, in degrees (1-ms sampling period). None if vertical
        eye position was not recorded (rare), **OR if behavioral response traces were not requested**.
        """
        return self._info['vepos']

    @property
    def hevel(self) -> Optional[np.ndarray]:
        """
        Subject's horizontal eye velocity trajectory during trial, in deg/sec (1-ms sampling period). None if
        horizontal eye velocity was not recorded (rare), **OR if behavioral response traces were not requested**.
        """
        return self._info['hevel']

    @property
    def vevel(self) -> Optional[np.ndarray]:
        """
        Subject's vertical eye velocity trajectory during trial, in deg/sec (1-ms sampling period). None if vertical
        eye velocity was not recorded (rare), **OR if behavioral response traces were not requested**.
        """
        return self._info['vevel']

    @property
    def events(self) -> Dict[int, np.ndarray]:
        """
        Timestamps (in seconds relative to trial start) of any marker pulses detected on the digital inputs in the
        recording rig. **The returned dictionary will be empty if event timestamps were not requested.**

        Returns:
            A dictionary mapping the input channel # (in 0..15) to a Numpy array of the timestamps of marker pulse
                events detected on that channel. Only channels on which at least one pulse occurred are included; the
                dictionary could be empty if no pulses were detected on any of the available digital inputs over the
                course of the trial, or if event timestamps were not requested.
        """
        return self._info['events']

    @property
    def spike_trains(self) -> Dict[int, Optional[np.ndarray]]:
        """
        Neural unit spike trains recorded during trial, with spike times in seconds since trial start. **The returned
        dictionary will be empty if neural responses were not requested.**

        Returns:
            A dictionary mapping the unit ID to a Numpy array holding the timestamps of any spikes detected from that
                neural unit during the trial. If the array is empty, then the corresponding unit was recorded during the
                trial but no spikes occurred. However, if the array is None, then that unit was not recorded during
                the trial (units may be acquired and lost any time during an experiment session). The dictionary will
                be empty if no neuronal response data was requested for this trial rep.
        """
        return self._info['spike_trains']

    @property
    def fix1_pos(self) -> np.ndarray:
        """
        Computed position trajectory of fixation target #1 during this trial rep, as Nx2 Numpy array -- with horizontal
        position in column 0 and vertical position in column 1, in degrees subtended at eye. If fixation target #1 was
        not used at all, returns a zero-length Numpy array. Otherwise, during any portion of the trial in which fixation
        target #1 is undefined, its position is (NaN, NaN).

        **NOTE: If velocity stabilization was in effect during the trial, the fixation target trajectory is adjusted
        accordingly -- but only if behavioral response data was requested , and both horizontal and vertical eye
        position traces were recorded during the trial.**
        """
        if self._fix1_pos is None:
            self._init_fixation_target_trajectories()
        return self._fix1_pos

    @property
    def fix2_pos(self) -> np.ndarray:
        """
        Computed position trajectory of fixation target #2 during this trial rep, as Nx2 Numpy array -- with horizontal
        position in column 0 and vertical position in column 1, in degrees subtended at eye. If fixation target #2 was
        not used at all, returns a zero-length Numpy array. Otherwise, during any portion of the trial in which fixation
        target #2 is undefined, its position is (NaN, NaN).

        **NOTE: If velocity stabilization was in effect during the trial, the fixation target trajectory is adjusted
        accordingly -- but only if behavioral response data was requested , and both horizontal and vertical eye
        position traces were recorded during the trial.**
        """
        if self._fix2_pos is None:
            self._init_fixation_target_trajectories()
        return self._fix2_pos

    def _init_fixation_target_trajectories(self) -> None:
        fix1, fix2 = self.protocol.compute_fixation_target_trajectories(self.rv_values, self.hgpos, self.vepos,
                                                                        self.vstab_window_length)
        self._fix1_pos = np.zeros(shape=(0, 2), dtype=np.float32) if fix1 is None else fix1
        self._fix2_pos = np.zeros(shape=(0, 2), dtype=np.float32) if fix2 is None else fix2

    @property
    def fix1_on_epochs(self) -> List[int]:
        """
        Computed epochs during which designated fixation target #1 is ON over the course of this trial rep. The returned
        list of 2*N elapsed times (ms since trial start) [S1, E1, S2, E2, ..., SN, EN] specify the N non-overlapping ON
        epochs, in chronological order. If the target was not used or never turned on, the list is empty.
        """
        if self._fix1_on_epochs is None:
            self._init_fixation_target_on_epochs()
        return self._fix1_on_epochs.copy()

    @property
    def fix2_on_epochs(self) -> List[int]:
        """
        Computed epochs during which designated fixation target #2 is ON over the course of this trial rep. The returned
        list of 2*N elapsed times (ms since trial start) [S1, E1, S2, E2, ..., SN, EN] specify the N non-overlapping ON
        epochs, in chronological order. If the target was not used or never turned on, the list is empty.
        """
        if self._fix2_on_epochs is None:
            self._init_fixation_target_on_epochs()
        return self._fix2_on_epochs.copy()

    def _init_fixation_target_on_epochs(self) -> None:
        self._fix1_on_epochs, self._fix2_on_epochs = self.protocol.compute_fixation_target_on_epochs(self.rv_values)

    def to_bytes(self) -> bytes:
        """ Serialize this object to a byte sequence. """
        return json.dumps(self._info, cls=_CustomJSONEncoder).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> TrialRep:
        """ Reconstruct TrialRep from a byte sequence previously generated by `to_bytes()`. """
        return TrialRep(json.loads(raw.decode(), object_hook=_CustomJSONEncoder.decoder_hook))

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
        spike_times = self.spike_trains[unit_id]
        firing_rate = np.zeros(self.duration)
        if (spike_times is None) or len(spike_times) < 2:
            return firing_rate  # not enough information to compute firing rate

        spikes_ms = np.floor(spike_times*1000.0).astype(int)
        num_spikes = len(spike_times)

        for i in range(num_spikes):
            t = spikes_ms[i]
            if i == 0:
                t_plus = spikes_ms[i+1]
                firing_rate[t:t_plus] = 1.0 / (spike_times[i+1] - spike_times[i])
            elif i == num_spikes - 1:
                t_minus = spikes_ms[i-1]
                t_last = min(2*t - t_minus, self.duration - 1)
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
            self, t_vel: float = 20, t_vel_max: float = 50, t_acc: float = 1250,
            t_acc_max: float = 2000, pre_ticks: int = 2, post_ticks: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Return the horizontal and vertical eye velocity traces for this trial rep with any saccade epochs replaced by
        NaN samples. This method ASSUMES a sampling rate of 1KHz!

        Args:
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
        if (self.hevel is None) and (self.vevel is None):
            return np.zeros(self.duration, dtype=np.float32), np.zeros(self.duration, dtype=np.float32)
        if self.hevel is None:
            hevel = np.zeros(self.duration, dtype=np.float32)
        else:
            hevel = np.copy(self.hevel)
        if self.vevel is None:
            vevel = np.zeros(self.duration, dtype=np.float32)
        else:
            vevel = np.copy(self.vevel)

        speed = np.sqrt(hevel ** 2 + vevel ** 2)
        acceleration = np.diff(speed) / 0.001   # sampling rate = 1KHz!!
        acceleration = np.append(acceleration, np.nan)
        acceleration = np.abs(acceleration)

        in_saccade = np.intersect1d(np.where(speed > t_vel)[0], np.where(acceleration > t_acc)[0])
        in_saccade = functools.reduce(
            np.union1d, (in_saccade, np.where(speed > t_vel_max)[0], np.where(acceleration > t_acc_max)[0]))

        saccading = False
        onset_indices = []
        offset_indices = []
        for i in range(1, len(in_saccade)):
            if in_saccade[i] == in_saccade[i-1] + 1:
                if not saccading:
                    onset_indices.append(max(0, in_saccade[i-1]-pre_ticks))
                    saccading = True
            elif saccading and (in_saccade[i] >= in_saccade[i-1]+post_ticks):
                offset_indices.append(in_saccade[i-1] + post_ticks)
                saccading = False
        if saccading:
            offset_indices.append(min(len(speed), in_saccade[-1]+post_ticks))

        for i in range(len(offset_indices)):
            hevel[onset_indices[i]:offset_indices[i]] = np.nan
            vevel[onset_indices[i]:offset_indices[i]] = np.nan

        return hevel, vevel


class _CustomJSONEncoder(json.JSONEncoder):
    """
    JSONEncoder subclass customized to jsonify one-dimensional Numpy float arrays and the Maestro trial protocol
    object. It is required in order to use JSON to serialize NeuronInfo and TrialRep objects.
    """
    def default(self, obj):
        if isinstance(obj, np.ndarray) and obj.ndim == 1:
            return {'_nparray_b64': base64.b64encode(obj.tobytes()).decode('utf-8')}
        elif isinstance(obj, Protocol):
            return {'_proto_b64': base64.b64encode(obj.to_bytes()).decode('utf-8')}
        return super(_CustomJSONEncoder, self).default(obj)

    @staticmethod
    def decoder_hook(dict_obj):
        if isinstance(dict_obj, dict) and (len(dict_obj.keys()) == 1) and ('_nparray_b64' in dict_obj):
            return np.frombuffer(base64.b64decode(dict_obj['_nparray_b64']))
        if isinstance(dict_obj, dict) and (len(dict_obj.keys()) == 1) and ('_proto_b64' in dict_obj):
            return Protocol.from_bytes(base64.b64decode(dict_obj['_proto_b64']))
        return dict_obj


class APISerializeError(Exception):
    """ An error that occurred while serializing or deserializing a response to/from the Lisberger lab portal API. """
    def __init__(self, reason: Optional[str] = None, cause: Optional[BaseException] = None):
        self.message = reason if reason else (f"Caused by {str(cause)}" if cause else "Undefined error")

    def __str__(self):
        return self.message


class Route:
    """
    A collection of all supported API routes. Each route is a defined subpath under the portal's base URL:
     - `Route.AUTHENTICATE`: API route by which client authenticates with portal and receives access token required
       for all other API endpoints.
     - `Route.SESSIONINFO`: API route to retrieve summary information on a filtered subset of experiment sessions in
       the portal database.
     - `Route.SESSION_NEURONS`: API route to retrieve summary information about selected neural units recorded during a
       specified experiment session.
     - `Route.SESSION_PROTOCOLS`: API route to retrieve the defintions of all distinct Maestro trial protocols presented
       during an experiment session.
     - `Route.SESSION_TRIAL`: API route to retrieve behavioral and neuronal responses for a single Maestro trial
       presented during an experiment session.
     - `Route.SESSION_BLOCK`: API route to retrieve response data, etc for a sequential block of Maestro trials
       presented during an experiment session.
     - `Route.SESSION_PROTOCOL_REPS`: API route to retrieve response data for all reps of a specified trial protocol
       during an experiment session.
     - `Route.METADATA_TABLE`: API route to retrieve the contents of one of the small metadata tables in the portal
       database.
     - `Route.NEURONS`: API route to search portal database for comparable neural units satisfying a set of filters.
    """
    AUTHENTICATE: Final[str] = '/api'
    SESSIONINFO: Final[str] = '/api/sessions'
    SESSION_NEURONS: Final[str] = '/api/session/neurons'
    SESSION_PROTOCOLS: Final[str] = '/api/session/protocols'
    SESSION_TRIAL: Final[str] = '/api/session/trial'
    SESSION_BLOCK: Final[str] = '/api/session/block'
    SESSION_PROTOCOL_REPS: Final[str] = '/api/session/protocol/reps'
    METADATA_TABLE: Final[str] = '/api/metadata'
    NEURONS: Final[str] = '/api/neurons'

    _KNOWN_ROUTES: List[str] = [
        AUTHENTICATE, SESSIONINFO, SESSION_NEURONS, SESSION_PROTOCOLS,
        SESSION_TRIAL, SESSION_BLOCK, SESSION_PROTOCOL_REPS, METADATA_TABLE, NEURONS
    ]
    """ List of all supported API routes. """

    @classmethod
    def is_supported_api(cls, route: str) -> bool:
        """
        Is the specified URL subpath a recognized and supported portal API endpoint?

        Args:
            route: The route subpath (beyond the portal's base URL).
        Returns:
            True for a valid API enpoint; else False.
        """
        return route in cls._KNOWN_ROUTES

    @classmethod
    def describe_api_request(cls, entry: Dict[str, Any]) -> Tuple[str, str]:
        """
        Provide a descriptor and parameter list for a logged API request.

        Args:
            entry: An API request log entry.

        Returns:
            A 2-tuple (R, P) containing a brief descriptor of the API request R and the list P of parameters that were
                part of the request. Both strings R and P are formatted in markdown text as they are intended for
                web browser display. For most API routes, the request R is the name of the clientside method that
                targets that route. Returns ('unknown', '') if log entry is invalid.
        """
        try:
            desc, param_list = cls._ROUTE_TO_DESCRIBE_INFO[entry['route']]
            params = ", ".join([f"**{p}**={entry[p]}" for p in param_list if p in entry])
            return desc, params
        except Exception:
            return '**unknown**', ''

    _ROUTE_TO_DESCRIBE_INFO: Dict[str, Tuple[str, List[str]]] = {
        '/api_client': ('API client package download', []),   # not really an API, but the URL for the download page
        AUTHENTICATE: ('API client access granted', []),
        SESSIONINFO: ('**sessions**', ['experimenter', 'subj_id', 'when']),
        SESSION_NEURONS: ('**session_neurons**', ['session_key', 'min_spikes', 'min_snr']),
        SESSION_PROTOCOLS: ('**session_protocols**', ['session_key']),
        SESSION_TRIAL: ('**session_trial**', ['session_key', 'trial_index', 'unit_ids', 'what']),
        SESSION_BLOCK: ('**session_trial_block**', ['session_key', 'start', 'end', 'unit_ids', 'what']),
        SESSION_PROTOCOL_REPS: ('**session_protocol_reps**',
                                ['session_key', 'proto_hash', 'completed', 'unit_ids', 'what']),
        METADATA_TABLE: ('**metadata_table**', ['table']),
        NEURONS: ('**neurons**', ['min_spikes', 'min_snr', 'min_rate', 'neuron_type', 'subj_id', 'study_title',
                                  'proto_hash', 'min_complete'])
    }
    """
    Maps API route name to a tuple (D, L), where D is a short description of the API function and L is a list of
    paramaeters (corresponding to keys in the dictionary defining the API request log entry. D is in Markdown format
    and L will be empty for any API that has no request parameters.
    """

    _ROUTE_TO_RESP_INFO: Dict[str, Tuple[str, Type, bool]] = {
        AUTHENTICATE: (None, None, None),
        SESSIONINFO: ('sessions', SessionInfo, True),
        SESSION_NEURONS: ('neurons', NeuronInfo, True),
        SESSION_PROTOCOLS: ('protocols', Protocol, True),
        SESSION_TRIAL: ('trial', TrialRep, False),
        SESSION_BLOCK: ('trials', TrialRep, True),
        SESSION_PROTOCOL_REPS: ('trials', TrialRep, True),
        METADATA_TABLE: ('metatable', MetadataTable, False),
        NEURONS: ('neurons', NeuronInfo, True)
    }
    """ 
    Maps API route name to a tuple (K, T, L), where K is the string key for the response field holding the object(s)
    returned; T is the object type; L==True if the response field is a list of objects of type T, else the response
    field is just an object of type T
    """

    @classmethod
    def serialize_api_response(cls, route: str, **kwargs) -> bytes:
        """
        Serialize a response from one of the Lisberger lab portal API endpoints.

        Args:
            route: The endpoint route name.
            kwargs: The response dictionary.
        Returns:
            A byte sequence encoding the response.
        Raises:
            APISerializeError: If endpoint route is invalid, if response dictionary is missing any required keyword,
                or if any error occurs while serializing the response.
        """

        try:
            out = bytearray()
            hdr: Dict[str, Any] = dict(route=route, version=API_VERSION)
            obj_key, obj_class, is_list = None, None, False
            if not cls.is_supported_api(route):
                raise ValueError(f"Unsupported API endpoint: {route}")
            elif 'error' in kwargs:
                hdr['error'] = kwargs['error']
            elif route == cls.AUTHENTICATE:
                hdr['token'], hdr['expires_in'] = kwargs['token'], kwargs['expires_in']
            else:
                obj_key, obj_class, is_list = cls._ROUTE_TO_RESP_INFO[route]
            if obj_key:
                hdr['num_objects'] = len(kwargs[obj_key]) if is_list else 1
            raw_hdr = json.dumps(hdr).encode()
            out.extend(struct.pack("<i", len(raw_hdr)))
            out.extend(raw_hdr)
            if obj_key:
                # NOTE: This relies on fact that all object types returned in an API response implement to_bytes()
                obj_list: List[Any] = kwargs[obj_key] if is_list else [kwargs[obj_key]]
                for o in obj_list:
                    raw_object = o.to_bytes()
                    out.extend(struct.pack("<i", len(raw_object)))
                    out.extend(raw_object)

            return bytes(out)
        except Exception as e:
            raise APISerializeError(cause=e)

    @classmethod
    def deserialize_api_response(cls, route: str, raw: bytes) -> Dict[str, Any]:
        """
        Deserialize the response object received from a Lisberger lab portal API endpoint.

        Args:
            route: The endpoint route name.
            raw: The byte sequence encoding the endpoint's response
        Returns:
            The deserialized response dictionary.
        Raises:
            APISerializeError: If the endpoint route name is invalid, if a required keyword is missing in the
                deserialized response dictionary, or if any error occurs during deserialization.
        """
        try:
            if not cls.is_supported_api(route):
                raise ValueError(f"Unsupported API endpoint: {route}")
            int_sz = struct.calcsize('<i')
            offset = 0
            hdr_sz, = struct.unpack_from('<i', raw, offset)
            offset += int_sz
            resp: Dict[str, Any] = json.loads(raw[offset:offset + hdr_sz].decode())
            offset += hdr_sz
            if resp['route'] != route:
                raise ValueError('Route mismatch in response!')
            elif resp['version'] != API_VERSION:
                raise ValueError(f"Invalid API version in response: {resp['version']}")
            elif route == Route.AUTHENTICATE:
                if not all([(k in resp) for k in ['token', 'expires_in']]):
                    raise KeyError(f"Missing one or more keys in response")
                return resp
            elif 'error' in resp:
                return resp

            # NOTE: This relies on fact that all object types returned in an API response implement from_bytes()
            obj_key, obj_class, is_list = cls._ROUTE_TO_RESP_INFO[route]
            num_objects = resp['num_objects']
            obj_list: List[obj_class] = list()
            for i in range(num_objects):
                info_sz, = struct.unpack_from('<i', raw, offset)
                offset += int_sz
                # noinspection PyUnresolvedReferences
                obj_list.append(obj_class.from_bytes(raw[offset:offset + info_sz]))
                offset += info_sz
            resp[obj_key] = obj_list if is_list else obj_list[0]
            return resp
        except Exception as e:
            raise APISerializeError(cause=e)
