"""
maestro.py: Structures and functions for digesting Maestro trial data files.

TODO: Only implementing support for digesting Trial-mode data files. Cannot be used to process files recorded in
 Continuous mode. Need to comment code throughout

@author: sruffner
@created: 08dec2020
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import sys
import traceback
from typing import NamedTuple, List, Optional, Dict, Any, Tuple, Union, Set
from datetime import date
import struct
import re
import math
import zipfile
import hashlib
import pickle
from dash import html
from dash import dash_table as dt
import dash_bootstrap_components as dbc
import numpy as np

from utils.common import DocEnum


class DataFileError(Exception):
    def __init__(self, reason: Optional[str] = None):
        self.message = reason if reason else "Undefined error"

    def __str__(self):
        return self.message


# CONSTANTS
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
    file_name: str
    header: DataFileHeader
    ai_data: Dict[int, List[int]]
    spike_wave: Optional[List[int]]
    trial: Trial
    events: Optional[Dict[int, List[float]]]
    blinks: Optional[List[int]]
    sorted_spikes: Optional[Dict[int, List[float]]]

    @staticmethod
    def load(content: bytes, file_name: str) -> DataFile:
        data: Dict[str, Any] = dict()
        num_total_bytes = len(content)
        if (num_total_bytes % RECORD_SIZE) != 0:
            raise DataFileError(f"Maestro data file size in bytes ({num_total_bytes}) is not a multiple of 1024!")
        header = DataFileHeader.parse_header(content)
        if (header.version < 21) or header.is_continuous_mode():
            raise DataFileError("No support for version<21 Maestro data files or files recorded in Continuous mode!")
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

            # Process JMW/XWork actions, if any TODO

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
        data: Dict[str, Any] = dict()
        num_total_bytes = len(content)
        if (num_total_bytes % RECORD_SIZE) != 0:
            raise DataFileError(f"Maestro data file size in bytes ({num_total_bytes}) is not a multiple of 1024!")
        header = DataFileHeader.parse_header(content)
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
            # TODO: DEBUGGING
            traceback.print_exc(file=sys.stdout)
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


class DataFileHeader(NamedTuple):
    trial_name: str
    num_ai_channels: int
    channel_list: List[int]
    display_height_pix: int
    display_width_pix: int
    display_distance_mm: int
    display_width_mm: int
    display_height_mm: int
    display_framerate_hz: float
    pos_scale: float
    pos_theta: float
    vel_scale: float
    vel_theta: float
    reward_len1_ms: int
    reward_len2_ms: int
    date_recorded: date
    version: int
    flags: int
    num_bytes_compressed: int
    num_scans_saved: int
    num_spike_bytes_compressed: int
    spike_sample_intv_us: int
    xy_random_seed: int
    rp_distro_start: int
    rp_distro_dur: int
    rp_distro_response: int
    rp_distro_windows: List[int]
    rp_distro_response_type: int
    horizontal_start_pos: float
    vertical_start_pos: float
    trial_flags: int
    search_target_selected: int
    velocity_stab_window_len_ms: int
    eyelink_info: List[int]
    trial_set_name: str
    trial_subset_name: str
    rmvideo_sync_size_mm: int
    rmvideo_sync_dur_frames: int
    timestamp_ms: int
    rmvideo_duplicate_events: List[int]

    @staticmethod
    def parse_header(record: bytes) -> DataFileHeader:
        header_format = f"<{MAX_NAME_SIZE}s5h{MAX_AI_CHANNELS}h7h11iI3i{MAX_NAME_SIZE}s2iI10iI11i" \
                        f"{MAX_NAME_SIZE}s{MAX_NAME_SIZE}s2hi{RMVIDEO_DUPE_SZ}i"
        try:
            raw_fields = struct.unpack_from(header_format, record, 0)
            version = raw_fields[39]
            if version < 2:
                raise DataFileError("Data file version 1 or earlier is not supported")
            kept_fields = list()
            idx = 0
            kept_fields.append(raw_fields[idx].decode('ascii').split('\0', 1)[0])  # name
            idx += 5   # skip obsolete fields trhdir, trvdir, nchar, npdig
            kept_fields.append(raw_fields[idx])   # nchans
            idx += 1
            kept_fields.append(raw_fields[idx:idx+MAX_AI_CHANNELS])   # chlist (array)
            idx += MAX_AI_CHANNELS
            kept_fields.extend(raw_fields[idx:idx+2])   # d_rows, d_cols
            idx += 4   # skips ignored fields d_crow, d_ccol
            kept_fields.extend(raw_fields[idx:idx+3])   # d_dist, d_dwidth, d_dheight
            idx += 3
            # d_framerate - convert to Hz, preserving precision, which changes from milli- to micro-Hz in V=22
            kept_fields.append(float(raw_fields[idx]) / (1.0e6 if version >= 22 else 1.0e3))
            idx += 1
            # iPosScale .. iVelTheta: The raw values are scaled by 1000
            kept_fields.extend([float(raw_fields[idx+i]) / 1000.0 for i in range(4)])
            idx += 4
            kept_fields.extend(raw_fields[idx:idx+2])   # iRewLen1, iRewLen2
            idx += 2
            kept_fields.append(date(raw_fields[idx+2], raw_fields[idx+1], raw_fields[idx]))  # day/month/yearRecorded
            idx += 3
            kept_fields.extend(raw_fields[idx:idx+2])   # version, flags
            idx += 3   # nScanIntvUS skipped b/c it is always 1000 (trials) or 2000 (continuous)
            kept_fields.extend(raw_fields[idx:idx+2])   # nBytesCompressed, nScansSaved
            idx += 3   # spikesFName skipped b/c we won't support old spikesPC file
            kept_fields.extend(raw_fields[idx:idx+6])   # nSpikeBytesCompressed .. iRPDResponse
            idx += 6
            kept_fields.append(raw_fields[idx:idx+4])   # iRPDWindows (int array of size 4)
            idx += 4
            kept_fields.append(raw_fields[idx])   # iRPDRespType
            idx += 1
            # iStartPosH, iStartPosV: The raw values are scaled by 1000
            kept_fields.extend([float(raw_fields[idx+i]) / 1000.0 for i in range(2)])
            idx += 2
            kept_fields.extend(raw_fields[idx:idx+3])  # dwTrialFlags, iSTSelected, iVStabWinLen
            idx += 3
            kept_fields.append(raw_fields[idx:idx+9])  # iELInfo (int array of size 9)
            idx += 9
            kept_fields.append(raw_fields[idx].decode('ascii').split('\0', 1)[0])  # setName
            kept_fields.append(raw_fields[idx+1].decode('ascii').split('\0', 1)[0])  # subsetName
            idx += 2
            kept_fields.extend(raw_fields[idx:idx+3])  # rmvSyncSz, rmvSyncDur, timestampMS
            idx += 3
            kept_fields.append(raw_fields[idx:idx+RMVIDEO_DUPE_SZ])   # rmvDupEvents (int array)
            idx += RMVIDEO_DUPE_SZ

            return DataFileHeader._make(kept_fields)
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing data file header: {str(err)}")

    def is_continuous_mode(self):
        return (self.flags & FLAG_IS_CONTINUOUS) != 0

    def is_eyelink_used(self):
        return (self.version >= 20) and ((self.flags & FLAG_EYELINK_USED) != 0)

    def global_transform(self) -> TargetTransform:
        return TargetTransform._make([self.horizontal_start_pos, self.vertical_start_pos, self.pos_scale,
                                      self.pos_theta, self.vel_scale, self.vel_theta])


class TargetTransform(NamedTuple):
    pos_offsetH_deg: float
    pos_offsetV_deg: float
    pos_scale: float
    pos_rotate_deg: float
    vel_scale: float
    vel_rotate_deg: float

    def __eq__(self, other: TargetTransform) -> bool:
        """
        Two target transforms are equal if their corresponding parameters are "close enough" (using math.isclose()).
        """
        return (self.__class__ == other.__class__) and math.isclose(self.pos_offsetH_deg, other.pos_offsetH_deg) and \
            math.isclose(self.pos_offsetV_deg, other.pos_offsetV_deg) and \
            math.isclose(self.pos_scale, other.pos_scale) and \
            math.isclose(self.pos_rotate_deg, other.pos_rotate_deg) and \
            math.isclose(self.vel_scale, other.vel_scale) and \
            math.isclose(self.vel_rotate_deg, other.vel_rotate_deg)

    def __hash__(self) -> int:
        return(hash((self.pos_offsetH_deg, self.pos_offsetV_deg, self.pos_scale, self.pos_rotate_deg,
                     self.vel_scale, self.vel_rotate_deg)))

    def __str__(self) -> str:
        """
        Returns compact string representation of target transform: '[(A, B); pos=C, D deg; vel=E, F deg]', where (A, B)
        are the horizontal and vertical initial position offsets; C is the position scale factor, D is the position
        rotation angle, E is the velocity scale factor, and F is the velocity rotation angle.
        """
        ofs_x = f"{self.pos_offsetH_deg:.2f}".rstrip('0').rstrip('.')
        ofs_y = f"{self.pos_offsetV_deg:.2f}".rstrip('0').rstrip('.')
        pos_scale = f"{self.pos_scale:.2f}".rstrip('0').rstrip('.')
        pos_rotate = f"{self.pos_rotate_deg:.2f}".rstrip('0').rstrip('.')
        vel_scale = f"{self.vel_scale:.2f}".rstrip('0').rstrip('.')
        vel_rotate = f"{self.vel_rotate_deg:.2f}".rstrip('0').rstrip('.')
        return f"[({ofs_x},{ofs_y}); pos={pos_scale}, {pos_rotate} deg; vel={vel_scale}, {vel_rotate} deg]"

    def is_identity_for_pos(self) -> bool:
        """
        Is this target transform the identity (unity scale, zero rotation) WRT target position? By convention, a
        rotation within 0.01 deg of zero and a scale factor within 0.01 of unity is considered an identity transform.
        """
        return (abs(self.pos_scale - 1) < 0.01) and (abs(self.pos_rotate_deg) < 0.01)

    def is_identity_for_vel(self) -> bool:
        """
        Is this target transform the identity (unity scale, zero rotation) WRT target velocity? By convention, a
        rotation within 0.01 deg of zero and a scale factor within 0.01 of unity is considered an identity transform.
        """
        return (abs(self.vel_scale - 1) < 0.01) and (abs(self.vel_rotate_deg) < 0.01)

    def transform_position(self, p: Point2D) -> None:
        """
        Rotate and scale a target position vector IAW this global target transform.

        Args:
            p: Target position vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_pos():
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta += self.pos_rotate_deg * math.pi / 180.0
        amp = p.distance_from(0, 0) * self.pos_scale
        p.set(amp*math.cos(theta), amp*math.sin(theta))

    def transform_velocity(self, p: Point2D) -> None:
        """
        Rotate and scale a target velocity vector IAW this global target transform.

        Args:
            p: Target velocity vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_vel():
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta += self.vel_rotate_deg * math.pi / 180.0
        amp = p.distance_from(0, 0) * self.vel_scale
        p.set(amp*math.cos(theta), amp*math.sin(theta))

    def invert_position(self, p: Point2D) -> None:
        """
        Rotate and scale a target position vector IAW the inverse of this global target transform.
        Args:
            p: Target position vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_pos():
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta -= self.pos_rotate_deg * math.pi / 180.0
        amp = 0 if (self.pos_scale == 0) else (p.distance_from(0, 0) / self.pos_scale)
        p.set(amp * math.cos(theta), amp * math.sin(theta))

    def invert_velocity(self, p: Point2D) -> None:
        """
        Rotate and scale a target velocity vector IAW the inverse of this global target transform.

        Args:
            p: Target velocity vector (x,y). Updated in place.
        """
        if (p is None) or self.is_identity_for_vel():
            return
        theta = 0 if (p.x == 0) and (p.y == 0) else math.atan2(p.y, p.x)
        theta -= self.vel_rotate_deg * math.pi / 180.0
        amp = 0 if (self.vel_scale == 0) else (p.distance_from(0, 0) / self.vel_scale)
        p.set(amp * math.cos(theta), amp * math.sin(theta))


SECTION_TAG_SIZE = 18  # max length of tagged section label in a TAG_SECT_RECORD
MAX_SEGMENTS = 30  # max number of segments allowed in a Maestro trial


class TaggedSection(NamedTuple):
    """
    Immutable representation of a tagged section in a Maestro trial, as culled from a Maestro trial data file.
    """
    start_seg: int
    end_seg: int
    label: str

    def __eq__(self, other: TaggedSection) -> bool:
        return ((self.__class__ == other.__class__) and (self.start_seg == other.start_seg) and
                (self.end_seg == other.end_seg) and (self.label == other.label))

    def __hash__(self) -> int:
        return hash((self.start_seg, self.end_seg, self.label))

    def __str__(self) -> str:
        return f"{self.label} [{self.start_seg}:{self.end_seg}]"

    @staticmethod
    def parse_tagged_sections(record: bytes) -> List[TaggedSection]:
        """
        Parse one or more tagged sections from a Maestro data file record (there will be only one such record in the
        data file, and only if the trial therein contains one or more tagged sections).

        Args:
            record: A data file record. Record tag ID must be TAG_SECT_RECORD.
        Returns:
            List[TaggedSection] - List of one or more tagged sections culled from the record.
        Raises:
            DataFileError if an error occurs while parsing the record
        """
        try:
            if record[0] != TAG_SECT_RECORD:
                raise DataFileError("Not a tagged section record!")
            sect_format = f"<{SECTION_TAG_SIZE}sbb"
            sect_size = struct.calcsize(sect_format)
            sections = []
            idx = RECORD_TAG_SIZE
            while (idx + sect_size < RECORD_SIZE) and (record[idx] != 0):
                label_bytes, start_seg, end_seg = struct.unpack_from(sect_format, record, idx)
                label_str = label_bytes.decode('ascii').split('\0', 1)[0]
                if (start_seg < 0) or (start_seg > end_seg) or (end_seg >= MAX_SEGMENTS):
                    raise DataFileError("Invalid tagged section found")
                sections.append(TaggedSection._make([start_seg, end_seg, label_str]))
            return sections
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure: {str(err)}")

    @staticmethod
    def validate_tagged_sections(sections: Optional[List[TaggedSection]], num_segs: int) -> bool:
        """
        Verify that no tagged section overlaps another section in the list provided, and verify that each section's
        span is valid.
        Args:
            sections: List of tagged sections in a Maestro trial. Could be None or empty list.
            num_segs: Number of segments in the trial

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


def validate_range(value: float, min_value: float, max_value: float, tol: float = 1e-6) -> bool:
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


class Target(NamedTuple):
    """
    Immutable representation of a Maestro target definition, as culled from a Maestro data file.
    """
    hardware_type: int
    name: str
    definition: Optional[XYScopeTarget, VSGVideoTarget, RMVideoTarget]

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
        max_def_fmt = VSGVideoTarget.struct_format() if version <= 7 else RMVideoTarget.struct_format(version)
        return struct.calcsize(f"<H{MAX_TGT_NAME_SIZE}s{max_def_fmt}L2f")

    @staticmethod
    def parse_targets(record: bytes, version: int) -> List[Target]:
        """
        Parse one or more Maestro target definitions listed in the specified record culled from a Maestro data file.
        Each target "block" within the record has the same size, regardless the target type. The target block size
        has changed over the course of Maestro's development, and that block size determines how many target
        definitions can be stored in 1KB record. Target definitions do NOT cross record boundaries.

        Args:
            record: The 1KB target record (tag = TGT_RECORD)
            version: The data file version number. This is required b/c the exact layout of the target record has
                evolved over time.
        Returns:
            List[Target] - The target definitions found in the record (order preserved).
        Raises:
            DataFileError - If an error occurs while parsing the record
        """
        tgt_list = []
        block_size = Target._block_size(version)
        offset = RECORD_TAG_SIZE
        try:
            while (offset + block_size) < RECORD_SIZE:
                # unpack hardware type and target name. If hardware type is 0, we've reached end of target list
                hardware, name_bytes = struct.unpack_from(f"<H{MAX_TGT_NAME_SIZE}s", record, offset)
                if hardware == 0:
                    break
                if not (CX_CHAIR <= hardware <= CX_RMV_TGT):
                    raise DataFileError(f"Unrecognized target hardware type ({hardware})")
                name = name_bytes.decode("ascii").split('\0', 1)[0]
                offset_to_def = struct.calcsize(f"<H{MAX_TGT_NAME_SIZE}s")

                # unpack video target definition, if applicable
                tgt_def = None
                if hardware == CX_XY_TGT:
                    tgt_def = XYScopeTarget.parse_definition(record, offset + offset_to_def, version)
                elif hardware == CX_RMV_TGT:
                    if version < 8:
                        tgt_def = VSGVideoTarget.parse_definition(record, offset + offset_to_def)
                    else:
                        tgt_def = RMVideoTarget.parse_definition(record, offset + offset_to_def, version)

                tgt_list.append(Target._make([hardware, name, tgt_def]))
                offset += block_size
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure while parsing target record: {str(err)}")
        if len(tgt_list) == 0:
            raise DataFileError("Found no target definitions in a Maestro target record")
        return tgt_list


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


class XYScopeTarget(NamedTuple):
    """
    Immutable representation of an XYScope target definition, as culled from a Maestro data file.
    """
    type: int
    n_dots: int
    dot_life_in_ms: bool
    dot_life: float
    width: float
    height: float
    inner_width: float
    inner_height: float
    inner_x: float          # these two fields were added in file version 9
    inner_y: float

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

    @staticmethod
    def struct_format(version: int) -> str:
        return "3i7f" if version >= 9 else "3i5f"

    @staticmethod
    def parse_definition(record: bytes, offset: int, version: int) -> XYScopeTarget:
        try:
            xy_fmt = "<" + XYScopeTarget.struct_format(version)
            raw_fields = struct.unpack_from(xy_fmt, record, offset)
            # convert "dot life in ms?" field to bool.
            adj_fields = []
            adj_fields.extend(raw_fields[0:2])
            adj_fields.append((raw_fields[2] == 0))
            adj_fields.extend(raw_fields[3:])
            if version < 9:         # add defaults for fields added in version 9 if file version is older
                adj_fields.extend([0.0, 0.0])
            target = XYScopeTarget._make(adj_fields)
            if not target._is_valid():
                raise DataFileError("Invalid XYScope target definition found")
            return target
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure: {str(err)}")

    def _is_valid(self) -> bool:
        ok = (self.type >= XY_RECT_DOT) and (self.type < NUM_XY_TYPES) and (self.n_dots > 0)
        if ok and (self.type in [XY_FC_DOT_LIFE, XY_NOISY_DIR, XY_NOISY_SPEED]):
            ok = validate_range(self.dot_life, 0, MAX_DOT_LIFE_MS if self.dot_life_in_ms else MAX_DOT_LIFE_DEG)
        if ok and (self.type != XY_RECT_DOT):
            if self.type == XY_FLOW_FIELD:
                ok = validate_range(self.width, MIN_FLOW_RADIUS_DEG, MAX_FLOW_RADIUS_DEG)
            else:
                ok = validate_range(self.width, MIN_RECT_DIM_DEG, float('inf'))
        if ok and not (self.type in [XY_RECT_DOT, XY_FLOW_FIELD]):
            ok = validate_range(self.height, MIN_RECT_DIM_DEG, float('inf'))
        if ok:
            if self.type == XY_RECTANNU:
                ok = validate_range(self.inner_width, MIN_RECT_DIM_DEG, float('inf'))
            elif self.type == XY_FLOW_FIELD:
                ok = validate_range(self.inner_width, MIN_FLOW_RADIUS_DEG, MAX_FLOW_RADIUS_DEG)
                ok = ok and validate_range(self.width - self.inner_width, MIN_FLOW_DIFF_DEG, float('inf'))
            elif self.type == XY_ORIENTED_BAR:
                ok = validate_range(self.inner_width, 0, MAX_BAR_DRIFT_AXIS_DEG)
            elif self.type == XY_NOISY_DIR:
                ok = validate_range(self.inner_width, 0, MAX_DIR_OFFSET)
            elif self.type == XY_NOISY_SPEED:
                ok = validate_range(self.inner_width, 0, MAX_SPEED_OFFSET) if (int(self.inner_x) == 0) else \
                    (MIN_SPEED_LOG2 <= int(self.inner_width) <= MAX_SPEED_LOG2)
            elif self.type == XY_FC_COHERENT:
                ok = validate_range(self.inner_width, 0, 100)
            if self.type == XY_RECTANNU:
                ok = validate_range(self.inner_height, MIN_RECT_DIM_DEG, float('inf'))
            elif self.type in [XY_NOISY_DIR, XY_NOISY_SPEED]:
                ok = (MIN_NOISE_UPDATE_MS <= int(self.inner_height) <= MAX_NOISE_UPDATE_MS)
        return ok


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


class VSGVideoTarget(NamedTuple):
    """
    Immutable representation of a VSG2/3 video target, as culled from a Maestro data file. Applicable only to Maestro
    data file versions 7 or earlier. The VSG2/3 hardware was deprecated as of file version 8.
    """
    type: int
    is_rect: bool
    rgb_mean: List[int]
    rgb_contrast: List[int]
    width: float
    height: float
    sigma: float
    spatial_frequency: List[float]
    drift_axis: List[float]
    spatial_phase: List[float]

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

    @staticmethod
    def struct_format() -> str:
        return "8i9f"

    @staticmethod
    def parse_definition(record: bytes, offset: int) -> VSGVideoTarget:
        try:
            raw_fields = struct.unpack_from("<" + VSGVideoTarget.struct_format(), record, offset)
            # convert second field to bool, pack array fields
            adj_fields = [raw_fields[0], (raw_fields[1] == VSG_RECT_WINDOW), raw_fields[2:5], raw_fields[5:8],
                          raw_fields[8], raw_fields[9], raw_fields[10], raw_fields[11:13], raw_fields[13:15],
                          raw_fields[15:17]]
            target = VSGVideoTarget._make(adj_fields)
            if not target._is_valid():
                raise DataFileError("Invalid VSG video target definition found")
            return target
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure: {str(err)}")

    def _is_valid(self) -> bool:
        ok = (self.type >= VSG_PATCH) and (self.type < NUM_VSG_TYPES)
        if ok:
            for i in range(3):
                ok = (0 <= self.rgb_mean[i] <= VSG_MAX_LUM) and (0 <= self.rgb_contrast[i] <= VSG_MAX_CON)
                if not ok:
                    break
        return ok


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
RMV_FILENAME_PATTERN = re.compile(r'[a-zA-Z0-9_\\.]+')
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


class RMVideoTarget(NamedTuple):
    """
    Immutable representation of a Remote Maestro video (RMVideo) target, as culled from a Maestro data file. Applicable
    only to Maestro data file versions 8 or later. The RMVideo system replaced VSG2/3 hardware as of file version 8.
    """
    type: int
    aperture: int
    flags: int
    rgb_mean: List[int]
    rgb_contrast: List[int]
    outer_w: float
    outer_h: float
    inner_w: float
    inner_h: float
    num_dots: int
    dot_size: int
    seed: int
    percent_coherent: int
    noise_update_intv: int
    noise_limit: int
    dot_life: float
    spatial_frequency: List[float]
    drift_axis: List[float]
    spatial_phase: List[float]
    sigma: List[float]
    media_folder: str = ""          # next two fields added in file version 13
    media_file: str = ""
    flicker_on_dur: int = 0         # next three fields added in file version 23
    flicker_off_dur: int = 0
    flicker_delay: int = 0

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

    @staticmethod
    def struct_format(version: int) -> str:
        return "7i4f6i9f32s32s3i" if version > 22 else ("7i4f6i9f32s32s" if version > 12 else "7i4f6i9f")

    @staticmethod
    def parse_definition(record: bytes, offset: int, version: int) -> RMVideoTarget:
        try:
            if version < 8:
                raise DataFileError("RMVideo targets not supported for data file versions 7 and earlier")
            rmv_fmt = "<" + RMVideoTarget.struct_format(version)
            raw_fields = struct.unpack_from(rmv_fmt, record, offset)
            # pack the various array fields
            adj_fields = []
            adj_fields.extend(raw_fields[0:3])
            adj_fields.append(raw_fields[3:5])
            adj_fields.append(raw_fields[5:7])
            adj_fields.extend(raw_fields[7:18])
            adj_fields.append(raw_fields[18:20])
            adj_fields.append(raw_fields[20:22])
            adj_fields.append(raw_fields[22:24])
            adj_fields.append(raw_fields[24:26])
            # process additional fields added in versions 13, 23; for earlier versions, use default values. NOTE that
            # the folder, file names MUST be set to "" if the type is neither RMV_MOVIE or RMV_IMAGE, because they may
            # contain garbage bytes otherwise!
            valid_file_folder = (version > 12) and ((raw_fields[0] == RMV_MOVIE) or (raw_fields[0] == RMV_IMAGE))
            adj_fields.append(raw_fields[26].decode('ascii').split('\0', 1)[0] if valid_file_folder else "")
            adj_fields.append(raw_fields[27].decode('ascii').split('\0', 1)[0] if valid_file_folder else "")
            adj_fields.extend(raw_fields[28:31] if version > 22 else [0, 0, 0])

            target = RMVideoTarget._make(adj_fields)
            if not target._is_valid(version):
                raise DataFileError("Invalid RMVideo target definition found")
            return target
        except DataFileError:
            raise
        except Exception as err:
            raise DataFileError(f"Unexpected failure: {str(err)}")

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
            ok = ok and validate_range(self.spatial_frequency[0], 0.01, float('inf'))
            if ok and (self.type == RMV_PLAID):
                con = self.rgb_contrast[1]
                ok = ok and ((con & 0x0FF) <= 100) and (((con >> 8) & 0x0FF) <= 100) and (((con >> 16) & 0x0FF) <= 100)
                ok = ok and validate_range(self.spatial_frequency[1], 0.01, float('inf'))
        ok = ok and validate_range(self.outer_w, (0 if self.type == RMV_BAR else RMV_MIN_RECT_DIM), RMV_MAX_RECT_DIM)
        ok = ok and validate_range(self.outer_h, RMV_MIN_RECT_DIM, RMV_MAX_RECT_DIM)
        ok = ok and validate_range(self.inner_w, RMV_MIN_RECT_DIM, RMV_MAX_RECT_DIM)
        ok = ok and validate_range(self.inner_h, RMV_MIN_RECT_DIM, RMV_MAX_RECT_DIM)
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
            ok = ok and validate_range(self.dot_life, 0, float('inf'))
            if ok and ((self.flags & RMV_F_DIR_NOISE) != 0):
                ok = (0 <= self.noise_limit <= RMV_MAX_NOISE_DIR)
            if ok and ((self.flags & RMV_F_DIR_NOISE) == 0):
                min_speed = RMV_MIN_SPEED_LOG2 if (self.flags & RMV_F_SPEED_LOG2) != 0 else 0
                max_speed = RMV_MAX_SPEED_LOG2 if (self.flags & RMV_F_SPEED_LOG2) != 0 else RMV_MAX_NOISE_SPEED
                ok = (min_speed <= self.noise_limit <= max_speed)
        if ok and (self.type in [RMV_SPOT, RMV_RANDOM_DOTS, RMV_GRATING, RMV_PLAID]):
            ok = validate_range(self.sigma[0], 0, float('inf')) and validate_range(self.sigma[1], 0, float('inf'))
        return ok


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
        self.x: float = x
        self.y: float = y

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
        self.x = x
        self.y = y

    def offset_by(self, x_ofs: float, y_ofs: float) -> None:
        self.x += x_ofs
        self.y += y_ofs

    def distance_from(self, x_ref: float, y_ref: float) -> float:
        x_ref -= self.x
        y_ref -= self.y
        return math.sqrt(x_ref*x_ref + y_ref*y_ref)

    def is_origin(self) -> bool:
        return math.isclose(self.x, 0) and math.isclose(self.y, 0)


class Trial(NamedTuple):
    name: str
    set_name: Optional[str]     # trial set and subset names added to data file in V=21
    subset_name: Optional[str]
    segments: List[Trial.Segment]
    targets: List[Target]
    perts: List[Trial.Perturbation]
    sections: List[TaggedSection]
    record_seg: int
    skip_seg: int
    file_version: int
    xy_seed: int
    global_transform: TargetTransform

    class Segment:
        """
        A single segment within the segment table of a Maestro trial.
        """
        def __init__(self, num_targets: int, prev_seg: Optional[Trial.Segment]):
            self.dur: int = 0
            """ The segment duration in milliseconds. """
            self.pulse_ch: int = -1
            """ Digital output channel number for marker pulse delivered at segment start (-1 if no marker pulse). """
            self.fix1: int = -1
            self.fix2: int = -1
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
            return len(self.tgt_on)

        def value_of(self, param_type: SegParamType, tgt: int) -> Union[int, float, bool, None]:
            # NOTE: Avoided dispatch table implementation here b/c I need to be able to pickle Trial object
            if param_type.is_target_trajectory_parameter() and not (0 <= tgt < self.num_targets()):
                return None
            elif param_type == SegParamType.DURATION:
                return self.dur
            elif param_type == SegParamType.MARKER:
                return self.pulse_ch
            elif param_type == SegParamType.FIX_TGT1:
                return self.fix1
            elif param_type == SegParamType.FIX_TGT2:
                return self.fix2
            elif param_type == SegParamType.XY_UPDATE_INTV:
                return self.xy_update_intv
            elif param_type == SegParamType.TGT_ON_OFF:
                return self.tgt_on[tgt]
            elif param_type == SegParamType.TGT_REL:
                return self.tgt_rel[tgt]
            elif param_type == SegParamType.TGT_VSTAB:
                return self.tgt_vel_stab_mask[tgt]
            elif param_type == SegParamType.TGT_POS_H:
                return self.tgt_pos[tgt].x
            elif param_type == SegParamType.TGT_POS_V:
                return self.tgt_pos[tgt].y
            elif param_type == SegParamType.TGT_VEL_H:
                return self.tgt_vel[tgt].x
            elif param_type == SegParamType.TGT_VEL_V:
                return self.tgt_vel[tgt].y
            elif param_type == SegParamType.TGT_ACC_H:
                return self.tgt_acc[tgt].x
            elif param_type == SegParamType.TGT_ACC_V:
                return self.tgt_acc[tgt].y
            elif param_type == SegParamType.TGT_PAT_VEL_H:
                return self.tgt_pat_vel[tgt].x
            elif param_type == SegParamType.TGT_PAT_VEL_V:
                return self.tgt_pat_vel[tgt].y
            elif param_type == SegParamType.TGT_PAT_ACC_H:
                return self.tgt_pat_acc[tgt].x
            elif param_type == SegParamType.TGT_PAT_ACC_V:
                return self.tgt_pat_acc[tgt].y
            return None

        def set_value_of(self, param_type: SegParamType, tgt: int, value: Union[int, float, bool]) -> None:
            # NOTE: Avoided dispatch table implementation here b/c I need to be able to pickle Trial object
            if param_type.is_target_trajectory_parameter() and not (0 <= tgt < self.num_targets()):
                return
            elif param_type == SegParamType.DURATION:
                self.dur = int(value)
            elif param_type == SegParamType.MARKER:
                self.pulse_ch = int(value)
            elif param_type == SegParamType.FIX_TGT1:
                self.fix1 = int(value)
            elif param_type == SegParamType.FIX_TGT2:
                self.fix2 = int(value)
            elif param_type == SegParamType.XY_UPDATE_INTV:
                self.xy_update_intv = int(value)
            elif param_type == SegParamType.TGT_ON_OFF:
                self.tgt_on[tgt] = bool(value)
            elif param_type == SegParamType.TGT_REL:
                self.tgt_rel[tgt] = bool(value)
            elif param_type == SegParamType.TGT_VSTAB:
                self.tgt_vel_stab_mask[tgt] = int(value)
            elif param_type == SegParamType.TGT_POS_H:
                self.tgt_pos[tgt].x = float(value)
            elif param_type == SegParamType.TGT_POS_V:
                self.tgt_pos[tgt].y = float(value)
            elif param_type == SegParamType.TGT_VEL_H:
                self.tgt_vel[tgt].x = float(value)
            elif param_type == SegParamType.TGT_VEL_V:
                self.tgt_vel[tgt].y = float(value)
            elif param_type == SegParamType.TGT_ACC_H:
                self.tgt_acc[tgt].x = float(value)
            elif param_type == SegParamType.TGT_ACC_V:
                self.tgt_acc[tgt].y = float(value)
            elif param_type == SegParamType.TGT_PAT_VEL_H:
                self.tgt_pat_vel[tgt].x = float(value)
            elif param_type == SegParamType.TGT_PAT_VEL_V:
                self.tgt_pat_vel[tgt].y = float(value)
            elif param_type == SegParamType.TGT_PAT_ACC_H:
                self.tgt_pat_acc[tgt].x = float(value)
            elif param_type == SegParamType.TGT_PAT_ACC_V:
                self.tgt_pat_acc[tgt].y = float(value)

        def summary(self) -> Dict[str, Any]:
            """
            Generate a summary of this trial segment for display purposes only. Returns a dictionary with the following
            fields: 'dur' is the segment duration in ms (int); 'fix1' and 'fix2' are the target indices of the two
            designated fixation targets during segment (int; -1 = 'None'); 'xy_update' is the XYScope update interval
            (if applicable) during segment, in ms (int); 'marker' is the DO pulse channel on which marker pulse is
            delivered at segment start (int; -1 = 'None'). Lastly, trajectories' is a list, with the i-the element a
            dictionary describing the trajectory of the i-th target during the segment: 'on' indicates whether that
            target is on during the segment (bool), 'vstab' is a description of the target's velocity stabilization
            status (str), 'pos' is the target's (x,y) position change in visual degrees at the start of the segment,
            including whether that change is relative or absolute (str), 'vel' is the target's (x,y) velocity in
            deg/sec, 'acc' is its (x,y) acceleration in deg/sec^2, 'patvel' is its (x,y) pattern velocity, and 'patacc'
            is its pattern acceleration.
            """
            out = {'dur': self.dur, 'fix1': self.fix1, 'fix2': self.fix2, 'xy_update': self.xy_update_intv,
                   'marker': self.pulse_ch}
            trajectories = list()
            for i in range(self.num_targets()):
                trajectory = {'on': self.tgt_on[i],
                              'pos': f"{str(self.tgt_pos[i])} {'rel' if self.tgt_rel[i] else 'abs'}",
                              'vel': f"{str(self.tgt_vel[i])}",
                              'acc': f"{str(self.tgt_acc[i])}",
                              'patvel': f"{str(self.tgt_pat_vel[i])}",
                              'patacc': f"{str(self.tgt_pat_acc[i])}",
                              }
                if not (self.tgt_vel_stab_mask[i] & VEL_STAB_ON):
                    trajectory['vstab'] = 'OFF'
                else:
                    is_snap = (self.tgt_vel_stab_mask[i] & VEL_STAB_SNAP) != 0
                    is_h = (self.tgt_vel_stab_mask[i] & VEL_STAB_H) != 0
                    is_v = (self.tgt_vel_stab_mask[i] & VEL_STAB_V) != 0
                    trajectory['vstab'] = f"{'H' if is_h else ''}{'V' if is_v else ''} {'snap' if is_snap else ''}"
                trajectories.append(trajectory)
            out['trajectories'] = trajectories
            return out

    class Perturbation(NamedTuple):
        tgt_pos: int
        component: int
        seg_start: int     # index of segment at which perturbation begins
        amplitude: int     # in 0.1 deg/sec
        type: int
        dur: int
        extras: List[int]
        # for PERT_TYPE_SINE: [period in ms, phase in 0.01 deg]
        # for PERT_TYPE_TRAIN: [pulse dur in ms, ramp dur in ms, pulse intv in ms]
        # for PERT_TYPE_NOISE, _GAUSS: [noise update intv in ms, noise mean * 1000, noise seed]

        def __eq__(self, other: Trial.Perturbation) -> bool:
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

        @staticmethod
        def create_perturbation(codes: List[TrialCode], start: int, seg_idx: int) -> Optional[Trial.Perturbation]:
            if (start < 0) or ((start + 5) > len(codes)) or (codes[start].code != TC_TARGET_PERTURB):
                return None
            try:
                tgt_pos = codes[start+1].code
                component = (codes[start+1].time >> 4) & 0x0F
                if not (PERT_CMPT_H_WIN <= component <= PERT_CMPT_SPEED):
                    return None
                seg_start = seg_idx
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
                return Trial.Perturbation._make([tgt_pos, component, seg_start, amp, pert_type, dur, extras])
            except Exception:
                return None

    @staticmethod
    def prepare_trial(codes: List[TrialCode], header: DataFileHeader, targets: List[Target],
                      sections: Optional[List[TaggedSection]]) -> Trial:
        """
        Reconstruct the definition of a Maestro trial from the trial codes, targets, tagged sections, and file header
        culled from a Maestro data file. NOTE: We DO NOT invert the trial trajectory parameters IAW the global target
        transform found in the file header. Because the "similarity test" for two trial instances now requires that
        they have the same transform, there is no need to do so.
        Args:
            codes: The trial codes culled from data file
            header: The data file header
            targets: The participating trial target list.
            sections: List of tagged sections, or None if no sections are defined.

        Returns:
            The reconstructed trial definition.
        """
        segments: List[Trial.Segment] = []
        perturbations: List[Trial.Perturbation] = []
        record_seg_idx: int = -1
        skip_seg_idx: int = -1
        curr_segment: Optional[Trial.Segment] = None
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
                    curr_segment = Trial.Segment(len(targets), prev_segment)
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
                        pert = Trial.Perturbation.create_perturbation(codes, code_idx, len(segments)-1)
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
                    elif tc.code == TC_PULSE_ON:
                        # at segment start, deliver marker pulse on specified DO channel (N=2
                        curr_segment.pulse_ch = codes[code_idx+1].code
                        code_idx += 2
                    elif tc.code in [TC_ADC_OFF, TC_CHECK_RESP_OFF, TC_FAILSAFE, TC_START_TRIAL]:
                        # N=1 code groups that are not needed to prepare trial object
                        code_idx += 1
                    elif tc.code in [TC_FIX_ACCURACY, TC_REWARD_LEN, TC_MID_TRIAL_REW, TC_CHECK_RESP_ON, TC_RANDOM_SEED,
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

            # ensure any tagged sections span valid trial segments and do not overlap any other section
            if not TaggedSection.validate_tagged_sections(sections, len(segments)):
                raise DataFileError("Detected invalid or overlapping tagged sections in trial definition")

            # ensure any target perturbations identify valid targets in the list of participating trial targets
            for pert in perturbations:
                if (pert.tgt_pos < 0) or (pert.tgt_pos >= len(targets)):
                    raise DataFileError("Detected invalid perturbation target index in trial definition")

            # return the trial definition!
            set_name = header.trial_set_name if header.version >= 21 else None
            subset_name = header.trial_subset_name if header.version >= 21 else None
            return Trial._make([header.trial_name, set_name, subset_name, segments, targets, perturbations,
                                sections if sections else [], record_seg_idx, skip_seg_idx, header.version,
                                header.xy_random_seed, header.global_transform()])
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
        velocity stabilization masks must also match.

        Args:
            other: The trial to compare.
        Returns:
            True if this trial is similar to the other, as described.
        """
        similar = (other is not None) and (self.name == other.name) and (len(self.segments) == len(other.segments)) and\
                  (self.record_seg == other.record_seg) and (self.targets == other.targets) and \
                  (self.perts == other.perts) and (self.sections == other.sections) and \
                  (self.global_transform == other.global_transform)
        if similar and not (self.set_name is None):
            similar = (self.set_name == other.set_name) and (self.subset_name == other.subset_name)
        if not similar:
            return False
        for i, seg in enumerate(self.segments):
            other_seg = other.segments[i]
            if (seg.pulse_ch != other_seg.pulse_ch) or (seg.fix1 != other_seg.fix1) or (seg.fix2 != other_seg.fix2) or \
               (self.uses_xy_scope() and (seg.xy_update_intv != other_seg.xy_update_intv)):
                return False
            for j in range(len(self.targets)):
                if (seg.tgt_on[j] != other_seg.tgt_on[j]) or (seg.tgt_rel[j] != other_seg.tgt_rel[j]) or \
                   (seg.tgt_vel_stab_mask[j] != other_seg.tgt_vel_stab_mask[j]):
                    return False
        return True

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

        if self.set_name is None:
            return self.name
        elif len(self.subset_name) > 0:
            return "/".join([self.set_name, self.subset_name, self.name])
        else:
            return "/".join([self.set_name, self.name])

    def uses_xy_scope(self) -> bool:
        """
        Does this trial use targets presented on Maestro's older XYScope video platform?

        Returns:
            True if any of the trial's participating targets use the XYScope platform.
        """
        for target in self.targets:
            if target.hardware_type == CX_XY_TGT:
                return True
        return False

    def uses_fix1(self) -> bool:
        """
        Does this trial designate a participating target as fixation target #1 during any segment of the trial? The
        target must also be turned on in at least one segment in which it is designated as fixation target #1.
        """
        for seg in self.segments:
            if seg.fix1 >= 0 and seg.tgt_on[seg.fix1]:
                return True
        return False

    def uses_fix2(self) -> bool:
        """
        Does this trial designate a participating target as fixation target #2 during any segment of the trial? The
        target must also be turned on in at least one segment in which it is designated as fixation target #2.
        """
        for seg in self.segments:
            if seg.fix2 >= 0 and seg.tgt_on[seg.fix2]:
                return True
        return False

    def uses_vstab(self) -> bool:
        """ Does this trial velocity-stabilize any participating target during any segment? """
        for seg in self.segments:
            for i in range(len(self.targets)):
                if seg.tgt_vel_stab_mask[i] != 0:
                    return True
        return False

    def duration(self) -> int:
        """ Return the total duration of this trial in milliseconds. This method merely returns the sum of the segment
        durations as defined in the trial. """
        return sum([seg.dur for seg in self.segments])

    def segment_table_differences(self, other: Trial) -> Optional[List[SegParam]]:
        """
        Return a list of all segment parameter differences between this trial and another. This method is called when
        determining whether or not two Maestro trial instances are repetitions of the same trial definition, with the
        exception of one or more randomized segment table parameters. A segment table parameter is randomized in Maestro
        by defining a trial random variable or, more commonly, by specifying different minimum and maximum durations for
        a segment.

        Float-valued target trajectory parameters are "different" only if the absolute difference between their
        values exceeds 0.05. This is because there's a loss of precision when storing floating-point values in the
        trial codes (as scaled 16-bit integers), and a further error is introduced when applying the inverse of the
        target transform to recover the trajectory parameter values as they would have appeared in Maestro. Because
        of these errors, two instances of the same trial protocol presented with two different target transforms
        could have slightly different target trajectory parameters.

        For two trials to be comparable, they must be similar enough -- see is_similar_to().

        Args:
            other: The other trial. Must have the same number of targets as this segment.

        Returns:
            Optional[List[SegParam]] - List of segment table parameters in which this trial differs from the trial
                specified. Returns an empty list if there are no differences. Returns None if the two trials are NOT
                comparable..
        """
        if not self.is_similar_to(other):
            return None
        out: List[SegParam] = list()
        for i, seg in enumerate(self.segments):
            other_seg = other.segments[i]
            if seg.dur != other_seg.dur:
                out.append(SegParam._make([SegParamType.DURATION, i, -1]))
            if seg.pulse_ch != other_seg.pulse_ch:
                out.append(SegParam._make([SegParamType.MARKER, i, -1]))
            if seg.fix1 != other_seg.fix1:
                out.append(SegParam._make([SegParamType.FIX_TGT1, i, -1]))
            if seg.fix2 != other_seg.fix2:
                out.append(SegParam._make([SegParamType.FIX_TGT2, i, -1]))
            if self.uses_xy_scope() and (seg.xy_update_intv != other_seg.xy_update_intv):
                out.append(SegParam._make([SegParamType.XY_UPDATE_INTV, i, -1]))
            for j in range(len(self.targets)):
                if seg.tgt_on[j] != other_seg.tgt_on[j]:
                    out.append(SegParam._make([SegParamType.TGT_ON_OFF, i, j]))
                if seg.tgt_rel[j] != other_seg.tgt_rel[j]:
                    out.append(SegParam._make([SegParamType.TGT_REL, i, j]))
                if seg.tgt_on[j] != other_seg.tgt_on[j]:
                    out.append(SegParam._make([SegParamType.TGT_ON_OFF, i, j]))
                if seg.tgt_vel_stab_mask[j] != other_seg.tgt_vel_stab_mask[j]:
                    out.append(SegParam._make([SegParamType.TGT_VSTAB, i, j]))
                if abs(seg.tgt_pos[j].x - other_seg.tgt_pos[j].x) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_POS_H, i, j]))
                if abs(seg.tgt_pos[j].y - other_seg.tgt_pos[j].y) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_POS_V, i, j]))
                if abs(seg.tgt_vel[j].x - other_seg.tgt_vel[j].x) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_VEL_H, i, j]))
                if abs(seg.tgt_vel[j].y - other_seg.tgt_vel[j].y) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_VEL_V, i, j]))
                if abs(seg.tgt_acc[j].x - other_seg.tgt_acc[j].x) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_ACC_H, i, j]))
                if abs(seg.tgt_acc[j].y - other_seg.tgt_acc[j].y) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_ACC_V, i, j]))
                if abs(seg.tgt_pat_vel[j].x - other_seg.tgt_pat_vel[j].x) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_PAT_VEL_H, i, j]))
                if abs(seg.tgt_pat_vel[j].y - other_seg.tgt_pat_vel[j].y) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_PAT_VEL_V, i, j]))
                if abs(seg.tgt_pat_acc[j].x - other_seg.tgt_pat_acc[j].x) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_PAT_ACC_H, i, j]))
                if abs(seg.tgt_pat_acc[j].y - other_seg.tgt_pat_acc[j].y) > 0.05:
                    out.append(SegParam._make([SegParamType.TGT_PAT_ACC_V, i, j]))
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
            out = self.segments[param.seg_idx].value_of(param.type, param.tgt_idx)
        except IndexError:
            pass
        return out

    def record_start(self) -> int:
        """
        Get elapsed trial time at which recording began. Normally, this is 0. However, if the trial's record segment
        index is NOT the first segment, then it is the sum of the segment durations prior to the record segment.

        Returns:
            Time at which recording of behavioral responses and events began, in milliseconds since trial start.
        """
        return sum(self.segments[i].dur for i in range(self.record_seg))

    def target_trajectories(self, hgpos: Optional[np.ndarray] = None, vepos: Optional[np.ndarray] = None,
                            vstab_win_len: Optional[int] = None) -> List[np.ndarray]:
        """
        Compute the position trajectories of all targets participating in this trial.

        This implementation does a basic piecewise integration similar to what happens on the fly in Maestro during a
        trial. However, it does NOT account for ANY of the following: velocity stabilization, velocity perturbations,
        the video update rate of the RMVideo and XYScope platforms. Also, it calculates position only, not velocity nor
        pattern velocity for video targets. Finally, the calculation assumes that targets move even if they are turned
        off. This has always been the case -- except for XYScope targets prior to Maestro 1.2.1

        Args:
            hgpos: The horizontal eye position trajectory (in deg) during trial -- used to adjust target trajectories
                during periods of velocity stabilization. Default = None, in which case no adjustment can be made.
            vepos: The vertical eye position trajectory (in deg) during trial -- used to adjust target trajectories
                during periods of velocity stabilization. Default = None, in which case no adjustment can be made.
            vstab_win_len: The length of the sliding window (1 to 20 ms) for smoothing eye position when computing the
                target trajectory adjustment for velocity stabilization. Default = None (no smoothing).
        Returns:
            A list of 2D Numpy arrays, where the I-th array is the position trajectory of the I-th participating target.
                Each array is Nx2, where N is the trial duration and the N-th "row" is the (H,V) position of the target
                N milliseconds since trial start. Position is in degrees subtended at the eye.
        """
        dur = self.duration()
        num_tgts = len(self.targets)
        trajectories: List[np.ndarray] = [np.zeros((dur, 2)) for _ in range(num_tgts)]
        current_pos: List[Point2D] = [Point2D(0, 0) for _ in range(num_tgts)]
        current_vel: List[Point2D] = [Point2D(0, 0) for _ in range(num_tgts)]

        # enable velocity stabilization compensation if all restrictions met
        t_record = self.record_start()
        do_vstab = self.uses_vstab() and (hgpos is not None) and (vepos is not None) and (len(hgpos) == len(vepos)) \
            and (len(hgpos) >= (dur - t_record))
        vstab_win_len = 1 if (not isinstance(vstab_win_len, int)) else max(min(20, vstab_win_len), 1)
        current_eye_pos = Point2D(0, 0)
        last_eye_pos = Point2D(0, 0)

        t = 0
        delta = 0.001  # in Maestro, one "tick" = 1 millisecond
        for seg_idx, seg in enumerate(self.segments):
            for i in range(num_tgts):
                if seg.tgt_rel[i]:
                    current_pos[i].offset_by(seg.tgt_pos[i].x, seg.tgt_pos[i].y)
                else:
                    current_pos[i].set_point(seg.tgt_pos[i])
                current_vel[i].set_point(seg.tgt_vel[i])

            t_start_seg = t
            while t < (t_start_seg + seg.dur):
                # if doing VStab compensation, get current eye position, smoothed if window length > 1.
                if do_vstab and (t >= t_record):
                    if (vstab_win_len == 1) or (t == t_record):
                        current_eye_pos.set(hgpos[t-t_record], vepos[t-t_record])
                    else:
                        start = max(0, t-t_record-vstab_win_len)
                        end = t-t_record
                        current_eye_pos.set(np.nanmean(hgpos[start:end]), np.nanmean(vepos[start:end]))

                for i in range(num_tgts):
                    # velocity stabilization adjustment of target position, if applicable
                    vstab_mask = seg.tgt_vel_stab_mask[i]
                    if do_vstab and (t >= t_record) and vstab_mask != 0:
                        if (t == t_start_seg) and \
                              ((seg_idx == 0) or (self.segments[seg_idx-1].tgt_vel_stab_mask[i] == 0)) and \
                              ((vstab_mask & VEL_STAB_SNAP) != 0):
                            current_pos[i].set_point(current_eye_pos)
                        else:
                            current_pos[i].offset_by(
                                (current_eye_pos.x - last_eye_pos.x) if ((vstab_mask & VEL_STAB_H) != 0) else 0,
                                (current_eye_pos.y - last_eye_pos.y) if ((vstab_mask & VEL_STAB_V) != 0) else 0
                            )
                    trajectories[i][t, :] = [current_pos[i].x, current_pos[i].y]
                    current_pos[i].offset_by(current_vel[i].x * delta, current_vel[i].y * delta)
                    current_vel[i].offset_by(seg.tgt_acc[i].x * delta, seg.tgt_acc[i].y * delta)
                t += 1

                # if doing VStab compensation, remember eye position
                if do_vstab:
                    last_eye_pos.set_point(current_eye_pos)

        return trajectories


class SegParamType(DocEnum):
    """
    An enumeration of (most of) the parameter types that define a segment within a Maestro trial.
    """
    DURATION = 1, "Segment duration (milliseconds)"
    MARKER = 2, "Channel on which marker pulse is delivered at segment start (if any)"
    FIX_TGT1 = 3, "Index position (zero-based) of target designated as the first fixation target"
    FIX_TGT2 = 4, "Index position (zero-based) of target designated as the second fixation target"
    XY_UPDATE_INTV = 5, "XYScope update interval during segment (milliseconds)"
    TGT_ON_OFF = 6, "Target on/off state"
    TGT_REL = 7, "Target position change relative or absolute"
    TGT_VSTAB = 8, "Target velocity stabilization state"
    TGT_POS_H = 9, "Horizontal target position change at segment start (degrees)"
    TGT_POS_V = 10, "Vertical target position change at segment start (degrees)"
    TGT_VEL_H = 11, "Horizontal target velocity during segment (deg/sec)"
    TGT_VEL_V = 12, "Vertical target velocity during segment (deg/sec)"
    TGT_ACC_H = 13, "Horizontal target acceleration during segment (deg/sec^2)"
    TGT_ACC_V = 14, "Vertical target acceleration during segment (deg/sec^2)"
    TGT_PAT_VEL_H = 15, "Horizontal target pattern velocity during segment (deg/sec)"
    TGT_PAT_VEL_V = 16, "Vertical target pattern velocity during segment (deg/sec)"
    TGT_PAT_ACC_H = 17, "Horizontal target pattern acceleration during segment (deg/sec^2)"
    TGT_PAT_ACC_V = 18, "Vertical target pattern acceleration during segment (deg/sec^2)"

    def is_target_trajectory_parameter(self) -> bool:
        """
        Does this SegParamType identify a target trajectory parameter within a Maestro trial's segment table?
        """
        return self not in [SegParamType.DURATION, SegParamType.MARKER, SegParamType.FIX_TGT1, SegParamType.FIX_TGT2,
                            SegParamType.XY_UPDATE_INTV]

    def can_vary_randomly(self) -> bool:
        """
        Can this segment parameter type vary randomly across repeated instances of the same Maestro trial?
        """
        return (self == SegParamType.DURATION) or \
               (self.is_target_trajectory_parameter() and
                (self not in [SegParamType.TGT_ON_OFF, SegParamType.TGT_REL, SegParamType.TGT_VSTAB]))


class SegParam(NamedTuple):
    """
    A tuple (type, seg_idx, tgt_idx) identifying the type, segment index, and target index of a particular parameter
    within the segment table of a Maestro trial. The target index only applies to a target trajectory parameter.
    """
    type: SegParamType
    seg_idx: int
    tgt_idx: int

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


class Protocol(NamedTuple):
    """
    A Maestro trial "protocol" refers to the definition of a Maestro trial that is typically presented many times over
    the course of multiple experiment sessions. A typical trial definition will very often include one or more segment
    table parameters that vary randomly from one repeated instance of that trial to the next. The most frequently
    randomized parameter is the duration of an initial "fixation segment", but the introduction of "random variables" in
    Maestro trials allows the experimenter to randomly vary other target trajectory parameters.

    The attributes of the Protocol include:
        trial (Trial) - The trial definition (targets, segment table, and so on).

        rvs (List[SegParam]) - Identifies all segment table parameters that vary randomly over repeated instances of
        the protocol. Typically, this will contain zero or just a single parameter.

        md5_digest (str) - The hexadecimal character digest of the MD5 hash for the protocol object. It is 32 characters
        long and serves to uniquely identify the protocol. Currently, the hash includes the following aspects of a trial
        protocol: trial's full name (including set and subset, if applicable), # of segments, the participating target
        list, any defined perturbations and tagged sections, and the index of the segment when recording started. The
        detailed segment table is NOT part of the hash digest, nor is the 'diffs' attribute listing any 'random
        variables' in the trial protocol.
    """
    trial: Trial
    rvs: List[SegParam]
    md5_digest: str

    @staticmethod
    def from_candidate(candidate: ProtocolCandidate) -> Protocol:
        # calculating the (hopefully unique!) MD5 hash digest for the protocol. Note that all segment table parameters
        # are included EXCEPT those that are protocol RVs.
        hash_attrs = [candidate.trial.path_name(), candidate.trial.record_seg, hash(candidate.trial.global_transform),
                      [hash(tgt) for tgt in candidate.trial.targets],
                      [hash(pert) for pert in candidate.trial.perts],
                      [hash(section) for section in candidate.trial.sections]]
        for seg_idx, segment in enumerate(candidate.trial.segments):
            seg_params = list()
            num_targets = segment.num_targets()
            for param_type in SegParamType:
                if param_type.is_target_trajectory_parameter():
                    for tgt_idx in range(num_targets):
                        if not (SegParam(param_type, seg_idx, tgt_idx) in candidate.rvs):
                            seg_params.append(segment.value_of(param_type, tgt_idx))
                elif not (SegParam(param_type, seg_idx, -1) in candidate.rvs):
                    seg_params.append(segment.value_of(param_type, -1))
            hash_attrs.append(seg_params)

        digester = hashlib.md5()
        digester.update(pickle.dumps(hash_attrs))
        return Protocol._make([candidate.trial, list(candidate.rvs), digester.hexdigest()])

    def can_aggregate_responses(self) -> bool:
        """
        Can the behavioral and neural responses to repeated presentations of this trial protocol be aggregated in some
        fashion, typically by averaging? By convention, the protocol must have AT MOST one defined random variable, and
        that random variable can only the duration of one segment (not necessarily the first) in the trial protocol.

        Returns:
            True if protocol is amenable to averaging response data, as described; else False.
        """
        return (len(self.rvs) == 0) or \
               ((len(self.rvs) == 1) and (self.rvs[0].type == SegParamType.DURATION))

    def summary(self) -> Dict[str, Any]:
        """
        Generate a summary of this Maestro trial protocol for display purposes only. Returns a dictionary with the
        following fields: 'digest' is the protocol's 32-character MD5 hexadecimal digest (str); 'path_name' is the
        trial's full path name, including trial set and subset if applicable and available (str); 'transform' is the
        global target transform applicable to all instances of the trial protocol (str); 'diffs' is a list of the
        segment table parameters that may randomly vary from one presentation of the trial protocol to the next
        (List[str]); 'targets' is a list of participating target descriptors (List[str]); 'perts' is a list of any
        defined perturbations in the protocol (List[str]); 'sections' is a list of any tagged sections (List[str];
        'record_seg' is the index of the segment at which recording starts (int); and 'segments' is a list of trial
        segment descriptors (List[dict]).
        """
        return {'digest': self.md5_digest, 'path_name': self.trial.path_name(),
                'transform': str(self.trial.global_transform),
                'rvs': [str(rv) for rv in self.rvs],
                'targets': [str(target) for target in self.trial.targets],
                'perts': [str(pert) for pert in self.trial.perts],
                'sections': [str(section) for section in self.trial.sections],
                'record_seg': self.trial.record_seg,
                'segments': [segment.summary() for segment in self.trial.segments]}

    def display_definition(self) -> List[Any]:
        """
        Generate an Dash-based presentation of this trial protocol's definition. A Dash Datatable component displays the
        segment table for the protocol, and an HTML Div component houses a series of Bootstrap badges that display key
        information like: segment index at which recording begins, the target transform, targets, perturbations, tagged
        sections, and random variables. Tooltips associated with some of the badges reveal more detailed information
        like target definitions, perturbation details, and so on.

        Returns:
            A list of two components, a div and a Dash Datable, which should be embedded as children of an outer div.
        """
        proto_summary = self.summary()
        segments = proto_summary['segments']
        targets = proto_summary['targets']
        target_names = [target_desc.split(':')[0] for target_desc in targets]  # THIS IS A HACK
        perts = proto_summary['perts']
        sections = proto_summary['sections']
        rvs = proto_summary['rvs']

        badges = [
            dbc.Badge(f"Record Seg: {proto_summary['record_seg']}", color="primary", class_name="me-3"),
            dbc.Badge(f"Transform: {proto_summary['transform']}", color="primary", class_name="me-3"),
            dbc.Badge(f"Targets: {len(target_names)}", id="disp_proto_targets", color="primary", class_name="me-3"),
            dbc.Badge(f"Perturbations: {len(perts)}", id="disp_proto_perts", color="primary", class_name="me-3"),
            dbc.Badge(f"Tagged Sects: {len(sections)}", id="disp_proto_sections", color="primary", class_name="me-3"),
            dbc.Badge(f"Random Vars: {len(rvs)}", id="disp_proto_random_vars", color="primary"),
            dbc.Tooltip([html.Div(f"{str(target)}") for target in targets],
                        target="disp_proto_targets", style={'max-width': '600px'})
        ]
        if len(perts) > 0:
            badges.append(
                dbc.Tooltip([html.Div(f"{str(pert)}") for pert in perts],
                            target="disp_proto_perts", style={'max-width': '600px'})
            )
        if len(sections) > 0:
            badges.append(
                dbc.Tooltip([html.Div(f"{str(section)}") for section in sections],
                            target="disp_proto_sections", style={'max-width': '600px'})
            )
        if len(rvs) > 0:
            badges.append(
                dbc.Tooltip([html.Div(f"{str(rv)}") for rv in rvs],
                            target="disp_proto_random_vars", style={'max-width': '600px'})
            )

        columns = [{"name": "", "id": "param"}]
        columns.extend([{"name": f"Segment {i}", "id": f"seg_{i}"} for i in range(len(segments))])

        # NOTE: Any parameter that varies randomly in the trial protocol is represented by an asterisk '*' in the
        # segment table rendering rather than its value in the representative trial.
        duration = {"param": "Duration (ms)"}
        fix1_tgt = {"param": "Fix Tgt #1"}
        fix2_tgt = {"param": "Fix Tgt #2"}
        xy_delta = {"param": "XYScope Intv (ms)"}
        marker = {"param": "Marker Pulse"}
        tgt_on = [{"param": name} for name in target_names]
        tgt_vstab = [{"param": "VStab"} for _ in target_names]
        tgt_pos = [{"param": "Position (deg)"} for _ in target_names]
        tgt_vel_acc = [{"param": "Vel (d/s), Acc (d/s^2)"} for _ in target_names]
        tgt_pat = [{"param": "Pattern Vel, Acc"} for _ in target_names]
        for i, seg in enumerate(segments):
            seg_id = f"seg_{i}"
            duration[seg_id] = "***" if SegParam(SegParamType.DURATION, i, -1) in self.rvs else seg['dur']
            fix1_tgt[seg_id] = "NONE" if seg['fix1'] < 0 else target_names[seg['fix1']]
            fix2_tgt[seg_id] = "NONE" if seg['fix2'] < 0 else target_names[seg['fix2']]
            xy_delta[seg_id] = seg['xy_update']
            marker[seg_id] = "NONE" if seg['marker'] < 0 else f"DO{seg['marker']}"
            for tgt_idx in range(len(target_names)):
                trajectory = seg['trajectories'][tgt_idx]
                tgt_on[tgt_idx][seg_id] = "ON" if trajectory['on'] else 'OFF'
                tgt_vstab[tgt_idx][seg_id] = trajectory['vstab']
                tgt_pos[tgt_idx][seg_id] = self.trial.segments[i].tgt_pos[tgt_idx].as_string_with_wildcard(
                    (SegParam(SegParamType.TGT_POS_H, i, tgt_idx) in self.rvs),
                    (SegParam(SegParamType.TGT_POS_V, i, tgt_idx) in self.rvs))
                tgt_pos[tgt_idx][seg_id] += " rel" if self.trial.segments[i].tgt_rel[tgt_idx] else " abs"
                tgt_vel_out = self.trial.segments[i].tgt_vel[tgt_idx].as_string_with_wildcard(
                    (SegParam(SegParamType.TGT_VEL_H, i, tgt_idx) in self.rvs),
                    (SegParam(SegParamType.TGT_VEL_V, i, tgt_idx) in self.rvs))
                tgt_acc_out = self.trial.segments[i].tgt_acc[tgt_idx].as_string_with_wildcard(
                    (SegParam(SegParamType.TGT_ACC_H, i, tgt_idx) in self.rvs),
                    (SegParam(SegParamType.TGT_ACC_V, i, tgt_idx) in self.rvs))
                tgt_vel_acc[tgt_idx][seg_id] = f"{tgt_vel_out}  {tgt_acc_out}"
                tgt_pat_vel_out = self.trial.segments[i].tgt_pat_vel[tgt_idx].as_string_with_wildcard(
                    (SegParam(SegParamType.TGT_PAT_VEL_H, i, tgt_idx) in self.rvs),
                    (SegParam(SegParamType.TGT_PAT_VEL_V, i, tgt_idx) in self.rvs))
                tgt_pat_acc_out = self.trial.segments[i].tgt_pat_acc[tgt_idx].as_string_with_wildcard(
                    (SegParam(SegParamType.TGT_PAT_ACC_H, i, tgt_idx) in self.rvs),
                    (SegParam(SegParamType.TGT_PAT_ACC_V, i, tgt_idx) in self.rvs))
                tgt_pat[tgt_idx][seg_id] = f"{tgt_pat_vel_out}  {tgt_pat_acc_out}"
                # tgt_pos[tgt_idx][seg_id] = trajectory['pos']
                # tgt_vel_acc[tgt_idx][seg_id] = f"{trajectory['vel']}  {trajectory['acc']}"
                # tgt_pat[tgt_idx][seg_id] = f"{trajectory['patvel']}  {trajectory['patacc']}"
        rows = [duration, fix1_tgt, fix2_tgt, xy_delta, marker]
        for i in range(len(target_names)):
            rows.extend([tgt_on[i], tgt_vstab[i], tgt_pos[i], tgt_vel_acc[i], tgt_pat[i]])

        # the segment table rendered as a Dash DataTable...
        # right-align first column displaying parameter descriptions, but left-align and underline the target names
        # that appear in that column. Use a brownish-yellow background to ighlight the target name rows, which separate
        # the target trajectory sections in the segment table. Finally, use a green background to highligh any cell in
        # the segment table that houses a random variable.
        tgt_name_row_indices = [5 + i*5 for i in range(len(target_names))]
        style_data_conditional = [
            {'if': {'column_id': 'param'}, 'textAlign': 'right'},
            {'if': {'column_id': 'param', 'row_index': tgt_name_row_indices},
             'textDecoration': 'underline', 'textAlign': 'left'},
            {'if': {'row_index': tgt_name_row_indices}, 'backgroundColor': 'rgba(218,165,32,128)', 'color': 'black'}
        ]
        for rv in self.rvs:
            seg_id = f"seg_{rv.seg_idx}"
            row_idx = 0
            if rv.type != SegParamType.DURATION:
                if rv.type in [SegParamType.TGT_POS_H, SegParamType.TGT_POS_V]:
                    ofs = 2
                elif rv.type in [SegParamType.TGT_VEL_H, SegParamType.TGT_VEL_V, SegParamType.TGT_ACC_H,
                                 SegParamType.TGT_ACC_V]:
                    ofs = 3
                else:
                    ofs = 4
                row_idx = 5 + rv.tgt_idx*5 + ofs
            style_data_conditional.append(
                {'if': {'column_id': seg_id, 'row_index': [row_idx]}, 'backgroundColor': 'limegreen', 'color': 'black'}
            )
        segment_table = dt.DataTable(
            columns=columns,
            data=rows,
            cell_selectable=False,
            style_header={'fontWeight': 'bold', 'textAlign': 'center'},
            style_cell={'textAlign': 'center', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
            style_cell_conditional=[
                {'if': {'column_id': 'param'}, 'width': '200px'}
            ],
            style_data_conditional=style_data_conditional,
            style_data={'whiteSpace': 'pre-wrap'},
            style_table={'height': '330px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
            fixed_rows={'headers': True, 'data': 0},
            fixed_columns={'headers': True, 'data': 0}
        )
        return [html.Div(badges, className='mt-3 mb-1'), segment_table]

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
            trial_rvs: The value of any random variables for the particular trial instance. Length must match the
                number of RVs defined on the protocol. Ignored if the protocol lacks any random variables.
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
            ValueError: If the length of trial_rvs does not match the number of random variables for this protocol.
        """
        # if there are any random variables, replace their values in the trial definition with the supplied values. NOTE
        # that this alters the definition of the internal trial object, but that should not matter because we must
        # always supply the RV values for a given trial instance!
        if len(self.rvs) > 0:
            if len(self.rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, param in enumerate(self.rvs):
                self.trial.segments[param.seg_idx].set_value_of(param.type, param.tgt_idx, trial_rvs[i])

        trial_dur = self.trial.duration()
        tgt_pos_trajectories: List[np.ndarray] = self.trial.target_trajectories(hgpos, vepos, vstab_win_len)
        fix1: Optional[np.ndarray] = None
        if self.trial.uses_fix1():
            fix1 = np.empty((trial_dur, 2))
            fix1[:] = np.nan
            t = 0
            for seg in self.trial.segments:
                if seg.fix1 >= 0:
                    fix1[t:t+seg.dur, :] = tgt_pos_trajectories[seg.fix1][t:t+seg.dur, :].copy()
                t += seg.dur
        fix2: Optional[np.ndarray] = None
        if self.trial.uses_fix2():
            fix2 = np.empty((trial_dur, 2))
            fix2[:] = np.nan
            t = 0
            for seg in self.trial.segments:
                if seg.fix2 >= 0:
                    fix2[t:t+seg.dur, :] = tgt_pos_trajectories[seg.fix2][t:t+seg.dur, :].copy()
                t += seg.dur
        return fix1, fix2

    def compute_fixation_target_on_epochs(self, trial_rvs: List[Union[int, float]]) -> Tuple[List[int], List[int]]:
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
            trial_rvs: The value of any random variables for the particular trial instance. Length must match the
                number of RVs defined on the protocol. Ignored if the protocol lacks any random variables.
        Returns:
            A 2-tuple (fix1, fix2). The first element is a list of 2*N elapsed times (ms since trial start) [S1, E1,
                S2, E2, ..., SN, EN] specifying the N non-overlapping ON epochs for fixation target #1. The second
                element holds the ON epochs for fixation target #2. If a fixation target is unused or never turned on,
                the corresponding element will be an empty list.
        Raises:
            ValueError: If the length of trial_rvs does not match the number of random variables for this protocol.
        """
        # if there are any random variables, replace their values in the trial definition with the supplied values. NOTE
        # that this alters the definition of the internal trial object, but that should not matter because we must
        # always supply the RV values for a given trial instance!
        if len(self.rvs) > 0:
            if len(self.rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, param in enumerate(self.rvs):
                self.trial.segments[param.seg_idx].set_value_of(param.type, param.tgt_idx, trial_rvs[i])

        fix1: List[int] = list()
        fix2: List[int] = list()
        t1_start = t2_start = -1
        t = 0
        for seg in self.trial.segments:
            if (t1_start == -1) and (seg.fix1 >= 0) and seg.tgt_on[seg.fix1]:
                t1_start = t
            elif t1_start > -1 and ((seg.fix1 < 0) or not seg.tgt_on[seg.fix1]):
                fix1.extend([t1_start, t])
                t1_start = -1
            if (t2_start == -1) and (seg.fix2 >= 0) and seg.tgt_on[seg.fix2]:
                t2_start = t
            elif t2_start > -1 and ((seg.fix2 < 0) or not seg.tgt_on[seg.fix2]):
                fix2.extend([t2_start, t])
                t2_start = -1
            t += seg.dur

        # close the last ON epoch, if fixation target is on through end of trial
        if t1_start > -1:
            fix1.extend([t1_start, t])
        if t2_start > -1:
            fix2.extend([t2_start, t])

        return fix1, fix2

    def duration_of_rep(self, trial_rvs: List[Union[int, float]]) -> int:
        """
        Get expected duration of a particular presentation of this trial protocol, taking into account the durations
        of any random-duration trial segments. If the protocol lacks any random-duration segments, then all reps will
        have the same duration.

        Args:
            trial_rvs: The value of any random variables for the particular trial instance. Length must match the
                number of RVs defined on the protocol. Ignored if the protocol lacks any random variables.
        Returns:
            The expected duration of the trial rep given the durations -- specified in trial_rvs -- of any
                random-duration segments in the protocol.
        Raises:
            ValueError: If the length of trial_rvs does not match the number of random variables for this protocol.
        """
        if len(self.rvs) > 0:
            if len(self.rvs) != len(trial_rvs):
                raise ValueError("Random-variable value list does not match trial protocol definition!")
            for i, param in enumerate(self.rvs):
                self.trial.segments[param.seg_idx].set_value_of(param.type, param.tgt_idx, trial_rvs[i])
        return sum([seg.dur for seg in self.trial.segments])


class ProtocolCandidate:
    """
    A candidate for a Maestro trial protocol, as extracted from a single experimental session.

    A typical Maestro experiment usually involves the repeated presentation of a set or sets of Maestro trials, with the
    results from each trial presentation stored in a data file. That file includes the sequence of trial codes defining
    the trial, as well as the definitions of participating targets. The definition of the trial as it appears in Maestro
    is the "trial protocol", as distinguished from a particular presentation of that protocol -- a "trial rep". Part of
    the workflow in committing an experiment to the lab database is to detect all of the distinct trial protocols
    presented during the session. The difficulty lies in the fact that a typical protocol will often include a "random
    variable" -- a segment table parameter that varies randomly from one trial rep to the next; the most typical example
    of this is an initial "fixation" segment with random duration.

    In order to "detect" a unique protocol, we need to process at least 2 reps of that protocol in order to identify
    any random variables; the more reps processed, the better the chances of identifying all random variables defined in
    the protocol. If only one rep is encountered, then the user must validate the protocol definition and identify any
    and all random variables in it.

    For these reasons, we distinguish a protocol "candidate" from a confirmed trial protocol. Protocol candidates are
    extracted while processing the trial data files, then validated in one of several ways:
        1) If only one trial rep was processed, user validation is required -- unless there is an existing protocol
        in the lab database that matches the protocol candidate.

        2) If two trial reps were processed, user validation is recommended -- again unless there is an existing
        protocol matching the candidate protocol.

        3) If 3+ trial reps were processed, it is assumed that the trial protocol candidate definition is accurate and
        user validation is not required.
    """
    def __init__(self, trial: Trial):
        self.trial = trial
        """ The trial defining this trial protocol candidate, excluding any random variables. """
        self.rvs: Set[SegParam] = set()
        """ The protocol candidate's set of random variables, i.e., those segment table parameters that vary randomly
        over repeated presentations of the protocol. """
        self.num_reps = 1
        """ The number of trial reps that were processed to identify this trial protocol candidate. """
        self.matches_existing = False
        """ Flag set if protocol candidate matches an existing trial protocol in the lab database -- in which case
        user validation is not required. """
        self.user_validated = False
        """ Flag set if protocol candidate definition has been marked valid via user interaction."""

    def needs_validation(self) -> bool:
        return not (self.user_validated or (self.num_reps > 2) or (self.num_reps == 2 and self.matches_existing))

    def add_random_variable(self, rv: SegParam) -> bool:
        """
        Add a random variable to the definition of this Maestro trial protocol candidate.

        Args:
            rv: The random variable
        Returns:
            False only if specified random variable is invalid (bad parameter type, segment index, or target index).
        """
        if rv.type.can_vary_randomly() and (0 <= rv.seg_idx < len(self.trial.segments)) and \
                ((not rv.type.is_target_trajectory_parameter()) or (0 <= rv.tgt_idx < len(self.trial.targets))):
            self.rvs.add(rv)
            return True
        return False

    @staticmethod
    def extract_protocols_from_session_data(archive: zipfile.ZipFile) -> Tuple[List[ProtocolCandidate], Dict[str, int]]:
        """
        Examine all Maestro data files contained in the ZIP archive specified and return the list of trial protocol
        candidates culled from those files. This is an important task when committing an experiment session's worth of
        data to the lab database.

        Args:
            archive: An open ZIP archive containing the Maestro data files collected during an experiment session. Must
            be open for reading and is NOT closed on return.
        Returns:
            A 2-tuple: a list of all trial protocol candidates culled from the session data, and a dictionary that maps
            the filename of each trial data file to the list index identifying the trial protocol candidate presented
            when that file was recorded.
        Raises:
            DataFileError: If a problem occurs while reading the ZIP archive and processing the data files therein.
        """
        try:
            archive_list = archive.infolist()
            data_file_name_pattern = re.compile('.[0-9][0-9][0-9][0-9]+$')
            proto_candidates: List[ProtocolCandidate] = list()
            filename_to_protocol: Dict[str, int] = dict()
            for info in archive_list:
                if data_file_name_pattern.search(info.filename) is not None:
                    try:
                        trial = DataFile.load_trial(archive.read(info), info.filename)
                        found = False
                        for i, proto in enumerate(proto_candidates):
                            if proto.trial.is_similar_to(trial):
                                found = True
                                proto.rvs.update(proto.trial.segment_table_differences(trial))
                                filename_to_protocol[info.filename] = i
                                proto.num_reps += 1
                                break
                        if not found:
                            proto_candidates.append(ProtocolCandidate(trial))
                            filename_to_protocol[info.filename] = len(proto_candidates) - 1
                    except DataFileError as err:
                        msg = f"===> Error: Failed loading file {info.filename}: {str(err)}"
                        raise DataFileError(msg)
            return proto_candidates, filename_to_protocol
        except DataFileError:
            raise
        except Exception as err:
            msg = f"Unexpected error while extracting trial protocols from session data: {str(err)}"
            raise DataFileError(msg)
