"""
spikes.py: Structures and functions for digesting spike sorting results and aligning with Maestro trial data.

To commit data from an experiment to the Lisberger lab database, the researcher must compress all of the session data
files into a single ZIP archive. For those experiments that include electrophysiological recordings with the Plexon MAP
or Omniplex system, the following data files must be present in the archive.

    1) All Maestro trial data files.
    2) A single pickle file (other formats may be supported in the future) containing the results of the researcher's
       own spike-sorting analysis.
    3) One or more Omniplex PL2 files containing the original Omniplex-recorded data from which the sorted spike trains
       were derived.

The pickle file is identified by the extension '.pickle' or '.pkl', and there must be only one such file in the archive.
It must contain a single dictionary with 2 or 3 keys: 'channel' is a List[str] where the N-th element is the name of the
Omniplex source channel on which a neural unit was detected, 'filename' is a List[str] where the N-the element in the
name of the PL2 file in which the neural unit was recorded (this field is present ONLY if the archive contains more than
one PL2 file), and 'spiketimes' is a List[] where the N-the element is a Numpy array containing the spike timestamps for
that neural unit. The timestamps are single-precision floats in seconds since the start of the Omniplex recording.

This module contains functions to digest the neural unit data in the pickle and PL2 files, prepare or calculate
information about each unit that will be added to the lab database, including the unit's response during each recorded
Maestro trial, with the spike times converted to the trial's timeline.

@author: sruffner
@created: 21jan2021
"""
from typing import Optional, Dict, List, Tuple, NamedTuple, IO, Any
import numpy as np
import scipy.signal
import zipfile
import re
import pickle
import database.maestro as maestro
import database.PL2 as PL2


class OmniplexUnit(NamedTuple):
    """
    Tuple containing information that will be stored in the lab database for each identified neural unit in an Omniplex
    recording session. The Omniplex source filename, channel ID, and spike timestamps for each unit are extracted from
    the spike-sort results file that must be included in the session data ZIP archive when committing an experiment
    session to the Lisberger lab database. Other metrics are computed from the original Omniplex analog data stream
    from which the unit spike times were "sorted". The named tuple has the following attributes:

        source_file (str) - The name of the Omniplex PL2 file containing the analog data for the neural unit.

        channel (str) - The relevant source channel. The channel name starts with a short string identifier followed by
        a 2-digit number, e.g., 'WB01' (wide band channel 1).

        spike_times (np.ndarray) - A Numpy array holding the "sorted" spike times in seconds since the start of the
        Omniplex recording.

        firing_rate (float) - Mean firing rate in Hz (computed from spike times array).

        snr (float) - Signal-to-noise ratio (computed from spike times array and original analog data stream).

        template (np.ndarray) - Average spike template waveform (computed by averaging 10-ms "clips" of filtered
        analog channel stream starting 1ms before each spike timestamp in the spike times array).
    """
    source_file: str
    channel: str
    spike_times: np.ndarray
    firing_rate: float
    snr: float
    template: np.ndarray

    def summary(self) -> Dict[str, Any]:
        """
        Generate a summary of this Omniplex-recorded neural unit for display purposes only. Returns a dictionary with
        the following fields: 'channel_id' is the ID of the Omniplex analog data channel on which the unit was
        recorded (str); 'spike_times' is the list of spike timestamps for the unit, in seconds since start of the
        Omniplex recording (List[float]); 'firing_rate' is the unit's mean firing rate in Hz (float); 'snr' is the
        unit's estimated signal-to-noise ratio (float); and 'template' is a 10-ms clip of the average spike waveform
        in mV (List[float]).
        """
        return {'channel_id': self.channel,
                'spike_times': self.spike_times.tolist(),
                'firing_rate': self.firing_rate,
                'snr': self.snr,
                'template': self.template.tolist()}


def load_neural_units(archive: zipfile.ZipFile) -> List[OmniplexUnit]:
    """
    Process relevant files in the session data archive and prepare metrics on identified neural units.

    When researchers prepare the ZIP archive containing all data files for an experiment session including neural unit
    recordings, they must provide a single Python pickle file with the results of their spike-sorting analysis of all
    units recorded during the session. This pickle file contains a dictionary with 2-3 fields: 'channel', 'spiketimes',
    and (optionally) 'filename'. The last field is required ONLY if there is more than one Omniplex PL2 file in the
    archive. Each field is a list of length N, where N is the number of neural units. The 'channel' key holds the
    Omniplex-specific channel ID for the analog channel on which the unit was recorded, the 'filename' key holds the
    name of the Omniplex PL2 file within the ZIP archive, and the 'spiketimes' key holds the spike times (in seconds
    since the Omniplex recording started) for each unit, as a Numpy array.

    After parsing the pickle file, this method calculates select metrics for each identified neural unit (mean firing
    rate, signal-to-noise ratio, and the average spike template waveform) using the spike times in the pickle file and
    the original Omniplex analog data stream(s) from which those spike times were "sorted". These metrics are ultimately
    stored in the lab database.

    On calculating the template waveform and SNR for each neural unit: The channel ID in the pickle file must start with
    "WB" (wide band data) or "SPKC" (narrow band data). Wide band data is preferred because the filtering parameters for
    SPKC can be changed during an Omniplex session and are not stored in the PL2 file. If the specified channel ID is
    "SPKC<num>", where <num> is a 2-digit number, the method first looks for the wide-band channel "WB<num>". If that is
    available, the analog trace is bandpass-filtered between 300-8000Hz using a second-order Butterworth filter via the
    SciPy package. If not, the analog trace on "SPKC<num>" is used as is (it should already have been filtered).

    To calculate the template waveform, the method averages 10-ms "clips" in the filtered trace that start 1ms prior to
    each spike timestamp from the pickle file. To calculate SNR, the method first estimates the standard deviation of
    the background noise as 1.4826 * median absolute deviation (MAD) of the data trace. The MAD = median(abs(x-X)) =
    median(abs(x)) because X = median(x) is approximately 0 since the trace x has been bandpass-filtered, removing any
    DC offset. Then: SNR = (max(template) - min(template)) / 1.96 * std_background_noise.

    Args:
        archive: The ZIP archive containing all data files for the experiment session. Must be open and will NOT be
            closed upon return.

    Returns:
        List of neural units culled from the spikes data file, with channel ID, PL2 filename, spike times array, mean
            firing rate, SNR, and spike template waveform for each unit. Returns an empty list if the archive lacks any
            neural unit data.
    """
    units: List[OmniplexUnit] = list()
    archive_list = archive.infolist()
    pl2_filenames = [x.filename for x in archive_list if (len(x.filename) > 4) and x.filename[-4:].lower() == '.pl2']
    spikes_files = [x.filename for x in archive_list
                    if ((len(x.filename) > 7) and (x.filename[-7:].lower() == '.pickle')) or
                       ((len(x.filename) > 4) and (x.filename[-4:].lower() == '.pkl'))]
    if len(spikes_files) == 0:
        return units   # No neural unit data for this session!
    elif len(spikes_files) > 1:
        raise Exception("Found more than one spikes data file in session data archive!")
    elif len(pl2_filenames) == 0:
        raise Exception("Found no Omniplex PL2 files in session data archive!")

    # validate expected file contents. If 'filename' field not present, then there must be exactly one PL2 file. If it
    # is present, it must only contain PL2 files named in the session archive.
    unit_data = pickle.loads(archive.read(spikes_files[0]))
    ok = isinstance(unit_data, dict) and ('channel' in unit_data) and ('spiketimes' in unit_data)
    if ok:
        ok = isinstance(unit_data['channel'], list) and isinstance(unit_data['spiketimes'], list) and \
             len(unit_data['channel']) == len(unit_data['spiketimes']) and \
             all(isinstance(x, str) for x in unit_data['channel']) and \
             all(isinstance(x, np.ndarray) for x in unit_data['spiketimes'])
    if ok:
        if 'filename' in unit_data:
            ok = isinstance(unit_data['filename'], list) and (len(unit_data['filename']) == len(unit_data['channel'])) \
                 and all(x in pl2_filenames for x in unit_data['filename'])
        else:
            ok = (len(pl2_filenames) == 1)
    if not ok:
        raise Exception(f"Detected invalid spikes data file: {spikes_files[0]}")
    if 'filename' not in unit_data:
        unit_data['filename'] = [pl2_filenames[0]]*len(unit_data['channel'])

    # the unit's mean firing rate is simply the # of spikes divided by the time between the first and last one
    firing_rate = [float(len(x)) / (x[-1] - x[0]) for x in unit_data['spiketimes']]

    # we need look at original Omniplex recordings to calculate SNR and average spike waveform...
    snr: List[float] = [0.0]*len(unit_data['channel'])
    template: List[np.ndarray] = [np.zeros(1)]*len(unit_data['channel'])
    for pl2_filename in pl2_filenames:
        with archive.open(pl2_filename) as pl2_file:
            pl2_info = PL2.load_file_information(pl2_file)
            for i, file_name in enumerate(unit_data['filename']):
                if file_name == pl2_filename:
                    # if narrow band channel SPKC<num> specified, use wide band channel WB<num> if it is available
                    channel_id = unit_data['channel'][i]
                    is_wide_band = (len(channel_id) > 2) and (channel_id[0:2].lower() == 'wb')
                    is_narrow_band = (len(channel_id) > 4) and (channel_id[0:4].lower() == 'spkc')
                    if not (is_wide_band or is_narrow_band):
                        raise RuntimeError(f"Bad Omniplex channel ID: {channel_id}")
                    ch_index = -1
                    try:
                        if is_narrow_band:
                            alt_id = "WB" + channel_id[-2:]
                            ch_index = [ch['name'] for ch in pl2_info['analog_channels']].index(alt_id)
                            is_wide_band = True
                        else:
                            ch_index = [ch['name'] for ch in pl2_info['analog_channels']].index(channel_id)
                    except ValueError:
                        pass
                    if ch_index == -1:
                        raise RuntimeError(f"Did not find Omniplex analog channel data for channel ID: {channel_id}")

                    # TODO: If more than one unit is on the same channel, then we'll be repeating some operations
                    # here -- the bandpass filtering, and the SNR calculation...
                    samples = PL2.load_analog_channel(pl2_file, ch_index, pl2_info, True)   # converted to float mV
                    samples_per_sec: float = pl2_info['analog_channels'][ch_index]['samples_per_second']
                    if is_wide_band:
                        samples = _bandpass_filter_wide_band_stream(samples, samples_per_sec)
                    samples_in_template = int(samples_per_sec * 0.01)
                    total_samples = len(samples)
                    template[i] = np.zeros(samples_in_template)
                    if len(unit_data['spiketimes'][i]) > 0:
                        num_good_clips = 0
                        for ts in unit_data['spiketimes'][i]:
                            start = int((ts - 0.001) * samples_per_sec)
                            end = start + samples_in_template
                            if (start >= 0) and (end < total_samples):
                                template[i] = np.add(template[i], samples[start:end])
                                num_good_clips += 1
                        template[i] /= num_good_clips
                        signal = np.max(template[i]) - np.min(template[i])
                        # MAD estimate of std of background noise for bandpassed trace (median(x) ~ 0)
                        noise = np.median(np.abs(samples)) * 1.4826
                        snr[i] = signal / (1.96 * noise)

    # assemble results as list of named tuples
    for i, ch in enumerate(unit_data['channel']):
        units.append(OmniplexUnit._make([unit_data['filename'][i], str(ch), unit_data['spiketimes'][i], firing_rate[i],
                                         snr[i], template[i]]))
    return units


def _bandpass_filter_wide_band_stream(data: np.ndarray, sample_rate_hz: float) -> np.ndarray:
    [b, a] = scipy.signal.butter(2, [2 * 300 / sample_rate_hz, 2 * 8000 / sample_rate_hz], btype='bandpass')
    return scipy.signal.lfilter(b, a, data)


class TrialTiming(NamedTuple):
    """
    Tuple of timing information used to determine the order in which trials were presented during an experiment and to
    align spike times of neural units recorded on the Omniplex system with respect to the timeline of the Maestro trials
    in which behavioral response data is recorded. The named tuple has the following attributes:

        file_index (int) - The trial data file's 4-digit numeric string extension converted to an integer.

        header_timestamp (int) - The internal timestamp found in the data file header, in ms since Maestro started. Will
        be available for all data files with version >= 21.

        duration (float) - The trial duration in seconds, as culled from the data file header.

        omniplex_start (float) - The Omniplex timestamp for the XS2 pulse delivered at the start of the trial, in
        seconds since the Omniplex recording began.

        omniplex_stop (float) - The Omniplex timestamp for the XS2 pulse delivered at the end of the trial, in seconds
        since the Omniplex recording began.

    For behavior-only sessions, the last two attributes will be None, since there is no Omniplex data. For these
    sessions, we rely only on the internal timestamps to determine the trial order. If those timestamps are unavailable,
    then we rely on the file indices. When the Omniplex data is available, then it is the start/stop times as recorded
    on the Omniplex that determine both the trial presentation order and the conversion of neural unit spike times to
    the individual Maestro trial timelines.
    """
    file_index: int
    header_timestamp: Optional[int]
    duration: float
    omniplex_start: Optional[float]
    omniplex_stop: Optional[float]


def load_trial_timings(archive: zipfile.ZipFile) -> Dict[str, TrialTiming]:
    """
    Load trial timing information for all Maestro trial data files stored in the session data archive. This information
    is primarily used to align neural unit responses recorded in the Omniplex system with the behavioral responses
    recorded in each individual Maestro trial. It can also be used to verify the order in which trials were presented
    over the course of the session.

    Args:
        archive: The ZIP archive containing all data files for the experiment session. Must be open and will NOT be
            closed upon return.

    Returns:
        A dictionary mapping the filename of each Maestro data file in the archive to a named tuple listing the timing
        information for that trial.
    """
    # find all Maestro trial data files in ZIP and parse headers for internal timestamp and trial duration
    archive_list = archive.infolist()
    data_file_name_pattern = re.compile('.[0-9][0-9][0-9][0-9]+$')
    result: Dict[str, TrialTiming] = dict()
    for info in archive_list:
        if data_file_name_pattern.search(info.filename) is not None:
            header = maestro.DataFileHeader.parse_header(archive.read(info))
            file_index = int(info.filename[-4:])
            header_timestamp = header.timestamp_ms if header.version >= 21 else None
            duration = float(header.num_scans_saved - 1) / 1000.0   # Trial mode scan rate is fixed at 1KHz
            result[info.filename] = TrialTiming._make([file_index, header_timestamp, duration, None, None])
    if len(result) == 0:
        raise Exception("No Maestro data files found in session archive!")

    # examine any and all PL2 files in ZIP and analyze strobed character event channel and XS2 pulse event channel in
    # order to find the Omniplex start and stop timestamps for each trial found above.
    omniplex_timings: Dict[str, Tuple[float, float]] = dict()
    got_pl2_file = False
    for info in archive_list:
        if (len(info.filename) > 3) and (info.filename[-3:].lower() == 'pl2'):
            got_pl2_file = True
            with archive.open(info) as pl2_file:
                timings_dict = _get_trial_timing_from_pl2_file(pl2_file)
                if len(omniplex_timings.keys() & timings_dict.keys()) > 0:
                    raise Exception("Found duplicate Maestro data files in Omniplex recording!")
                omniplex_timings.update(timings_dict)

    # if there are PL2 file(s), then add the Omniplex start and stop timestamps for each trial data file. It is a fatal
    # error if any timestamps are missing.
    if got_pl2_file:
        if len(result.keys() & omniplex_timings.keys()) < len(result.keys()):
            raise Exception("Missing Omniplex timing information for one or more Maestro data files in archive")
        for key in result.keys():
            old = result[key]
            start_ts, stop_ts = omniplex_timings[key]
            result[key] = TrialTiming._make([old.file_index, old.header_timestamp, old.duration, start_ts, stop_ts])

    return result


def _get_trial_timing_from_pl2_file(fp: IO) -> Dict[str, Tuple[float, float]]:
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

    Returns:
        A dictionary mapping the name of each saved data file to a 2-tuple (start, stop) containing the start and stop
        timestamps of the corresponding Maestro trial in seconds since the start of the Omniplex recording. The
        dictionary will be empty if the expected event channel data is not found in the PL2 file.
    """
    result: Dict[str, Tuple[float, float]] = dict()
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
