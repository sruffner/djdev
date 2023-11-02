"""
commit_ops.py: Operations involved in committing an experiment session archive to the Lisberger lab portal database.

The behavioral and neural response data recorded during an experiment session can easily reach several gigabytes in
size, so uploading, processing and committing that data to the portal database can take a significant amount of time
and must be offloaded to a background process so that the portal backend remains responsive to client requests.

Furthermore, it is important that the backend itself be "stateless" so that, when deployed "to the cloud", multiple
replicas of the backend can run simultaneously in order to field requests from multiple clients. Committing an
experimment session to the database is an inherently stateful workflow, so we need a way to maintain that state.

Rather than use our MySQL/MariaDB database to store state for in-progress session commit jobs, we decided to use a
Redis server to cache this information. The Redis server does double-duty, since we use Redis Queue (RQ) workers to
handle the work during the preprocessing and commit stages of the workflow. Additional information is kept in the
staging directory for each pending commit job.

Two Redis keys are dedicated to session commit jobs:
   - COMMITS : Redis HASH set storing status information on all pending commit jobs. The fields (aka, keys) of the
     hash set are the job IDs, and the corresponding values are CommitJobStatus objects (converted to byte strings).
     The CommitJobStatus object holds essential job state information and a progress message history.
   - PROTODEFS_NS:<job_id> : Redis LIST of trial protocol definitions found during preprocessing. This key is present
     for the given job ONLY if that job enters the review phase because one or more of the trial protocols require
     user validation.

When the server starts a new session commit job, it initializes a CommitJobStatus object for the job and assigns it a
unique job ID of the form "<experimenter>_<subject ID>_<session date>_<suffix>", where:
   - <experimenter> is the registered username of the person that conducted the experiment.
   - <subject ID> is the ID of the experiment subject.
   - <session date> is the session recording date in ISO format, "YYYY-DD-MM".
   - <suffix> is the session suffix, an integer in [1..9], to distinguish multiple sessions on the same date.
Note that the registered user committing the session need not be the same user as the experimenter. Also note that the
four components of the job ID form the primary key that uniquely identifies an experiment session in the portal
database. These parameters are included in the session metadata that must accompany the client request to start a
session commit job.

Required contents of an experiment session archive: Two configurations are currently supported, depending on whether the
original Omniplex PL2 file is available.
1) Original Omniplex recording included in archive.
    - All Maestro trial files recorded during the experiment.
    - One or more (typically just one) Omniplex PL2 files containing the multi-electrode recording from which neural
unit data is obtained. Not applicable for behavioral-only experiments (no neural units).
    - A pickle file containing spike times for each neural unit recorded. The pickle file contains contains a dictionary
with 2-3 fields: 'channel', 'spiketimes', and (optionally) 'filename'. The last field is required ONLY if there is more
than one Omniplex PL2 file in the archive. Each field is a list of length N, where N is the number of neural units. The
'channel' key holds the Omniplex channel ID for the analog channel on which the unit was recorded, the 'filename' key
holds the name of the Omniplex PL2 file within the ZIP archive, and the 'spiketimes' key holds the spike times (in
seconds since the Omniplex recording started) for each unit, as a Numpy array.

2) Omniplex recording NOT included in archive. In this scenario, the Omniplex start/stop times for each Maestro trial
cannot be deduced, nor can the SNR or spike template waveform be calculated for each unit. Therefore, additional info
must be supplied:
    - All Maestro trial files recorded during the experiment.
    - A single CSV file containing the elapsed start time in the electrophysiological recording timeline (same timeline
in which all unit spike times are recorded) for each Maestro trial. Format: Each text line should read "trial_file, T"
where "trial_file" is the Maestro trial file name as it appears in the session archive and T is the start time for that
trial in milliseconds. Any text line not conforming to this format is ignored.
    - A pickle file containing spike times, estimated SNR, and the spike template waveform for each neural unit. The
file must contain a dictionary with the fields 'channel', 'spiketimes', 'snr', and 'template'. Each field is a list of
length N, where N is the number of neural units. The first two fields are the same as described above. The 'snr' field
is a list of the unit signal-to-noise ratios, and the 'template' field contains the unit spike waveforms -- each of
which is a Numpy array. To be consistent with how the template waveform is calculated from Omniplex data, it should
contain 0.01 * R samples, where R is the ephys recording sampling rate (40KHz for Omniplex) as reported in the session
metadata supplied when the session commit is initiated, and all samples should be in microvolts. The dictionary COULD
also contain the 'filename' field holding the name of the original Omniplex PL2 source file even though that file is
not part of the session archive.

The workflow for committing an experiment session has the following stages:
    0) Initialization. A committer (a registered user with "commit"-level access on the portal) can initiate a session
       commit in two ways: interactively through the portal website, or by using the `sglportalapi` package from a
       Python script or the Python console (this package communicates with the portal server through a number of
       REST-like API endpoints). The commit request must be accompanied by session metadata, plus a list of N neuron
       types, one for each neural unit recorded during the session. The server will check this metadata for validity.
       Providing the information in advance helps to automate the entire commit workflow.

       Once the commit request is validated, the server creates a subfolder in local storage (not S3) at
       `$PORTAL_WS/staging/<job_id>` and writes the session metadata and neuron types list to a file within that folder,
       `commit_info.bin`. It also initializes the CommitJobStatus object and stores that under the job's ID in the
       COMMIT_JOBS hash in Redis. The commit job now enters the upload phase.

    1) Uploading. During this phase, the session data archive (containing all Maestro and Omniplex files, as well as
       a file with neural unit spike times from spike sorting) is uploaded to the portal's backup repository in AWS S3,
       at the key `/staging/<job_id>/archive.ZIP`. If the commit is initiated via the `sglportalapi` package, the
       archive is uploaded directly to S3 via a multipart upload. If it is initiated on a browser client via the portal
       web application, a Dash uploader component transfers the archive in chunks to an upload folder in the portal
       server's workspace, at $PORTAL_WS/staging/<job_id>. Upon completion, the server queues a background task to
       recompose the original file from the chunks and then transfer it to S3 at the key /staging/<job_id>/archive.ZIP.
       Obviously, this is a slower route, but it will have to do until we can find a React-based solution to handle a
       direct upload to S3.

       In either scenario, once the upload to S3 is successfully completed, the server queues a background task to
       begin preprocessing the commit job.

    2) Preprocessing. The preprocessing task downloads the session archive from S3 to the job's staging directory in the
       portal workspace, then analyzes the archive to collect timing information on trials, parse out trial protocols
       presented, calculate neural unit metrics, etc. All of this information is added to the file `commit_info.bin` in
       the staging directory. IF any trial protocols require user validation, then the workflow enters an interactive
       review phase. Since the user may not be available to review the results immediately, the background task puts
       the job in the review phase, deletes the session archive from the staging directory, and terminates. However, if
       no protocols require validation, then the background task continues immediately to the final commit phase.

    3) Review. In this interactive stage, the user (on the client) reviews the results of preprocessing, validates any
       trial protocols that require it, then requests that the session be committed to the database. In response, the
       server queues another background task. The review stage is handled only on the portal website; there's currently
       no option to review the preprocessing results via the `sglportalapi` package.

    4) Commit. Here is where the experimental data is pushed into the various database tables in our schema. Again, the
       work is performed in a background process with no user interaction. If the workflow did not skip the review
       phase, then the session archive must be downloaded again from S3 to the staging directory in the portal workspace
       before the final commit can begin.

       Once the data from an experiment session has been fully committed to the portal database, the session archive and
       the preprocessing results are NOT discarded. Rather, the `commit_info.bin` file in the staging directory is added
       to the archive ZIP and then this ZIP file is uploaded to the portal backing repository in S3, under the key
       /repo/<experimenter>/<subject ID>_<session date>_<suffix>.zip. Note that the S3 key contains the four attributes
       comprising the primary key for the experiment session.

       Once the archive is safely backed up, the staging directory for the commit job is removed from the portal
       workspace, the original archive file uploaded to S3 at /staging/<job_ID>/archive.ZIP is deleted, and the job
       moves to the "Done" state.

    5) Cancelling. The user should be able to cancel the job during any of the stages. In the preprocessing and commit
       phases, it may be a little while before the background worker detects the cancellation and aborts.

    6) Done/Fail. The session commit completed successfully, or failed for whatever reason.

When a session is committed via the portal website and contains one or more trial protocols requiring user validation,
the large session archive must be transferred between the portal's workspace in cluster local storage and its backing
repository on S3 at least three times: (1) After chunked upload from the client browser, a background task reforms the
archive file from the chunks, then uploads it to S3. (2) Another background worker downloads the archive to the local
staging directory for preprocessing. (3) At some later time after successful review, a third task downloads the archive
to do the final commit.

There is a reason for this "wastefulness". Disk space on the portal server is MUCH more expensive than disk space on S3.
If we kept all pending session archives on the portal server, it is conceivable that we would need hundreds of GB, even
TB of space. With this scheme, the number of large archives stored on the portal server's volume is limited to the
number of commit jobs actively being processed at the same time (plus any active uploads via the portal website). The
number of active commit jobs is limited by the number of RQ worker processes running in the deployed portal application
(currently there are 2 such workers).

Session commits are restricted to registered users with the appropriate access level. Calls to this module should be
protected by a mechanism that verifies the specified user is logged in with the access level required.
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import csv
import json
import pickle
import re
import shutil
import struct
import sys
import time
import zipfile
from io import TextIOWrapper

import numpy as np
import scipy.signal
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Union, List, Dict, Any, Optional, Tuple, IO

from dash_uploader.httprequesthandler import get_chunk_name
from rq import Queue
from werkzeug.security import generate_password_hash

from config.app_logging import get_application_logger
from config.config import get_config
from database import repo
from database.repo import abort_multipart_upload, finish_multipart_upload
from sglportalapi import maestro, PL2
from database.log_ops import log_session_commit, read_log_entries
from database.table_info import DBTable, AttributeValue
from database.table_ops import fetch_attribute_values, fetch_rows, check_row, fetch_restrict_proj, \
    SessionCommitter, rollback_session_commit, database_empty, insert_into_table, delete_from_table, update_table_row, \
    update_mapping_table
from database.user_ops import validate_username, PASSWORD_HASH_METHOD, validate_password
from sglportalapi.PL2 import get_analog_channel_record_index
from sglportalapi.data_containers import SessionInfo
from sglportalapi.util import DocEnum

_logger = get_application_logger()


job_queue = Queue(connection=get_config().redis_conn)
""" Background jobs queue. """


COMMITS: str = 'commits'
""" Redis HASH set storing status information and progress history for all pending commit jobs, keyed by job ID. """
PROTODEFS_NS: str = 'protodefs:'
"""
Redis key namespace for the definitions of all trial protocols presented during an experiment session. Append commit job
ID to access the trial protocols for that job. Each element in the LIST is a serialized Protocol object. This key is 
present only after the preprocessing phase of the commit job has finished, but ONLY if user validation of one or more
trial protocols is required, in which case the commit job must enter the review phase.
"""
PROGRESS_HISTORY_SIZE: int = 30
""" Maximum number of messages kept in a commit job's progress message history."""
COMMIT_INFO_FNAME: str = 'commit_info.bin'
"""
When a commit job is initiated, the user-supplied session metadata and neuron types list are stored in this binary file
in the job's staging directory. After preprocessing, trial prototol definitions, trial timing information, and recorded
neural unit metrics are added to the file. After the review phase (if necessary), validatated trial protocol definitions
are updated in the file. Finally, after session commit, the file is added added to the original session archive ZIP so 
that database reconstruction can happen without user intervention.
"""
ARCHIVE_FNAME: str = 'archive.zip'
""" 
Regardless the original name of the session archive file, this is the name given to the file when it resides in a
dedicated staging area in the S3-based portal repository, or when being actively processed in the commit job's staging
folder in the portal's local workspace.
"""


class CommitStateEnum(DocEnum):
    """ Enum of commit job state codes. The 'doc' for each code serves as a short human-facing status string. """
    UPLOADING = 1, "Uploading archive ZIP"
    PREPROCESS = 2, "Preprocessing archive"
    REVIEW = 3, "Under user review"
    CANCEL = 4, "Cancelling job..."
    COMMIT = 5, "Committing to database"
    DONE = 6, "Successfully committed"
    FAIL = 7, "Failed"

    def get_state_descriptor(self) -> str:
        """ Get a very brief descriptor for this commit job state. """
        return self.__doc__

    def can_delete_job_in_this_state(self) -> bool:
        """
        Can commit job be deleted immediately in this state? In any state where a background process could be
        working on the job, the job should NOT be deleted. One exception -- cancellation is permitted while the
        archive is being uploaded
        """
        return self in [CommitStateEnum.UPLOADING, CommitStateEnum.REVIEW, CommitStateEnum.DONE, CommitStateEnum.FAIL]

    def after_preprocessing(self) -> bool:
        """ Does this commit job state represent any state after the preprocessing phase? """
        return self not in [CommitStateEnum.UPLOADING, CommitStateEnum.PREPROCESS]


class CommitJobStatus:
    """ Status information for a session commit job pending or in progress on the lab portal server. """
    def __init__(self, job_id: str, committer: str, is_api: bool, mupload_id: Optional[str] = None,
                 started: Optional[float] = None, updated: Optional[float] = None,
                 state: CommitStateEnum = CommitStateEnum.UPLOADING, messages: Optional[List[str]] = None):
        """
        Construct a session commit job status object. The commit job ID reflects the primary key of the experiment
        session being committed: <experimenter username>_<subject ID>_<session date ISO>_<session suffix>.
        Args:
            job_id: The job ID.
            committer: The registered portal user that initiated the commit job.
            is_api: True if commit was triggered by an API endpoint dedicated to the purpose; False if it was
                triggered on the relevant  page in the portal web app.
            mupload_id: The ID of the multipart upload task by which the session archive is uploaded directly to S3.
               This applies ONLY to a commit job initiated via the dedicated API endpoint.
            started: Timestamp (seconds since the Epoch) when job was started. If None, use the current time.
            updated: Timestamp when job was last updated. If None, use the value of the 'started' argument.
            state: The current job state.
            messages: Progress message history for this job, in reverse chronological order. If None, then the message
                list contains a single message indicating that archive is being uploaded from client.
        """
        self._definition: Dict[str, Any] = dict()
        self._definition['id'] = job_id
        self._definition['committer'] = committer
        self._definition['is_api'] = is_api
        self._definition['mupload_id'] = "" if ((not is_api) or (mupload_id is None)) else mupload_id
        self._definition['started'] = started if isinstance(started, float) else time.time()
        self._definition['updated'] = updated if isinstance(updated, float) else self._definition['started']
        self._definition['state'] = state
        self._definition['messages'] = messages if isinstance(messages, list) else ["Awaiting archive upload..."]

    @property
    def id(self) -> str:
        """ The commit job's ID. """
        return self._definition['id']

    @property
    def committer(self) -> str:
        """ The registered portal user that originated this commit job. """
        return self._definition['committer']

    @property
    def api_triggered(self) -> bool:
        """ True if session commit job was triggered via portal API endpoint rather than via interactive web page. """
        return self._definition['is_api']

    @property
    def mupload_id(self) -> str:
        """
        S3 multipart upload task ID for a session commit job initiated via portal API endpoint. Not applicable to
        a commit initiated via interactive web page (empty string). Upon leaving the 'upload' phase, the upload ID
        reads as 'done'.
        """
        return self._definition['mupload_id']

    @mupload_id.setter
    def mupload_id(self, uid: str) -> None:
        """
        Sets the ID os the S3 multipart upload ID task for a commit job triggered by a dedicated API endpoint. Has no
        effect for a commit job initiated via the 'commit' page in the portal web app."""
        if self.api_triggered:
            self._definition['mupload_id'] = uid

    @property
    def started(self) -> float:
        """ Timestamp (seconds since the Epoch) when commit job was initiated. """
        return self._definition['started']

    @property
    def updated(self) -> float:
        """ Timestamp (seconds since the Epoch) when last progress message was posted for the commit job. """
        return self._definition['updated']

    @property
    def state(self) -> CommitStateEnum:
        """ The job's current state/phase. """
        return self._definition['state']

    @property
    def msg(self) -> str:
        """ Text of the last progress message posted for the commit job. """
        return self._definition['messages'][0]

    @property
    def message_history(self) -> List[str]:
        """ The progress message history for the commit job, in reverse chronological order. """
        return self._definition['messages'].copy()

    def on_update(self, msg: str, overwrite: bool = False, state: Optional[CommitStateEnum] = None,
                  updated: Optional[float] = None) -> None:
        """
        Update this session commit job status.

        Args:
            msg: The latest progress message.
            overwrite: If True, the most recent progress message in the message history is overwritten with the message
                provided, rather than pushing the new message onto the history. This is useful when posting updates
                about a long running task indicating percent complete.
            state: If not None, the updated job state.
            updated: Timestamp for this update (seconds since Epoch). If None, use the current time.
        """
        if overwrite:
            self._definition['messages'][0] = msg
        else:
            self._definition['messages'].insert(0, msg)
            if len(self._definition['messages']) > PROGRESS_HISTORY_SIZE:
                self._definition['messages'].pop(PROGRESS_HISTORY_SIZE-1)
        if isinstance(state, CommitStateEnum):
            if (self._definition['state'] == CommitStateEnum.UPLOADING) and (len(self._definition['mupload_id']) > 0):
                self._definition['mupload_id'] = 'done' if state == CommitStateEnum.PREPROCESS else 'cancelled'
            self._definition['state'] = state
        self._definition['updated'] = updated if isinstance(updated, float) else time.time()

    def to_bytes(self) -> bytes:
        """ Encode this commit job status object as a byte sequence. """
        # we transform to JSON, but convert floats to hex strings to keep precision
        out = dict()
        for k, v in self._definition.items():
            out[k] = float(v).hex() if k in ['updated', 'started'] else v.value if k == 'state' else v
        return json.dumps(out).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> CommitJobStatus:
        """
        Reconstruct a commit job status from a byte sequence previously generated by to_bytes().

        Args:
            raw: A byte sequence
        Raises:
            ValueError: If deserialization fails for any reason.
        """
        try:
            d = json.loads(raw.decode())
            return CommitJobStatus(
                job_id=d['id'], committer=d['committer'], is_api=d['is_api'], mupload_id=d['mupload_id'],
                started=float.fromhex(d['started']), updated=float.fromhex(d['updated']),
                state=CommitStateEnum(d['state']), messages=d['messages'])
        except Exception as e:
            raise ValueError(f"Failed to deserialize CommitJobStatus: {str(e)}")


@dataclass
class _TrialInfo:
    """
    A data container to accumulate information about each trial presented during an experiment session during the
    pre-processing phase of the session commit workflow: (1) identity of the trial protocol to which each trial rep
    belongs, and (2) timing information used to determine the order in which trials were presented during the experiment
    and to align spike times of neural units recorded on a separate multi-electrode recording apparatus (typically the
    Plexon/Omniplex system) with respect to the timeline of the Maestro trials in which behavioral response data is
    recorded.

    Behavior-only experiments have no neural unit data. For these sessions, we rely only on the internal timestamps to
    determine the trial order. If those timestamps are unavailable, then we rely on the file indices. When the Omniplex
    PL2 file(s) is incldued in the session archive, it is the start/stop times as recorded on the Omniplex that
    determine both the trial presentation order and the conversion of neural unit spike times to the individual Maestro
    trial timelines. Finally, if neural unit data is included without the Omniplex file, the archive must contain a
    CSV file containing the elapsed time on the neural recording system (Omniplex) at which each trial started.
    """
    file_index: int
    """ The trial data file's 4-digit numeric string extension converted to an integer."""
    duration: float
    """ The trial duration in seconds, as culled from the data file header."""
    header_timestamp: Optional[int]
    """ The internal timestamp found in the data file header, in ms since Maestro started. Will be None for data files
    prior to version 21. """
    omniplex_start: Optional[float] = None
    """ For experiment session archives that include the original Omniplex PL2 recording file, this is the Omniplex
    timestamp for the XS2 pulse delivered at the start of the trial, in seconds since the Omniplex recording began. For
    archive lacking the PL2 file (but including neural unit data), this timestamp is instead extracted from a CSV file
    included in the archive. Will be None for behavior-only experiment sessions. """
    omniplex_stop: Optional[float] = None
    """ The Omniplex timestamp for the XS2 pulse delivered at the end of the trial, in seconds since the Omniplex
    recording began. Will be None for behavior-only experiment sessions, or if the Omniplex PL2 file is not included in
    the session archive. In the latter case, the trial start time is supplied by a separate CSV file, and the trial
    stop time is simply that the start time + the trial duration."""
    proto_index: Optional[int] = None
    """ The zero-based index into the list of all trial protocol candidates presented during the session. """
    proto_hash: Optional[str] = None
    """ The MD5 hexadecimal digest uniquely identifying the trial protocol for this particular trial instance. It is
    set just prior to committing the experiment session to the database. """


class OmniplexUnit:
    """
    Data object containing information that will be stored in the Session.Neuron part table in the lab database for each
    identified neural unit in an Omniplex recording session.

    The Omniplex source filename (if available), channel ID, and spike timestamps for each unit are extracted from the
    a pickle file that must be included in the session data archive when committing an experiment session to the
    database. The unit SNR and spike template waveform are typically computed from the original Omniplex analog data
    stream from which the unit spike times were "sorted". If the Omniplex PL2 source file(s) are NOT in the archive,
    those metrics must also be included in the pickle file.

    Intended for read-only use outside of this module.
    """
    def __init__(self, src: str, ch: str, spikes: Optional[np.ndarray], num_spikes: int, rate: float, snr: float,
                 template: np.ndarray, neuron_type: int = -1):
        """
        An Omniplex neural unitrecord, storing calculated metrics and the spike train recorded from this unit.

        NOTE: The session commit procedure now allows for an archive lacking the Omniplex PL2 file. Instead, the SNR
        and template waveform for a unit are included in additional fields in the Python pickle file holding information
        on all identificed neural units. When the PL2 file is available, SNR and a 10-ms template waveform are computed
        using the original Omniplex analog data. When the SNR and waveform are supplied by the experimenter in the
        pickle file, the waveform duration may be different. In this scenario, the duration is found by multiplying the
        waveform length in samples by the sampling rate, which is part of the session metadata.

        Args:
            src: The filename of the original Omniplex source file. If None, this will be set to "unknown".
            ch: Omniplex channel on which unit was recorded. This should be either a wide-band analog channel "WB<num>"
                or a narrow-band analog channel "SPKC<num>", where <num> is a 1-, 2- or 3-digit positive integer.
            spikes: The spike train, with times in seconds since neural recording started. May be None when using
                this structure to store unit metrics without the spike train, which can be VERY large.
            num_spikes: The total number of spikes recorded. If `spikes` is not None, then this argument is
                ignored and `len(spikes)` is the number of recorded spikes.
            rate: Estimated mean firing rate in Hz.
            snr: Estimated signal-to-noise ratio. See NOTE.
            template: The spike template waveform. See NOTE.
            neuron_type: The neuron type ID (-1 if not known).
        """
        self._definition: Dict[str, Any] = dict()
        """ The Omniplex-recorded neural unit as a dictionary of parameter values keyed by parameter names. """
        self._definition['source_file'] = "unkknown" if not isinstance(src, str) else src
        self._definition['channel'] = ch
        self._definition['spike_times'] = spikes
        self._definition['num_spikes'] = num_spikes if (spikes is None) else len(spikes)
        self._definition['firing_rate'] = rate
        self._definition['snr'] = snr
        self._definition['template'] = template
        self._definition['neuron_type'] = neuron_type

    @property
    def source_file(self) -> str:
        """
        The name of the original Omniplex PL2 file containing the analog data for the neural unit. Do NOT rely on
        this field. In an alternate acceepted format for a session archive, the PL2 file(s) are not present in the
        archive.
        """
        return self._definition['source_file']

    @property
    def channel(self) -> str:
        """
        The Omniplex source channel name, which consists of the tag 'WB' (wide-band analog) or 'SPKC' (narrow-band
        analog) followed by a 1-, 2-digit or 3-digit positive integer specifying the channel number. For example: "WB8",
        "WB08" and "WB008" all refer to wide-band analog channel #8.
        """
        return self._definition['channel']

    @property
    def spike_times(self) -> Optional[np.ndarray]:
        """
        Ordered train of spike times in seconds since the start of the Omniplex recording (1D Numpy array). If None,
        then spike train times were not saved in this record.
        """
        return self._definition['spike_times']

    @property
    def num_spikes(self) -> int:
        """ Number of spikes recorded/detected from this neurol unit. """
        return self._definition['num_spikes']

    @property
    def firing_rate(self) -> float:
        """ Mean firing rate in Hz (computed from spike times array). """
        return self._definition['firing_rate']

    @property
    def snr(self) -> float:
        """ Signal-to-noise ratio (computed from spike times array and original analog data stream). """
        return self._definition['snr']

    @property
    def template(self) -> np.ndarray:
        """
        Average spike template waveform. When the original Omniplex PL2 recording is available in the session archive,
        this is computed by averaging 10-ms clips of filtered analog channel stream starting 1ms before each timestamp
        in the spike times array). When no PL2 file is available and the template waveform is supplied directly by the
        committer (in the session archive's neural unit data file), the waveform duration and the method of computation
        will vary. Units = micro-volts.
        """
        return self._definition['template']

    @property
    def neuron_type(self) -> Optional[int]:
        """
        ID of the neuron type associated with this unit (value of primary key in NeuronType table). None if not yet set
        (must be manually selected by user during review phase of a session commit.
        """
        return None if self._definition['neuron_type'] <= 0 else self._definition['neuron_type']

    @neuron_type.setter
    def neuron_type(self, nt_id: Optional[int] = None) -> None:
        """ Update neuron type for this neural unit. Intended for internal use only. """
        self._definition['neuron_type'] = nt_id if (isinstance(nt_id, int) and nt_id > 0) else -1

    def to_bytes(self, omit_spikes: bool = False) -> bytes:
        """
        Serialize this Omniplex neural unit record as a byte sequence.

        Args:
            omit_spikes: If True, the unit's spike train (which can be very large) is NOT serialized. The number of
                spikes in the train is serialized.
        """

        hdr = dict(source_file=self.source_file, channel=self.channel, firing_rate=self.firing_rate.hex(),
                   snr=self.snr.hex(), neuron_type=self._definition['neuron_type'], num_spikes=self.num_spikes)
        hdr_raw = json.dumps(hdr).encode()
        if not omit_spikes:
            spikes_raw = self.spike_times.tobytes()
        else:
            spikes_raw = []
        template_raw = self.template.tobytes()
        out = bytearray(struct.pack("<3i", len(hdr_raw), len(spikes_raw), len(template_raw)))
        out.extend(hdr_raw)
        if not omit_spikes:
            out.extend(spikes_raw)
        out.extend(template_raw)
        return bytes(out)

    @staticmethod
    def from_bytes(raw: bytes) -> OmniplexUnit:
        """
        Reconstruct an Omniplex neurol unit record previously serialized by to_bytes().

        Args:
            raw: The byte sequence.
        Returns:
            The reconstructed Omniplex neural unit record.
        Raises:
            ValueError: If unable to parse byte sequence as an Omniplex unit record, for whatever reason.
        """
        try:
            hdr_len, spks_len, template_len = struct.unpack_from("<3i", raw, offset=0)
            offset = struct.calcsize("<3i")
            hdr = json.loads(raw[offset:offset+hdr_len].decode())
            offset += hdr_len
            spikes: Optional[np.ndarray] = None
            if spks_len > 0:
                spikes = np.frombuffer(raw[offset:offset+spks_len])
                offset += spks_len
            template = np.frombuffer(raw[offset:offset+template_len])
            return OmniplexUnit(src=hdr['source_file'], ch=hdr['channel'], spikes=spikes, num_spikes=hdr['num_spikes'],
                                rate=float.fromhex(hdr['firing_rate']), snr=float.fromhex(hdr['snr']),
                                template=template, neuron_type=hdr['neuron_type'])
        except Exception as e:
            raise ValueError(f"Failed to deserialize _OmniplexUnit record: {str(e)}")


def _get_job_subfolder(job_id: str) -> Path:
    """ The subfolder (in the portal workspace) in which key files are stored for the specified session commit job. """
    return Path(get_config().dash_upload_dir, job_id)


def _remove_job_subfolder(job_id: str) -> None:
    """ Remove the subfolder (in the portal workspace) dedicated to the commit job specified. """
    staging_dir = _get_job_subfolder(job_id)
    try:
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
    except Exception:
        _logger.error(f"Unexpected error deleting staging directory in repository at {str(staging_dir)}")


def initiate_session_commit(
        is_api: bool, committer: str, unit_types: List[str], experimenter: str, subject: str, rec_date: str,
        suffix: int, rig: str, study: str | int, notes: str, brain_area: Optional[str | int] = None,
        src: Optional[str] = None, probe: Optional[str] = None, rate: Optional[float] = None, x: Optional[float] = None,
        y: Optional[float] = None, z: Optional[float] = None) \
        -> Tuple[bool, str]:
    """
    Initiate a session commit job on the lab database server.

    The method first validates the supplied session metadata and neuron types list. The specified session must not yet
    exist in the portal database, nor be among the pending commit jobs. One neuron type must be specified for each
    neural unit recorded during the session, assumed to be in the order in which neural units are listed in the session
    archive. Neuron type names must be specified (not the opaque type IDs).

    If the supplied metadata is valid, the method generates a unique ID for the job, creates a dedicated subfolder for
    the commit job in the portal's workspace, writes the session and neural unit information to a binary file in that
    folder, and registers the job in Redis. The pending commit job starts in the "uploading" phase.

    Args:
        is_api: True if session commit is triggered by a request from the `sglportalapi` package to the 'commit' API
            endpoint; False if commit was triggerd through the relevant page in the portal web app. In the former case,
            the clientside will upload the session archive directly to the staging area in the portal's S3-base repo;
            in the latter case, a React-based uploader component uploads the archive to the portal's local workspace.
        committer: The username of the registered portal user requesting the session commit. The username is only
            checked for validity; it is ASSUMED that the specified user is currently logged-in and has the necessary
            privileges to commit experiment data to the portal.
        unit_types: The n-th element in this list is the neuron type assigned to the n-th neural unit recorded during
            the session. List length must match the number of neurons recorded. An empty list indicates a behavior-only
            session.
        experimenter: Username of the registered portal user that conducted the experiment.
        subject: ID of the subject of the experiment.
        rec_date: Recording date in ISO format - 'YYYY-MM-DD'.
        suffix: Session suffix in [1..9].
        rig: ID of the rig on which experiment was conducted.
        study: The research study to which experiment belongs -- specify either the study title or the unique integer
            key identifying the study in the portal database.
        notes: Session notes. Can be an empty string.
        brain_area: The region of brain in which neural units were recorded -- specify either the brain area name or the
            unique integer key identifying it in the portal database. None for behavioral session.
        src: The electrophysiology recording source. Must be one of 'Omniplex', 'Omniplex clips', 'Plexon MAP',
            'Maestro Waveform', 'Maestro Spike Ch'; currently, only 'Omniplex' supported. None for behavioral session.
        probe: The probe type. Must be one of 'single', '32-channel', 'other'. None for behavioral session.
        rate: The probe sampling rate in Hz. None for behavioral session.
        x: The X-coordinate of probe location within implant cylinder, in mm. None for behavioral sesion.
        y: The Y-coordinate of probe location within implant cylinder, in mm. None for behavioral sesion.
        z: Probe insertion depth in mm. None for behavioral sesion.
    Returns:
        A 2-tuple: (True, job ID) on success, or (False, error description) on failure. The client must supply the
            assigned job ID in all future requests involving the commit job.
    Raises:
        ValueError: If `committer` is an invalid username.
    """
    if not (isinstance(committer, str) and validate_username(committer)):
        raise ValueError('Invalid username for session committer')

    err_msg, info, nt_ids = _check_pending_session_metadata(unit_types, experimenter, subject, rec_date, suffix, rig,
                                                            study, notes, brain_area, src, probe, rate, x, y, z)
    if len(err_msg) > 0:
        return False, err_msg

    # create job subfolder and save session metadata to file there.
    job_id = f"{info.experimenter}_{info.subject}_{info.iso_recording_date}_{info.suffix}"
    job_folder = _get_job_subfolder(job_id)
    try:
        job_folder.mkdir(parents=True, exist_ok=False)
        _write_commit_info_file(Path(job_folder, COMMIT_INFO_FNAME), info, nt_ids)
    except Exception as err:
        err_msg = f"Error initializing staging folder for commit job {job_id}: {str(err)}"
        _logger.error(err_msg)
        return False, err_msg

    # register job in Redis -- another job with the same ID (ie, same experiment session primary key!) cannot exist!
    job_status = CommitJobStatus(job_id=job_id, committer=committer, is_api=is_api)
    try:
        conn = get_config().redis_conn
        ret = conn.hsetnx(name=COMMITS, key=job_id, value=job_status.to_bytes())
        if ret == 0:
            raise Exception("Commit job with same ID already pending!")
    except Exception as e:
        err_msg = f"Failed to register commit job: {str(e)}."
        _logger.error(err_msg)
        _remove_job_subfolder(job_id)
        return False, err_msg

    return True, job_id


def _check_pending_session_metadata(
        unit_types: List[str], experimenter: str, subject: str, rec_date: str, suffix: int, rig: str, study: str | int,
        notes: str, brain_area: Optional[str | int] = None, src: Optional[str] = None, probe: Optional[str] = None,
        rate: Optional[float] = None, x: Optional[float] = None, y: Optional[float] = None,
        z: Optional[float] = None) -> Tuple[str, Optional[SessionInfo], List[int]]:
    """
    Helper method for `initiate_session_commit()`. Validates the session information and neural unit types supplied when
    a user initiates a session commit job, and maps the neuron type names to their corresponding integer IDs in the
    NeuronType database table. The session must not already exist in the portal database, the session parameters must be
    valid, the length of the neuron types list must match the number of recorded units, and all listed neuron types must
    exist in the database.

    Returns:
        A 3-tuple. On success, ("", a SessionInfo object encapsulating validated session metadata, and a list of neuron
            type IDs corresponding to the list of type names provided). On failure, (error description, None, []).
    """
    # research study is specified by title or integer ID. Need both to initialize SessionInfo.
    study_title, study_id, study_ok = "", -1, False
    if isinstance(study, int):
        study_id = study
        res = fetch_attribute_values(DBTable.STUDY, "study_title", dict(study_id=study_id))
        study_ok = (len(res) == 1)
        study_title = res[0]
    elif isinstance(study, str):
        study_title = study
        rows = fetch_rows(DBTable.STUDY, dict(study_title=study_title))
        study_ok = (len(rows) == 1)
        study_id = rows[0]['study_id']
    if not study_ok:
        return "Invalid ID or title for study", None, []

    # analogously for the brain area...
    ba_name, ba_id, ba_ok = "", -1, False
    if isinstance(brain_area, int):
        ba_id = brain_area
        res = fetch_attribute_values(DBTable.BRAIN_AREA, "ba_name", dict(ba_id=ba_id))
        ba_ok = (len(res) == 1)
        ba_name = res[0]
    elif isinstance(brain_area, str):
        ba_name = brain_area
        rows = fetch_rows(DBTable.BRAIN_AREA, dict(ba_name=ba_name))
        ba_ok = (len(rows) == 1)
        ba_id = rows[0]['ba_id']
    if not ba_ok:
        return "Invalid ID or title for brain area", None, []

    # construct the session metadata argument. Don't know the number of trials yet, so we set that to 1.
    session_dict: Dict = dict(
        experimenter=experimenter,
        subj_id=subject,
        session_date=rec_date,
        session_sfx=suffix,
        rig_id=rig,
        study_id=study_id,
        study_title=study_title,
        session_notes=notes,
        num_trials=1,
        num_units=len(unit_types)
    )
    if len(unit_types) > 0:
        session_dict.update(dict(
            ephys_src=src,
            probe_type=probe,
            sampling_rate=float(rate),
            probe_x=float(x),
            probe_y=float(y),
            probe_depth=float(z),
            ba_id=ba_id,
            brain_area=ba_name
        ))
    try:
        info = SessionInfo(session_dict)
    except ValueError as e:
        return str(e), None, []

    msg = check_row(DBTable.SESSION, info.session_table_entry())
    if (msg is None) and info.number_of_units > 0:
        msg = check_row(DBTable.SESSION_EPHYS, info.ephys_table_entry(), omit_master=True)
    if msg:
        return msg, None, []
    nt_ids = []
    if info.number_of_units > 0:
        neuron_types = fetch_rows(DBTable.NEURON_TYPE)
        if neuron_types is None:
            return "Internal database error while retrieving neuron types", None, []
        nt_map = {d['nt_name']: d['nt_id'] for d in neuron_types}
        try:
            nt_ids = [nt_map[type_name] for type_name in unit_types]
        except KeyError:
            return "Invalid neuron type specified", None, []

    return "", info, nt_ids


def get_pending_commit_jobs_for(username: Optional[str] = None) -> Union[str, List[CommitJobStatus]]:
    """
    Retrieve status information for all session commit jobs pending on the portal server, or the subset of those that
    belonging to a specific portal user.

    Args:
        username: If None, method returns status information for all in-progress session commit jobs, across all portal
            users. Otherwise, it should be the username of a registered portal user, and the method only returns those
            pending jobs that were initiated by that user. In the latter case, if the specified user does not exist or
            lacks commit-level privileges, the method will return an empty list
    Returns:
        On failure, returns a brief error description. Otherwise, a list of job status objects, one for each pending
            commit job. Jobs are listed in descending order by start time, with the most recently initiated job first.
            If a username is specified, but that user does not exist or lacks commit-level privileges, then the pending
            jobs list will be empty.
    """
    try:
        conn = get_config().redis_conn
        raw_jobs: Dict = conn.hgetall(name=COMMITS)  # IMPORTANT: Returns Dict[job_id, CommitJobStatus as byte string]
        out: List[CommitJobStatus] = list()
        for _, r in raw_jobs.items():
            job_status: CommitJobStatus = CommitJobStatus.from_bytes(r)
            if (username is None) or (job_status.committer == username):
                out.append(job_status)
        out.sort(key=lambda j: j.started, reverse=True)
        return out
    except Exception as e:
        _logger.error(f"Failed to retrieve status for pending commit jobs: {str(e)}", exc_info=True)
        return "Unable to retrieve commit job status information on server!"


def commit_job_status(job_id: str) -> Union[str, CommitJobStatus]:
    """
    Retrieve current status information for a pending commit job.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns the job's latest status information. If job not found or a server error occurs, returns a brief error
            description.
    """
    try:
        conn = get_config().redis_conn
        job_status_bytes = conn.hget(name=COMMITS, key=job_id)
        if job_status_bytes is None:
            _logger.debug(f"Got request for status info on a commit job (id={job_id}) that does not exist.")
            return f"Commit job (id={job_id}) not found on server."
        job_status: CommitJobStatus = CommitJobStatus.from_bytes(job_status_bytes)
        return job_status
    except Exception as e:
        _logger.error(f"Error while retrieving commit job status info: {str(e)}", exc_info=True)
        return "An error occurred while retrieving commit job status on server"


def on_archive_uploaded_to_workspace(job_id: str) -> Union[str, CommitJobStatus]:
    """
    Update a session commit job after uploading the session ZIP archive file to the portal workspace in the server's
    local disk storage.

    When the session commit is initiated by the Flask/Dash-based frontend running on a browser client, that frontend
    is designed to upload the archive file in "chunks" to the folder in the portal's workspace dedicated to the commit
    job: $PORTAL_WS/staging/<job_id>. Ultimately, the archive must be "knitted back together" from the individual file
    chunks, then transferred to a staging area in the portal's backup repository in S3 (see file header for more info).
    Only then can the job enter the preprocessing phase.

    This method retrieves the specified job's status information, verifies that the job was originated by the portal
    application's Flask-based frontend (rather than through an API endpoint that supports session commits via the
    `sglportalapi` clientside package), and that the commit job staging folder exists. It then queues a background task
    to reform the archive file, transfer it to the staging area in S3, and then queue another task to preprocess the
    archive's contents.

    This method should *NOT* be called for a commit job managed by the sglportalapi package. In that scenario, the
    archive ZIP is uploaded directly (via a multipart upload) to the S3-based repository, which saves time compared to
    commits initiated on the portal frontend.

    Args:
        job_id: The commit job ID.
    Returns:
        On success, the job's updated status information. The job will remain in the "uploading" state, in which it
            remains until the archive ZIP has been transferred to the staging area in S3. If the job was not found or a
            server error occurs, returns a brief error description.
    """
    try:
        # get current job status and do checks...
        conn = get_config().redis_conn
        job = conn.hget(name=COMMITS, key=job_id)
        if job is None:
            _logger.debug(f"Called on a commit job [{job_id}] that does not exist")
            return "Commit job not found on server"
        job_status: CommitJobStatus = CommitJobStatus.from_bytes(job)
        if job_status.state != CommitStateEnum.UPLOADING:
            _logger.debug(f"Called on a commit job [{job_id}] NOT in upload phase")
            return "Commit job was not in upload phase"
        if job_status.api_triggered:
            raise Exception(f"Not for use with commit jobs initiated via API")
        job_folder = _get_job_subfolder(job_id)
        if not job_folder.is_dir():
            _logger.debug(f"Staging folder for commit job [{job_id}] not found in portal workspace")
            return "Staging folder for commit job not found"

        job_status.on_update("Archive uploaded to portal server. Moving archive to staging area in S3 repository...")
        conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        job_queue.enqueue(transfer_archive_to_repo, job_id, job_id=f"{job_id}-2repo", job_timeout='60m')

        return job_status
    except Exception as e:
        _logger.error(f"Error while checking or updating commit job status info: {str(e)}", exc_info=True)
        return "An error occurred while checking or updating commit job status on server"


def transfer_archive_to_repo(job_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the portal server, transfers the session
    archive for a commit job to the staging area in the portal's S3 repository.

    When a commit job is initiated on the Dash/Flask frontend, a Dash-based component uploads the session archive in
    "chunks" to the job's subfolder in the portal's workspace in local cluster storage. Upon completion, the frontend
    informs the server, and `on_archive_uploaded_to_workspace()` updates the commit job and queues a background task to
    run this method, which reassembles the chunks into the original file, uploads that file to the S3 repository
    at /staging/<job_id>/archive.zip, and cleans out uploaded chunks from the job's workspace folder. Finally, it calls
    `on_archive_uploaded_to_repo()`, which transitions the job to the "Preprocessing" phase and queues a new background
    task to begin processing the archive.

    If the operation is cancelled or fails at any point, the job is moved to the "Failed" state before returning.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        True if successful; False otherwise.
    """
    # callback during upload to repo which updates job progress. Stop reporting progress if user cancels. We cannot
    # stop the upload, but there's no point in posting further progress messages!
    t_last_update: float = -1

    def _upload_progress(pct: float) -> None:
        nonlocal t_last_update

        try:
            t = time.time()
            if (t_last_update < 0) or (t-t_last_update > 10):
                t_last_update = t
                _background_job_update(job_id, f"Uploading session archive to portal repository... {pct:.1f}%",
                                       dont_fail=True, overwrite=True)
        except Exception:
            pass

    try:
        if _background_job_update(job_id, "Starting archive transfer to S3", log=True):
            raise Exception("Operation cancelled")
        # validate job state and reassemble archive from chunks
        job_status = commit_job_status(job_id)
        if isinstance(job_status, str):
            _logger.error(f"Aborting transfer on error: {job_status}")
            return False
        elif job_status.state != CommitStateEnum.UPLOADING:
            _logger.error(f"Aborting transfer: Commit job in unexpected state [{job_status.state}]")
            return False
        elif not _reassemble_archive_from_chunked_upload(job_id):
            return False
        else:
            zip_path = Path(_get_job_subfolder(job_id), ARCHIVE_FNAME)
        if not zip_path.is_file():
            msg_pfx = f"Cannot find archive file for commit job {job_id}: "
            _logger.debug(f"{msg_pfx}: {str(zip_path)}")
            _background_job_update(job_id, f"{msg_pfx}: {zip_path.name}", CommitStateEnum.FAIL)
            return False

        # move archive to S3 -- can take a while -- so check for user cancel
        key = f"/staging/{job_id}/{ARCHIVE_FNAME}"
        if not repo.upload_file(zip_path, key, log_func=_upload_progress):
            raise Exception(f"Failed to transfer archive to {key} in S3")
        if _background_job_update(job_id, "Archive transfer complete.", log=True):
            raise Exception("Operation cancelled")
        zip_path.unlink(missing_ok=True)

        # signal that archive is now in S3, transition to preprocessing phase.
        res = on_archive_uploaded_to_repo(job_id)
        if isinstance(res, str):
            raise Exception(res)
    except Exception as err:
        error_msg = f"Error during archive transfer to S3: {str(err)}"
        _logger.error(error_msg, exc_info=True)
        _background_job_update(job_id, error_msg, CommitStateEnum.FAIL)
        return False

    return True


def staged_archive_key_in_repo(job_id: str) -> str:
    """
    The bucket key to which a session archive is uploaded in the portal's S3-based repository for the commit job
    specified.
    """
    return f"/staging/{job_id}/{ARCHIVE_FNAME}"


def on_archive_mupload_initialized(job_id: str, mupload_id: str) -> Optional[str]:
    """
    When a session commit is triggered through the dedicated API endpoint, the endpoint code initializes a multipart
    upload task so that the clientside code can upload the session archive directly to the staging area in the portal's
    S3-based repository. This method updates the commit job's status with the upload task ID, which will be needed
    later on to complete the multipart upload, or abort it if the upload failed.

    Args:
        job_id: The commit job ID.
        mupload_id: The multipart upload task ID.
    Returns:
        None if successful, else an error message.
    """
    try:
        # get current job status and do checks...
        conn = get_config().redis_conn
        job = conn.hget(name=COMMITS, key=job_id)
        if job is None:
            _logger.debug(f"Called on a commit job [{job_id}] that does not exist")
            return "Commit job not found on server"
        job_status: CommitJobStatus = CommitJobStatus.from_bytes(job)
        if job_status.state != CommitStateEnum.UPLOADING:
            _logger.debug(f"Called on a commit job [{job_id}] NOT in upload phase")
            return "Commit job was not in upload phase"
        if not job_status.api_triggered:
            return "Commit job was not API triggered; multipart upload does not apply."

        job_status.mupload_id = mupload_id
        conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        return None
    except Exception as e:
        _logger.error(str(e), exc_info=True)
        return "A server error occurred while storing multipart upload ID for API-triggered commit job."


def abort_archive_mupload(job_id: str) -> Optional[str]:
    """
    Abort the S3 multipart upload task for an API-triggered session commit job. The session commit job is moved to the
    'failed' state.

    Args:
        job_id: The commit job ID.
    Returns:
        None if successful, else an error message.
    """
    try:
        # get current job status and do checks...
        conn = get_config().redis_conn
        job = conn.hget(name=COMMITS, key=job_id)
        if job is None:
            _logger.debug(f"Called on a commit job [{job_id}] that does not exist")
            return "Commit job not found on server"
        job_status: CommitJobStatus = CommitJobStatus.from_bytes(job)
        if job_status.state != CommitStateEnum.UPLOADING:
            _logger.debug(f"Called on a commit job [{job_id}] NOT in upload phase")
            return "Commit job was not in upload phase"
        if not job_status.api_triggered:
            return "Commit job was not API triggered; multipart upload does not apply."

        abort_ok = abort_multipart_upload(staged_archive_key_in_repo(job_id), job_status.mupload_id)
        msg = f"{'Aborted ' if abort_ok else 'Failed to abort'} archive upload to S3 repo"
        job_status.on_update(msg=msg, state=CommitStateEnum.FAIL)
        conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        return None if abort_ok else f"{msg}. Contact portal admin."
    except Exception as e:
        _logger.error(str(e), exc_info=True)
        return "A server error occurred while aborting multipart upload task for API-triggered commit job."


def complete_archive_mupload(job_id: str, parts: List[Dict]) -> Optional[str]:
    """
    Complete the S3 multipart upload task for an API-triggered session commit job, then transition the job to the
    preprocessing phase and queue a background task to handle that work.

    Args:
        job_id: The commit job ID.
        parts: List of completed upload parts. Each entry is a dictionary {'ETag': str, 'PartNumber': int}
            holding the upload part's entity tag (returned in response header when part is uploaded) and part number.
    Returns:
        None if successful, else an error message.
    """
    try:
        # get current job status and do checks...
        conn = get_config().redis_conn
        job = conn.hget(name=COMMITS, key=job_id)
        if job is None:
            _logger.debug(f"Called on a commit job [{job_id}] that does not exist")
            return "Commit job not found on server"
        job_status: CommitJobStatus = CommitJobStatus.from_bytes(job)
        if job_status.state != CommitStateEnum.UPLOADING:
            _logger.debug(f"Called on a commit job [{job_id}] NOT in upload phase")
            return "Commit job was not in upload phase"
        if not job_status.api_triggered:
            return "Commit job was not API triggered; multipart upload does not apply."

        ok = finish_multipart_upload(staged_archive_key_in_repo(job_id), job_status.mupload_id, parts)
        msg = f"{'Completed ' if ok else 'Failed to complete'} archive upload to S3 repo"
        job_status.on_update(msg=msg, state=CommitStateEnum.FAIL if (not ok) else None)
        conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        if not ok:
            return msg

        archive_key = staged_archive_key_in_repo(job_id)
        sz = repo.file_size(archive_key)
        if sz == 0:
            _logger.debug(f"Uploaded archive not found in repo at: {archive_key}")
            return "Uploaded archive not found in portal repository"

        job_status.on_update(f"Archive uploaded to portal repository in S3. Queued for preprocessing...",
                             state=CommitStateEnum.PREPROCESS)
        conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        job_queue.enqueue(preprocess_commit_job, job_id, job_id=f"{job_id}-preproc", job_timeout='60m')

        return None
    except Exception as e:
        _logger.error(str(e), exc_info=True)
        return "A server error occurred while completing multipart upload task for API-triggered commit job."


def on_archive_uploaded_to_repo(job_id: str) -> Union[str, CommitJobStatus]:
    """
    Update a session commit job after uploading the session ZIP archive file to the portal's S3-based repository.

    To support processing an indeterminate number of ongoing session commit jobs, each session archive must be
    uploaded to the S3-based repository, as the portal server's local storage is much more expensive and relatively
    limited in capacity. When a commit job is originated through the Flask/Dash frontend, the archive is first
    uploaded in chunks to server local storage, then a background task reassembles the chunks and transfers the
    archive to S3. When originated through a portal API endpoint via the sglportalapi clientside package, the archive
    is uploaded directly to the portal's S3-based repository (multipart upload using presigned URLs). Regardless, once
    the archive is in the repo, this method is called to transition the commit job to the preprocessing phase, queueing
    a background task to perform the work.

    Args:
        job_id: The commit job ID.
    Returns:
        On success, the job's updated status information. The job will be in the "preprocessing" stage. If the job was
            not found or a server error occurs, returns a brief error description.
    """
    try:
        # get current job status and do checks...
        conn = get_config().redis_conn
        job = conn.hget(name=COMMITS, key=job_id)
        if job is None:
            _logger.debug(f"Called on a commit job [{job_id}] that does not exist")
            return "Commit job not found on server"
        job_status: CommitJobStatus = CommitJobStatus.from_bytes(job)
        if job_status.state != CommitStateEnum.UPLOADING:
            _logger.debug(f"Called on a commit job [{job_id}] NOT in upload phase")
            return "Commit job was not in upload phase"
        job_folder = _get_job_subfolder(job_id)
        if not job_folder.is_dir():
            _logger.debug(f"Staging folder for commit job [{job_id}] not found in portal workspace")
            return "Staging folder for commit job not found"

        archive_key = staged_archive_key_in_repo(job_id)
        sz = repo.file_size(archive_key)
        if sz == 0:
            _logger.debug(f"Uploaded archive not found in repo at: {archive_key}")
            return "Uploaded archive not found in portal repository"

        job_status.on_update(f"Archive uploaded to portal repository in S3. Queued for preprocessing...",
                             state=CommitStateEnum.PREPROCESS)
        conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        job_queue.enqueue(preprocess_commit_job, job_id, job_id=f"{job_id}-preproc", job_timeout='60m')

        return job_status
    except Exception as e:
        _logger.error(f"Error while checking or updating commit job status info: {str(e)}", exc_info=True)
        return "An error occurred while checking or updating commit job status on server"


def cancel_or_remove_commit_job(job_id: str) -> Tuple[bool, str, Optional[CommitJobStatus]]:
    """
    Cancel or remove the pending commit job. If a background process is currently working on the commit, it may take
    some time before the operation completes. In this case, the commit job will remain on the server in the "Cancelled"
    state. When the background process detects the user cancellation, it will move the job to the "Failed" state and
    stop.

    If no background process is working on the commit job, the job is removed immediately. If a commit job finished
    successfully -- the "Done" state, meaning that the session data has been committed to the archive, this method
    merely removes the completed job from the set of pending commit jobs cached in Redis.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns a 3-tuple (removed, err_msg, job_status), where removed==True if commit job was removed (or not found);
            err_msg is a non-empty string only if a server error occurred, and job_status is the job's updated status
            if it was cancelled but removal is pending.
    """
    try:
        job_status = commit_job_status(job_id)
        if isinstance(job_status, str):
            return True, job_status, None

        conn = get_config().redis_conn

        # remove job now if background process is not working on it. Else, if not already cancelled, move job to that
        # state and append a progress message in Redis
        if job_status.state.can_delete_job_in_this_state():
            # when an API-triggered commit is cancelled in the upload phase, be sure to abort the S3 multipart upload
            archive_on_repo = staged_archive_key_in_repo(job_id)
            if job_status.state == CommitStateEnum.UPLOADING and job_status.api_triggered:
                repo.abort_multipart_upload(archive_on_repo, job_status.mupload_id)

            _remove_job_subfolder(job_id)
            if repo.file_size(archive_on_repo) > 0:
                repo.delete_file(archive_on_repo)

            with conn.pipeline(True) as pipe:
                pipe.hdel(COMMITS, job_id)
                pipe.delete(f"{PROTODEFS_NS}{job_id}")
                pipe.execute()

            return True, "", None
        elif job_status.state != CommitStateEnum.CANCEL:
            job_status.on_update(msg="User cancelled job", state=CommitStateEnum.CANCEL)
            conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

        return False, "", job_status
    except Exception as e:
        _logger.error(f"Failed to cancel or remove commit job {job_id}: {str(e)}", exc_info=True)
        return False, "An error occurred while trying to cancel/remove commit job on server", None


def preprocess_commit_job(job_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the backend server, preprocesses the
    experiment session data archive for a pending commit job.

    After a commit job is initiated, the client is largely responsible for uploading the session archive to the portal's
    backup repository in S3, at the key `/staging/<job_id>/archive.zip`. [The upload process is different depending on
    whether the commit is initiated through the Dash frontend or via a dedicated API endpoint using the `sglportalapi`
    Python package. See file header for details.] Once uploaded, the server queues a task to preprocess the archive.

    Since the archive is cached on S3, the method must first download it to the staging folder in the server's local
    workspace. It then scans the archive contents and extracts information that will be needed when the session is
    actually committed to the lab database: (1) the unique trial protocols presented during the session; (2) timing
    information for all trial reps, in particular, the start and stop timestamps for the trial in the Omniplex timeline
    (for electrophysiological experiments using the Omniplex system); and (3) metrics for all neural units recorded in
    the session. These preprocessing results are added to a file in the staging folder, `commit_info.bin`, that already
    contains user-supplied metadata for the session.

    Preprocessing a large (>1GB) session can take many minutes, so the method periodically posts progress messages for
    the job and checks whether or not the user has requested the job be cancelled.

    If the operation is cancelled or fails at any point, the job is moved to the "Failed" state before returning. On
    successful completion:
        - If any trial protocol definitions require user validation, the job is moved to the interactive "Review" stage.
          Since the user may not check the status of commit for an indefinite period of time, the large archive file
          is deleted from the staging folder in the portal workspace (it's still backed up in the repo) before the
          method returns.
        - Otherwise, no review is necessary and the background task starts work on committing the session to the
          database (the "Commit" stage).

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        True if successful; False otherwise.
    """
    commit_info_path = Path(_get_job_subfolder(job_id), COMMIT_INFO_FNAME)
    """ Location of the commit information file in job's staging folder within local portal workspace. """
    zip_path: Path = Path(_get_job_subfolder(job_id), ARCHIVE_FNAME)
    """ Location of session archive in staging folder within local portal workspace. """
    trial_info: Dict[str, _TrialInfo] = dict()
    """ 
    Dictionary maps the filename for each Maestro data file in the session archive to timing and trial protocol info for
    the particular trial instance recorded in that file. In particular, this includes the Omniplex start and stop
    timestamps required to align neural responses recorded on the Omniplex system with the behavioral responses recorded
    by Maestro.
    """
    units: List[OmniplexUnit] = list()
    """ 
    The list of neural units culled from the session data archive during preprocessing. Includes information required
    to prepare an entry in the Session.Neuron part table for each neural unit.
    """
    protocols: List[maestro.Protocol]
    """ The list of trial protocol culled from the session data archive during pre-processing. """
    session_info: SessionInfo
    """
    Metadata about session that was supplied by the user when the commit job was initiated and stored in a dedicated
    file in the job's staging folder. After preprocessing, we must update it to reflect the number of trials recorded
    during the experiment session.
    """

    # callback during download from repo which posts progress updates for job. Stop reporting progress if user cancels.
    # We cannot stop the download, but there's no point in posting further progress messages!
    t_last_update: float = -1

    def _download_progress(pct: float) -> None:
        nonlocal t_last_update

        try:
            t = time.time()
            if t_last_update < 0 or (t - t_last_update > 10):
                t_last_update = t
                _background_job_update(job_id, f"Downloading session archive to local staging folder... {pct:.1f}%",
                                       dont_fail=True, overwrite=True)
        except Exception:
            pass

    archive_on_repo = staged_archive_key_in_repo(job_id)
    fail_msg, job_found, perform_cleanup = "", False, False
    try:
        if _background_job_update(job_id, f"Starting preprocessing phase...", log=True):
            raise Exception("Operation cancelled")

        # verify job status and local staging folder
        job_status = commit_job_status(job_id)
        job_found = not isinstance(job_status, str)
        if not job_found:
            _logger.error(f"Failed to retrieve job status from Redis for {job_id}")
            perform_cleanup = True
            return False
        elif job_status.state != CommitStateEnum.PREPROCESS:
            _logger.error(f"Commit job is not in the correct stage for background preprocessing: {job_status.state}")
            try:
                _background_job_update(job_id, "Preprocessing task aborted; out of sync?")
            except Exception:
                pass
            return False
        elif not commit_info_path.is_file():
            _logger.error(f"Commit job information file for job {job_id} not found at: {str(commit_info_path)}")
            raise Exception("Missing commit information file in workspace staging area")

        # download archive to staging folder
        if repo.file_size(archive_on_repo) == 0:
            _logger.error(f"Archive for commit job {job_id} not found on S3 repo")
            raise Exception("Missing ession archive in staging area on ortal repo")
        if _background_job_update(job_id, f"Downloading session archive from portal repo"):
            raise Exception("Operation cancelled")
        if not repo.download_file(archive_on_repo, zip_path, log_func=_download_progress):
            _logger.error(f"Failed to download session archive from S3 repo for commit job {job_id}")
            raise Exception("Failed to download session archive from portal repo")

        # preprocess the archive
        if _background_job_update(job_id, f"Preprocessing session archive...", log=True):
            raise Exception("Operation cancelled")

        # if archive contains unit data without a PL2 file, then the pickle file with unit spike trains must also
        # include the unit's SNR and template, and a CSV file must be included that specifies the elapsed time -- in
        # the Omniplex timeline! -- at which each trial started.
        is_alt_archive = False

        with zipfile.ZipFile(zip_path, 'r') as archive:
            data_file_name_pattern = re.compile("[.]\\d\\d\\d\\d$")
            archive_list = archive.infolist()
            pl2s_archived: List[zipfile.ZipInfo] = list()
            units_zip_info: Optional[zipfile.ZipInfo] = None   # the pickle file, if present
            csv_ts_info: Optional[zipfile.ZipInfo] = None   # contains trial timestamps when PL2 not present
            session_date: Optional[date] = None
            for info in archive_list:
                if (len(info.filename) > 3) and (info.filename[-3:].lower() == 'pl2'):
                    pl2s_archived.append(info)
                elif data_file_name_pattern.search(info.filename) is not None:
                    header = maestro.DataFileHeader(archive.read(info))
                    file_index = int(info.filename[-4:])
                    header_timestamp = header.timestamp_ms if header.version >= 21 else None
                    if session_date is None:
                        session_date = header.date_recorded
                    elif session_date != header.date_recorded:
                        raise Exception("Recorded date must be the same for all trial files in archive!")
                    duration = float(header.num_scans_saved - 1) / 1000.0  # Trial mode scan rate is fixed at 1KHz
                    trial_info[info.filename] = _TrialInfo(file_index, duration, header_timestamp)
                elif ((len(info.filename) > 7) and (info.filename[-7:].lower() == '.pickle')) or \
                        ((len(info.filename) > 4) and (info.filename[-4:].lower() == '.pkl')):
                    if units_zip_info is None:
                        units_zip_info = info
                    else:
                        raise Exception("Found more than one neural units file in session data archive!")
                elif (len(info.filename) > 4) and (info.filename[-4:].lower() == '.csv'):
                    if csv_ts_info is None:
                        csv_ts_info = info
                    else:
                        raise Exception("Found more than one trial timestamps CSV file in session data archive!")

            if (units_zip_info is not None) and (len(pl2s_archived) == 0):
                if csv_ts_info is not None:
                    is_alt_archive = True
                else:
                    raise Exception("Missing Omniplex file(s) for spike-sorted unit data!")

            if _background_job_update(job_id, f"Found {len(trial_info)} trial files. Processing archive for trial "
                                              f"protocols.", log=True):
                raise Exception("Operation cancelled")
            existing_protos = set([str(h) for h in fetch_attribute_values(DBTable.TRIAL_PROTOCOL, 'proto_hash')])
            protocols, file_to_proto = \
                maestro.Protocol.extract_protocols_from_session_data(archive, existing_protos)
            if len(protocols) == 0:
                raise Exception("No trial protocols found in session archive!")
            for filename, proto_index in file_to_proto.items():
                trial_info[filename].proto_index = proto_index

            # load and validate the neural unit data from pickle file, if there is one.
            unit_data: Optional[Dict[str, List[Any]]] = None
            if units_zip_info is not None:
                if _background_job_update(job_id, f"Loading neural units file {units_zip_info.filename}...", log=True):
                    raise Exception("Operation cancelled")
                unit_data = pickle.loads(archive.read(units_zip_info))
                pl2_filenames = [x.filename for x in pl2s_archived]  # will be empty if archive lacks PL2 file
                if not _validate_neural_unit_data(unit_data, pl2_filenames):
                    raise Exception(f"Invalid format for neural units file: {units_zip_info.filename}")
                # if 'filename' field missing, assume all units recorded in same Omniplex file. If no PL2 file found
                # in archive (alternate scheme), then set 'filename' field to 'unavailable'
                if 'filename' not in unit_data:
                    fname = 'unavailable' if len(pl2_filenames) == 0 else pl2_filenames[0]
                    unit_data['filename'] = [fname] * len(unit_data['channel'])

            # we support an alternative to supplying the Omniplex PL2 file containing the electrode recordings: a CSV
            # file supplies the start timestamp in ms for each Maestro trial in the same timeline as the spike times
            # found in the pickle file. In addition, the pickle file itself must have fields defining the SNR and
            # templates waveform for each neural unit (this is verified above).
            if units_zip_info is not None:
                if is_alt_archive:
                    if _background_job_update(job_id, "No PL2 found; processing CSV and pickle file for unit metrics.", log=True):
                        raise Exception("Operation cancelled")
                    for i, ch_id in enumerate(unit_data['channel']):
                        spiketimes = unit_data['spiketimes'][i]
                        n_spikes = len(spiketimes)
                        firing_rate = 0 if n_spikes < 2 else n_spikes / (spiketimes[-1] - spiketimes[0])
                        units.append(OmniplexUnit(
                            src=unit_data['filename'][i], ch=ch_id, spikes=unit_data['spiketimes'][i],
                            num_spikes=n_spikes, rate=firing_rate, snr=unit_data['snr'][i],
                            template=unit_data['template'][i]))
                    _get_trial_timing_from_csv_file(archive, csv_ts_info, trial_info)
                else:
                    for pl2_zip_info in pl2s_archived:
                        save_path = _chunked_extract_from_archive(job_id, archive, pl2_zip_info, zip_path.parent)
                        if save_path is None:
                            raise Exception("Operation cancelled")
                        if _process_omniplex_file(job_id, save_path, unit_data, trial_info, units):
                            raise Exception("Operation cancelled")
                        # discard Omniplex file (which is huge) once we're done processing it. If session requires manual
                        # review, we don't want to leave this in the local staging folder!
                        save_path.unlink(missing_ok=True)

                    # if there is unit data, we require metrics for each unit specified in the neural units data file,
                    # and there must be Omniplex timestamps for all trials
                    if len(units) < len(unit_data['channel']):
                        raise Exception(
                            f"Missing analog data for at least one unit defined in {units_zip_info.filename}")
                    for key in trial_info.keys():
                        if trial_info[key].omniplex_start is None:
                            raise Exception(f"Missing Omniplex start/stop timestamps for {key}")

            # add results from preprocessing to the commit information file in the local staging folder
            if _background_job_update(job_id, "Saving results from preprocessing...", log=True):
                raise Exception("Operation cancelled")
            session_info, nt_ids, _, _, _ = _read_commit_info_file(commit_info_path)
            session_info.number_of_trials = len(trial_info)
            if len(units) > 0:
                if len(units) != len(nt_ids):
                    raise Exception("Length of neuron types list does not match number of neural units found!")
                for i, u in enumerate(units):
                    u.neuron_type = nt_ids[i]
            _write_commit_info_file(commit_info_path, session_info, [], trial_info, protocols, units)

            # if any trial protocol needs validation, then transition to review stage, caching protocol definitions in
            # Redis for efficient access. The archive is deleted locally, since we won't need it for an indefinite
            # period of time. Otherwise, proceed immediately (on the same background task) to the final commit.
            requires_review = any([p.is_candidate for p in protocols])
            if requires_review:
                zip_path.unlink(missing_ok=True)
                proto_defs = [p.to_bytes() for p in protocols]
                get_config().redis_conn.rpush(f"{PROTODEFS_NS}{job_id}", *proto_defs)
                if _background_job_update(job_id, "Preprocessing complete. User review required.",
                                          CommitStateEnum.REVIEW, log=True):
                    raise Exception("Operation cancelled")
                return True
            else:
                if _background_job_update(job_id, "Preprocessing complete. Committing session to database...",
                                          CommitStateEnum.COMMIT, log=True):
                    raise Exception("Operation cancelled")
                return finish_commit_job(job_id)

    except Exception as err:
        fail_msg = f"Error during preprocessing: {str(err)}"
        perform_cleanup = True
        _logger.error(fail_msg, exc_info=True)
        return False
    finally:
        if perform_cleanup:
            _remove_job_subfolder(job_id)
            repo.delete_file(archive_on_repo)
        if (len(fail_msg) > 0) and job_found:
            try:
                _background_job_update(job_id, fail_msg, CommitStateEnum.FAIL)
            except Exception:
                pass


def _reassemble_archive_from_chunked_upload(job_id: str) -> bool:
    """
    Helper method for background task function `transfer_archive_to_repo()`. It reconstructs the session archive from
    the individual file chunks that are uploaded to the commit job's staging folder in the server's workspace.

    Session archives will typically be several GB in size, and the current upload mechanism via the Dash frontend uses
    chunking to keep the client responsive. If the upload was successful, the commit job's staging folder will contain a
    single subfolder holding all of the file chunks, with file names "zipfilename_part_NNN", where NNN is the chunk
    number.

    This method verifies the existence of the upload folder under the job's staging folder, knits together the chunks in
    order into the original archive, which is stored directly under the staging folder. The subfolder with the chunks is
    deleted.

    It can take a while to rebuild a multi-GB file, so the method will post progress messages and check for user
    cancel.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        True if successful, false otherwise.
    Raises:
        Exception: If a chunk file is missing, an IO or other error occurs.
    """
    # expect to find a SINGLE folder under the staging folder that contains the file chunks
    commit_job_dir = _get_job_subfolder(job_id)
    temp_dir: Optional[Path] = None
    for child in commit_job_dir.iterdir():
        if child.is_dir():
            temp_dir = child
            break
    num_chunks = 0 if (temp_dir is None) else len([child for child in temp_dir.iterdir() if child.is_file()])
    if num_chunks == 0:
        error_msg = "Failed to reassemble archive from chunked upload - file chunks not found"
        _background_job_update(job_id, error_msg, CommitStateEnum.FAIL)
        return False

    # chunk filenames all have the format "<base>_part_NNN", where N is the chunk number. Find the value of <base>.
    base_name: str = ""
    for child in temp_dir.iterdir():
        if child.is_file():
            base_name = child.name[0:-len("_part_NNN")]
            break
    if len(base_name) == 0:
        _background_job_update(job_id, "Failed to reassemble archive from chunked upload - bad chunk file name",
                               CommitStateEnum.FAIL)
        return False

    # reassemble chunks into ZIP file -- with progress updates every 10 seconds
    t0 = time.time()
    zip_path = Path(commit_job_dir, ARCHIVE_FNAME)
    with open(zip_path, "ab") as target_file:
        for i in range(1, num_chunks + 1):
            chunk_path = Path(temp_dir, get_chunk_name(base_name, i))
            with open(chunk_path, "rb") as stored_chunk_file:
                target_file.write(stored_chunk_file.read())
            if (time.time() - t0) > 10:
                msg = f"Reassembling session archive from chunked upload: {i} of {num_chunks} chunks processed."
                if _background_job_update(job_id, msg, overwrite=True, log=((i == 1) or (i == num_chunks - 1))):
                    return False
                t0 = time.time()
    shutil.rmtree(temp_dir)
    return True


def _background_job_update(job_id: str, msg: str, next_state: Optional[CommitStateEnum] = None,
                           dont_fail: bool = False, overwrite: bool = False, log: bool = False) -> bool:
    """
    Helper method used to update progress and, optionally, the state of a commit job. Intended for use ONLY within the
    background workers that handle the uploading, preprocessing and final commit phases of a job, this method will
    detect if the job has been cancelled and, if so, move the job to the "Failed" state. In this scenario, the specified
    progress message is not posted. However, if the job has just finished and is being moved to the "Done" state, the
    method does NOT check if the job was cancelled.

    No action is taken if the specified commit job has failed or already finished, or is in the interactive review
    phase. A background task should not be actively working on the commit job in these states.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        msg: The new progress message to post.

        next_state: If not None, transition the job to this state. Default is None.
        dont_fail: If True and job has been cancelled, do not transition to the failed state and do not post the new
            progress message. This flag should be set if the background task is unable to stop work immediately.
        overwrite: If True, overwrite the most recent progress message with the new one. This is useful when posting
            updates about a long running task indicating percent complete.
        log: If True, the progress message is also written to the application logger at INFO level. Default = False.
    Returns:
        True if job was in the "Cancelled" state and therefore moved to the "Failed" state; False otherwise.
    Raises:
        Exception: If an error occurs while reading or writing job status/progress history in Redis.
    """
    was_cancelled = False
    job_status = commit_job_status(job_id)
    if isinstance(job_status, str):
        raise Exception(str)
    # a background task should not run in any of these states; do nothing
    if job_status.state in [CommitStateEnum.REVIEW, CommitStateEnum.DONE, CommitStateEnum.FAIL]:
        return False
    if job_status.state == CommitStateEnum.CANCEL:
        if dont_fail:
            return False
        next_state = CommitStateEnum.FAIL
        was_cancelled = True

    if (job_status.state == CommitStateEnum.CANCEL) and (next_state != CommitStateEnum.DONE) and not dont_fail:
        next_state = CommitStateEnum.FAIL
        was_cancelled = True

    if log and isinstance(msg, str):
        _logger.info(f"Commit {job_id}: {msg}")

    job_status.on_update(msg="Background task cancelled!" if was_cancelled else msg, overwrite=overwrite,
                         state=next_state)
    get_config().redis_conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())

    return was_cancelled


def _validate_neural_unit_data(unit_data: Dict[str, List[Any]], pl2_filenames: List[str]) -> bool:
    """
    Helper method validates the object loaded from a single dedicated pickle file in the session ZIP archive that lists
    all identified neurons and their spike times, and possibly some other information

    When researchers prepare the ZIP archive containing all data files for an experiment session including neural
    unit recordings, they must provide a single Python pickle file with the results of their spike-sorting analysis of
    all units recorded during the session. This file contains a dictionary with 2-3 fields: 'channel', 'spiketimes', and
    (optionally) 'filename'. The last field is required ONLY if there is more than one Omniplex PL2 file in the archive.
    Each field is a list of length N, where N is the number of neural units. The 'channel' key holds the Omniplex
    channel ID for the analog channel on which the unit was recorded, the 'filename' key holds the name of the Omniplex
    PL2 file within the ZIP archive, and the 'spiketimes' key holds the spike times (in seconds since the Omniplex
    recording started) for each unit, as a Numpy array.

    In order to commit an archive when the original PL2 source file is no longer available, an alternative archive
    format is supported in which the elapsed start times of each trial are listed in a CSV file in the archive, and the
    dictionary within the neural units pickle file must contain additional fields 'snr' (signal-to-noise ratio for each
    unit) and 'template' (spike template waveform for each unit, as a Numpy array). Note that, in this scenario, the
    template length and the method for computing SNR may be different than what is done when the PL2 file is available.

    Args:
        unit_data: The dictionary loaded from the neural units file.
        pl2_filenames: List of all Omniplex PL2 files found in the session archive. If the archive lacks any PL2 files,
            then the dictionary must contain the additional fields as described above.

    Returns:
        True if unit_data is validly formatted as described above, false otherwise.
    """
    required_keys = ['channel', 'spiketimes']
    if len(pl2_filenames) == 0:
        required_keys.extend(['snr', 'template'])

    ok = (isinstance(unit_data, dict) and
          all((k in unit_data) and isinstance(unit_data[k], list) for k in required_keys))
    num_units = len(unit_data['channel']) if ok else 0
    if ok:
        ok = all(len(unit_data[k]) == num_units for k in required_keys) and \
             all(isinstance(x, str) for x in unit_data['channel']) and \
             all(isinstance(x, np.ndarray) for x in unit_data['spiketimes']) and \
             (('snr' not in unit_data) or all(isinstance(x, float) for x in unit_data['snr'])) and \
             (('template' not in unit_data) or all(isinstance(x, np.ndarray) for x in unit_data['template']))
    if ok and (len(pl2_filenames) > 0):
        if 'filename' in unit_data:
            ok = isinstance(unit_data['filename'], list) and (len(unit_data['filename']) == num_units) \
                 and all(x in pl2_filenames for x in unit_data['filename'])
        else:
            ok = (len(pl2_filenames) == 1)

    return ok


def _chunked_extract_from_archive(job_id: str, archive: zipfile.ZipFile, pl2_info: zipfile.ZipInfo,
                                  dst: Path) -> Optional[Path]:
    """
    Helper method for preprocess_commit_job: It extracts a potentially very large Omniplex PL2 file from a session data
    archive. If the uncompressed size of the file is under 300MB, it is extracted in one go using ZipFile.extract().
    Otherwise, it is extracted in 200MB chunks so that progress can be reported during the extraction and so that the
    operation can be cancelled prior to completion.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        archive: The source ZIP archive.
        pl2_info: The file to be extracted.
        dst: The target directory to which the file is extracted.

    Returns:
        File system path for the extracted file, or None if the extraction was cancelled
    Raises:
        Exception: If a fatal error occurs while updating job status or processing the archive.
    """
    chunk_size = 200 * 1024 * 1024
    size_in_mb: float = pl2_info.file_size / (1024 * 1024)
    if pl2_info.file_size < 300:
        msg = f"Extracting Omniplex file {pl2_info.filename} (size={size_in_mb:.1f} MB)"
        if _background_job_update(job_id, msg, log=True):
            return None
        save_path = Path(archive.extract(pl2_info, str(dst)))
        return save_path

    # Large file extract in chunks
    t0 = time.time()
    msg = f"Extracting Omniplex file {pl2_info.filename}: 0 of {size_in_mb:.1f} MB ..."
    if _background_job_update(job_id, msg, log=True):
        return None
    save_path = Path(dst, pl2_info.filename)
    bytes_written: int = 0
    with archive.open(pl2_info, 'r') as source, open(save_path, 'wb') as target:
        while True:
            buffer = source.read(chunk_size)
            if len(buffer) == 0:
                return save_path
            target.write(buffer)
            bytes_written += len(buffer)
            written_mb: float = bytes_written / (1024 * 1024)
            if (time.time() - t0) > 5:
                msg = f"Extracting Omniplex file {pl2_info.filename}: {written_mb:.1f} of {size_in_mb:.1f} MB ..."
                if _background_job_update(job_id, msg, overwrite=True):
                    return None
                t0 = time.time()


def _process_omniplex_file(job_id: str, omniplex_file: Path, unit_data: Dict[str, List[Any]],
                           trial_info: Dict[str, _TrialInfo], units: List[OmniplexUnit]) -> bool:
    """
    Helper method for preprocess_commit_job(): It processes an Omniplex PL2 file for information needed when committing
    an electrophysiological recording session to the lab database.

    First, the method analyses the "Strobed" and "EVT02" event channels to find the Omniplex-recorded start and stop
    timestamps for each Maestro trial presented. These timestamps are essential in order to align neural unit
    spike times derived from the Omniplex recording with behavioral responses recorded in each individual Maestro
    trial data file.

    Second, the method calculates selected metrics for each identified neural unit (mean firing rate, signal-to-noise
    ratio, and the average spike template waveform) using the unit spike times (in "Omniplex time") and the original
    Omniplex analog data stream(s) from which those spike times were "sorted". These metrics are ultimately stored
    in the lab database. For details, see _prepare_neural_units().

    Omniplex PL2 files can be very large, so this method will regularly update the commit job with progress messages
    and check to see if the user has cancelled the job.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        omniplex_file: The path to the Omniplex PL2 file to be processed.
        unit_data: The identified neural unit data, including channel ID, PL2 source file, and the spike timestamps
            in seconds since the Omniplex recording started. For a full description of this dictionary, see
            _validate_neural_unit_data().
        trial_info: [in/out] A dictionary with partial information about each Maestro trial presented, keyed by trial
            data filename. The method adds the Omniplex-recorded start and stop times for each trial, as culled from
            the Omniplex file.
        units: [in/out] The list of neural units recorded during the experiment session. As the Omniplex file is
            processed, the metrics for each recorded unit are appended to this list.

    Returns:
        True if commit job was cancelled during Omniplex file processing; False otherwise.

    Raises:
        Exception: If an error occurs while loading and processing data in the Omniplex file, or while updating
            commit job status or progress messages.
    """
    with open(omniplex_file, 'rb') as fp:
        msg = f"Processing trial timing information in Omniplex file {omniplex_file.name}..."
        if _background_job_update(job_id, msg, log=True):
            return True
        info = PL2.load_file_information(fp)
        timings_dict = _get_trial_timing_from_pl2_file(fp, info)
        for key in (timings_dict.keys() & trial_info.keys()):
            t_info = trial_info[key]
            t_info.omniplex_start, t_info.omniplex_stop = timings_dict[key]

        # which units are recorded in this PL2 file
        units_in_file = [i for i, filename in enumerate(unit_data['filename']) if filename == omniplex_file.name]

        # multiple units may be recorded on the same analog channel, but we only want to load and process a given
        # analog channel once because the recordings can be very long!
        channel_ids = {unit_data['channel'][unit_idx] for unit_idx in units_in_file}
        for channel_id in sorted(channel_ids):
            spikes = [unit_data['spiketimes'][unit_idx] for unit_idx in units_in_file
                      if unit_data['channel'][unit_idx] == channel_id]
            units_found = _prepare_neural_units(job_id, channel_id, spikes, omniplex_file.name, fp, info)
            if units_found is None:
                return True   # job cancelled
            units.extend(units_found)

def _get_trial_timing_from_csv_file(
        archive: zipfile.ZipFile, csv_info: zipfile.ZipInfo, trial_info: Dict[str, _TrialInfo]) -> None:
    """
    In the event that the original Omniplex PL2 recording is no longer available, an alternative archive content format
    is supported: (1) Neural unit SNR and template waveforms are included via additional fields in the neural unit data
    pickle file. (2) Start times for all trials relative to that recording timeline (so we can determine which spikes
    occurred during each trial!) are supplied in a CSV file.

    This method parses the CSV file. Each text line in the file is parsed as "filename,ts", where "filename" is the
    Maestro trial data filename and "ts" is the start time for that trial in milliseconds.

    Args:
        archive: The source ZIP archive.
        csv_info: The CSV file within the archive.
        trial_info: [in/out] A dictionary with partial information about each Maestro trial presented, keyed by trial
            data filename. The method adds the start timestamp for each trial, as culled from the CSV file specified.

    Raises:
        Exception: If an error occurs while parsing CSV file or if a timestamp is missing for any trial data file.
    """
    with archive.open(csv_info, 'r') as csv_file:
        rdr = csv.reader(TextIOWrapper(csv_file, 'utf-8'))
        for line in rdr:
            if len(line) < 2:
                continue
            fname = line[0]
            try:
                ts_msec = float(line[1])
            except ValueError:
                continue
            if fname in trial_info:
                trial_info[fname].omniplex_start = ts_msec / 1000.0

    # now verify we got a timestamp for every trial!
    for k in trial_info:
        if trial_info[k].omniplex_start is None:
            raise Exception(f"No timestamp found for {k} in CSV file {csv_info.filename}")

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

    **NOTE:** During testing, we discovered a number of sessions where the Omniplex system was stopped in the middle of
    a running trial. As a result, the last "start" code recorded by the Omniplex is not matched with a "stop" code.
    Instead of throwing an exception in this case, we now simply skip that "start" code -- just as we skip any
    "start-stop" event sequence that doesn't include the "file saved" code, corresponding to the many aborted trials
    that happen in a typical Maestro recording session.

    Args:
        fp: The PL2 file object. It must be open and is NOT closed upon return.
        info: Header and footer information from the PL2 file, for navigating a potentially multi-GB file. If None,
            the method will read in that information first.
    Returns:
        A dictionary mapping the name of each saved data file to a 2-tuple (start, stop) containing the start and stop
            timestamps of the corresponding Maestro trial in seconds since the start of the Omniplex recording. The
            dictionary will be empty if the expected event channel data is not found in the PL2 file.
    Raises:
        Exception: If a problem is detected while analyzing the Omniplex strobed character and event channels.
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
    start_code_mask = np.equal(strobed_data["strobed"], 0x02)
    stop_code_mask = np.equal(strobed_data["strobed"], 0x03)
    null_code_mask = np.equal(strobed_data["strobed"], 0x00)
    start_code_indices = np.where(start_code_mask)[0]

    # helper function used to find, eg, the stop code character after a start code character. Returns -1 if not found!
    def find_next(mask: np.ndarray, after: int) -> int:
        for _i in range(after + 1, len(mask)):
            if mask[_i]:
                return _i
        return -1

    for idx, start_code_index in enumerate(start_code_indices):
        first_null_index = find_next(null_code_mask, start_code_index)
        second_null_index = -1 if first_null_index == -1 else find_next(null_code_mask, first_null_index+1)
        stop_code_index = -1 if second_null_index == -1 else find_next(stop_code_mask, second_null_index)
        if stop_code_index == -1:
            continue   # see NOTE in function header

        file_name = "".join([chr(code) for code in strobed_data['strobed'][first_null_index + 1:second_null_index]])
        file_was_saved = (any(np.equal(strobed_data["strobed"][second_null_index + 1:stop_code_index], 0x06)))
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


def _prepare_neural_units(job_id: str, channel_id: str, spikes: List[np.ndarray], filename: str,
                          fp: IO, info: Dict[str, Any]) -> Optional[List[OmniplexUnit]]:
    """
    Helper method for _process_omniplex_file(). It processes the Omniplex analog data channel on which identified neural
    units were recorded and calculates selected metrics for those units: firing rate, SNR, and the average spike
    template waveform.

    On calculating the template waveform and SNR for each neural unit: The unit's recorded channel ID must start
    with "WB" (wide band data) or "SPKC" (narrow band data). Wide band data is preferred because the filtering
    parameters for SPKC can be changed during an Omniplex session and are not stored in the PL2 file. If the
    specified channel ID is "SPKC<num>", the method first looks for the wide-band channel "WB<num>". If that is
    available, the analog trace is bandpass-filtered between 300-8000Hz using a econd-order Butterworth filter via the
    SciPy package. If not, the analog trace on "SPKC<num>" is used as is (it should already have been filtered).

    [NOTE: In the channel ID string "SPKC<num>" or "WB<num>", "<num>" should evaluate to a 1-, 2- or 3-digit positive
    integer. Since Plexon software version 1.19, there is support for 128 wide-band and narrow-band analog channels, so
    a 3-digit (zero-filled, eg, "001") number is needed. In earlier versions, only 2 digits were needed to represent
    the channel number.]

    To calculate the template waveform, the method averages 10-ms "clips" in the filtered trace that start 1ms prior
    to each spike timestamp. To calculate SNR, the method first estimates the standard deviation of the background
    noise as 1.4826 * median absolute deviation (MAD) of the data trace. The MAD = median(abs(x-X)) = median(abs(x))
    because X = median(x) is approximately 0 since the trace x has been bandpass-filtered, removing any DC offset.
    Then: SNR = (max(template) - min(template)) / 1.96 * std_background_noise.

    Since Omniplex recordings can be very long, and a wide-band analog trace must be bandpass-filtered once it is
    loaded from the PL2 file, this method would consume a lot of memory if the entire trace is loaded at once.
    Furthermore, loading a huge array can take many seconds, which prevents regular job progress updates or promptly
    detecting that the commit job was cancelled by the user. For these reasons, the method uses a "chunked" approach to
    the calculations, reading and processing one block (65535 samples each, except the last block) at a time. Progress
    messages are posted every 5 seconds.

    It is possible for a single extracellular electrode to record activity from multiple neural units at the same
    time. It would be wasteful to re-process the same analog channel for each neural unit "sorted" from that
    channel, especially since the background noise calculation will be the same for all. Hence, the "spikes"
    argument is a list containing a spike timestamps array for each distinct unit recorded on the channel.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        channel_id: ID of the Omniplex analog data channel: wide-band "WB<num>" or narrow-band "SPKC<num>", where <num>
            is a 1-, 2- or 3-digit positive integer. Thus, "WB8", "WB08", and "WB008" all identify the analog wide-band
            channel number 8.
        spikes: List of Numpy arrays; each array holds the spike timestamps (in seconds during Omniplex recording)
            for a distinct neural unit recorded on the specified analog channel. It is assumed that each array
            contains at least two spike times.
        filename: The source PL2 filename.
        fp: The PL2 file object. The file must be open and is NOT closed on return.
        info: Dictionary containing "table of contents" for the PL2 file (see PL2.load_file_information).

    Returns:
        A list of neural unit objects containing the spike times array, firing rate, and other metrics calculated
            from the original analog data. Returns None if the commit task was cancelled while preparing the units.

    Raises:
        Exception: If an error occurs while processing the analog data channel, or while updating commit job status or
            progress messages.
    """
    msg = f"Calculating metrics for {len(spikes)} neural unit(s) on Omniplex channel {channel_id} ..."
    if _background_job_update(job_id, msg, log=True):
        return None

    # wide-band or narrow-band channel. Extract Plexon-assigned channel number (a positive integer)
    is_wide_band = (len(channel_id) > 2) and (channel_id[0:2].lower() == 'wb')
    is_narrow_band = (len(channel_id) > 4) and (channel_id[0:4].lower() == 'spkc')
    if not (is_wide_band or is_narrow_band):
        raise Exception(f"Bad Omniplex channel ID: {channel_id}")
    channel_num = -1   # this is the Plexon-assigned channel number
    try:
        channel_num = int(channel_id[(2 if is_wide_band else 4):])
    except Exception:
        pass
    if channel_num < 1:
        raise Exception(f"Bad channel number in Omniplex channel ID: {channel_id}")

    # find zero-based index of the channel record in the Plexon file's list of analog channel records. If narrow-band
    # channel SPKC<num> was specified, try to use corresponding wide band channel WB<num>, IF it is available
    idx = get_analog_channel_record_index(info, is_wide_band=True, channel_number=channel_num)
    if idx > -1:
        is_wide_band = True
    elif is_narrow_band:
        idx = get_analog_channel_record_index(info, is_wide_band=False, channel_number=channel_num)
    if idx < 0:
        raise Exception(f"Did not find Omniplex analog channel data for channel ID: {channel_id}")

    num_blocks = len(info["analog_channels"][idx]["block_num_items"])
    samples_per_sec: float = info['analog_channels'][idx]['samples_per_second']
    to_volts: float = info['analog_channels'][idx]['coeff_to_convert_to_units']
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
        curr_block = PL2.load_analog_channel_block_faster(fp, idx, block_idx, info)
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
        if (time.time() - t0) > 5:
            msg = f"Calculating metrics for {len(spikes)} neural unit(s) on Omniplex channel {channel_id} ... " \
                  f"{100.0*block_idx/num_blocks:.1f}%"
            if _background_job_update(job_id, msg, overwrite=True):
                return None
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
        out.append(OmniplexUnit(src=filename, ch=channel_id, spikes=spikes[i], num_spikes=len(spikes[i]),
                                rate=firing_rate, snr=snr, template=template[i]))
    return out


def protocol_names(job_id: str) -> Optional[List[str]]:
    """
    Get the path names (in the form 'set/subset/trial_name') of all trial protocols detected during pre-processing of
    the session data ZIP archive for the specified commit job. Any protocol for which fewer than 3 trial reps were
    processed -- and which don't match an existing protocol in the database -- are "protocol candidates" requiring user
    review and verification.

    This information is available ONLY during the "Review" phase of a commit job -- after preprocessing and before the
    actual database commit begins.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.

    Returns:
        The list of protocol path names. The list is not sorted, but indicates the order in which the protocols were
            detected in the preprocessing stage. It is unlikely, but theoretically possible, that two protocols could
            have the same path name. A protocol's pathname is prepended with '**' if that protocol requires manual user
            validation. Returns None if operation fails.
    """
    try:
        raw_protocols = get_config().redis_conn.lrange(f"{PROTODEFS_NS}{job_id}", 0, -1)
        if not isinstance(raw_protocols, list):
            raise Exception(f"Cached protocol definitions not found")
        protocols = [maestro.Protocol.from_bytes(r) for r in raw_protocols]
        proto_names = list()
        for p in protocols:
            proto_names.append(f"{'** ' if p.is_candidate else ''}{p.trial.path_name}")
        return proto_names
    except Exception as e:
        _logger.error(f"Error while retrieving trial protocol names for commit job {job_id}: {str(e)}", exc_info=True)
        return None


def protocol_definition(job_id: str, index: int) -> Optional[maestro.Protocol]:
    """
    Get the full definition of a trial protocol culled during preprocessing of the session data ZIP archive for the
    specified commit job.

    This information is available ONLY during the "Review" phase of a commit job -- after preprocessing and before the
    actual database commit begins. In concert with protocol_names(), this method provides a mechanism by which the
    client front-end can present a user interface for reviewing each trial protocol and validating any protcol that
    requires manual validation (1 or 2 reps encountered, and does not match an existing protocol in the database).

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        index: The zero-based index of the protocol candidate requested. IMPORTANT: This corresponds to the protocol's
            ordinal position in the list returned by protocol_names().
    Returns:
        The requested trial protocol. Returns None if operation fails.
    """
    try:
        raw_proto = get_config().redis_conn.lindex(f"{PROTODEFS_NS}{job_id}", index)
        if raw_proto is None:
            raise Exception(f"Cached protocol definition not found at index {index}")
        return maestro.Protocol.from_bytes(raw_proto)
    except Exception as e:
        _logger.error(f"Error while retrieving trial protocol definition for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return None


def add_rv_to_protocol(job_id: str, index: int, rv: maestro.SegParam) -> Optional[maestro.Protocol]:
    """
    Add a random variable to the definition of a trial protocol culled during preprocessing of the session data ZIP
    archive for the specified commit job. This operation is available only during the review phase of the job, when the
    user interactively validates any trial protocols that require manual validation.

    When a protocol definition is based on fewer than 3 trial reps over the course of a session, AND it does not match
    an existing trial protocol in the lab database, it is considered a "candidate" protocol. The user must validate the
    definition before the protocol and the session can be committed to the database. Part of validation may require
    adding any missing random variables that are part of that definition. When only 1 rep is processed, it is impossible
    to identify any random variables; with only 2 reps, it's possible we might miss one.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        index: The zero-based index of the protocol candidate to update.
        rv: The random variable to be added to the protocol definition.
    Returns:
        The revised protocol candidate definition. Returns None if the operation failed for whatever reason.
    """
    try:
        conn = get_config().redis_conn
        raw_proto = conn.lindex(f"{PROTODEFS_NS}{job_id}", index)
        if raw_proto is None:
            raise Exception(f"Cached protocol definition not found at index {index}")
        proto: maestro.Protocol = maestro.Protocol.from_bytes(raw_proto)
        if not proto.add_random_variable(rv):
            raise Exception("Invalid random variable specification, or protocol is already validated")
        conn.lset(f"{PROTODEFS_NS}{job_id}", index, proto.to_bytes())
        return proto
    except Exception as e:
        _logger.error(f"Error while adding RV to trial protocol definition for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return None


def validate_protocol(job_id: str, index: int) -> bool:
    """
    Validate the definition of a trial protocol culled during preprocessing of of the session data ZIP archive for the
    specified commit job. This operation is available only during the review phase of the job.

    When a protocol's definition is based on fewer than 3 trial reps over the course of a session, AND it does not match
    an existing trial protocol in the lab database, the user must manually add any missing random variables in the
    protocol definition and mark the protocol candidate as valid before the protocol and the session can be committed to
    the database.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        index: The zero-based index of the protocol candidate to validate.
    Returns:
        True if candidate was marked as validated. Returns False if the operation failed for whatever reason.
    """
    try:
        conn = get_config().redis_conn
        raw_proto = conn.lindex(f"{PROTODEFS_NS}{job_id}", index)
        if raw_proto is None:
            raise Exception(f"Cached protocol definition not found at index {index}")
        proto: maestro.Protocol = maestro.Protocol.from_bytes(raw_proto)
        proto.validate()
        conn.lset(f"{PROTODEFS_NS}{job_id}", index, proto.to_bytes())
        return True
    except Exception as e:
        _logger.error(f"Error while validating trial protocol definition for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return False


def ready_to_commit(job_id: str) -> Tuple[bool, bool, str]:
    """
    Check whether or not the session data for an in-progress commit job is valid and ready to be committed to the portal
    database. This operation is only available during the review phase of the job.

    After preprocessing, a commit job enters the review phase ONLY if there is at least one trial protocol in the commit
    that requires manual validation. This method checks all trial protocol definitions (cached in Redis). If any
    remain unvalidated, the session is not ready to be committed.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        A 3-tuple (ok, ready, msg), where ok indicates whether or not the check was successful. If ok==False, an error
            occurred on the server and msg contains an error description. Otherwise, ready==False indicates that input
            from the user is required before the session can be committed and msg is a brief description of what is
            needed. If ready==True, then the session is ready to commit, and msg will contain a user-facing message to
            that effect.
    """
    try:
        raw_protocols = get_config().redis_conn.lrange(f"{PROTODEFS_NS}{job_id}", 0, -1)
        if not isinstance(raw_protocols, list):
            raise Exception(f"Cached protocol definitions not found")
        protocols = [maestro.Protocol.from_bytes(r) for r in raw_protocols]
        n = [p.is_candidate for p in protocols].count(True)
        if n > 0:
            return True, False, f"{n} trial protocols (marked with '**') require manual validation."
        else:
            return True, True, "\u2713 OK. Ready to commit."
    except Exception as e:
        _logger.error(f"Error while checking if commit job {job_id} is ready to commit: {str(e)}", exc_info=True)
        return False, False, "A server error occurred while checking cached trial protocols."


def commit_to_database(job_id: str) -> Optional[str]:
    """
    Request that the experiment data for a pending session commit job be committed to the database. This is the final
    phase of the session commit workflow.

    The specified commit job must currently be in the review stage, and the user must have validated all trial protocols
    found during preprocessing. If these requirements are met, the server transitions the job to the final "Commit"
    phase and queues a background task to perform that work.

    Args:
        job_id:  The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns None if successful. If the job is not in the review phase or is not ready to commit, or if a server
            error occurs, returns a brief error description.
    """
    try:
        job_status = commit_job_status(job_id)
        if isinstance(job_status, str):
            raise Exception(str)
        if job_status.state != CommitStateEnum.REVIEW:
            _logger.debug(f"Got request to finalize a commit job (id={job_id}) that is not in the review phase.")
            return f"Commit job must be in the 'Review' stage before committing to database"

        ok, ready, msg = ready_to_commit(job_id)
        if not (ok and ready):
            return f"Session not ready to be committed to database: {msg}"

        update_msg = "Queueing job to commit experiment session to the database."
        job_status.on_update(msg=update_msg, state=CommitStateEnum.COMMIT)
        get_config().redis_conn.hset(name=COMMITS, key=job_id, value=job_status.to_bytes())
        job_queue.enqueue(finish_commit_job, job_id, job_id=f"{job_id}-commit", job_timeout='60m')
        return None
    except Exception as e:
        _logger.error(f"Error while transitioning commit job {job_id} to commit phase: {str(e)}", exc_info=True)
        return "An error occurred while checking or updating commit job status on server"


def finish_commit_job(job_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the backend server, performs the final
    stage of the session commit workflow, in which the session data is pushed to the portal database.

    Committing a preprocessed experiment session to the database involves the following steps:
        1. Validate the commit job status. Make sure the job's staging folder exists in the server's local workspace
           and includes the commit information file, containing session metadata and results from preprocessing -- .
           trial timing information, trial protocol definitions, and neural unit metrics.

        2. Update trial protcol definitions in the commit information file, if necessary.

           If no trial protocols required validation, the workflow will have skipped the "review" phase and proceeded
           directly to the final commit. In this case, the session archive must also be present in the job's local
           staging folder, and the commit information file is in its final form.

           If some trial protocols required validation, the preprocessing task will cache ALL trial protocol definitions
           in Redis, delete the session archive from the local staging folder, and transition the job to the review
           phase. During that phase, the user interactively validates any trial protocols requiring it, and the relevant
           protocol definitions are updated in the Redis cache. In this scenario, the method must retrieve the updated
           trial protocol definitions and rewrite the commit information file accordingly. It must also download the
           session archive from the portal repository on S3 to the local staging folder.

        3. Entries are inserted into the Session, Session.EPhys, and Session.Neuron tables as appropriate, and all
           trial protocols not already in the database are inserted into the TrialProtocol table.

        4. The Trial table and its part tables are populated with data from all the trials presented during the
           session. We post a progress message and check for user cancel periodically during this process.

        5. The conmmit information file is appended to the original session archive ZIP. As a result, the ZIP file
           contains everything needed to recommit the experiment session -- without user intervention -- in the event
           the portal database was corrupted and had to be reconstructed from scratch.

        6. The altered ZIP file is uploaded to the portal's backing repository on S3. The object key under which the
           ZIP file is stored uniquely identifies the experiment session: "/repo/<E>/<S>_<D>_<F>.zip",
           where <E> is the username of the registered portal user that conducted the experiment, <S> is the experiment
           subject's ID, <D> is the experiment date as an ISO-formatted string 'YYYY-MM-DD', and <F> is the integer
           session suffix.

        7. The completed session commit is recorded in the database operations log. This single log entry (along
           with the ZIP file just stored in the backing repository) accounts for all of the database insertions required
           to commit the data from the experiment session.

    We rely on the database server's transaction mechanisms to ensure data consistency; all insertions into the database
    are encapsulated in a transaction. If an error occurs at any point during the commit, any changes to the database
    and the file repository are unwound before returning.

    Committing a large (>1GB) session to the database can take many minutes, so progress messages are periodically
    pushed to the commit job's state object cached in Redis. The method also checks the job's status regularly in
    case the user cancels the job through the backend.

    If the operation completes successfully, the job is moved to the "Done" stage in Redis, the job's staging folder
    in the local workspace is deleted, and the session archive in the staging area in the S3 repository (not the final
    version of the archive that's saved to "/repo") is deleted. The job status remains in Redis so that the user has a
    record of what happened and remove the completed job at a later time.

    If the operation is cancelled or fails at any point, the job is moved to the "Failed" state and the same cleanup
    is performed, with two notable exceptions:
        - If the job was not found in Redis, something may have gone wrong in Redis. We still try to do the cleanup, but
          the local staging folder and staged archive in S3 may not exist.
        - If the job was found but is not in the COMMIT state, we try to post a message to the job's progress history
          indicating the problem. We leave the job in its current state and do no cleanup. This should never happen.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        True if successful, in which case the session is fully committed to the database; False otherwise.
    """
    commit_info_path = Path(_get_job_subfolder(job_id), COMMIT_INFO_FNAME)
    """ Location of the commit information file in job's staging folder within local portal workspace. """
    zip_path: Path = Path(_get_job_subfolder(job_id), ARCHIVE_FNAME)
    """ Location of session archive in staging folder within local portal workspace. """
    trial_info: Dict[str, _TrialInfo]
    """ 
    Dictionary maps the filename for each Maestro data file in the session archive to timing and trial protocol info for
    the particular trial instance recorded in that file. In particular, this includes the Omniplex start and stop
    timestamps required to align neural responses recorded on the Omniplex system with the behavioral responses recorded
    by Maestro.
    """
    units: List[OmniplexUnit]
    """ 
    The list of neural units culled from the session data archive during pre-processing. Includes information required
    to prepare an entry in the Session.Neuron part table for each neural unit.
    """
    protocols: List[maestro.Protocol]
    """ 
    The list of trial protocols culled from the session data archive during preprocessing, stored in the commit
    information file, and possibly validated in the review phase.
    """
    session_info: Optional[SessionInfo] = None
    """
    Metadata about session that is supplied when the commit job is initiated and kept in the commit information file.
    It includes attributes from the Session and Session.EPhys tables.
    """
    added_proto_hashes: List[str] = []
    """ List containing the MD5 digest of each trial protocol that was added to the database during the commit. """
    archive_key_final: str = ""
    """ S3 repo key under which session archive is permanently stored after a successful commit. """

    # these two inner functions serve as callbacks while downloading the session archive from the S3 repo prior to
    # the database commit, or uploading the amended archive (with commit information file) to the S3 repo after the
    # database commit. These merely post progress updates for the job.
    t_last_dnld: float = -1
    t_last_upld: float = -1

    def _download_progress(pct: float) -> None:
        nonlocal t_last_dnld

        try:
            t = time.time()
            if t_last_dnld < 0 or (t - t_last_dnld > 10):
                t_last_dnld = t
                _background_job_update(job_id, f"Downloading session archive to local staging folder... {pct:.1f}%",
                                       dont_fail=True, overwrite=True)
        except Exception:
            pass

    def _upload_progress(pct: float) -> None:
        nonlocal t_last_upld

        try:
            t = time.time()
            if t_last_upld < 0 or (t - t_last_upld > 10):
                t_last_upld = t
                _background_job_update(job_id, f"Uploading session archive to portal repository... {pct:.1f}%",
                                       dont_fail=True, overwrite=True)
        except Exception:
            pass

    fail_msg, perform_cleanup, commit_done, archive_uploaded, job_found = "", True, False, False, False
    try:
        if _background_job_update(job_id, "Started final commit phase...", log=True):
            raise Exception("Operation cancelled")

        # verify job status and commit information file.
        job_status = commit_job_status(job_id)
        job_found = not isinstance(job_status, str)
        if not job_found:
            _logger.error(f"Failed to retrieve job status from Redis for {job_id}")
            return False
        elif job_status.state != CommitStateEnum.COMMIT:
            _logger.error(f"Commit job is not in the final commit phase: {job_status.state}")
            try:
                _background_job_update(job_id, f"Job not in the final commit phase: {job_status.state}. Try again?")
            except Exception:
                pass
            perform_cleanup = False
            return False
        elif not commit_info_path.is_file():
            _logger.error(f"Commit job information file for job {job_id} not found at: {str(commit_info_path)}")
            raise Exception("Missing commit information file in workspace staging area")

        # read in commit information and check if any protocols required validation
        session_info, _, trial_info, protocols, units = _read_commit_info_file(commit_info_path)
        review_required = any([p.is_candidate for p in protocols])

        # if protocol validation was required, get all protocol definitions from Redis and verify all are now
        # validated.
        if review_required:
            raw_protocols = get_config().redis_conn.lrange(f"{PROTODEFS_NS}{job_id}", 0, -1)
            if not isinstance(raw_protocols, list):
                raise Exception(f"Cached protocol definitions not found")
            protocols = [maestro.Protocol.from_bytes(r) for r in raw_protocols]
            for p in protocols:
                if p.is_candidate:
                    msg = f"Error: At least one trial protocol ({p.trial.path_name} still requires user validation!"
                    _logger.debug(f"Commit job {job_id} failed: {msg}")
                    raise Exception(msg)

        # check if session archive is in local staging folder, and download it from repo if not.
        if not zip_path.is_file():
            if _background_job_update(job_id, "Downloading session archive from S3 repo...", log=True):
                raise Exception("Operation cancelled")
            archive_on_repo = staged_archive_key_in_repo(job_id)
            if not repo.download_file(archive_on_repo, zip_path, log_func=_download_progress):
                _logger.error(f"Failed to download session archive from S3 repo for commit job {job_id}")
                raise Exception("Error: Unable to download session archive from staging area in repo")

        # at this point, all trial protocols should be validated, and the original session archive should be in the
        # staging folder. Now that trial protocol definitions are finalized, update per-trial info to include the
        # corresponding protocol's unique MD5 hexadecimal digest.
        for _, t_info in trial_info.items():
            protocol = protocols[t_info.proto_index]
            t_info.proto_hash = protocol.md5_digest

        # rewrite the commit information file to persist any changes in trial protocol definitions, as well as the
        # update to per-trial info.
        _write_commit_info_file(commit_info_path, session_info, [], trial_info, protocols, units)

        # here's where it all happens: the database inserts, rollback on failure, progress messages and check for
        # cancellation.
        if _background_job_update(job_id, "Committing session to database...", log=True):
            raise Exception("Operation cancelled")
        commit_mgr = _SessionCommitMgr(job_id, zip_path, session_info, trial_info, protocols, units)
        error_msg = commit_mgr.commit()
        if error_msg is not None:
            raise Exception(error_msg)
        commit_done = True

        added_proto_hashes = [p['proto_hash'] for p in commit_mgr.trial_protocols()]

        # at this point, session has been committed to the database and the commit information file is complete. Now we
        # need to add that file to the ZIP archive, then upload the amended ZIP archive to permanent storage in the the
        # portal backing repository. If any of those operations fail, we have to remove the session from the database!
        archive_key_final = f"/repo/{session_info.experimenter}/" \
                            f"{session_info.subject}_{session_info.iso_recording_date}_{session_info.suffix}.zip"

        if _background_job_update(job_id, "Adding commit information to session archive...", log=True):
            raise Exception("Operation cancelled")
        with zipfile.ZipFile(zip_path, 'a') as f:
            f.write(commit_info_path, COMMIT_INFO_FNAME)

        if _background_job_update(job_id, "Transferring archive to portal repository...", log=True):
            raise Exception("Operation cancelled")
        if not repo.upload_file(zip_path, archive_key_final, log_func=_upload_progress):
            raise Exception(f"Unable to push committed session archive [{archive_key_final}] to portal repository")
        archive_uploaded = True

        # finally, log the session commit
        res = log_session_commit(session_info.experimenter, session_info.subject, session_info.iso_recording_date,
                                 session_info.suffix)
        if res:
            raise Exception(res)

        return True
    except Exception as e:
        fail_msg = str(e)
        return False
    finally:
        # cleanup: remove workspace staging folder and delete original archive ZIP from staging area in S3
        if perform_cleanup:
            _remove_job_subfolder(job_id)
            repo.delete_file(staged_archive_key_in_repo(job_id))

        # an error occurred after commit finished, so we attempt to delete the session and any added trial protocols
        if (len(fail_msg) > 0) and commit_done:
            _logger.error(f"Session commit {job_id} failed in final phase, after database insertions: {fail_msg}")
            # rollback the session commit, including any added trial protocols.
            ok = True
            err_msg = rollback_session_commit(session_info.session_table_entry(), added_proto_hashes)
            if err_msg:
                _logger.critical(f"Session commit rollback failed: {str(err_msg)}")
                ok = False
            if archive_uploaded:
                if not repo.delete_file(archive_key_final):
                    _logger.critical(f"Failed to remove session archive from repository {archive_key_final} during "
                                     f"commit rollback")
                    ok = False
            err_msg = f"Commit failed after database insertions; rollback {'successful' if ok else 'FAILED!'}"
            if job_found:
                try:
                    _background_job_update(job_id, err_msg, log=True)
                except Exception:
                    pass

        # transition to FAIL or DONE state, depending on whether commit succeeded. Can't do it if we didn't find the
        # job in Redis in the first place.
        if job_found and (commit_done or (len(fail_msg) > 0)):
            try:
                _background_job_update(job_id, fail_msg if (len(fail_msg) > 0) else "Done!",
                                       CommitStateEnum.FAIL if (len(fail_msg) > 0) else CommitStateEnum.DONE,
                                       log=True)
            except Exception:
                pass


class _SessionCommitMgr(SessionCommitter):
    """
    Helper class that performs the actual database table insertions that commit an experiment session to the portal
    database. It is used in two contexts: (1) during a new commit managed by a background worker process initiated
    through the portal server; or (2) during reconstruction of the database contents from the database update log
    and the archive files stored in the portal's backing repository on AWS S3.

    NOTE: Inserting data associated with a single trial can involve many individual database inserts: one for the entry
    into the Trial table itself, one for EACH recorded behavioral response trace inserted into the BehavioralResponse
    part table, one for EACH recorded unit spike train inserted into the NeuronalResponse part table, and one for EACH
    set of event timestamps inserted into the Event part table. To reduce the volume of database calls, we accumulate
    trial entries, behavioral response traces, neural unit spike trains and event timestamp data over "chunks" of 25
    trials at a time. Testing showed that a chunk size of ~25 trials significantly reduced the time it took to insert
    all trial data (versus one database insert at a time), but larger chunk sizes did not further increase performance.

    Usage: Construct the _SessionCommitMgr object, passing the required session, trial, and neural unit data and the
    path to the session data archive. Then invoke commit() to begin the (potentially long-running) database commit.
    """
    def __init__(self, job_id: Optional[str], zip_path: Path, session_info: SessionInfo,
                 trial_info: Dict[str, _TrialInfo], protocols: List[maestro.Protocol], units: List[OmniplexUnit]):
        """
        Construct the experiment session data commit manager.

        Args:
            job_id: If this is a normal session commit initiated by a user via the backend server, then this is ID of
                the associated commit job. In the context of a commit job, the Redis-cached job state is updated
                periodically with progress messages, and cancellation is possible. If the ID is None, then the commit is
                part of a database reconstruction task. In this case, progress messages are written to STDOUT, and the
                operation cannot be cancelled.
            zip_path: The path to the session data archive containing all Maestro trial data files.
            session_info: Metadata for the experiment session.
            trial_info: Dictionary of information about all trials presented during the experiment session, ascertained
                during preprocessing of the session archive. Keyed by trial data filenames.
            protocols: List of all Maestro trial protocols presented during the experiment session. Some may already
                exist in the database.
            units: List of all neural units recorded during the session. Will be an empty list for a behavior-only
                experiment session.
        """
        self.job_id: Optional[str] = job_id
        """
        The unique ID for the commit job, if the commit was initiated through the portal backend. If None, then
        progress messages are printed to the console and the commit is not cancellable.
        """
        self.zip_path: Path = zip_path
        """ File path to the session data archive. """
        self.session_info: SessionInfo = session_info
        """ Session metadata that must be written to the database. """
        self.trial_info: Dict[str, _TrialInfo] = trial_info
        """ Dictionary of trial information objects, keyed by trial data filenames. """
        self.protocols: List[maestro.Protocol] = protocols
        """ List of all trial protocols presented during the experiment session. """
        self.new_protocol_entries: Optional[List[Dict[str, AttributeValue]]] = None
        """ List of all new protocol entries that must be added to database, lazily created. """
        self.units: List[OmniplexUnit] = units
        """ List of all neural units recorded during experiment. Will be empty list for a behavioral session. """
        self.cancelled: bool = False
        """ Flag set if a cancel request detected. """

    def session_table_entry(self) -> Dict[str, AttributeValue]:
        return self.session_info.session_table_entry()

    def ephys_table_entry(self) -> Optional[Dict[str, AttributeValue]]:
        return self.session_info.ephys_table_entry() if self.session_info.number_of_units > 0 else None

    def neurons(self) -> List[Dict[str, AttributeValue]]:
        neuron_entries: List[Dict[str, AttributeValue]] = list()
        for i, unit in enumerate(self.units):
            neuron = dict()
            neuron['experimenter'] = self.session_info.experimenter
            neuron['subj_id'] = self.session_info.subject
            neuron['session_date'] = self.session_info.iso_recording_date
            neuron['session_sfx'] = self.session_info.suffix
            neuron['unit_id'] = i + 1
            neuron['unit_channel'] = unit.channel
            neuron['unit_type'] = unit.neuron_type
            neuron['unit_rate'] = unit.firing_rate
            neuron['unit_spikes'] = len(unit.spike_times)
            neuron['unit_snr'] = unit.snr
            neuron['unit_template'] = unit.template
            neuron_entries.append(neuron)
        return neuron_entries

    def trial_protocols(self) -> List[Dict[str, AttributeValue]]:
        if not self.new_protocol_entries:
            existing_proto_hashes = fetch_restrict_proj([DBTable.TRIAL_PROTOCOL])  # returns only 'proto_hash' PK
            if existing_proto_hashes is None:
                raise Exception("A database error occurred while retrieving set of existing trial protocols")
            existing_map = {p['proto_hash']: 1 for p in existing_proto_hashes}
            self.new_protocol_entries = list()
            for p in self.protocols:
                if p.md5_digest not in existing_map:
                    protocol_entry: Dict[str, Any] = dict()
                    protocol_entry['proto_hash'] = p.md5_digest
                    protocol_entry['proto_name'] = p.trial.name
                    protocol_entry['proto_set'] = "" if (p.trial.set_name is None) else p.trial.set_name
                    protocol_entry['proto_subset'] = "" if (p.trial.subset_name is None) else p.trial.subset_name
                    protocol_entry['proto_def'] = p.to_bytes()
                    self.new_protocol_entries.append(protocol_entry)
        return self.new_protocol_entries

    def update_progress(self, msg: str) -> None:
        if isinstance(self.job_id, str):
            if _background_job_update(self.job_id, msg):
                self.cancelled = True
                raise Exception("Commit job was cancelled")
        else:
            print(msg, file=sys.stdout, flush=True)

    def was_cancelled(self) -> bool:
        return self.cancelled

    def insert_trials_for_session(self, session_key: Dict[str, Any]) -> None:
        # generate list of trial file names in presentation order. We CANNOT rely on file creation time!
        sort_strategy = None
        sorted_filenames = None
        if isinstance(self.units, list) and (len(self.units) > 0):
            # if there is unit data, a CSV file in session archive may supply trial start times during Ephys recording
            # in lieu of PL2 file(s). If a single PL2 file present, we order trials by Omniplex start time. If more
            # than one PL2 file, we can't since there will be different timelines!
            uses_csv_file = (self.trial_info[next(iter(self.trial_info))].omniplex_stop is None)
            if uses_csv_file:
                sort_strategy = 'csv'
                sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].omniplex_start)
            else:
                pl2_file_set = {unit.source_file for unit in self.units}
                if len(pl2_file_set) == 1:
                    sort_strategy = 'omniplex'
                    sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].omniplex_start)
        if not sort_strategy:
            # otherwise, use internal timestamp if found in Maestro file header. Else order by numeric file suffix.
            if self.trial_info[next(iter(self.trial_info))].header_timestamp:
                sort_strategy = 'timestamp'
                sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].header_timestamp)
            else:
                sort_strategy = 'index'
                sorted_filenames = sorted(self.trial_info, key=lambda k: self.trial_info[k].file_index)

        num_trials = len(sorted_filenames)
        num_inserted = 0
        self.update_progress(f"{num_inserted} of {num_trials} trials added to database...")

        t0 = time.time()
        trial1_start_sec: float = 0

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
                if (t_info.omniplex_start is not None) and (t_info.omniplex_stop is not None):
                    trial_length = (data_file.trial.record_start + data_file.header.num_scans_saved - 1) / 1000.0
                    omniplex_length = t_info.omniplex_stop - t_info.omniplex_start
                    if abs(trial_length - omniplex_length) > 0.002:
                        raise Exception(f"Trial duration on Omniplex does not match Maestro trial "
                                        f"duration: {trial_filename}")
                    maestro_omniplex_time_scaling = trial_length / omniplex_length

                trial_entry: Dict[str, Any] = dict(
                    session_key,
                    trial_idx=(num_inserted + 1),
                    proto_hash=t_info.proto_hash,
                    trial_header=data_file.header.to_bytes(),
                    trial_filename=trial_filename,
                    trial_dur=data_file.header.num_scans_saved - 1,
                    trial_record_start=data_file.trial.record_start,
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
                    t_sec = t_info.omniplex_start if (sort_strategy == 'csv' or sort_strategy == 'omniplex') else \
                        t_info.header_timestamp / 1000.0
                    if num_inserted == 0:
                        trial_entry['trial_ts'] = 0
                        trial1_start_sec = t_sec
                    else:
                        trial_entry['trial_ts'] = t_sec - trial1_start_sec

                # for this trial, get the values of the protocol's random variables
                protocol = next((x for x in self.protocols if x.md5_digest == t_info.proto_hash), None)
                if protocol is None:
                    raise Exception(
                        f"Internal inconsistency: No trial protocol defined for trial in {trial_filename}")
                rv_values: List[Any] = list()
                for param in protocol.random_variables:
                    rv_value = data_file.trial.retrieve_segment_table_parameter_value(param)
                    if rv_value is None:
                        raise Exception(
                            f"Internal inconsistency: Invalid RV ({param}) for trial in {trial_filename}")
                    rv_values.append(rv_value)
                trial_entry['trial_rvs'] = json.dumps(rv_values).encode()

                trial_entries.append(trial_entry)
                num_trials_chunked += 1

                # accumulate recorded behavioral responses
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

                # accumulate neural unit responses, if any. A neural unit may not fire any spikes during a trial, but
                # that could be a valid response. Only exclude a unit if the last spike time is before trial start or
                # the first spike time is after trial end!
                for i, unit in enumerate(self.units):
                    spikes = unit.spike_times

                    # need to handle special case when there's no PL2 file in archive, and trial start times are
                    # supplied via a CSV file
                    t_start, t_stop = t_info.omniplex_start, t_info.omniplex_stop
                    if t_stop is None:
                        t_stop = t_info.omniplex_start + t_info.duration

                    if (spikes[-1] < t_start) or (spikes[0] > t_stop):
                        continue

                    spikes_in_trial = \
                        spikes[(spikes >= t_start) & (spikes <= t_stop)]
                    spikes_in_trial = (spikes_in_trial - t_start) * maestro_omniplex_time_scaling
                    response_entry = dict(
                        session_key,
                        trial_idx=(num_inserted + 1),
                        unit_id=(i + 1),
                        spike_times=spikes_in_trial
                    )
                    neuronal_entries.append(response_entry)

                # accumulate any recorded marker event timestamps (recorded in Maestro file, not by Omniplex).
                if data_file.events is not None:
                    for di_channel in data_file.events:
                        # convert event times from ms to sec and offset if event recording started after trial began
                        event_times = np.array(
                            data_file.events[di_channel]) * 0.001 + data_file.trial.record_start
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
                    self.batch_insert_trials(trial_entries, behavioral_entries, neuronal_entries, event_entries)
                    trial_entries.clear()
                    num_trials_chunked = 0
                    behavioral_entries.clear()
                    neuronal_entries.clear()
                    event_entries.clear()

                # report progress and check for cancel roughly once every 10 seconds
                if (time.time() - t0) > 10:
                    self.update_progress(f"{num_inserted} of {num_trials} trials added to database...")
                    t0 = time.time()


def reconstruct_database(initial_pwd: str) -> None:
    """
    Reconstruct the contents of the portal database by processing all entries in the database operations log.

    The database operations log is a single file containing the entire history of operations on the portal database. The
    database contents can be reconstructed from scratch by executing the operations stored in the log file in order. Of
    course, to execute a session commit job, the requisite session archive must also be available. These archives, one
    per committed session, are persisted in the portal's backing repository, which is maintained in a lab-provisioned
    AWS S3 bucket. (The database operations log is also backed up in S3, but only occasionally. The most up-to-date
    operations log will be found in the portal's workspace directory at ./logs/database_ops.log.)

    It is ESSENTIAL that the database be empty when the script is called -- that is it's assumed state just before the
    first operation recorded in the log file. The operations are logged in chronological order, and this method simply
    performs each operation in the log in the same order, thereby reconstructing the database content.

    Obviously, re-committing an experiment session to the database is the single most time-consuming task. The relevant
    session archive must be downloaded from the portal repository on S3 to a staging location in the portal workspace
    dirctory, and that archive is then "digested" to re-commit the experiment's data. The archive includes a binary file
    with the original results of pre-processing, so the re-commit is much faster than the original commit. The slowest
    part is likely to be downloading the archive from S3.

    NOTE: THIS IS AN ADMINISTRATIVE FUNCTION FOR USE ONLY WHEN THE PORTAL APPLICATION IS DOWN. It must be run in a
    python console script. During reconstruction, progress messages are written to STDOUT. No user intervention is
    required, as the operations log and the portal backing repository store everything that is needed to repopulate
    the database. The supplied password serves as the default password for all users added to the database. A default
    user password is required because no password information is stored in the database operations log.

    NOTE2: When a registered user changes their password, the User table in the database is updated accordingly, but
    the operation is NOT logged. So the database operations history does not preserve user password changes. See also
    change_portal_user_password() in user_ops.py.

    Args:
        initial_pwd: Serves as the initial password for all portal users added to the database during reconstruction.
            Must be a valid password, else reconstruction fails.
    """
    print(f"Starting database reconstruction from repository using operations log file in portal workspace...",
          file=sys.stdout, flush=True)
    get_application_logger().info("Starting database reconstruction")

    err_msg = database_empty()
    if err_msg:
        print(f"=====> ERROR: {err_msg}. Database must be empty prior to reconstruction!\n", file=sys.stdout,
              flush=True)
        get_application_logger().error("Reconstruction failed -- database was not empty.")
        return

    err_msg = validate_password(initial_pwd)
    if err_msg:
        print(f"=====> ERROR: Initial user password invalid - {err_msg}\n", file=sys.stdout, flush=True)
        return

    entries: List[Dict[str, Any]]
    try:
        entries = read_log_entries()
    except Exception as e:
        err_msg = f"Error occurred while reading database operations log: {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
        print(f"=====> {err_msg}", file=sys.stdout, flush=True)
        return

    try:
        for i, entry in enumerate(entries):
            print(f"Processing log entry #{i:04}: \n    {entry}", file=sys.stdout)
            if entry['op'] == 'add':
                # SPECIAL CASE: When adding a user, we must prompt for a password unless a common one is supplied
                if entry['table'] == DBTable.USER:
                    entry['row']['password'] = generate_password_hash(initial_pwd, method=PASSWORD_HASH_METHOD)
                err_msg = insert_into_table(entry['table'], entry['row'], log=False)
            elif entry['op'] == 'delete':
                err_msg = delete_from_table(entry['table'], entry['restriction'], log=False)
            elif entry['op'] == 'update':
                err_msg = update_table_row(entry['table'], entry['row'], log=False)
            elif entry['op'] == 'mapping':
                err_msg = update_mapping_table(entry['table'], entry['src_pk'], set(entry['dst_pks']), log=False)
            elif entry['op'] == 'session':
                err_msg = _reconstruct_session(entry)
            else:
                err_msg = f"Invalid log entry!"

            if err_msg is not None:
                raise Exception(err_msg)
    except Exception as e:
        print(f"=====> ERROR: Exception while reconstructing portal database: {str(e)}", file=sys.stdout, flush=True)
        get_application_logger().error(f"Reconstruction failed [{str(e)}].", exc_info=True)
        print("Manual reconstruction of database content required. Consult this progress log to assist in that"
              "reconstruction.", file=sys.stdout, flush=True)
        return

    print("Reconstruction completed successfully!", file=sys.stdout, flush=True)
    get_application_logger().info("Reconstruction completed successfully!")


def _reconstruct_session(log_entry: Dict[str, Union[str, int]]) -> Optional[str]:
    """
    Helper method for reconstruct_database().

    Commit an experiment session during scripted reconstruction of the portal database content from entries in the
    database operations log file and experiment data archives stored in the backing repository.

    For each experiment session, the session archive is stored in the portal's backing repository in AWS S3 under the
    object key "/repo/<experimenter>/<subj_id>_<session_date>_<session_sfx>.zip, where <experimenter> is the registered
    username of the experimenter, <subj_id> is the experiment subject's ID, <session_date> is the date of the experiment
    in the format 'YYYY-MM-DD', and <session_sfx> is the integer session suffix.

    During the original commit, session metadata and preprocessing results are stored in a file "commit_info.bin", which
    in turn is stored in the original session archive ZIP. As a result, re-committing the session requires no user
    intervention and is significantly faster because it does not require processing of a large PL2 file (which also
    would have to be extracted from the ZIP file). However, the archive ZIP must be downloaded from the repository to a
    staging directory in the portal workspace before it is processed, which could take a while.

    Progress messages are written to STDOUT.

    Args:
        log_entry: A database log entry for a session commit. This dictionary must have the form {'op': 'session',
            'username': str, 'subj_id': str, 'date': 'YYYY-MM-DD', 'suffix': int}. See description above.
    Returns:
        An error description if session commit fails; else None
    """
    key = f"/repo/{log_entry['username']}/" \
          f"{log_entry['subj_id']}_{str(log_entry['date'])}_{log_entry['suffix']}.zip"
    recon_dir = _get_job_subfolder("reconstruct")
    error_msg = None
    try:
        # create a staging folder for the reconstruction in the portal's local workspace
        recon_dir.mkdir(parents=True, exist_ok=False)
        zip_path = Path(recon_dir, f"{log_entry['subj_id']}_{str(log_entry['date'])}_{log_entry['suffix']}.zip")
        print(f"  > Downloading session archive from repository at {key}...", file=sys.stdout, flush=True)
        if not repo.download_file(key, zip_path, log_func=False):
            raise Exception("Failed while downloading session archive from repository.")

        # extract commit information file from session archive and read in its contents
        print(f"  > Loading commit information stored in session archive...")
        commit_info_path = Path(recon_dir, COMMIT_INFO_FNAME)
        with zipfile.ZipFile(zip_path, 'r') as archive:
            archive.extract(COMMIT_INFO_FNAME, path=str(recon_dir.absolute()))
        if not commit_info_path.is_file():
            raise Exception("Failed to extract commit information file from session archive")
        session_info, _, trial_info, protocols, units = _read_commit_info_file(commit_info_path)

        # here's where it all happens: the database inserts, rollback on failure, progress messages and check for
        # cancellation.
        session_label = f"{session_info.experimenter}-{session_info.subject}-{session_info.iso_recording_date}-" \
                        f"{session_info.suffix}"
        print(f"   > Reconstructing session [{session_label}] in database...", file=sys.stdout)
        commit_mgr = _SessionCommitMgr(None, zip_path, session_info, trial_info, protocols, units)
        error_msg = commit_mgr.commit()
        if error_msg:
            return error_msg
        print("   > Session was successfully committed to database.", file=sys.stdout, flush=True)
    except Exception as err:
        error_msg = f"Exception while reconstructing experiment session:\n  {str(err)}"
    finally:
        # dispose of the temporary directory in which files were stored during reconstruction
        try:
            shutil.rmtree(recon_dir)
        except Exception as e:
            print(f"   > Warning - An exception occured while removing temporary "
                  f"directory {str(recon_dir)}:\n   {str(e)}", file=sys.stdout, flush=True)
    return error_msg


_COMMIT_INFO_VERSION: int = 1
""" Current version number for the binary file containing session metadata and preprocessing results. """


def _write_commit_info_file(file_path: Path, info: SessionInfo, nt_ids: List[int],
                            trial_info: Optional[Dict[str, _TrialInfo]] = None,
                            protocols: Optional[List[maestro.Protocol]] = None,
                            units: Optional[List[OmniplexUnit]] = None) -> None:
    """
    Write the binary session commit information file.

    This file contains information provided by the committer or compiled during the preprocessing phase. It is
    generated several times over the course of the commit workflow:
        - Upon initializing the commit job, the session metadata and neuron types are stored in the file.
        - After the preprocessing phase, trial protocol definitions, per-trial timing information, and neural unit
          metrics are added to the file. The unit metrics replace the list of unit types, as neuron type is one
          part of the metrics object, `OmniplexUnit`.
        - After the review phase (if necessary), the definition of each validated trial protocol is updated.

    Note that the file is updated merely by overwriting its contents entirely. After the experiment data has been
    fully committed to the portal database, this file is added to the session archive, which is then uploaded to the
    portal backup repository on S3. The file enables automated recommit of the experiment session in the event that
    database reconstruction is necessary.

    Args:
        file_path: Destination path for the commit information file.
        info: The session metadata.
        nt_ids: Neuron type ID assigned to each recorded neural unit. Ignored for behavior-only sessions, or if unit
            metrics are supplied (the neuron type ID is included in those metrics).
        trial_info: Information on each trial rep presented during session, keyed by the Maestro data file name.
            Provided after the preprocessing phase.
        protocols: The list of distinct Maestro trial protocols (vs individual reps) presented during session. Provided
            after the the preprocessing phase.
        units: Metrics (including neuron type ID) for each neural unit recorded during the session. Provided after the
            preprocessing phase. Ignored for behavior-only sessions.
    Raises:
        Exception: If operation fails for any reason.
    """
    try:
        info_raw = info.to_bytes()
        nt_ids_raw = json.dumps(nt_ids).encode() if ((info.number_of_units > 0) and (units is None)) else None
        trial_info_raw: Optional[bytes] = None
        if trial_info:
            json_trial_info = dict()
            for k, tinfo in trial_info.items():
                json_trial_info[k] = [
                    tinfo.file_index, tinfo.duration, tinfo.header_timestamp, tinfo.omniplex_start,
                    tinfo.omniplex_stop, tinfo.proto_index, tinfo.proto_hash
                ]
            trial_info_raw = json.dumps(json_trial_info).encode()
        hdr_raw = struct.pack("<6i", _COMMIT_INFO_VERSION, len(info_raw),
                              len(nt_ids_raw) if (nt_ids_raw is not None) else 0,
                              len(trial_info_raw) if (trial_info_raw is not None) else 0,
                              len(protocols) if (protocols is not None) else 0,
                              len(units) if ((info.number_of_units > 0) and (units is not None)) else 0)
        with open(file_path, 'wb') as f:
            f.write(hdr_raw)
            f.write(info_raw)
            if nt_ids_raw is not None:
                f.write(nt_ids_raw)
            if trial_info_raw is not None:
                f.write(trial_info_raw)
            if protocols is not None:
                for p in protocols:
                    proto_raw = p.to_bytes()
                    f.write(struct.pack('<i', len(proto_raw)))
                    f.write(proto_raw)
            if (info.number_of_units > 0) and (units is not None):
                for u in units:
                    unit_raw = u.to_bytes()
                    f.write(struct.pack('<i', len(unit_raw)))
                    f.write(unit_raw)
    except Exception as e:
        emsg = f"Failed to write commit information file - {str(e)}"
        raise Exception(emsg)


def _read_commit_info_file(file_path: Path) -> \
        Tuple[SessionInfo, List[int], Dict[str, _TrialInfo], List[maestro.Protocol], List[OmniplexUnit]]:
    """
    Read the binary session commit information file, as previously written by `_write_commit_info_file()`.

    The information contained in the file and returned by this method varies depending on the current phase of the
    commit workflow and whether or not any neural units were recorded during the session:
        - The session metadata is always present.
        - The neuron type ID list is non-empty only after initialization and before preprocessing has finished -- and
          only if neural units were recorded.
        - The trial timing information dictionary and trial protocols list are non-empty only after preprocessing has
          completed.
        - The unit metrics list is empty only after preprocessing has finished -- and only if neural units were
          recorded during the session.

    Args:
        file_path: Source path for the session preprocessing file.
    Returns:
        A 5-tuple: session metadata; the list of neuron type IDs assigned to the recorded neurol units; a dictionary
            with information on each trial rep presented during session, keyed by the Maestro data file name; the list
            of distinct Maestro trial protocols (vs individual reps) presented; and the list of neural unit metrics for
            the recorded neural units. As described above, some of these may be empty depending on the phase of the
            commit job and whether or not neural units were recorded.
    Raises:
        Exception: If operation fails for any reason.
    """
    try:
        with open(file_path, 'rb') as f:
            hdr_size = struct.calcsize("<6i")
            hdr_raw = f.read(hdr_size)
            if (not hdr_raw) or (len(hdr_raw) != hdr_size):
                raise Exception('Hit EOF unexpectedly while reading file header')
            v, info_size, nt_id_size, tinfo_size, num_proto, num_units = struct.unpack("<6i", hdr_raw)
            if v != _COMMIT_INFO_VERSION:
                raise Exception('Bad file version')
            if (info_size < 0) or (nt_id_size < 0) or (tinfo_size < 0) or (num_proto < 0) or (num_units < 0):
                raise Exception('Invalid file header')

            info_raw = f.read(info_size)
            if (not info_raw) or (len(info_raw) != info_size):
                raise Exception('Hit EOF unexpectedly while reading session metadata')
            info = SessionInfo.from_bytes(info_raw)

            nt_ids = []
            if nt_id_size > 0:
                nt_ids_raw = f.read(nt_id_size)
                if (not nt_ids_raw) or (len(nt_ids_raw) != nt_id_size):
                    raise Exception('Hit EOF unexpectedly while reading neuron type ID list')
                nt_ids = json.loads(nt_ids_raw.decode())

            trial_info: Dict[str, _TrialInfo] = dict()
            if tinfo_size > 0:
                trial_info_raw = f.read(tinfo_size)
                if (not trial_info_raw) or (len(trial_info_raw) != tinfo_size):
                    raise Exception('Hit EOF unexpectedly while reading trial reps info')
                json_trial_info = json.loads(trial_info_raw.decode())
                for k, v in json_trial_info.items():
                    trial_info[k] = _TrialInfo(file_index=v[0], duration=v[1], header_timestamp=v[2],
                                               omniplex_start=v[3], omniplex_stop=v[4], proto_index=v[5],
                                               proto_hash=v[6])

            int_sz = struct.calcsize("<i")
            protocols: List[maestro.Protocol] = list()
            if num_proto > 0:
                for _ in range(num_proto):
                    sz_raw = f.read(int_sz)
                    if (not sz_raw) or (len(sz_raw) != int_sz):
                        raise Exception('Hit EOF unexpectedly in trial protocols section')
                    proto_raw_sz, = struct.unpack("<i", sz_raw)
                    proto_raw = f.read(proto_raw_sz)
                    if (not proto_raw) or (len(proto_raw) != proto_raw_sz):
                        raise Exception('Hit EOF unexpectedlyin trial protocols section')
                    protocols.append(maestro.Protocol.from_bytes(proto_raw))

            units: List[OmniplexUnit] = list()
            if num_units > 0:
                for _ in range(num_units):
                    sz_raw = f.read(int_sz)
                    if (not sz_raw) or (len(sz_raw) != int_sz):
                        raise Exception('Hit EOF unexpectedly in neural units section')
                    unit_raw_sz, = struct.unpack("<i", sz_raw)
                    unit_raw = f.read(unit_raw_sz)
                    if (not unit_raw) or (len(unit_raw) != unit_raw_sz):
                        raise Exception('Hit EOF unexpectedly while reading a trial protocol')
                    units.append(OmniplexUnit.from_bytes(unit_raw))

        return info, nt_ids, trial_info, protocols, units
    except Exception as e:
        emsg = f"Failed to read commit information file - {str(e)}"
        raise Exception(emsg)


def queue_task_to_clean_commit_staging_areas(delay_minutes: float = 0) -> None:
    """
    Queue a background task that scans the local and remote session commit staging areas for any ophaned folders and
    files from commit jobs that were "lost" due to a prior system crash/restart, or other reason.

    Intended for administrative use only.

    Args:
        delay_minutes: If > 0, the background task is enqueued after the specfied delay in minutes. Default is 0,
            meaning the task is queued immediately.
    """
    if delay_minutes <= 0:
        job_queue.enqueue(clean_commit_staging_areas, job_id=f"clean-commit-staging", job_timeout='60m')
        _logger.info("Queued background task to clean commit staging areas.")
    else:
        job_queue.enqueue_in(timedelta(minutes=delay_minutes), func=clean_commit_staging_areas,
                             job_id=f"clean-commit-staging", job_timeout='60m')
        _logger.info(f"Queueing background task to clean commit staging areas {delay_minutes:.1f} min from now.")


def clean_commit_staging_areas() -> bool:
    """
    This method, intended to be called on a background process independent from the backend server, removes orphaned
    files from the session commit staging areas in the portal server's local workspace and in the backing repository in
    AWS S3.

    Status information on pending session commit jobs is kept in a dedicated key on the Redis server. The current
    implementation uses an in-memory Redis cache; it is NOT backed up to a persistent store. If the Redis server goes
    down for whatever reason, all pending commit job state is lost. However, the session archives and other files
    associated with those jobs are left "orphaned" in the portal workspace staging folder and/or the staging area in
    the portal's S3-based backing repository. (The session archives for all pending commits are uploaded to the S3
    staging area because the server can only spawn a few workers to do commit tasks, but there's no limit on how many
    experiment sessions may be queued for committing to the portal database.)

    This method gets a up-to-date list of all pending commit jobs from Redis, then removes any orphaned commit job files
    in both the local and S3-based staging areas. It should be run on a daily basis to avoid wasting local and S3
    storage on these orphaned files, which can be very large (session archives can be hundreds of MB to 10GB in size!).

    The method logs INFO messages to indicate what files/folders were removed, if any.

    Returns:
        True if successful; else False.

    """
    _logger.info("Scanning local and remote staging areas for orphaned commit job folders/files...")

    # get the current list of pending session commit jobs
    jobs = get_pending_commit_jobs_for(None)
    if isinstance(jobs, str):
        _logger.error(f"Unable to retrieve status pending session commit jobs [{jobs}]. Stopping.")
        return False
    job_ids: List[str] = [job.id for job in jobs]

    # remove any subfolder in the local staging folder that does not correspond to an existing job. The subfolder name
    # is the job ID.
    n_found, n_removed = 0, 0
    staging_dir = Path(get_config().dash_upload_dir)
    for child in staging_dir.iterdir():
        if not (child.name in job_ids):
            n_found += 1
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                ok = not child.exists()
                if ok:
                    n_removed += 1
                _logger.info(f"{'Removed' if ok else 'Could not remove'} orphaned commit job folder in local staging "
                             f"area: {child.name}")
            else:
                child.unlink(missing_ok=True)
                ok = not child.exists()
                if ok:
                    n_removed += 1
                _logger.info(f"{'Removed' if ok else 'Could not remove'} unexpected file in local staging "
                             f"area: {child.name}")
    _logger.info(f"Cleaned {n_removed} of {n_found} orphaned files/folders from local commit staging area.")

    # remove any session archives in the repository staging area that do not correspond to an existing job.
    n_found, n_removed = 0, 0
    s3_listing = repo.listing()
    if s3_listing is None:
        _logger.error("Unable to get repository file listing. Stopping.")
        return False
    for k, v in s3_listing.items():
        if k.startswith("/staging/"):
            job_id = k[9:]
            if not (job_id in job_ids):
                n_found += 1
                for file_info in v:  # there should be just one file, archive.zip, under each job.
                    ok = repo.delete_file(f"{k}/{file_info['name']}")
                    if ok:
                        n_removed += 1
                    _logger.info(f"{'Removed' if ok else 'Could not remove'} orphaned session archive in remote "
                                 f"staging area at: {k}/{file_info['name']}")
    _logger.info(f"Cleaned {n_removed} of {n_found} orphaned session archives from remote commit staging area.")
    return True
