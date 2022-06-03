"""
commit_ops.py: Operations involved in committing an experiment session archive to the Lisberger lab portal database.

The behavioral and neural response data recorded during an experiment session can easily reach several gigabytes in
size, so uploading, processing and committing that data to the portal database can take a significant amount of time
and must be offloaded to a background process so that the portal backend remains responsive to client requests.

Furthermore, it is important that the backend itself be "stateless" so that, when deployed "to the cloud", multiple
replicas of the backend can run simultaneously in order to field requests from multiple clients. Committing an
experimment session to the database is an inherently stateful workflow, so we need a way to maintain that state.

The workflow for committing an experiment session has the following stages:
    1) Uploading. During this phase, the session data archive (containing all Maestro and Omniplex files, as well as
       a pickle file with neural unit spike times from spike sorting) is uploaded to a staging directory in the
       repository.
    2) Preprocessing. The session archive is preprocessed to collect timing information on trials, parse out trial
       protocols presented, calculate neural unit metrics, etc. This phase does not require user interaction and can
       occur in a background process. The preprocessing results must be cached somewhere so that the user can review
       them in the next phase.
    3) Review. In this interactive stage, the user (on the client) reviews the results of preprocessing, "fills in" any
       required information that is missing, then requests that the session be actually committed to the database.
    4) Commit. Here is where the experimental data is pushed into the various database tables in our schema. Again, the
       work is performed in a background process with no user interaction.
    5) Cancelling. The user should be able to cancel the job during any of the stages. In the preprocessing and commit
       phases, it may be a little while before the background worker detects the cancellation and aborts.
    6) Done/Fail. The session commit completed successfully, or failed for whatever reason.

Rather than use our MySQL/MariaDB database to store state for in-progress session commit jobs, we decided to use a
Redis server to cache this information. The Redis server does double-duty, since we use Redis Queue (RQ) workers to
handle the work during the preprocessing and commit stages of the workflow.

Various Redis keys are used to stare status information, progress messages, and selected preprocessing results for an
in-progress commit job. See the descriptions of the various Redis "namespace" prefixes defined in this module:
    COMMIT_NS:<username> : Redis LIST of the job IDs for all pending commits owned by <username>.
    STATUS_NS:<job_id> : Redis STRING holding latest status information for commit job <job_id>.
    PROGRESS_NS:<job_id> : Redis LIST of the most recent 30 (or less) progress messages for <job_id>.
    INFO_NS:<job_id> : Redis STRING holding session metadata for <job>id>. This key is created during preprocessing and
        may be revised via client input during the review phase.
    PROTONAMES_NS:<job_id> : Redis LIST of trial protocol names culled during preprocessing for commit job <job_id>.
    PROTODEFS_NS:<job_id> : Redis LIST of trial protocol definitions found during preprocessing and possibly modified
        via client input during the review phase. In same order as PROTONAMES_NS:<job_id>.
    UNITMETRICS_NS:<job_id> : Redis LIST of neural unit metrics (SNR, 10-ms template, etc; but no spike times) objects,
        one per unit recorded. Created during preprocessing stage and accessed during review stage.
    UNITTYPES_NS:<job_id> : Redis LIST of neuron type IDs assigned to each recorded unit for a commit job. Created
        during preprocessing and reviewed/revised during review phase. In same order as UNITMETRICS_NS:<job_id>.
The last two keys will not exist for a given commit job if the experiment session did not record from neural units.

When a new session commit job is initiated, it is assigned a unique identifier of the form 'commit-<uid>' and enters the
"uploading" phase. That stage is managed by the Dash Uploader component, which transfers the session data archive (a
single ZIP file that could be up to 10GB in size) in chunks from the client machine to an upload folder in the portal
repository (unique to that client's Flask session). After the archive is uploaded and ready for preprocessing, the
upload subfolder is renamed to the job ID (so the client can reuse the original upload subfolder for the next upload --
due to limitations of using Dash and the Dash Uploader component on the client). The results of pre-processing are also
stored in a pickle file in this directory.

Session commits are restricted to registered users with the appropriate access level. Calls to this module should be
protected by a mechanism that verifies the specified user is logged in with the access level required.

Storing session archives in S3. Once the data from an experiment session has been committed to the portal database, the
uploaded session archive and the preprocessing results are NOT discarded. Rather, the preprocessing results (a pickle
file) are added to the archive ZIP, and then this ZIP file is uploaded to the portal backing repository, which is
maintained in a Amazon Web Services S3 "bucket". The archive's object key is like a file system path:
/repo/<experimenter>/<subj_id>_<session_date>_<session_sfx>.zip, where <experimenter>, <subj_id>, <session_date> and
<session_sfx> form the primary key for the experiment session.

@author: sruffner
@created: 14oct2021
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import pickle
import re
import shutil
import sys
import time
import uuid
import zipfile

import numpy as np
import scipy.signal
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Union, List, Dict, Any, Optional, Tuple, IO, Set

from dash_uploader.httprequesthandler import get_chunk_name
from rq import Queue
from werkzeug.security import generate_password_hash

from config.app_logging import get_application_logger
from config.config import get_config
from database import repo
from sglportalapi import maestro, PL2
from database.log_ops import log_session_commit, log_file_path
from database.table_info import DBTable, AttributeValue, primary_key_of
from database.table_ops import fetch_attribute_values, fetch_one_row, fetch_rows, check_row, fetch_restrict_proj, \
    SessionCommitter, rollback_session_commit, database_empty, insert_into_table, delete_from_table, update_table_row, \
    update_mapping_table
from database.user_ops import validate_username, PASSWORD_HASH_METHOD, prompt_for_password
from sglportalapi.util import DocEnum

_logger = get_application_logger()


job_queue = Queue(connection=get_config().redis_conn)
""" Background jobs queue. """


COMMIT_NS: str = 'commit:'
""" Redis namepace for pending commit jobs. Append username to access LIST of the commit job IDs for that user. """
STATUS_NS: str = 'status:'
""" 
Redis namespace for commit job status. Append job ID to retrieve status information for that job. The STRING key
holds a pickled CommitJobStatus object summarizing the job's current status.
"""
PROGRESS_NS: str = 'progress:'
"""
Redis namespace for commit job progress  history. Append job ID to access a ZSET holding the most recent progress
messages posted for that job, scored by message timestamp.
"""
INFO_NS: str = 'info:'
"""
Redis namespace for cached session metadata. Append job ID to access session metadata for a commit job. The STRING key
is a pickled SessionMetaData object containing information to prepare the Session and -- if applicable -- Session.EPhys
table entries when the experiment session is committed to the database.
"""
PROTONAMES_NS: str = 'protonames:'
"""
Redis key namespace for the names of trial protocols presented during an experiment seesion. Append commit job ID to
access the trial protocol names for that job. Each element in the LIST is the name of a different protocol candidate -- 
prepended with '** ' if the candidate requires user validation and has not yet been validated. This key is present only
after the preprocessing phase of the commit job has finished, and the order in the list matches the order in which
protocol candidates were culled from the session data archive during that phase.
"""
PROTODEFS_NS: str = 'protodefs:'
"""
Redis key namespace for the definitions of all trial protocols presented during an experiment session. Append commit job
ID to access the trial protocol "candidate" definitions for that job. Each element in the LIST is a pickled
ProtocolCandidate object; the order matches that in the corresponding PROTONAMES_NS key. This key is present only after
the preprocessing phase of the commit job has finished.
"""
UNITMETRICS_NS: str = 'unitmetrics:'
"""
Redis key namespace for select metrics on neural units recorded during an experiment session. Append commit job ID to 
access the unit metrics for that job. Each element in the LIST is a pickled OmniplexUnit object holding metrics for a
distinct neural unit recorded during the session. The order in the list reflects the order in which units were culled
from the data archive during preprocessing. The key is present only after the preprocessing phase of the commit job has
finished, and only for experiment sessions in which neural units were recorded. NOTE that the unit spike times are
excluded from the metrics because they are not needed during the review phase and could potentially require a lot of
storage space.
"""
UNITTYPES_NS: str = 'unittypes:'
"""
Redis key namespace for the neuron type assigned to each neural unit recorded during an experiment session. Append 
commit job ID to access the neural unit types for that job. Each element in the LIST is an integer specifying the ID
of the neuron type (NeuronType table in portal database) associated with the corresponding unit in the UNITMETRICS_NS
key. If no type has been associated with a given unit, then the ID is -1. This key is present only after preprocessing
of the commit job has finished, and only for experiment sessions in which neural units were recorded. The user can
review and update the type of each neural unit during the review phase. 
"""
_STAGING_DIR_PREFIX: str = 'commit-'
""" Commit staging directory prefix, followed by a generated UUID. """
PROGRESS_HISTORY_SIZE: int = 30
""" Maximum number of messages kept in a commit job's progress message history."""
PREPROC_FNAME = 'preproc.pickle'
""" Results from preprocesing phase are stored in this file in the staging directory for a commit job. """


@dataclass()
class CommitJobStatus:
    id: str
    """ The commit job's ID. """
    owner: str
    """ The username of the commit job owner. """
    started: float
    """ Timestamp (seconds since the Epoch) when commit job was initiated. """
    state: CommitStateEnum
    """ The job's current state/phase. """
    zip: str
    """ 
    When the commit job is started, this is the name of the subfolder (within the portal's commit staging directory) to 
    which the session data archive file will be uploaded. The folder name is a UUID assigned when the uploader UI is 
    realized on the client. After upload has finished and is verified on the server side, this will be the archive
    filename. 
    """
    units: Optional[int] = None
    """ Number of neural units recorded in the session. Set during preprocessing; 0 for behavioral sessions. """
    updated: Optional[float] = None
    """ Timestamp (seconds since the Epoch) when last progress message was posted for the commit job. """
    msg: Optional[str] = None
    """ Text of the last progress message posted for the commit job. """


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
        working on the job, the job should NOT be deleted.
        """
        return self not in [CommitStateEnum.PREPROCESS, CommitStateEnum.CANCEL, CommitStateEnum.COMMIT]


@dataclass
class _TrialInfo:
    """
    A data container to accumulate information about each trial presented during an experiment session during the
    pre-processing phase of the session commit workflow: (1) identity of the trial protocol to which each trial rep
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
    num_spikes: int
    """ 
    Number of spikes sorted. This field is here for technical reasons -- so we can store unit metrics EXCEPT the spike
    times array, which could be VERY large.
    """
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
class SessionMetaData:
    """
    Data object holding metadata for an experiment session to be committed to the portal database. It includes all
    attributes of the Session table and its Session.EPhys part table that may be updated by the user during the review
    phase of a session commit job. It also includes the number of neural units recorded during the session. If zero,
    then the session is behavioral only and the Session.EPhys attributes do not apply.
    """
    experimenter: Optional[str] = None
    """ The user responsible for the experiment session (primary key into User table). """
    subj_id: Optional[str] = None
    """ ID of experiment subject (primary key into Subject table). """
    session_date: Optional[date] = None
    """ Date of session. """
    session_suffix: Optional[int] = None
    """ Session suffix (in case multiple sessions were recorded with the same subject on the same day. """
    rig_id: Optional[int] = None
    """ ID of the experiment rig (primary key into Rig table). """
    study_id: Optional[int] = None
    """ ID of the associated research study (primary key into Study table). """
    session_notes: Optional[str] = None
    """ Session notes. """
    num_units: Optional[int] = 0
    """ Number of neural units recorded during session; 0 for a behavior-only session. Not user-editable. """
    num_trials: Optional[int] = 0
    """ Total number of trials presented during session. Not user-editable. """
    ephys_src: Optional[str] = None
    """ The electrophysiology recording method/source. """
    probe_type: Optional[str] = None
    """ The electrophysiology recording probe type. """
    sampling_rate: Optional[float] = None
    """ Electrode signal sampling rate in Hz. """
    probe_x: Optional[float] = None
    """ X-coordinate of electrode location within recording cylinder implant, in mm.  """
    probe_y: Optional[float] = None
    """ Y-coordinate of electrode location within recording cylinder implant, in mm. """
    probe_depth: Optional[float] = None
    """ Electrode insertion depth in mm. """
    ba_id: Optional[int] = None
    """ ID of brain region in which electrode was inserted (primary key into BrainArea table). """

    def session_table_entry(self) -> Dict[str, Optional[AttributeValue]]:
        return dict(experimenter=self.experimenter, subj_id=self.subj_id, session_date=self.session_date,
                    session_sfx=self.session_suffix, rig_id=self.rig_id, study_id=self.study_id,
                    session_notes=self.session_notes, num_units=self.num_units, num_trials=self.num_trials)

    def ephys_table_entry(self) -> Dict[str, Optional[AttributeValue]]:
        return dict(experimenter=self.experimenter, subj_id=self.subj_id, session_date=self.session_date,
                    session_sfx=self.session_suffix, ephys_src=self.ephys_src, probe_type=self.probe_type,
                    sampling_rate=self.sampling_rate, probe_x=self.probe_x, probe_y=self.probe_y,
                    probe_depth=self.probe_depth, ba_id=self.ba_id)


def _get_subfolder_in_staging_directory(subfolder: str) -> Path:
    """
    The file system path of a subfolder within the portal's temporary staging directory. Session ZIP archives are
    uploaded to a subfolder in the staging directory. After upload, each session commit job has its own subfolder
    containing the uploaded ZIP archive, preprocessing results, and possibly other temporary files.
    """
    return Path(get_config().dash_upload_dir, subfolder)


def _remove_staging_dir(staging_dir: Path) -> None:
    """ Remove a commit task staging directory in its entirety."""
    try:
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir))
    except OSError:
        _logger.error(f"Failed to delete staging directory in repository at {str(staging_dir)}", exc_info=True)


def initiate_session_commit(username: str, upload_id: str) -> Union[str, CommitJobStatus]:
    """
    Initiate a session commit job on the lab database server. The method generates a unique ID for the job, creates a
    folder in the portal's staging directory where the ZIP archive is uploaded, and persists status information about
    the job. The client must supply the job ID in all future requests involving the commit job.

    Args:
        username: The username of the registered portal user requesting the session commit. The username is only
            checked for validity; it is ASSUMED that the specified user is currently logged-in and has the necessary
            privileges to commit experiment data to the portal.
        upload_id: Upload ID serves as the name of the upload folder within staging directory.
    Returns:
        Returns status information for the new commit job; if operation failed, returns a brief error description.
    """
    if not (isinstance(username, str) and validate_username(username)):
        raise ValueError('Invalid username')

    job_id = f"{_STAGING_DIR_PREFIX}{str(uuid.uuid4())}"
    upload_dir = _get_subfolder_in_staging_directory(upload_id)
    try:
        upload_dir.mkdir(parents=True, exist_ok=False)
    except Exception as err:
        msg = f"Failed to create temporary upload directory for commit job {job_id}: {str(err)}"
        _logger.error(msg, exc_info=True)
        return msg

    now = time.time()
    init_progress_msg = "Waiting for ZIP archive upload from client..."
    job_status = CommitJobStatus(id=job_id, owner=username, started=now, state=CommitStateEnum.UPLOADING,
                                 zip=upload_id, updated=now, msg=init_progress_msg)
    commit_jobs_key = f"{COMMIT_NS}{username}"
    status_key = f"{STATUS_NS}{job_id}"
    job_progress_key = f"{PROGRESS_NS}{job_id}"
    try:
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.rpush(commit_jobs_key, job_id)
            pipe.set(status_key, pickle.dumps(job_status))
            pipe.zadd(job_progress_key, {init_progress_msg: now})
            pipe.execute()
    except Exception as e:
        _logger.error(f"Failed to persist commit job info: {str(e)}", exc_info=True)
        _remove_staging_dir(upload_dir)
        return f"Failed to persist commit job information on server. Job dropped."
    return job_status


def get_pending_commit_jobs_for(username: str) -> Union[str, List[CommitJobStatus]]:
    """
    Retrieve status information for all in-progress session commit jobs belonging to the specified user.

    Args:
        username: The username of the registered portal user requesting the session commit. The username is only
            checked for validity; it is ASSUMED that the specified user is currently logged-in and has the necessary
            privileges to commit experiment data to the portal.
    Returns:
        On failure, returns a brief error description. Otherwise, returns a list job status objects for the pending
            commit jobs belonging to the user. Jobs are listed in descending order by start time, with the most
            recently initiated job first.
    """
    if not (isinstance(username, str) and validate_username(username)):
        raise ValueError('Invalid username')

    try:
        conn = get_config().redis_conn
        raw_job_ids = conn.lrange(f"{COMMIT_NS}{username}", 0, -1)  # IMPORTANT: List of byte strings, not strings
        out: List[CommitJobStatus] = list()
        if len(raw_job_ids) > 0:
            with conn.pipeline() as pipe:
                for raw_job_id in raw_job_ids:
                    pipe.get(f"{STATUS_NS}{raw_job_id.decode('utf-8')}")
                res = pipe.execute()
            for r in res:
                job_status: CommitJobStatus = pickle.loads(r)
                out.append(job_status)
        return out
    except Exception as e:
        _logger.error(f"Failed to retrieve status for pending commit jobs: {str(e)}", exc_info=True)
        return "Unable to retrieve commit job status information on server!"


def commit_job_status(job_id: str) -> Union[str, CommitJobStatus]:
    """
    Retrieve current status information for a pending commit job belonging to the specified user.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns the job's latest status information. If job not found or a server error occurs, returns a brief error
            description.
    """
    try:
        conn = get_config().redis_conn
        job = conn.get(f"{STATUS_NS}{job_id}")
        if job is None:
            _logger.debug(f"Got request for status info on a commit job (id={job_id}) that does not exist.")
            return f"Commit job (id={job_id}) not found on server."
        job_status: CommitJobStatus = pickle.loads(job)
        return job_status
    except Exception as e:
        _logger.error(f"Error while retrieving commit job status info: {str(e)}", exc_info=True)
        return "An error occurred while retrieving commit job status on server"


def commit_job_progress(job_id: str) -> Union[str, List[str]]:
    """
    Retrieve the progress message history for a pending commit job.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns the job's progress history as a list of up to 30 message strings, sorted from most recent to oldest.
            The message strings are formatted as '(**datetime**) msg_text' so that they can be displayed in a
            Markdown element, emphasizing the datetime. If task not found or a server error occurs, returns a brief
            error description.
    """
    try:
        conn = get_config().redis_conn
        timestamped_messages = conn.zrevrange(f"{PROGRESS_NS}{job_id}", 0, -1, withscores=True)
        if (not isinstance(timestamped_messages, list)) or (len(timestamped_messages) == 0):
            _logger.debug(f"Got request for progress history on a commit task (id={job_id}) that does not exist.")
            return f"Commit job (id={job_id}) not found on server."
        out = [f"(**{datetime.fromtimestamp(x[1]).isoformat(' ', 'seconds')}**) {x[0].decode('utf-8')}"
               for x in timestamped_messages]
        return out
    except Exception as e:
        _logger.error(f"Error retrieving progress history for commit job {job_id}: {str(e)}", exc_info=True)
        return "An error occurred while retrieving commit job progress history on server"


def update_commit_job_on_archive_upload(job_id: str, filename: str) -> Union[str, CommitJobStatus]:
    """
    Update the status of a pending session commit job after the session archive has been fully uploaded to the staging
    directory for the commit. When the ZIP file has been uploaded, the server will queue a background worker to begin
    preprocessing the data in the archive.

    NOTE: The archive is uploaded in file "chunks" of 100MB each. These chunks are reassembled as the first step of
    preprocessing.

    Args:
        job_id:  The commit job identifier, assigned when the session commit was initiated on server.
        filename: The name of the ZIP file that was uploaded.
    Returns:
        On success, returns the job's latest status information, updated to include the name of the session ZIP file
            that finished uploading. If job not found or a server error occurs, returns a brief error description.
    """
    try:
        status_key = f"{STATUS_NS}{job_id}"
        progress_key = f"{PROGRESS_NS}{job_id}"
        # get job status dictionary
        conn = get_config().redis_conn
        job = conn.get(status_key)
        if job is None:
            _logger.debug(f"Got upload complete for a commit job (id={job_id}) that does not exist.")
            return f"Commit job (id={job_id}) not found on server."
        job_status: CommitJobStatus = pickle.loads(job)
        if job_status.state != CommitStateEnum.UPLOADING:
            _logger.debug(f"Got upload complete for a commit task (id={job_id}), but upload was already finished.")

        # the upload folder name is initially stored in the job status object in the 'zip' field. Rename that folder
        # with the job ID. This frees the original upload folder name for the next upload from the same client session.
        upload_dir = _get_subfolder_in_staging_directory(job_status.zip)
        staging_dir = _get_subfolder_in_staging_directory(job_id)
        upload_dir.rename(staging_dir)

        now = time.time()
        update_msg = f"Upload complete - {filename}. Queued job to preprocess session archive."
        job_status.zip = filename
        job_status.state = CommitStateEnum.PREPROCESS
        job_status.updated = now
        job_status.msg = update_msg
        with conn.pipeline() as pipe:
            pipe.set(status_key, pickle.dumps(job_status))
            pipe.zadd(progress_key, {update_msg: now})
            pipe.zremrangebyrank(progress_key, 0, -(PROGRESS_HISTORY_SIZE + 1))
            pipe.execute()

        job_queue.enqueue(preprocess_commit_job, job_id, job_id=f"{job_id}-preprocess", job_timeout='60m')

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

    In no background process is working on the commit job, the job is removed immediately. If a commit job finished
    successfully -- the "Done" state, meaning that the session data has been committed to the archive, this method
    merely removes the completed job from the owner's commit queue.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns a 3-tuple (removed, err_msg, job_status), where removed==True if commit job was removed (or not found);
            err_msg is a non-empty string only if a server error occurred, and job_status is the job's updated status
            if it was cancelled but removal is pending.
    """
    status_key = f"{STATUS_NS}{job_id}"
    progress_key = f"{PROGRESS_NS}{job_id}"
    try:
        conn = get_config().redis_conn
        job = conn.get(status_key)
        if job is None:
            # job not found -- assume it was already removed
            _logger.debug(f"Got request to remove a commit job (id={job_id}) that was not found.")
            return True, "", None
        job_status: CommitJobStatus = pickle.loads(job)
        if job_status.state.can_delete_job_in_this_state():
            # Job can be deleted immediately -- remove from Redis cache and delete relevant directory in repo
            # If cancelled during upload, remove the upload folder; else remove the staging folder for the commit
            target_dir = _get_subfolder_in_staging_directory(
                job_status.zip if job_status.state == CommitStateEnum.UPLOADING else job_id)
            _remove_staging_dir(target_dir)
            # all the job-specific keys that may need to be deleted. After preprocessing, there are keys holding info
            # used during subsequent phases. But if a session is behavioral only, the two neural unit keys won't exist.
            delete_keys = [progress_key]
            if job_status.state.value > CommitStateEnum.PREPROCESS.value:
                delete_keys.extend([f"{INFO_NS}{job_id}", f"{PROTONAMES_NS}{job_id}", f"{PROTODEFS_NS}{job_id}"])
                if job_status.units and job_status.units > 0:
                    delete_keys.extend([f"{UNITMETRICS_NS}{job_id}", f"{UNITTYPES_NS}{job_id}"])
            with conn.pipeline(True) as pipe:
                pipe.lrem(f"{COMMIT_NS}{job_status.owner}", 0, job_id)
                pipe.delete(*delete_keys)
                pipe.execute()
            return True, "", None
        elif job_status.state != CommitStateEnum.CANCEL:
            # If not already cancelling, move job to that state and append a progress message in Redis
            now = time.time()
            cancel_msg = "User cancelled job."
            job_status.state = CommitStateEnum.CANCEL
            job_status.msg = cancel_msg
            job_status.update = now
            with conn.pipeline() as pipe:
                pipe.set(status_key, pickle.dumps(job_status))
                pipe.zadd(progress_key, {cancel_msg: now})
                pipe.zremrangebyrank(progress_key, 0, -(PROGRESS_HISTORY_SIZE+1))
                pipe.execute()
        return False, "", job_status
    except Exception as e:
        _logger.error(f"Failed to cancel commit job {job_id}: {str(e)}", exc_info=True)
        return False, "An error occurred while trying to cancel commit job on server", None


def preprocess_commit_job(job_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the Dash/Flask backend server,
    pre-processes the uploaded session data ZIP archive for an in-progress commit job.

    A session archive ZIP file is uploaded from client to the server in 100MB chunks, and those chunks are stored in a
    unique subfolder in the portal's commit staging directory. The first step in preprocessing is to reassemble the
    archive file from the individual chunks. It then scans the archive contents and extracts information that will be
    needed when the session is actually committed to the lab database: (1) the unique trial protocols presented during
    the session; (2) timing information for all trial reps, in particular, the start and stop timestamps for the trial
    in the Omniplex timeline (for electrophysiological experiments using the Omniplex system); and (3) metrics for all
    neural units recorded in the session. It also initializes metadata that will be added to the database (Session and
    Session.EPhys tables) when the session is committed.

    Pre-processing a large (>1GB) session can take many minutes, so progress messages are delivered periodically to the
    commit job's Redis-cached progress history. The method also checks the job's status regularly in case the user
    cancels the job through the backend.

    If the operation is cancelled or fails at any point, the job is moved to the "Failed" state before returning. On
    successful completion, the job is moved to the "Review" stage.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        True if preprocessing is successful; False otherwise.
    """
    _logger.debug(f"Started preprocessing phase for commit job {job_id}")

    zip_path: Path
    """ Location of session archive in portal repository. """
    trial_info: Dict[str, _TrialInfo] = dict()
    """ 
    Dictionary maps the filename for each Maestro data file in the session archive to timing and trial protocol info for
    the particular trial instance recorded in that file. In particular, this includes the Omniplex start and stop
    timestamps required to align neural responses recorded on the Omniplex system with the behavioral responses recorded
    by Maestro.
    """
    units: List[OmniplexUnit] = list()
    """ 
    The list of neural units culled from the session data archive during pre-processing. Includes information required
    to prepare an entry in the Session.Neuron part table for each neural unit.
    """
    protocols: List[maestro.Protocol]
    """ The list of trial protocol culled from the session data archive during pre-processing. """
    session_info: SessionMetaData = SessionMetaData()
    """
    Metadata about session that is partially initialized during preprocessing phase, then reviewed and updated by user
    before committing the session to the database. It includes attributes from the Session and Session.EPhys tables.
    """

    try:
        # verify job status and ZIP file location in repo
        job_status = commit_job_status(job_id)
        if isinstance(job_status, str):
            _logger.error(f"Failed to retrieve job status from Redis for {job_id}")
            return False
        elif job_status.state != CommitStateEnum.PREPROCESS:
            _logger.error(f"Commit job is not in the correct stage for background preprocessing: {job_status.state}")
            return False
        elif not _reassemble_archive_from_chunked_upload(job_id, job_status.zip):
            return False
        else:
            zip_path = Path(_get_subfolder_in_staging_directory(job_id), job_status.zip)
        if not zip_path.is_file():
            msg_pfx = f"Cannot find archive file for commit job {job_id}: "
            _logger.debug(f"{msg_pfx}: {str(zip_path)}")
            _background_job_update(job_id, f"{msg_pfx}: {zip_path.name}", CommitStateEnum.FAIL)
            return False
        if _background_job_update(job_id, f"Preprocessing session archive {job_status.zip}"):
            return False

        with zipfile.ZipFile(zip_path, 'r') as archive:
            data_file_name_pattern = re.compile("[.]\\d\\d\\d\\d$")
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
                        raise Exception("Found more than one spikes data file in session data archive!")
            if (units_zip_info is not None) and (len(pl2s_archived) == 0):
                raise Exception("Missing Omniplex file(s) for spike-sorted unit data!")

            if _background_job_update(job_id, "Processing archive for trial protocols..."):
                return False
            existing_protos = set([str(h) for h in fetch_attribute_values(DBTable.TRIAL_PROTOCOL, 'proto_hash')])
            protocols, file_to_proto = \
                maestro.Protocol.extract_protocols_from_session_data(archive, existing_protos)
            if len(protocols) == 0:
                raise Exception("No trial protocols found in session archive!")
            for filename, proto_index in file_to_proto.items():
                trial_info[filename].proto_index = proto_index

            unit_data: Optional[Dict[str, List[Any]]] = None
            if units_zip_info is not None:
                if _background_job_update(job_id, f"Loading neural units file {units_zip_info.filename}..."):
                    return False
                unit_data = pickle.loads(archive.read(units_zip_info))
                pl2_filenames = [x.filename for x in pl2s_archived]
                if not _validate_neural_unit_data(unit_data, pl2_filenames):
                    raise Exception(f"Invalid format for neural units file: {units_zip_info.filename}")
                # if 'filename' field missing, assume all units recorded in same Omniplex file
                if 'filename' not in unit_data:
                    unit_data['filename'] = [pl2_filenames[0]] * len(unit_data['channel'])

            if units_zip_info is not None:
                for pl2_zip_info in pl2s_archived:
                    save_path = _chunked_extract_from_archive(job_id, archive, pl2_zip_info, zip_path.parent)
                    if save_path is None:
                        return False
                    if _process_omniplex_file(job_id, save_path, unit_data, trial_info, units):
                        return False

                # if there is unit data, we require metrics for each unit specified in the neural units data file,
                # and there must be Omniplex timestamps for all trials
                if len(units) < len(unit_data['channel']):
                    raise Exception(
                        f"Missing analog data for at least one unit defined in {units_zip_info.filename}")
                for key in trial_info.keys():
                    if trial_info[key].omniplex_start is None:
                        raise Exception(f"Missing Omniplex start/stop timestamps for {key}")

            # initialize session metadata. We get the session date from the Maestro trials, and we may get the
            # subject ID from the ZIP archive file name or a Maestro data file name.
            subject_choices = fetch_attribute_values(DBTable.SUBJECT, 'subj_id')
            subj_id_found: Optional[str] = None
            test_str = ','.join([zip_path.name.lower(), sample_maestro_file_name.lower()])
            for choice in subject_choices:
                if choice.lower() in test_str:
                    subj_id_found = choice
                    break
            _initialize_session_metadata(job_status.owner, session_date, subj_id_found, session_info, units)
            session_info.num_trials = len(trial_info)

            # save preprocessing results in a pickle file in the staging directory
            if _background_job_update(job_id, "Saving results from preprocessing..."):
                return False
            results = {'protocols': protocols, 'trials': trial_info, 'units': units,
                       'session': session_info}
            with open(Path(_get_subfolder_in_staging_directory(job_id), PREPROC_FNAME), 'wb') as file:
                pickle.dump(results, file)

            # store in Redis all information that will be needed to interact with user during the review phase: session
            # and electrophysiology metadata; protocol candidate definitions; and unit metrics (excluding spike times,
            # which could consume a lot of storage!)
            info = pickle.dumps(session_info)
            proto_names = list()
            proto_defs = list()
            for p in protocols:
                proto_names.append(f"{'** ' if p.is_candidate else ''}{p.trial.path_name}")
                proto_defs.append(pickle.dumps(p))
            unit_metrics = list()
            unit_types = list()
            for u in units:
                # we don't store spike times in Redis, and we leave neuron type as None b/c all of the unit neuron
                # types are stored in a separate key -- the user can only edit the neuron type of each unit.
                modified_unit = OmniplexUnit(u.source_file, u.channel, np.asarray([]), u.num_spikes, u.firing_rate,
                                             u.snr, u.template)
                unit_metrics.append(pickle.dumps(modified_unit))
                unit_types.append(-1 if u.neuron_type is None else u.neuron_type)
            with get_config().redis_conn.pipeline() as pipe:
                pipe.set(f"{INFO_NS}{job_id}", info)
                pipe.rpush(f"{PROTONAMES_NS}{job_id}", *proto_names)
                pipe.rpush(f"{PROTODEFS_NS}{job_id}", *proto_defs)
                if len(unit_metrics) > 0:
                    pipe.rpush(f"{UNITMETRICS_NS}{job_id}", *unit_metrics)
                    pipe.rpush(f"{UNITTYPES_NS}{job_id}", *unit_types)
                pipe.execute()

            if _background_job_update(job_id, "Preprocessing complete!", CommitStateEnum.REVIEW, len(units)):
                return False
    except Exception as err:
        error_msg = f"Error during preprocessing: {str(err)}"
        _logger.error(error_msg)
        _background_job_update(job_id, error_msg, CommitStateEnum.FAIL)
        return False

    return True


def _reassemble_archive_from_chunked_upload(job_id: str, zip_file_name: str) -> bool:
    """
    Helper method for preprocess_commit_job() handles the task of reconstructing the session archive from the
    individual file chunks that are uploaded to the server from the client.

    Session archives will typically be several GB in size, and the current upload mechanism uses chunking to keep the
    client responsive. If the upload was successful, the commit job's staging folder will contain a single subfolder
    containing all of the file chunks, with file names "zipfilename_part_NNN", where NNN is the chunk number.

    This method verifies the expeected contents of the staging folder, knits together the chunks in order into the
    original zip file, which is stored directly under the staging folder. The subfolder with the chunks is deleted.

    It can take a while to rebuild a multi-GB file, so the method will post progress messages and check for user
    cancel.
    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        zip_file_name: The file name for the session archive.
    Returns:
        True if successful, false otherwise.
    Raises:
        Exception: If a chunk file is missing, an IO or other error occurs.
    """
    # expect to find a SINGLE folder under the staging folder that contains the file chunks
    commit_job_dir = _get_subfolder_in_staging_directory(job_id)
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

    # reassemble chunks into ZIP file -- with progress updates every 5 seconds
    t0 = time.time()
    zip_path = Path(commit_job_dir, zip_file_name)
    with open(zip_path, "ab") as target_file:
        for i in range(1, num_chunks + 1):
            chunk_path = Path(temp_dir, get_chunk_name(zip_file_name, i))
            with open(chunk_path, "rb") as stored_chunk_file:
                target_file.write(stored_chunk_file.read())
            if (time.time() - t0) > 5:
                msg = f"Reassembling {zip_file_name} from chunked upload: {i} of {num_chunks} chunks processed."
                if _background_job_update(job_id, msg):
                    return False
                t0 = time.time()
    shutil.rmtree(temp_dir)
    return True


def _background_job_update(job_id: str, msg: str, next_state: Optional[CommitStateEnum] = None,
                           num_units: Optional[int] = None) -> bool:
    """
    Helper method used to update progress and, optionally, the state of a commit job. Intended for use ONLY within the
    background workers that handle the preprocessing and final commit phases of a job, this method will detect if the
    job has been cancelled and, if so, move the job to the "Failed" state. In this scenario, the specified progress
    message is not posted. However, if the job has just finished and is being moved to the "Done" state, the method
    does NOT check if the job was cancelled.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        msg: The new progress message to post.
        next_state: If not None, transition the job to this state. Default is None.
        num_units: When preprocessing finishes and the job is moved to the "Review" phase, this is the number of neural
            units found while preprocessing the session archive. It is cached in a field in the job's status object.

    Returns:
        True if job was in the "Cancelled" state and therefore moved to the "Failed" state; False otherwise.
    Raises:
        Exception: If an error occurs while reading or writing job status/progress history in Redis.
    """
    status_key = f"{STATUS_NS}{job_id}"
    progress_key = f"{PROGRESS_NS}{job_id}"

    was_cancelled = False
    conn = get_config().redis_conn
    job = conn.get(status_key)
    if job is None:
        raise Exception(f"Got request for status info on a commit job (id={job_id}) that does not exist.")
    job_status: CommitJobStatus = pickle.loads(job)
    if job_status.state not in [CommitStateEnum.PREPROCESS, CommitStateEnum.COMMIT, CommitStateEnum.CANCEL]:
        raise Exception(f"Commit job {job_id} found in an unexpected state for background work.")
    if (job_status.state == CommitStateEnum.CANCEL) and (next_state != CommitStateEnum.DONE):
        next_state = CommitStateEnum.FAIL
        was_cancelled = True

    if next_state:
        job_status.state = next_state
        if next_state == CommitStateEnum.REVIEW:
            job_status.units = num_units if isinstance(num_units, int) else 0
    now = time.time()
    job_status.msg = "Background task cancelled!" if was_cancelled else msg
    job_status.updated = now
    with conn.pipeline() as pipe:
        pipe.set(status_key, pickle.dumps(job_status))
        pipe.zadd(progress_key, {job_status.msg: now})
        pipe.zremrangebyrank(progress_key, 0, -(PROGRESS_HISTORY_SIZE + 1))
        pipe.execute()
    return was_cancelled


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
        unit_data: The dictionary loaded from the neural units pickle file.
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
        if _background_job_update(job_id, msg):
            return None
        save_path = Path(archive.extract(pl2_info, str(dst)))
        return save_path

    # Large file extract in chunks
    t0 = time.time()
    msg = f"Extracting Omniplex file {pl2_info.filename}: 0 of {size_in_mb:.1f} MB ..."
    if _background_job_update(job_id, msg):
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
                if _background_job_update(job_id, msg):
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
        if _background_job_update(job_id, msg):
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


def _prepare_neural_units(job_id: str, channel_id: str, spikes: List[np.ndarray], filename: str,
                          fp: IO, info: Dict[str, Any]) -> Optional[List[OmniplexUnit]]:
    """
    Helper method for _process_omniplex_file(). It processes the Omniplex analog data channel on which identified neural
    units were recorded and calculates selected metrics for those units: firing rate, SNR, and the average spike
    template waveform.

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
        channel_id: ID of the Omniplex analog data channel: wide-band "WBnn" or narrow-band "SPKCnn"
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
    if _background_job_update(job_id, msg):
        return None

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
        if (time.time() - t0) > 5:
            msg = f"Calculating metrics for {len(spikes)} neural unit(s) on Omniplex channel {channel_id} ... " \
                  f"{100.0*block_idx/num_blocks:.1f}%"
            if _background_job_update(job_id, msg):
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
        out.append(OmniplexUnit(filename, channel_id, spikes[i], len(spikes[i]), firing_rate, snr, template[i]))
    return out


def _initialize_session_metadata(
        username: str, session_date: Optional[date], subj_id: Optional[str], session_info: SessionMetaData,
        units: List[OmniplexUnit]) -> None:
    """
    Helper method for _preprocess_session_archive(). It looks up the experiment session most recently committed to
    the database by the user committing the current session, and uses metadata from that previous session to fill in
    reasonable defaults for the current session. If this is the user's first session commit, at least some session
    metadata will be left uninitialized.

    (NOTE there's an implicit assumption here that the user committing the current session is, in fact, the person
    that conducted that session.)

    Args:
        username:  The username of the registered portal user to which the commit job belongs.
        session_date: The session date as extracted from the header of a Maestro data file.
        subj_id: The ID of the experiment subject, if matched in the session archive filename or the name of a
            Maestro data file in that archive.
        session_info: [in/out] The session metadata object. This method initializes as many fields in this object as
            it can, based on information available.
        units: A list of all neural units recorded during the session. Will be empty for a behavioral session. This
            method will associate each unit with the "Unspecified" neuron type IF that type is in the database. Else,
            it is left untouched.
    Raises:
        Exception: If an error occurs while looking up the previous experiment session in the database.
    """
    # get most recent session committed by user (if one exists)
    recent_session: Optional[Dict[str, AttributeValue]] = None
    recent_ephys: Optional[Dict[str, AttributeValue]] = None
    sessions_for_user = fetch_rows(DBTable.SESSION, dict(experimenter=username))
    if sessions_for_user is None:
        raise Exception(f"A database error occurred while retrieving previous session metadata.")
    if len(sessions_for_user) > 0:
        recent_session = sorted(sessions_for_user, key=lambda s: (s['session_date'], s['session_sfx']), reverse=True)[0]
        pk = {k: recent_session[k] for k in primary_key_of(DBTable.SESSION)}
        recent_ephys = fetch_one_row(DBTable.SESSION_EPHYS, pk)

    # get defaults for subject, rig, study, and brain area IDs
    if subj_id is None:
        if recent_session:
            subj_id = recent_session['subj_id']
        else:
            subj_ids = fetch_attribute_values(DBTable.SUBJECT, 'subj_id')
            if subj_ids and (len(subj_ids) > 0):
                subj_id = subj_ids[0]
    default_rig_id = recent_session and recent_session['rig_id']
    default_study_id = recent_session and recent_session['study_id']
    if recent_session is None:
        rig_ids = fetch_attribute_values(DBTable.RIG, 'rig_id')
        study_ids = fetch_attribute_values(DBTable.STUDY, 'study_id')
        default_rig_id = rig_ids and (len(rig_ids) > 0) and int(rig_ids[0])
        default_study_id = study_ids and (len(study_ids) > 0) and int(study_ids[0])   # fetch returns np.int64 !!
    default_ba_id = recent_ephys and recent_ephys['ba_id']
    if recent_ephys is None:
        ba_ids = fetch_attribute_values(DBTable.BRAIN_AREA, 'ba_id')
        default_ba_id = ba_ids and (len(ba_ids) > 0) and int(ba_ids[0])    # fetch returns np.int64 !!

    # to initialize session suffix, we need to check if there are any sessions already committed by user with the
    # same subject on the same date. If we don't know subject or date, we can't do this and we use 1 for the suffix.
    session_sfx = 1
    if isinstance(session_date, date) and isinstance(subj_id, str):
        used: Set[int] = set()
        for session in sessions_for_user:
            if (session['subj_id'] == subj_id) and (session['session_date'] == session_date):
                used.add(session['session_sfx'])
        for i in range(1, 10):
            if i not in used:
                session_sfx = i
                break

    # initialize all neural units to neuron type "Unspecified" if it exists in database -- it should!
    unspecified_id: Optional[int] = None
    res = fetch_rows(DBTable.NEURON_TYPE, dict(nt_name="Unspecified"))
    if res and (len(res) == 1):
        unspecified_id = int(res[0]['nt_id'])

    # fill in whatever session metadata we can. The electrophysiology metadata is left untouched if no neural units
    # were recorded.
    session_info.experimenter = username
    session_info.subj_id = subj_id
    session_info.session_date = session_date
    session_info.session_suffix = session_sfx
    session_info.rig_id = default_rig_id
    session_info.study_id = default_study_id
    session_info.session_notes = ""
    session_info.num_units = len(units)
    if len(units) > 0:
        for unit in units:
            unit.neuron_type = unspecified_id
        channel_ids = {unit.channel for unit in units}
        session_info.ephys_src = 'Omniplex'
        session_info.probe_type = 'single' if len(channel_ids) == 1 else '32-channel'
        session_info.sampling_rate = len(units[0].template) / 0.01
        session_info.probe_x = None if (recent_ephys is None) else recent_ephys['probe_x']
        session_info.probe_y = None if (recent_ephys is None) else recent_ephys['probe_y']
        session_info.probe_depth = None if (recent_ephys is None) else recent_ephys['probe_depth']
        session_info.ba_id = default_ba_id


def session_metadata(job_id: str) -> Optional[SessionMetaData]:
    """
    Retrieve the session metadata for a commit job. The metadata is only available during the "Review" phase of a
    session commit job. It includes information that will be inserted into the Session and -- for an experiment in
    which one or more neural units were recorded -- the Session.EPhys tables in the portal database.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        The session metadata, or None if the operation fails.
    """
    session_info: Optional[SessionMetaData]
    try:
        info_raw = get_config().redis_conn.get(f"{INFO_NS}{job_id}")
        if info_raw is None:
            raise Exception("Session metadata not found!")
        return pickle.loads(info_raw)
    except Exception as e:
        _logger.error(f"Error while retrieving session metadata for commit job {job_id}: {str(e)}", exc_info=True)
        return None


def update_session_metadata(job_id: str, session_info: SessionMetaData) -> bool:
    """
    Update the session metadata for an in-progress commit job. This operation is available only during the review
    phase of the job, when the user interactively reviews and edits information required before the experiment session
    can be committed to the portal database.

    The supplied metadata need not be complete or valid; if not, the method returns a description of the first attribute
    in the metadata that is missing or invalid.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        session_info: The updated session metadata. It is not checked for completeness or validity. Select fields are
            ignored (num_units, num_trials) because these are fixed and cannot be changed by the user.

    Returns:
        True if successful; False otherwise
    """
    try:
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.get(f"{STATUS_NS}{job_id}")
            pipe.get(f"{INFO_NS}{job_id}")
            res = pipe.execute()
        if res is None:
            raise Exception("Did not find commit job status or session metadata on server!")
        job_status = pickle.loads(res[0])
        old_session_info = pickle.loads(res[1])
        if job_status.state != CommitStateEnum.REVIEW:
            raise Exception("Cannot modify session metadata for a commit job that is not in the 'Review' stage.")
        session_info.num_units = old_session_info.num_units   # the client must not change these
        session_info.num_trials = old_session_info.num_trials
        conn.set(f"{INFO_NS}{job_id}", pickle.dumps(session_info))
        return True
    except Exception as e:
        _logger.error(f"Error while updating session metadata for commit job {job_id}: {str(e)}", exc_info=True)
        return False


def protocol_names(job_id: str) -> Optional[List[str]]:
    """
    Get the path names (in the form 'set/subset/trial_name') of all trial protocols detected during pre-processing of
    the session data ZIP archive for the specified commit job. Any protocol for which fewer than 3 trial reps were
    processed -- and which don't match an existing protocol in the database -- are "protocol candidates" requiring user
    review and verification.

    This information is available ONLY during the "Review" phase of a commit job -- after pre-processing and before the
    actual database commit begins.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.

    Returns:
        The list of protocol path names. The list is not sorted, but indicates the order in which the protocols were
            detected in the pre-processing stage. It is unlikely, but theoretically possible, that two protocols could
            have the same path name. A protocol's pathname is prepended with '**' if that protocol requires manual user
            validation. Returns None if operation fails.
    """
    try:
        raw_names = get_config().redis_conn.lrange(f"{PROTONAMES_NS}{job_id}", 0, -1)
        if not isinstance(raw_names, list):
            raise Exception(f"Cached protocol names not found")
        return [r.decode('utf-8') for r in raw_names]
    except Exception as e:
        _logger.error(f"Error while retrieving trial protocol names for commit job {job_id}: {str(e)}", exc_info=True)
        return None


def protocol_definition(job_id: str, index: int) -> Optional[maestro.Protocol]:
    """
    Get the full definition of a trial protocol culled during pre-processing of the session data ZIP archive for the
    specified commit job.

    This information is available ONLY during the "Review" phase of a commit job -- after pre-processing and before the
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
        return pickle.loads(raw_proto)
    except Exception as e:
        _logger.error(f"Error while retrieving trial protocol definition for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return None


def add_rv_to_protocol(job_id: str, index: int, rv: maestro.SegParam) -> Optional[maestro.Protocol]:
    """
    Add a random variable to the definition of a trial protocol culled during preprocessing of the session data ZIP
    archive for the specified commit job. This operation is available only during the review phase of the job, when the
    user interactively reviews and edits information required before the experiment session can be committed o the
    portal database.

    When a protocol definition is based on fewer than 3 trial reps over the course of a session, AND it does not match
    an existing trial protocol in the lab database, it is considered a "candiaate" protocol. The user must validate the
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
        proto: maestro.Protocol = pickle.loads(raw_proto)
        if not proto.add_random_variable(rv):
            raise Exception("Invalid random variable specification, or protocol is already validated")
        conn.lset(f"{PROTODEFS_NS}{job_id}", index, pickle.dumps(proto))
        return proto
    except Exception as e:
        _logger.error(f"Error while adding RV to trial protocol definition for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return None


def validate_protocol(job_id: str, index: int) -> bool:
    """
    Validate the definition of a trial protocol culled during preprocessing of of the session data ZIP archive for the
    specified commit job. This operation is available only during the review phase of the job, when the user reviews and
    edits information required before the experiment session can be committed to the portal database.

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
        proto: maestro.Protocol = pickle.loads(raw_proto)
        proto.validate()
        # we cache the updated protocol candidate definition AND remove the '** ' from the cached protocol path name
        with conn.pipeline() as pipe:
            pipe.lset(f"{PROTODEFS_NS}{job_id}", index, pickle.dumps(proto))
            pipe.lset(f"{PROTONAMES_NS}{job_id}", index, proto.trial.path_name)
            pipe.execute()
        return True
    except Exception as e:
        _logger.error(f"Error while validating trial protocol definition for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return False


def metrics_for_neural_unit(job_id: str, index: int) -> Optional[OmniplexUnit]:
    """
    Get the metrics for a neural unit identified during preprocessing of the session data ZIP archive for the specified
    commit job.

    This information is available ONLY during the "Review" phase of a commit job -- after pre-processing and before the
    actual database commit begins.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        index: The zero-based index of the neural unit requested. The index position reflects the order in which units
            were culled from the archive during preprocessing.

    Returns:
        The requested neural unit. Returns None if index invalid or the operation failed for whatever reason.
    """
    try:
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.lindex(f"{UNITMETRICS_NS}{job_id}", index)
            pipe.lindex(f"{UNITTYPES_NS}{job_id}", index)
            raw_unit, raw_type = pipe.execute()
        if (raw_unit is None) or (raw_type is None):
            return None
        unit: OmniplexUnit = pickle.loads(raw_unit)
        type_id: int = int(raw_type.decode('utf-8'))
        unit.neuron_type = None if type_id == -1 else type_id
        return unit
    except Exception as e:
        _logger.error(f"Error while retrieving neural unit metrics for commit job {job_id}: {str(e)}",
                      exc_info=True)
        return None


def set_unit_type(job_id: str, index: int, neuron_type: int) -> bool:
    """
    Update the neuron type ID assigned to one or all neural units identified during preprocessing of the session data
    archive for the specified commit job. This operation is available only during the review phase of the job, when the
    user interactively reviews and edits information required before the experiment session can be committed to the
    portal database.

    During preprocessing, all neural units are assigned to the "Unspecified" neuron type, if it exists in the portal
    database. During the review phase, the user can review the unit metrics and assign a more specific type to each
    unit, or leave it as "Unspecified". If the "Unspecified" type does not exist (which should never be the case), the
    user MUST assign a valid type to each unit.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
        index: The zero-based index of the neural unit requested. If -1, then the specified neuron type is applied
            to ALL identified units in the session.
        neuron_type: The neuron type ID. This should identify an existing entry in the database's NeuronType table,
            but it is not checked until the session is actually committed to the database.

    Returns:
        True if successful; False if the operation fails for whatever reason.
    """
    try:
        conn = get_config().redis_conn
        n = conn.llen(f"{UNITTYPES_NS}{job_id}")
        if n <= 0:
            raise Exception("Found no cached unit types")
        if index == -1:
            unit_types = [neuron_type] * n
            with conn.pipeline() as pipe:
                pipe.delete(f"{UNITTYPES_NS}{job_id}")
                pipe.rpush(f"{UNITTYPES_NS}{job_id}", *unit_types)
                pipe.execute()
        else:
            if (index < 0) or (index >= n):
                raise Exception("Invalid unit index position")
            conn.lset(f"{UNITTYPES_NS}{job_id}", index, neuron_type)
        return True
    except Exception as e:
        _logger.error(f"Error while updating neural unit type for commit job {job_id}, index={index}: {str(e)}",
                      exc_info=True)
        return False


def ready_to_commit(job_id: str) -> Tuple[bool, bool, str]:
    """
    Check whether or not the session data for an in-progress commit job is valid and ready to be committed to the portal
    database. This operation is only available during the review phase of the job, when the user interactively reviews
    and edits information required before the experiment is committed to the database.

    The method makes these checks in the order indicated: (1) Are all required session metadata attributes valid?
    (2) Do any trial protocol candidates require user validation? (3) Are any recorded neural units lacking a neuron
    type?  If all tests pass, the experiment session is ready to commit to the database.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        A 3-tuple (ok, ready, msg), where ok indicates whether or not the check was successful. If ok==False, an error
            occurred on the server and msg contains an error description. Otherwise, ready==False indicates that input
            from the user is required before the session can be committed and msg is a brief description of the first
            issue encountered during the check. If ready==True, then the session is ready to commit, and msg will
            contain a user-facing message to that effect.
    """
    try:
        conn = get_config().redis_conn
        status_raw = conn.get(f"{STATUS_NS}{job_id}")
        if status_raw is None:
            return False, False, "Did not find commit job status on server!"
        job_status = pickle.loads(status_raw)
        if job_status.state != CommitStateEnum.REVIEW:
            return False, False, "Cannot check session data readiness for a commit job not in the 'Review' stage."

        has_units = job_status.units and (job_status.units > 0)
        with conn.pipeline() as pipe:
            pipe.get(f"{INFO_NS}{job_id}")
            pipe.lrange(f"{PROTONAMES_NS}{job_id}", 0, -1)
            if has_units:
                pipe.lrange(f"{UNITTYPES_NS}{job_id}", 0, -1)
            res = pipe.execute()
            if len(res) != (3 if has_units else 2):
                raise Exception(f"Got {len(res)} responses from Redis pipe; expected {3 if has_units else 2}")
            if any([(r is None) for r in res]):
                raise Exception(f"Missing Redis response data from pipe")
        info = pickle.loads(res[0])
        msg = check_row(DBTable.SESSION, info.session_table_entry())
        if (msg is None) and job_status.units and (job_status.units > 0):
            msg = check_row(DBTable.SESSION_EPHYS, info.ephys_table_entry(), omit_master=True)
        if msg:
            return True, False, msg
        proto_names = [raw.decode('utf-8') for raw in res[1]]
        n = [s.startswith('**') for s in proto_names].count(True)
        if n > 0:
            return True, False, f"{n} trial protocols (marked with '**') require manual validation."
        if has_units:
            type_ids = [int(t.decode('utf-8')) for t in res[2]]  # have to convert from byte strings!!!!
            n = type_ids.count(-1)
            if n > 0:
                return True, False, f"{n} neural units are missing a neuron type identification."

            # let user know if some units have the "Unspecified" neuron type (if it exists in database)
            res = fetch_rows(DBTable.NEURON_TYPE, dict(nt_name="Unspecified"))
            if res and (len(res) == 1):
                n = type_ids.count(res[0]['nt_id'])
                if n > 0:
                    return True, True, f"\u2713 OK. Ready to commit, but {n} units have 'Unspecified' neuron type."
        return True, True, "\u2713 OK. Ready to commit."
    except Exception as e:
        _logger.error(f"Error while checking if commit job {job_id} is ready to commit: {str(e)}", exc_info=True)
        return False, False, "A server error occurred while checking cached session data."


def commit_to_database(job_id: str) -> Optional[str]:
    """
    Request that the experiment data for a pending session commit job be committed to the database. This is the final
    phase of the session commit workflow.

    The specified commit job must currently be in the review stage, with no missing metadata (the user supplies this
    information interactively during the review stage). If these requirements are met, the server transitions the job to
    the final "Commit" phase and queues a background task to perform that work.

    Args:
        job_id:  The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        Returns None if successful. If the job is not in the review phase or is not ready to commit, or if a server
            error occurs, returns a brief error description.
    """
    ok, ready, msg = ready_to_commit(job_id)
    if not (ok and ready):
        return f"Session not ready to be committed to database: {msg}"

    try:
        status_key = f"{STATUS_NS}{job_id}"
        progress_key = f"{PROGRESS_NS}{job_id}"
        # get job status dictionary
        conn = get_config().redis_conn
        job = conn.get(status_key)
        if job is None:
            _logger.debug(f"Got request to finalize a commit job (id={job_id}) that does not exist.")
            return f"Commit job (id={job_id}) not found on server."
        job_status: CommitJobStatus = pickle.loads(job)
        if job_status.state != CommitStateEnum.REVIEW:
            _logger.debug(f"Got request to finalize a commit job (id={job_id}) that is not in the review phase.")
            return f"Commit job must be in the 'Review' stage before committing to database"

        now = time.time()
        update_msg = "Queueing job to commit experiment session to the database."
        job_status.state = CommitStateEnum.COMMIT
        job_status.updated = now
        job_status.msg = update_msg
        with conn.pipeline() as pipe:
            pipe.set(status_key, pickle.dumps(job_status))
            pipe.zadd(progress_key, {update_msg: now})
            pipe.zremrangebyrank(progress_key, 0, -(PROGRESS_HISTORY_SIZE + 1))
            pipe.execute()

        job_queue.enqueue(finish_commit_job, job_id, job_id=f"{job_id}-commit", job_timeout='60m')
        return None
    except Exception as e:
        _logger.error(f"Error while transitioning commit job {job_id} to commit phase: {str(e)}", exc_info=True)
        return "An error occurred while checking or updating commit job status on server"


def finish_commit_job(job_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the Dash/Flask backend server, performs
    the final stage of the session commit workflow, in which the session data is pushed to the portal database.

    Committing a preprocessed experiment session to the database involves the following steps:

        0) Verify that the user has supplied all required information during the interactive review (session metadata,
        all trial protocols validated, etc).

        1) Entries are inserted into the Session, Session.EPhys, and Session.Neuron tables as appropriate, and all
        trial protocols not already in the database are inserted into the TrialProtocol table.

        2) The Trial table and its part tables are populated with data from all the trials presented during the
        session. We post a progress message and check for user cancel periodically during this process.

        3) A pickle file, "preproc.pickle", is generated that contains the preprocessing results, along with session and
        electrophysiology metadata entered manually by the user during the review stage. The pickle file is appended to
        the original session archive ZIP. As a result, the ZIP file contains everything needed to recommit the
        experiment session -- without user intervention -- in the event the portal database was corrupted and had to be
        reconstructed from scratch.

        4) The altered ZIP file is uploaded to the portal's backing repository, which is maintained in an AWS S3 bucket
        provisioned by the lab expressly for this purpose. The object key under which the ZIP file is stored uniquely
        identifies the experiment session: "/repo/<experimenter>/<subj_id>_<session_date>_<session_sfx>.zip", where
        <experimenter> is the portal username of the experimenter, <subj_id> is the experiment subject's ID,
        <session_date> is the experiment date as an ISO-formatted string 'YYYY-MM-DD', and <session_sfx> is the integer
        session suffix.

        4) Lastly, the completed session commit is recorded in the database operations log. This single log entry (along
        with the ZIP file just stored in the backing repository) accounts for all of the database insertions required to
        commit the data from the experiment session.

    We rely on the database server's transaction mechanisms to ensure data consistency; all insertions into the database
    are encapsulated in a transaction. If an error occurs at any point during the commit, any changes to the database
    and the file repository are unwound before returning.

    Committing a large (>1GB) session to the database can take many minutes, so progress messages are delivered
    periodically to the commit job's Redis-cached progress history. The method also checks the job's status regularly in
    case the user cancels the job through the backend.

    If the operation is cancelled or fails at any point, the job is moved to the "Failed" state before returning. On
    successful completion, the job is moved to the "Done" stage.

    Args:
        job_id: The commit job identifier, assigned when the session commit was initiated on server.
    Returns:
        True if successful, in which case the session is fully committed to the database; False otherwise.
    """
    _logger.debug(f"Started final commit phase for commit job {job_id}")

    zip_path: Path
    """ Location of session archive in staging directory. """
    preproc_path: Path
    """ Location of temporary file in staging directory holding results from preprocessing phase. """
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
    """ The list of trial protocols culled from the session data archive during pre-processing. """
    session_info: SessionMetaData
    """
    Metadata about session that is partially initialized during preprocessing phase, then reviewed and updated by user
    before committing the session to the database. It includes attributes from the Session and Session.EPhys tables.
    """

    try:
        # verify job status, existence of ZIP archive and preprocessing results file in staging directory
        job_status = commit_job_status(job_id)
        if isinstance(job_status, str):
            _logger.error(f"Failed to retrieve job status from Redis for {job_id}")
            return False
        elif job_status.state != CommitStateEnum.COMMIT:
            _logger.error(f"Commit job is not in the final commit phase: {job_status.state}")
            return False
        else:
            zip_path = Path(_get_subfolder_in_staging_directory(job_id), job_status.zip)
            preproc_path = Path(_get_subfolder_in_staging_directory(job_id), PREPROC_FNAME)
        if not zip_path.is_file():
            msg_pfx = f"Cannot find archive file for commit job {job_id}: "
            _logger.debug(f"{msg_pfx}: {str(zip_path)}")
            _background_job_update(job_id, f"{msg_pfx}: {zip_path.name}", CommitStateEnum.FAIL)
            return False
        if not preproc_path.is_file():
            msg_pfx = f"Cannot find temporary file with preprocessed data for commit job {job_id}: "
            _logger.debug(f"{msg_pfx}: {str(preproc_path)}")
            _background_job_update(job_id, f"{msg_pfx}: {preproc_path.name}", CommitStateEnum.FAIL)
            return False

        # retrieve Redis-cached information "filled in" by user during review phase, and verify nothing is missing.
        num_units = job_status.units if job_status.units else 0
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.get(f"{INFO_NS}{job_id}")
            pipe.lrange(f"{PROTODEFS_NS}{job_id}", 0, -1)
            if num_units > 0:
                pipe.lrange(f"{UNITTYPES_NS}{job_id}", 0, -1)
            res = pipe.execute()
        session_info = pickle.loads(res[0])
        msg = check_row(DBTable.SESSION, session_info.session_table_entry())
        if (msg is None) and num_units > 0:
            msg = check_row(DBTable.SESSION_EPHYS, session_info.ephys_table_entry(), omit_master=True)
        if msg:
            msg = f"Error: Session metadata incomplete: {msg}"
            _logger.debug(f"Commit job {job_id} failed: {msg}")
            _background_job_update(job_id, msg, CommitStateEnum.FAIL)
            return False
        protocols = [pickle.loads(proto_raw) for proto_raw in res[1]]
        for p in protocols:
            if p.is_candidate:
                msg = f"Error: At least one trial protocol ({p.trial.path_name} still requires user validation!"
                _logger.debug(f"Commit job {job_id} failed: {msg}")
                _background_job_update(job_id, msg, CommitStateEnum.FAIL)
                return False
        unit_types: List[int] = [raw.decode('utf-8') for raw in res[2]] if num_units > 0 else list()
        if len(unit_types) != num_units:
            msg = f"Error: Number of cached units inconsistent with job status info!"
            _logger.debug(f"Commit job {job_id} failed: {msg}")
            _background_job_update(job_id, msg, CommitStateEnum.FAIL)
            return False
        if unit_types.count(-1) > 0:   # in Redis key, an undefined neuron type is specified as -1
            msg = f"Error: The neuron type is undefined for at least one neural unit!"
            _logger.debug(f"Commit job {job_id} failed: {msg}")
            _background_job_update(job_id, msg, CommitStateEnum.FAIL)
            return False

        # load preprocessing results from temporary file in staging directory. We only need the trial info and the
        # units. We don't need trial info nor the (potentially huge) unit spike time arrays during the review phase,
        # so these weren't cached in Redis and so must be recovered from the temporary file. Set neuron type for each
        # unit IAW cached neuron type list that may have been altered during review phase.
        with open(preproc_path, 'rb') as file:
            res = pickle.load(file)
            trial_info = res['trials']
            units = res['units']
            if len(units) != num_units:
                msg = f"Error: Number of preprocessed units inconsistent with job status info!"
                _logger.debug(f"Commit job {job_id} failed: {msg}")
                _background_job_update(job_id, msg, CommitStateEnum.FAIL)
                return False
            for i, u in enumerate(units):
                u.neuron_type = unit_types[i]

        # update the individual trial info to include the corresponding protocol's unique MD5 hexadecimal digest
        for _, t_info in trial_info.items():
            protocol = protocols[t_info.proto_index]
            t_info.proto_hash = protocol.md5_digest
    except Exception as err:
        error_msg = f"Error occurred before starting commit: {str(err)}"
        _logger.error(error_msg)
        try:
            _background_job_update(job_id, error_msg, CommitStateEnum.FAIL)
        except Exception:
            pass
        return False

    # here's where it all happens: the database inserts, rollback on failure, progress messages and check for
    # cancellation.
    commit_mgr = _SessionCommitMgr(job_id, zip_path, session_info, trial_info, protocols, units)
    error_msg = commit_mgr.commit()
    if error_msg and not commit_mgr.was_cancelled():
        try:
            _background_job_update(job_id, error_msg, CommitStateEnum.FAIL)
        except Exception:
            pass
        return False
    added_proto_hashes = [p['proto_hash'] for p in commit_mgr.trial_protocols()]

    # at this point, the session has been committed to the database. Now we need rewrite the preprocessing pickle file
    # to include the information supplied during the review stage, and append it to the ZIP archive. Then we upload the
    # amended ZIP archive to the portal backing repository. If any of those operations fail, we have to remove the
    # session from the database!
    key = f"/repo/{session_info.experimenter}/" \
          f"{session_info.subj_id}_{str(session_info.session_date)}_{session_info.session_suffix}.zip"
    archive_uploaded, commit_logged = False, False
    try:
        if _background_job_update(job_id, "Adding pre-processing results to session archive..."):
            raise Exception("Operation cancelled")

        results = {'protocols': protocols, 'trials': trial_info, 'units': units,
                   'session': session_info.session_table_entry(),
                   'ephys': None if len(units) <= 0 else session_info.ephys_table_entry()}
        with open(preproc_path, 'wb') as file:
            pickle.dump(results, file)
        with zipfile.ZipFile(zip_path, 'a') as f:
            f.write(preproc_path, PREPROC_FNAME)

        if _background_job_update(job_id, "Uploading session archive to portal repository..."):
            raise Exception("Operation cancelled")
        if not repo.upload_file(zip_path, key):
            raise Exception(f"Unable to push committed session archive [{key}] to portal repository")
        archive_uploaded = True

        # finally, log the session commit
        res = log_session_commit(session_info.experimenter, session_info.subj_id, str(session_info.session_date),
                                 session_info.session_suffix)
        if res:
            raise Exception(res)
        commit_logged = True

        _background_job_update(job_id, "Done!", CommitStateEnum.DONE)
    except Exception as e:
        # if the exception occurs AFTER we've logged the session commit, don't rollback. Technically, everything is
        # OK with the database and repository -- something went wrong with Redis at the worst possible time!
        if commit_logged:
            _logger.critical("Exception occurred after session successfully committed. Check Redis server.")
            return True
        _logger.error(f"Session commit {job_id} failed in final phase, after database insertions: {str(e)}")
        # rollback the session commit, including any added trial protocols.
        ok = True
        err_msg = rollback_session_commit(session_info.session_table_entry(), added_proto_hashes)
        if err_msg:
            _logger.critical(f"Session commit rollback failed: {str(e)}")
            ok = False
        if archive_uploaded:
            if not repo.delete_file(key):
                _logger.critical(f"Failed to remove session archive from repository {key} during commit rollback")
                ok = False
        err_msg = f"Commit failed after database insertions; rollback {'successful' if ok else 'FAILED!'}"
        try:
            _background_job_update(job_id, err_msg, CommitStateEnum.FAIL)
        except Exception:
            pass
        return False

    return True


class _SessionCommitMgr(SessionCommitter):
    """
    Helper class that performs the actual database table insertions that commit an experiment session to the portal
    database. It is used in two contexts: (1) during a new commit managed by a background worker process initiated
    through the Dash backend server; or (2) during reconstruction of the database contents from the database update log
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
    def __init__(self, job_id: Optional[str], zip_path: Path, session_info: SessionMetaData,
                 trial_info: Dict[str, _TrialInfo], protocols: List[maestro.Protocol], units: List[OmniplexUnit]):
        """
        Construct the experiment session data commit manager.

        Args:
            job_id: If this is a normal session commit initiated by a user via the backend server, then this is ID of
                the associated commit job. In the context of a commit job, progress messages are posted to the relevant
                Redis key, and cancellation is possible. If the ID is None, then the commit is part of a database
                reconstruction task. In this case, progress messages are written to STDOUT, and the operation cannot be
                cancelled.
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
        self.session_info: SessionMetaData = session_info
        """ Session metadata that must be written to the database. """
        self.trial_info: Dict[str, _TrialInfo] = trial_info
        """ Dictionary of trial information objects, keyed by trial data filenames. """
        self.protocols: List[maestro.Protocol] = protocols
        """ List of all trial protocols presented during the experiment session."""
        self.new_protocol_entries: Optional[List[Dict[str, AttributeValue]]] = None
        """ List of all new protocol entries that must be added to database, lazily created. """
        self.units: List[OmniplexUnit] = units
        """ List of all neural units recorded during experiment. Will be empty list for a behavioral session. """
        self.cancelled: bool = False
        """ Flag set if a cancel request detected. """

    def session_table_entry(self) -> Dict[str, AttributeValue]:
        return self.session_info.session_table_entry()

    def ephys_table_entry(self) -> Optional[Dict[str, AttributeValue]]:
        return self.session_info.ephys_table_entry() if self.session_info.num_units > 0 else None

    def neurons(self) -> List[Dict[str, AttributeValue]]:
        neuron_entries: List[Dict[str, AttributeValue]] = list()
        for i, unit in enumerate(self.units):
            neuron = dict()
            neuron['experimenter'] = self.session_info.experimenter
            neuron['subj_id'] = self.session_info.subj_id
            neuron['session_date'] = self.session_info.session_date
            neuron['session_sfx'] = self.session_info.session_suffix
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
                    protocol_entry['proto_def'] = pickle.dumps(p)
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
                if t_info.omniplex_start is not None:
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
                    trial_header=pickle.dumps(data_file.header),
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
                    t_sec = t_info.omniplex_start if (
                                sort_strategy == 'omniplex') else t_info.header_timestamp / 1000.0
                    if num_inserted == 0:
                        trial_entry['trial_ts'] = 0
                        trial1_start_sec = t_sec
                    else:
                        trial_entry['trial_ts'] = t_sec - trial1_start_sec

                # for this trial, get the values of the protocol's random variables
                protocol = next((x for x in self.protocols if x.md5_digest == t_info.proto_hash), None)
                if not protocol:
                    raise Exception(
                        f"Internal inconsistency: No trial protocol defined for trial in {trial_filename}")
                rv_values: List[Any] = list()
                for param in protocol.random_variables:
                    rv_value = data_file.trial.retrieve_segment_table_parameter_value(param)
                    if not rv_value:
                        raise Exception(
                            f"Internal inconsistency: Invalid RV ({param}) for trial in {trial_filename}")
                    rv_values.append(rv_value)
                trial_entry['trial_rvs'] = pickle.dumps(rv_values)

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

                # report progress and check for cancel roughly once every 5 seconds
                if (time.time() - t0) > 5:
                    self.update_progress(f"{num_inserted} of {num_trials} trials added to database...")
                    t0 = time.time()


def reconstruct_database() -> None:
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
    dirctory, and that archive is then "digested" to re-commit the experiment's data. The archive includes a pickle file
    with the original results of pre-processing, so the re-commit is much faster than the original commit. The slowest
    part is likely to be downloading the archive from S3.

    NOTE: THIS IS AN ADMINISTRATIVE FUNCTION FOR USE ONLY WHEN THE PORTAL APPLICATION IS DOWN. It must be run in a
    python console script. During reconstruction, progress messages are written to STDOUT. Very little user intervention
    is required, as the operations log and the portal backing repository store everything that is needed to repopulate
    the database. There is one exception, however: User passwords are, for security reasons, NEVER included in the
    database operations log entries. Therefore, in order to process a log entry that registers a new user on the portal,
    the function will prompt for an initial password for that user's account.

    NOTE2: When a registered user changes their password, the User table in the database is updated accordingly, but
    the operation is NOT logged. So the database operations history does not preserve user password changes. See also
    change_portal_user_password() in user_ops.py.
    """
    # ensure database update log exists and verify that database is empty
    log_path: Path = log_file_path()
    if not log_path.is_file():
        print(f"=====> ERROR: No database log file found at {str(log_file_path)}\n", file=sys.stdout, flush=True)
        return
    err_msg = database_empty()
    if err_msg:
        print(f"=====> ERROR: {err_msg}. Database must be empty prior to reconstruction!\n", file=sys.stdout,
              flush=True)
        return

    print(f"Starting database reconstruction from repository using log file at {str(log_file_path)}...",
          file=sys.stdout, flush=True)

    try:
        num_entries = 0
        with open(log_path, 'rb') as file:
            while True:
                try:
                    entry = pickle.load(file)
                    num_entries += 1
                    print(f"Processing log entry #{num_entries}: \n    {entry}", file=sys.stdout)
                    if entry['op'] == 'add':
                        # SPECIAL CASE: When adding a user account, we must prompt for an initial password
                        if entry['table'] == DBTable.USER:
                            print(f" *** You must specify a valid initial password for each user added to database...",
                                  file=sys.stdout, flush=True)
                            password = prompt_for_password(entry['row']['username'])
                            if password is None:
                                raise Exception(f"Password not supplied for {entry['row']['username']}. Aborting.")
                            entry['row']['password'] = generate_password_hash(password, method=PASSWORD_HASH_METHOD)
                        err_msg = insert_into_table(entry['table'], entry['row'], log=False)
                    elif entry['op'] == 'delete':
                        err_msg = delete_from_table(entry['table'], entry['restriction'], log=False)
                    elif entry['op'] == 'update':
                        err_msg = update_table_row(entry['table'], entry['row'], log=False)
                    elif entry['op'] == 'mapping':
                        err_msg = update_mapping_table(entry['table'], entry['src_pk'], entry['dst_pks'], log=False)
                    elif entry['op'] == 'session':
                        err_msg = _reconstruct_session(entry)
                    else:
                        err_msg = f"Invalid log entry!"

                    if err_msg is not None:
                        raise Exception(err_msg)
                except EOFError:
                    break
    except Exception as e:
        print(f"=====> ERROR: Exception while reconstructing portal database: {str(e)}", file=sys.stdout, flush=True)
        print("Manual reconstruction of database content required. Consult this progress log to assist in that"
              "reconstruction.", file=sys.stdout, flush=True)
        return

    print("Reconstruction completed successfully!", file=sys.stdout, flush=True)


def _reconstruct_session(log_entry: Dict[str, Union[str, int]]) -> Optional[str]:
    """
    Helper method for reconstruct_database().

    Commit an experiment session during scripted reconstruction of the portal database content from entries in the
    database operations log file and experiment data archives stored in the backing repository.

    For each experiment session, the session archive is stored in the portal's backing repository in AWS S3 under the
    object key "/repo/<experimenter>/<subj_id>_<session_date>_<session_sfx>.zip, where <experimenter> is the registered
    username of the experimenter, <subj_id> is the experiment subject's ID, <session_date> is the date of the experiment
    in the format 'YYYY-MM-DD', and <session_sfx> is the integer session suffix.

    During the original commit, all pre-processing results -- as well as any information entered manually via user
    interaction -- are stored in the pickle file "preproc.pickle", which in turn is appended to the session archive ZIP.
    As a result, re-committing the session requires no user intervention and is significantly faster because it does not
    require processing of a large PL2 file (which also would have to be extracted from the ZIP file). However, the
    archive ZIP must be downloaded from the repository to a staging directory in the portal workspace before it is
    processed, which could take a while.

    Progress messages are written to STDOUT.

    Args:
        log_entry: A database log entry for a session commit. This dictionary must have the form {'op': 'session',
            'username': str, 'subj_id': str, 'date': 'YYYY-MM-DD', 'suffix': int}. See description above.
    Returns:
        An error description if session commit fails; else None
    """
    key = f"/repo/{log_entry['username']}/" \
          f"{log_entry['subj_id']}_{str(log_entry['date'])}_{log_entry['suffix']}.zip"
    recon_dir = _get_subfolder_in_staging_directory("reconstruct")
    error_msg = None
    try:
        # create a temporary folder in the staging directory on the portal server
        recon_dir.mkdir(parents=True, exist_ok=False)
        zip_path = Path(recon_dir, f"{log_entry['subj_id']}_{str(log_entry['date'])}_{log_entry['suffix']}.zip")
        print(f"  > Downloading session archive from repository at {key}...", file=sys.stdout, flush=True)
        if not repo.download_file(key, zip_path, log=False):
            raise Exception("Failed while downloading session archive from repository.")

        # load pre-processing results from pickle file in session archive
        print(f"  > Loading preprocessed results stored in session archive...")
        preproc_path = Path(recon_dir, PREPROC_FNAME)
        with zipfile.ZipFile(zip_path, 'r') as archive:
            archive.extract(PREPROC_FNAME, path=str(recon_dir.absolute()))
        if not preproc_path.is_file():
            raise Exception("Failed to extract pre-processing results file from session archive")
        with open(preproc_path, 'rb') as file:
            results = pickle.load(file)
        if not (isinstance(results, dict) or
                all([(k in results) for k in ['protocols', 'trials', 'units', 'session', 'ephys']])):
            raise Exception("Invalid or incomplete pre-processing results file!")
        protocols: List[maestro.Protocol] = results['protocols']
        trial_info: Dict[str, _TrialInfo] = results['trials']
        units: Optional[List[OmniplexUnit]] = results['units']
        session_info: Dict[str, Optional[AttributeValue]] = results['session']
        ephys_info: Optional[Dict[str, Optional[AttributeValue]]] = results['ephys']

        metadata = SessionMetaData(experimenter=session_info['experimenter'], subj_id=session_info['subj_id'],
                                   session_date=session_info['session_date'],
                                   session_suffix=session_info['session_sfx'], rig_id=session_info['rig_id'],
                                   study_id=session_info['study_id'], session_notes=session_info['session_notes'],
                                   num_units=len(units) if units else 0, num_trials=session_info['num_trials'])
        if ephys_info:
            metadata.ephys_src = ephys_info['ephys_src']
            metadata.probe_type = ephys_info['probe_type']
            metadata.sampling_rate = ephys_info['sampling_rate']
            metadata.probe_x = ephys_info['probe_x']
            metadata.probe_y = ephys_info['probe_y']
            metadata.probe_depth = ephys_info['probe_depth']
            metadata.ba_id = ephys_info['ba_id']

        # here's where it all happens: the database inserts, rollback on failure, progress messages and check for
        # cancellation.
        session_label = f"{metadata.experimenter}-{metadata.subj_id}-{str(metadata.session_date)}-" \
                        f"{metadata.session_suffix}"
        print(f"   > Reconstructing session [{session_label}] in database...", file=sys.stdout)
        commit_mgr = _SessionCommitMgr(None, zip_path, metadata, trial_info, protocols, units)
        error_msg = commit_mgr.commit()
        if error_msg:
            return error_msg
        print("   > Session was successfully committed to database.", file=sys.stdout, flush=True)
    except Exception as err:
        error_msg = f"Exception while reconstructing experiment session:\n  {str(err)}"
    finally:
        # dispose of the temporary directory in which the archive and pickle files were stored during reconstruction
        try:
            shutil.rmtree(recon_dir)
        except Exception as e:
            print(f"   > Warning - An exception occured while removing temporary "
                  f"directory {str(recon_dir)}:\n   {str(e)}", file=sys.stdout, flush=True)
    return error_msg
