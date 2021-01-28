"""
Copyright (c) 2018 David Herzfeld

Written by David J. Herzfeld <herzfeldd@gmail.com>

This module contains functions written by DH to parse the contents of a Plexon/Omniplex PL2 data file. In the original
code, the load functions take the filename of the PL2 file to be read. Here, the functions have been adapted to take a
a Python file-like object so that they can be used with a PL2 file compressed within a ZIP archive. In addition, I have
made some cosmetic changes such as docstrings and some type annotations.
"""

import struct
import os
import numpy as np
from typing import Dict, Any, Optional, Union, Tuple, List, IO


# Define the constants for this module
# Constants for data blocks
PL2_DATA_BLOCK_ANALOG_CHANNEL = 0x42
PL2_DATA_BLOCK_EVENT_CHANNEL = 0x5A
PL2_DATA_BLOCK_SPIKE_CHANNEL = 0x31
PL2_DATA_BLOCK_START_STOP_CHANNEL = 0x59

# Constants for footers
PL2_FOOTER_ANALOG_CHANNEL = 0xDD
PL2_FOOTER_EVENT_CHANNEL = 0xDF
PL2_FOOTER_SPIKE_CHANNEL = 0xDE
PL2_FOOTER_START_STOP_CHANNEL = 0xE0

# Constants for headers
PL2_HEADER_ANALOG_CHANNEL = 0xD4
PL2_HEADER_SPIKE_CHANNEL = 0xD5
PL2_HEADER_EVENT_CHANNEL = 0xD6

# Data subtypes
PL2_ANALOG_TYPE_WB = 0x03
PL2_ANALOG_TYPE_AI = 0x0C
PL2_ANALOG_TYPE_FP = 0x07
PL2_ANALOG_TYPE_SPKC = 0x04
PL2_EVENT_TYPE_SINGLE_BIT = 0x09
PL2_EVENT_TYPE_STROBED = 0x0A
PL2_SPIKE_TYPE_SPK = 0x06
PL2_SPIKE_TYPE_SPK_SPKC = 0x01


def load_file_information(fp: IO) -> Dict[str, Any]:
    """
    Loads metadata from the header, various channel subheaders, and the footer of a PL2 file. This information can be
    passed to other loading functions to speed up processing.

    Args:
        fp: The PL2 file object. The file must be open and is NOT closed on return.

    Returns:
        Dictionary containing the metadata culled from the PL2 file.
    """
    data: Dict[str, Any] = dict()
    # read the header from the file (storing contents in the dictionary)
    _read_header(fp, data)

    # Create empty start/stop channel values
    data["start_stop_channels"] = {}
    data["start_stop_channels"]["block_offsets"] = []
    data["start_stop_channels"]["block_timestamps"] = []
    data["start_stop_channels"]["block_num_items"] = []
    data["start_stop_channels"]["num_events"] = 0

    fp.seek(0x480)
    data["spike_channels"] = []
    for i in range(0, data["total_number_of_spike_channels"]):
        data["spike_channels"].append(_read_spike_channel_header(fp))

    data["analog_channels"] = []
    for i in range(0, data["total_number_of_analog_channels"]):
        data["analog_channels"].append(_read_analog_channel_header(fp))

    data["event_channels"] = []
    for i in range(0, data["number_of_event_channels"]):
        data["event_channels"].append(_read_event_channel_header(fp))

    if data["internal_value_3"] != 0 and data["internal_value_4"] != 0:
        _read_footer(fp, data)
    else:
        _reconstruct_footer(fp, data)

    return data


def load_all(fp: IO, info: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Load all of the data and metadata in a PL2 file and store in a dictionary.

    Args:
        fp: The PL2 file object. The file must be open and is NOT closed on return.
        info: Dictionary containing information already culled from the file. If None, load_file_information() is
            called first to load metadata from the file.

    Returns:
        Dictionary containing all information culled from the PL2 file.
    """
    if info is None:
        info = load_file_information(fp)
    _parse_data_blocks(fp, info)
    return info


def load_analog_channel(fp: IO, channel: int, info: Dict[str, Any] = None,
                        scale: bool = False) -> Optional[np.ndarray]:
    """
    Loads recorded data for a specified analog channel in a PL2 file.

    Args:
        fp: The PL2 file object. The file must be open and is NOT closed on return.
        channel: The analog channel index (zero-based).
        info: Dictionary containing information already culled from the file. If None, load_file_information() is
            called first to load basic information from the file. If not None, then the dictionary will be updated to
            include the specified analog channel's data.
        scale: If True, returns the results as an array of single precision floating point numbers, appropriately scaled
            by the conversion factor specified in the file header and then converted to millivolts. Otherwise, the raw
            unscaled ADC data is returned.

    Returns:
        A Numpy array of the analog channel data, optionally scaled to millivolts. Returns None if data not found.
    """
    if info is None:
        info = load_file_information(fp)

    # Ensure that the appropriate analog channel exists and there is data there
    if channel >= len(info["analog_channels"]) or channel < 0:
        raise RuntimeError(f"Invalid analog channel index: {channel}")

    if "values" not in info["analog_channels"][channel]:
        if ("block_offsets" not in info["analog_channels"][channel]) or \
                (len(info["analog_channels"][channel]["block_offsets"]) == 0):
            return None

        # Attempt to load the results
        total_items = sum(info["analog_channels"][channel]["block_num_items"])
        results = np.zeros(total_items, dtype=np.int16)
        for i in range(0, len(info["analog_channels"][channel]["block_offsets"])):
            block_offset = info["analog_channels"][channel]["block_offsets"][i]
            fp.seek(block_offset)
            _read(fp, "<B")   # data_type not used
            _read(fp, "<B")   # data_subtype not used

            num_items = _read(fp, "<H")
            if num_items != info["analog_channels"][channel]["block_num_items"][i]:
                raise RuntimeError(f"Invalid number of items encountered for analog channel index {channel}.")
            _read(fp, "<H")  # Channel
            _read(fp, "<H")  # Unknown
            timestamp = _read(fp, "<Q")  # Timestamp
            if timestamp != info["analog_channels"][channel]["block_timestamps"][i]:
                raise RuntimeError(f"Invalid timestamp encountered for analog channel index {channel}")

            # _read each of the items
            values = _read(fp, "<{:d}h".format(num_items))
            start = sum(info["analog_channels"][channel]["block_num_items"][0:i])
            stop = start + num_items
            results[start:stop] = values
        # Save our results in the info structure
        info["analog_channels"][channel]["values"] = results

    results = np.array(info["analog_channels"][channel]["values"])
    if scale:
        results = results.astype(np.float)
        results *= info["analog_channels"][channel]["coeff_to_convert_to_units"] * 1000  # to mV
    return results


def load_event_channel(fp: IO, channel: int, info: Dict[str, Any] = None,
                       scale: bool = False) -> Optional[Dict[str, np.ndarray]]:
    """
    Load all of the events from a from a given event channel.

    Args:
        fp: The PL2 file object. The file must be open and is NOT closed on return.
        channel: The event channel index (zero-based).
        info: Dictionary containing information already culled from the file. If None, load_file_information() is
            called first to load basic information from the file. If not None, then the dictionary will be updated to
            include the specified event channel's data.
        scale: If True, event timestamps are converted to seconds since the start of the recording; otherwise, they
            remain as integer tick counts.

    Returns:
        A dictionary with two keys. "timestamps" is a Numpy array holding the event timestamps (in seconds if scale is
            True), and "strobed" is a Numpy array holding the corresponding event values. Returns None if event
            channel data not found.
    """
    if info is None:
        info = load_file_information(fp)

    # Ensure that the appropriate analog channel exists and there is data there
    if channel >= len(info["event_channels"]) or channel < 0:
        raise RuntimeError(f"Invalid event channel index: {channel}")

    if "timestamps" not in info["event_channels"][channel]:
        if ("block_offsets" not in info["event_channels"][channel]) or \
                (len(info["event_channels"][channel]["block_offsets"]) == 0):
            return None

        # Attempt to load the results
        total_items = sum(info["event_channels"][channel]["block_num_items"])
        results = dict()
        results["timestamps"] = np.zeros(total_items, dtype=np.uint64)
        results["strobed"] = np.zeros(total_items, dtype=np.uint16)
        for i in range(0, len(info["event_channels"][channel]["block_offsets"])):
            block_offset = info["event_channels"][channel]["block_offsets"][i]
            fp.seek(block_offset)
            _read(fp, "<B")   # data_type not used
            _read(fp, "<B")   # data_subtype not used

            _read(fp, "<H")   # ??
            _read(fp, "<H")   # Channel not used
            num_items = _read(fp, "<Q")
            if num_items != info["event_channels"][channel]["block_num_items"][i]:
                raise RuntimeError(f"Invalid number of items encountered for event channel index {channel}")
            _read(fp, "<H")

            # _read each of the items
            start = sum(info["event_channels"][channel]["block_num_items"][0:i])
            stop = start + num_items
            results["timestamps"][start:stop] = _read(fp, "<{:d}Q".format(num_items))
            results["strobed"][start:stop] = _read(fp, "<{:d}H".format(num_items))
        # Store the results in the info structure
        info["event_channels"][channel]["timestamps"] = results["timestamps"]
        info["event_channels"][channel]["strobed"] = results["strobed"]

    results = dict()
    results["timestamps"] = np.array(info["event_channels"][channel]["timestamps"])
    results["strobed"] = np.array(info["event_channels"][channel]["strobed"])
    if scale:
        results["timestamps"] = results["timestamps"].astype(np.float)
        results["timestamps"] /= info["timestamp_frequency"]
    return results


def load_spike_channel(fp: IO, channel: int, info: Dict[str, Any] = None, scale: bool = False,
                       spike_number: Optional[int] = None) -> Optional[Dict[str, np.ndarray]]:
    """
    Load the timestamps and waveform clips for spikes that were identified/sorted in real time on a given spike channel
    during the PL2 recording. NOTE: Since researchers do their own spike sorting offline, and those spike sorting
    results must be supplied when committing an experiment session to the Lisberger lab database, we don't plan to use
    this function.

    Args:
        fp: The PL2 file object. The file must be open and is NOT closed on return.
        channel: The spike channel index (zero-based).
        info: Dictionary containing information already culled from the file. If None, load_file_information() is
            called first to load basic information from the file. If not None, then the dictionary will be updated to
            include the specified spike channel's data.
        scale: If True, timestamps and spike waveform clips are scaled and converted to seconds (since the start of
            recording) and millivolts, respectively. Otherwise, they are left in their raw digitized form.
        spike_number: A given spike channel may contain multiple identified spike units -- numbered 0..3. If not None,
            then this function only returns the data for the specified spike unit, rather than for all units identified
            on the channel specified.
    Returns:
        A dictionary with 3 keys. "timestamps" is a Numpy array holding the spike timestamps (in seconds if scale is
            True), "assignments" specifies the assigned spike number for each individual spike, and "spikes" (confusing
            name) are small clips of the voltage waveform around each spike timestamp. The waveform clips will be in
            millivolts if scale is True.
    """
    if info is None:
        info = load_file_information(fp)

    # Ensure that the appropriate analog channel exists and there is data there
    if channel >= len(info["spike_channels"]) or channel < 0:
        raise RuntimeError(f"Invalid spike channel index: {channel}")

    if "timestamps" not in info["spike_channels"][channel]:
        if ("block_offsets" not in info["spike_channels"][channel]) or \
                (len(info["spike_channels"][channel]["block_offsets"]) == 0):
            return None

        # Attempt to load the results
        total_items = np.sum(info["spike_channels"][channel]["block_num_items"])
        results = dict()
        results["num_points"] = info["spike_channels"][channel]["samples_per_spike"]
        results["timestamps"] = np.empty(total_items, dtype=np.uint64)
        results["spikes"] = np.empty((total_items, results["num_points"]), dtype=np.int16)
        results["assignments"] = np.empty(total_items, dtype=np.uint16)

        for i in range(0, len(info["spike_channels"][channel]["block_offsets"])):
            block_offset = info["spike_channels"][channel]["block_offsets"][i]
            fp.seek(block_offset)
            _read(fp, "<B")   # data_type not used
            _read(fp, "<B")   # data_subtype not used

            _read(fp, "<H")   # ??
            _read(fp, "<H")   # Channel
            num_sample_points = _read(fp, "<H")
            if num_sample_points != info["spike_channels"][channel]["samples_per_spike"]:
                raise RuntimeError(f"Invalid number of samples per spike encountered for spike channel index {channel}:"
                                   f" expected {info['spike_channels'][channel]['block_num_items']} but "
                                   f"got {num_sample_points}")

            num_items = _read(fp, "<Q")
            if num_items != info["spike_channels"][channel]["block_num_items"][i]:
                raise RuntimeError(f"Invalid number of items encountered for spike channel index {channel}: expected "
                                   f"{info['spike_channels'][channel]['block_num_items']} but got {num_items}")

            # read each of the items
            start = sum(info["spike_channels"][channel]["block_num_items"][0:i])
            stop = start + num_items
            results["timestamps"][start:stop] = _read(fp, "<{:d}Q".format(num_items))
            results["assignments"][start:stop] = _read(fp, "<{:d}H".format(num_items))
            for j in range(start, stop):
                results["spikes"][j, :] = _read(fp, "<{:d}h".format(results["num_points"]))

        # Save our results in the info structure
        info["spike_channels"][channel]["timestamps"] = results["timestamps"]
        info["spike_channels"][channel]["assignments"] = results["assignments"]
        info["spike_channels"][channel]["spikes"] = results["spikes"]

    results = dict()
    results["timestamps"] = np.array(info["spike_channels"][channel]["timestamps"])
    results["assignments"] = np.array(info["spike_channels"][channel]["assignments"])
    results["spikes"] = np.array(info["spike_channels"][channel]["spikes"])
    if spike_number is not None:
        select = np.array(results["assignments"]) == spike_number
        results["timestamps"] = results["timestamps"][select]
        results["spikes"] = results["spikes"][select, :]
        results["assignments"] = results["assignments"][select]
    if scale:
        results["timestamps"] = results["timestamps"].astype(np.float)
        results["timestamps"] /= info["timestamp_frequency"]
        results["spikes"] = results["spikes"].astype(np.float)
        results["spikes"] *= info["spike_channels"][channel]["coeff_to_convert_to_units"] * 1000  # to mV
    return results


def _read(fp: IO, data_types: str, force_list=False) -> Union[Any, Tuple[Any], List[Any]]:
    """
    Read a series of bytes starting at the current file location and unpack them using struct.unpack(). This function
    serves to avoid needing a byte array that is exactly the same size of the size of the data type

    Args:
        fp: The PL2 file object. The file pointer advances to the byte after the series of bytes unpacked.
        data_types: A string defining how to unpack the bytes starting at the current file location.
        force_list: If True, the unpacked data is always returned as a list. Otherwise, returns a single unpacked value
            or a tuple (if more than one unpacked value).

    Returns:
        The unpacked value or values
    """
    num_bytes = struct.calcsize(data_types)
    _read_bytes = fp.read(num_bytes)
    values = struct.unpack(data_types, _read_bytes)
    if len(values) == 1:
        if force_list:
            return [values[0]]
        return values[0]
    else:
        if force_list:
            return list(values)
        return values


def _read_header(fp: IO, data: Dict[str, Any]) -> None:
    """
    Read the contents of the PL2 file header.

    Args:
        fp: The PL2 file object. Upon return, the file pointer should be positioned on the byte after the header.
        data: A dictionary in which the header information is stored.
    """
    fp.seek(0, os.SEEK_END)
    data["file_length"] = fp.tell()
    fp.seek(0)

    data["version"] = {}
    data["version"]["major_version"] = _read(fp, "<B")
    data["version"]["minor_version"] = _read(fp, "<B")
    data["version"]["bug_version"] = _read(fp, "<B")

    fp.seek(0x20)
    data["internal_value_1"] = _read(fp, "<Q")  # End of header
    data["internal_value_2"] = _read(fp, "<Q")  # First data block
    data["internal_value_3"] = _read(fp, "<Q")  # End of data blocks
    data["internal_value_4"] = _read(fp, "<Q")  # This is the start of footer
    data["start_recording_time_ticks"] = _read(fp, "<Q")
    data["duration_of_recording_ticks"] = _read(fp, "<Q")

    fp.seek(0xE0)
    data["creator_comment"] = bytearray(_read(fp, "<256B")).decode('ascii').split('\0', 1)[0]
    data["creator_software_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["creator_software_version"] = bytearray(_read(fp, "<16B")).decode('ascii').split('\0', 1)[0]
    data["creator_date_time"] = _read_date_time(fp)
    data["timestamp_frequency"] = _read(fp, "<d")
    data["duration_of_recording_sec"] = data["duration_of_recording_ticks"] / data["timestamp_frequency"]
    _read(fp, "<I")  # Off by 4 bytes
    data["total_number_of_spike_channels"] = _read(fp, "<I")
    data["number_of_recorded_spike_channels"] = _read(fp, "<I")
    data["total_number_of_analog_channels"] = _read(fp, "<I")
    data["number_of_recorded_analog_channels"] = _read(fp, "<I")
    data["number_of_event_channels"] = _read(fp, "<I")
    data["minimum_trodality"] = _read(fp, "<I")
    data["maximum_trodality"] = _read(fp, "<I")
    data["number_of_non_omniplex_sources"] = _read(fp, "<I")

    fp.seek(4, os.SEEK_CUR)
    data["reprocessor_comment"] = bytearray(_read(fp, "<256B")).decode('ascii').split('\0', 1)[0]
    data["reprocessor_software_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    # data["reprocessor_date_time"] = _read_date_time(fp)


def _parse_data_blocks(fp: IO, data: Dict[str, Any]) -> None:
    """
    Parses the data blocks in the PL2 file in sequence, appending items to the data dictionary as they are unpacked.
    This function ensures that all of the data is read without relying on the data in the footer.

    Args:
        fp: The PL2 file object. Upon return, the file pointer should be positioned on the next 16-byte boundary after
            the last data block.
        data: The dictionary in which all data parsed from the file is stored. It must already contain, at a minimum,
            the contents of the file header, since this information is needed to parse the data blocks.
    """
    # Seek to the first data block and begin _reading
    fp.seek(data["internal_value_2"])

    if data["internal_value_3"] == 0:
        data["internal_value_3"] = data["internal_value_4"]  # File is not complete

    while fp.tell() < data["internal_value_3"]:
        # read type
        data_type = _read(fp, "<B")
        data_subtype = _read(fp, "<B")
        offset = _get_channel_offset(data, data_subtype)

        if data_type == PL2_DATA_BLOCK_ANALOG_CHANNEL:
            num_items = _read(fp, "<H")
            channel = _read(fp, "<H") + offset - 1
            _read(fp, "<H")  # Unknown
            _read(fp, "<Q")  # timestamp? not used

            # read each of the items
            values = _read(fp, "<{:d}h".format(num_items), True)

            # Store in the output
            if "values" not in data["analog_channels"][channel]:
                data["analog_channels"][channel]["values"] = values
            else:
                data["analog_channels"][channel]["values"].extend(values)
        elif data_type == PL2_DATA_BLOCK_SPIKE_CHANNEL:
            _read(fp, "<H")
            channel = _read(fp, "<H") + offset - 1
            num_sample_points = _read(fp, "<H")
            num_items = _read(fp, "<Q")

            # _read each of the items (64 byte values)
            timestamps = _read(fp, "<{:d}Q".format(num_items), True)
            assignments = _read(fp, "<{:d}H".format(num_items), True)  # These are probably assignments
            spikes = np.zeros((num_items, num_sample_points), dtype=np.int16)
            for i in range(0, num_items):
                spikes[i, :] = _read(fp, "<{:d}h".format(num_sample_points))  # _read actual sample points
            if "timestamps" not in data["spike_channels"][channel]:
                data["spike_channels"][channel]["timestamps"] = timestamps
                data["spike_channels"][channel]["assignments"] = assignments
                data["spike_channels"][channel]["spikes"] = spikes
            else:
                data["spike_channels"][channel]["timestamps"].extend(timestamps)
                data["spike_channels"][channel]["assignments"].extend(assignments)
                data["spike_channels"][channel]["spikes"] = np.append(data["spike_channels"][channel]["spikes"],
                                                                      spikes, axis=0)
        elif data_type == PL2_DATA_BLOCK_EVENT_CHANNEL:
            _read(fp, "<H")
            channel = _read(fp, "<H") + offset - 1
            num_items = _read(fp, "<Q")
            _read(fp, "<H")
            timestamps = _read(fp, "<{:d}Q".format(num_items), True)
            strobed = _read(fp, "<{:d}H".format(num_items), True)

            if "timestamps" not in data["event_channels"][channel]:
                data["event_channels"][channel]["timestamps"] = timestamps
                data["event_channels"][channel]["strobed"] = strobed
            else:
                data["event_channels"][channel]["timestamps"].extend(timestamps)
                data["event_channels"][channel]["strobed"].extend(strobed)
        elif data_type == PL2_DATA_BLOCK_START_STOP_CHANNEL:
            # Start-stop Channel
            _read(fp, "<H")
            _read(fp, "<H") - 1  # channel - not used
            num_items = _read(fp, "<Q")
            timestamps = list(_read(fp, "<{:d}Q".format(num_items)))
            assignments = list(_read(fp, "<{:d}H".format(num_items)))
            if "timestamps" not in data["start_stop_channels"]:
                data["start_stop_channels"]["timestamps"] = timestamps
                data["start_stop_channels"]["assignments"] = assignments
            else:
                data["start_stop_channels"]["timestamps"].extend(timestamps)
                data["start_stop_channels"]["assignments"].extend(assignments)
        else:
            raise RuntimeError("Unknown data type at position ", fp.tell() - 2, "Got value: ", data_type)
        # Align to next 16 byte boundary
        fp.seek(int((fp.tell() + 15) / 16) * 16)


def _read_date_time(fp: IO) -> Dict[str, int]:
    """
    Read a date/time structure starting at the current location in the file.

    Args:
        fp: The PL2 file object. Upon return, current location is on the byte immediately after the date/time structure.

    Returns:
        A dictionary holding the date/time structure, with fields 'second', 'minute', 'hour', 'month_day', 'month',
        'year', 'week_day', 'year_day', 'is_daylight_savings', and 'millisecond'. All integer values.
    """
    data = dict()
    data["second"] = _read(fp, "<I")
    data["minute"] = _read(fp, "<I")
    data["hour"] = _read(fp, "<I")
    data["month_day"] = _read(fp, "<I")
    data["month"] = _read(fp, "<I")
    data["year"] = _read(fp, "<I")
    data["week_day"] = _read(fp, "<I")
    data["year_day"] = _read(fp, "<I")
    data["is_daylight_savings"] = _read(fp, "<I")
    data["millisecond"] = _read(fp, "<I")
    return data


def _read_spike_channel_header(fp: IO) -> Dict[str, Any]:
    """
    Read the header for a spike data channel in the file, starting at the current file location.

    Args:
        fp: The PL2 file object. Upon return, current location is immediately after the spike channel header.

    Returns:
        A dictionary holding the contents of the spike channel header.
    """
    data = {}
    data_type = _read(fp, "<B")
    _read(fp, "<3B")  # data_subtype - not used
    if data_type != PL2_HEADER_SPIKE_CHANNEL:  # D5 06 08 05
        raise RuntimeError(f"Invalid type in spike channel header. Got {data_type}, "
                           f"expected {PL2_HEADER_SPIKE_CHANNEL}")

    data["plex_channel"] = _read(fp, "<I")

    # Two more empty items
    _read(fp, "<I")
    _read(fp, "<I")

    data["name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["source"] = _read(fp, "<I")
    data["channel"] = _read(fp, "<I")
    data["enabled"] = _read(fp, "<I")
    data["recording_enabled"] = _read(fp, "<I")
    data["units"] = bytearray(_read(fp, "<16B")).decode('ascii').split('\0', 1)[0]
    data["samples_per_second"] = _read(fp, "<d")
    data["coeff_to_convert_to_units"] = _read(fp, "<d")
    data["samples_per_spike"] = _read(fp, "<I")
    data["threshold"] = _read(fp, "<i")
    data["pre_threshold_samples"] = _read(fp, "<I")
    data["sort_enabled"] = _read(fp, "<I")
    data["sort_method"] = _read(fp, "<I")
    data["number_of_units"] = _read(fp, "<I")
    data["sort_range_start"] = _read(fp, "<I")
    data["sort_range_end"] = _read(fp, "<I")
    data["unit_counts"] = list(_read(fp, "<256Q"))
    data["source_trodality"] = _read(fp, "<I")
    data["trode"] = _read(fp, "<I")
    data["channel_in_trode"] = _read(fp, "<I")
    data["number_of_channels_in_source"] = _read(fp, "<I")
    data["device_id"] = _read(fp, "<I")
    data["number_of_channels_in_device"] = _read(fp, "<I")
    data["source_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["source_device_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["probe_device_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["probe_source_id"] = _read(fp, "<I")
    data["probe_device_channel"] = _read(fp, "<I")
    data["probe_device_channel"] = _read(fp, "<I")
    data["probe_device_id"] = _read(fp, "<I")
    data["input_voltage_minimum"] = _read(fp, "<d")
    data["input_voltage_maximum"] = _read(fp, "<d")
    data["total_gain"] = _read(fp, "<d")

    # Create empty vectors for our block offsets
    data["block_offsets"] = []
    data["block_num_items"] = []
    data["block_timestamps"] = []
    data["num_spikes"] = 0

    # Skip 128 bytes
    fp.seek(128, os.SEEK_CUR)
    return data


def _read_analog_channel_header(fp: IO):
    """
    Read the header for an analog data channel in the file, starting at the current file location.

    Args:
        fp: The PL2 file object. Upon return, current location is immediately after the analog channel header.

    Returns:
        A dictionary holding the contents of the analog channel header.
    """
    data = {}

    data_type = _read(fp, "<B")
    _read(fp, "<3B")  # data_subtype - not used
    if data_type != PL2_HEADER_ANALOG_CHANNEL:  # D4 03 F8 00 or D4 04 F8 00 01, D4 07 F8 00
        raise RuntimeError(f"Invalid type in analog channel header. Got {data_type}, "
                           f"expected {PL2_HEADER_ANALOG_CHANNEL}")
    data["plex_channel"] = _read(fp, "<I")

    # Two more empty items
    _read(fp, "<I")
    _read(fp, "<I")

    data["name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["source"] = _read(fp, "<I")
    data["channel"] = _read(fp, "<I")
    data["enabled"] = _read(fp, "<I")
    data["recording_enabled"] = _read(fp, "<I")
    data["units"] = bytearray(_read(fp, "<16B")).decode('ascii').split('\0', 1)[0]
    data["samples_per_second"] = _read(fp, "<d")
    data["coeff_to_convert_to_units"] = _read(fp, "<d")
    data["source_trodality"] = _read(fp, "<I")
    data["trode"] = _read(fp, "<I")
    data["channel_in_trode"] = _read(fp, "<I")
    data["number_of_channels_in_source"] = _read(fp, "<I")
    data["device_id"] = _read(fp, "<I")
    data["number_of_channels_in_device"] = _read(fp, "<I")
    data["source_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["source_device_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["probe_device_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["probe_source_id"] = _read(fp, "<I")
    data["probe_source_channel"] = _read(fp, "<I")
    data["probe_device_id"] = _read(fp, "<I")
    data["probe_device_channel"] = _read(fp, "<I")
    data["input_voltage_minimum"] = _read(fp, "<d")
    data["input_voltage_maximum"] = _read(fp, "<d")
    data["total_gain"] = _read(fp, "<d")

    # Create empty vectors for our block offsets
    data["block_offsets"] = []
    data["block_num_items"] = []
    data["block_timestamps"] = []
    data["num_values"] = 0

    # Skip 128 bytes
    fp.seek(128, os.SEEK_CUR)
    return data


def _read_event_channel_header(fp: IO):
    """
    Read the header for an event channel in the file, starting at the current file location.

    Args:
        fp: The PL2 file object. Upon return, current location is immediately after the event channel header.

    Returns:
        A dictionary holding the contents of the event channel header.
    """
    data = {}

    data_type = _read(fp, "<B")
    _read(fp, "<3B")  # data_subtype - not used
    if data_type != PL2_HEADER_EVENT_CHANNEL:  # D6
        raise RuntimeError(f"Invalid type in analog channel header. Got {data_type}, "
                           f"expected {PL2_HEADER_EVENT_CHANNEL}")
    data["plex_channel"] = _read(fp, "<I")

    # Two more empty items
    _read(fp, "<I")
    _read(fp, "<I")

    data["name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["source"] = _read(fp, "<I")
    data["channel"] = _read(fp, "<I")
    data["enabled"] = _read(fp, "<I")
    data["recording_enabled"] = _read(fp, "<I")
    data["number_of_channels_in_source"] = _read(fp, "<I")
    data["number_of_channels_in_device"] = _read(fp, "<I")
    data["device_id"] = _read(fp, "<I")
    data["num_events"] = _read(fp, "<I")  # TODO - this is not right
    data["source_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]
    data["source_device_name"] = bytearray(_read(fp, "<64B")).decode('ascii').split('\0', 1)[0]

    # Create empty vectors for our block offsets
    data["block_offsets"] = []
    data["block_num_items"] = []
    data["block_timestamps"] = []
    data["num_events"] = 0

    # Skip 128 bytes
    fp.seek(128, os.SEEK_CUR)
    return data


def _get_channel_offset(data: Dict[str, Any], data_subtype: int) -> int:
    """
    Return the offset in the type of channel given the data subtype.
    Args:
        data: Dictionary holding PL2 file contents culled thus far.
        data_subtype: The channel data subtype. Must be one of PL2_ANALOG_TYPE_WB, _AI, _FP, _SPKC;
            PL2_EVENT_TYPE_SINGLE_BIT, _STROBED; PL2_SPIKE_TYPE_SPK, or PL2_SPIKE_TYPE_SPK_SPKC.

    Returns:
        The offset value
    """
    if data_subtype == PL2_ANALOG_TYPE_WB:
        offset = next(x for x in range(len(data["analog_channels"]))
                      if data["analog_channels"][x]["source_name"] == "WB")
    elif data_subtype == PL2_ANALOG_TYPE_AI:
        offset = next(x for x in range(len(data["analog_channels"]))
                      if data["analog_channels"][x]["source_name"] == "AI")
    elif data_subtype == PL2_ANALOG_TYPE_FP:
        offset = next(x for x in range(len(data["analog_channels"]))
                      if data["analog_channels"][x]["source_name"] == "FP")
    elif data_subtype == PL2_ANALOG_TYPE_SPKC:
        offset = next(x for x in range(len(data["analog_channels"]))
                      if data["analog_channels"][x]["source_name"] == "SPKC")
    elif data_subtype == PL2_EVENT_TYPE_SINGLE_BIT:
        offset = next(x for x in range(len(data["event_channels"]))
                      if data["event_channels"][x]["source_name"] == "Single-bit events")
    elif data_subtype == PL2_EVENT_TYPE_STROBED:
        offset = next(x for x in range(len(data["event_channels"]))
                      if data["event_channels"][x]["source_name"] == "Other events")
    elif data_subtype == PL2_SPIKE_TYPE_SPK:
        offset = next(x for x in range(len(data["spike_channels"]))
                      if data["spike_channels"][x]["source_name"] == "SPK")
    elif data_subtype == PL2_SPIKE_TYPE_SPK_SPKC:
        offset = next(x for x in range(len(data["spike_channels"]))
                      if data["spike_channels"][x]["source_name"] == "SPK_SPKC")
    elif data_subtype == 0x00:
        offset = 0
    else:
        raise RuntimeError(f"Unknown channel data subtype provided: 0x{data_subtype:x}")
    return offset


def _read_footer(fp: IO, data: Dict[str, Any]) -> None:
    """
    Parse the PL2 file's footer and add its contents to the data dictionary provided.
    Args:
        fp: The PL2 file object.
        data: The data/information dictionary culled from the file thus far. The footer contents are added to this. At a
            minimum, it must include the contents of the file header in order to locate the start of the footer.
    """
    fp.seek(data["internal_value_4"])  # Seek to start of footer

    while fp.tell() < data["file_length"]:
        data_type = _read(fp, "<B")
        data_subtype = _read(fp, "<B")

        # Get the offset for this type of data
        offset = _get_channel_offset(data, data_subtype)

        # All items are stored as the following
        num_words = _read(fp, "<H")
        channel = _read(fp, "<H") + offset - 1  # Python is base 0
        _read(fp, "<H")  # Skipped

        # Determine how many items we have based on the number of words
        # Each element is stored as position ("<Q"), timestamp ("<Q"),
        # and number of elements ("<H")
        num_items = int(num_words * 2 / (8 + 8 + 2))
        num_values = _read(fp, "<Q")

        if data_type == PL2_FOOTER_SPIKE_CHANNEL:
            data["spike_channels"][channel]["num_spikes"] = num_values
            data["spike_channels"][channel]["block_offsets"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["spike_channels"][channel]["block_timestamps"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["spike_channels"][channel]["block_num_items"] = _read(fp, "<{:d}H".format(num_items), True)
        elif data_type == PL2_FOOTER_ANALOG_CHANNEL:
            data["analog_channels"][channel]["num_values"] = num_values
            data["analog_channels"][channel]["block_offsets"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["analog_channels"][channel]["block_timestamps"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["analog_channels"][channel]["block_num_items"] = _read(fp, "<{:d}H".format(num_items), True)
        elif data_type == PL2_FOOTER_EVENT_CHANNEL:
            data["event_channels"][channel]["num_events"] = num_values
            data["event_channels"][channel]["block_offsets"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["event_channels"][channel]["block_timestamps"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["event_channels"][channel]["block_num_items"] = _read(fp, "<{:d}H".format(num_items), True)
        elif data_type == PL2_FOOTER_START_STOP_CHANNEL:
            data["start_stop_channels"]["num_events"] = _read(fp, "<Q")
            data["start_stop_channels"]["block_offsets"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["start_stop_channels"]["block_timestamps"] = _read(fp, "<{:d}Q".format(num_items), True)
            data["start_stop_channels"]["block_num_items"] = _read(fp, "<{:d}H".format(num_items), True)
        else:
            raise RuntimeError(f"Unknown data type in footer at {fp.tell()-2}: Got 0x{data_type:x}")
        # Skip to next 16 byte aligned value
        fp.seek(int((fp.tell() + 15) / 16) * 16)


def _reconstruct_footer(fp, data: Dict[str, Any]) -> None:
    """
    Given a PL2 file without a footer, attempt to reconstruct the footer by parsing individual data records.
    Args:
        fp: The PL2 file object. It must be open and is NOT closed upon return.
        data: The data/information dictionary culled from the file thus far. Fields of the reconstructed footer will be
            added to this dictionary.
    """
    # Seek to the first data block and begin _reading
    fp.seek(data["internal_value_2"])

    if data["internal_value_3"] == 0:
        data["internal_value_3"] = data["internal_value_4"]  # File is not complete

    while fp.tell() < data["internal_value_3"]:
        # _read type
        block_offset = fp.tell()
        data_type = _read(fp, "<B")
        data_subtype = _read(fp, "<B")
        offset = _get_channel_offset(data, data_subtype)

        if data_type == PL2_DATA_BLOCK_ANALOG_CHANNEL:
            num_items = _read(fp, "<H")
            channel = _read(fp, "<H") + offset - 1
            _read(fp, "<H")  # Unknown
            timestamp = _read(fp, "<Q")

            data["analog_channels"][channel]["num_values"] += num_items
            data["analog_channels"][channel]["block_offsets"].append(block_offset)
            data["analog_channels"][channel]["block_timestamps"].append(timestamp)
            data["analog_channels"][channel]["block_num_items"].append(num_items)

            # Skip over the values
            fp.seek(2 * num_items, os.SEEK_CUR)
        elif data_type == PL2_DATA_BLOCK_SPIKE_CHANNEL:
            _read(fp, "<H")
            channel = _read(fp, "<H") + offset - 1
            num_sample_points = _read(fp, "<H")
            num_items = _read(fp, "<Q")

            # _read each of the items (64 byte values)
            timestamp = _read(fp, "<Q")

            data["spike_channels"][channel]["num_spikes"] += num_items
            data["spike_channels"][channel]["block_offsets"].append(block_offset)
            data["spike_channels"][channel]["block_timestamps"].append(timestamp)
            data["spike_channels"][channel]["block_num_items"].append(num_items)

            # Skip to next instance
            # Spikes: Int16 * * num_items * num_samples pints
            # Assignments: Unit16 * num_items
            # Timestamps = Uint64 * num_items (but we read one already)
            fp.seek(2 * num_items + 2 * num_sample_points * num_items + (num_items - 1) * 8, os.SEEK_CUR)
        elif data_type == PL2_DATA_BLOCK_EVENT_CHANNEL:
            _read(fp, "<H")
            channel = _read(fp, "<H") + offset - 1
            num_items = _read(fp, "<Q")
            _read(fp, "<H")
            timestamp = _read(fp, "<Q")

            data["event_channels"][channel]["num_events"] += num_items
            data["event_channels"][channel]["block_offsets"].append(block_offset)
            data["event_channels"][channel]["block_timestamps"].append(timestamp)
            data["event_channels"][channel]["block_num_items"].append(num_items)

            # Skip to next item
            fp.seek((num_items - 1) * 8 + num_items * 2, os.SEEK_CUR)

        elif data_type == PL2_DATA_BLOCK_START_STOP_CHANNEL:
            # Start-stop Channel
            _read(fp, "<H")
            _read(fp, "<H") - 1  # channel - not used
            num_items = _read(fp, "<Q")
            timestamp = _read(fp, "<Q")

            data["start_stop_channels"]["num_events"] += num_items
            data["start_stop_channels"]["block_offsets"].append(block_offset)
            data["start_stop_channels"]["block_timestamps"].append(timestamp)
            data["start_stop_channels"]["block_num_items"].append(num_items)
            fp.seek((num_items - 1) * 8 + num_items * 2, os.SEEK_CUR)
        else:
            raise RuntimeError(f"Unknown data type at position {fp.tell()-2}. Got 0x{data_type:x}")
        # Align to next 16 byte boundary
        fp.seek(int((fp.tell() + 15) / 16) * 16)
