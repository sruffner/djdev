"""
session_builder.py: Handles the interactive process of committing an experiment session to the lab database.

The SessionBuilder object handles client requests in the multi-stage, user-interactive procedure that commits an
experiment session to the laboratory database and associated raw data repository.

It is designed as a singleton that is shared among all web app instances, as it manages a limited pool of worker threads
that handle long-running tasks in the commit procedure: extracting files from the uploaded session data ZIP archive,
processing the data files to collect the information that is stored in the database, and finally committing the trial
behavioral and neural data to the database. As a shared resource, it maintains no state that is specific to a particular
session commit in progress. Rather, per-build state information is maintained within a JSON file in the build's
staging directory on the server.

The build process will take an extended period of time, and the user could leave and return to the process for any
number of reasons. On the server side, uncommitted session data will be eventually expunged by a daemon. Thus, it is
imperative to synchronize the client and server state during the session commit process. To that end, a small dictionary
is maintained in client local storage to track the build status on the client end. This dictionary contains three
fields: (1) 'stage', an integer indicating the build stage (starting from 1); (2) 'experimenter' is the username of the
researcher that conducted the experiment; and (3) 'uuid' is a random universally unique identifier assigned to the
build, converted to a string in standard form. At the beginning of the commit process -- before any communication with
the server -- the client-side state is either an empty dictionary or has 'stage' = 1 (the other fields are irrelevant in
stage 1). To transition to stage 2, the client must provide valid information about the session, including the
experimenter's username. The username and a server-generated UUID are combined to create a "staging" directory --
$DJDEV_ROOT_REPO/staging/username-UUID -- to which the session data archive is uploaded and then processed.

The build status on the server end is maintained in the file build_state.json within the staging directory (of course,
that file won't exist until stage 2). This file will include the 3 abovementioned fields along with other information
needed to manage the build process.

    Stage 1: Session commit not started. Client must provide valid session information.
    Stage 2: Upload session data archive and extract trial protocols. At this point the session staging directory
        exists, and the client uploads all session data files compressed in a single ZIP archive. This "chunked" upload
        is handled by the dash-uploader component, independent of SessionBuilder. SessionBuilder launches a worker
        thread upon entering stage 2, and that thread monitors the staging directory for upload progress. On completion,
        the worker processes all Maestro trial data files in the archive (in place -- the archive is not decompressed)
        and generates a list of distinct trial protocols presented over the course of the experiment session. Progress
        status is maintained in the build_state.json file. If an error occurs at any point during or after the upload,
        the worker thread terminates, reporting the error in the build_state.json file. The main app thread performs any
        necessary cleanup in the staging directory and awaits a new archive upload.
            After upload, the client merely polls the server and updates the front end to indicate progress. If an error
        occurs, the client reports the error and gives the user the opportunity to upload the archive again. If the
        process completes successfully, client and server transition to stage 3.
    Stage 3: Review trial protocols. In this stage, the builder serves all trial protocols culled from the session
        archive to the client for user review. Client confirmation of the protocols transitions to stage 4 or 5.
    Stage 4: Review neuron data and enter parametric information about the electrophysiology recording. If the session
        included neural recordings, the client must supply some information that goes into the Session.EPhys part table,
        then review the neural spike train data extracted from the session data files. This stage is skipped if the
        experiment only included behavioral data. Client confirmation transitions the build to stage 5.
    Stage 5: Commit session to database. At this point, the session builder has everything it needs to commit the
        experiment session to the lab database and raw data repository. Upon receiving the command from the client,
        the commit procedure is started, and its status is maintained in the build_state.json file. The client will
        query the server at intervals to track progress. Upon completion (successful or otherwise), the session builder
        proceeds to step 6.
    Stage 6: Finish. At this point, either the session was successfully committed or the process has failed. Upon
        receiving acknowledgement from the client, the staging directory is removed from the data repository, and the
        client returns to stage 1.

In any of the stages 2-5, the client may issue a "start over" command -- in which case the session builder deletes the
staging directory from the data repository, and the client returns to stage 1.

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
name of the PL2 file in which the neural unit was recorded (this field must be present ONLY if the spike-sorted units
were derived from multiple PL2 files, all of which must be in the archive), and 'spiketimes' is a List[] where the N-th
element is a Numpy array containing the spike timestamps for that neural unit. The timestamps are single-precision
floats in seconds since the start of the Omniplex recording.

@author: sruffner
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import gc
import re
import threading
from queue import Queue
from typing import Optional, Dict, Any, Tuple, List, NamedTuple, IO
import os
import time
import shutil
from pathlib import Path
import json
import pickle
import uuid
import zipfile
import numpy as np
import scipy.signal
import database.table_views as tv
import database.maestro as maestro
import database.PL2 as PL2


class SessionBuilderError(Exception):
    def __init__(self, reason: Optional[str] = None):
        self.message = reason if reason else "Undefined error"

    def __str__(self):
        return self.message


class SessionBuilder(object):
    _singleton: Optional[SessionBuilder] = None
    _MAX_WORKERS: int = 8

    def __new__(cls):
        """
        Constructs the singleton SessionBuilder instance if it does not yet exist; else returns that singleton.
        """
        if cls._singleton is None:
            cls._singleton = super(SessionBuilder, cls).__new__(cls)
            cls._singleton.__init__()
        return cls._singleton

    def __init__(self):
        """ Initialize the SessionBuilder """
        if not hasattr(self, 'running_tasks'):
            self.running_tasks: Dict[str, ProcessArchiveThread] = dict()

    @staticmethod
    def sync_client_state(client_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Synchronize this session builder's state with the client's build state, if possible.

        The build state is a dictionary ['stage': int, 'experimenter': str, 'uuid': str]. (It may contain other fields,
        but only these fields matter in terms of synchronizing client and server. See the file header
        comments for a description of the 6 stages of the commit process. In stage 1, the experimenter and UUID are
        empty strings because the commit process has not begun. In all later stages, those fields must be specified,
        and the temporary staging directory for the commit will be located in '$REPO/staging/experimenter-uuid', where
        $REPO is the root directory for the raw data repository. Furthermore, the build state from the server's
        perspective will be maintained in the file build_state.json within the staging directory.

        Args:
            client_state (dict): This is the current state of a session build in progress, from client's perspective
            perspective. If the client is in stage 1, then the SessionBuilder is also in stage 1, and the method merely
            returns this argument. Otherwise, this method uses the information in the client state object to locate the
            staging directory and the build state file on the server. If found, then the server has an active commit
            session in progress with experimenter ID and session UUID as specified in the client state. In this case,
            the client state is returned, with the 'stage' corrected if necessary to match the server. If not, then
            the server and client must be in the initial stage 1.

        Returns:
            The client state, corrected to match the server, as described.
        """
        if not SessionBuilder._is_valid_build_state(client_state):
            client_state = {'stage': 1, 'experimenter': "", 'uuid': ""}
        if client_state['stage'] == 1:
            return client_state
        server_state = SessionBuilder._load_build_state(client_state)
        if not server_state:
            client_state = {'stage': 1, 'experimenter': "", 'uuid': ""}
        else:
            client_state['stage'] = server_state['stage']
        return client_state

    @staticmethod
    def _load_build_state(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        res = None
        try:
            state_file_path = SessionBuilder._get_staging_directory_for(state) / "build_state.json"
            with state_file_path.open(mode='rt') as f:
                res = json.load(f)
                if not SessionBuilder._is_valid_build_state(res):
                    res = None
        except Exception:
            pass
        return res

    @staticmethod
    def _write_build_state(state: Dict[str, Any]) -> Optional[str]:
        err_msg = None
        try:
            state_file_path = SessionBuilder._get_staging_directory_for(state) / "build_state.json"
            with state_file_path.open(mode='wt') as f:
                json.dump(state, f)
        except Exception as err:
            err_msg = f"Failed to update build state on server: {str(err)}"
        return err_msg

    @staticmethod
    def _is_valid_build_state(state: dict) -> bool:
        ok = False
        try:
            ok = isinstance(state, dict) and isinstance(state['stage'], int) and (1 <= state['stage'] <= 6)
            ok = ok and isinstance(state['experimenter'], str) and isinstance(state['uuid'], str)
            if ok and state['stage'] > 1:
                ok = (len(state['experimenter']) > 0) and (len(state['uuid']) > 0)
        except Exception:
            pass
        return ok

    @staticmethod
    def _get_staging_directory_for(state: dict) -> Path:
        return Path(os.environ['DJDEV_ROOT_REPO'], 'staging', f"{state['experimenter']}-{state['uuid']}")

    def stage1_enter_session_info(self, client_state: Dict[str, Any], session_info: Dict[str, Any]) -> Dict[str, Any]:
        if not SessionBuilder._is_valid_build_state(client_state):
            raise SessionBuilderError("Invalid client build state")
        if client_state['stage'] != 1:
            raise SessionBuilderError("Incorrect stage on client (must be stage 1)")

        err_msg = self._validate_session_info(session_info)
        if err_msg:
            raise SessionBuilderError(err_msg)

        state = dict()
        state['session_info'] = session_info
        state['stage'] = 2
        state['experimenter'] = session_info['experimenter']
        state['uuid'] = str(uuid.uuid4())
        staging_dir = SessionBuilder._get_staging_directory_for(state)
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
        except Exception as err:
            err_msg = f"Failed to create staging directory on server: {str(err)}"

        if not err_msg:
            err_msg = SessionBuilder._write_build_state(state)
        if not err_msg:
            err_msg = self._start_process_archive_task(staging_dir)
        if err_msg:
            self.delete_directory_tree(staging_dir)
            raise SessionBuilderError(err_msg)
        return state

    @staticmethod
    def _validate_session_info(session_info: Dict[str, Any]) -> Optional[str]:
        err_msg = tv.SessionView().check_row(session_info)
        return err_msg

    def _start_process_archive_task(self, staging_dir: Path) -> Optional[str]:
        if len(self.running_tasks) > SessionBuilder._MAX_WORKERS:
            return "Server is too busy; try again later"
        worker = ProcessArchiveThread(staging_dir)
        self.running_tasks[staging_dir.name] = worker
        worker.start()
        return None

    def stage2_progress_update(self, client_state: Dict[str, Any]) -> Tuple[Optional[bool], List[str], Dict[str, Any]]:
        if not SessionBuilder._is_valid_build_state(client_state):
            raise SessionBuilderError("Invalid client build state")
        if client_state['stage'] != 2:
            raise SessionBuilderError("Incorrect stage on client (must be stage 2)")
        server_state = SessionBuilder._load_build_state(client_state)
        if not server_state:
            raise SessionBuilderError("Staging directory not found; recommend starting over")
        if server_state['stage'] != 2:
            return None, ["Client out of sync with server"], server_state
        if 'cancelled' in server_state:
            return None, ["Session commit cancelled by user"], server_state
        staging_dir = SessionBuilder._get_staging_directory_for(server_state)
        session_worker = self.running_tasks[staging_dir.name]
        latest_messages = list()
        while session_worker.msg_q.qsize() > 0:
            latest_messages.append(session_worker.msg_q.get_nowait())

        if session_worker.is_alive():
            if (len(latest_messages) > 0) and latest_messages[-1].startswith("Error"):
                session_worker.join()
            else:
                return None, latest_messages, server_state

        # worker has terminated
        return session_worker.result, latest_messages, server_state

    def stage2_next(self, client_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # transition from stage 2 to stage 3 if stage 2 processing was completed successfully. Include the culled trial
        # protocols in the state object. Return to stage 1 if an error occurs while preparing this information. Do
        # nothing if stage 2 processing in progress or failed.
        if not SessionBuilder._is_valid_build_state(client_state):
            raise SessionBuilderError("Invalid client build state")
        if client_state['stage'] != 2:
            raise SessionBuilderError("Incorrect stage on client (must be stage 2)")
        server_state = SessionBuilder._load_build_state(client_state)
        if not server_state:
            raise SessionBuilderError("Staging directory not found; recommend starting over")
        if (server_state['stage'] != 2) or ('cancelled' in server_state):
            return None
        staging_dir = SessionBuilder._get_staging_directory_for(server_state)
        session_worker = self.running_tasks[staging_dir.name]
        if not session_worker.result:
            return None

        self.running_tasks.pop(staging_dir.name, None)
        server_state['stage'] = 3
        err_msg = SessionBuilder._write_build_state(server_state)
        if not err_msg:
            try:
                with open(Path(staging_dir, 'preprocessing.pickle'), 'rb') as file:
                    results = pickle.load(file)
                    server_state['protocols'] = \
                        {protocol.md5_digest: protocol.summary() for protocol in results['protocols']}
            except Exception as err:
                err_msg = str(err)
        return {'stage': 1, 'experimenter': '', 'uuid': ''} if err_msg else server_state

    @staticmethod
    def stage3_next(client_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # transition from stage 3 to stage 4, passing neural unit summaries through the returned state object
        if not SessionBuilder._is_valid_build_state(client_state):
            raise SessionBuilderError("Invalid client build state")
        if client_state['stage'] != 3:
            raise SessionBuilderError("Incorrect stage on client (must be stage 3)")
        server_state = SessionBuilder._load_build_state(client_state)
        if not server_state:
            raise SessionBuilderError("Staging directory not found; recommend starting over")
        if (server_state['stage'] != 3) or ('cancelled' in server_state):
            return None
        staging_dir = SessionBuilder._get_staging_directory_for(server_state)

        server_state['stage'] = 4
        err_msg = SessionBuilder._write_build_state(server_state)
        if not err_msg:
            try:
                with open(Path(staging_dir, 'preprocessing.pickle'), 'rb') as file:
                    results = pickle.load(file)
                    server_state['units'] = [unit.summary() for unit in results['units']]
            except Exception as err:
                err_msg = str(err)

        return {'stage': 1, 'experimenter': '', 'uuid': ''} if err_msg else server_state

    def cancel(self, client_state: Dict[str, Any]) -> None:
        if not SessionBuilder._is_valid_build_state(client_state):
            raise SessionBuilderError("Invalid client build state")
        if client_state['stage'] == 1:
            return
        server_state = SessionBuilder._load_build_state(client_state)
        if (not server_state) or ('cancelled' in server_state):
            return
        staging_dir = SessionBuilder._get_staging_directory_for(server_state)
        if not staging_dir.exists():
            return
        session_worker = self.running_tasks.pop(staging_dir.name, None)
        if session_worker and session_worker.is_alive():
            session_worker.cancel()
            server_state['cancelled'] = True
            SessionBuilder._write_build_state(server_state)
        else:
            SessionBuilder.delete_directory_tree(staging_dir)

    @staticmethod
    def delete_directory_tree(staging_dir: Path) -> None:
        try:
            if staging_dir.exists():
                shutil.rmtree(str(staging_dir))
        except OSError:
            # TODO: Need to log this error to an admin log so it can be addressed
            pass


class ProcessArchiveThread(threading.Thread):
    """
    This worker thread handles server-side processing during stage 2 of a session commit:
        1) Wait for upload of session archive ZIP to the staging directory, monitoring its progress once per second. If
        the upload does not start after 10 minutes of waiting, or the upload stalls for more than 10 minutes, report the
        error and terminate.

        2) Once the upload completes, process all Maestro data files in the session archive (in situ -- the files are
        NOT extracted from the ZIP file) and generate the list of distinct trial protocols presented over the course of
        the experiment session. If an error occurs, report the error and terminate.

        3) Load timing information for each trial. The primary purpose of this step is to process the strobed and event
        data in the Omniplex file(s) in the archive in order to align any neural unit responses with the individual
        trial timelines.

        4) Further process the archive for any neural unit data in the archive. The experimenter must provide their
        own spike sorting results in a single pickle file in the archive. See spikes.load_neural_data() for a
        description of the file contents. For now, we only support neural units recorded on the Omniplex system, and the
        archive must include the relevant PL2 file(s) for each unit specified in the pickle.

        5) Store the results as a dictionary {'protocols': ..., 'timings': ..., 'units': ...} in the pickle file
        'preprocessing.pickle' within the staging directory.

    To communicate progress to the main thread, the worker will post a message to the synchronous queue passed in the
    constructor. A message is posted whenever there's a significant progress transition (eg., upload started, N parts of
    archive uploaded, etc). It is incumbent on the thread that launched the worker to monitor this queue. If an error
    occurs, the error description is the last message posted to the queue, and that message starts with the string
    "Error".

    To cancel the session commit, call cancel(). This method sets a flag to inform the worker thread and returns
    immediately. The worker thread will stop its work in progress and remove the staging directory in its entirety --
    which could take a significant amount of time depending on the directory content at the time.
    """
    def __init__(self, staging_dir: Path):
        super(ProcessArchiveThread, self).__init__(name=f"ProcessArchive-{staging_dir.name}")
        self.staging_dir = staging_dir
        self.msg_q = Queue()
        self.result: Optional[bool] = None   # set to True/False to indicate success upon termination
        self.cancel_request = threading.Event()

    def run(self):
        cancelled = False
        self.msg_q.put_nowait("Awaiting upload...")

        # Steps 1 & 2: Wait for upload to begin, then monitor progress until ZIP file is present in staging directory.
        # Fail if upload does not start within 10 minutes or, once started, if it stalls for longer than 10 minutes.
        # NOTE: If upload is fast enough, the ZIP file could be present before even detecting that the upload started!
        t0 = time.time()
        upload_path: Optional[Path] = None
        zip_path: Optional[Path] = None
        n_parts_uploaded = 0
        while (not zip_path) and (not cancelled):
            time.sleep(1)
            if self.cancel_requested():
                cancelled = True
            elif not upload_path:
                if time.time() - t0 > 600:
                    self.msg_q.put_nowait("Error: Upload failed to start for more than 10 minutes")
                    self.result = False
                    return
                for child in self.staging_dir.iterdir():
                    if child.is_dir() and child.name.endswith('zip'):
                        upload_path = child
                        t0 = time.time()
                        self.msg_q.put_nowait("Upload started...")
                    elif child.is_file() and child.name.endswith('.zip'):
                        zip_path = child
            else:
                try:
                    n_chunks = len([f for f in upload_path.iterdir() if f.is_file()])
                    if n_chunks == n_parts_uploaded:
                        if time.time() - t0 > 600:
                            self.msg_q.put_nowait("Error: Upload has stalled for more than 10 minutes.")
                            self.result = False
                            return
                    else:
                        n_parts_uploaded = n_chunks
                        t0 = time.time()
                        self.msg_q.put_nowait(f"Uploading archive - {n_parts_uploaded} parts received")
                except Exception as err:
                    # check to see if the upload has finished - in which case the temporary upload folder will have been
                    # replaced by a ZIP file (causing exception in code above)
                    for child in self.staging_dir.iterdir():
                        if child.is_file() and child.name.endswith('.zip'):
                            zip_path = child
                            break
                    if not zip_path:
                        self.msg_q.put_nowait(f"Error: Archive upload failed, ZIP file missing ({err})")
                        self.result = False
                        return

        # Steps 3-5: Preprocessing archive contents...
        cancelled = self.cancel_requested()
        if not cancelled:
            error_msg = self.preprocess_session_archive(zip_path)
            if error_msg:
                self.msg_q.put_nowait(error_msg)
                self.result = False
                return
            cancelled = self.cancel_requested()

        if cancelled:
            SessionBuilder.delete_directory_tree(self.staging_dir)

        self.msg_q.put_nowait("Processing complete!" if not cancelled else "Staging directory removed after cancel")
        self.result = False if cancelled else True

    def cancel_requested(self) -> bool:
        if self.cancel_request.isSet():
            return True
        return False

    def cancel(self) -> None:
        self.cancel_request.set()

    def preprocess_session_archive(self, zip_path: Path) -> Optional[str]:
        """
        This method pre-processes the uploaded session data ZIP archive, scanning the archive contents and extracting
        information that will be needed when the session is actually committed to the lab database: (1) the unique trial
        protocols presented during the session; (2) timing information for all trial reps, in particular, the start and
        stop timestamps for the trial in the Omniplex timeline (for electrophysiological experiments using the Omniplex
        system); and (3) metrics for all neural units recorded in the session.

        The pre-processing results are stored in a pickle file, 'preprocessing.pickle', in the same directory as the ZIP
        archive.

        Pre-processing a large (>1GB) session can take many minutes, so progress messages are delivered over the
        thread's synchronous message queue. The method also checks regularly for a cancel request.

        Args:
            zip_path: File system path for the session data ZIP archive.

        Returns:
            None if successful; otherwise an error description. Returns None if the session commit is cancelled.
        """
        error_msg: Optional[str] = None
        try:
            trial_timings: Dict[str, TrialTiming] = dict()
            neural_units: List[OmniplexUnit] = list()
            with zipfile.ZipFile(zip_path, 'r') as archive:
                self.msg_q.put_nowait("Scanning archive contents...")
                data_file_name_pattern = re.compile('.[0-9][0-9][0-9][0-9]+$')
                archive_list = archive.infolist()
                pl2s_archived: List[zipfile.ZipInfo] = list()
                units_zip_info: Optional[zipfile.ZipInfo] = None
                for info in archive_list:
                    if (len(info.filename) > 3) and (info.filename[-3:].lower() == 'pl2'):
                        pl2s_archived.append(info)
                    elif data_file_name_pattern.search(info.filename) is not None:
                        header = maestro.DataFileHeader.parse_header(archive.read(info))
                        file_index = int(info.filename[-4:])
                        header_timestamp = header.timestamp_ms if header.version >= 21 else None
                        duration = float(header.num_scans_saved - 1) / 1000.0  # Trial mode scan rate is fixed at 1KHz
                        trial_timings[info.filename] = \
                            TrialTiming._make([file_index, header_timestamp, duration, None, None])
                    elif ((len(info.filename) > 7) and (info.filename[-7:].lower() == '.pickle')) or \
                            ((len(info.filename) > 4) and (info.filename[-4:].lower() == '.pkl')):
                        if units_zip_info is None:
                            units_zip_info = info
                        else:
                            raise Exception("Found more than one spikes data file in session data archive!")
                if (units_zip_info is not None) and (len(pl2s_archived) == 0):
                    raise Exception("Missing Omniplex file(s) for spike-sorted unit data!")
                if self.cancel_requested():
                    return None

                self.msg_q.put_nowait("Processing archive for trial protocols...")
                trial_protocols: List[maestro.Protocol] = maestro.Protocol.extract_protocols_from_session_data(archive)
                if len(trial_protocols) == 0:
                    raise Exception("No trial protocols found in session archive!")
                if self.cancel_requested():
                    return None

                unit_data: Optional[Dict[str, List[Any]]] = None
                if units_zip_info is not None:
                    self.msg_q.put_nowait(f"Loading neural units data file {units_zip_info.filename}...")
                    unit_data = pickle.loads(archive.read(units_zip_info))
                    pl2_filenames = [x.filename for x in pl2s_archived]
                    if not _validate_neural_unit_data(unit_data, pl2_filenames):
                        raise Exception(f"Invalid format for neural units file: {units_zip_info.filename}")
                    # if 'filename' field missing, assume all units recorded in same Omniplex file
                    if 'filename' not in unit_data:
                        unit_data['filename'] = [pl2_filenames[0]] * len(unit_data['channel'])
                    if self.cancel_requested():
                        return None

                if units_zip_info is not None:
                    for pl2_zip_info in pl2s_archived:
                        self.msg_q.put_nowait(f"Extracting Omniplex file {pl2_zip_info.filename}... PLEASE WAIT")
                        save_path = Path(archive.extract(pl2_zip_info, str(zip_path.parent)))
                        self._process_omniplex_file(save_path, unit_data, trial_timings, neural_units)

                    # if there is unit data, we require metrics for each unit specified in the neural units data file,
                    # and there must be Omniplex timestamps for all trials
                    if len(neural_units) < len(unit_data):
                        raise Exception(
                            f"Missing analog data for at least one unit defined in {units_zip_info.filename}")
                    for key in trial_timings.keys():
                        if trial_timings[key].omniplex_start is None:
                            raise Exception(f"Missing Omniplex start/stop timestamps for {key}")
                if self.cancel_requested():
                    return None

                self.msg_q.put_nowait(f"Saving pre-processed session data...")
                save_path = Path(zip_path.parent, 'preprocessing.pickle')
                results = {'protocols': trial_protocols, 'timings': trial_timings, 'units': neural_units}
                with open(save_path, 'wb') as file:
                    pickle.dump(results, file)
        except Exception as err:
            error_msg = f"Error: {str(err)}"

        return error_msg

    def _process_omniplex_file(self, path: Path, unit_data: Dict[str, List[Any]], trial_timings: Dict[str, TrialTiming],
                               units: List[OmniplexUnit]) -> None:
        """
        Process the Omniplex file for information needed when committing an electrophysiological recording session to
        the lab database.

        First, the method analyses the "Strobed" and "EVT02" event channels to find the Omniplex-recorded start and stop
        timestamps for each Maestro trial presented. These timestamps are essential in order to align neural unit
        spike times derived from the Omniplex recording with behavioral responses recorded in each individual Maestro
        trial data file.

        Second, the method calculate selected metrics for each identified neural unit (mean firing rate, signal-to-noise
        ratio, and the average spike template waveform) using the unit spike times (in "Omniplex time") and  the
        original Omniplex analog data stream(s) from which those spike times were "sorted". These metrics are ultimately
        stored in the lab database.

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

        Args:
            path: The path to the Omniplex PL2 file to be processed.
            unit_data: The identified neural unit data, including channel ID, PL2 source file, and the spike timestamps
                in seconds since the Omniplex recording started. For a full description of this dictionary, see
                _validate_neural_unit_data().
            trial_timings: Dictionary holding timing information culled from the Maestro trial files in the session
                archive, keyed by the trial data file name. On return, each TrialTiming tuple in the dictionary should
                include the relevant Omniplex start and stop timestamps.
            units: List of neural units. The method will append an OmniplexUnit tuple for each neural unit recorded in
                the specified PL2 file.

        Raises:
            Exception if an error occurs while loading and processing data in the Omniplex file.
        """
        units_in_file = [i for i, filename in enumerate(unit_data['filename']) if filename == path.name]
        with open(path, 'rb') as fp:
            self.msg_q.put_nowait(f"Processing trial timing information in Omniplex file {path.name}...")
            info = PL2.load_file_information(fp)
            timings_dict = _get_trial_timing_from_pl2_file(fp, info)
            for key in (timings_dict.keys() & trial_timings.keys()):
                old = trial_timings[key]
                start_ts, stop_ts = timings_dict[key]
                trial_timings[key] = \
                    TrialTiming._make([old.file_index, old.header_timestamp, old.duration, start_ts, stop_ts])
            if self.cancel_requested():
                return

            for unit_idx in units_in_file:
                self.msg_q.put_nowait(f"Calculating firing rate and other metrics for neural unit {unit_idx}...")

                # if narrow band channel SPKC<num> specified, use wide band channel WB<num> instead IF it is available
                channel_id = unit_data['channel'][unit_idx]
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

                samples = PL2.load_analog_channel(fp, ch_index, info)  # samples NOT converted to mV
                samples_per_sec: float = info['analog_channels'][ch_index]['samples_per_second']
                to_volts: float = info['analog_channels'][ch_index]['coeff_to_convert_to_units']
                if is_wide_band:
                    samples = _bandpass_filter_wide_band_stream(samples, samples_per_sec)
                samples_in_template = int(samples_per_sec * 0.01)
                total_samples = len(samples)
                template = np.zeros(samples_in_template)
                snr = 0.0
                firing_rate = 0.0
                spike_times = unit_data['spiketimes'][unit_idx]
                if len(spike_times) > 0:
                    firing_rate = float(len(spike_times)) / (spike_times[-1] - spike_times[0])
                    num_good_clips = 0
                    for ts in spike_times:
                        start = int((ts - 0.001) * samples_per_sec)
                        end = start + samples_in_template
                        if (start >= 0) and (end < total_samples):
                            template = np.add(template, samples[start:end])
                            num_good_clips += 1
                    template /= num_good_clips
                    signal = np.max(template) - np.min(template)
                    # MAD estimate of std of background noise for bandpassed trace (median(x) ~ 0)
                    noise = np.median(np.abs(samples)) * 1.4826
                    snr = signal / (1.96 * noise)
                    # convert template waveform from raw digitized units to micro-volts
                    template *= to_volts * 1.0e6
                units.append(OmniplexUnit._make([path.name, channel_id, spike_times, firing_rate, snr, template]))

                # trigger GC cycle because the analog data arrays could be HUGE
                gc.collect()

                if self.cancel_requested():
                    return


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
        unit_data: The dictionary loaded from the neural units pickle file
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


def _bandpass_filter_wide_band_stream(data: np.ndarray, sample_rate_hz: float) -> np.ndarray:
    """
    Bandpass filter an Omniplex "wide-band" analog data stream with a 2nd-order Butterworth bandpass digital filter
    between 300 and 8000Hz.

    NOTE: Memory concerns -- The input Numpy array will typically be int16 (2 bytes per element), as the analog samples
    are 16-bit in the Omniplex file. The output array will be float32 or float64 (4 or 8 bytes per element). If the
    input array is huge, the output will be even larger and could strain memory resources.

    Args:
        data: The wide-band input stream.
        sample_rate_hz: The sample rate for the input stream in Hz.

    Returns:
        The filtered stream. Note that the Numpy array returned will be floating-point.
    """
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
        analog channel stream starting 1ms before each spike timestamp in the spike times array). Units = micro-volts.
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
        in microV (List[float]).
        """
        return {'channel_id': self.channel,
                'spike_times': self.spike_times.tolist(),
                'firing_rate': self.firing_rate,
                'snr': self.snr,
                'template': self.template.tolist()}
