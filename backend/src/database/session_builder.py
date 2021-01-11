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

@author: sruffner
"""

from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class
import threading
from queue import Queue
from typing import Optional, Dict, Any, NamedTuple, Tuple, List
import os
import time
import shutil
from pathlib import Path
import json
import pickle
import uuid
import zipfile
import database.table_views as tv
import database.maestro as maestro


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
            self.running_tasks: Dict[str, SessionWorker] = dict()

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
        msg_q = Queue()
        worker = ProcessArchiveThread(staging_dir, msg_q)
        key = staging_dir.name
        self.running_tasks[key] = SessionWorker(worker, ['Awaiting upload...'], msg_q)
        worker.start()
        return None

    def stage2_progress_update(self, client_state: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if not SessionBuilder._is_valid_build_state(client_state):
            raise SessionBuilderError("Invalid client build state")
        if client_state['stage'] != 2:
            raise SessionBuilderError("Incorrect stage on client (must be stage 2)")
        server_state = SessionBuilder._load_build_state(client_state)
        if not server_state:
            raise SessionBuilderError("Staging directory not found; recommend starting over")
        if server_state['stage'] != 2:
            return "Client out of sync with server", server_state
        if 'cancelled' in server_state:
            return "Session commit cancelled by user", server_state
        staging_dir = SessionBuilder._get_staging_directory_for(server_state)
        session_worker = self.running_tasks[staging_dir.name]
        latest_msg = None
        while session_worker.msg_queue.qsize() > 0:
            latest_msg = session_worker.msg_queue.get_nowait()
        if latest_msg:
            session_worker.last_msg_list[0] = latest_msg
        else:
            latest_msg = session_worker.last_msg_list[0]

        if session_worker.thread.is_alive():
            if latest_msg.startswith("Error"):
                session_worker.thread.join()
            else:
                return latest_msg, server_state

        # stage 2 worker has terminated. Remove if from set of running tasks. If archive upload or processing failed,
        # remove staging directory, recreate it with only the build-state file, and respawn a worker to await upload
        # retry. Otherwise, stage 2 completed successfully -- transition to stage 3.
        self.running_tasks.pop(staging_dir.name, None)
        if latest_msg.startswith("Error"):
            SessionBuilder.delete_directory_tree(staging_dir)
            err_msg = None
            try:
                staging_dir.mkdir(parents=True, exist_ok=False)
            except Exception as err:
                err_msg = f"Failed to create staging directory on server: {str(err)}"

            if not err_msg:
                err_msg = SessionBuilder._write_build_state(server_state)
            if not err_msg:
                err_msg = self._start_process_archive_task(staging_dir)
            if err_msg:
                self.delete_directory_tree(staging_dir)
                return err_msg, {'stage': 1, 'experimenter': '', 'uuid': ''}

            return latest_msg, server_state
        else:
            server_state['stage'] = 3
            err_msg = SessionBuilder._write_build_state(server_state)
            if err_msg:
                self.delete_directory_tree(staging_dir)
                return "Failed to save commit build state... resetting", {'stage': 1, 'experimenter': '', 'uuid': ''}
            else:
                try:
                    with open(Path(staging_dir, 'protocols.pickle'), 'rb') as file:
                        protocols = pickle.load(file)
                        server_state['protocols'] = {protocol.md5_digest: protocol.summary() for protocol in protocols}
                except Exception as err:
                    latest_msg = f"Failed to retrieve trial protocols from staging directory [{str(err)}]:... resetting"
                    self.delete_directory_tree(staging_dir)
                    server_state = {'stage': 1, 'experimenter': '', 'uuid': ''}
            return latest_msg, server_state

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
        if session_worker:
            server_state['cancelled'] = True
            SessionBuilder._write_build_state(server_state)
            session_worker.thread.cancel()
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
        the upload does not start after 10 minutes of waiting, or the upload stalls for more than 1 minute, report the
        error and terminate.

        2) Once the upload completes, process all Maestro data files in the session archive (in situ -- the files are
        NOT extracted from the ZIP file) and generate the list of distinct trial protocols presented over the course of
        the experiment session. Pickle the list of protocols in the file 'protocols.pickle' in the staging directory,
        then terminate. If an error occurs, report the error and terminate.

        3) TODO: Once we've settled on format for neural data, that should be processed as well, if present.

    To communicate progress to the main thread, the worker will post a message to the synchronous queue passed in the
    constructor. A message is posted whenever there's a significant progress transition (eg., upload started, N parts of
    archive uploaded, etc). It is incumbent on the thread that launched the worker to monitor this queue. If an error
    occurs, the error description is the last message posted to the queue, and that message starts with the string
    "Error".

    To cancel the session commit, call cancel(). This method sets a flag to inform the worker thread and returns
    immediately. The worker thread will stop its work in progress and remove the staging directory in its entirety --
    which could take a significant amount of time depending on the directory content at the time.
    """
    def __init__(self, staging_dir: Path, msg_q: Queue):
        super(ProcessArchiveThread, self).__init__(name=f"ProcessArchive-{staging_dir.name}")
        self.staging_dir = staging_dir
        self.msg_q = msg_q
        self.cancel_request = threading.Event()

    def run(self):
        # Step 1: Wait for upload to begin, but give up after 10 minutes (or if requested to stop)
        cancelled = False
        self.msg_q.put_nowait("Awaiting upload...")
        t0 = time.time()
        upload_path: Optional[Path] = None
        while (not upload_path) and (not cancelled):
            time.sleep(1)
            if time.time() - t0 > 600:
                self.msg_q.put_nowait("Error: Upload failed to start for more than 10 minutes")
                return
            if self.cancel_requested():
                cancelled = True
            # if temporary ZIP folder present in staging directory, then upload has started -- proceed to step 2
            elif self.staging_dir.is_dir():
                for child in self.staging_dir.iterdir():
                    if child.is_dir() and child.name.endswith('zip'):
                        upload_path = child
                        self.msg_q.put_nowait("Upload started...")
                        break

        # Step 2: Monitor upload and report progress, but give up if upload stalls for more than 10 minutes. The
        # temporary folder into which chunks of the ZIP file are uploaded will exist until upload is complete, at which
        # point it is removed and the ZIP file should exist in the staging directory. Deliver progress messages
        # indicating how many chunks have been uploaded thus far.
        n_parts_uploaded = 0
        t0 = -1
        zip_path = None
        while (not zip_path) and (not cancelled):
            time.sleep(1)
            if self.cancel_requested():
                cancelled = True
                break
            try:
                n_chunks = len([f for f in upload_path.iterdir() if f.is_file()])
                if n_chunks == n_parts_uploaded:
                    if t0 < 0:
                        t0 = time.time()
                    elif time.time() - t0 > 60:
                        self.msg_q.put_nowait("Error: Upload has stalled for more than 1 minute.")
                        return
                else:
                    n_parts_uploaded = n_chunks
                    t0 = -1
                    self.msg_q.put_nowait(f"Uploading archive - {n_parts_uploaded} parts received")
            except Exception as err:
                # check to see if the upload has finished - in which case the temporary upload folder will have been
                # replaced by a ZIP file (causing exception in code above)
                for child in self.staging_dir.iterdir():
                    if child.is_file() and child.name.endswith('.zip'):
                        zip_path = child
                        self.msg_q.put_nowait("Archive upload complete - processing...")
                if not zip_path:
                    self.msg_q.put_nowait(f"Error: Archive upload failed, ZIP file missing ({err})")
                    return

        # Steps 3-5: Verify archive (this may take a while), extract trial protocols (0.5 secs for 1000 data files),
        # and pickle the list of protocols to 'protocols.pickle' in staging directory
        try:
            if not cancelled:
                with zipfile.ZipFile(zip_path, 'r') as archive:
                    self.msg_q.put_nowait("Verifying archive...")
                    if archive.testzip():
                        self.msg_q.put_nowait("Error: Uploaded archive appears to be corrupted.")
                        return
                cancelled = self.cancel_requested()
            trial_protocols: List[maestro.Protocol] = []
            if not cancelled:
                self.msg_q.put_nowait("Processing archive for trial protocols...")
                trial_protocols = maestro.Protocol.extract_protocols_from_session_data(zip_path)
                if len(trial_protocols) == 0:
                    self.msg_q.put_nowait("Error: No trial protocols found in session archive!")
                    return
                cancelled = self.cancel_requested()
            if not cancelled:
                proto_path = Path(self.staging_dir, 'protocols.pickle')
                with open(proto_path, 'wb') as file:
                    pickle.dump(trial_protocols, file)
                cancelled = self.cancel_requested()
        except maestro.DataFileError as err:
            self.msg_q.put_nowait(f"Error while extracting trial protocols: {str(err)}")
            return
        except Exception as err:
            msg = f"Error: Unexpected failure -- {str(err)}"
            self.msg_q.put_nowait(msg)
            return

        if cancelled:
            SessionBuilder.delete_directory_tree(self.staging_dir)

        self.msg_q.put_nowait("Processing complete!" if not cancelled else "Staging directory removed after cancel")

    def cancel_requested(self) -> bool:
        if self.cancel_request.isSet():
            self.msg_q.put_nowait("Error: Session commit cancelled")
            return True
        return False

    def cancel(self) -> None:
        self.cancel_request.set()


class SessionWorker(NamedTuple):
    thread: ProcessArchiveThread
    last_msg_list: List[str]
    msg_queue: Queue
    pass
