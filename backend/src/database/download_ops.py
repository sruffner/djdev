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
      process, not in the Dash/Flask backend. The file is stored in a temporary location in the portal's backup
      repository.
    - Back on the frontend, the logged-in user can monitor the progress of the pending download. Once the data file is
      ready, the user can initiate the actural download. Download requests may fail for whatever reason. They also
      expire after a set period of time; upon expiration, the data file is removed permanently from the backup
      repository, and the request is marked as "expired".

To safeguard data provenance, it is important to maintain a record of all *FULFILLED* data download requests. For this
reason, each completed download is logged to a dedicated table in the portal's MySQL/MariaDB database -- recording info
on what was downloaded and by whom. However, while the download request is being prepared in the background and before
the client receives the download URL, information about the download request is cached on the Redis server under the
following keys.
    - DOWNLOAD_KEY : Redis LIST of all currently pending donwload requests. Each element is a string "<usr>-<req_id>",
      where <usr> is the portal username of the request originator, while <req_id> is the download request ID,
      a 32-char hex string.
    - DOWNLOAD_INFO_NS:<req_id> : A STRING key holding a description of the request <req_id>.
    - DOWNLOAD_STATUS_NS:<req_id> : A STRING key holding status information for the request <req_id>.

The Redis server does double-duty, since we use Redis Queue (RQ) workers to handle the work of preparing the data file
and storing it in the backup repository.

Data downloads are restricted to registered users with the appropriate access level. Calls to this module should be
protected by a mechanism that verifies the specified user is logged in with the access level required.

@author: sruffner
@created: 17feb2022
"""
import logging
import pickle
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import scipy.io
from rq import Queue
from config.config import get_config
from config.logging import setup_logging
from database.repo import upload_file_to_bucket, presigned_url_for_file
from database.table_info import AttributeValue, DBTable, primary_key_of
from database.table_ops import row_exists, fetch_attribute_values, fetch_one_row, insert_into_table
from database.trial_data_ops import TrialData, retrieve_trial_block

logger = logging.getLogger(__name__)


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
parameters for a particular request. The STRING key is a pickled DownloadRequest object and does not change once it is
created.
"""
_DOWNLOAD_STATUS_NS: str = 'downloadstatus:'
"""
Redis key namespace for status information on pending data download requests. Append the request ID to access status
information for a particular request. The STRING key is a pickled DownloadRequestStatus object.
"""

DOWNLOAD_PREPPING: int = 0
DOWNLOAD_READY: int = 1
DOWNLOAD_FAIL: int = 2
_DOWNLOAD_STATES: List[str] = ['Preparing data file', 'Data file ready to download', 'Failed']
""" The possible states of a pending download request. """

_DOWNLOAD_SUBFOLDER = 'downloads'
""" Serves as name of subfolder in backend respository where data files are prepared for download."""


@dataclass()
class DownloadRequest:
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


@dataclass()
class DownloadRequestStatus:
    state: int
    """ The state of the pending download request (index into _DOWNLOAD_STATES). """
    msg: str
    """ The most recent status/progress message posted. On failure, this is a brief error description. """
    pct_complete: int
    """ Completion percentage for background task fulfilling the download request (0 to 100). """
    updated: float
    """ Timestamp (seconds since the Epoch) when the request's status was last updated. """


def get_data_downloads_directory() -> Path:
    """ Construct file system path where data download files are temporarily stored in the backend repository. """
    return Path(get_config().repo_root, _DOWNLOAD_SUBFOLDER)


def get_data_download_file_path(req_info: DownloadRequest) -> Path:
    """ Construct file system path where the data file for a download request is stored in the backend repository. """
    return Path(get_config().repo_root, _DOWNLOAD_SUBFOLDER,
                f"{req_info.requester}-{req_info.id}.{req_info.output_fmt}")


def request_data_download(
        requester: str, session: Dict[str, AttributeValue], unit_ids: List[int],  output_fmt: str = 'npz',
        remove_sacc: bool = False, complete_reps: bool = False) -> Tuple[bool, str]:
    """
    Submit a request to download trial-aligned behavioral and neural response data recorded during the specified
    experiment session. The method generates a unique request ID and queues a background task to fulfill the request.
    The client must supply the request ID in all future queries involving the download request.

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
        logger.error(emsg, exc_info=True)
        return False, emsg

    req_id = uuid.uuid4().hex
    now = time.time()
    req_info = DownloadRequest(
        id=req_id, requester=requester, session_key=session_pk, selected_units=clean_unit_ids,
        remove_saccades=remove_sacc, complete_reps=complete_reps,
        output_fmt=output_fmt if (output_fmt in DOWNLOAD_FORMATS.keys()) else 'npz', requested=now)
    req_status = DownloadRequestStatus(
        state=DOWNLOAD_PREPPING, msg="Queueing download request to a background process", pct_complete=0, updated=now)

    try:
        info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
        status_key = f"{_DOWNLOAD_STATUS_NS}{req_id}"
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.lpush(_DOWNLOAD_KEY, f"{requester}-{req_id}")
            pipe.set(info_key, pickle.dumps(req_info))
            pipe.set(status_key, pickle.dumps(req_status))
            pipe.execute()

        job_queue.enqueue(fulfill_pending_download_request, req_id, job_id=f"download-{req_id}", job_timeout='60m')

        return True, req_id
    except Exception as e:
        logger.error(f"Error while submitting a new download request: {str(e)}", exc_info=True)
        return False, "An internal error occurred on server while submitting the download request"


def pending_download_request_status(req_id: str) -> Optional[DownloadRequestStatus]:
    """
    Get the current status of a pending data download request.

    Args:
        req_id: The download request identifier.
    Returns:
        The download request's current status, or None if pending download request not found on server.
    """
    try:
        status_key = f"{_DOWNLOAD_STATUS_NS}{req_id}"
        conn = get_config().redis_conn
        status_blob = conn.get(status_key)
        if status_blob is None:
            logger.debug(f"Pending download request ID={req_id} not found on Redis server")
            return None
        req_status: DownloadRequestStatus = pickle.loads(status_blob)
        return req_status
    except Exception as e:
        logger.error(f"Error retrieve pending download request status: {str(e)}", exc_info=True)
        return None


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
        status_key = f"{_DOWNLOAD_STATUS_NS}{req_id}"
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.lrem(_DOWNLOAD_KEY, 0, f"{requester}-{req_id}")
            pipe.delete(info_key, status_key)
            pipe.execute()
        return True
    except Exception as e:
        logger.error(f"Error cancelling a pending download request {requester}-{req_id}: {str(e)}", exc_info=True)
        return False


def fulfill_pending_download_request(req_id: str) -> bool:
    """
    This method, intended to be called on a background process independent from the Dash/Flask backend server,
    prepares a data download archive to fulfill the specified download request.

    Fulfilling a typical download request involves retrieving trial-aligned behavioral and neuronal response data for
    all or a subset of the trials presented and recorded during an experiment session. The response data is then
    packaged, along with some supporting metadata into a data file in one of 3 supported formats -- a Python pickle
    file, a Numpy multi-array file, or a Matlab MAT file. That file is then compressed into a standard ZIP archive for
    download and stored in the portal's respository at /downloads/<req_id>.zip, where <req_id> is the unique identifier
    assigned to the original download request.

    Depending on the length and number of trials, it could take a minute or more to prepare the download ZIP, so
    progress is updated regularly in the _DOWNLOAD_STATUS_NS<req_id> key. The request status has 3 possible states -
    'in progress', 'ready for download', and 'failed'.

    Oncd submitted, a download request cannot be cancelled, but it can be deleted. This method will abort if it
    detects that the request it's working on has been removed from Redis.

    Args:
        req_id: The download request identifier, assigned when the request was initially submitted to the backend.
    Returns:
        True if the download request is successfully fulfilled; False otherwise.
    """
    # since this method is called in an RQ work horse process, logging has not been configured. So we do it here.
    setup_logging(cfg_file='../config/logging.yaml')

    logger.debug(f"Generating data file for pending download request {req_id}")
    try:
        conn = get_config().redis_conn
        info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
        raw = conn.get(info_key)
        if raw is None:
            raise Exception("No pending download request found")
        req_info: DownloadRequest = pickle.loads(raw)
        session_info = fetch_one_row(DBTable.SESSION, req_info.session_key)
        if session_info is None:
            raise Exception("Session not found, or database error while retrieving session metadata")

        idx_start = 1
        n_trials = session_info['num_trials']
        trial_data: List[TrialData] = list()
        while idx_start < n_trials:
            idx_end = int(min(n_trials - idx_start + 1, 50)) + idx_start - 1
            block = retrieve_trial_block(
                req_info.session_key, idx_start, idx_end, unit_ids=req_info.selected_units,
                completed_only=req_info.complete_reps, remove_saccades=req_info.remove_saccades, include_fixtgts=True
            )
            if block is None:
                raise Exception(f"Failed to retrive trial block between indices {idx_start} and {idx_end}")
            trial_data.extend(block)
            idx_start = idx_end + 1
            if _request_status_update(req_id, f"Retrieved response data for {idx_start-1} of {n_trials} trials",
                                      int(50 * (idx_start - 1) / n_trials)):
                return False

        # make sure the downloads/ folder exists in the repository root
        downloads_dir = get_data_downloads_directory()
        if not downloads_dir.is_dir():
            logger.debug("Creating downloads/ folder in backend repository")
            downloads_dir.mkdir(parents=True, exist_ok=False)

        # write data file
        file_path = get_data_download_file_path(req_info)
        if _request_status_update(req_id, f"Writing trial data to {file_path.name}. This will take a while...", 55):
            return False
        _save_trial_data_to_file(file_path, trial_data)

        if _request_status_update(req_id, f"Pushing {file_path.name} to temporary storage", 90):
            file_path.unlink(missing_ok=True)
            return False

        # ... then upload it to temporary storage in S3 (it will be auto-deleted after 1 dqy)
        if not upload_file_to_bucket(file_path, get_config().repo_bucket, f"/{_DOWNLOAD_SUBFOLDER}/{file_path.name}"):
            raise Exception("An error occurred while uploading data file to S3 bucket")

        # remove the data file from local storage -- we're done with it.
        file_path.unlink(missing_ok=True)

        if _request_status_update(req_id, f"DONE!", 100, DOWNLOAD_READY):
            return False
        logger.debug(f"Successfully generated data file for download request {req_id}")
        return True
    except Exception as err:
        error_msg = f"ERROR while preparing download archive for request {req_id}: {str(err)}"
        logger.error(error_msg, exc_info=True)
        _request_status_update(req_id, error_msg, 100, DOWNLOAD_FAIL)
        return False


def _save_trial_data_to_file(file_path: Path, trial_data: List[TrialData]) -> None:
    """
    Helper method that saves trial response data to a Matlab MAT file or a Numpy NPZ file.

    Args:
        file_path: The target file. The file extension indicates the output format requested.
        trial_data: The collected trial response data. The list is emptied as it is consumed, since it could eat up
            significant memory depending on the total number of trials, units, and trial durations.
    Raises:
        Exception: If an error occurs while writing the file or preparing the data for the output format requested.
    """
    trials = list()
    while len(trial_data) > 0:
        td = trial_data.pop(0)
        curr_trial = dict(
            index=td.trial_idx,
            protocol_name=td.protocol.trial.path_name(),
            duration_ms=td.duration_ms,
            record_start_ms=td.record_start_ms,
            success=td.success,
            timestamp_sec=td.timestamp_sec,
            hgpos=td.behavior['HEPOS'] if 'HEPOS' in td.behavior else np.array([], dtype=np.float32),
            vepos=td.behavior['VEPOS'] if 'VEPOS' in td.behavior else np.array([], dtype=np.float32),
            hevel=td.behavior['HEVEL'] if 'HEVEL' in td.behavior else np.array([], dtype=np.float32),
            vevel=td.behavior['VEVEL'] if 'VEVEL' in td.behavior else np.array([], dtype=np.float32),
            fix1_hpos=np.array([], dtype=np.float32) if (td.fix1_pos is None) else td.fix1_pos[:, 0],
            fix1_vpos=np.array([], dtype=np.float32) if (td.fix1_pos is None) else td.fix1_pos[:, 1],
            fix2_hpos=np.array([], dtype=np.float32) if (td.fix2_pos is None) else td.fix2_pos[:, 0],
            fix2_vpos=np.array([], dtype=np.float32) if (td.fix2_pos is None) else td.fix2_pos[:, 1]
        )
        for unit_id, spiketimes in td.neuronal.items():
            curr_trial[f"unit_{unit_id}"] = np.nan if (spiketimes is None) else spiketimes
        trials.append(curr_trial)

    if file_path.name.endswith('mat'):
        scipy.io.savemat(file_path, dict(trials=np.array(trials, dtype=object)))
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
        np.savez(str(file_path), **trials_dict)
    else:
        raise Exception(f'Unsupported output format: {file_path.name}')


def _request_status_update(req_id: str, msg: str, pct: int, next_state: Optional[int] = None) -> bool:
    """
    Helper method used to update progress and, optionally, the state of a pending download request. Intended for use
    ONLY within the background worker that prepares the data file requested for download. While the request cannot be
    cancelled, it can be removed while the download file is being prepared.

    Args:
        req_id: The download request identifier, assigned when the request was initially submitted to the backend.
        msg: The new status/progress message to post. If job failed, this should be an error description.
        pct: Estimated task progress as 'percent completed', to nearest 1%.
        next_state: If not None, transition the job to this state. Default is None.

    Returns:
        True if the download request no longer exists, in which case an ongoing background task to fulfill the request
            will abort. False otherwise.
    Raises:
        Exception: If an error occurs while reading or writing download request status cache in Redis.
    """
    status_key = f"{_DOWNLOAD_STATUS_NS}{req_id}"

    conn = get_config().redis_conn
    status_blob = conn.get(status_key)
    if status_blob is None:
        return True
    req_status: DownloadRequestStatus = pickle.loads(status_blob)
    if isinstance(next_state, int):
        req_status.state = next_state
    req_status.msg = msg
    req_status.pct_complete = int(min(max(0, pct), 100))
    req_status.updated = time.time()

    # TODO: Issue - The key could disappear between the previous read and this write.
    conn.set(status_key, pickle.dumps(req_status))
    return False


def get_data_download_url(requester: str, req_id: str) -> Tuple[bool, str]:
    """
    Get the presigned URL by which a data file -- previously prepared in response to a data download request -- can be
    downloaded from the portal's backend repository.

    Once a data download request is fulfilled, the prepared data file is available for download from the backend
    repository, implemented in an AWS S3 bucket. By design, the data file will "expire" (ie, it is deleted permanently)
    approximately 24 hours after it is uploaded to S3. Since all files in the S3 bucket are private, a presigned URL
    must be supplied to download any given file.

    Only one presigned URL will be supplied per download request. The URL should be accessed immediately, as it is set
    to expire in one hour. After preparing the URL, this method removes the completed download request from the Redis
    cache and stores a permanent record of the download in the portal database as a data provenance measure.

    Args:
        requester: The username of the registered portal user that originally requested the download.
        req_id: The download request identifier.
    Returns:
        A 3-tuple: (False, error message) if an error occurs; (True, url-string) otherwise.
    """
    clear = False  # if set, clear the download request from Redis cache
    info_key = f"{_DOWNLOAD_INFO_NS}{req_id}"
    status_key = f"{_DOWNLOAD_STATUS_NS}{req_id}"
    try:
        # get download request info and status from Redis
        conn = get_config().redis_conn
        with conn.pipeline() as pipe:
            pipe.get(info_key)
            pipe.get(status_key)
            res = pipe.execute()
        req_info: Optional[DownloadRequest] = None if res[0] is None else pickle.loads(res[0])
        req_status: Optional[DownloadRequestStatus] = None if res[1] is None else pickle.loads(res[1])
        if (req_info is None) or (req_status is None):
            clear = True
            return False, f"Download request {req_id} not found on server."

        # verify requester and check that file has not expired.
        if req_info.requester != requester:
            return False, f"You did not request download {req_id}. Permission denied."
        elif time.time() - req_info.requested > 24 * 3600:
            clear = True
            return False, f"The prepared data file has expired and is no longer available for download."

        # generate presigned URL
        file_key = f"/{_DOWNLOAD_SUBFOLDER}/{get_data_download_file_path(req_info).name}"
        ok, url = presigned_url_for_file(get_config().repo_bucket, file_key)
        if not ok:
            return False, url

        # push a record of the completed download into the portal database. If this fails, do not consider it
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
            logger.error(f"Failed to record completed data download in database: {err_msg}")

        clear = True
        return True, url
    except Exception as e:
        logger.error(f"Failed to get download URL for request {req_id}: {str(e)}", exc_info=True)
        return False, f"Internal error while trying to generate URL for data file download."
    finally:
        if clear:
            try:
                conn = get_config().redis_conn
                with conn.pipeline() as pipe:
                    pipe.lrem(_DOWNLOAD_KEY, 0, f"{requester}-{req_id}")
                    pipe.delete(info_key, status_key)
                    pipe.execute()
            except Exception:
                pass
