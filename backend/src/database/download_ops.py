"""
download_ops.py: Operations involved in downloading experimental data from the Lisberger lab portal database.

Authenticated users with download access can submit requests to download trial-aligned behavioral and neuronal
response data recorded during a particular experiment session. Preparing a file with the requested data can take a
significant amount of time and must be offloaded to a background process so that the portal backend remains responsive
to client requests.

As with the experiment session commit procedure, submitting and fulfilling a download request is an inherently
stateful workflow. The workflow has the following stages:
    - Request submittal. An authenticated user can submit a data download request on the portal's main landing page.
      The submittal identifies the experiment session, up to 5 recorded neural units to include with the behavioral
      response data, the format of the download file, and a few other specifics. See explore.py.
    - Generation of the download file. The per-trial response data, along with some descriptive metadata, are retrieved
      from the portal database and written to the data file IAW the download request. This happens in a background
      process, not in the Dash/Flask backend. The file is then uploaded to the portal's backup repository on S3 and a
      presigned URL generated so that the requester can later download the file directly from S3. At this point, the
      download request is considered to be "fulfilled", and the final task of the background job is to log the request
      in a dedicated table in the portal's MySQL/MariaDB database -- recording information on what data was requested
      and by whom. The intent here is to safeguard data provenance by maintaing a record of who is downloading data.
    - Back on the frontend, the logged-in user can monitor the progress of the pending download. Once the data file is
      ready, the frontend client enables a link button tied to the presigned URL; the user clicks on the button to
      iniitate the actual download.

The data file prepared in response to a download request is removed from the S3 repository after 24 hours, and the
presigned URL expires after only 1 hour. Hence the frontend UI should be designed to start the actual download shortly
after the background process has fulfilled the request.

While the download request is being prepared in the background, information about the download request is cached on the
Redis server under the following keys.
    - DOWNLOAD_KEY : Redis LIST of all currently pending donwload requests. Each element is a string "<usr>-<req_id>",
      where <usr> is the portal username of the request originator, while <req_id> is the download request ID,
      a 32-char hex string.
    - DOWNLOAD_INFO_NS:<req_id> : A STRING key holding a description and status information for the request <req_id>.

The Redis server does double-duty, since we use Redis Queue (RQ) workers to handle the work of preparing the data file
and storing it in the backup repository.

Data downloads are restricted to registered users with the appropriate access level. Calls to this module should be
protected by a mechanism that verifies the specified user is logged in with the access level required.

@author: sruffner
@created: 17feb2022
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import scipy.io
from rq import Queue

from config.app_logging import get_application_logger
from config.config import get_config
from database import repo
from database.table_info import AttributeValue, DBTable, primary_key_of
from database.table_ops import row_exists, fetch_attribute_values, fetch_one_row, insert_into_table
from database.trial_data_ops import retrieve_session_trial_reps
from sglportalapi.data_containers import TrialRep

job_queue = Queue(connection=get_config().redis_conn)
""" Background jobs queue. """

MAX_UNITS_PER_DOWNLOAD: int = 5
""" Maximum number of neural units the responses of which can be included in a single data download request. """
DOWNLOAD_FORMATS: Dict[str, str] = dict(npz='Numpy file (.NPZ)', mat='Matlab MAT file (.MAT)')
""" Supported experiment data download file formats. """

_DOWNLOAD_KEY: str = 'download'
"""
Redis key for the LIST of all pending data download requests. Each element represents one request and is a string of
the form '<usr>-<req_id>', where 'usr' is the username of the authenticated user that made the request and 'req-id' is
the request ID (a 32-bit hex string).
"""
_DOWNLOAD_INFO_NS: str = 'downloadinfo:'
"""
Redis key namespace for cached information on pending data download requests. Append the request ID to access defining
parameters for a particular request. The STRING key is a serialized _DownloadRequest object.
"""

DOWNLOAD_PREPPING: int = 0
DOWNLOAD_READY: int = 1
DOWNLOAD_FAIL: int = 2
_DOWNLOAD_STATES: List[str] = ['Preparing data file', 'Data file ready to download', 'Failed']
""" The possible states of a pending download request. """

_DOWNLOAD_SUBFOLDER = 'downloads'
""" Serves as name of subfolder in backend respository where data files are prepared for download."""


@dataclass()
class _DownloadRequest:
    """
    Parameters and status information for a pending or recently fulfilled data download request.
    """
    id: str
    """ The request ID, a 32-character hex string representing the randomly generated UUID of the request. """
    requester: str
    """ The username of the authenticated portal user that made the request. """
    session_key: Dict[str, AttributeValue]
    """ The primary key identifying the experiment session from which data is to be downloaded. """
    selected_units: List[int]
    """ Unit IDs for neural units to include in response data downloaded. If empty, only behavioral data included. """
    remove_saccades: bool
    """ If True, eye velocity trajectories are corrected for DC offset and saccade epochs replaced with NaNs. """
    complete_reps: bool
    """ If True, only response data from successfully completed trial reps are included in the download. """
    output_fmt: str
    """ Desired format for the response data file downloaded: 'npz', or 'mat'. """
    requested: float
    """ Timestamp (seconds since the Epoch) when download request was submitted. """
    state: int
    """ The state of the pending download request (index into _DOWNLOAD_STATES). """
    msg: str
    """ The most recent status/progress message posted. On failure, this is a brief error description. """
    pct_complete: int
    """ Completion percentage for background task fulfilling the download request (0 to 100). """
    download_url: str
    """ Presigned URL to download prepared data file from S3. Will be an empty string until download is ready. """

    def to_bytes(self) -> bytes:
        """ Serialize this object. """
        fields = [self.id, self.requester, self.session_key['experimenter'], self.session_key['subj_id'],
                  self.session_key['session_date'].isoformat(), self.session_key['session_sfx'], self.selected_units,
                  self.remove_saccades, self.complete_reps, self.output_fmt, self.requested, self.state, self.msg,
                  self.pct_complete, self.download_url]
        return json.dumps(fields).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> _DownloadRequest:
        """ Reconstruct a _DownloadRequest object from a byte sequence generated by `to_bytes()`."""
        v = json.loads(raw.decode())
        session_key = dict(experimenter=v[2], subj_id=v[3], session_date=date.fromisoformat(v[4]), session_sfx=v[5])
        return _DownloadRequest(id=v[0], requester=v[1], session_key=session_key, selected_units=v[6],
                                remove_saccades=v[7], complete_reps=v[8], output_fmt=v[9], requested=v[10],
                                state=v[11], msg=v[12], pct_complete=v[13], download_url=v[14])


class DownloadRequestStatus:
    """
    Status of a pending data download request. Once the data file is generated and ready for download, it includes a
    URL to initiate the download.
    """
    def __init__(self, state: int, msg: str, pct_complete: int, download_url: str):
        self._state = state
        self._msg = msg
        self._pct_complete = pct_complete
        self._download_url = download_url

    @property
    def state(self) -> int:
        """ The state of the pending download request (one of DOWNLOAD_PREPPING, _READY, or _FAIL). """
        return self._state

    @property
    def message(self) -> str:
        """ The most recent status/progress message posted. On failure, this is a brief error description. """
        return self._msg

    @property
    def pct_complete(self) -> int:
        """ Completion percentage for background task fulfilling the download request (0 to 100). """
        return self._pct_complete

    @property
    def presigned_url(self) -> str:
        """ URL to download prepared data file from portal. Will be an empty string until download is ready. """
        return self._download_url


def _get_data_downloads_directory() -> Path:
    """ Construct file system path where data download files are temporarily stored in the portal's workspace. """
    return Path(get_config().workspace_dir, _DOWNLOAD_SUBFOLDER)


def _get_data_download_file_path(req_info: _DownloadRequest) -> Path:
    """ Construct path where the data file for a download request is temporarily stored in portal's workspace. """
    return Path(get_config().workspace_dir, _DOWNLOAD_SUBFOLDER,
                f"{req_info.requester}-{req_info.id}.{req_info.output_fmt}")


def request_data_download(
        requester: str, session: Dict[str, AttributeValue], unit_ids: List[int],  output_fmt: str = 'npz',
        remove_sacc: bool = False, complete_reps: bool = False) -> Tuple[bool, str]:
    """
    Submit a request to download trial-aligned behavioral and neural response data recorded during the specified
    experiment session. The method generates a unique request ID and queues a background task to fulfill the request.
    The client must supply the request ID and requester's username in all future queries involving the download request.

    Args:
        requester: The username of the registered portal user requesting the download. The method only verifies that the
            user exists in the portal database; it is ASSUMED that the specified user is currently logged-in and has the
            necessary privileges to download experiment data from the portal.
        session: A dictionary containing, at a minimum, the primary key for a particular experiment session in the
            portal database.
        unit_ids: A list of up to 5 neural unit IDs identifying the neurons the recorded responses of which should be
            included in the download. If None or empty, the download only contains behavioral response data. Any extra
            entries beyond the first 5 are ignored.
        output_fmt: Format of the data download file - either 'npz' (for a multi-array Numpy file), or 'mat' (for a
            Matlab MAT  file). Default = 'npz'.
        remove_sacc: If False, the raw recorded eye velocity trajectories are supplied; else, the trajectories are
            corrected for DC offset and saccade epochs within the response are replaced w/ NaNs. Default = False.
        complete_reps: If False, response data from all recorded trials are included in the data file; else, only
            successfully completed trial reps are included. Default = False.
    Returns:
        A 2-tuple -- either (True, req_ID) or (False, error_description) -- where req_id is the unique identifier
            assigned to the pending download request. Use this to check progress while request is fulfilled in a
            background process.
    """
    unique_ids = {i for i in unit_ids[0:MAX_UNITS_PER_DOWNLOAD]} if isinstance(unit_ids, list) else set()
    clean_unit_ids = sorted([i for i in unique_ids])
    session_pk: Dict[str, AttributeValue]
    try:
        if not row_exists(DBTable.USER, dict(username=requester)):
            raise ValueError('User invalid or not found in database. Make sure you are logged into the portal.')
        if not row_exists(DBTable.SESSION, session):
            raise ValueError("Specified experiment session not found in database.")
        session_pk = {k: session[k] for k in primary_key_of(DBTable.SESSION)}
        valid_units = fetch_attribute_values(DBTable.SESSION_NEURON, 'unit_id', session_pk)
        for i in clean_unit_ids:
            if not (i in valid_units):
                raise ValueError(f"No recorded neural unit with ID={i}")
    except Exception as e:
        emsg = f"Download request submission failed: {str(e)}"
        get_application_logger().error(emsg, exc_info=True)
        return False, emsg

    req_id = uuid.uuid4().hex
    now = time.time()
    req_info = _DownloadRequest(
        id=req_id, requester=requester, session_key=session_pk, selected_units=clean_unit_ids,
        remove_saccades=remove_sacc, complete_reps=complete_reps,
        output_fmt=output_fmt if (output_fmt in DOWNLOAD_FORMATS.keys()) else 'npz', requested=now,
        state=DOWNLOAD_PREPPING, msg="Queueing download request to a background process", pct_complete=0,
        download_url="")

    try:
        info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.lpush(_DOWNLOAD_KEY, f"{requester}-{req_id}")
            pipe.set(info_key, req_info.to_bytes())
            pipe.execute()

        job_queue.enqueue(fulfill_pending_download_request, req_id, job_id=f"download-{req_id}", job_timeout='60m')
        return True, req_id
    except Exception as e:
        get_application_logger().error(f"Error while submitting a new download request: {str(e)}", exc_info=True)
        return False, "An internal error occurred on server while submitting the download request"


def pending_download_request_status(requester: str, req_id: str) -> Optional[DownloadRequestStatus]:
    """
    Get the current status of a pending data download request.

    NOTE: The first time this method is called AFTER the data file is ready for download, the download request is
    removed from the portal's set of pending requests and the returned status information includes the URL at which the
    prepared data file can be downloaded.

    Args:
        requester: The username of the registered portal user that originally requested the download.
        req_id: The download request identifier.
    Returns:
        The download request's current status, or None if pending download request not found on server, or if the
            requester username does not match that of the user that originated the download request.
    """
    clear = False
    info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
    try:

        conn = get_config().redis_conn
        info_blob = conn.get(info_key)
        if info_blob is None:
            get_application_logger().debug(f"Pending download request ID={req_id} not found on Redis server")
            return None
        req_info: _DownloadRequest = _DownloadRequest.from_bytes(info_blob)
        if requester != req_info.requester:
            get_application_logger().debug(f"Requester does not match original requester!")
            return None
        clear = (req_info.state == DOWNLOAD_READY)
        return DownloadRequestStatus(state=req_info.state, msg=req_info.msg, pct_complete=req_info.pct_complete,
                                     download_url=req_info.download_url)
    except Exception as e:
        get_application_logger().error(f"Error retrieve pending download request status: {str(e)}", exc_info=True)
        return None
    finally:
        if clear:
            try:
                conn = get_config().redis_conn
                with conn.pipeline() as pipe:
                    pipe.lrem(_DOWNLOAD_KEY, 0, f"{requester}-{req_id}")
                    pipe.delete(info_key)
                    pipe.execute()
            except Exception:
                pass


def cancel_pending_download_request(requester: str, req_id: str) -> bool:
    """
    Cancel and remove a pending data download request.

    Args:
        requester: The username of the registered portal user that originally requested the download.
        req_id: The download request identifier.
    Returns:
        True if successful, False if pending download request not found on server, or an error occurred.
    """
    try:
        info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.lrem(_DOWNLOAD_KEY, 0, f"{requester}-{req_id}")
            pipe.delete(info_key)
            pipe.execute()
        return True
    except Exception as e:
        get_application_logger().error(f"Error cancelling a pending download request {requester}-{req_id}: {str(e)}",
                                       exc_info=True)
        return False


def fulfill_pending_download_request(req_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the Dash/Flask backend server,
    prepares a data download archive to fulfill the specified download request.

    Fulfilling a typical download request involves retrieving trial-aligned behavioral and neuronal response data for
    all or a subset of the trials presented and recorded during an experiment session. The response data is then
    packaged, along with some supporting metadata into a data file in one of 2 supported formats -- a Numpy multi-array
    file (.npz) or a Matlab file (.mat). That file is stored in the portal's respository at /downloads/<req_id>.<ext>,
    where <req_id> is the unique identifier assigned to the original download request.

    Depending on the length and number of trials, it could take a minute or more to prepare the download. The file is
    then uploaded to S3 and a presigned URL generated so that the client can download the generated file directly from
    S3. Finally, the download request is logged in a dedicated download history in the portal database, in order to
    track data provenance.

    The download request info/status object is updated periodically in the _DOWNLOAD_INFO_NS<req_id> key. The request
    status has 3 possible states: DOWNLOAD_PREPPING, DOWNLOAD_READY, and DOWNLOAD_FAIL.

    Once submitted, a download request can be cancelled by simply removing the corresponding info/status object from
    Redis. This method will abort if it detects that the request it's working no longer exists in Redis.

    Args:
        req_id: The download request identifier, assigned when the request was initially submitted to the backend.
    Returns:
        True if the download request is successfully fulfilled; False otherwise.
    """

    get_application_logger().debug(f"Generating data file for pending download request {req_id}")
    tmp_file_path: Optional[Path] = None
    req_info: Optional[_DownloadRequest] = None
    info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
    try:
        conn = get_config().redis_conn
        raw = conn.get(info_key)
        if raw is None:
            raise Exception("No pending download request found")
        req_info = _DownloadRequest.from_bytes(raw)
        session_info = fetch_one_row(DBTable.SESSION, req_info.session_key)
        if session_info is None:
            raise Exception("Session not found, or database error while retrieving session metadata")

        idx_start = 1
        n_trials = session_info['num_trials']
        trial_reps: List[TrialRep] = list()
        while idx_start < n_trials:
            idx_end = int(min(n_trials - idx_start + 1, 50)) + idx_start - 1
            block = retrieve_session_trial_reps(req_info.session_key, start=idx_start, end=idx_end,
                                                unit_ids=req_info.selected_units, completed=req_info.complete_reps)
            if isinstance(block, str):
                raise Exception(f"Failed to retrive trial block between indices {idx_start} and {idx_end} [{block}]")
            trial_reps.extend(block)
            idx_start = idx_end + 1

            # check for cancel and update progress
            if conn.get(info_key) is None:
                return False
            req_info.msg = f"Retrieved response data for {idx_start-1} of {n_trials} trials"
            req_info.pct_complete = int(50 * (idx_start - 1) / n_trials)
            conn.set(info_key, req_info.to_bytes())

        # make sure the downloads/ folder exists in the portal workspace
        downloads_dir = _get_data_downloads_directory()
        if not downloads_dir.is_dir():
            get_application_logger().debug("Creating downloads/ folder in portal workspace")
            downloads_dir.mkdir(parents=True, exist_ok=False)

        # check for cancel, then update progress and start writing data to file
        tmp_file_path = _get_data_download_file_path(req_info)
        if conn.get(info_key) is None:
            return False
        req_info.msg = f"Writing trial data to {tmp_file_path.name}. This will take a while..."
        req_info.pct_complete = 55
        conn.set(info_key, req_info.to_bytes())
        _save_trial_data_to_file(tmp_file_path, trial_reps, req_info.remove_saccades)

        # check for cancel, update progress, and upload data file to S3 repo (it will be auto-deleted after 1 day)
        if conn.get(info_key) is None:
            return False
        req_info.msg = f"Pushing {tmp_file_path.name} to portal repository"
        req_info.pct_complete = 90
        conn.set(info_key, req_info.to_bytes())
        if not repo.upload_file(tmp_file_path, f"/{_DOWNLOAD_SUBFOLDER}/{tmp_file_path.name}"):
            raise Exception("An error occurred while uploading data file to portal repository")

        # check for cancel and update progress
        if conn.get(info_key) is None:
            return False
        req_info.msg = f"Finishing up..."
        req_info.pct_complete = 99
        conn.set(info_key, req_info.to_bytes())

        # get presigned URL
        file_key = f"/{_DOWNLOAD_SUBFOLDER}/{_get_data_download_file_path(req_info).name}"
        url = repo.download_url_for(file_key)
        if url is None:
            raise Exception("Unable to generate download URL for the data file")

        # push a record of the fulfilled download request into the portal database. If this fails, do not consider it
        # catastrophic, but log the issue
        download_entry = dict(
            request_id=req_info.id, requester=req_info.requester, experimenter=req_info.session_key['experimenter'],
            subj_id=req_info.session_key['subj_id'], session_date=req_info.session_key['session_date'],
            session_sfx=req_info.session_key['session_sfx'], complete_reps=req_info.complete_reps,
            remove_sacc=req_info.remove_saccades, out_format=req_info.output_fmt,
            selected_units=" ".join([str(i) for i in sorted(req_info.selected_units)]),
            downloaded=datetime.now().isoformat(sep=' ', timespec='seconds')
        )
        err_msg = insert_into_table(DBTable.DATA_DOWNLOAD, download_entry)
        if err_msg is not None:
            get_application_logger().error(f"Failed to record completed data download in database: {err_msg}")

        req_info.state = DOWNLOAD_READY
        req_info.msg = "DONE!"
        req_info.pct_complete = 100
        req_info.download_url = url
        conn.set(info_key, req_info.to_bytes())

        get_application_logger().debug(f"Successfully generated data file for download request {req_id}")
        return True
    except Exception as err:
        error_msg = f"ERROR while preparing download file for request {req_id}: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
        try:
            req_info.state = DOWNLOAD_FAIL
            req_info.msg = error_msg
            req_info.pct_complete = 100
            get_config().redis_conn.set(info_key, req_info.to_bytes())
        except Exception:
            pass
        return False
    finally:
        # remove the data file from local storage if it's there.
        if isinstance(tmp_file_path, Path):
            tmp_file_path.unlink(missing_ok=True)


_DOWNLOAD_FILE_CONTENT_INFO: str = \
    "This data download file contains trial response data from a Maestro experiment session. In the NPZ file \r\n" \
    "format, each trial's data is stored in a separate structured array labeled 'trial_N', where N is the trial \r\n" \
    "index. In the MAT file format, the individual trial records are stored in a single Matlab cell array \r\n" \
    "called 'trials'. Regardless the file format, here are the fields available in a single trial record: \r\n" \
    "   index: The trial index (integer) \r\n" \
    "   protocol_name: The trial protcol name (string) \r\n" \
    "   duration_ms: The recorded trial duration in milliseconds (integer) \r\n" \
    "   record_start_ms: Start time of recording, relative to start of trial, in milliseconds (integer) \r\n" \
    "   timestamp_sec: Trial start time relative to start of experiment session, in seconds (float) \r\n" \
    "   hgpos: 1KHz-sampled horizontal eye position trajectory in degrees (float array) \r\n" \
    "   vepos: 1KHz-sampled vertical eye position trajectory in degrees (float array) \r\n" \
    "   hevel: 1KHz-sampled horizontal eye velocity trajectory in deg/sec (float array) \r\n" \
    "   hgpos: 1KHz-sampled vertical eye velocity trajectory in deg/sec (float array) \r\n" \
    "   fix1_hpos, _vpos: 1KHz-computed position trajectory of fixation target #1 in deg (float array) \r\n" \
    "   fix2_hpos, _vpos: Analogously for fixation target #2 \r\n" \
    "If a behavioral response was not recorded or a fixation target not designated, the corresponding array \r\n" \
    "will be empty. In addition, for each neural unit 'unit_M' requested, the trial record includes a field: \r\n" \
    "   unit_M: The spike occurrence times for unit M during the trial, in seconds since trial start (float \r\n" \
    "           array. There will be one such field for each neural unit requested. If a unit was being \r\n" \
    "           recorded during the trial but no spikes occurred, this field is set to NaN. \r\n"


def _save_trial_data_to_file(file_path: Path, trial_reps: List[TrialRep], remove_saccades: bool) -> None:
    """
    Helper method that saves trial response data to a Matlab MAT file or a Numpy NPZ file.

    Args:
        file_path: The target file. The file extension indicates the output format requested.
        trial_reps: The collected trial response data. The list is emptied as it is consumed, since it could eat up
            significant memory depending on the total number of trials, units, and trial durations.
        remove_saccades: If True, the horizontal and vertical eye velocity traces are modified: any baseline offset is
            removed, and detected saccade epochs are replaced with NaN.
    Raises:
        Exception: If an error occurs while writing the file or preparing the data for the output format requested.
    """
    trials = list()
    while len(trial_reps) > 0:
        rep = trial_reps.pop(0)
        if remove_saccades:
            hevel, vevel = rep.eye_velocity_saccades_removed()
        else:
            hevel, vevel = rep.hevel, rep.vevel
        curr_trial = dict(
            index=rep.index,
            protocol_name=rep.protocol.trial.path_name,
            duration_ms=rep.duration,
            record_start_ms=rep.record_start,
            success=rep.success,
            timestamp_sec=rep.timestamp,
            hgpos=rep.hgpos if isinstance(rep.hgpos, np.ndarray) else np.array([], dtype=np.float32),
            vepos=rep.vepos if isinstance(rep.vepos, np.ndarray) else np.array([], dtype=np.float32),
            hevel=hevel if isinstance(rep.hevel, np.ndarray) else np.array([], dtype=np.float32),
            vevel=vevel if isinstance(rep.vevel, np.ndarray) else np.array([], dtype=np.float32),
            fix1_hpos=rep.fix1_pos[:, 0],
            fix1_vpos=rep.fix1_pos[:, 1],
            fix2_hpos=rep.fix2_pos[:, 0],
            fix2_vpos=rep.fix2_pos[:, 1]
        )
        for unit_id, spiketimes in rep.spike_trains.items():
            curr_trial[f"unit_{unit_id}"] = np.nan if (spiketimes is None) else spiketimes
        trials.append(curr_trial)

    if file_path.name.endswith('mat'):
        scipy.io.savemat(file_path, dict(trials=np.array(trials, dtype=object),
                                         contents=np.array(_DOWNLOAD_FILE_CONTENT_INFO, dtype=np.str_)))
    elif file_path.name.endswith('npz'):
        trials_dict: Dict[str, np.ndarray] = dict()
        while len(trials) > 0:
            t = trials.pop(0)
            dtype = [
                ('index', 'i4'), ('protocol_name', f'|U{len(t["protocol_name"])}'), ('duration_ms', 'i4'),
                ('record_start_ms', 'i4'), ('success', '?'), ('timestamp_sec', 'f4'),
                ('hgpos', 'f4', (len(t['hgpos']),)), ('vepos', 'f4', (len(t['vepos']),)),
                ('hevel', 'f4', (len(t['hevel']),)), ('vevel', 'f4', (len(t['vevel']),)),
                ('fix1_hpos', 'f4', (len(t['fix1_hpos']),)), ('fix1_vpos', 'f4', (len(t['fix1_vpos']),)),
                ('fix2_hpos', 'f4', (len(t['fix2_hpos']),)), ('fix2_vpos', 'f4', (len(t['fix2_vpos']),))
            ]
            data = [
                t['index'], t['protocol_name'], t['duration_ms'], t['record_start_ms'], t['success'],
                t['timestamp_sec'], t['hgpos'], t['vepos'], t['hevel'], t['vevel'],
                t['fix1_hpos'], t['fix1_vpos'], t['fix2_hpos'], t['fix2_vpos']
            ]
            unit_keys = [k for k in t.keys() if k.startswith('unit')]
            for k in unit_keys:
                dtype.append((k, 'f4') if isinstance(t[k], float) else (k, 'f4', (len(t[k]),)))
                data.append(t[k])
            trials_dict[f"trial_{t['index']}"] = np.array([tuple(data)], dtype=dtype)
        trials_dict["contents"] = np.array(_DOWNLOAD_FILE_CONTENT_INFO, dtype=np.str_)
        np.savez(str(file_path), **trials_dict)
    else:
        raise Exception(f'Unsupported output format: {file_path.name}')
