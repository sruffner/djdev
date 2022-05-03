"""
data_containers.py: Definition of the data objects that can be retrieved from the Lisberger lab portal API.

TODO: Description.

Author: saruffner
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

from datetime import date
from typing import Dict, Any, Optional, Tuple, Set

import numpy as np

API_VERSION: int = 1
""" The current version number for the portal database access API. """


class SessionInfo:
    """
    Information about an experiment session stored in the Lisberger lab portal database.
    """
    _REQUIRED_KEY_SET: Set[str] = {
        'experimenter', 'subj_id', 'session_date', 'session_sfx', 'num_trials', 'num_units', 'study_title'
    }
    """ The session information dictionary must always have these keys. """
    _EPHYS_KEY_SET: Set[str] = {
        'brain_area', 'ephys_src', 'probe_type', 'sampling_rate', 'probe_x', 'probe_y', 'probe_depth'
    }
    """ These keys are required in the session information dictionary if neural units were recorded during session. """

    @staticmethod
    def _validate_init_arg(info: Dict[str, Any]) -> None:
        """
        Validates the dictionary defining session information. For a behavior-only session, all keys related to the
        electrophysiological recording are set to None.

        Args:
            info: A dictionary containing session information.
        Raises:
            ValueError - If any key is missing or any value is invalid.
        """
        all_keys = info.keys()
        if not SessionInfo._REQUIRED_KEY_SET.issubset(all_keys):
            raise ValueError('Missing one or more required session information keys')
        try:
            date.fromisoformat(info['session_date'])
        except Exception:
            raise ValueError("Invalid date string for key 'session_date'")
        if info['num_units'] > 0:
            if not SessionInfo._EPHYS_KEY_SET.issubset(all_keys):
                raise ValueError('Missing one or more keys about session electrophysiology')
        else:
            for k in SessionInfo._EPHYS_KEY_SET:
                info[k] = None

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

    @property
    def primary_key(self) -> Dict[str, Any]:
        """ The experiment session's primary key within the releveant table in the Lisberger lab portal database. """
        return dict(experimenter=self.experimenter, subj_id=self.subject, session_date=self.recording_date,
                    session_suffix=self.suffix)

    @property
    def experimenter(self) -> str:
        """ The registered username of the research that performed the experiment. """
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


class NeuronInfo:
    """
    Information about a recorded neural unit with response data stored in the Lisberger lab portal database.
    """
    _REQUIRED_KEY_SET: Set[str] = {
        'experimenter', 'subj_id', 'session_date', 'session_sfx', 'unit_id', 'unit_channel', 'unit_rate', 'unit_spikes',
        'unit_snr', 'unit_template', 'neuron_type'
    }

    @staticmethod
    def _validate_init_arg(info: Dict[str, Any]) -> None:
        """
        Validates the dictionary defining information about a neural unit.
        Args:
            info: A dictionary containing session information.
        Raises:
            ValueError - If any key is missing or any value is invalid.
        """
        all_keys = info.keys()
        if not NeuronInfo._REQUIRED_KEY_SET.issubset(all_keys):
            raise ValueError('Missing one or more required neuron information keys')
        try:
            date.fromisoformat(info['session_date'])
        except Exception:
            raise ValueError("Invalid date string for key 'session_date'")
        if (not isinstance(info['unit_template'], list)) or (len(info['unit_template']) == 0) or \
           (not isinstance(info['unit_template'][0], (float, int))):
            raise ValueError("Value for 'unit_template' must be a non-empty list of numerical values")

    def __init(self, info: Dict[str, Any]):
        """
        Summary information for a recorded neural unit stored in the Lisberger lab portal database. Use read-only
        properties to access the information.

        Args:
            info: Dictionary containing neural unit information as gleaned from portal database.
        Raises:
            ValueError: If dictionary argument is missing any required keys, or any key value is deemed invalid.
        """
        self._info = info

    @property
    def primary_key(self) -> Dict[str, Any]:
        """ The neural unit's unique primary key within the relevant table in the Lisberger lab portal database. """
        return dict(experimenter=self.experimenter, subj_id=self.subject, session_date=self.recording_date,
                    session_suffix=self.suffix, unit_id=self.id)

    @property
    def experimenter(self) -> str:
        """ The registered username of the research that performed the experiment during which unit was recorded. """
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
