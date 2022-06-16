"""
maestro.py: Structures and functions for digesting Maestro trial data files.

This module contains Maestro-specific constructs for reading and parsing Maestro trial data files. In particular, it
defines Python classes encapsulating Maestro trial and target definitions, as well as trial segments, tagged sections,
and perturbations.

It also implements the important concept of a "trial protocol". The trial culled from a Maestro data file is really one
particular presentation of a Maestro trial protocol, represented by the Protocol object. In general, a Maestro trial is
presented many times over the course of many experiment sessions. Often, the trial includes at least one parameter --
most typically, the duration of a so-called "fixation segment" -- that varies randomly from one trial presentation to
the next. But there can be others. A Protocol consists of a nominal Trial definition and a list of segment table
parameters that vary randomly across repeated presentations of that protocol. When processing Maestro data files from a
given experiment session, it is imperative to identify "similar" trials that are repeated presentations of the same
trial protocol, so that we can "average" behavioral and neuronal responses across those repetitions.

Limitations:
    * Only supports Trial-mode data files with file version >= 21. Cannot process Continuous-mode data files!
    * Does not process JMWork/XWork action edit codes, but does parse out sorted spike train channel data.
    * The Trial class does not extract all available information in the trial codes. Notable omissions include info on
      special operations, a failsafe segment, staircase sequencer-related parameters (rarely if ever used), and any
      mid-trial rewards.

SAMPLE USAGE::

    import maestro
    from pathlib import Path

    file_path = Path('/maestrodata/session/myfile.0001')
    with open(file_path, 'rb') as f:
        data_file = maestro_file.DataFile.load(f.read(), file_path.name)

Author: sruffner
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

from typing import NamedTuple, List, Optional, Dict, Any, Tuple, Union, Set
from datetime import date
import struct
import re
import math
import zipfile
import hashlib
import pickle
import numpy as np

from sglportalapi.util import DocEnum


class DataFileError(Exception):
    """ An error that occurred while reading and parsing a Maestro data file. """
    def __init__(self, reason: Optional[str] = None):
        self.message = reason if reason else "Undefined error"

    def __str__(self):
        return self.message


# CONSTANTS
MIN_SUPPORTED_VERSION = 21
CURRENT_VERSION = 23  # data file version number as of Maestro 4.1.0
MAX_NAME_SIZE = 40  # fixed size of ASCII character fields in data file header (num bytes)
MAX_AI_CHANNELS = 16   # size of analog input channel list in data file header
NUM_DI_CHANNELS = 16  # number of digital input channels on which events may be time-stamped
RMVIDEO_DUPE_SZ = 6  # size of RMVideo duplicate events array in data file header

# Flag bits defined in the 'flags' field within the data file header
FLAG_IS_CONTINUOUS = (1 << 0)
FLAG_SAVED_SPIKES = (1 << 1)
FLAG_REWARD_EARNED = (1 << 2)
FLAG_REWARD_GIVEN = (1 << 3)
FLAG_FIX1_SELECTED = (1 << 4)
FLAG_FIX2_SELECTED = (1 << 5)
FLAG_END_SELECT = (1 << 6)
FLAG_HAS_TAGGED_SECTIONS = (1 << 7)
FLAG_IS_RP_DISTRO = (1 << 8)
FLAG_GOT_RP_RESPONSE = (1 << 9)
FLAG_IS_SEARCH_TASK = (1 << 10)
FLAG_IS_ST_OK = (1 << 11)
FLAG_IS_DISTRACTED = (1 << 12)
FLAG_EYELINK_USED = (1 << 13)
FLAG_DUPE_FRAME = (1 << 14)

RECORD_SIZE = 1024  # size of each record (including header) in data file
RECORD_TAG_SIZE = 8  # size of record tag
RECORD_BYTES = RECORD_SIZE - RECORD_TAG_SIZE  # size of record "body" (minus tag)
RECORD_SHORTS = int(RECORD_BYTES / 2)
RECORD_INTS = int(RECORD_BYTES / 4)

INVALID_RECORD = -1
AI_RECORD = 0
EVENT0_RECORD = 1
EVENT1_RECORD = 2
OTHER_EVENT_RECORD = 3
TRIAL_CODE_RECORD = 4
ACTION_RECORD = 5
SPIKE_SORT_RECORD_FIRST = 8
SPIKE_SORT_RECORD_LAST = 57
MAX_SPIKE_SORT_TRAINS = SPIKE_SORT_RECORD_LAST - SPIKE_SORT_RECORD_FIRST + 1
# V1_TGT_RECORD = 64  --> No support for V<2
TGT_RECORD = 65
STIM_RUN_RECORD = 66
SPIKEWAVE_RECORD = 67
TAG_SECT_RECORD = 68
END_OF_TRIAL_CODES = 99  # Trial code marking end of trial code sequence in data file
END_OF_EVENTS = 0x7fffffff  # "end-of-data" marker for digital pulse event and spike-sorting records
EYELINK_BLINK_START_MASK = 0x00010000  # mask for other event flag bit indicating "blink start" on EyeLink tracker
EYELINK_BLINK_END_MASK = 0x00020000   # mask for other event flag bit indicating "blink end" on EyeLink tracker
ACTION_ID_FIRST = 100  # action ID code for first recognized X/JMWork action
ACTION_CUT = 101
ACTION_MARK = 106
ACTION_SET_MARK1 = 107
ACTION_SET_MARK2 = 108
ACTION_REMOVE_SPIKE = 113
ACTION_ADD_SPIKE = 114
ACTION_DEFINE_TAG = 115
ACTION_TAG_MAX_LEN = 16  # max number of visible ASCII characters in the label for a general purpose tag
ACTION_DISCARD = 116

ADC_TO_DEG = 0.025
""" Multiplicative scale factor converts Maestro's 12-bit raw ADC sample to degrees (for position signals) """
ADC_TO_DPS = 0.09189
""" Multiplicative scale factor converts Maestro's 12-bit raw ADC sample to degrees per sec (for velocity signals) """

BEHAVIOR_TO_CHANNEL = {'HEPOS': 0, 'VEPOS': 1, 'HEVEL': 2, 'VEVEL': 3, 'HDVEL': 4}
""" Dictionary mapping selected Maestro behavioral responses to the ADC channel number on which they are recorded. """


class DataFile(NamedTuple):
    """
    Parsed content of a single Maestro data file.
    """
    file_name: str
    """ The Maestro data file name, eg 'basename.0001'. """
    header: DataFileHeader
    """ Contents of the data file's header record. """
    ai_data: Dict[int, List[int]]
    """ The recorded 1KHz analog data traces, keyed by AI channel index, decompressed in raw ADC units. """
    spike_wave: Optional[List[int]]
    """ The decompressed high-resolution spike waveform. None if not saved in file. """
    trial: Trial
    """ Definition of the Maestro trial presented. """
    events: Optional[Dict[int, List[float]]]
    """ Marker pulse event times in ms since trial start, keyed by DI channel index. """
    blinks: Optional[List[int]]
    """ EyeLink-recorded blink epochs in ms since trial start: [start1, end1, start2, end2, ....]. """
    sorted_spikes: Optional[Dict[int, List[float]]]
    """ Spike occurrence times in ms since trial start, keyed by sorted spike train channel index. """

    @staticmethod
    def load(content: bytes, file_name: str) -> DataFile:
        """
        Read and parse the contents of a Maestro data file.

        Args:
            content: The file's binary contents as a sequence of bytes.
            file_name: The data file name -- should be in the format 'basename.0001'.
        Returns:
            A DataFile encapsulating the content.
        """
        data: Dict[str, Any] = dict()
        num_total_bytes = len(content)
        if (num_total_bytes % RECORD_SIZE) != 0:
            raise DataFileError(f"Data file size in bytes ({num_total_bytes}) is not a multiple of {RECORD_SIZE}")
        header = DataFileHeader(content)
        if (header.version < MIN_SUPPORTED_VERSION) or header.is_continuous_mode():
            raise DataFileError(f"No support for Continuous mode or for data file version<{MIN_SUPPORTED_VERSION}")
        try:
            offset = RECORD_SIZE
            while offset < num_total_bytes:
                DataFile._parse_record(content[offset:offset+RECORD_SIZE], data, header)
                offset += RECORD_SIZE

            # Decompress analog data recorded. All channels must have same number of samples, matching the number of
            # scans saved as reported in header.
            if 'ai_compressed' in data:
                ai_data_per_channel = DataFile._decompress_ai(data['ai_compressed'], header.num_ai_channels)
                ai_dict = dict()
                for i, ai_trace in enumerate(ai_data_per_channel):
                    if len(ai_trace) != header.num_scans_saved:
                        msg = f"Channel={header.channel_list[i]}, N={len(ai_trace)}, expected {header.num_scans_saved}"
                        raise DataFileError(f"Incorrect number of samples found: {msg}")
                    ai_dict[header.channel_list[i]] = ai_trace
                data['ai_data'] = ai_dict
                data.pop('ai_compressed', None)

            if 'spike_wave_compressed' in data:
                data['spike_wave'] = DataFile._decompress_ai(data['spike_wave_compressed'], 1)[0]
                data.pop('spike_wave_compressed', None)

            # Process trial codes to generate trial definition
            if not (('trial_codes' in data) and ('targets' in data)):
                raise DataFileError("Trial codes and/or target definitions missing from Maestro Trial-mode data file!")
            data['trial'] = Trial.prepare_trial(data['trial_codes'], header, data['targets'],
                                                data['tagged_sections'] if ('tagged_sections' in data) else None)

            # Process JMW/XWork actions, if any -- currently not supported.

            # prepare and return the DataFile object -- but there must be some recorded analog data
            if 'ai_data' not in data:
                raise DataFileError("Found no recorded analog data in file!")
            return DataFile._make([
                file_name,
                header,
                data['ai_data'],
                data['spike_wave'] if ('spike_wave' in data) else None,
                data['trial'],
                data['events'] if ('events' in data) else None,
                data['blinks'] if ('blinks' in data) else None,
                data['sorted_spikes'] if ('sorted_spikes' in data) else None,
            ])
        except DataFileError as err:
            raise DataFileError(f"({file_name}) {str(err)}")
        except Exception as err:
            raise DataFileError(f"({file_name} Unexpected failure while loading data file: str{err}")

    @staticmethod
    def load_trial(content: bytes, file_name: str) -> Trial:
        """
        Load the definition of the Maestro trial found in a Maestro data file.

        Args:
            content: The file's binary contents as a sequence of bytes.
            file_name: The data file name -- should be in the format 'basename.0001'.
        Returns:
            A Trial object encapsulating the definition of the Maestro trial presented (including participating trial
                targets) when the data file was recorded.
        """
        data: Dict[str, Any] = dict()
        num_total_bytes = len(content)
        if (num_total_bytes % RECORD_SIZE) != 0:
            raise DataFileError(f"Maestro data file size in bytes ({num_total_bytes}) is not a multiple of 1024!")
        header = DataFileHeader(content)
        if (header.version < 21) or header.is_continuous_mode():
            raise DataFileError("No support for version<21 Maestro data files or files recorded in Continuous mode!")
        try:
            offset = RECORD_SIZE
            while offset < num_total_bytes:
                # we only consume records that we need to reconstruct the trial
                if content[offset] in [TRIAL_CODE_RECORD, TGT_RECORD, TAG_SECT_RECORD]:
                    DataFile._parse_record(content[offset:offset+RECORD_SIZE], data, header)
                offset += RECORD_SIZE

            # Process trial codes to generate trial definition
            if not (('trial_codes' in data) and ('targets' in data)):
                raise DataFileError("Trial codes and/or target definitions missing from Maestro Trial-mode data file!")
            return Trial.prepare_trial(data['trial_codes'], header, data['targets'],
                                       data['tagged_sections'] if ('tagged_sections' in data) else None)
        except DataFileError as err:
            raise DataFileError(f"({file_name}) {str(err)}")
        except Exception as err:
            raise DataFileError(f"({file_name} Unexpected failure while loading trial from data file: str{err}")

    @staticmethod
    def _parse_record(record: bytes, data: dict, header: DataFileHeader) -> None:
        record_id = record[0]
        if record_id == AI_RECORD:
            if not ('ai_compressed' in data):
                data['ai_compressed'] = []
            data['ai_compressed'].extend(record[RECORD_TAG_SIZE:RECORD_SIZE])
        elif record_id == SPIKEWAVE_RECORD:
            if not ('spike_wave_compressed' in data):
                data['spike_wave_compressed'] = []
            data['spike_wave_compressed'].extend(record[RECORD_TAG_SIZE:RECORD_SIZE])
        elif record_id == TRIAL_CODE_RECORD:
            if not ('trial_codes' in data):
                data['trial_codes'] = []
            elif data['trial_codes'][-1].code == TC_END_TRIAL:
                raise DataFileError("Encountered another trial code record after getting end-of-trial code!")
            data['trial_codes'].extend(TrialCode.parse_codes(record))
        elif record_id == ACTION_RECORD:
            raw_actions = struct.unpack_from(f"{RECORD_INTS}i", record, RECORD_TAG_SIZE)
            if not ('action_codes' in data):
                data['action_codes'] = [code for code in raw_actions]
            else:
                data['action_codes'].extend(raw_actions)
        elif record_id == TGT_RECORD:
            if not ('targets' in data):
                data['targets'] = []
            data['targets'].extend(Target.parse_targets(record, header.version))
        elif record_id == TAG_SECT_RECORD:
            if 'tagged_sections' in data:
                raise DataFileError('A Maestro data file cannot contain more than one tagged section record!')
            data['tagged_sections'] = TaggedSection.parse_tagged_sections(record)
        elif (record_id == EVENT0_RECORD) or (record_id == EVENT1_RECORD) or (record_id == OTHER_EVENT_RECORD) or\
                (SPIKE_SORT_RECORD_FIRST <= record_id <= SPIKE_SORT_RECORD_LAST):
            DataFile._parse_events(record, data, header.is_eyelink_used())
        else:
            raise DataFileError(f"Record tag={record_id} is invalid for a Maestro version 2+ trial data file")

    @staticmethod
    def _parse_events(record: bytes, data: dict, eyelink_used: bool) -> None:
        record_id = record[0]
        events = struct.unpack_from(f"{RECORD_INTS}i", record, RECORD_TAG_SIZE)
        if (record_id == EVENT0_RECORD) or (record_id == EVENT1_RECORD):
            if not ('events' in data):
                data['events'] = dict()
            channel = 0 if record_id == EVENT0_RECORD else 1
            last_event_time_ms = 0
            if channel in data['events']:
                last_event_time_ms = data['events'][channel][-1]
            else:
                data['events'][channel] = []
            for event_time in events:
                if event_time == END_OF_EVENTS:
                    break
                else:
                    last_event_time_ms += event_time / 100.0   # convert event time from 10-us ticks to milliseconds
                    data['events'][channel].append(last_event_time_ms)
        elif record_id == OTHER_EVENT_RECORD:  # Events on DI<2..15>, or Eyelink blink epochs
            blink_mask = (EYELINK_BLINK_START_MASK | EYELINK_BLINK_END_MASK) if eyelink_used else 0
            for i in range(0, len(events), 2):
                event_mask = events[i]
                event_time = events[i+1]
                if event_time == END_OF_EVENTS:
                    break
                if (event_mask & blink_mask) != 0:
                    # Eyelink blink epochs are defined in start/end pairs, in chronological order in ms. Exception: If
                    # the first blink event is "blink end", assume it's accompanied by a "blink start" at t=0
                    if not ('blinks' in data):
                        data['blinks'] = []
                    if (len(data['blinks']) == 0) and ((event_mask & blink_mask) == EYELINK_BLINK_END_MASK):
                        data['blinks'].append(0)
                    data['blinks'].append(event_time)
                else:
                    # Events on DI<2..15>. There could be events on multiple channels at the same time! Event times are
                    # converted from 10-us ticks to milliseconds
                    if not ('events' in data):
                        data['events'] = dict()
                    for j in range(2, NUM_DI_CHANNELS):
                        if (event_mask & (1 << j)) != 0:
                            if not (j in data['events']):
                                data['events'][j] = [event_time/100.0]
                            else:
                                data['events'][j].append(event_time/100.0)
        else:  # sorted spikes
            if not ('sorted_spikes' in data):
                data['sorted_spikes'] = dict()
            channel = record_id - SPIKE_SORT_RECORD_FIRST
            last_event_time_ms = 0
            if channel in data['sorted_spikes']:
                last_event_time_ms = data['sorted_spikes'][channel][-1]
            else:
                data['sorted_spikes'][channel] = []
            for event_time in events:
                if event_time == END_OF_EVENTS:
                    break
                else:
                    last_event_time_ms += event_time / 100.0   # convert event time from 10-us ticks to milliseconds
                    data['sorted_spikes'][channel].append(last_event_time_ms)

    @staticmethod
    def _decompress_ai(compressed_data: List[int], n_channels: int) -> List[List[int]]:
        out = [[] for _ in range(n_channels)]
        sample_idx = 0
        last_sample = [0] * n_channels
        while sample_idx < len(compressed_data):
            for channel in range(n_channels):
                value = compressed_data[sample_idx] if sample_idx < len(compressed_data) else 0
                if value == 0 or value == -1:
                    # Reached end of byte array or detected end-of-data marker
                    return out
                if value & 0x080:
                    # Bit 7 is set - next dataum is 2 bytes. NOTE - We assume we're not at end of compressed bytes!
                    temp = (((value & 0x7F) << 8) | (0x00FF & (compressed_data[sample_idx + 1]))) - 4096
                    sample_idx += 1  # Used next byte
                    last_sample[channel] += temp  # Datum is difference from last sample
                else:
                    # Bit 7 is clear - next data is 1 byte
                    last_sample[channel] += (value - 64)  # Datum is difference from last sample
                out[channel].append(last_sample[channel])
                sample_idx += 1
        return out


class DataFileHeader:
    """
    The contents of the header record of a Maestro data file. Some obsolete fields are omitted.
    """
    _header_format = f"<{MAX_NAME_SIZE}s5h{MAX_AI_CHANNELS}h7h11iI3i{MAX_NAME_SIZE}s2iI10iI11i" \
                     f"{MAX_NAME_SIZE}s{MAX_NAME_SIZE}s2hi{RMVIDEO_DUPE_SZ}i"
    """ Format string defining byte packing of the header record. """

    def __init__(self, record: bytes):
        """
        Construct a Maestro data file header from the original raw 1KB header record.

        Args:
            record: The raw header record (first 1KB of the Maestro data file). Technically, only the first 408 bytes
                are needed, since the rest of the 1KB record is unused.
        Raises:
            DataFileError: If a parsing error occurs or the file version is not supported.
        """
        self._header = dict()
        """ Dictionary containing all header fields that are not obsolete. """
        self._raw_bytes = record[0:struct.calcsize(DataFileHeader._header_format)]
        """ Copy of the portion of the data file header record containing meaningful information. """

        try:
            raw_fields = struct.unpack_from(DataFileHeader._header_format, self._raw_bytes, 0)
            version = raw_fields[39]
            if version < 2:
                raise DataFileError("Data file version 1 or earlier is not supported")
            idx = 0
            self._header['trial_name'] = raw_fields[idx].decode('ascii').split('\0', 1)[0]  # name
            idx += 5   # skip obsolete fields trhdir, trvdir, nchar, npdig
            self._header['num_ai_channels'] = raw_fields[idx]  # nchans
            idx += 1
            self._header['channel_list'] = raw_fields[idx:idx+MAX_AI_CHANNELS]  # chlist (array)
            idx += MAX_AI_CHANNELS
            self._header['display_height_pix'] = raw_fields[idx]  # d_rows
            self._header['display_width_pix'] = raw_fields[idx+1]  # d_cols
            idx += 4   # skips ignored fields d_crow, d_ccol
            self._header['display_distance_mm'] = raw_fields[idx]  # d_dist
            self._header['display_width_mm'] = raw_fields[idx+1]  # d_dwidth
            self._header['display_height_mm'] = raw_fields[idx+2]  # d_dheight
            idx += 3
            # d_framerate - convert to Hz, preserving precision, which changes from milli- to micro-Hz in V=22
            self._header['display_framerate_hz'] = float(raw_fields[idx]) / (1.0e6 if version >= 22 else 1.0e3)
            idx += 1
            # iPosScale .. iVelTheta: The raw values are scaled by 1000
            self._header['pos_scale'] = float(raw_fields[idx]) / 1000.0
            self._header['pos_theta'] = float(raw_fields[idx+1]) / 1000.0
            self._header['vel_scale'] = float(raw_fields[idx+2]) / 1000.0
            self._header['vel_theta'] = float(raw_fields[idx+3]) / 1000.0
            idx += 4
            self._header['reward_len1_ms'] = raw_fields[idx]  # iRewLen1
            self._header['reward_len2_ms'] = raw_fields[idx+1]  # iRewLen2
            idx += 2
            # year/month/dayRecorded
            self._header['date_recorded'] = date(raw_fields[idx+2], raw_fields[idx+1], raw_fields[idx])
            idx += 3
            self._header['version'] = raw_fields[idx]  # version
            self._header['flags'] = raw_fields[idx+1]  # flags
            idx += 3   # nScanIntvUS skipped b/c it is always 1000 (trials) or 2000 (continuous)
            self._header['num_bytes_compressed'] = raw_fields[idx]  # nBytesCompressed
            self._header['num_scans_saved'] = raw_fields[idx+1]  # nScansSaved
            idx += 3   # spikesFName skipped b/c we won't support old spikesPC file
            self._header['num_spike_bytes_compressed'] = raw_fields[idx]  # nSpikeBytesCompressed .. iRPDResponse
            self._header['spike_sample_intv_us'] = raw_fields[idx+1]
            self._header['xy_random_seed'] = raw_fields[idx+2]
            self._header['rp_distro_start'] = raw_fields[idx+3]
            self._header['rp_distro_dur'] = raw_fields[idx+4]
            self._header['rp_distro_response'] = float(raw_fields[idx+5]) / 1000.0
            idx += 6
            self._header['rp_distro_windows'] = raw_fields[idx:idx+4]  # iRPDWindows (int array of size 4)
            idx += 4
            self._header['rp_distro_response_type'] = raw_fields[idx]  # iRPDRespType
            idx += 1
            # iStartPosH, iStartPosV: The raw values are scaled by 1000
            self._header['horizontal_start_pos'] = float(raw_fields[idx]) / 1000.0
            self._header['vertical_start_pos'] = float(raw_fields[idx + 1]) / 1000.0
            idx += 2
            self._header['trial_flags'] = raw_fields[idx]  # dwTrialFlags
            self._header['search_target_selected'] = raw_fields[idx+1]  # iSTSelected
            self._header['velocity_stab_window_len_ms'] = raw_fields[idx+2]  # iVStabWinLen
            idx += 3
            self._header['eyelink_info'] = raw_fields[idx:idx+9]  # iELInfo (int array of size 9)
            idx += 9
            self._header['trial_set_name'] = raw_fields[idx].decode('ascii').split('\0', 1)[0]  # setName
            self._header['trial_subset_name'] = raw_fields[idx+1].decode('ascii').split('\0', 1)[0]  # subsetName
            idx += 2
            self._header['rmvideo_sync_size_mm'] = raw_fields[idx]  # rmvSyncSz
            self._header['rmvideo_sync_dur_frames'] = raw_fields[idx+1]  # rmvSyncDur
            self._header['timestamp_ms'] = raw_fields[idx+2]  # timestampMS
            idx += 3
            self._header['rmvideo_duplicate_events'] = raw_fields[idx:idx+RMVIDEO_DUPE_SZ]  # rmvDupEvents (int array)
            idx += RMVIDEO_DUPE_SZ
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing data file header: {str(err)}")

    def to_bytes(self) -> bytes:
        """
        The byte sequence defining the contents of this Maestro data file header. Reconstruct the header object by
        passing this byte sequence to the constructor.
        """
        return self._raw_bytes[:]

    @property
    def trial_name(self) -> str:
        """ The name of the Maestro trial presented. """
        return self._header['trial_name']

    @property
    def num_ai_channels(self) -> int:
        """ Number of analog input channels recorded and saved. """
        return self._header['num_ai_channels']

    @property
    def channel_list(self) -> List[int]:
        """ Analog input channel scan list (AI channel indices in scanning order per 'tick'). """
        return self._header['channel_list']

    @property
    def display_height_pix(self) -> int:
        """ Height of target display in pixels. """
        return self._header['display_height_pix']

    @property
    def display_width_pix(self) -> int:
        """ Width of target display in pixels. """
        return self._header['display_width_pix']

    @property
    def display_distance_mm(self) -> int:
        """ Distance from subject's eye to center of target display in millimeters. """
        return self._header['display_distance_mm']

    @property
    def display_width_mm(self) -> int:
        """ Width of target display in millimeters. """
        return self._header['display_width_mm']

    @property
    def display_height_mm(self) -> int:
        """ Heigt of target display in millimeters. """
        return self._header['display_height_mm']

    @property
    def display_framerate_hz(self) -> float:
        """ Target display vertical refresh rate in Hz. """
        return self._header['display_framerate_hz']

    @property
    def pos_scale(self) -> float:
        """ Target position scale factor. """
        return self._header['pos_scale']

    @property
    def pos_theta(self) -> float:
        """ Target position vector rotation angle in degrees CCW. """
        return self._header['pos_theta']

    @property
    def vel_scale(self) -> float:
        """ Target velocity scale factor. """
        return self._header['vel_scale']

    @property
    def vel_theta(self) -> float:
        """ Target velocity vector rotation angle in degrees CCW. """
        return self._header['vel_theta']

    @property
    def reward_len1_ms(self) -> int:
        """ Reward pulse length #1 in ms. """
        return self._header['reward_len1_ms']

    @property
    def reward_len2_ms(self) -> int:
        """ Reward pulse length #2 in ms. """
        return self._header['reward_len2_ms']

    @property
    def date_recorded(self) -> date:
        """ Recording date. """
        return self._header['date_recorded']

    @property
    def version(self) -> int:
        """ Data file version number. """
        return self._header['version']

    @property
    def flags(self) -> int:
        """ Header flags. """
        return self._header['flags']

    @property
    def num_bytes_compressed(self) -> int:
        """ Total number of bytes of compressed analog data collected. """
        return self._header['num_bytes_compressed']

    @property
    def num_scans_saved(self) -> int:
        """ Total number of channel scans saved (essentially the recorded duration in ms for a trial). """
        return self._header['num_scans_saved']

    @property
    def num_spike_bytes_compressed(self) -> int:
        """ Total number of bytes of compressed high-resolution spike waveform data. """
        return self._header['num_spike_bytes_compressed']

    @property
    def spike_sample_intv_us(self) -> int:
        """ Sample interval for the spike waveform trace, in microseconds. """
        return self._header['spike_sample_intv_us']

    @property
    def xy_random_seed(self) -> int:
        """ Number used to seed random number generation on the XY scope controller. """
        return self._header['xy_random_seed']

    @property
    def rp_distro_start(self) -> int:
        """ Start of R/P Distro designated trial segment, in milliseconds relative to start of trial. """
        return self._header['rp_distro_start']

    @property
    def rp_distro_dur(self) -> int:
        """ Duration of R/P Distro designated segment, in milliseconds. """
        return self._header['rp_distro_dur']

    @property
    def rp_distro_response(self) -> float:
        """ Average response during R/P Distro segment, in response sample units. """
        return self._header['rp_distro_response']

    @property
    def rp_distro_windows(self) -> List[int]:
        """
        Reward windows for the R/P Distro trial: [a b c d]. a<=b defines the first window; c<=d defines the second.
        If a==b, window is undefined. As of version 7, c=d==0 (only one reward window). Units are 0.001 deg/sec.
        """
        return self._header['rp_distro_windows']

    @property
    def rp_distro_response_type(self) -> int:
        """ R/P Distro behavioral response type. """
        return self._header['rp_distro_response_type']

    @property
    def horizontal_start_pos(self) -> float:
        """ Horizontal offset in starting target position, in degrees. """
        return self._header['horizontal_start_pos']

    @property
    def vertical_start_pos(self) -> float:
        """ Vertical offset in starting target position, in degrees. """
        return self._header['vertical_start_pos']

    @property
    def trial_flags(self) -> int:
        """ Maestro trial's flag bits. """
        return self._header['trial_flags']

    @property
    def search_target_selected(self) -> int:
        """ Selected target index for 'searchTask' trial; -1 = not selected, 0 if not a 'searchTask' trial. """
        return self._header['search_target_selected']

    @property
    def velocity_stab_window_len_ms(self) -> int:
        """ Sliding window length to average eye position noise for velocity stabilization, in milliseconds. """
        return self._header['velocity_stab_window_len_ms']

    @property
    def eyelink_info(self) -> List[int]:
        """ Eyelink parameters and information (very rarely used). """
        return self._header['eyelink_info']

    @property
    def trial_set_name(self) -> str:
        """ Trial set name (for data file version >= 21; for older versions, this is an empty string). """
        return self._header['trial_set_name']

    @property
    def trial_subset_name(self) -> str:
        """ Trial subset name (for version >= 21). An empty string if there is no subset or V < 21. """
        return self._header['trial_subset_name']

    @property
    def rmvideo_sync_size_mm(self) -> int:
        """ Spot size (mm) for RMVideo "vertical sync" flash; 0 = disabled. """
        return self._header['rmvideo_sync_size_mm']

    @property
    def rmvideo_sync_dur_frames(self) -> int:
        """ Duration (number of video frames) for RMVideo "vertical sync" flash. """
        return self._header['rmvideo_sync_dur_frames']

    @property
    def timestamp_ms(self) -> int:
        """ Time at which trial recording started, in milliseconds since Maestro started. """
        return self._header['timestamp_ms']

    @property
    def rmvideo_duplicate_events(self) -> List[int]:
        """
        Information on up to 3 duplicate frame events detected by RMVideo during trial (version >= 22).

        Each event is represented by a pair of integers [N,M]. N>0 is the frame index of the first repeat frame in the
        event, and M is the number of contiguous duplicate frames caused by a rendering delay on the RMVideo side.
        However, if M=0, then a single duplicate frame occurred at frame N because RMVideo did not receive a target
        update in time.
        """
        return self._header['rmvideo_duplicate_events']

    def is_continuous_mode(self):
        """ Was the Maestro data file recorded in Continuous mode rather than Trial mode? """
        return (self.flags & FLAG_IS_CONTINUOUS) != 0

    def is_eyelink_used(self):
        """ Was the EyeLink in use when this Maestro data file was recorded? """
        return (self.version >= 20) and ((self.flags & FLAG_EYELINK_USED) != 0)

    def global_transform(self) -> TargetTransform:
        """ Get the global target transform in effect when this Maestro data file was recorded. """
        return TargetTransform(self)


class TargetTransform:
    """
    A Maestro target vector transform, as culled from parameters in the header record of a Maestro data file: scale
    factor and rotation angle for both position and velocity, plus a starting target position offset (Ho, Vo) applied
    only at the start of a trial.
    """
    _struct_format: str = "<6f"
    """ Format string for converting a target transform object to/from a raw byte sequence. """

    def __init__(self, *args):
        """
        Construct the target vector global transform in effect when a Maestro data file was recorded, in accordance with
        (i) a byte sequence encoding the transform, as supplied by to_bytes(); or (ii) the the transform parameters as
        stored in the data file's header record.

        Args:
            *args: Anonymous arguments. The first argument must be a DataFileHeader or a byte sequence. In the latter
                case, the second argument must be an integer indicating an offset into the byte sequence supplied.
        Raises:
            DataFileError: If argument is neither a byte sequence nor a Maestro data file header, or if the supplied
                byte sequence cannot be parsed as a trial target transform object.
        """

        self._definition = dict()
        """ The target transform as a dictionary of parameter values keyed by parameter names. """

        if isinstance(args[0], DataFileHeader):
            hdr: DataFileHeader = args[0]
            self._definition['pos_offset_h'] = hdr.horizontal_start_pos
            self._definition['pos_offset_v'] = hdr.vertical_start_pos
            self._definition['pos_scale'] = hdr.pos_scale
            self._definition['pos_theta'] = hdr.pos_theta
            self._definition['vel_scale'] = hdr.vel_scale
            self._definition['vel_theta'] = hdr.vel_theta
        elif isinstance(args[0], bytes):
            try:
                raw_fields = struct.unpack_from(TargetTransform._struct_format, args[0], args[1])
                self._definition['pos_offset_h'] = raw_fields[0]
                self._definition['pos_offset_v'] = raw_fields[1]
                self._definition['pos_scale'] = raw_fields[2]
                self._definition['pos_theta'] = raw_fields[3]
                self._definition['vel_scale'] = raw_fields[4]
                self._definition['vel_theta'] = raw_fields[5]
            except Exception as e:
                raise DataFileError(f"Cannot parse trial target transform from byte sequence: {str(e)}")
        else:
            raise DataFileError("TargetTransform constructor requires DataFileHeader or byte sequence")

    def __eq__(self, other: TargetTransform) -> bool:
        """
        Two target transforms are equal if their corresponding parameters are "close enough" (using math.isclose()).
        """
        return (self.__class__ == other.__class__) and math.isclose(self.pos_offset_h, other.pos_offset_h) and \
            math.isclose(self.pos_offset_v, other.pos_offset_v) and \
            math.isclose(self.pos_scale, other.pos_scale) and \
            math.isclose(self.pos_theta, other.pos_theta) and \
            math.isclose(self.vel_scale, other.vel_scale) and \
            math.isclose(self.vel_theta, other.vel_theta)

    def __hash__(self) -> int:
        return(hash((self.pos_offset_h, self.pos_offset_v, self.pos_scale, self.pos_theta,
                     self.vel_scale, self.vel_theta)))

    def __str__(self) -> str:
        """
        Returns compact string representation of target transform: '[(A, B); pos=C, D deg; vel=E, F deg]', where (A, B)
        are the horizontal and vertical initial position offsets; C is the position scale factor, D is the position
        rotation angle, E is the velocity scale factor, and F is the velocity rotation angle.
        """
        ofs_x = f"{self.pos_offset_h:.2f}".rstrip('0').rstrip('.')
        ofs_y = f"{self.pos_offset_v:.2f}".rstrip('0').rstrip('.')
        pos_scale = f"{self.pos_scale:.2f}".rstrip('0').rstrip('.')
        pos_rotate = f"{self.pos_theta:.2f}".rstrip('0').rstrip('.')
        vel_scale = f"{self.vel_scale:.2f}".rstrip('0').rstrip('.')
        vel_rotate = f"{self.vel_theta:.2f}".rstrip('0').rstrip('.')
        return f"[({ofs_x},{ofs_y}); pos={pos_scale}, {pos_rotate} deg; vel={vel_scale}, {vel_rotate} deg]"

    @property
    def pos_offset_h(self) -> float:
        """ Initial horizontal position offset applied to all participating targets at trial start, in degrees. """
        return self._definition['pos_offset_h']

    @property
    def pos_offset_v(self) -> float:
        """ Initial vertical position offset applied to all participating targets at trial start, in degrees. """
        return self._definition['pos_offset_v']

    @property
    def pos_scale(self) -> float:
        """ Target position vector scale factor. """
        return self._definition['pos_scale']

    @property
    def pos_theta(self) -> float:
        """ Target position vector rotation angle, in degrees CCW. """
        return self._definition['pos_theta']

    @property
    def vel_scale(self) -> float:
        """ Target velocity vector scale factor. """
        return self._definition['vel_scale']

    @property
    def vel_theta(self) -> float:
        """ Target velocity vector rotation angle, in degrees CCW. """
        return self._definition['vel_theta']

    @property
    def is_identity_for_pos(self) -> bool:
        """
        Is this target transform the identity (unity scale, zero rotation) WRT target position? By convention, a
        rotation within 0.01 deg of zero and a scale factor within 0.01 of unity is considered an identity transform.
        """
        return (abs(self.pos_scale - 1) < 0.01) and (abs(self.pos_theta) < 0.01)

    @property
    def is_identity_for_vel(self) -> bool:
        """
        Is this target transform the identity (unity scale, zero rotation) WRT target velocity? By convention, a
        rotation within 0.01 deg of zero and a scale factor within 0.01 of unity is considered an identity transform.
        """
        return (abs(self.vel_scale - 1) < 0.01) and (abs(self.vel_theta) < 0.01)

    def to_bytes(self) -> bytes:
        """
        Prepare a byte sequence encoding this Maestro trial target transform. To reconstruct the transform object, pass
        this byte sequence to the constructor.

        Returns:
            The byte sequence encoding this Maestro trial target transform.
        """

        return struct.pack(TargetTransform._struct_format, self.pos_offset_h, self.pos_offset_v, self.pos_scale,
                           self.pos_theta, self.vel_scale, self.vel_theta)

    @staticmethod
    def size_in_bytes() -> int:
        """ Length of the byte sequence encoding a TargetTransform instance, as generated by to_bytes(). """
        return struct.calcsize(TargetTransform._struct_format)

    def transform_position(self, p: Point2D) -> None:
        """
        Rotate and scale a target position vector IAW this global target transform.

        Args:
            p: Target position vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_pos:
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta += self.pos_theta * math.pi / 180.0
        amp = p.distance_from(0, 0) * self.pos_scale
        p.set(amp*math.cos(theta), amp*math.sin(theta))

    def transform_velocity(self, p: Point2D) -> None:
        """
        Rotate and scale a target velocity vector IAW this global target transform.

        Args:
            p: Target velocity vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_vel:
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta += self.vel_theta * math.pi / 180.0
        amp = p.distance_from(0, 0) * self.vel_scale
        p.set(amp*math.cos(theta), amp*math.sin(theta))

    def invert_position(self, p: Point2D) -> None:
        """
        Rotate and scale a target position vector IAW the inverse of this global target transform.

        Args:
            p: Target position vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_pos:
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta -= self.pos_theta * math.pi / 180.0
        amp = 0 if (self.pos_scale == 0) else (p.distance_from(0, 0) / self.pos_scale)
        p.set(amp * math.cos(theta), amp * math.sin(theta))

    def invert_velocity(self, p: Point2D) -> None:
        """
        Rotate and scale a target velocity vector IAW the inverse of this global target transform.

        Args:
            p: Target velocity vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_vel:
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta -= self.vel_theta * math.pi / 180.0
        amp = 0 if (self.vel_scale == 0) else (p.distance_from(0, 0) / self.vel_scale)
        p.set(amp * math.cos(theta), amp * math.sin(theta))


SECTION_TAG_SIZE = 18  # max length of tagged section label in a TAG_SECT_RECORD
MAX_SEGMENTS = 30  # max number of segments allowed in a Maestro trial


class TaggedSection:
    """
    The definition of a tagged section in a Maestro trial, as culled from a Maestro data file.
    """
    _sect_format: str = f"<{SECTION_TAG_SIZE}sbb"
    """ Format string defining byte packing of one taggec section within a tagged section record. """

    def __init__(self, record: bytes, offset: int = 0):
        """
        Construct a Maestro trial tagged section from the original raw byte sequence within a Maestro data file record.

        Args:
            record: The tagged section record (typically 1KB in size).
            offset: Offset within record to the start of the byte sequence defining the tagged section. Default = 0.
        Raises:
            DataFileError: If an error occurs while parsing the tagged section.
        """
        self._definition = dict()
        """ The tagged section as a dictionary of parameter values keyed by parameter names. """
        self._raw_bytes = record[offset:offset + struct.calcsize(TaggedSection._sect_format)]
        """ Internal copy of the byte sequence that defines the tagged section. """

        try:
            raw_fields = struct.unpack_from(TaggedSection._sect_format, self._raw_bytes, 0)
            self._definition['label'] = raw_fields[0].decode("ascii").split('\0', 1)[0]
            self._definition['start_seg'] = int(raw_fields[1])
            self._definition['end_seg'] = int(raw_fields[2])

            if not self._is_valid():
                raise DataFileError("Invalid trial tagged section found")

        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing trial tagged section: {str(err)}")

    def _is_valid(self) -> bool:
        """ Validity check after parsing tagged section from byte sequence. Not a complete validity check. """
        return (0 <= self.start_seg <= self.end_seg) and (self.end_seg < MAX_SEGMENTS) and (len(self.label) > 0)

    def __eq__(self, other: TaggedSection) -> bool:
        return ((self.__class__ == other.__class__) and (self.start_seg == other.start_seg) and
                (self.end_seg == other.end_seg) and (self.label == other.label))

    def __hash__(self) -> int:
        return hash((self.start_seg, self.end_seg, self.label))

    def __str__(self) -> str:
        return f"{self.label} [{self.start_seg}:{self.end_seg}]"

    @property
    def start_seg(self) -> int:
        """ Index of the first trial segment in the tagged section. """
        return self._definition['start_seg']

    @property
    def end_seg(self) -> int:
        """ Index of the last trial segment in the tagged section. """
        return self._definition['end_seg']

    @property
    def label(self) -> str:
        """ The tagged section label. """
        return self._definition['label']

    @staticmethod
    def size_in_bytes() -> int:
        """ Length of the byte sequence encoding a TaggedSection instance, as generated by to_bytes(). """
        return struct.calcsize(TaggedSection._sect_format)

    def to_bytes(self) -> bytes:
        """
        The raw byte sequence encoding this Maestro trial tagged section. To reconstruct the tagged section object, pass
        this byte sequence to the constructor (with zero offset).

        Returns:
            The byte sequence encoding this tagged section.
        """
        return self._raw_bytes[:]

    @staticmethod
    def parse_tagged_sections(record: bytes) -> List[TaggedSection]:
        """
        Parse one or more tagged sections from a Maestro data file record (there will be only one such record in the
        data file, and only if the trial therein contains one or more tagged sections).

        Args:
            record: A data file record. Record tag ID must be TAG_SECT_RECORD.
        Returns:
            A list of one or more tagged sections culled from the record.
        Raises:
            DataFileError if an error occurs while parsing the record.
        """
        try:
            if record[0] != TAG_SECT_RECORD:
                raise DataFileError("Not a tagged section record!")
            sect_size = struct.calcsize(TaggedSection._sect_format)
            sections = []
            idx = RECORD_TAG_SIZE
            while (idx + sect_size < RECORD_SIZE) and (record[idx] != 0):
                sections.append(TaggedSection(record, offset=idx))
                idx += sect_size
            return sections
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing tagged section record: {str(err)}")

    @staticmethod
    def validate_tagged_sections(sections: Optional[List[TaggedSection]], num_segs: int) -> bool:
        """
        Verify that no tagged section overlaps another section in the list provided, and verify that each section's
        span is valid.

        Args:
            sections: List of tagged sections within a Maestro trial. Could be None or empty list.
            num_segs: Number of segments in the trial.
        Returns:
            bool - True if tagged section list is valid for a trial with the specified number of segments.
        """
        if isinstance(sections, list):
            for i, s1 in enumerate(sections):
                if (s1.start_seg >= num_segs) or (s1.end_seg >= num_segs):
                    return False
                for j, s2 in enumerate(sections):
                    if i == j:
                        continue
                    if (s1.start_seg <= s2.start_seg <= s1.end_seg) or (s1.start_seg <= s2.end_seg <= s1.end_seg):
                        return False
        return True


MAX_TGT_NAME_SIZE = 50
CX_CHAIR = 0x0016
CX_FIBER1 = 0x0017
CX_FIBER2 = 0x0018
CX_RED_LED1 = 0x0019
CX_RED_LED2 = 0x001A
CX_OKNDRUM = 0x001B
CX_XY_TGT = 0x001C
CX_RMV_TGT = 0x001D


def _validate_range(value: float, min_value: float, max_value: float, tol: float = 1e-6) -> bool:
    """
    Verify that the specified floating-point value lies in the specified min-max range within the specified tolerance.
    Since floating-point values can rarely be represented EXACTLY in computer hardware (eg, 0.01 = 0.0099999...787),
    it is important to take this into account when deciding whether a given value falls within a given rang.

    Args:
        value: The floating-point value to test
        min_value: The minimum of the range
        max_value: The maximum of the range
        tol: The maximum allowed difference between value and either range endpoint if 'min <= value <= max' test
            fails. Default = 1e-6

    Returns:
        True if value is within specified range or within tolerance of either range endpoint
    """
    return (min_value <= value <= max_value) or math.isclose(value, min_value, rel_tol=tol) or\
        math.isclose(value, max_value, rel_tol=tol)


class Target:
    """
    A Maestro target object, as culled from a Maestro data file.

    This is roughly equivalent to the CX_TARGET structure in Maestro's C++ codebase, except that it (and the classes
    defining the video target types -- `XYScopeTarget, VSGVideoTarget, and RMVideoTarget`) handles all the different
    versions of CX_TARGET since data file version 1. In addition, the continuous-mode specific parameters that are
    part of a "target block" are not parsed, as we generally don't support parsing of continuous-mode data files.
    """
    _tgt_hdr_format: str = f"<H{MAX_TGT_NAME_SIZE}s"
    """ 
    Byte-packing format for the header of a target definition block within a Maestro target record. The header 
    includes the target hardware type and name. The remainder of the block is the size of the largest possible video
    target parametric definition, followed by several parameters relevant only in Continuous mode (1 unsigned long and
    two floats).
    """
    _tgt_hdr_format_v: str = f"<2H{MAX_TGT_NAME_SIZE}s"
    """
    Byte-packing format for target header with data file version prepended as a 2-byte integer. This format is used
    when encoding/decoding a target object to/from a byte sequence.
    """

    def __init__(self, record: bytes, version: int, offset: int = 0):
        """
        Construct a Maestro target from the original raw byte sequence within a Maestro data file's target record.

        Args:
            record: The target record (typically 1KB in size).
            version: The data file version. This is required because the size and contents of a single Maestro target
                block has changed over time.
            offset: Offset within record to the start of a target block. Default = 0.
        Raises:
            DataFileError: If an error occurs while parsing, or if the target definition fails a validity check.
        """
        self._version = version
        """ The version number of the data file from which this target was extracted. """
        self._definition = dict()
        """ The target object as a dictionary of parameter values keyed by parameter names. """

        try:
            # unpack hardware type and target name.
            hardware, name_bytes = struct.unpack_from(Target._tgt_hdr_format, record, offset)
            if not (CX_CHAIR <= hardware <= CX_RMV_TGT):
                raise DataFileError(f"Unrecognized target hardware type ({hardware})")
            self._definition['hardware_type'] = hardware
            self._definition['name'] = name_bytes.decode("ascii").split('\0', 1)[0]
            offset_to_def = struct.calcsize(Target._tgt_hdr_format)

            # unpack video target definition, if applicable
            tgt_def: Optional[VSGVideoTarget, XYScopeTarget, RMVideoTarget] = None
            if hardware == CX_XY_TGT:
                tgt_def = XYScopeTarget(record, version, offset + offset_to_def)
            elif hardware == CX_RMV_TGT:
                if version < 8:
                    tgt_def = VSGVideoTarget(record, offset + offset_to_def)
                else:
                    tgt_def = RMVideoTarget(record, version, offset + offset_to_def)
            self._definition['definition'] = tgt_def
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing target record: {str(err)}")

    def __eq__(self, other: Target) -> bool:
        """
        Two targets are equal if they are implemented on the same hardware and, if applicable, have the same target
        definition. The target name is excluded from the equality test b/c it has no bearing on target behavior.
        """
        ok = (self.__class__ == other.__class__) and (self.hardware_type == other.hardware_type)
        if ok and (self.definition is not None):
            ok = (self.definition == other.definition)
        return ok

    def __hash__(self) -> int:
        """ The hash code includes only the target hardware type and definition, and excludes the target name. """
        return hash((self.hardware_type, self.definition))

    def __str__(self) -> str:
        if self.definition is None:
            if CX_FIBER1 <= self.hardware_type <= CX_RED_LED2:
                out = f"{self.name} (Optic Bench)"
            else:
                out = f"{self.name}"
        else:
            out = f"{self.name}: {str(self.definition)}"
        return out

    @property
    def data_file_version(self) -> int:
        """ The version number found in the Maestro data file from which this target was originally parsed. """
        return self._version

    @property
    def hardware_type(self) -> int:
        """ The defined constant identifying the target hardware type. """
        return self._definition['hardware_type']

    @property
    def name(self) -> str:
        """ The name assigned to this target."""
        return self._definition['name']

    @property
    def definition(self) -> Optional[XYScopeTarget, VSGVideoTarget, RMVideoTarget]:
        """ Parameterized definition of an extended video target implemented on the XYScope, RMVideo, or VSG2/3 video
        displays. Older targets like the 'Chair' and the fiber-optic bench targets have no additional parameters. """
        return self._definition['definition']

    @staticmethod
    def _block_size(version: int) -> int:
        """
        Return size of one target definition block within a 1KB target record in the Maestro data file. One target block
        includes the target hardware type and name, defining parameters for a video (XY Scope, VSG, RMVideo) target, and
        several parameters that apply only for continuous-mode files. Regardless the target type, the block size is
        determined by the size of the largest video target definition -- the VSG video target (version <= 7) or the
        RMVideo target definition (version > 7).

        Args:
            version: Applicable data file version number. This is required because the exact structure of the target
                definition block has evolved over time.
        Returns:
            int - Number of bytes in one target definition block (for the given file version)
        """
        max_def_fmt = VSGVideoTarget.record_format(version) if version <= 7 else RMVideoTarget.record_format(version)
        # note: the first character of max_def_fmt is the little-endian indicator '<'. This should only appear once.
        return struct.calcsize(f"<H{MAX_TGT_NAME_SIZE}s{max_def_fmt[1:]}L2f")

    @staticmethod
    def parse_targets(record: bytes, version: int) -> List[Target]:
        """
        Parse one or more Maestro target definitions listed in the specified record culled from a Maestro data file.
        Each target "block" within the record has the same size, regardless the target type. The target block size
        has changed over the course of Maestro's development, and that block size determines how many target
        definitions can be stored in 1KB record. Target definitions do NOT cross record boundaries.

        Args:
            record: The 1KB target record (tag = TGT_RECORD).
            version: The data file version number. This is required b/c the exact layout of the target record has
                evolved over time.
        Returns:
            The list of target definitions found in the record (order preserved).
        Raises:
            DataFileError - If any error occurs while parsing the record. Th
        """
        tgt_list = []
        block_size = Target._block_size(version)
        offset = RECORD_TAG_SIZE
        try:
            while (offset + block_size) < RECORD_SIZE:
                # unpack hardware type for next target block. If it is 0, we'ver reached end of target list
                hardware = struct.unpack_from("<H", record, offset)[0]
                if hardware == 0:
                    break
                tgt_list.append(Target(record, version, offset))
                offset += block_size
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing target record: {str(err)}")
        if len(tgt_list) == 0:
            raise DataFileError("Found no target definitions in a Maestro target record!")
        return tgt_list

    def to_bytes(self) -> bytes:
        """
        Prepare a byte sequence encoding this Maestro target object.

        For all target types, the byte sequence starts with the original data file's version number (as a short integer)
        followed by the hardware type and target name. For the video target types, the remainder of the sequence encodes
        the full parametric definition of the video target. The source file version is needed because the video target
        definitions have evolved over time.

        To reconstruct the target object, pass this byte sequence to from_bytes().

        Returns:
            The byte sequence encoding this Maestro target object.
        """
        raw = struct.pack(Target._tgt_hdr_format_v, self._version, self.hardware_type, self.name.encode('ascii'))
        if self.hardware_type in [CX_XY_TGT, CX_RMV_TGT]:
            raw += self.definition.to_bytes()
        return raw

    def size_in_bytes(self) -> int:
        """ Length of byte sequence serializing this Maestro target definition, as generated by to_bytes(). """
        n = struct.calcsize(Target._tgt_hdr_format_v)
        if isinstance(self.definition, VSGVideoTarget):
            n += struct.calcsize(VSGVideoTarget.record_format(self._version))
        elif isinstance(self.definition, XYScopeTarget):
            n += struct.calcsize(XYScopeTarget.record_format(self._version))
        elif isinstance(self.definition, RMVideoTarget):
            n += struct.calcsize(RMVideoTarget.record_format(self._version))
        return n

    @staticmethod
    def from_bytes(raw: bytes, offset: int = 0) -> Target:
        """
        Reconstruct a Maestro target object from its encoded byte sequence, as provided by to_bytes().

        Arguments:
            raw: The byte sequence.
            offset: Offset into byte sequence at which Maestro target definition begins. Default = 0.
        Returns:
            The target object encoded by the byte sequence.
        Raises:
            DataFileError: If the byte sequence cannot be parsed as a valid Maestro target object.
        """
        version = struct.unpack_from('<H', raw, offset)[0]
        return Target(raw, version, offset + struct.calcsize('<H'))


NUM_XY_TYPES = 11
XY_RECT_DOT = 0
XY_CENTER = 1
XY_SURROUND = 2
XY_RECTANNU = 3
XY_FAST_CENTER = 4
XY_FC_DOT_LIFE = 5
XY_FLOW_FIELD = 6
XY_ORIENTED_BAR = 7
XY_NOISY_DIR = 8
XY_FC_COHERENT = 9
XY_NOISY_SPEED = 10
XY_TYPE_LABELS = ['Spot/Dot Array', 'Center', 'Surround', 'Rectangular Annulus', 'Optimized Center',
                  'Opt Center Dot Life', 'Flow Field', 'Bar/Line', 'Noisy Dots (Direction)', 'Opt Center Coherence',
                  'Noisy Dots (Speed)']
MAX_DOT_LIFE_MS = 32767
MAX_DOT_LIFE_DEG = 327.67
MAX_DIR_OFFSET = 100
MAX_SPEED_OFFSET = 300
MIN_SPEED_LOG2 = 1
MAX_SPEED_LOG2 = 7
MIN_NOISE_UPDATE_MS = 2
MAX_NOISE_UPDATE_MS = 1024
MIN_FLOW_RADIUS_DEG = 0.5
MAX_FLOW_RADIUS_DEG = 44.99
MIN_FLOW_DIFF_DEG = 2.0
MAX_BAR_DRIFT_AXIS_DEG = 359.99
MIN_RECT_DIM_DEG = 0.01


class XYScopeTarget:
    """
    The definition of an XYScope target, as culled from a Maestro data file. Note that the XYScope fell out of use
    and is obsolete as of Maestro v4 (data file version 21).
    """
    _struct_format_pre_v9: str = "<3i5f"
    """ Format string defining byte packing of XYScope target definition prior to file version 9. """
    _struct_format: str = "<3i7f"
    """ Format string defining byte packing of XYScope target definition for file versions 9 and later. """

    @staticmethod
    def record_format(version: int) -> str:
        """ Byte-packing format for an XYScope target block within a Maestro data file of the specified version. """
        return XYScopeTarget._struct_format_pre_v9 if version < 9 else XYScopeTarget._struct_format

    def __init__(self, record: bytes, version: int, offset: int = 0):
        """
        Construct an XYScope target definition from the original raw byte sequence within a Maestro data file's
        target record.

        Args:
            record: The target record (typically 1KB in size).
            version: The data file version. This is required because the byte sequence is longer (to accommodate two
                more fields) in data file versions 9 and later.
            offset: Offset within record to the start of the byte sequence defining the XYScope target. Default = 0.
        Raises:
            DataFileError: If an error occurs while parsing the definition, or if the definition fails validity check.
        """
        self._version = version
        """ The version number of the data file from which this XYScope target definition was extracted. """
        self._definition = dict()
        """ The XYScope target definition as a dictionary of parameter values keyed by parameter names. """
        fmt = XYScopeTarget.record_format(version)
        self._raw_bytes = record[offset:offset + struct.calcsize(fmt)]
        """ Internal copy of the byte sequence that defines the XYScope target. """

        try:
            raw_fields = struct.unpack_from(fmt, self._raw_bytes, 0)
            self._definition['type'] = raw_fields[0]
            self._definition['n_dots'] = raw_fields[1]
            self._definition['dot_life_in_ms'] = (raw_fields[2] == 0)
            self._definition['dot_life'] = raw_fields[3]
            self._definition['width'] = raw_fields[4]
            self._definition['height'] = raw_fields[5]
            self._definition['inner_width'] = raw_fields[6]
            self._definition['inner_height'] = raw_fields[7]

            # the following fields were added in version 9. Set to 0 for older versions.
            self._definition['inner_x'] = 0.0 if version < 9 else raw_fields[8]
            self._definition['inner_y'] = 0.0 if version < 9 else raw_fields[9]

            if not self._is_valid():
                raise DataFileError("Invalid XYScope target definition found")
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing XYScope target definition: {str(err)}")

    def _is_valid(self) -> bool:
        """ Validity check after parsing definition from byte sequence. Not a complete validity check. """
        ok = (self.type >= XY_RECT_DOT) and (self.type < NUM_XY_TYPES) and (self.n_dots > 0)
        if ok and (self.type in [XY_FC_DOT_LIFE, XY_NOISY_DIR, XY_NOISY_SPEED]):
            ok = _validate_range(self.dot_life, 0, MAX_DOT_LIFE_MS if self.dot_life_in_ms else MAX_DOT_LIFE_DEG)
        if ok and (self.type != XY_RECT_DOT):
            if self.type == XY_FLOW_FIELD:
                ok = _validate_range(self.width, MIN_FLOW_RADIUS_DEG, MAX_FLOW_RADIUS_DEG)
            else:
                ok = _validate_range(self.width, MIN_RECT_DIM_DEG, float('inf'))
        if ok and not (self.type in [XY_RECT_DOT, XY_FLOW_FIELD]):
            ok = _validate_range(self.height, MIN_RECT_DIM_DEG, float('inf'))
        if ok:
            if self.type == XY_RECTANNU:
                ok = _validate_range(self.inner_width, MIN_RECT_DIM_DEG, float('inf'))
            elif self.type == XY_FLOW_FIELD:
                ok = _validate_range(self.inner_width, MIN_FLOW_RADIUS_DEG, MAX_FLOW_RADIUS_DEG)
                ok = ok and _validate_range(self.width - self.inner_width, MIN_FLOW_DIFF_DEG, float('inf'))
            elif self.type == XY_ORIENTED_BAR:
                ok = _validate_range(self.inner_width, 0, MAX_BAR_DRIFT_AXIS_DEG)
            elif self.type == XY_NOISY_DIR:
                ok = _validate_range(self.inner_width, 0, MAX_DIR_OFFSET)
            elif self.type == XY_NOISY_SPEED:
                ok = _validate_range(self.inner_width, 0, MAX_SPEED_OFFSET) if (int(self.inner_x) == 0) else \
                    (MIN_SPEED_LOG2 <= int(self.inner_width) <= MAX_SPEED_LOG2)
            elif self.type == XY_FC_COHERENT:
                ok = _validate_range(self.inner_width, 0, 100)
            if self.type == XY_RECTANNU:
                ok = _validate_range(self.inner_height, MIN_RECT_DIM_DEG, float('inf'))
            elif self.type in [XY_NOISY_DIR, XY_NOISY_SPEED]:
                ok = (MIN_NOISE_UPDATE_MS <= int(self.inner_height) <= MAX_NOISE_UPDATE_MS)
        return ok

    def __eq__(self, other: XYScopeTarget) -> bool:
        """
        Two XYScope targets are equal if they are the same type and the values of all RELEVANT parameters for that
        type are the same.
        """
        ok = (self.__class__ == other.__class__) and (self.type == other.type) and (self.n_dots == other.n_dots) and \
             (self.width == other.width) and (self.height == other.height)
        if ok and (self.type in [XY_FC_DOT_LIFE, XY_NOISY_DIR, XY_NOISY_SPEED]):
            ok = (self.dot_life_in_ms == other.dot_life_in_ms) and self.dot_life == other.dot_life
        if ok and \
           (self.type in [XY_RECTANNU, XY_FLOW_FIELD, XY_ORIENTED_BAR, XY_NOISY_DIR, XY_NOISY_SPEED, XY_FC_COHERENT]):
            ok = (self.inner_width == other.inner_width)
            if ok and (self.type in [XY_RECTANNU, XY_NOISY_DIR, XY_NOISY_SPEED]):
                ok = (self.inner_height == other.inner_height)
        if ok and (self.type in [XY_RECTANNU, XY_NOISY_SPEED]):
            ok = (self.inner_x == other.inner_x)
            if ok and (self.type == XY_RECTANNU):
                ok = (self.inner_y == other.inner_y)
        return ok

    def __hash__(self) -> int:
        """
        Hash code is computed on a tuple of all RELEVANT parameters. Any parameters irrelevant to the target type are
        excluded from the hash.
        """
        hash_attrs = [self.type, self.n_dots, self.width, self.height]
        if self.type in [XY_FC_DOT_LIFE, XY_NOISY_DIR, XY_NOISY_SPEED]:
            hash_attrs.extend([self.dot_life_in_ms, self.dot_life])
        if self.type in [XY_RECTANNU, XY_FLOW_FIELD, XY_ORIENTED_BAR, XY_NOISY_DIR, XY_NOISY_SPEED, XY_FC_COHERENT]:
            hash_attrs.append(self.inner_width)
            if self.type in [XY_RECTANNU, XY_NOISY_DIR, XY_NOISY_SPEED]:
                hash_attrs.append(self.inner_height)
        if self.type in [XY_RECTANNU, XY_NOISY_SPEED]:
            hash_attrs.append(self.inner_x)
            if self.type == XY_RECTANNU:
                hash_attrs.append(self.inner_y)
        return hash(tuple(hash_attrs))

    def __str__(self) -> str:
        out = f"[XYScope] {XY_TYPE_LABELS[self.type]}: #dots={self.n_dots}; "
        if self.type == XY_RECT_DOT:
            out += f"width={self.width:.2f} deg, spacing={self.height:.2f} deg"
        elif self.type == XY_RECTANNU:
            out += f"outer={self.width:.2f} x {self.height:.2f} deg, inner={self.inner_width:.2f} x " \
                   f"{self.inner_height:.2f} deg, center=({self.inner_x:.2f}, {self.inner_y:.2f}) deg"
        elif self.type == XY_FLOW_FIELD:
            out += f"outer radius={self.width:.2f} deg, inner={self.inner_width:.2f} deg"
        else:
            out += f"{self.width:.2f} x {self.height:.2f} deg"
            if self.type == XY_ORIENTED_BAR:
                out += f"; drift axis = {self.inner_width:.2f} deg CCW"
            elif self.type == XY_FC_COHERENT:
                out += f"; coherence={int(self.inner_width)}%"
            elif self.type in [XY_FC_DOT_LIFE, XY_NOISY_DIR, XY_NOISY_SPEED]:
                out += f"; max dot life = "
                out += f"{int(self.dot_life)} ms" if self.dot_life_in_ms else f"{self.dot_life:.2f} deg"
                if self.type == XY_NOISY_DIR:
                    out += f"; noise range limit = +/-{int(self.inner_width)} deg"
                    out += f"; update interval = {int(self.inner_height)} ms"
                elif self.type == XY_NOISY_SPEED:
                    if 0 == int(self.inner_x):
                        out += f" additive speed noise, range limit = +/-{int(self.inner_width)}%"
                    else:
                        out += f" multiplicative speed noise 2^x, x in +/-{int(self.inner_width)}"
                    out += f"; update interval = {int(self.inner_height)} ms"
        return out

    def to_bytes(self) -> bytes:
        """
        The raw byte sequence encoding this XYScope target definition.  To reconstruct the target definition, pass this
        byte sequence to the constructor, along with the file version of the original data file. This method is
        intended only for use by `Target` when preparing the byte sequence encoding a Maestro trial object.

        Returns:
            The byte sequence encoding this XYScope target definition.
        """
        return self._raw_bytes[:]

    @property
    def type(self) -> int:
        """ The XYScope target type. """
        return self._definition['type']

    @property
    def n_dots(self) -> int:
        """ The number of dots in the target. """
        return self._definition['n_dots']

    @property
    def dot_life_in_ms(self) -> bool:
        """ True if dot lifetime is specified in ms; else in distance traveled in degrees subtended at eye. """
        return self._definition['dot_life_in_ms']

    @property
    def dot_life(self) -> float:
        """ Maximum lifetime of each target dot. """
        return self._definition['dot_life']

    @property
    def width(self) -> float:
        """
        Width of rectangle bounding target window, in degrees. Note, however, that the meaning of this property varies
        with target type.
        """
        return self._definition['width']

    @property
    def height(self) -> float:
        """
        Height of rectangle bounding target window, in degrees. Note, however, that the meaning of this property varies
        with target type.
        """
        return self._definition['height']

    @property
    def inner_width(self) -> float:
        """
        Width of inner bounding rectangle for the RECTANNU target, in degrees. The meaning of this property varies with
        target type, and is not applicable to some types.
        """
        return self._definition['inner_width']

    @property
    def inner_height(self) -> float:
        """
        Height of inner bounding rectangle for the RECTANNU target, in degrees. The meaning of this property varies with
        target type, and is not applicable to some types.
        """
        return self._definition['inner_height']

    @property
    def inner_x(self) -> float:
        """
        X-coordinate of center of inner bounding rectangle for the RECTANNU target, in degrees. Note, however, that the
        meaning of this property varies with target type, and is not applicable to most types. This parameter was
        introduced as of Maestro data file version 9. For earlier versions, it is always 0.
        """
        return self._definition['inner_x']

    @property
    def inner_y(self) -> float:
        """
        Y-coordinate of center of inner bounding rectangle for the RECTANNU target, in degrees. Note, however, that the
        meaning of this property varies with target type, and is not applicable to most types. This parameter was
        introduced as of Maestro data file version 9. For earlier versions, it is always 0.
        """
        return self._definition['inner_y']


NUM_VSG_TYPES = 8
VSG_PATCH = 0
VSG_SINE_GRATING = 1
VSG_SQUARE_GRATING = 2
VSG_SINE_PLAID = 3
VSG_SQUARE_PLAID = 4
VSG_TWO_SINE_GRATINGS = 5
VSG_TWO_SQUARE_GRATINGS = 6
VSG_STATIC_GABOR = 7
VSG_TYPE_LABELS = ['Patch', 'Sine Grating', 'Square Grating', 'Sine Plaid', 'Square Plaid', 'Two Sine Gratings'
                   'Two Square Gratings', 'Static Gabor']
VSG_RECT_WINDOW = 0
VSG_OVAL_WINDOW = 1
VSG_MAX_LUM = 1000
VSG_MAX_CON = 100


class VSGVideoTarget:
    """
    The definition of a VSG2/3 video target, as culled from a Maestro data file. Applicable only to Maestro data file
    versions 7 or earlier. The VSG2/3 hardware was deprecated as of file version 8.
    """
    _struct_format: str = "<8i9f"
    """ Format string defining byte packing of a VSG2/3 video target definition within a target record. """

    # noinspection PyUnusedLocal
    @staticmethod
    def record_format(version: int) -> str:
        """ Byte-packing format for a VSG2/3 video target block within a Maestro data file of the specified version. """
        return VSGVideoTarget._struct_format

    def __init__(self, record: bytes, offset: int = 0):
        """
        Construct a VSG2/3 video target definition from the original raw byte sequence within a Maestro data file's
        target record.

        Args:
            record: The target record (typically 1KB in size).
            offset: Offset within record to the start of the byte sequence defining a VSG2/3 video target. Default = 0.
        Raises:
            DataFileError: If an error occurs while parsing the definition, or if the definition fails validity check.
        """
        self._definition = dict()
        """ The VSG2/3 definition as a dictionary of parameter values keyed by parameter names. """
        self._raw_bytes = record[offset:offset + struct.calcsize(VSGVideoTarget._struct_format)]
        """ Internal copy of the byte sequence that defines the VSG2/3 video target. """

        try:
            raw_fields = struct.unpack_from(VSGVideoTarget._struct_format, self._raw_bytes, 0)
            self._definition['type'] = raw_fields[0]
            self._definition['is_rect'] = (raw_fields[1] == VSG_RECT_WINDOW)
            self._definition['rgb_mean'] = tuple(raw_fields[2:5])
            self._definition['rgb_contrast'] = tuple(raw_fields[5:8])
            self._definition['width'] = raw_fields[8]
            self._definition['height'] = raw_fields[9]
            self._definition['sigma'] = raw_fields[10]
            self._definition['spatial_frequency'] = tuple(raw_fields[11:13])
            self._definition['drift_axis'] = tuple(raw_fields[13:15])
            self._definition['spatial_phase'] = tuple(raw_fields[15:17])

            if not self._is_valid():
                raise DataFileError("Invalid VSG video target definition found")

        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing VSG video target definition: {str(err)}")

    def _is_valid(self) -> bool:
        """ Validity check after parsing definition from byte sequence. Not a complete validity check. """
        ok = (self.type >= VSG_PATCH) and (self.type < NUM_VSG_TYPES)
        if ok:
            for i in range(3):
                ok = (0 <= self.rgb_mean[i] <= VSG_MAX_LUM) and (0 <= self.rgb_contrast[i] <= VSG_MAX_CON)
                if not ok:
                    break
        return ok

    def __eq__(self, other: VSGVideoTarget) -> bool:
        """
        Two VSGVideo targets are equal if they are the same type and the values of all RELEVANT parameters for that
        type are the same.
        """
        ok = (self.__class__ == other.__class__) and (self.type == other.type) and (self.is_rect == other.is_rect) and \
             (self.width == other.width) and (self.height == other.height)
        ok = ok and (self.rgb_mean == other.rgb_mean)
        if ok and (self.type != VSG_PATCH):
            ok = ok and (self.rgb_contrast == other.rgb_contrast)
            i = 0
            n_gratings = 2 if self.type > VSG_SQUARE_GRATING else 1
            while ok and i < n_gratings:
                ok = (self.spatial_frequency[i] == other.spatial_frequency[i]) and \
                     (self.spatial_phase[i] == other.spatial_phase[i]) and \
                     (self.drift_axis[i] == other.drift_axis[i])
                i += 1
            if ok and (self.type == VSG_STATIC_GABOR):
                ok = (self.sigma == other.sigma)
        return ok

    def __hash__(self) -> int:
        """
        Hash code is computed on a tuple of all RELEVANT parameters. Any parameters irrelevant to the target type are
        excluded from the hash.
        """
        hash_attrs = [self.type, self.is_rect, self.width, self.height]
        hash_attrs.extend(self.rgb_mean)
        if self.type != VSG_PATCH:
            hash_attrs.extend(self.rgb_contrast)
            n_gratings = 2 if self.type > VSG_SQUARE_GRATING else 1
            for i in range(n_gratings):
                hash_attrs.extend([self.spatial_frequency[i], self.spatial_phase[i], self.drift_axis[i]])
            if self.type == VSG_STATIC_GABOR:
                hash_attrs.append(self.sigma)
        return hash(tuple(hash_attrs))

    def __str__(self) -> str:
        out = f"[VSGVideo] {VSG_TYPE_LABELS[self.type]}: {self.width:.2f} x {self.height:.2f} deg "
        out += f"{'rect' if self.is_rect else 'oval'}, RGB={self.rgb_mean}"
        if self.type != VSG_PATCH:
            out += f", contrast_RGB={self.rgb_contrast}"
            if self.type == VSG_STATIC_GABOR:
                out += f", sigma={self.sigma:.2f}"
            for i in range(2 if self.type >= VSG_SQUARE_GRATING else 1):
                out += f"\n   Grating {i + 1}: freq={self.spatial_frequency[i]:.2f}, " \
                       f"phase={self.spatial_phase[i]:.2f}, drift axis={self.drift_axis[i]:.2f}"
        return out

    def to_bytes(self) -> bytes:
        """
        The raw byte sequence encoding this VSG2/3 video target definition. To reconstruct the target definition, pass
        this byte sequence to the constructor (with zero offset). This method is intended only for use by `Target` when
        preparing a byte sequence encoding a Maestro target object.

        Returns:
            The byte sequence encoding the target definition.
        """
        return self._raw_bytes[:]

    @property
    def type(self) -> int:
        """ The VSG2/3 video target type. """
        return self._definition['type']

    @property
    def is_rect(self) -> bool:
        """ True if target window shape is rectangular; else oval. """
        return self._definition['is_rect']

    @property
    def rgb_mean(self) -> Tuple[int]:
        """ The mean RGB color of the target as a tuple (R,G,B), where each color component lies in [0..1000]. """
        return self._definition['rgb_mean']

    @property
    def rgb_contrast(self) -> Tuple[int]:
        """ The mean RGB contrast of the target as a tuple (R,G,B), where each component lies in [0..100%]. """
        return self._definition['rgb_contrast']

    @property
    def width(self) -> float:
        """ Width of rectangle bounding target window, in degrees subtended at eye. """
        return self._definition['width']

    @property
    def height(self) -> float:
        """ Height of rectangle bounding target window, in degrees subtended at eye. """
        return self._definition['height']

    @property
    def sigma(self) -> float:
        """ Standard deviation of circular Gaussian window for STATICGABOR target type. """
        return self._definition['sigma']

    @property
    def spatial_frequency(self) -> Tuple[float]:
        """ Grating spatial frequencies for two gratings -- as tuple (f1, f2) -- in cycles/degree. """
        return self._definition['spatial_frequency']

    @property
    def drift_axis(self) -> Tuple[float]:
        """ Drift axes for two gratings -- as tuple (ax1, ax2) -- in degrees CCW. """
        return self._definition['drift_axis']

    @property
    def spatial_phase(self) -> Tuple[float]:
        """ Initial spatial phase for two gratings -- as tuple (ph1, ph2) -- in degrees. """
        return self._definition['spatial_phase']


NUM_RMV_TYPES = 9
RMV_POINT = 0
RMV_RANDOM_DOTS = 1
RMV_FLOW_FIELD = 2
RMV_BAR = 3
RMV_SPOT = 4
RMV_GRATING = 5
RMV_PLAID = 6
RMV_MOVIE = 7    # Support added as of file version 13
RMV_IMAGE = 8    # Support added in Maestro 3.3.1 (file version 20)
RMV_TYPE_LABELS = ['Point', 'Random-Dot Patch', 'Flow Field', 'Bar', 'Spot', 'Grating', 'Plaid', 'Movie', 'Image']

RMV_RECT = 0
RMV_OVAL = 1
RMV_RECT_ANNULUS = 2
RMV_OVAL_ANNULUS = 3
RMV_APERTURE_LABELS = ['Rectangular', 'Oval', 'Rect. Annulus', 'Oval Annulus']

RMV_FILENAME_LEN = 30
RMV_FILENAME_PATTERN = re.compile(r'[a-zA-Z\d_\\.]+')
RMV_MIN_RECT_DIM = 0.01
RMV_MAX_RECT_DIM = 120.0
RMV_MAX_NUM_DOTS = 9999
RMV_MIN_DOT_SIZE = 1
RMV_MAX_DOT_SIZE = 10
RMV_MAX_NOISE_DIR = 180
RMV_MAX_NOISE_SPEED = 300
RMV_MIN_SPEED_LOG2 = 1
RMV_MAX_SPEED_LOG2 = 7

# flag bits in RMVideoTarget.flags
RMV_F_DOT_LIFE_MS = (1 << 0)
RMV_F_DIR_NOISE = (1 << 1)
RMV_F_IS_SQUARE = (1 << 2)
RMV_F_INDEPENDENT_GRATINGS = (1 << 3)
RMV_F_SPEED_LOG2 = (1 << 4)
RMV_F_REPEAT = (1 << 5)
RMV_F_PAUSE_WHEN_OFF = (1 << 6)
MV_F_AT_DISPLAY_RATE = (1 << 7)
RMV_F_ORIENT_ADJ = (1 << 8)
RMV_F_WRT_SCREEN = (1 << 9)


class RMVideoTarget:
    """
    The definition of an RMVideo target, as culled from a Maestro data file. The RMVideo target display was first
    introduced in Maestro 2.0.0 (file version 8).
    """
    _struct_format_pre_v13: str = "<7i4f6i9f"
    """ Format string defining byte packing of RMVideo target definition prior to file version 12. """
    _struct_format_pre_v23: str = "<7i4f6i9f32s32s"
    """ Format string defining byte packing of RMVideo target definition for file versions 12 - 22. """
    _struct_format: str = "<7i4f6i9f32s32s3i"
    """ Format string defining byte packing of RMVideo target defintion for file versions > 22. """

    @staticmethod
    def record_format(version: int) -> str:
        """ Byte-packing format for an RMVideo target block within a Maestro data file of the specified version. """
        return RMVideoTarget._struct_format_pre_v13 if version < 13 else \
            (RMVideoTarget._struct_format_pre_v23 if version < 23 else RMVideoTarget._struct_format)

    def __init__(self, record: bytes, version: int, offset: int = 0):
        """
        Construct an RMVideo target definition from the original raw byte sequence within a Maestro data file's
        target record.

        Args:
            record: The target record (typically 1KB in size).
            version: The data file version. This is required because the byte sequence has changed several times as
                the Maestro application has evovled.
            offset: Offset within record to the start of the byte sequence defining the RMVideo target. Default = 0.
        Raises:
            DataFileError: If an error occurs while parsing the definition, or if the definition fails validity check.
        """
        self._version = version
        """ The version number of the data file from which this RMVideo target definition was extracted. """
        self._definition = dict()
        """ The RMVideo target definition as a dictionary of parameter values keyed by parameter names. """
        fmt = RMVideoTarget.record_format(version)
        self._raw_bytes = record[offset:offset + struct.calcsize(fmt)]
        """ Internal copy of the byte sequence that defines the RMVideo target. """

        try:
            if version < 8:
                raise DataFileError("RMVideo targets not supported for data file versions 7 and earlier")
            raw_fields = struct.unpack_from(fmt, self._raw_bytes, 0)
            self._definition['type'] = raw_fields[0]
            self._definition['aperture'] = raw_fields[1]
            self._definition['flags'] = raw_fields[2]
            self._definition['rgb_mean'] = tuple(raw_fields[3:5])
            self._definition['rgb_contrast'] = tuple(raw_fields[5:7])
            self._definition['outer_w'] = raw_fields[7]
            self._definition['outer_h'] = raw_fields[8]
            self._definition['inner_w'] = raw_fields[9]
            self._definition['inner_h'] = raw_fields[10]
            self._definition['num_dots'] = raw_fields[11]
            self._definition['dot_size'] = raw_fields[12]
            self._definition['seed'] = raw_fields[13]
            self._definition['percent_coherent'] = raw_fields[14]
            self._definition['noise_update_intv'] = raw_fields[15]
            self._definition['noise_limit'] = raw_fields[16]
            self._definition['dot_life'] = raw_fields[17]
            self._definition['spatial_frequency'] = tuple(raw_fields[18:20])
            self._definition['drift_axis'] = tuple(raw_fields[20:22])
            self._definition['spatial_phase'] = tuple(raw_fields[22:24])
            self._definition['sigma'] = tuple(raw_fields[24:26])

            # process additional fields added in versions 13, 23; for earlier versions, use default values. NOTE that
            # the folder, file names MUST be set to "" if the type is neither RMV_MOVIE or RMV_IMAGE, because they may
            # contain garbage bytes otherwise!
            valid_folder = (version >= 13) and ((raw_fields[0] == RMV_MOVIE) or (raw_fields[0] == RMV_IMAGE))
            self._definition['media_folder'] = raw_fields[26].decode('ascii').split('\0', 1)[0] if valid_folder else ""
            self._definition['media_file'] = raw_fields[27].decode('ascii').split('\0', 1)[0] if valid_folder else ""
            self._definition['flicker_on_dur'] = raw_fields[28] if version >= 23 else 0
            self._definition['flicker_off_dur'] = raw_fields[29] if version >= 23 else 0
            self._definition['flicker_delay'] = raw_fields[30] if version >= 23 else 0

            if not self._is_valid(self._version):
                raise DataFileError("Invalid RMVideo target definition found")
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing RMVideo target definition: {str(err)}")

    def _is_valid(self, version: int) -> bool:
        """
        Does this RMVideoTarget represent a reasonable, valid RMVideo frame buffer target definition? This is primarily
        a check to ensure the target parameters have been successfully parsed from a valid target record; it is not an
        exhaustive check of validity. Only relevant parameters are checked. This is important, because Maestro only
        initializes relevant parameters when it stores the target record in the data file; other, irrelevant parameters
        may contain invalid garbage values.

        NOTE that we have to be careful when comparing floating-point values, since most real values cannot be
        represented exactly in hardware. Such comparisons must be done within "tolerances".

        Args:
            version: Version number of data file from which target definition was extracted.
        Returns:
            bool - True if valid, else False.
        """
        last_type = RMV_IMAGE if version >= 20 else (RMV_MOVIE if version >= 13 else RMV_GRATING)
        ok = (0 <= self.type <= last_type)
        # only need to check media folder and file names for the MOVIE and IMAGE target types
        if self.type in [RMV_MOVIE, RMV_IMAGE]:
            ok = (0 < len(self.media_folder) <= RMV_FILENAME_LEN) and (0 < len(self.media_file) <= RMV_FILENAME_LEN)
            ok = ok and (RMV_FILENAME_PATTERN.fullmatch(self.media_folder) is not None)
            ok = ok and (RMV_FILENAME_PATTERN.fullmatch(self.media_file) is not None)
            return ok
        if ok:
            ok = (RMV_RECT <= self.aperture <= RMV_OVAL_ANNULUS)
        if ok and (self.type in [RMV_GRATING, RMV_PLAID]):
            ok = (self.aperture <= RMV_OVAL)
            con = self.rgb_contrast[0]
            ok = ok and ((con & 0x0FF) <= 100) and (((con >> 8) & 0x0FF) <= 100) and (((con >> 16) & 0x0FF) <= 100)
            ok = ok and _validate_range(self.spatial_frequency[0], 0.01, float('inf'))
            if ok and (self.type == RMV_PLAID):
                con = self.rgb_contrast[1]
                ok = ok and ((con & 0x0FF) <= 100) and (((con >> 8) & 0x0FF) <= 100) and (((con >> 16) & 0x0FF) <= 100)
                ok = ok and _validate_range(self.spatial_frequency[1], 0.01, float('inf'))
        ok = ok and _validate_range(self.outer_w, (0 if self.type == RMV_BAR else RMV_MIN_RECT_DIM), RMV_MAX_RECT_DIM)
        ok = ok and _validate_range(self.outer_h, RMV_MIN_RECT_DIM, RMV_MAX_RECT_DIM)
        ok = ok and _validate_range(self.inner_w, RMV_MIN_RECT_DIM, RMV_MAX_RECT_DIM)
        ok = ok and _validate_range(self.inner_h, RMV_MIN_RECT_DIM, RMV_MAX_RECT_DIM)
        if ok and (self.type in [RMV_FLOW_FIELD, RMV_RANDOM_DOTS, RMV_SPOT]):
            ok = (self.outer_w > self.inner_w)
        if ok and (self.type in [RMV_RANDOM_DOTS, RMV_SPOT]):
            ok = (self.outer_h > self.inner_h)
        if ok and (self.type in [RMV_RANDOM_DOTS, RMV_FLOW_FIELD]):
            ok = (0 <= self.num_dots <= RMV_MAX_NUM_DOTS)
        if ok and (self.type in [RMV_RANDOM_DOTS, RMV_FLOW_FIELD, RMV_POINT]):
            ok = (RMV_MIN_DOT_SIZE <= self.dot_size <= RMV_MAX_DOT_SIZE)
        if ok and (self.type == RMV_RANDOM_DOTS):
            ok = (0 <= self.percent_coherent <= 100)
            ok = ok and _validate_range(self.dot_life, 0, float('inf'))
            if ok and ((self.flags & RMV_F_DIR_NOISE) != 0):
                ok = (0 <= self.noise_limit <= RMV_MAX_NOISE_DIR)
            if ok and ((self.flags & RMV_F_DIR_NOISE) == 0):
                min_speed = RMV_MIN_SPEED_LOG2 if (self.flags & RMV_F_SPEED_LOG2) != 0 else 0
                max_speed = RMV_MAX_SPEED_LOG2 if (self.flags & RMV_F_SPEED_LOG2) != 0 else RMV_MAX_NOISE_SPEED
                ok = (min_speed <= self.noise_limit <= max_speed)
        if ok and (self.type in [RMV_SPOT, RMV_RANDOM_DOTS, RMV_GRATING, RMV_PLAID]):
            ok = _validate_range(self.sigma[0], 0, float('inf')) and _validate_range(self.sigma[1], 0, float('inf'))
        return ok

    def __eq__(self, other: RMVideoTarget) -> bool:
        """
        Two RMVideo targets are equal if they are the same type and the values of all RELEVANT parameters for that
        type are the same. NOTE, however, that the random seed parameter is excluded from the equality test.
        """
        # NOTE that attribute 'seed' is excluded from equality test and hash
        ok = (self.__class__ == other.__class__) and (self.type == other.type)
        if not ok:
            return False

        if self.type == RMV_POINT:
            ok = (self.rgb_mean[0] == other.rgb_mean[0]) and (self.dot_size == other.dot_size)
        elif self.type == RMV_RANDOM_DOTS:
            ok = (self.rgb_mean[0] == other.rgb_mean[0]) and (self.rgb_contrast[0] == other.rgb_contrast[0]) and \
                 (self.outer_w == other.outer_w) and (self.outer_h == other.outer_h) and \
                 (self.aperture == other.aperture) and (self.flags == other.flags)
            if ok and (self.aperture > RMV_OVAL):
                ok = (self.inner_w == other.inner_w) and (self.inner_h == other.inner_h)
            ok = ok and (self.sigma == other.sigma) and (self.num_dots == other.num_dots) and \
                (self.dot_size == other.dot_size) and (self.percent_coherent == other.percent_coherent) and \
                (self.dot_life == other.dot_life) and (self.noise_update_intv == other.noise_update_intv) and \
                (self.noise_limit == other.noise_limit)
        elif self.type == RMV_FLOW_FIELD:
            ok = (self.rgb_mean[0] == other.rgb_mean[0]) and (self.outer_w == other.outer_w) and \
                 (self.inner_w == other.inner_w) and (self.num_dots == other.num_dots) and \
                 (self.dot_size == other.dot_size)
        elif self.type == RMV_BAR:
            ok = (self.rgb_mean[0] == other.rgb_mean[0]) and (self.outer_w == other.outer_w) and \
                 (self.outer_h == other.outer_h) and (self.drift_axis[0] == other.drift_axis[0])
        elif self.type == RMV_SPOT:
            ok = (self.rgb_mean[0] == other.rgb_mean[0]) and (self.outer_w == other.outer_w) and \
                 (self.outer_h == other.outer_h) and (self.aperture == other.aperture) and (self.sigma == other.sigma)
            if ok and (self.aperture > RMV_OVAL):
                ok = (self.inner_w == other.inner_w) and (self.inner_h == other.inner_h)
        elif self.type in [RMV_GRATING, RMV_PLAID]:
            ok = (self.aperture == other.aperture) and (self.flags == other.flags) and \
                 (self.outer_w == other.outer_w) and (self.outer_h == other.outer_h) and (self.sigma == other.sigma)
            if ok and (self.aperture > RMV_OVAL):
                ok = (self.inner_w == other.inner_w) and (self.inner_h == other.inner_h)
            if ok:
                for i in range(2 if self.type == RMV_PLAID else 1):
                    ok = ok and (self.rgb_mean[i] == other.rgb_mean[i]) and \
                         (self.rgb_contrast[i] == other.rgb_contrast[i]) and \
                         (self.spatial_frequency[i] == other.spatial_frequency[i]) and \
                         (self.spatial_phase[i] == other.spatial_phase[i]) and \
                         (self.drift_axis[i] == other.drift_axis[i])
        elif self.type in [RMV_MOVIE, RMV_IMAGE]:
            ok = (self.media_folder == other.media_folder) and (self.media_file == other.media_file)
            if self.type == RMV_MOVIE:
                ok = ok and (self.flags == other.flags)
        else:
            ok = False
        return ok

    def __hash__(self) -> int:
        """
        Hash code is computed on a tuple of all RELEVANT parameters. Any parameters irrelevant to the target type,
        plus the random seed parameter, are excluded from the hash.
        """
        hash_attrs = [self.type]
        if self.type == RMV_POINT:
            hash_attrs.extend([self.rgb_mean[0], self.dot_size])
        elif self.type == RMV_RANDOM_DOTS:
            hash_attrs.extend([self.rgb_mean[0], self.rgb_contrast[0], self.outer_w, self.outer_h, self.aperture,
                               self.flags])
            if self.aperture > RMV_OVAL:
                hash_attrs.extend([self.inner_w, self.inner_h])
            hash_attrs.extend(self.sigma)
            hash_attrs.extend([self.num_dots, self.dot_size, self.percent_coherent, self.dot_life,
                               self.noise_update_intv, self.noise_limit])
        elif self.type == RMV_FLOW_FIELD:
            hash_attrs.extend([self.rgb_mean[0], self.outer_w, self.inner_w, self.num_dots, self.dot_size])
        elif self.type == RMV_BAR:
            hash_attrs.extend([self.rgb_mean[0], self.outer_w, self.outer_h, self.drift_axis[0]])
        elif self.type == RMV_SPOT:
            hash_attrs.extend([self.rgb_mean[0], self.outer_w, self.outer_h, self.aperture])
            hash_attrs.extend(self.sigma)
            if self.aperture > RMV_OVAL:
                hash_attrs.extend([self.inner_w, self.inner_h])
        elif self.type in [RMV_GRATING, RMV_PLAID]:
            hash_attrs.extend([self.aperture, self.flags, self.outer_w, self.outer_h])
            hash_attrs.extend(self.sigma)
            if self.aperture > RMV_OVAL:
                hash_attrs.extend([self.inner_w, self.inner_h])
            for i in range(2 if self.type == RMV_PLAID else 1):
                hash_attrs.extend([self.rgb_mean[i], self.rgb_contrast[i], self.spatial_frequency[i],
                                   self.spatial_phase[i], self.drift_axis[i]])
        else:  # RMV_MOVIE, RMV_IMAGE
            hash_attrs.extend([self.media_folder, self.media_file])
            if self.type == RMV_MOVIE:
                hash_attrs.append(self.flags)
        return hash(tuple(hash_attrs))

    def __str__(self) -> str:
        out = f"[RMVideo] {RMV_TYPE_LABELS[self.type]}: "
        if self.type == RMV_POINT:
            out += f"RGB={self.rgb_mean[0]:X}, dot size={self.dot_size}"
        elif self.type == RMV_RANDOM_DOTS:
            out += f"RGB={self.rgb_mean[0]:X}, contrast_RGB={self.rgb_contrast[0]:X}, " \
                  f"flags={self.flags:X}, shape={RMV_APERTURE_LABELS[self.aperture]}, {self.outer_w:.2f} x " \
                  f"{self.outer_h:.2f} deg, "
            if self.aperture > RMV_OVAL:
                out += f"hole: {self.inner_w:.2f} x {self.inner_h:.2f} deg, "
            out += f"sigma x,y: {self.sigma[0]:.2f}, {self.sigma[1]:.2f} deg\n#dots={self.num_dots}, " \
                   f"dot size={self.dot_size}, dot life={self.dot_life:.2f}, coherence={self.percent_coherent}%, " \
                   f"noise limit, update interval={self.noise_limit}, {self.noise_update_intv}"
        elif self.type == RMV_FLOW_FIELD:
            out += f"RGB={self.rgb_mean[0]:X}, #dots={self.num_dots}, dot size={self.dot_size}, " \
                  f"radii={self.inner_w:.2f} deg, {self.outer_w:.2f} deg"
        elif self.type == RMV_BAR:
            out += f"RGB={self.rgb_mean[0]:X}, {self.outer_w:.2f} x {self.outer_h:.2f} deg, " \
                  f"drift axis={self.drift_axis[0]:.2f} deg"
        elif self.type == RMV_SPOT:
            out += f"RGB={self.rgb_mean[0]:X}, shape={RMV_APERTURE_LABELS[self.aperture]}, " \
                  f"{self.outer_w:.2f} x {self.outer_h:.2f} deg, "
            if self.aperture > RMV_OVAL:
                out += f"hole: {self.inner_w:.2f} x {self.inner_h:.2f} deg, "
            out += f"sigma x,y: {self.sigma[0]:.2f}, {self.sigma[1]:.2f} deg"
        elif self.type in [RMV_GRATING, RMV_PLAID]:
            out += f"shape={RMV_APERTURE_LABELS[self.aperture]}, flags={self.flags:X}, " \
                  f"{self.outer_w:.2f} x {self.outer_h:.2f} deg, "
            if self.aperture > RMV_OVAL:
                out += f"hole: {self.inner_w:.2f} x {self.inner_h:.2f} deg, "
            out += f"sigma x,y: {self.sigma[0]:.2f}, {self.sigma[1]:.2f} deg"
            for i in range(2 if self.type == RMV_PLAID else 1):
                out += f"\n   Grating {i+1}: RGB={self.rgb_mean[i]:X}, contrast_RGB={self.rgb_contrast[i]:X}, " \
                       f"freq={self.spatial_frequency[i]:.2f}, phase={self.spatial_phase[i]:.2f}, " \
                       f"drift axis={self.drift_axis[i]:.2f}"
        elif self.type in [RMV_MOVIE, RMV_IMAGE]:
            out += f"folder={self.media_folder}, file={self.media_file}"
            if self.type == RMV_IMAGE:
                out += f" flags={self.flags:X}"
        return out

    def to_bytes(self) -> bytes:
        """
        The raw byte sequence encoding this RMVideo target definition. To reconstruct the target definition, pass this
        byte sequence to the constructor, along with the file version of the original data file. This method is
        intended only for use by `Target` when preparing the byte sequence encoding a Maestro trial object.

        Returns:
            The byte sequence encoding this XYScope target definition.
        """
        return self._raw_bytes[:]

    @property
    def type(self) -> int:
        """ The RMVideo target type. """
        return self._definition['type']

    @property
    def aperture(self) -> int:
        """ The RMVideo target window shape. """
        return self._definition['aperture']

    @property
    def flags(self) -> int:
        """ The RMVideo target flag bits. """
        return self._definition['flags']

    @property
    def rgb_mean(self) -> Tuple[int]:
        """
        Mean color for each of two gratings -- as tuple (rgb1, rgb2) --, where the R/G/B color components are packed
        into an integer as 0x00RRGGBB.
        """
        return self._definition['rgb_mean']

    @property
    def rgb_contrast(self) -> Tuple[int]:
        """
        Mean color contrast for each of two gratings -- as tuple (con1, con2) --, where the R/G/B constrast components
        are packed into an integer as 0x00RRGGBB and each component is percent contrast restricted to [0..100].
        """
        return self._definition['rgb_contrast']

    @property
    def outer_w(self) -> float:
        """
        Width of outer rectangle bounding target window, in degrees.
        """
        return self._definition['outer_w']

    @property
    def outer_h(self) -> float:
        """
        Height of outer rectangle bounding target window, in degrees.
        """
        return self._definition['outer_h']

    @property
    def inner_w(self) -> float:
        """
        Width of inner rectangle bounding target window, in degrees. For annular target shapes only.
        """
        return self._definition['inner_w']

    @property
    def inner_h(self) -> float:
        """
        Height of inner rectangle bounding target window, in degrees. For annular target shapes only.
        """
        return self._definition['inner_h']

    @property
    def num_dots(self) -> int:
        """ Total number of dots comprising the target, for selected target types. """
        return self._definition['num_dots']

    @property
    def dot_size(self) -> int:
        """ Target dot size in pixels, for selected target types. """
        return self._definition['dot_size']

    @property
    def seed(self) -> int:
        """
        Seed for a random number generator that determines initial target dot locations, for relevant target types.
        It also seeds a separate RNG for dot directional or speed noise.
        """
        return self._definition['seed']

    @property
    def percent_coherent(self) -> int:
        """ Percent coherence in target dot motion, in [0..100%]. For random-dot target type only. """
        return self._definition['percent_coherent']

    @property
    def noise_update_intv(self) -> int:
        """ Noise update interval in milliseconds; 0 for no noise. For random-dot target type only. """
        return self._definition['noise_update_intv']

    @property
    def noise_limit(self) -> int:
        """ Speed or directional noise limit. For random-dot target type only. """
        return self._definition['noise_limit']

    @property
    def dot_life(self) -> float:
        """
        Maximum lifetime of each target dot, in milliseconds or in degrees traveled (depending on target flag
        RMV_F_LIFEINMS). For random-dot target type only. 0 => infinite lifetime.
        """
        return self._definition['dot_life']

    @property
    def spatial_frequency(self) -> Tuple[float]:
        """ Grating spatial frequencies for two gratings -- as tuple (f1, f2) -- in cycles/degree. """
        return self._definition['spatial_frequency']

    @property
    def drift_axis(self) -> Tuple[float]:
        """ Drift axes for two gratings -- as tuple (ax1, ax2) -- in degrees CCW. """
        return self._definition['drift_axis']

    @property
    def spatial_phase(self) -> Tuple[float]:
        """ Initial spatial phase for two gratings -- as tuple (ph1, ph2) -- in degrees. """
        return self._definition['spatial_phase']

    @property
    def sigma(self) -> Tuple[float]:
        """ Standard deviations in (X,Y) for an elliptical Gaussian window, in deg subtended at eye. """
        return self._definition['sigma']

    @property
    def media_folder(self) -> str:
        """ Name of media store folder containing source media file. For movie and image type targets only. """
        return self._definition['media_folder']

    @property
    def media_file(self) -> str:
        """ Name of source media file. For movie and image type targets only. """
        return self._definition['media_file']

    @property
    def flicker_on_dur(self) -> int:
        """ Flicker ON duration in # of video frames (0 = feature disabled). Since data file version 23. """
        return self._definition['flicker_on_dur']

    @property
    def flicker_off_dur(self) -> int:
        """ Flicker ON duration in # of video frames (0 = feature disabled). Since data file version 23. """
        return self._definition['flicker_off_dur']

    @property
    def flicker_delay(self) -> int:
        """ Initial delay prior to first flicker, in # of video frames. Since data file version 23. """
        return self._definition['flicker_delay']


# Trial code-related constants
TC_TARGET_ON = 1
TC_TARGET_OFF = 2
TC_TARGET_HVEL = 3
TC_TARGET_VVEL = 4
TC_TARGET_HPOS_REL = 5
TC_TARGET_VPOS_REL = 6
TC_TARGET_HPOS_ABS = 7
TC_TARGET_VPOS_ABS = 8
TC_ADC_ON = 10
TC_ADC_OFF = 11
TC_FIX1 = 12
TC_FIX2 = 13
TC_FIX_ACCURACY = 14
TC_PULSE_ON = 16
TC_TARGET_HACC = 18
TC_TARGET_VACC = 19
TC_TARGET_PERTURB = 20
TC_TARGET_VEL_STAB_OLD = 21
TC_TARGET_SLOW_HVEL = 27
TC_TARGET_SLOW_VVEL = 28
TC_TARGET_SLOW_HACC = 29
TC_TARGET_SLOW_VACC = 30
TC_DELTA_T = 36
TC_XY_TARGET_USED = 38
TC_INSIDE_HVEL = 39
TC_INSIDE_VVEL = 40
TC_INSIDE_SLOW_HVEL = 41
TC_INSIDE_SLOW_VVEL = 42
TC_INSIDE_HACC = 45
TC_INSIDE_VACC = 46
TC_INSIDE_SLOW_HACC = 47
TC_INSIDE_SLOW_VACC = 48
TC_SPECIAL_OP = 60
TC_REWARD_LEN = 61
TC_PSGM = 62
TC_CHECK_RESP_ON = 63
TC_CHECK_RESP_OFF = 64
TC_FAILSAFE = 65
TC_MID_TRIAL_REW = 66
TC_RPD_WINDOW = 67
TC_TARGET_VEL_STAB = 68
TC_RANDOM_SEED = 97
TC_START_TRIAL = 98
TC_END_TRIAL = 99

TC_STD_SCALE = 10.0  # Multiplier converts float in [-3276.8..3276.7] to 2-byte integer
TC_SLO_SCALE1 = 500.0  # Multiplier converts float in [-65.536 .. 65.535] to 2-byte integer
TC_SLO_SCALE2 = 100.0  # Multiplier converts float in [-327.68 .. 327.67] to 2-byte integer
SPECIAL_OP_SKIP = 1  # The "skip on saccade" special operation

# velocity stabilization flags -- both old (file verion < 8) and current
OPEN_MODE_MASK = (1 << 0)
OPEN_MODE_SNAP = 0
OPEN_MODE_NO_SNAP = 1
OPEN_ENABLE_MASK = (0x03 << 1)
OPEN_ENABLE_H_ONLY = 2
OPEN_ENABLE_V_ONLY = 4
VEL_STAB_ON = (1 << 0)
VEL_STAB_SNAP = (1 << 1)
VEL_STAB_H = (1 << 2)
VEL_STAB_V = (1 << 3)
VEL_STAB_MASK = (VEL_STAB_ON | VEL_STAB_SNAP | VEL_STAB_H | VEL_STAB_V)

PERT_TYPE_SINE = 0
PERT_TYPE_TRAIN = 1
PERT_TYPE_NOISE = 2
PERT_TYPE_GAUSS = 3
PERT_TYPE_LABELS = ['Sinusoid', 'Pulse Train', 'Uniform Noise', 'Gaussian Noise']
PERT_CMPT_H_WIN = 0
PERT_CMPT_V_WIN = 1
PERT_CMPT_H_PAT = 2
PERT_CMPT_V_PAT = 3
PERT_CMPT_DIR_WIN = 4
PERT_CMPT_DIR_PAT = 5
PERT_CMPT_SPEED_WIN = 6
PERT_CMPT_SPEED_PAT = 7
PERT_CMPT_DIR = 8
PERT_CMPT_SPEED = 9
PERT_CMPT_LABELS = ['win_h', 'win_v', 'pat_h', 'pat_v', 'win_dir', 'pat_dir', 'win_speed', 'pat_speed', 'dir', 'speed']
MAX_TRIAL_PERTS = 4

MAX_TRIALTARGS = 25


class TrialCode(NamedTuple):
    code: int
    time: int

    @staticmethod
    def parse_codes(record: bytes) -> List[TrialCode]:
        """
         Parse one or more trial codes from a Maestro data file record (the file could contain more than one trial code
         record, but a trial code never spans across records).

         Args:
             record: A data file record. Record tag ID must be TRIAL_CODE_RECORD.
         Returns:
             List[TrialCode] - List of one or more trial codes culled from the record
         Raises:
             DataFileError if an error occurs while parsing the record
         """
        try:
            if record[0] != TRIAL_CODE_RECORD:
                raise DataFileError("Not a trial code record!")
            raw_fields = struct.unpack_from(f"<{RECORD_SHORTS}h", record, RECORD_TAG_SIZE)
            trial_codes = []
            for i in range(0, len(raw_fields), 2):
                trial_code = TrialCode._make([raw_fields[i], raw_fields[i+1]])
                trial_codes.append(trial_code)
                if trial_code.code == TC_END_TRIAL:
                    break
            return trial_codes
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure parsing trial code record: {str(err)}")


class Point2D:
    def __init__(self, x: Optional[float] = 0.0, y: Optional[float] = 0.0):
        self.x: float = float(x)   # need to make sure these are floats, not ints
        self.y: float = float(y)

    def __str__(self) -> str:
        str_x = "0" if math.isclose(self.x, 0) else f"{self.x:.3f}".rstrip('0').rstrip('.')
        str_y = "0" if math.isclose(self.y, 0) else f"{self.y:.3f}".rstrip('0').rstrip('.')
        return f"({str_x}, {str_y})"

    def as_string_with_wildcard(self, x_wild: bool = False, y_wild: bool = False):
        """
        Display the point's coordinates in string form as "(x, y)", but with the option to replace either or both
        coordinate values with the asterisk character '*'.
        Args:
            x_wild: If true, x-component value is replaced by an '*'. Default = False.
            y_wild: If True, y-component value is replaced by an '*'. Default = False.

        Returns:
            String representation of the 2D coordinate point, as described.
        """
        str_x = "*" if x_wild else ("0" if math.isclose(self.x, 0) else f"{self.x:.3f}".rstrip('0').rstrip('.'))
        str_y = "*" if y_wild else ("0" if math.isclose(self.y, 0) else f"{self.y:.3f}".rstrip('0').rstrip('.'))
        return f"({str_x}, {str_y})"

    def set_point(self, p: Point2D) -> None:
        self.x = p.x
        self.y = p.y

    def set(self, x: float, y: float) -> None:
        self.x = float(x)   # want to make SURE these are floats
        self.y = float(y)

    def set_coords(self, coords: Tuple[float]) -> None:
        self.x = float(coords[0])
        self.y = float(coords[1])

    def offset_by(self, x_ofs: float, y_ofs: float) -> None:
        self.x += x_ofs
        self.y += y_ofs

    def distance_from(self, x_ref: float, y_ref: float) -> float:
        x_ref -= self.x
        y_ref -= self.y
        return math.sqrt(x_ref*x_ref + y_ref*y_ref)

    def is_origin(self) -> bool:
        return math.isclose(self.x, 0) and math.isclose(self.y, 0)


class Perturbation:
    """
    A trial perturbation object, as defined by the TC_TARGET_PERTURB code group in the trial codes that define a trial
    within the Maestro data file.
    """
    def __init__(self, tgt: int, cmpt: int, seg: int, amp: int, pert_type: int, dur: int, extras: List[int]):
        """
        Construct a trial perturbation object.

        Args:
            tgt: Zero-based index of affected target within the trial's participating target list.
            cmpt: Defined constant (PERT_CMPT_***) identifying the target trajectory component affected.
            seg: Zero-based index of trial segment at which the perturbation starts.
            amp: Perturbation amplitude in units of 0.1 deg/sec.
            pert_type: Defined constant (PERT_TYPE_***) identifying the perturbation waveform type.
            dur: The duration of the perturbation in milliseconds.
            extras: A 3-element list of additional parameters, contents of which vary according to perturbation type.
        Raises:
            DataFileError: If arguments do not define a valid trial perturbation. This is not an exhaustive check, but
                it makes sure the target and segment indices are 'reasonable',
        """
        self._params = tuple([tgt, cmpt, seg, amp, pert_type, dur, extras[0], extras[1], extras[2]])
        """ 
        The trial perturbation object as a tuple of integer parameter values in the following order: tgt_pos, 
        component, seg_start, amplitude, type, dur, extras[0], extras[1], extras[2]. 
        """
        if not self._is_valid():
            raise DataFileError("Invalid trial perturbation object")

    def _is_valid(self) -> bool:
        """
        Validity check after reconstructing a trial perturbation from trial codes or from a byte sequence. Not a
        complete validity check.
        """
        # NOTE: Maestro will include trial codes for a perturbation with a target index of -1 (which effectively means
        # the perturbation will have no effect)
        ok = (-1 <= self.tgt_pos < MAX_TRIALTARGS) and (0 <= self.seg_start < MAX_SEGMENTS) and \
             (PERT_CMPT_H_WIN <= self.component <= PERT_CMPT_SPEED) and \
             (PERT_TYPE_SINE <= self.type <= PERT_TYPE_GAUSS) and (self.dur >= 10)
        if ok:
            if self.type == PERT_TYPE_SINE:
                ok = (self.extras[0] >= 10) and (abs(self.extras[1]) <= 18000)
            elif self.type == PERT_TYPE_TRAIN:
                pulse_dur, ramp_dur, pulse_intv = self.extras[0], self.extras[1], self.extras[2]
                ok = (pulse_dur >= 10) and (ramp_dur >= 0) and (pulse_intv >= (pulse_dur + 2 * ramp_dur))
            else:
                ok = (self.extras[0] >= 1) and (abs(self.extras[1]) <= 1000)
        return ok

    def __eq__(self, other: Perturbation) -> bool:
        ok = (self.__class__ == other.__class__) and (self.tgt_pos == other.tgt_pos) and \
             (self.component == other.component) and (self.seg_start == other.seg_start) and \
             (self.type == other.type) and (self.dur == other.dur) and (self.amplitude == other.amplitude) and \
             (len(self.extras) == len(other.extras))
        if ok:
            for i, extra in enumerate(self.extras):
                ok = ok and (extra == other.extras[i])
        return ok

    def __hash__(self) -> int:
        hash_attrs = [self.tgt_pos, self.component, self.seg_start, self.amplitude, self.type, self.dur]
        hash_attrs.extend(self.extras)
        return hash(tuple(hash_attrs))

    def __str__(self) -> str:
        out = f"Segment {self.seg_start}, target {self.tgt_pos}, component={PERT_CMPT_LABELS[self.component]}, " \
              f"type={PERT_TYPE_LABELS[self.type]}: "
        out += f"amplitude={self.amplitude/10.0:.2f} deg/s, dur={self.dur} ms"
        if self.type == PERT_TYPE_SINE:
            out += f", period={self.extras[0]} ms, phase={self.extras[1]/100.0:.2f} deg"
        elif self.type == PERT_TYPE_TRAIN:
            out += f", pulse={self.extras[0]} ms, ramp={self.extras[1]} ms, interval={self.extras[2]} ms"
        else:
            out += f" , noise update intv={self.extras[0]} ms, mean={self.extras[1]/1000.0:.2f}, " \
                  f"seed={self.extras[2]}"
        return out

    @property
    def tgt_pos(self) -> int:
        """ Zero-based index of affected target in the trial's participating target list. """
        return self._params[0]

    @property
    def component(self) -> int:
        """ Defined constant (PERT_CMPT_***) identifying the target trajectory component affected. """
        return self._params[1]

    @property
    def seg_start(self) -> int:
        """ Zero-based index of the trial segment at which the perturbation begins. """
        return self._params[2]

    @property
    def amplitude(self) -> int:
        """ The perturbation amplitude in units of 0.1 deg/sec. """
        return self._params[3]

    @property
    def type(self) -> int:
        """ Defined constant (PERT_TYPE_***) identifying the perturbation waveform type. """
        return self._params[4]

    @property
    def dur(self) -> int:
        """ The duration of the perturbation, in milliseconds. """
        return self._params[5]

    @property
    def extras(self) -> Tuple[int]:
        """
        A 3-tuple of additional parameters, the contents of which vary according to the perturbation type:
            - PERT_TYPE_SINE: (period in ms, phase in 0.01 deg, N/A)
            - PERT_TYPE_TRAIN: (pulse duration in ms, ramp duration in ms, pulse interval in ms)
            - PERT_TYPE_NOISE, _GAUSS: (noise update interval in ms, noise mean * 1000, noise seed)
        """
        return self._params[6:]

    _struct_format = "<9i"
    """ Format string for converting a trial perturbation object to/from a raw byte sequence. """

    def to_bytes(self) -> bytes:
        """
        Prepare a byte sequence encoding this Maestro trial perturbation object. To reconstruct the perturbation, pass
        this byte sequence to from_bytes().

        Returns:
            The byte sequence encoding this Maestro trial perturbation object.
        """

        return struct.pack(Perturbation._struct_format, *self._params)

    @staticmethod
    def size_in_bytes() -> int:
        """ Length of the byte sequence encoding a Perturbation instance, as generated by to_bytes(). """
        return struct.calcsize(Perturbation._struct_format)

    @staticmethod
    def from_bytes(raw: bytes) -> Perturbation:
        """
        Reconstruct a Maestro trial perturbation object from the byte sequence encoding it, as supplied by to_bytes().

        Args:
            raw: The byte sequence.
        Returns:
            The trial perturbation.
        Raises:
            DataFileError: If the byte sequence does not encode a valid Maestro trial perturbation.
        """
        try:
            raw_fields = struct.unpack(Perturbation._struct_format, raw)
            return Perturbation(raw_fields[0], raw_fields[1], raw_fields[2], raw_fields[3], raw_fields[4],
                                raw_fields[5], list(raw_fields[6:]))
        except DataFileError:
            raise
        except Exception as e:
            raise DataFileError(f"Unexpected error while reconstructing trial pertubation from byte sequence: {str(e)}")

    @staticmethod
    def from_trial_codes(codes: List[TrialCode], start: int, seg_idx: int) -> Optional[Perturbation]:
        """
        Reconstruct a Maestro trial perturbation from the original definiing trial code sequence (TC_TARGET_PERTURB
        code group) extracted from a Maestro data file record.

        Args:
            codes: A list of trial codes.
            start: The code index at which the TC_TARGET_PERTURB code group starts.
            seg_idx: The index of the trial segment at which the perturbation begins.

        Returns:
            The trial perturbation object, or None if reconstruction fails for whatever reason.
        """
        if (start < 0) or ((start + 5) > len(codes)) or (codes[start].code != TC_TARGET_PERTURB):
            return None
        try:
            tgt = codes[start+1].code
            cmpt = (codes[start+1].time >> 4) & 0x0F
            amp = codes[start+2].code
            pert_type = (codes[start+1].time & 0x0F)
            if not (PERT_TYPE_SINE <= pert_type <= PERT_TYPE_GAUSS):
                return None
            dur = codes[start+2].time
            extras = [codes[start+3].code, codes[start+3].time]
            if pert_type == PERT_TYPE_TRAIN:
                extras.append(codes[start+4].code)
            elif pert_type in [PERT_TYPE_NOISE, PERT_TYPE_GAUSS]:
                extras.append((codes[start+4].time << 8) | (codes[start+4].code & 0x0FF))
            return Perturbation(tgt, cmpt, seg_idx, amp, pert_type, dur, extras)
        except Exception:
            return None


_TGT_ON_FLAG: int = (1 << 8)
_TGT_REL_FLAG: int = (1 << 9)
SEG_MAX_MARKER: int = 10


class Segment:
    """ A 'readonly' representation of a single segment with the segment table of a Maestro trial. """
    def __init__(self, num_targets: int, dur: int, pulse_ch: int, fix1: int, fix2: int, grace: int, xy_update_intv: int,
                 fixacc_h: float, fixacc_v: float, tgt_flags: Tuple[int], tgt_pos: Tuple[float],
                 tgt_vel: Tuple[float], tgt_acc: Tuple[float], tgt_pat_vel: Tuple[float],
                 tgt_pat_acc: Tuple[float]):
        self._definition: Dict[str, Any] = dict()
        self._definition['dur'] = dur
        self._definition['pulse_ch'] = pulse_ch
        self._definition['fix1'] = fix1
        self._definition['fix2'] = fix2
        self._definition['grace'] = grace
        self._definition['xy_update_intv'] = xy_update_intv
        self._definition['fixacc_h'] = fixacc_h
        self._definition['fixacc_v'] = fixacc_v
        self._definition['tgt_flags'] = tgt_flags
        self._definition['tgt_pos'] = tgt_pos
        self._definition['tgt_vel'] = tgt_vel
        self._definition['tgt_acc'] = tgt_acc
        self._definition['tgt_pat_vel'] = tgt_pat_vel
        self._definition['tgt_pat_acc'] = tgt_pat_acc
        self._validity_check(num_targets)

    def _validity_check(self, num_targets: int) -> None:
        if num_targets < 1 or num_targets > MAX_TRIALTARGS:
            raise DataFileError(f"Invalid number of trial targets in segment ({num_targets})")
        traj_keys = ['tgt_pos', 'tgt_vel', 'tgt_acc', 'tgt_pat_vel', 'tgt_pat_acc']
        ok = len(self._definition['tgt_flags']) == num_targets and \
            all([len(self._definition[k]) == 2*num_targets for k in traj_keys])
        if not ok:
            raise DataFileError(f"Target trajectory parameters in segment not consistent with number of targets")
        if self.dur <= 0:
            raise DataFileError("Non-positive duration for trial segment")
        if not (self.pulse_ch <= SEG_MAX_MARKER):
            raise DataFileError(f"Invalid marker pulse channel ({self.pulse_ch}) for trial segment")
        if not (-1 <= self.fix1 < num_targets):
            raise DataFileError(f"Invalid target index for fixation target #1 ({self.fix1})")
        if not (-1 <= self.fix2 < num_targets):
            raise DataFileError(f"Invalid target index for fixation target #2 ({self.fix2})")

    def check_equality_test(self, other: Segment) -> bool:
        match = (self.num_targets == other.num_targets) and (self.dur == other.dur) and \
                (self.pulse_ch == other.pulse_ch) and (self.fix1 == other.fix1) and (self.fix2 == other.fix2) and \
                (self.grace == other.grace) and (self.xy_update_intv == other.xy_update_intv) and \
                (self.fixacc_h == other.fixacc_h) and (self.fixacc_v == other.fixacc_v)
        if match:
            for idx in range(self.num_targets):
                match = (self.tgt_on(idx) == other.tgt_on(idx)) and (self.tgt_rel(idx) == other.tgt_rel(idx)) and \
                    (self.tgt_vel_stab_mask(idx) == other.tgt_vel_stab_mask(idx)) and \
                    (self.tgt_pos(idx) == other.tgt_pos(idx)) and (self.tgt_vel(idx) == other.tgt_vel(idx))
                match = match and (self.tgt_acc(idx) == other.tgt_acc(idx)) and \
                    (self.tgt_pat_vel(idx) == other.tgt_pat_vel(idx)) and \
                    (self.tgt_pat_acc(idx) == other.tgt_pat_acc(idx))
                if not match:
                    return False
        return match

    def to_bytes(self) -> bytes:
        """
        Prepare a byte sequence encoding this Maestro trial segment. To reconstruct the segment, pass this byte sequence
        to from_bytes().

        Returns:
            The byte sequence encoding this Maestro trial segment.
        """
        n_tgts = self.num_targets
        raw = struct.pack(f"<7i2f{n_tgts}i{5*2*n_tgts}f", n_tgts, self.dur, self.pulse_ch, self.fix1, self.fix2,
                          self.grace, self.xy_update_intv, self.fixacc_h, self.fixacc_v, *self._definition['tgt_flags'],
                          *self._definition['tgt_pos'], *self._definition['tgt_vel'], *self._definition['tgt_acc'],
                          *self._definition['tgt_pat_vel'], *self._definition['tgt_pat_acc'])
        return raw

    @staticmethod
    def size_in_bytes(num_targets: int) -> int:
        """
        Length of the byte sequence encoding a Segment instance, as generated by to_bytes().

        Args:
            num_targets: The number of targets participating in the trial.
        """
        return struct.calcsize(f"<7i2f{num_targets}i{5*2*num_targets}f")

    @staticmethod
    def from_bytes(raw: bytes, offset: int) -> Segment:
        """
        Reconstruct a Maestro trial segment object from a raw byte sequence generated by to_bytes().

        Args:
            raw: The source byte buffer.
            offset: Offset into buffer at which the byte sequence defining the segment begins.
        Returns:
            The reconstructed trial segment objec
        Raises:
            DataFileError: If any error occurs while parsing the byte sequence.
        """
        try:
            num_tgts = struct.unpack_from("<i", raw, offset)[0]   # need num targets in order to parse the rest!
            fields = struct.unpack_from(f"<7i2f{num_tgts}i{5*2*num_tgts}f", raw, offset)
            ofs = 9
            flags = fields[ofs:ofs + num_tgts]
            ofs += num_tgts
            pos = fields[ofs:ofs + 2 * num_tgts]
            ofs += 2 * num_tgts
            vel = fields[ofs:ofs + 2 * num_tgts]
            ofs += 2 * num_tgts
            acc = fields[ofs:ofs + 2 * num_tgts]
            ofs += 2 * num_tgts
            pat_vel = fields[ofs:ofs + 2 * num_tgts]
            ofs += 2 * num_tgts
            pat_acc = fields[ofs:ofs + 2 * num_tgts]

            # noinspection PyTypeChecker
            segment = Segment(num_targets=num_tgts, dur=fields[1], pulse_ch=fields[2], fix1=fields[3], fix2=fields[4],
                              grace=fields[5], xy_update_intv=fields[6], fixacc_h=fields[7], fixacc_v=fields[8],
                              tgt_flags=flags, tgt_pos=pos, tgt_vel=vel, tgt_acc=acc, tgt_pat_vel=pat_vel,
                              tgt_pat_acc=pat_acc)
            return segment
        except DataFileError:
            raise
        except Exception as e:
            raise DataFileError(f"Unexpected error while deserializing trial segment from byte sequence: {str(e)}")

    @property
    def num_targets(self) -> int:
        """ The number of targets participating in the trial. The segment holds trajectory parameters for each. """
        return len(self._definition['tgt_flags'])

    @property
    def dur(self) -> int:
        """ The segment duration in milliseconds. """
        return self._definition['dur']

    @property
    def pulse_ch(self) -> int:
        """
        Digital output channel number for marker pulse delivered at segment start, between 1 and 10; otherwise, no
        marker pulse is delivered.
        """
        return self._definition['pulse_ch']

    @property
    def fix1(self) -> int:
        """ Index of first fixation target for segment (-1 if none). """
        return self._definition['fix1']

    @property
    def fix2(self) -> int:
        """ Index of second fixation target for segment (-1 if none). """
        return self._definition['fix2']

    @property
    def grace(self) -> int:
        """ Grace period during which fixation is not enforced, in ms (0 = no grace period). """
        return self._definition['grace']

    @property
    def xy_update_intv(self) -> int:
        """ XYScope update interval for segment, in milliseconds. """
        return self._definition['xy_update_intv']

    @property
    def fixacc_h(self) -> float:
        """ Horizontal fixation accuracy during segment in visual deg (if fixation enforced during segment). """
        return self._definition['fixacc_h']

    @property
    def fixacc_v(self) -> float:
        """ Vertical fixation accuracy during segment in visual deg (if fixation enforced during segment). """
        return self._definition['fixacc_v']

    def tgt_on(self, idx: int) -> bool:
        """
        Is trial target ON during this segment?

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            True if target is ON during the segment; False if it is turned off.
        Raises:
            IndexError: If target index is invalid.
        """
        flags: int = self._definition['tgt_flags'][idx]
        return (flags & _TGT_ON_FLAG) != 0

    def tgt_rel(self, idx: int) -> bool:
        """
        Is specified target's instantaneous position change at the start of this segment relative to its position at
        the end of the previous segment?

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            True if target position change is relative to position at end of previous segment; False if the target
                is repositioned absolutely at the start of this segment.
        Raises:
            IndexError: If target index is invalid.
        """
        flags: int = self._definition['tgt_flags'][idx]
        return (flags & _TGT_REL_FLAG) != 0

    def tgt_vel_stab_mask(self, idx: int) -> int:
        """
        A target's velocity stabilization state during this segment.

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            The velocity stabilization state for the target. See VEL_STAB_*** mask bits for meaning.
        Raises:
            IndexError: If target index is invalid.
        """
        flags: int = self._definition['tgt_flags'][idx]
        return flags & VEL_STAB_MASK

    def tgt_vel_stab_as_string(self, idx: int) -> str:
        """
        A target's velocity stabilization state during this segment, in string form.

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            The velocity stabilization state in string form: "OFF", "H", "V", or "H+V". For the latter 3 possibilities,
                "w/SNAP" is appended if the eye snaps to the target at the start of the segment.
        Raises:
            IndexError: If target index is invalid.
        """
        vstab = self.tgt_vel_stab_mask(idx)
        if vstab == 0:
            return "OFF"
        snap = " w/SNAP" if ((vstab & VEL_STAB_SNAP) != 0) else ""
        is_h = (vstab & VEL_STAB_H) != 0
        is_v = (vstab & VEL_STAB_V) != 0
        return f"H+V{snap}" if (is_h and is_v) else (f"H{snap}" if is_h else f"V{snap}")

    def tgt_pos(self, idx: int) -> Tuple[float]:
        """
        A target's instantaneous position change horizontally and vertically at segment start. The change is either
        absolute, or relative to its position at the end of the previous segment -- see tgt_rel().

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            A 2-tuple containing the target's position change (H, V) in degrees.
        Raises:
            IndexError: If target index is invalid.
        """
        coords: Tuple[float] = self._definition['tgt_pos']
        return tuple(coords[2*idx:2*idx+2])

    def tgt_vel(self, idx: int) -> Tuple[float]:
        """
        A target's constant horizontal and vertical velocity during segment.

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            A 2-tuple containing the target's velocity (H, V) in deg/sec.
        Raises:
            IndexError: If target index is invalid.
        """
        coords: Tuple[float] = self._definition['tgt_vel']
        return tuple(coords[2*idx:2*idx+2])

    def tgt_acc(self, idx: int) -> Tuple[float]:
        """
        A target's constant horizontal and vertical acceleration during segment.

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            A 2-tuple containing the target's acceleration (H, V) in deg/sec^2.
        Raises:
            IndexError: If target index is invalid.
        """
        coords: Tuple[float] = self._definition['tgt_acc']
        return tuple(coords[2*idx:2*idx+2])

    def tgt_pat_vel(self, idx: int) -> Tuple[float]:
        """
        A target's constant horizontal and vertical pattern velocity during segment.

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            A 2-tuple containing the target's pattern velocity (H, V) in deg/sec.
        Raises:
            IndexError: If target index is invalid.
        """
        coords: Tuple[float] = self._definition['tgt_pat_vel']
        return tuple(coords[2*idx:2*idx+2])

    def tgt_pat_acc(self, idx: int) -> Tuple[float]:
        """
        A target's constant horizontal and vertical pattern acceleration during segment.

        Args:
            idx: The (zero-based) index of target in the trial's participating target list.
        Returns:
            A 2-tuple containing the target's pattern acceleration (H, V) in deg/sec^2.
        Raises:
            IndexError: If target index is invalid.
        """
        coords: Tuple[float] = self._definition['tgt_pat_acc']
        return tuple(coords[2*idx:2*idx+2])

    def value_of(self, param_type: SegParamType, idx: int) -> Union[bool, int, float]:
        """
        Retrieve the value of the specified segment header or target trajectory parameter.

        Args:
            param_type: The parameter type.
            idx: For a target trajectory parameter, this is the target index. Otherwise ignored.
        Returns:
            The parameter value -- a boolean, integer or float, depending on the parameter type.
        Raises:
            IndexError: If a trajectory parameter is requested but the target index is invalid.
        """
        if param_type.is_target_trajectory_parameter() and not (0 <= idx < self.num_targets):
            raise IndexError("Invalid target index")
        elif param_type == SegParamType.DURATION:
            return self.dur
        elif param_type == SegParamType.MARKER:
            return self.pulse_ch
        elif param_type == SegParamType.FIX_TGT1:
            return self.fix1
        elif param_type == SegParamType.FIX_TGT2:
            return self.fix2
        elif param_type == SegParamType.FIXACC_H:
            return self.fixacc_h
        elif param_type == SegParamType.FIXACC_V:
            return self.fixacc_v
        elif param_type == SegParamType.GRACE_PER:
            return self.grace
        elif param_type == SegParamType.XY_UPDATE_INTV:
            return self.xy_update_intv
        elif param_type == SegParamType.TGT_ON_OFF:
            return self.tgt_on(idx)
        elif param_type == SegParamType.TGT_REL:
            return self.tgt_rel(idx)
        elif param_type == SegParamType.TGT_VSTAB:
            return self.tgt_vel_stab_mask(idx)
        elif param_type == SegParamType.TGT_POS_H:
            return self.tgt_pos(idx)[0]
        elif param_type == SegParamType.TGT_POS_V:
            return self.tgt_pos(idx)[1]
        elif param_type == SegParamType.TGT_VEL_H:
            return self.tgt_vel(idx)[0]
        elif param_type == SegParamType.TGT_VEL_V:
            return self.tgt_vel(idx)[1]
        elif param_type == SegParamType.TGT_ACC_H:
            return self.tgt_acc(idx)[0]
        elif param_type == SegParamType.TGT_ACC_V:
            return self.tgt_acc(idx)[1]
        elif param_type == SegParamType.TGT_PAT_VEL_H:
            return self.tgt_pat_vel(idx)[0]
        elif param_type == SegParamType.TGT_PAT_VEL_V:
            return self.tgt_pat_vel(idx)[1]
        elif param_type == SegParamType.TGT_PAT_ACC_H:
            return self.tgt_pat_acc(idx)[0]
        else:  # SegParamType.TGT_PAT_ACC_V
            return self.tgt_pat_acc(idx)[1]


class Trial:
    """
    Definition of a particular instance of a Maestro trial, as culled from the trial codes and other information in a
    Maestro data file. It is intended as a pseudo-immutable representation of the trial definition, providing read-only
    access to the trial's segment table, participating target list, any perturbations or tagged sections, and other
    trial properties.

    Do not use the constructor directly to create a `Trial` instance; instead, use the static methods:
        - `prepare_trial()`: To recreate the trial from the defining trial codes, participating target list, and any
          tagged sections culled from the original Maestro data file, along with the file's header record.
        - `from_bytes()`: To recreate the trial from an encoded byte sequence prepared by `to_bytes()`.
    """
    _PARAM_DICT: Dict[str, type] = {
        'name': str, 'set_name': (str, type(None)), 'subset_name': (str, type(None)), 'segments': tuple,
        'targets': tuple, 'perts': tuple, 'sections': tuple, 'record_seg': int, 'skip_seg': int, 'file_version': int,
        'xy_seed': int, 'global_transform': TargetTransform
    }
    """ Maps trial definition dictionary keys to their value types. """

    def __init__(self, name: str, set_name: Optional[str], subset_name: Optional[str], segments: Tuple[Segment],
                 targets: Tuple[Target], perts: Tuple[Perturbation], sections: Tuple[TaggedSection], record_seg: int,
                 skip_seg: int, file_version: int, xy_seed: int, global_transform: TargetTransform):
        """
        Construct the Maestro trial defnition. **This constructor is intended for internal use only.**

        Raises:
            DataFileError: If the definition is invalid (a sanity check rather than an exhaustive check).
        """
        self._definition: Dict[str, Any] = dict()
        """ The trial definition as a dictionary of parameter values keyed by parameter names. """
        self._definition['name'] = name
        self._definition['set_name'] = set_name
        self._definition['subset_name'] = subset_name
        self._definition['segments'] = segments
        self._definition['targets'] = targets
        self._definition['perts'] = perts
        self._definition['sections'] = sections
        self._definition['record_seg'] = record_seg
        self._definition['skip_seg'] = skip_seg
        self._definition['file_version'] = file_version
        self._definition['xy_seed'] = xy_seed
        self._definition['global_transform'] = global_transform
        self._validity_check()

    def _validity_check(self):
        if not ((0 < self.num_segments <= MAX_SEGMENTS) and (0 < self.num_targets <= MAX_TRIALTARGS)):
            raise DataFileError(f'Invalid number of trial targets or segments.')
        if not all([isinstance(p, Perturbation) for p in self._definition['perts']]):
            raise DataFileError('Invalid trial perturbations table')
        if not all([isinstance(s, TaggedSection) for s in self._definition['sections']]):
            raise DataFileError('Invalid trial tagged sections list')
        ok = all([isinstance(tgt, Target) for tgt in self._definition['targets']]) and \
            all([isinstance(seg, Segment) for seg in self._definition['segments']]) and \
            all([(self.num_targets == seg.num_targets) for seg in self._definition['segments']])
        if not ok:
            raise DataFileError('Invalid trial segments table')
        if not TaggedSection.validate_tagged_sections(self._definition['sections'], self.num_segments):
            raise DataFileError("Detected invalid or overlapping tagged sections in trial definition")
        # ensure any target perturbations identify valid targets in the list of participating trial targets. However,
        # a target index of -1 indicates the perturbation is disabled.
        pert: Perturbation
        for pert in self._definition['perts']:
            if (pert.tgt_pos < -1) or (pert.tgt_pos >= self.num_targets):
                raise DataFileError("Detected invalid perturbation target index in trial definition")

    @property
    def name(self) -> str:
        """ The trial's name. """
        return self._definition['name']

    @property
    def set_name(self) -> Optional[str]:
        """ Name of the set to which trial belongs, or None if not available (added to data file in version 21). """
        return self._definition['set_name']

    @property
    def subset_name(self) -> Optional[str]:
        """
        Name of the subset to which trial belongs (empty string if there is no subset), or None if not  available (added
        to the data file in version 21).
        """
        return self._definition['subset_name']

    @property
    def num_segments(self) -> int:
        """ Number of segments in the trial's segment table. """
        return len(self._definition['segments'])

    @property
    def segments(self) -> Tuple[Segment]:
        """ The trial's segments, in chronological order. """
        return self._definition['segments']

    @property
    def num_targets(self) -> int:
        """ Number of targets in the trial's participating target list. """
        return len(self._definition['targets'])

    @property
    def targets(self) -> Tuple[Target]:
        """ The trial's participating targets (in the order they are listed in the trial's segment table). """
        return self._definition['targets']

    @property
    def num_perturbations(self) -> int:
        """ Number of velocity perturbations defined on the trial (may be 0). """
        return len(self._definition['perts'])

    @property
    def perturbations(self) -> Tuple[Perturbation]:
        """ Perturbations defined on the trial (if any). """
        return self._definition['perts']

    @property
    def num_tagged_sections(self) -> int:
        """ Number of tagged sections defined on the trial (may be 0). """
        return len(self._definition['sections'])

    @property
    def tagged_sections(self) -> Tuple[TaggedSection]:
        """ Tagged sections defined on the trial (if any). """
        return self._definition['sections']

    @property
    def record_seg(self) -> int:
        """ Zero-based index of trial segment when recording started. """
        return self._definition['record_seg']

    @property
    def skip_seg(self) -> int:
        """ Zero-based index of trial segment for 'skip on saccade' special feature; -1 if feature not enabled. """
        return self._definition['skip_seg']

    @property
    def file_version(self) -> int:
        """ The file version number of the Maestro data file from which this trial was extracted. """
        return self._definition['file_version']

    @property
    def xy_seed(self) -> int:
        """ The random seed for the XYScope controller, as extracted from the original Maestro data file header. """
        return self._definition['xy_seed']

    @property
    def global_transform(self) -> TargetTransform:
        """ The global target transform in effect when this trial was presented. """
        return self._definition['global_transform']

    class _Seg:
        """
        A single segment within the segment table of a Maestro trial. This mutable version of a trial segment is only
        used while reconstructing a trial's definition from the trial code sequence extracted from a Maestro data file.
        """

        def __init__(self, num_targets: int, prev_seg: Optional[Trial._Seg] = None):
            self.dur: int = 0
            """ The segment duration in milliseconds. """
            self.pulse_ch: int = -1
            """ Digital output channel number for marker pulse delivered at segment start (-1 if no marker pulse). """
            self.fix1: int = -1
            self.fix2: int = -1
            self.fixacc_h: float = 0.0
            """ Horizontal fixation accuracy during segment in visual deg (if fixation enforced during segment). """
            self.fixacc_v: float = 0.0
            """ Vertical fixation accuracy during segment in visual deg (if fixation enforced during segment). """
            self.grace: int = 0
            """ Grace period during which fixation is not enforced, in ms (0 = no grace period). """
            self.xy_update_intv: int = 4
            """ XYScope update interval for segment, in milliseconds. """
            self.tgt_on: List[bool] = [False for _ in range(num_targets)]
            """ Per-target on/off state during segment. """
            self.tgt_rel: List[bool] = [True for _ in range(num_targets)]
            """ Is per-target position change relative (or absolute) at segment start? """
            self.tgt_vel_stab_mask: List[int] = [0 for _ in range(num_targets)]
            """ Per-target velocity stabilization mask for segment """
            self.tgt_pos: List[Point2D] = [Point2D(0, 0) for _ in range(num_targets)]
            """ Per-target instantaneous position change (H,V) at segment start, in degrees. """
            self.tgt_vel: List[Point2D] = [Point2D(0, 0) for _ in range(num_targets)]
            """ Per-target velocity (H,V) during segment, in deg/sec. """
            self.tgt_acc: List[Point2D] = [Point2D(0, 0) for _ in range(num_targets)]
            """ Per-target acceleration (H,V) during segment, in deg/sec^2. """
            self.tgt_pat_vel: List[Point2D] = [Point2D(0, 0) for _ in range(num_targets)]
            """ Per-target pattern velocity (H,V) during segment, in deg/sec. """
            self.tgt_pat_acc: List[Point2D] = [Point2D(0, 0) for _ in range(num_targets)]
            """ Per-target pattern acceleration (H,V) during segment, in deg/sec^2. """

            # new segment inherits trajectory parameters from previous segment -- except instantaneous position change
            if (prev_seg is not None) and (prev_seg.num_targets() == num_targets):
                self.fix1 = prev_seg.fix1
                """ Index of first fixation target for segment (-1 if none). """
                self.fix2 = prev_seg.fix2
                """ Index of second fixation target for segment (-1 if none) """
                for i in range(num_targets):
                    self.tgt_on[i] = prev_seg.tgt_on[i]
                    self.tgt_vel_stab_mask[i] = prev_seg.tgt_vel_stab_mask[i]
                    self.tgt_pos[i].set(0, 0)
                    self.tgt_vel[i].set_point(prev_seg.tgt_vel[i])
                    self.tgt_acc[i].set_point(prev_seg.tgt_acc[i])
                    self.tgt_pat_vel[i].set_point(prev_seg.tgt_pat_vel[i])
                    self.tgt_pat_acc[i].set_point(prev_seg.tgt_pat_acc[i])

        def num_targets(self) -> int:
            """ The number of targets participating in the trial. """
            return len(self.tgt_on)

    @staticmethod
    def prepare_trial(codes: List[TrialCode], header: DataFileHeader, targets: List[Target],
                      sections: Optional[List[TaggedSection]]) -> Trial:
        """
        Reconstruct the definition of a Maestro trial from the trial codes, targets, tagged sections, and file header
        culled from a Maestro data file. NOTE: We DO NOT invert the trial trajectory parameters IAW the global target
        transform found in the file header. Because the "similarity test" for two trial instances now requires that
        they have the same transform, there is no need to do so.

        Args:
            codes: The trial codes culled from the Maestro data file.
            header: The data file header.
            targets: The participating trial target list, as culled from the data file.
            sections: List of tagged sections, as culled from the data file, or None if no sections are defined.
        Returns:
            The reconstructed trial definition.
        Raises:
            DataFileError: If unable to reconstruct the trial definition for any reason.
        """
        segments: List[Trial._Seg] = []
        perturbations: List[Perturbation] = []
        record_seg_idx: int = -1
        skip_seg_idx: int = -1
        curr_segment: Optional[Trial._Seg] = None
        curr_tick: int = 0
        code_idx: int = 0
        seg_start_time: int = 0
        done: bool = False
        pre_v8_open_seg: int = -1
        pre_v8_open_mask: int = 0
        pre_v8_num_open_segs = 0

        try:
            tc = codes[code_idx]
            while not done:
                # peek at next trial code. Append a trial segment at each segment boundary. Note that certain trial
                # codes NEVER start a segment
                if (tc.time == curr_tick) and (tc.code != TC_END_TRIAL) and (tc.code != TC_FIX_ACCURACY):
                    if len(segments) == MAX_SEGMENTS:
                        raise DataFileError("Too many segments found in trial while processing trial codes!")
                    prev_segment = curr_segment
                    if prev_segment is not None:
                        prev_segment.dur = curr_tick - seg_start_time
                    curr_segment = Trial._Seg(len(targets), prev_segment)
                    segments.append(curr_segment)
                    seg_start_time = curr_tick

                # process all trial codes for the current trial "tick".
                while tc.time <= curr_tick and (not done):
                    if tc.code in [TC_TARGET_ON, TC_TARGET_OFF]:
                        # turn a selected target on/off (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        curr_segment.tgt_on[tgt_idx] = (tc.code == TC_TARGET_ON)
                        code_idx += 2
                    elif tc.code in [TC_TARGET_HVEL, TC_TARGET_SLOW_HVEL]:
                        # change a selected target's horizontal velocity (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = TC_STD_SCALE if tc.code == TC_TARGET_HVEL else TC_SLO_SCALE1
                        curr_segment.tgt_vel[tgt_idx].x = float(codes[code_idx + 1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_TARGET_VVEL, TC_TARGET_SLOW_VVEL]:
                        # change a selected target's vertical velocity (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = TC_STD_SCALE if tc.code == TC_TARGET_VVEL else TC_SLO_SCALE1
                        curr_segment.tgt_vel[tgt_idx].y = float(codes[code_idx+1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_INSIDE_HVEL, TC_INSIDE_SLOW_HVEL]:
                        # change a selected target's horizontal pattern velocity (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = TC_STD_SCALE if tc.code == TC_INSIDE_HVEL else TC_SLO_SCALE1
                        curr_segment.tgt_pat_vel[tgt_idx].x = float(codes[code_idx + 1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_INSIDE_VVEL, TC_INSIDE_SLOW_VVEL]:
                        # change a selected target's vertical pattern velocity (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = TC_STD_SCALE if tc.code == TC_INSIDE_VVEL else TC_SLO_SCALE1
                        curr_segment.tgt_pat_vel[tgt_idx].y = float(codes[code_idx+1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_INSIDE_HACC, TC_INSIDE_SLOW_HACC]:
                        # change a selected target's horizontal pattern acceleration (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = 1.0 if tc.code == TC_INSIDE_HACC else TC_SLO_SCALE2
                        curr_segment.tgt_pat_acc[tgt_idx].x = float(codes[code_idx + 1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_INSIDE_VACC, TC_INSIDE_SLOW_VACC]:
                        # change a selected target's vertical pattern acceleration (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = 1.0 if tc.code == TC_INSIDE_VACC else TC_SLO_SCALE2
                        curr_segment.tgt_pat_acc[tgt_idx].y = float(codes[code_idx + 1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_TARGET_HPOS_REL, TC_TARGET_HPOS_ABS]:
                        # relative or absolute change in selected target's horizontal position (N=2). NOTE that scale
                        # factor changed in file version 2, but we don't support v<2.
                        tgt_idx = codes[code_idx + 1].code
                        curr_segment.tgt_pos[tgt_idx].x = float(codes[code_idx + 1].time) / TC_SLO_SCALE2
                        curr_segment.tgt_rel[tgt_idx] = (tc.code == TC_TARGET_HPOS_REL)
                        code_idx += 2
                    elif tc.code in [TC_TARGET_VPOS_REL, TC_TARGET_VPOS_ABS]:
                        # relative or absolute change in selected target's vertical position (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        curr_segment.tgt_pos[tgt_idx].y = float(codes[code_idx + 1].time) / TC_SLO_SCALE2
                        curr_segment.tgt_rel[tgt_idx] = (tc.code == TC_TARGET_VPOS_REL)
                        code_idx += 2
                    elif tc.code in [TC_TARGET_HACC, TC_TARGET_SLOW_HACC]:
                        # change a selected target's horizontal acceleration (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = 1.0 if tc.code == TC_TARGET_HACC else TC_SLO_SCALE2
                        curr_segment.tgt_acc[tgt_idx].x = float(codes[code_idx + 1].time) / scale
                        code_idx += 2
                    elif tc.code in [TC_TARGET_VACC, TC_TARGET_SLOW_VACC]:
                        # change a selected target's vertical acceleration (N=2)
                        tgt_idx = codes[code_idx + 1].code
                        scale = 1.0 if tc.code == TC_TARGET_VACC else TC_SLO_SCALE2
                        curr_segment.tgt_acc[tgt_idx].y = float(codes[code_idx + 1].time) / scale
                        code_idx += 2
                    elif tc.code == TC_TARGET_PERTURB:
                        # handle target velocity perturbation (N=5)
                        if header.version < 5:
                            raise DataFileError("No support for pre-version 5 trials with perturbations.")
                        pert = Perturbation.from_trial_codes(codes, code_idx, len(segments)-1)
                        if pert is None:
                            raise DataFileError("Failed to parse trial code group defining velocity perturbation!")
                        elif len(perturbations) < MAX_TRIAL_PERTS:
                            perturbations.append(pert)
                        else:
                            raise DataFileError("Too many velocity perturbations defined on trial!")
                        code_idx += 5
                    elif tc.code == TC_TARGET_VEL_STAB_OLD:
                        # initialize velocity stabilization for a single target (file version < 8). Save information so
                        # we can configure the target's velocity stabilization mask after processing codes (N=2)
                        if (pre_v8_open_seg < 0) and (header.version < 8):
                            pre_v8_open_seg = len(segments) - 1
                            pre_v8_open_mask = codes[code_idx+1].time
                            pre_v8_num_open_segs = codes[code_idx+1].code if header.version == 7 else 1
                        code_idx += 2
                    elif tc.code == TC_TARGET_VEL_STAB:
                        # velocity stabilization state of a selected target has changed (file version >= 8, N=2)
                        tgt_idx = codes[code_idx+1].code
                        curr_segment.tgt_vel_stab_mask[tgt_idx] = codes[code_idx+1].time
                        code_idx += 2
                    elif tc.code == TC_DELTA_T:
                        # set XY scope frame update interval for current segment (N=2)
                        curr_segment.xy_update_intv = codes[code_idx+1].code
                        code_idx += 2
                    elif tc.code == TC_SPECIAL_OP:
                        # remember "skip on saccade" segment -- cannot compute target trajectories in this case! (N=2)
                        if codes[code_idx+1].code == SPECIAL_OP_SKIP:
                            skip_seg_idx = len(segments) - 1
                        code_idx += 2
                    elif tc.code == TC_ADC_ON:
                        # start recording data (N=1). Recording continues until trial's end.
                        if record_seg_idx < 0:
                            record_seg_idx = len(segments) - 1
                        code_idx += 1
                    elif tc.code == TC_FIX1:
                        # select/deselect a target as fixation target #1 (N=2)
                        curr_segment.fix1 = codes[code_idx+1].code
                        code_idx += 2
                    elif tc.code == TC_FIX2:
                        # select/deselect a target as fixation target #2 (N=2)
                        curr_segment.fix2 = codes[code_idx+1].code
                        code_idx += 2
                    elif tc.code == TC_FIX_ACCURACY:
                        # set H, V fixation accuracy and possibly grace period
                        curr_segment.fixacc_h = float(codes[code_idx + 1].code) / TC_SLO_SCALE2
                        curr_segment.fixacc_v = float(codes[code_idx + 1].time) / TC_SLO_SCALE2
                        if tc.time > seg_start_time:
                            curr_segment.grace = tc.time - seg_start_time
                        code_idx += 2
                    elif tc.code == TC_PULSE_ON:
                        # at segment start, deliver marker pulse on specified DO channel (N=2
                        curr_segment.pulse_ch = codes[code_idx+1].code
                        code_idx += 2
                    elif tc.code in [TC_ADC_OFF, TC_CHECK_RESP_OFF, TC_FAILSAFE, TC_START_TRIAL]:
                        # N=1 code groups that are not needed to prepare trial object
                        code_idx += 1
                    elif tc.code in [TC_REWARD_LEN, TC_MID_TRIAL_REW, TC_CHECK_RESP_ON, TC_RANDOM_SEED,
                                     TC_XY_TARGET_USED]:
                        # N=2 code groups that are not needed to prepare trial object
                        code_idx += 2
                    elif tc.code in [TC_RPD_WINDOW, TC_PSGM]:
                        # longer code groups that are not needed to prepare trial object
                        code_idx += (3 if tc.code == TC_RPD_WINDOW else 6)
                    elif tc.code == TC_END_TRIAL:
                        code_idx += 1
                        done = True
                    else:
                        # unrecognized trial code!
                        raise DataFileError(f"Found bad trial code = {tc.code}")

                    # move on to next code
                    if not done:
                        if code_idx >= len(codes):
                            raise DataFileError("Reached end of trial codes before seeing end-of-trial code!")
                        tc = codes[code_idx]
                    # END PROC CODES LOOP
                curr_tick += 1
                # END OF OUTER WHILE LOOP

            # if we did not get TC_ADC_ON, assume recording began at trial start
            if record_seg_idx < 0:
                record_seg_idx = 0
            # set duration of last segment (subtract 1 b/c we incremented tick counter past trial's end
            if curr_segment is not None:
                curr_segment.dur = curr_tick - 1 - seg_start_time
            # save pre-version 8 velocity stabilization state using the newer way of doing it
            if 0 <= pre_v8_open_seg <= len(segments):
                seg = segments[pre_v8_open_seg]
                tgt_idx = seg.fix1
                if 0 <= tgt_idx <= len(targets):
                    mask = VEL_STAB_ON
                    if (pre_v8_open_mask & OPEN_MODE_MASK) == OPEN_MODE_SNAP:
                        mask = mask | VEL_STAB_SNAP
                    if (pre_v8_open_mask & OPEN_ENABLE_H_ONLY) == OPEN_ENABLE_H_ONLY:
                        mask = mask | VEL_STAB_H
                    if (pre_v8_open_mask & OPEN_ENABLE_V_ONLY) == OPEN_ENABLE_V_ONLY:
                        mask = mask | VEL_STAB_V
                    for i in range(pre_v8_num_open_segs):
                        if (pre_v8_open_seg + i) >= len(segments):
                            break
                        segments[pre_v8_open_seg+i].tgt_vel_stab_mask[tgt_idx] = mask
                        if i == 0:
                            mask = mask & ~VEL_STAB_SNAP

            # now that we've computed the segment table from the trail codes, go back and apply the INVERSE of the
            # trial's global target transform (if NOT the identity transform) to all target trajectory parameters to
            # recover their values as the appeared in the original Maestro trial. This is important in order to decide
            # whether or not two trial reps are "similar" (ie, reps of the same trial protocol).
            ''' NOTE: Keeping this code just in case we decide not to include target transform in similarity test
            xfm = header.global_transform()
            if not (xfm.is_identity_for_vel() and xfm.is_identity_for_pos()):
                is_first_seg = True
                for seg in segments:
                    for tgt_idx in range(len(targets)):
                        xfm.invert_velocity(seg.tgt_vel[tgt_idx])
                        xfm.invert_velocity(seg.tgt_acc[tgt_idx])  # velocity xfm also applied to acceleration vectors
                        xfm.invert_velocity(seg.tgt_pat_vel[tgt_idx])
                        xfm.invert_velocity(seg.tgt_pat_acc[tgt_idx])

                        # the transform's H/V position offsets (added in file version 15) are only applied in the first
                        # segment, and only if the target is positioned relatively in that segment. Note that we do this
                        # before the rotate and scale step because we are inverting the transform!
                        if is_first_seg and (header.version >= 15) and seg.tgt_rel[tgt_idx]:
                            seg.tgt_pos[tgt_idx].offset_by(-xfm.pos_offsetH_deg, -xfm.pos_offsetV_deg)
                        # the target position vector is NOT transformed in the first segment IF the target is positioned
                        # absolutely. This change was effective 11Jun2010, data file version 16.
                        if (header.version < 16) or (not is_first_seg) or seg.tgt_rel[tgt_idx]:
                            xfm.invert_position(seg.tgt_pos[tgt_idx])
                    is_first_seg = False
            '''

            # HACK: Because trial codes store floating-point trajectory parameters as scaled 16-bit integers, there's a
            # loss of precision versus the original values in the Maestro trial. If the inverse transform has to be
            # applied, it introduces an even greater discrepancy between the value recovered from trial code processing
            # versus what was specified in Maestro. Here we look for trajectory values that are within +/-0.07 of an
            # integral value (except 0), and if so, "round" to that integral value.
            for seg in segments:
                for tgt_idx in range(len(targets)):
                    x = seg.tgt_vel[tgt_idx].x
                    y = seg.tgt_vel[tgt_idx].y
                    x_round = round(x)
                    y_round = round(y)
                    seg.tgt_vel[tgt_idx].set(x_round if ((x_round != 0) and (abs(x_round-x) < 0.07)) else x,
                                             y_round if ((y_round != 0) and (abs(y_round-y) < 0.07)) else y)
                    x = seg.tgt_acc[tgt_idx].x
                    y = seg.tgt_acc[tgt_idx].y
                    x_round = round(x)
                    y_round = round(y)
                    seg.tgt_acc[tgt_idx].set(x_round if ((x_round != 0) and (abs(x_round-x) < 0.07)) else x,
                                             y_round if ((y_round != 0) and (abs(y_round-y) < 0.07)) else y)
                    x = seg.tgt_pos[tgt_idx].x
                    y = seg.tgt_pos[tgt_idx].y
                    x_round = round(x)
                    y_round = round(y)
                    seg.tgt_pos[tgt_idx].set(x_round if ((x_round != 0) and (abs(x_round-x) < 0.07)) else x,
                                             y_round if ((y_round != 0) and (abs(y_round-y) < 0.07)) else y)
                    x = seg.tgt_pat_vel[tgt_idx].x
                    y = seg.tgt_pat_vel[tgt_idx].y
                    x_round = round(x)
                    y_round = round(y)
                    seg.tgt_pat_vel[tgt_idx].set(x_round if ((x_round != 0) and (abs(x_round-x) < 0.07)) else x,
                                                 y_round if ((y_round != 0) and (abs(y_round-y) < 0.07)) else y)
                    x = seg.tgt_pat_acc[tgt_idx].x
                    y = seg.tgt_pat_acc[tgt_idx].y
                    x_round = round(x)
                    y_round = round(y)
                    seg.tgt_pat_acc[tgt_idx].set(x_round if ((x_round != 0) and (abs(x_round-x) < 0.07)) else x,
                                                 y_round if ((y_round != 0) and (abs(y_round-y) < 0.07)) else y)

            # convert each mutable trial segment object to a read-only version for exposure in Trial object
            readonly_segs: List[Segment] = list()
            num_tgts = len(targets)
            for seg in segments:
                tgt_flags: List[int] = [0]*num_tgts
                tgt_pos: List[float] = [0.0] * (2 * num_tgts)
                tgt_vel: List[float] = [0.0] * (2 * num_tgts)
                tgt_acc: List[float] = [0.0] * (2 * num_tgts)
                tgt_pat_vel: List[float] = [0.0] * (2 * num_tgts)
                tgt_pat_acc: List[float] = [0.0] * (2 * num_tgts)
                for i in range(num_tgts):
                    tgt_flags[i] = seg.tgt_vel_stab_mask[i] + (_TGT_ON_FLAG if seg.tgt_on[i] else 0) + \
                                   (_TGT_REL_FLAG if seg.tgt_rel[i] else 0)
                    tgt_pos[2 * i: 2 * i + 2] = seg.tgt_pos[i].x, seg.tgt_pos[i].y
                    tgt_vel[2 * i: 2 * i + 2] = seg.tgt_vel[i].x, seg.tgt_vel[i].y
                    tgt_acc[2 * i: 2 * i + 2] = seg.tgt_acc[i].x, seg.tgt_acc[i].y
                    tgt_pat_vel[2 * i: 2 * i + 2] = seg.tgt_pat_vel[i].x, seg.tgt_pat_vel[i].y
                    tgt_pat_acc[2 * i: 2 * i + 2] = seg.tgt_pat_acc[i].x, seg.tgt_pat_acc[i].y

                ro_seg = Segment(
                    num_targets=len(targets), dur=seg.dur, pulse_ch=seg.pulse_ch, fix1=seg.fix1, fix2=seg.fix2,
                    grace=seg.grace, xy_update_intv=seg.xy_update_intv, fixacc_h=seg.fixacc_h, fixacc_v=seg.fixacc_v,
                    tgt_flags=tuple(tgt_flags), tgt_pos=tuple(tgt_pos), tgt_vel=tuple(tgt_vel), tgt_acc=tuple(tgt_acc),
                    tgt_pat_vel=tuple(tgt_pat_vel), tgt_pat_acc=tuple(tgt_pat_acc)
                )
                readonly_segs.append(ro_seg)

            # return the trial definition!
            return Trial(name=header.trial_name, set_name=header.trial_set_name if header.version >= 21 else None,
                         subset_name=header.trial_subset_name if header.version >= 21 else None,
                         segments=tuple(readonly_segs), targets=tuple(targets), perts=tuple(perturbations),
                         sections=tuple(sections) if sections else tuple(), record_seg=record_seg_idx,
                         skip_seg=skip_seg_idx, file_version=header.version, xy_seed=header.xy_random_seed,
                         global_transform=header.global_transform())
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected error occurred while preparing trial definition: {str(err)}")

    def is_similar_to(self, other: Trial) -> bool:
        """
        Assess whether or not this Maestro trial is "similar enough" to another Maestro trial in the sense that the
        data collected during the two trials might be usefully compared or combined in some fashion. It requires that
        the trials have the same name, same set and subset names (for file versions >= 21), same number of segments,
        same participating targets (the seeds of RMVideo random-dot patch targets are not compared), same tagged
        sections, same perturbations, and the same global target transforms. Recording must start on the same segment
        in both trials. Per-segment marker pulse channel, fixation target designations, and XYScope update interval
        (if XYScope used) must match. Per-segment, per-target on/off states, relative/absolute position flags, and
        velocity stabilization masks must also match. Per-segment fixation accuracy and grace period are NOT considered.

        Args:
            other: The trial to compare.
        Returns:
            True if this trial is similar to the other, as described.
        """
        similar = (other is not None) and (self.name == other.name) and (self.num_segments == other.num_segments) and \
                  (self.record_seg == other.record_seg) and \
                  (self._definition['targets'] == other._definition['targets']) and \
                  (self._definition['perts'] == other._definition['perts']) and \
                  (self._definition['sections'] == other._definition['sections']) and \
                  (self.global_transform == other.global_transform)
        if similar and not (self.set_name is None):
            similar = (self.set_name == other.set_name) and (self.subset_name == other.subset_name)
        if not similar:
            return False
        seg: Segment
        for i, seg in enumerate(self._definition['segments']):
            other_seg: Segment = other._definition['segments'][i]
            if (seg.pulse_ch != other_seg.pulse_ch) or (seg.fix1 != other_seg.fix1) or (seg.fix2 != other_seg.fix2) or \
               (self.uses_xy_scope and (seg.xy_update_intv != other_seg.xy_update_intv)):
                return False
            for j in range(self.num_targets):
                if (seg.tgt_on(j) != other_seg.tgt_on(j)) or (seg.tgt_rel(j) != other_seg.tgt_rel(j)) or \
                   (seg.tgt_vel_stab_mask(j) != other_seg.tgt_vel_stab_mask(j)):
                    return False
        return True

    @property
    def path_name(self) -> str:
        """
        The full "path name" of a Maestro trial includes the names of the trial set and, optionally, trial subset
        containing the trial. However, the set and subset names were not included in the Maestro data file until V=21;
        for older files, the path name is simply the trial name itself.

        Returns:
            Trial path name in the format "set/subset/trial". For trials culled from pre-version 21 data files, the
                path name is just the trial name itself. Trial subsets are optional; if no subset is specified, the path
                name has the form "set/trial".
        """

        if (self.set_name is None) or (len(self.set_name) == 0):
            return self.name
        elif (self.subset_name is None) or (len(self.subset_name) == 0):
            return "/".join([self.set_name, self.name])
        else:
            return "/".join([self.set_name, self.subset_name, self.name])

    @property
    def uses_xy_scope(self) -> bool:
        """
        Does this trial use targets presented on Maestro's older XYScope video platform?

        Returns:
            True if any of the trial's participating targets use the XYScope platform.
        """
        tgt: Target
        return any([tgt.hardware_type == CX_XY_TGT for tgt in self._definition['targets']])

    @property
    def uses_fix1(self) -> bool:
        """
        Does this trial designate a participating target as fixation target #1 during any segment of the trial? The
        target must also be turned on in at least one segment in which it is designated as fixation target #1.
        """
        seg: Segment
        return any([(seg.fix1 >= 0 and seg.tgt_on(seg.fix1)) for seg in self._definition['segments']])

    @property
    def uses_fix2(self) -> bool:
        """
        Does this trial designate a participating target as fixation target #2 during any segment of the trial? The
        target must also be turned on in at least one segment in which it is designated as fixation target #2.
        """
        seg: Segment
        return any([(seg.fix2 >= 0 and seg.tgt_on(seg.fix2)) for seg in self._definition['segments']])

    @property
    def uses_vstab(self) -> bool:
        """ Does this trial velocity-stabilize any participating target during any segment? """
        seg: Segment
        for seg in self._definition['segments']:
            for i in range(self.num_targets):
                if seg.tgt_vel_stab_mask(i) != 0:
                    return True
        return False

    @property
    def duration(self) -> int:
        """ The total duration of this trial (sum of individual segment durations) in milliseconds. """
        seg: Segment
        return sum([seg.dur for seg in self._definition['segments']])

    def segment_table_differences(self, other: Trial) -> Optional[List[SegParam]]:
        """
        Return a list of all segment parameter differences between this trial and another. This method is called when
        determining whether or not two Maestro trial instances are repetitions of the same trial definition, with the
        exception of one or more randomized segment table parameters. A segment table parameter is randomized in Maestro
        by defining a trial random variable or, more commonly, by specifying different minimum and maximum durations for
        a segment.

        Float-valued target trajectory parameters are "different" only if the absolute difference between their
        values exceeds 0.05. This is because there's a loss of precision when storing floating-point values in the
        trial codes (as scaled 16-bit integers).

        For two trials to be comparable, they must be similar enough -- see `is_similar_to()`. Fixation accuracy and
        grace period are currently excluded from consideration in the similarity test, so those parameters will never
        appear in a list of segment table differences.

        Args:
            other: The other trial.
        Returns:
            List of segment table parameters in which this trial differs from the trial specified. Returns an empty list
                if there are no differences. Returns None if the two trials are NOT comparable.
        """
        if not self.is_similar_to(other):
            return None
        out: List[SegParam] = list()
        seg: Segment
        for i, seg in enumerate(self._definition['segments']):
            other_seg = other._definition['segments'][i]
            if seg.dur != other_seg.dur:
                out.append(SegParam(SegParamType.DURATION, i))
            if seg.pulse_ch != other_seg.pulse_ch:
                out.append(SegParam(SegParamType.MARKER, i))
            if seg.fix1 != other_seg.fix1:
                out.append(SegParam(SegParamType.FIX_TGT1, i))
            if seg.fix2 != other_seg.fix2:
                out.append(SegParam(SegParamType.FIX_TGT2, i))
            if self.uses_xy_scope and (seg.xy_update_intv != other_seg.xy_update_intv):
                out.append(SegParam(SegParamType.XY_UPDATE_INTV, i))
            for j in range(self.num_targets):
                if seg.tgt_on(j) != other_seg.tgt_on(j):
                    out.append(SegParam(SegParamType.TGT_ON_OFF, i, j))
                if seg.tgt_rel(j) != other_seg.tgt_rel(j):
                    out.append(SegParam(SegParamType.TGT_REL, i, j))
                if seg.tgt_vel_stab_mask(j) != other_seg.tgt_vel_stab_mask(j):
                    out.append(SegParam(SegParamType.TGT_VSTAB, i, j))
                if abs(seg.tgt_pos(j)[0] - other_seg.tgt_pos(j)[0]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_POS_H, i, j))
                if abs(seg.tgt_pos(j)[1] - other_seg.tgt_pos(j)[1]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_POS_V, i, j))
                if abs(seg.tgt_vel(j)[0] - other_seg.tgt_vel(j)[0]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_VEL_H, i, j))
                if abs(seg.tgt_vel(j)[1] - other_seg.tgt_vel(j)[1]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_VEL_V, i, j))
                if abs(seg.tgt_acc(j)[0] - other_seg.tgt_acc(j)[0]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_ACC_H, i, j))
                if abs(seg.tgt_acc(j)[1] - other_seg.tgt_acc(j)[1]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_ACC_V, i, j))
                if abs(seg.tgt_pat_vel(j)[0] - other_seg.tgt_pat_vel(j)[0]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_PAT_VEL_H, i, j))
                if abs(seg.tgt_pat_vel(j)[1] - other_seg.tgt_pat_vel(j)[1]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_PAT_VEL_V, i, j))
                if abs(seg.tgt_pat_acc(j)[0] - other_seg.tgt_pat_acc(j)[0]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_PAT_ACC_H, i, j))
                if abs(seg.tgt_pat_acc(j)[1] - other_seg.tgt_pat_acc(j)[1]) > 0.05:
                    out.append(SegParam(SegParamType.TGT_PAT_ACC_V, i, j))
        return out

    def retrieve_segment_table_parameter_value(self, param: SegParam) -> Union[bool, int, float, None]:
        """
        Retrieve a parameter from this trial's segment table. This is primarily intended to find the value of a defined
        random variable in a particular instance of a trial protocol.

        Args:
            param: The identified parameter.

        Returns:
            The parameter value (an int, float, or boolean). Returns None if the parameter is invalid.
        """
        out: Union[bool, int, float, None] = None
        try:
            seg: Segment = self._definition['segments'][param.seg_idx]
            out = seg.value_of(param.type, param.tgt_idx)
        except IndexError:
            pass
        return out

    @property
    def record_start(self) -> int:
        """
        The elapsed trial time at which recording o behavioral responses and events began, in milliseconds since trial
        start. Normally, this is 0. However, if the trial's record segment index is NOT the first segment, then it is
        the sum of the segment durations prior to the record segment.

        NOTE: If the trial is one rep of a trial protocol containing at least one random segment duration, then this
        will not be the elapsed start time for every possible rep if there is at least one random-duration segment
        prior to the record segment.
        """
        segs: List[Segment] = self._definition['segments']
        return sum(segs[i].dur for i in range(self.record_seg))


class SegParamType(DocEnum):
    """
    An enumeration of (most of) the parameter types that define a segment within a Maestro trial.
    """
    DURATION = 1, "Segment duration (milliseconds)"
    MARKER = 2, "Channel on which marker pulse is delivered at segment start (if any)"
    FIX_TGT1 = 3, "Index position (zero-based) of target designated as the first fixation target"
    FIX_TGT2 = 4, "Index position (zero-based) of target designated as the second fixation target"
    FIXACC_H = 5, "Horizontal fixation accuracy in visual degrees (if enforced)"
    FIXACC_V = 6, "Vertical fixation accuracy in visual degrees (if enforced)"
    GRACE_PER = 7, "Grace period for segment (milliseconds; 0 = no grace period)"
    XY_UPDATE_INTV = 8, "XYScope update interval during segment (milliseconds)"
    TGT_ON_OFF = 9, "Target on/off state"
    TGT_REL = 10, "Target position change relative or absolute"
    TGT_VSTAB = 11, "Target velocity stabilization state"
    TGT_POS_H = 12, "Horizontal target position change at segment start (degrees)"
    TGT_POS_V = 13, "Vertical target position change at segment start (degrees)"
    TGT_VEL_H = 14, "Horizontal target velocity during segment (deg/sec)"
    TGT_VEL_V = 15, "Vertical target velocity during segment (deg/sec)"
    TGT_ACC_H = 16, "Horizontal target acceleration during segment (deg/sec^2)"
    TGT_ACC_V = 17, "Vertical target acceleration during segment (deg/sec^2)"
    TGT_PAT_VEL_H = 18, "Horizontal target pattern velocity during segment (deg/sec)"
    TGT_PAT_VEL_V = 19, "Vertical target pattern velocity during segment (deg/sec)"
    TGT_PAT_ACC_H = 20, "Horizontal target pattern acceleration during segment (deg/sec^2)"
    TGT_PAT_ACC_V = 21, "Vertical target pattern acceleration during segment (deg/sec^2)"

    def is_target_trajectory_parameter(self) -> bool:
        """
        Does this SegParamType identify a target trajectory parameter within a Maestro trial's segment table?
        """
        return self not in [SegParamType.DURATION, SegParamType.MARKER, SegParamType.FIX_TGT1, SegParamType.FIX_TGT2,
                            SegParamType.FIXACC_H, SegParamType.FIXACC_V, SegParamType.GRACE_PER,
                            SegParamType.XY_UPDATE_INTV]

    def can_vary_randomly(self) -> bool:
        """
        Can this segment parameter type vary randomly across repeated instances of the same Maestro trial?
        """
        return (self == SegParamType.DURATION) or \
               (self.is_target_trajectory_parameter() and
                (self not in [SegParamType.TGT_ON_OFF, SegParamType.TGT_REL, SegParamType.TGT_VSTAB]))


class SegParam:
    """
    Identification of a single parameter within the segment table of a Maestro trial.
    """
    def __init__(self, param_type: SegParamType, seg: int, tgt: int = -1):
        """
        Construct a trial segment table parameter ID.

        Args:
            param_type: The parameter type.
            seg: The segment index.
            tgt: THe target index. Default = -1 (meaning not applicable -- for parameters like segment duration).
        Raises:
            DataFileError: If the segment or target index is invalid.
        """
        self._type: SegParamType = param_type
        self._seg_idx: int = seg
        self._tgt_idx: int = tgt
        if not self._is_valid():
            raise DataFileError('Invalid segment table parameter specification')

    def __eq__(self, other: SegParam) -> bool:
        """ Return true if the relevant attributes of this SegParam match the corresponding attributes of other."""
        return (self.__class__ == other.__class__) and (self.type == other.type) and \
               (self.seg_idx == other.seg_idx) and \
               ((not self.type.is_target_trajectory_parameter()) or (self.tgt_idx == other.tgt_idx))

    def __hash__(self) -> int:
        """ Return hash of the relevant attributes of this SegParam"""
        hash_attrs = [self.type, self.seg_idx]
        if self.type.is_target_trajectory_parameter():
            hash_attrs.append(self.tgt_idx)
        return hash(tuple(hash_attrs))

    def __str__(self) -> str:
        return f"Param type={self.type.name}, segment={self.seg_idx}, target={self.tgt_idx}"

    def _is_valid(self) -> bool:
        """ Verifies that segment index is a valid value; similarly for target index -- if applicable. """
        return (0 <= self.seg_idx < MAX_SEGMENTS) and ((not self.type.is_target_trajectory_parameter()) or
                                                       (0 <= self.tgt_idx < MAX_TRIALTARGS))

    @property
    def type(self) -> SegParamType:
        """ The segment table parameter type. """
        return self._type

    @property
    def seg_idx(self) -> int:
        """ The (zero-based) index of the trial segment to which this parameter applies. """
        return self._seg_idx

    @property
    def tgt_idx(self) -> int:
        """ The (zero-based) index of the trial target to which this parameter applies (-1 if not applicable)."""
        return self._tgt_idx


class Protocol:
    """
    A Maestro trial protocol.

    A typical Maestro experiment session involves the repeated presentation of many Maestro trials, with the results
    from each trial presentation stored in a data file. That file includes the sequence of trial codes defining the
    trial, as well as the definitions of participating targets and other trial-specific information. The definition of
    the trial as it appears in Maestro is the "trial protocol", as distinguished from a particular presentation of that
    protocol -- a "trial rep". Part of the workflow in committing an experiment to the lab database is to detect all of
    the distinct trial protocols presented during the session. The difficulty lies in the fact that a typical protocol
    will often include a "random variable" -- a segment table parameter that varies randomly from one trial rep to the
    next; the most typical example of this is an initial "fixation" segment with random duration.

    In order to "detect" a unique protocol, we need to process at least 2 reps of that protocol in order to identify
    any random variables; the more reps processed, the better the chances of identifying all random variables defined in
    the protocol. If only one rep is encountered, then the user must validate the protocol definition and identify any
    and all random variables in it.

    For these reasons, we distinguish a protocol "candidate" from a confirmed trial protocol. Protocol candidates are
    extracted while processing the trial data files, then validated as actual protocols in one of several ways:
        - If only one trial rep was processed, user validation is required.

        - If two trial reps were processed, user validation is required unless there is an existing (ie, stored in the
          lab database) matching the candidate protocol.

        - If 3+ trial reps were processed, the protocol candidate is assumed to represent a real Maestro trial protocol
          and user validation is not required.
    **Avoid constructing `Protocol` objects directly.** As described above, protocols are "found" by analyzing the
    Maestro trials presented during a single experiment -- `extract_protocols_from_session_data()`. This method is
    called during automatic preprocessing of a session archive. During an interactive review phase prior to committing
    the experiment session to the lab database, any protcol candidates for which only 1 rep was encountered -- or 2 reps
    but without a match to a protocol already in the database -- must be validated by the user manually. The user can
    also add additional random variables to the protocol, but no changes to the protocol's representative trial can be
    made. Once validated, the protocol is no longer considered a "candidate". Its definition is frozen and may not be
    altered.

    During a session commit, each (confirmed) protocol object is persisted in its entirety to the lab database, since
    it contains a lot of information needed to accurately compute target trajectories during any given rep of the
    protocol. Given the complex nature of the protocol definition, it is stored as an encoded byte sequence ("blob").
    The method to_bytes() prepares the encoded byte sequence, while from_bytes() reconstructs the `Protocol` object from
    that sequence.
    """
    def __init__(self, trial: Trial):
        """
        Construct a Maestro trial protocol. Initially, it is configured as a protocol candidate with no defined
        random variables.

        Args:
            trial: The underlying Maestro trial definition.
        """
        self._trial = trial
        """ The trial defining this trial protocol candidate, excluding any random variables. """
        self._rvs: List[SegParam] = list()
        """ 
        The protocol's random variables, i.e., those segment table parameters that vary randomly over repeated
        presentations of the protocol.
        """
        self._md5_digest: Optional[str] = None
        """
        The hexadecimal character digest of the MD5 hash for this validated trial protocol. It is 32 characters long and
        serves to uniquely identify the protocol. Currently, the hash includes the following aspects of the protocol's
        definition: trial's full name (including set and subset, if applicable), the participating target list, any
        defined perturbations and tagged sections, and the index of the segment when recording started. The detailed
        segment table is ALSO part of the hash digest, with the exception of per-segment fixation accuracy (H and V),
        per-segment grace period, and any target trajectory parameter that is a designated random variable.
        
        While the protocol is an unconfirmed 'candidate', the MD5 hash is undefined (None). Once it is validated as an
        actual protocol, the definition is frozen and the hash is calculated.
        """
        self._num_reps = 1
        """ 
        The number of trial reps that were processed to identify this trial protocol. Only used internally while 
        extracting protocols from the Maestro trials recorded during a single experiment session.
        """

    @property
    def trial(self) -> Trial:
        """ The underlying trial definition for this trial protocol. """
        return self._trial

    @property
    def random_variables(self) -> Tuple[SegParam]:
        """
        The trial protocol's random variables. Could be empty. The set of random variable may be modified as long as the
        protocol is marked as a "candidate" rather than an actual trial protocol. See `add_random_variable()`.
        """
        return tuple(self._rvs)

    @property
    def md5_digest(self) -> Optional[str]:
        """
        A 32-character MD5 hash digest that uniquely identifies this trial protocol. Returns None for a protocol
        candidate.
        """
        return self._md5_digest

    @property
    def is_candidate(self) -> bool:
        """ True if this is a trial protocol 'candidate' requiring manual user validation. """
        return self._md5_digest is None

    @property
    def num_reps(self) -> int:
        """ The number of reps of this trial protcol processed while scanning all trials in an experiment sesssion. """
        return self._num_reps

    @property
    def can_aggregate_responses(self) -> bool:
        """
        Can the behavioral and neural responses to repeated presentations of this trial protocol be aggregated in some
        fashion, typically by averaging? By convention, the protocol must have AT MOST one defined random variable, and
        that random variable must affect the duration of one segment (not necessarily the first) in the trial protocol.

        Returns:
            True if protocol is amenable to averaging response data, as described; else False.
        """
        return (len(self._rvs) == 0) or \
               ((len(self._rvs) == 1) and (self._rvs[0].type == SegParamType.DURATION))

    def validate(self) -> None:
        """
        Validate this protocol candidate as an actual Maestro trial protocol. This method only has an effect if this
        protocol object is a "candidate" requiring user validation; once validated, the protocol definition is frozen.
        """
        if self.is_candidate:
            self._md5_digest = Protocol.generate_md5_hash_digest_for_protocol(self)

    def add_random_variable(self, rv: SegParam) -> bool:
        """
        Add a random variable to the definition of this Maestro trial protocol 'candidate', unless it has already been
        validated an actual protocol. **Once validated, the protocol definition cannot be modified in any way, and this
        method has no effect.**

        Args:
            rv: The random variable
        Returns:
            False if the operation is not possible because protocol is already validated and its definition frozen, or
                the specified random variable is invalid (bad parameter type, segment index, or target index).
        """
        if self.is_candidate:
            if rv.type.can_vary_randomly() and (0 <= rv.seg_idx < self._trial.num_segments) and \
                    ((not rv.type.is_target_trajectory_parameter()) or (0 <= rv.tgt_idx < self._trial.num_targets)):
                if not (rv in self._rvs):
                    self._rvs.append(rv)
                return True
        return False

    def target_trajectories(
            self, trial_rvs: List[Union[int, float]], hgpos: Optional[np.ndarray] = None,
            vepos: Optional[np.ndarray] = None, vstab_win_len: Optional[int] = None) -> List[np.ndarray]:
        """
        Compute the position trajectories of all targets during a particular instance of this trial protocol.

        This implementation does a basic piecewise integration similar to what happens on the fly in Maestro during a
        trial. However, it does NOT account for velocity perturbations nor the video update rate of the RMVideo and
        XYScope platforms. Also, it calculates position only, not velocity nor pattern velocity for video targets.
        Finally, the calculation assumes that targets move even if they are turned off. This has always been the case --
        except for XYScope targets prior to Maestro 1.2.1

        Args:
            trial_rvs: Values to assign to protocol's random variables for the particular trial instance (if any).
            hgpos: The horizontal eye position trajectory (in deg) during the particular trial instance -- used to
                adjust target trajectories during periods of velocity stabilization. Default = None, in which case no
                adjustment can be made.
            vepos: The vertical eye position trajectory (in deg) during the trial -- used to adjust target trajectories
                during periods of velocity stabilization. Default = None, in which case no adjustment can be made.
            vstab_win_len: The length of the sliding window (1 to 20 ms) for smoothing eye position when computing the
                target trajectory adjustment for velocity stabilization. Default = None (no smoothing).
        Returns:
            A list of 2D Numpy arrays, where the I-th array is the position trajectory of the I-th participating target.
                Each array is Nx2, where N is the trial duration and the N-th "row" is the (H,V) position of the target
                N milliseconds since trial start. Position is in degrees subtended at the eye.
        Raises:
            ValueError: If the number of supplied RV values does not match the number of RVs defined on the protocol.
        """
        rv_map: Dict[SegParam, Union[int, float]] = dict()
        if len(self._rvs) > 0:
            if len(self._rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, rv in enumerate(self._rvs):
                rv_map[rv] = trial_rvs[i]

        # careful! duration of a particular trial rep will vary if there are any random segment durations
        dur = self.duration_of_rep(trial_rvs)

        num_tgts = self.trial.num_targets
        trajectories: List[np.ndarray] = [np.zeros((dur, 2)) for _ in range(num_tgts)]
        current_pos: List[Point2D] = [Point2D(0, 0) for _ in range(num_tgts)]
        current_vel: List[Point2D] = [Point2D(0, 0) for _ in range(num_tgts)]

        # enable velocity stabilization compensation if all restrictions met
        t_record = self.record_start_of_rep(trial_rvs)
        do_vstab = self.trial.uses_vstab and (hgpos is not None) and (vepos is not None) and \
            (len(hgpos) == len(vepos)) and (len(hgpos) >= (dur - t_record))
        vstab_win_len = 1 if (not isinstance(vstab_win_len, int)) else max(min(20, vstab_win_len), 1)
        current_eye_pos = Point2D(0, 0)
        last_eye_pos = Point2D(0, 0)

        t = 0
        delta = 0.001  # in Maestro, one "tick" = 1 millisecond
        segments: Tuple[Segment] = self.trial.segments
        for seg_idx, seg in enumerate(segments):
            for i in range(num_tgts):
                # HACK: We have to inject the supplied RV values whereever an RV applies!
                param = SegParam(SegParamType.TGT_POS_H, seg_idx, i)
                pos_h = rv_map[param] if param in rv_map else seg.tgt_pos(i)[0]
                param = SegParam(SegParamType.TGT_POS_V, seg_idx, i)
                pos_v = rv_map[param] if param in rv_map else seg.tgt_pos(i)[1]
                param = SegParam(SegParamType.TGT_VEL_H, seg_idx, i)
                vel_h = rv_map[param] if param in rv_map else seg.tgt_vel(i)[0]
                param = SegParam(SegParamType.TGT_VEL_V, seg_idx, i)
                vel_v = rv_map[param] if param in rv_map else seg.tgt_vel(i)[1]

                if seg.tgt_rel(i):
                    current_pos[i].offset_by(pos_h, pos_v)
                else:
                    current_pos[i].set(pos_h, pos_v)
                current_vel[i].set(vel_h, vel_v)

            param = SegParam(SegParamType.DURATION, seg_idx)
            seg_dur = rv_map[param] if param in rv_map else seg.dur
            t_start_seg = t
            while t < (t_start_seg + seg_dur):
                # if doing VStab compensation, get current eye position, smoothed if window length > 1.
                if do_vstab and (t >= t_record):
                    if (vstab_win_len == 1) or (t == t_record):
                        current_eye_pos.set(hgpos[t-t_record], vepos[t-t_record])
                    else:
                        start = max(0, t-t_record-vstab_win_len)
                        end = t-t_record
                        # noinspection PyTypeChecker
                        current_eye_pos.set(np.nanmean(hgpos[start:end]), np.nanmean(vepos[start:end]))

                for i in range(num_tgts):
                    # velocity stabilization adjustment of target position, if applicable
                    vstab_mask = seg.tgt_vel_stab_mask(i)
                    if do_vstab and (t >= t_record) and vstab_mask != 0:
                        if (t == t_start_seg) and \
                              ((seg_idx == 0) or (segments[seg_idx-1].tgt_vel_stab_mask(i) == 0)) and \
                              ((vstab_mask & VEL_STAB_SNAP) != 0):
                            current_pos[i].set_point(current_eye_pos)
                        else:
                            current_pos[i].offset_by(
                                (current_eye_pos.x - last_eye_pos.x) if ((vstab_mask & VEL_STAB_H) != 0) else 0,
                                (current_eye_pos.y - last_eye_pos.y) if ((vstab_mask & VEL_STAB_V) != 0) else 0
                            )
                    trajectories[i][t, :] = [current_pos[i].x, current_pos[i].y]
                    current_pos[i].offset_by(current_vel[i].x * delta, current_vel[i].y * delta)
                    current_vel[i].offset_by(seg.tgt_acc(i)[0] * delta, seg.tgt_acc(i)[1] * delta)
                t += 1

                # if doing VStab compensation, remember eye position
                if do_vstab:
                    last_eye_pos.set_point(current_eye_pos)

        return trajectories

    def compute_fixation_target_trajectories(
            self, trial_rvs: List[Union[int, float]], hgpos: Optional[np.ndarray] = None,
            vepos: Optional[np.ndarray] = None, vstab_win_len: Optional[int] = None) -> \
            Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """
        Compute the H,V position trajectory of designated fixation targets #1 and #2 over the course of a particular
        instance of this trial protocol.

        This implementation does a basic piecewise integration similar to what happens on the fly in Maestro during a
        trial. However, it does NOT account for ANY of the following: velocity perturbations, the video update rate of
        the RMVideo and XYScope platforms. Also, it does not provide window velocity and pattern velocity traces for the
        fixation targets. If the eye position trajectory is supplied, it will adjust target trajectories during any
        segments in which H and/or V velocity stabilization is enabled.

        In most protocols, only "Fix1" is used. In that case, the method will, obviously, not compute the position
        trajectory for "Fix2". Furthermore, if the designated "Fix1" target is unspecified during any segment of the
        trial, then its position is reported as "(NaN,NaN)" for each "tick" during that segment. NOTE that the position
        trajectory does NOT reflect whether or not the target is actually ON.

        Args:
            trial_rvs: Values to assign to protocol's random variables for the particular trial instance (if any).
            hgpos: The horizontal eye position trajectory (in deg) over the course of the particular trial instance --
                used to adjust target trajectories during periods of velocity stabilization. Default = None, in which
                case no adjustment can be made.
            vepos: The vertical eye position trajectory (in deg) over the course of the particular trial instance --
                used to adjust target trajectories during periods of velocity stabilization. Default = None, in which
                case no adjustment can be made.
            vstab_win_len: The length of the sliding window (1 to 20 ms) for smoothing eye position when computing the
                target trajectory adjustment for velocity stabilization. Default = None (no smoothing).
        Returns:
            A 2-tuple (fix1, fix2). The first element is the position trajectory for fixation target #1, and the second
                is that for fixation target #2. If a fixation target is unused, the corresponding element is None. Else,
                it is a 2D Numpy array, where the T-th row in the outer array is the (H,V) position of the
                designated fixation target, in degrees subtended at the eye, T milliseconds since the trial start.
        Raises:
            ValueError: If the number of supplied RV values does not match the number of RVs defined on the protocol.
        """
        segments: Tuple[Segment] = self.trial.segments
        trial_dur = self.duration_of_rep(trial_rvs)
        seg_durs_for_rep = self.segment_durations_for_rep(trial_rvs)
        tgt_pos_trajectories: List[np.ndarray] = self.target_trajectories(trial_rvs, hgpos, vepos, vstab_win_len)
        fix1: Optional[np.ndarray] = None
        if self.trial.uses_fix1:
            fix1 = np.empty((trial_dur, 2))
            fix1[:] = np.nan
            t = 0
            for i, seg in enumerate(segments):
                seg_dur = seg_durs_for_rep[i]
                if seg.fix1 >= 0:
                    fix1[t:t + seg_dur, :] = tgt_pos_trajectories[seg.fix1][t:t + seg_dur, :].copy()
                t += seg_dur
        fix2: Optional[np.ndarray] = None
        if self.trial.uses_fix2:
            fix2 = np.empty((trial_dur, 2))
            fix2[:] = np.nan
            t = 0
            for i, seg in enumerate(segments):
                seg_dur = seg_durs_for_rep[i]
                if seg.fix2 >= 0:
                    fix2[t:t + seg_dur, :] = tgt_pos_trajectories[seg.fix2][t:t + seg_dur, :].copy()
                t += seg_dur

        return fix1, fix2

    def compute_fixation_target_on_epochs(self, trial_rvs: List[Union[int, float]]) -> \
            Tuple[List[int], List[int]]:
        """
        Compute the epochs during which designated fixation targets #1 and #2 are turned ON over the course of a
        particular instance of this trial protocol.

        The trial target designated as "Fix 1" or "Fix 2" is set on a segment by segment basis, and any trial target
        can be turned on or off during each segment. This method analyzes the trial rep to define the intervals during
        which "Fix1" and "Fix2" is defined (they could be set to "NONE" for any given segment) and ON. Since segment
        duration can vary randomly, we need the random variable values for a trial rep to correctly calculate the
        epochs.

        Each ON epoch is an interval [S, E], with S and E in milliseconds since the start of the trial. If the fixation
        target is turned ON and OFF multiple times, there will be multiple epochs: [S1, E1, S2, E2, ..., SN, EN]. If
        the fixation target is ON for the entire trial, then there will be one epoch [S=0, E=duration of trial].

        Args:
            trial_rvs: Values to assign to protocol's random variables for the particular trial instance (if any).
        Returns:
            A 2-tuple (fix1, fix2). The first element is a list of 2*N elapsed times (ms since trial start) [S1, E1,
                S2, E2, ..., SN, EN] specifying the N non-overlapping ON epochs for fixation target #1. The second
                element holds the ON epochs for fixation target #2. If a fixation target is unused or never turned on,
                the corresponding element will be an empty list.
        Raises:
           ValueError: If the number of supplied RV values does not match the number of RVs defined on the protocol.
        """
        rv_map: Dict[SegParam, Union[int, float]] = dict()
        if len(self._rvs) > 0:
            if len(self._rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, rv in enumerate(self._rvs):
                rv_map[rv] = trial_rvs[i]

        segments: Tuple[Segment] = self.trial.segments
        fix1: List[int] = list()
        fix2: List[int] = list()
        t1_start = t2_start = -1
        t = 0
        for i, seg in enumerate(segments):
            if (t1_start == -1) and (seg.fix1 >= 0) and seg.tgt_on(seg.fix1):
                t1_start = t
            elif t1_start > -1 and ((seg.fix1 < 0) or not seg.tgt_on(seg.fix1)):
                fix1.extend([t1_start, t])
                t1_start = -1
            if (t2_start == -1) and (seg.fix2 >= 0) and seg.tgt_on(seg.fix2):
                t2_start = t
            elif t2_start > -1 and ((seg.fix2 < 0) or not seg.tgt_on(seg.fix2)):
                fix2.extend([t2_start, t])
                t2_start = -1
            # any segment could have a duration controlled by a random variable!
            param = SegParam(SegParamType.DURATION, i)
            seg_dur = rv_map[param] if param in rv_map else seg.dur
            t += seg_dur

        # close the last ON epoch, if fixation target is on through end of trial
        if t1_start > -1:
            fix1.extend([t1_start, t])
        if t2_start > -1:
            fix2.extend([t2_start, t])

        return fix1, fix2

    def duration_of_rep(self, trial_rvs: List[Union[int, float]]) -> int:
        """
        Get expected duration of a particular instance of this trial protocol. If the protocol lacks any random-duration
        segments, then all reps will have the same duration.

        Args:
            trial_rvs: Values to assign to protocol's random variables for the particular trial instance (if any).
        Returns:
            The expected duration of the trial rep given the durations -- specified in trial_rvs -- of any
                random-duration segments in the protocol. In milliseconds.
        Raises:
            ValueError: If the number of supplied RV values does not match the number of RVs defined on the protocol.
        """
        rv_map: Dict[SegParam, Union[int, float]] = dict()
        if len(self._rvs) > 0:
            if len(self._rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, rv in enumerate(self._rvs):
                rv_map[rv] = trial_rvs[i]
        dur = 0
        segments: Tuple[Segment] = self.trial.segments
        for i, seg in enumerate(segments):
            param = SegParam(SegParamType.DURATION, i)
            seg_dur = rv_map[param] if (param in rv_map) else seg.dur
            dur += seg_dur
        return dur

    def segment_durations_for_rep(self, trial_rvs: List[Union[int, float]]) -> List[int]:
        """
        Get the segment durations for a particular instance of this trial protocol. If a random variable controls the
        duration of any segment, that segment's duration will be different for each trial rep.

        Args:
            trial_rvs: Values to assign to protocol's random variables for the particular trial instance (if any).
        Returns:
            List of segment durations, in chronological order. If the protocol lacks any random-duration segments, the
                returned list is always the same.
        Raises:
            ValueError: If the number of supplied RV values does not match the number of RVs defined on the protocol.
        """
        if len(self._rvs) == 0:
            return [seg.dur for seg in self.trial.segments]

        rv_map: Dict[SegParam, Union[int, float]] = dict()
        if len(self._rvs) > 0:
            if len(self._rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, rv in enumerate(self._rvs):
                rv_map[rv] = trial_rvs[i]

        out: List[int] = list()
        segments: Tuple[Segment] = self.trial.segments
        for i, seg in enumerate(segments):
            param = SegParam(SegParamType.DURATION, i)
            seg_dur = rv_map[param] if (param in rv_map) else seg.dur
            out.append(seg_dur)
        return out

    def record_start_of_rep(self, trial_rvs: List[Union[int, float]]) -> int:
        """
        Get the elapsed trial time at which recording began for a particular instance of this trial protocol. If the
        protocol lacks any random-duration segments, then all reps will have the same record start time. (Of course, the
        record start time is always 0 if recording starts at the first segment.)

        Args:
            trial_rvs: Values to assign to protocol's random variables for the particular trial instance (if any).
        Returns:
            The record start time for the trial rep given the durations -- specified in trial_rvs -- of any
                random-duration segments in the protocol. In milliseconds.
        Raises:
            ValueError: If the number of supplied RV values does not match the number of RVs defined on the protocol.
        """
        if self.trial.record_seg <= 0:
            return 0
        elif len(self._rvs) == 0:
            return self.trial.record_start

        rv_map: Dict[SegParam, Union[int, float]] = dict()
        if len(self._rvs) > 0:
            if len(self._rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, rv in enumerate(self._rvs):
                rv_map[rv] = trial_rvs[i]

        t_start = 0
        segments: Tuple[Segment] = self.trial.segments
        for i in range(self.trial.record_seg):
            param = SegParam(SegParamType.DURATION, i)
            seg_dur = rv_map[param] if (param in rv_map) else segments[i].dur
            t_start += seg_dur
        return t_start

    @staticmethod
    def extract_protocols_from_session_data(archive: zipfile.ZipFile, proto_set: Set[str]) -> \
            Tuple[List[Protocol], Dict[str, int]]:
        """
        Examine all Maestro data files contained in the ZIP archive specified and return the list of trial protocols
        culled from those files. This is an important task when committing an experiment session's worth of data to the
        lab database.

        A protocol is automatically validated and its definition "frozen" under two scenarios:
            - A minimum of 3 reps of that protocol were processed.
            - 2 reps of that protocol were processed, AND it matches an already existing protocol stored in the lab
            database.
        Otherwise, the protocol is marked as a "candidate" and must be validated manually by the user during the
        'review' phase of the session commit workflow.

        Args:
            archive: An open ZIP archive containing the Maestro data files collected during an experiment session. Must
                be open for reading and is NOT closed on return.
            proto_set: This set contains the MD5 hash digests of all confirmed trial protocols currently stored in the
                lab database.
        Returns:
            A 2-tuple: a list of all trial protocols culled from the session data, and a dictionary that maps the
                filename of each trial data file to the list index identifying the trial protocol presented when that
                file was recorded.
        Raises:
            DataFileError: If a problem occurs while reading the ZIP archive and processing the data files therein.
        """
        try:
            archive_list = archive.infolist()
            data_file_name_pattern = re.compile("[.]\\d\\d\\d\\d$")
            proto_candidates: List[Protocol] = list()
            filename_to_protocol: Dict[str, int] = dict()
            for info in archive_list:
                if data_file_name_pattern.search(info.filename) is not None:
                    try:
                        trial = DataFile.load_trial(archive.read(info), info.filename)
                        found = False
                        for i, proto in enumerate(proto_candidates):
                            if proto._trial.is_similar_to(trial):
                                found = True
                                for rv in proto._trial.segment_table_differences(trial):
                                    proto.add_random_variable(rv)
                                filename_to_protocol[info.filename] = i
                                proto._num_reps += 1
                                break
                        if not found:
                            proto_candidates.append(Protocol(trial))
                            filename_to_protocol[info.filename] = len(proto_candidates) - 1
                    except DataFileError as err:
                        msg = f"===> Error: Failed loading file {info.filename}: {str(err)}"
                        raise DataFileError(msg)

            # auto-validate all protocol candidates with at least 3 reps, or with exactly 2 reps but that match
            # an existing protocol. Validation freezes the protocol def and sets its MD5 hash digest
            for p in proto_candidates:
                if p._num_reps > 2:
                    p.validate()
                elif p._num_reps == 2:
                    proto_hash = Protocol.generate_md5_hash_digest_for_protocol(p)
                    if proto_hash in proto_set:
                        p.validate()
            return proto_candidates, filename_to_protocol
        except DataFileError:
            raise
        except Exception as err:
            msg = f"Unexpected error while extracting trial protocols from session data: {str(err)}"
            raise DataFileError(msg)

    @staticmethod
    def generate_md5_hash_digest_for_protocol(proto: Protocol) -> str:
        """
        Generate the MD5 hash digest for this trial protocol. The following trial properties are included in the hash
        computation:
            - Trial path name, index of record start segment, global target transform.
            - Participating target list and any perturbations and tagged sections.
            - All segment table parameters EXCEPT fixation accuracy and grace period, plus any parameters that are in
              the protocol's list of random variables.

        Args:
            proto: A trial protocol.
        Returns:
            The MD5 hash digest, computed using the trial parameters described above.
        """
        hash_attrs = [proto.trial.path_name, proto.trial.record_seg, hash(proto.trial.global_transform),
                      [hash(tgt) for tgt in proto.trial.targets],
                      [hash(pert) for pert in proto.trial.perturbations],
                      [hash(section) for section in proto.trial.tagged_sections]]
        rvs: Tuple[SegParam] = proto.random_variables
        for seg_idx, seg in enumerate(proto.trial.segments):
            seg_params = list()
            num_targets = seg.num_targets
            for param_type in SegParamType:
                if param_type.is_target_trajectory_parameter():
                    for tgt_idx in range(num_targets):
                        if not (SegParam(param_type, seg_idx, tgt_idx) in rvs):
                            seg_params.append(seg.value_of(param_type, tgt_idx))
                elif not ((param_type in [SegParamType.FIXACC_H, SegParamType.FIXACC_V, SegParamType.GRACE_PER]) or
                          (SegParam(param_type, seg_idx, -1) in rvs)):
                    seg_params.append(seg.value_of(param_type, -1))
            hash_attrs.append(seg_params)

        digester = hashlib.md5()
        digester.update(pickle.dumps(hash_attrs))
        return digester.hexdigest()

    SERIAL_VERSION: int = 1
    """ The version number for serializing the protocol as a byte sequence. """

    def to_bytes(self) -> bytes:
        """
        Convert this trial protcol definition into a byte sequence for compact storage. To reconstruct the protocol
        definition object, pass this byte sequence to from_bytes().

        NOTE: We need to store trial protocol definitions in the database and send them to clients in response to select
        API calls in the lab portal. Given the security issues with pickle, and the need to possibly handle multiple
        versions of the trial protocol definition going forward, we decided to implement our own marshalling and
        unmarshalling routines.

        Returns:
            The byte sequence encoding this Maestro trial protocol definition.
        """
        raw = bytearray()
        raw.extend(struct.pack("<4H", Protocol.SERIAL_VERSION, self._num_reps, len(self._rvs),
                               len(self._md5_digest) if isinstance(self._md5_digest, str) else 0))
        if isinstance(self._md5_digest, str):
            digest = self._md5_digest.encode('ascii')
            raw.extend(struct.pack(f"<{len(digest)}s", digest))
        for rv in self._rvs:
            raw.extend(struct.pack("<3h", rv.type.value, rv.seg_idx, rv.tgt_idx))

        path_bytes = self.trial.path_name.encode('ascii')
        len_path = len(path_bytes)
        raw.extend(struct.pack(f"<H{len_path}s7hi", len_path, path_bytes, self.trial.num_segments,
                               self.trial.num_targets, self.trial.num_perturbations, self.trial.num_tagged_sections,
                               self.trial.record_seg, self.trial.skip_seg, self.trial.file_version, self.trial.xy_seed))
        raw.extend(self.trial.global_transform.to_bytes())
        for tgt in self.trial.targets:
            raw.extend(tgt.to_bytes())
        for section in self.trial.tagged_sections:
            raw.extend(section.to_bytes())
        for pert in self.trial.perturbations:
            raw.extend(pert.to_bytes())
        for segment in self.trial.segments:
            raw.extend(segment.to_bytes())

        return bytes(raw)

    @staticmethod
    def from_bytes(raw: bytes) -> Protocol:
        """
        Reconstruct a Maestro trial protocol definition from a raw byte sequence generated by to_bytes().

        Args:
            raw: The byte sequence encoding a Maestro trial protocol definition.
        Returns:
            The protocol definition.
        Raises:
            DataFileError: If any error occurs while parsing the byte sequence to reconstruct the protocol.
        """
        try:
            serial_version, num_reps, num_rvs, len_digest = struct.unpack_from("<4H", raw, 0)
            if serial_version != Protocol.SERIAL_VERSION:
                raise DataFileError(f"Invalid version detected in serialized trial protocol: {serial_version}")
            offset = struct.calcsize("<4H")
            md5_digest: Optional[str] = None
            if len_digest > 0:
                digest_raw = struct.unpack_from(f"<{len_digest}s", raw, offset)
                md5_digest = digest_raw[0].decode('ascii')
                offset += struct.calcsize(f"<{len_digest}s")
            rvs: List[SegParam] = list()
            for i in range(num_rvs):
                rv_type, seg_idx, tgt_idx = struct.unpack_from("<3h", raw, offset)
                rvs.append(SegParam(SegParamType(rv_type), seg_idx, tgt_idx))
                offset += struct.calcsize("<3h")

            len_path = struct.unpack_from("<H", raw, offset)[0]
            offset += struct.calcsize("<H")
            path_bytes, num_segs, num_tgts, num_perts, num_sects, record_seg, skip_seg, file_version, xy_seed = \
                struct.unpack_from(f"<{len_path}s7hi", raw, offset)
            offset += struct.calcsize(f"<{len_path}s7hi")
            path_parts: List[str] = path_bytes.decode('ascii').split('/')
            if len(path_parts) == 1:
                set_name, subset_name, trial_name = None, None, path_parts[0]
            elif len(path_parts) == 2:
                set_name, subset_name, trial_name = path_parts[0], None, path_parts[1]
            elif len(path_parts) == 3:
                set_name, subset_name, trial_name = path_parts[0], path_parts[1], path_parts[2]
            else:
                raise DataFileError("Bad trial path name found in serialized protocol definition")

            xfm = TargetTransform(raw, offset)
            offset += TargetTransform.size_in_bytes()

            targets: List[Target] = list()
            for _ in range(num_tgts):
                tgt = Target.from_bytes(raw, offset)
                offset += tgt.size_in_bytes()
                targets.append(tgt)
            sections: List[TaggedSection] = list()
            sect_size = TaggedSection.size_in_bytes()
            for _ in range(num_sects):
                sections.append(TaggedSection(raw, offset))
                offset += sect_size
            perts: List[Perturbation] = list()
            pert_size = Perturbation.size_in_bytes()
            for _ in range(num_perts):
                perts.append(Perturbation.from_bytes(raw[offset:offset+pert_size]))
                offset += pert_size
            segments: List[Segment] = list()
            seg_size = Segment.size_in_bytes(num_targets=num_tgts)
            for _ in range(num_segs):
                segments.append(Segment.from_bytes(raw, offset))
                offset += seg_size

            trial = Trial(name=trial_name, set_name=set_name, subset_name=subset_name, segments=tuple(segments),
                          targets=tuple(targets), perts=tuple(perts), sections=tuple(sections), record_seg=record_seg,
                          skip_seg=skip_seg, file_version=file_version, xy_seed=xy_seed, global_transform=xfm)
            protocol = Protocol(trial=trial)
            protocol._num_reps = num_reps
            protocol._rvs = rvs
            protocol._md5_digest = md5_digest
            return protocol
        except DataFileError:
            raise
        except Exception as e:
            raise DataFileError(f"Unexpected failure while parsing trial protocol definition: {str(e)}")
