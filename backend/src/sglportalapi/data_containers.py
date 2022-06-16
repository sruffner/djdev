"""
data_containers.py: Definition of the data objects that can be retrieved from the Lisberger lab portal API.

This module includes classes and methods shared by the server-side and client-side implementations of the Lisberger
lab portal API.

The supported API route endpoint paths are defined here, as well as methods for serializing and deserializing the
response to/from any endpoint.

Endpoint responses may encapsulate any of four kinds of objects: experiment session metadata, neural unit metadata,
Maestro trial protocol definitions, and individual trial reps. Three of the four objects are defined in this module:
`SessionInfo`, `NeuronInfo`, and `TrialRep`. The `Protocol` class in `sglportalapi.maestro`encapsulates a trial
protocol definition.

Author: saruffner
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import base64
import json
import struct
import sys
from datetime import date
from typing import Dict, Any, Optional, Tuple, List, Union

import numpy as np

from sglportalapi.maestro import Protocol

API_VERSION: int = 1
""" The current version number for the portal database access API. """
ROUTE_AUTHENTICATE: str = "/api"
""" API path to authenticate user and receive access token required for all other API endpoints. """
ROUTE_SESSIONINFO: str = "/api/sessions"
""" API path to retrieve summary information on selected experiment sessions in portal database. """
ROUTE_SESSION_NEURONS: str = "/api/session/neurons"
""" API path to retrieve summary information about selected neural unit recorded during a specified experiment. """
ROUTE_SESSION_PROTOCOLS: str = "/api/session/protocols"
""" API path to retrieve the defintions of all distinct Maestro trial protocols presented during an experiment. """
ROUTE_SESSION_TRIAL: str = "/api/session/trial"
""" API path to retrieve behavioral and neuronal responses for a Maestro trial presented during an experiment. """
ROUTE_SESSION_BLOCK: str = "/api/session/block"
""" API path to retrieve response data, etc for a sequential block of Maestro trials presented during an experiment. """
ROUTE_PROTOCOL_REPS: str = "/api/session/protocol/reps"
""" API path to retrieve response data for all reps of a specified trial protocol during an experiment session. """

_KNOWN_ROUTES: List[str] = [ROUTE_AUTHENTICATE, ROUTE_SESSIONINFO, ROUTE_SESSION_NEURONS, ROUTE_SESSION_PROTOCOLS,
                            ROUTE_SESSION_TRIAL, ROUTE_SESSION_BLOCK, ROUTE_PROTOCOL_REPS]
""" List of all supported API routes. """


class SessionInfo:
    """
    Information about an experiment session stored in the Lisberger lab portal database.
    """
    __REQUIRED_TYPES: Dict[str, type] = dict(
        experimenter=str, subj_id=str, session_date=str, session_sfx=int, num_trials=int, num_units=int,
        study_title=str,
    )
    __EPHYS_TYPES: Dict[str, type] = dict(
        brain_area=str, ephys_src=str, probe_type=str, sampling_rate=float, probe_x=float, probe_y=float,
        probe_depth=float
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
    def study(self) -> str:
        """ Title of the larger research study to which this experiment session belongs. """
        return self._info['study_title']

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


class TrialRep:
    """
    Response data recorded during a single presentation of a Maestro trial during an exeriment session committed to the
    Lisberger lab portal database, along with various metadata about the trial presented.
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
        session that is stored in the Lisberger lab portal database. Use read-only properties to access the information.

        Args:
            info: Dictionary containing the trial data as gleaned from portal database.
        Raises:
            ValueError: If dictionary argument is missing any required keys, or any key value is deemed invalid.
        """
        TrialRep._validate_init_arg(info)
        self._info = info

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
        was not recorded (rare).
        """
        return self._info['hgpos']

    @property
    def vepos(self) -> Optional[np.ndarray]:
        """
        Subject's vertical eye position trajectory during trial, in degrees (1-ms sampling period). None if vertical
        eye position was not recorded (rare).
        """
        return self._info['vepos']

    @property
    def hevel(self) -> Optional[np.ndarray]:
        """
        Subject's horizontal eye velocity trajectory during trial, in deg/sec (1-ms sampling period). None if
        horizontal eye velocity was not recorded (rare). """
        return self._info['hevel']

    @property
    def vevel(self) -> Optional[np.ndarray]:
        """
        Subject's vertical eye velocity trajectory during trial, in deg/sec (1-ms sampling period). None if vertical
        eye velocity was not recorded (rare).
        """
        return self._info['vevel']

    @property
    def events(self) -> Dict[int, np.ndarray]:
        """
        Timestamps (in seconds relative to trial start) of any marker pulses detected on the digital inputs in the
        recording rig.

        Returns:
            A dictionary mapping the input channel # (in 0..15) to a Numpy array of the timestamps of marker pulse
                events detected on that channel. Only channels on which at least one pulse occurred are included; the
                dictionary could be empty if no pulses were detected on any of the available digital inputs over the
                course of the trial.
        """
        return self._info['events']

    @property
    def spike_trains(self) -> Dict[int, Optional[np.ndarray]]:
        """
        Neural unit spike trains recorded during trial, with spike times in seconds since trial start.

        Returns:
            A dictionary mapping the unit ID to a Numpy array holding the timestamps of any spikes detected from that
                neural unit during the trial. If the array is empty, then the corresponding unit was recorded during the
                trial but no spikes occurred. However, if the array is None, then that unit was not recorded during
                the trial (units may be acquired and lost any time during an experiment session). The dictionary will
                be empty if no neuronal response data was requested for this trial rep.
        """
        return self._info['spike_trains']

    def to_bytes(self) -> bytes:
        """ Serialize this object to a byte sequence. """
        return json.dumps(self._info, cls=_CustomJSONEncoder).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> TrialRep:
        """ Reconstruct TrialRep from a byte sequence previously generated by `to_bytes()`. """
        return TrialRep(json.loads(raw.decode(), object_hook=_CustomJSONEncoder.decoder_hook))


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


def serialize_api_response(route: str, **kwargs) -> bytes:
    """
    Serialize a response from one of the Lisberger lab portal API endpoints.

    Args:
        route: The endpoint route name.
        kwargs: The response dictionary.
    Returns:
        A byte sequence encoding the response.
    Raises:
        APISerializeError: If endpoint route is invalid, if response dictionary is missing any required keyword, or if
            any error occurs while serializing the response.
    """
    try:
        if not (route in _KNOWN_ROUTES):
            raise ValueError(f"Unsupported API endpoint: {route}")
        out = bytearray()
        hdr: Dict[str, Any] = dict(route=route, version=API_VERSION)
        hdr_only = (route == ROUTE_AUTHENTICATE) or ('error' in kwargs)
        if hdr_only:
            if 'error' in kwargs:
                hdr['error'] = kwargs['error']
            else:
                hdr['token'], hdr['expires_in'] = kwargs['token'], kwargs['expires_in']
        elif route == ROUTE_SESSIONINFO:
            hdr['num_objects'] = len(kwargs['sessions'])
        elif route == ROUTE_SESSION_NEURONS:
            hdr['num_objects'] = len(kwargs['neurons'])
        elif route == ROUTE_SESSION_PROTOCOLS:
            hdr['num_objects'] = len(kwargs['protocols'])
        elif route == ROUTE_SESSION_TRIAL:
            hdr['num_objects'] = 1
        else:  # ROUTE_SESSION_BLOCK, ROUTE_PROTOCOL_REPS
            hdr['num_objects'] = len(kwargs['trials'])
        raw_hdr = json.dumps(hdr).encode()
        out.extend(struct.pack("<i", len(raw_hdr)))
        out.extend(raw_hdr)
        if hdr_only:
            return bytes(out)

        if route == ROUTE_SESSIONINFO:
            session: SessionInfo
            for session in kwargs['sessions']:
                raw_session = session.to_bytes()
                out.extend(struct.pack("<i", len(raw_session)))
                out.extend(raw_session)
        elif route == ROUTE_SESSION_NEURONS:
            neuron: NeuronInfo
            for neuron in kwargs['neurons']:
                raw_neuron = neuron.to_bytes()
                out.extend(struct.pack("<i", len(raw_neuron)))
                out.extend(raw_neuron)
        elif route == ROUTE_SESSION_PROTOCOLS:
            proto: Protocol
            for proto in kwargs['protocols']:
                raw_proto = proto.to_bytes()
                out.extend(struct.pack("<i", len(raw_proto)))
                out.extend(raw_proto)
        elif route == ROUTE_SESSION_TRIAL:
            trial: TrialRep = kwargs['trial']
            raw_trial = trial.to_bytes()
            out.extend(struct.pack("<i", len(raw_trial)))
            out.extend(raw_trial)
        else:  # ROUTE_SESSION_BLOCK, ROUTE_PROTOCOL_REPS
            trial: TrialRep
            for trial in kwargs['trials']:
                raw_trial = trial.to_bytes()
                out.extend(struct.pack("<i", len(raw_trial)))
                out.extend(raw_trial)

        return bytes(out)
    except Exception as e:
        raise APISerializeError(cause=e)


def deserialize_api_response(route: str, raw: bytes) -> Dict[str, Any]:
    """
    Deserialize the response object received from a Lisberger lab portal API endpoint.

    Args:
        route: The endpoint route name.
        raw: The byte sequence encoding the endpoint's response
    Returns:
        The deserialized response dictionary.
    Raises:
        APISerializeError: If the endpoint route name is invalid, if a required keyword is missing in the deserialized
            response dictionary, or if any error occurs during deserialization.
    """
    try:
        if not (route in _KNOWN_ROUTES):
            raise ValueError(f"Unsupported API endpoint: {route}")
        int_sz = struct.calcsize('<i')
        offset = 0
        hdr_sz, = struct.unpack_from('<i', raw, offset)
        offset += int_sz
        resp: Dict[str, Any] = json.loads(raw[offset:offset+hdr_sz].decode())
        offset += hdr_sz
        num_objects: int
        if resp['route'] != route:
            raise ValueError('Route mismatch in response!')
        elif resp['version'] != API_VERSION:
            raise ValueError(f"Invalid API version in response: {resp['version']}")
        if route == ROUTE_AUTHENTICATE:
            if not all([(k in resp) for k in ['token', 'expires_in']]):
                raise KeyError(f"Missing one or more keys in response")
            return resp
        elif 'error' in resp:
            return resp
        else:
            num_objects = resp['num_objects']
            if num_objects < 0 or (route == ROUTE_SESSION_TRIAL and num_objects != 1):
                raise ValueError(f"Invalid number of objects returned in response")

        if route == ROUTE_SESSIONINFO:
            session_list: List[SessionInfo] = list()
            for i in range(num_objects):
                info_sz, = struct.unpack_from('<i', raw, offset)
                offset += int_sz
                session_list.append(SessionInfo.from_bytes(raw[offset:offset+info_sz]))
                offset += info_sz
            resp['sessions'] = session_list
        elif route == ROUTE_SESSION_NEURONS:
            neuron_list: List[NeuronInfo] = list()
            for i in range(num_objects):
                info_sz, = struct.unpack_from('<i', raw, offset)
                offset += int_sz
                neuron_list.append(NeuronInfo.from_bytes(raw[offset:offset+info_sz]))
                offset += info_sz
            resp['neurons'] = neuron_list
        elif route == ROUTE_SESSION_PROTOCOLS:
            protocol_list: List[Protocol] = list()
            for i in range(num_objects):
                info_sz, = struct.unpack_from('<i', raw, offset)
                offset += int_sz
                protocol_list.append(Protocol.from_bytes(raw[offset:offset+info_sz]))
                offset += info_sz
            resp['protocols'] = protocol_list
        elif route == ROUTE_SESSION_TRIAL:
            info_sz, = struct.unpack_from('<i', raw, offset)
            offset += int_sz
            resp['trial'] = TrialRep.from_bytes(raw[offset:offset+info_sz])
            offset += info_sz
        else:   # ROUTE_SESSION_BLOCK, ROUTE_PROTOCOL_REPS
            trial_list: List[TrialRep] = list()
            for i in range(num_objects):
                info_sz, = struct.unpack_from('<i', raw, offset)
                offset += int_sz
                trial_list.append(TrialRep.from_bytes(raw[offset:offset + info_sz]))
                offset += info_sz
            resp['trials'] = trial_list
        return resp
    except Exception as e:
        raise APISerializeError(cause=e)
