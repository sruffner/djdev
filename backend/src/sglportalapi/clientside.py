"""
clientside.py: Client-side functions to access the Lisberger lab portal database via API endpoints.

The portal database is primarily an archive for experimental data collected in the Lisberger laboratory. While the web
portal offers graphical views of the data, including aggregate response statistics across repeated trial presentations,
it cannot possibly provide a full analytical suite.

Lab researchers would like to be able to retrieve preprocessed trial data from the database and perform their own
analyses. RESTful-like API endpoints are available on the portal server, providing a "read-only" avenue to retrieve
arbitrary experimental data sets. To protect data provenance, access to these endpoints requires user authentication
through a dedicated endpoint, which returns an access token that is supplied in requests to all other API endpoints.

It is also possible to commit an experiment session's worth of recorded data to the portal using the "/api/commit"
endpoint. The commit workflow was recently redesigned to make it more automated. All metadata required to commit an
experiment to the portal database now must be supplied **prior** to uploading the session archive; as a result, the
commit can run to completion on the server without further user input -- unless preprocessing the archive finds one or
more trial protocols that require manual validation (in the "review" phase). This redesign made API-managed commits
much more convenient.

This module is essentially a client-side "wrapper" for the API endpoints, intended for use by custom analysis scripts
-- or within an interactive Python console. It takes care of the details of user authentication, managing the access
token, preparing and sending the requests to the API and processing the responses. The data returned is generally a
data object or list of objects. The data object classes are defined in the modules data_containers.py; Maestro-specific
data objects such as trial protocol definitions and target definitions are defined in maestro.py.

The server-side implementation of the endpoints is found in the companion module endpoints.py.

Author: saruffner
"""
import sys
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional, Union, List, Tuple

import requests
from requests import RequestException

from sglportalapi.data_containers import SessionInfo, NeuronInfo, TrialRep, Route, APISerializeError, MetadataTable, \
    RequestedData
from sglportalapi.maestro import Protocol

_REQ_TIMEOUT_SECONDS: float = 20
""" Any request to a portal API endpoint will timeout after this many seconds. """


def check_session_archive(zip_path: Union[str, Path]) -> str:
    """
    Helper method examines the contents of a session archive to verify it meets the following minimum requirements.
    While no means an exhaustive check, it can save time by avoiding the time-consuming upload of a multi-GB archive
    that fails to satisfy these requirements.
    - Must not contain any directories.
    - Can contain at most ONE pickle file, in which information about recorded neural units is stored.
    - The archive must contain valid Maestro trial data files with version >= 19. If the file version < 21, the
    archive must contain the file 'setnames.csv' containing the trial set name (and, optionally, subset name) for every
    trial data file in the archive. To check these requirements, the method processes all Maestro data files (and the
    setnames.csv' file if necessary) to extract each distinct trial protocol presented in the session. This takes a
    few seconds at most.
    - If the archive has neural unit data, it must contain at least one Omniplex PL2 file OR the file 'timestamps.csv'.
    In lieu of the PL2 data, the latter file provides the elapsed start time (in the same timeline as the unit spike
    trains) for each trial recorded during the session. The file's contents are not checked.

    Args:
        zip_path: The path to the session archive ZIP.
    Returns:
        An empty string if the specified archive passes all checks, else a brief description of the problem.
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as archive:
            archive_list: List[zipfile.ZipInfo] = archive.infolist()

            got_pl2, got_pickle, got_ts_csv = False, False, False
            for info in archive_list:
                if info.is_dir() or (info.filename.find('/') > -1):
                    raise Exception("A session archive must be flat list of files with no directory structure.")
                elif info.filename == 'timestamps.csv':
                    got_ts_csv = True
                elif ((len(info.filename) > 7) and (info.filename[-7:].lower() == '.pickle')) or \
                        ((len(info.filename) > 4) and (info.filename[-4:].lower() == '.pkl')):
                    if got_pickle:
                        raise Exception("A session archive can contain only one neural units 'pickle' file.")
                    got_pickle = True
                elif (len(info.filename) > 3) and (info.filename[-3:].lower() == 'pl2'):
                    got_pl2 = True
            if got_pickle and not (got_pl2 or got_ts_csv):
                raise Exception("A session rchive with neural unit data must contain a PL2 file or timestamps.csv")

            # extracting trial protocols doesn't take too long and verifies a lot!
            _ , _ = Protocol.extract_protocols_from_session_data(archive, set())
    except Exception as e:
        return f"Session archive failed sanity check: {str(e)}"
    return ""


class PortalAccessor:
    """
    The portal API access manager.

    Instantiate this object with your portal username and password, then use the various methods to retrieve selected
    data from the portal, or to commit data from an experiment session to the portal database.

    For example::

        accessor = PortalAccessor(username='<uname>', password='<pwd>', url='<root url of portal website>')
        err_msg, subject_table = accessor.metadata_table(MetadataTable.SUBJECTS)
        sessions = accessor.sessions(experimenter=...)
        ...
    """
    def __init__(self, username: str, password: str, url: str):
        """
        Create an accessor object that manages all queries to the Lisberger lab portal database via RESTful-like API
        endpoints.

        Args:
            username: Username of a registered user on the portal.
            password: The registered user's password.
            url: The portal's root URL -- where the portal web application is accessed on a browser. All API endpoints
                are path extensions of this URL.
        """
        self._uname: str = username
        """ Username for authentication on portal. """
        self._pwd: str = password
        """ Password for authentication on portal. """
        self._base_url: str = url[:-1] if url.endswith('/') else url
        """ The portal's root URL."""
        self._token: Optional[str] = None
        """ Access token required for portal API access; obtained upon authenticating user at root endpoint. """
        self._expires: Optional[float] = None
        """ Approximate time at which access token expires, in seconds since the epoch."""

    def authenticate(self) -> Optional[str]:
        """
        Authenticate user with the portal API, obtaining a JSON web token as an access token that must be supplied in
        all future requests to retrieve portal data through API endpoints.

        Returns:
            None if authentication was successful (or user is already authenticated); else an error message. Reasons for
                failure include connection issues, invalid credentials, or internal server errors.
        """
        # authenticate if we don't have an access token, or token has expired
        if isinstance(self._token, str):
            if time.time() >= self._expires:
                self._token, self._expires = None, None
            else:
                return None  # already authenticated

        try:
            response = requests.post(f"{self._base_url}{Route.AUTHENTICATE}",
                                     json=dict(username=self._uname, password=self._pwd),
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            if response.status_code == 200 or response.status_code == 400:
                try:
                    content = Route.deserialize_api_response(Route.AUTHENTICATE, response.content)
                    if response.status_code == 200:
                        self._token = content['token']
                        self._expires = time.time() + content['expires_in'] - 60
                        return None
                    else:
                        return f"Authentication failed on server: [{response.status_code}] {content['error']}"
                except APISerializeError as e:
                    return f"Authentication failed; unable to decode server response: {str(e)}"
            return f"Authentication failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Authentication request failed on send: {str(e)}"

    def metadata_table(self, table_name: str) -> Tuple[str, Optional[MetadataTable]]:
        """
        Retrieve the entire contents of one of the small "general information" tables in the portal database. These
        tables contain information used to describe, categorize and search for experimental data sets, such as:
        experiment subjects, subject implants, experiment rigs, research studies, neuron types, and brain areas. The
        very large database tables storing experiment sessions, neural units, trial protocols, and recorded trial
        response data are **not** exposed by this method.

        Args:
            table_name: Name of the metadata table requested. For a list of recognized table names, see
                :py:class:`sglportalapi.data_containers.MetadataTable`.
        Returns:
            A 3-tuple (emsg, metatable). On failure, `emsg` is the error description and `metatable` is None. On
                success, emsg is an empty string, and `metatable' encapsulates the requested table's contents.
        """
        if (out := self.authenticate()) is not None:
            return out, None
        req_body = dict(table=table_name)
        try:
            response = requests.post(f"{self._base_url}{Route.METADATA_TABLE}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.METADATA_TABLE, response.content)
            if response.status_code == 200:
                return '', content['metatable']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}", None
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}", None
        except RequestException as e:
            return f"Request failed on send: {str(e)}", None

    def sessions(self, experimenter: Optional[str] = None, subject: Optional[str] = None,
                 when: Optional[str] = None) -> Union[str, List[SessionInfo]]:
        """
        Retrieve a list of experiment sessions from the portal database.

        Args:
            experimenter: If not None, restrict to sessions belonging to the registered portal user identified by this
                username.
            subject: If not None, restrict to sessions belonging to the experiment subject with this ID.
            when: If not None, restrict to sessions with a recording date that satisfies this condition. It must have
                the form 'op YYYY-MM-DD', where 'op' is one of '=' (on specified date), '>' (after specified date), or
                '<' (before specified date).
        Returns:
            If successful, a list (possibly empty) of session information objects for all experiment sessions in the
                portal database that satisfy the given constraints. If the operation fails, returns an error message.
        Raises:
            ValueError:
                If the 'when' argument is incorrectly formatted.
        """
        if (out := self.authenticate()) is not None:
            return out
        if when is not None:
            when_parts = when.split()
            try:
                if not ((len(when_parts) == 2) and (when_parts[0] in ['=', '>', '<'])):
                    raise ValueError()
                date.fromisoformat(when_parts[1])
            except Exception:
                raise ValueError("Arg 'when' must have the format '=|>|< YYYY-mm-dd'")
        req_body = dict(experimenter=experimenter, subj_id=subject, when=when)
        try:
            response = requests.post(f"{self._base_url}{Route.SESSIONINFO}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.SESSIONINFO, response.content)
            if response.status_code == 200:
                return content['sessions']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_neurons(self, session: SessionInfo, min_spikes: Optional[int] = None, min_snr: Optional[float] = None) \
            -> Union[str, List[NeuronInfo]]:
        """
        Retrieve information about selected neural units recorded during a specified experiment session committed to
        the Lisberger lab portal database.

        Args:
            session: The experiment session.
            min_spikes: If specified, retrieve only those units with a total recorded spike count matching or exceeding
                this limit. Default = None.
            min_snr: If specified, retrieve only those units with an estimated signal-to-noise value matching or
                exceeding this threshold. Default = None.
        Returns:
             If successful, a list (possibly empty) of neuron information records for all neural units recorded during
                the session that satisfy the given constraints. If the operation fails, returns an error message.
        """
        if (out := self.authenticate()) is not None:
            return out
        req_body = dict(session_key=session.primary_key, min_spikes=min_spikes, min_snr=min_snr)
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_NEURONS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.SESSION_NEURONS, response.content)
            if response.status_code == 200:
                return content['neurons']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_protocols(self, session: SessionInfo) -> Union[str, List[Protocol]]:
        """
        Retrieve the definitions of all distinct Maestro trial protocols presented during a specified experiment session
        committed to the Lisberger lab portal database.

        Args:
            session: The experiment session.
        Returns:
             If successful, a list of protocol definition objects. If the operation fails, returns an error message.
        """
        if (out := self.authenticate()) is not None:
            return out
        req_body = dict(session_key=session.primary_key)
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_PROTOCOLS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.SESSION_PROTOCOLS, response.content)
            if response.status_code == 200:
                return content['protocols']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_trial(self, session: SessionInfo, trial_idx: int, unit_ids: Optional[List[int]] = None,
                      what: Optional[RequestedData] = None) -> \
            Union[str, TrialRep]:
        """
        Retrieve response data and other metadata for a single trial rep recorded during a specified experiment session.

        Args:
            session: The experiment session.
            trial_idx: The index of the trial to retrieve. This is an integer in [1..N], where N is the number of trials
                presented during the experiment.
            unit_ids: The IDs of up to 5 neural units for which trial-aligned spike train responses are requested. Each
                ID is an integer in [1..M], where M is the number of distinct units recorded during the experiment. If
                None or empty list, no neural response data is retrieved. Only the first 5 unique IDs are included; any
                additional or repeat elements are ignored.
            what: Bit flag set indicating what types of data should be retrieved. By default, all available
                data are retrieved. For example, if you only need the neural spike trains for the units listed in
                `unit_ids`, set this argument to `RequestedData.NEURONAL`.
        Returns:
            The trial rep requested, or an error message if the operation fails.
        """
        if (out := self.authenticate()) is not None:
            return out
        unit_ids = sorted([x for x in set(unit_ids)]) if isinstance(unit_ids, list) else []
        req_body = dict(session_key=session.primary_key, trial_index=trial_idx, unit_ids=unit_ids[0:5],
                        what=int(what if isinstance(what, RequestedData) else RequestedData.ALL))
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_TRIAL}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.SESSION_TRIAL, response.content)
            if response.status_code == 200:
                return content['trial']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_trial_block(self, session: SessionInfo, start: int, end: int, unit_ids: Optional[List[int]] = None,
                            what: Optional[RequestedData] = None) -> Union[str, List[TrialRep]]:
        """
        Retrieve response data and other metadata for a sequential block of up to 25 trial reps recorded during a
        specified experiment session.

        Args:
            session: The experiment session.
            start: The index of the first trial in block. This is an integer in [1..N], where N is the number of trials
                presented during the experiment.
            end: Index of last trial in block. Must be >= 'start'.
            unit_ids: The IDs of up to 5 neural units for which trial-aligned spike train responses are requested. Each
                ID is an integer in [1..M], where M is the number of distinct units recorded during the experiment. If
                None or empty list, no neural response data is retrieved. Only the first 5 unique IDs are included; any
                additional or repeat elements are ignored.
            what: Bit flag set indicating what types of data should be retrieved. By default, all available
                data are retrieved. For example, if you only need the neural spike trains for the units listed in
                `unit_ids`, set this argument to `RequestedData.NEURONAL`.
        Returns:
            The list of trials requested, or an error message if the operation fails.
        """
        if (out := self.authenticate()) is not None:
            return out
        unit_ids = sorted([x for x in set(unit_ids)]) if isinstance(unit_ids, list) else []
        req_body = dict(session_key=session.primary_key, start=start, end=end, unit_ids=unit_ids[0:5],
                        what=int(what if isinstance(what, RequestedData) else RequestedData.ALL))
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_BLOCK}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.SESSION_BLOCK, response.content)
            if response.status_code == 200:
                return content['trials']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_protocol_reps(self, session: SessionInfo, proto: Protocol, completed: bool = False,
                              unit_ids: Optional[List[int]] = None, what: Optional[RequestedData] = None) -> \
            Union[str, List[TrialRep]]:
        """
        Retrieve response data and other metadata for all reps of a specified Maestro trial protocol recorded during a
        specified experiment session.

        Args:
            session: The experiment session.
            proto: The Maestro trial protocol.
            completed: If False (True), all (only successfully completed) reps of the specified protocol are included
                in the result. Default = False.
            unit_ids: The IDs of up to 5 neural units for which trial-aligned spike train responses are requested. Each
                ID is an integer in [1..M], where M is the number of distinct units recorded during the experiment. If
                None or empty list, no neural response data is retrieved. Only the first 5 unique IDs are included; any
                additional or repeat elements are ignored.
            what: Bit flag set indicating what types of data should be retrieved. By default, all available
                data are retrieved. For example, if you only need the neural spike trains for the units listed in
                `unit_ids`, set this argument to `RequestedData.NEURONAL`.
        Returns:
            The list of trials requested, or an error message if the operation fails.
        """
        if (out := self.authenticate()) is not None:
            return out
        unit_ids = sorted([x for x in set(unit_ids)]) if isinstance(unit_ids, list) else []
        req_body = dict(
            session_key=session.primary_key, proto_hash=proto.md5_digest, completed=completed, unit_ids=unit_ids[0:5],
            what=int(what if isinstance(what, RequestedData) else RequestedData.ALL)
        )
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_PROTOCOL_REPS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.SESSION_PROTOCOL_REPS, response.content)
            if response.status_code == 200:
                return content['trials']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def neurons(self, min_spikes: Optional[int] = None, min_snr: Optional[float] = None,
                min_rate: Optional[float] = None, neuron_type: Optional[str] = None, subj_id: Optional[str] = None,
                study_title: Optional[str] = None, proto: Optional[Protocol] = None,
                min_complete: Optional[int] = None) -> Union[str, List[NeuronInfo]]:
        """
        Search the portal database for all neurons that satisfy zero or more filter criteria. The available filtering
        constraints allow for a wide variety of searches, for example:
         - Find all neural units recorded in a specific experiment subject.
         - Find all neural units with a measured mean firing rate of at least 10Hz that were recorded in any experiment
           belonging to a specified research study.
         - Find all neural units recorded during at least 10 successfully completed reps of a specified trial protocol.

        If you specify no criteria at all, the method will return information on every neural unit currently stored in
        the portal database. Neuron type, subject ID, and research study title are metadata that can be queried via
        `metadata_table()` method. Trial protocols for a particular experiment session can be retrieved via
        `session_protocols()`.

        Args:
            min_spikes: If not None, include only those units with a total number of recorded spikes >= this value.
            min_snr: If not None, include only those units with SNR >= this value.
            min_rate: If not None, include only those units with mean firing rate >= this value.
            neuron_type: If not None, include only those units classified as this neuron type.
            subj_id: If not None, include only neural units recorded in this experiment subject.
            study_title: If not None, include only neural units recorded as a part of this research study.
            proto: If not None, include only neural units with trial responses recorded for this trial protocol.
            min_complete: If not None AND a trial protocol is specified, include only neural units for which response
                data is available from at least this many successfully completed reps of the specified trial protocol.
        Returns:
            If successful, a list (possibly empty) of neuron information records -- one for each neural unit in the
                portal database that satisfies all specified filter constraints. If the operation fails, returns an
                error message.
        Raises:
            ValueError:
                If any of the numeric arguments are not strictly positive.
        """
        try:
            for x in [min_spikes, min_snr, min_rate, min_complete]:
                if (x is not None) and (x <= 0):
                    raise ValueError("All numeric arguments must be strictly positive")
        except Exception:
            raise ValueError("Invalid type for numeric argument")

        if (out := self.authenticate()) is not None:
            return out

        req_body = dict(min_spikes=min_spikes, min_snr=min_snr, min_rate=min_rate, neuron_type=neuron_type,
                        subj_id=subj_id, study_title=study_title, proto_hash=proto.md5_digest if proto else None,
                        min_complete=min_complete)
        try:
            response = requests.post(f"{self._base_url}{Route.NEURONS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.NEURONS, response.content)
            if response.status_code == 200:
                return content['neurons']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def commit_start(
            self, zip_path: Path, unit_types: List[str], experimenter: str, subject: str, rec_date: str, suffix: int,
            rig: str, study: Union[str, int], notes: str, brain_area: Optional[Union[str, int]] = None,
            src: Optional[str] = None, probe: Optional[str] = None, rate: Optional[float] = None,
            x: Optional[float] = None, y: Optional[float] = None, z: Optional[float] = None,
            show_progress: bool = True) -> Tuple[bool, str]:
        """
        Start the process of committing an experiment session's worth of data to the portal database.

        This API provides an alternative to using the portal website to initiate a session commit. It is best
        suited to sessions in which the "review" phase can be skipped, which will be the case if no trial protocol
        presented during the experiment requires manual validation by the user. [This should be the case so long as
        every distinct protocol is presented a minimum of 3 times over the course of the session.] For such commits,
        no further user interaction is required -- unless an error occurs, in which case the experiment session must be
        resubmitted anyway.

        The method will send a "start commit" request accompanied by the required session metadata and unit types list,
        then upload the session archive directly to the portal's S3-based repository via a chunked, multipart upload
        (using a sequence of presigned upload part URLs provided by the portal server). The method will BLOCK until the
        upload is completed. An "upload_done" request informs the portal server that the multipart upload operation is
        done, at which point the server will queue a background task to process the archive.

        All session metadata supplied here -- the unit types list, experimenter, subject, etc. -- must be valid and
        must not correspond to an experiment session that is already committed to the portal or is currently pending.
        Some metdata requires knowledge of the contents of some of the so-called metadata tables in the database: the
        neuron type names, the experimenter's username, the subject ID, and so on. The operation will fail if any
        metadata are invalid, and the error message will indicate the first problem encountered.

        The session archive supplied must meet certain requirements which are checked before starting the time-consuming
        task of uploading the archive to the portal repository. See check_session_archive().

        If the operation succeeds, you can use the commit job ID returned to monitor the progress of the commit, and
        cancel/remove the job if desired. However, once the experiment is fully committed to the database, the commit
        job is considered "done" and cannot be "rolled back".

        If the operation fails during the upload phase, an attempt is made to abort the multipart upload and completely
        remove the commit job from the server. Contact the portal administrator if the server fails to perform this
        "clean-up" task, as some files may be left dangling in the commit staging area in the portal's S3-based
        repository, or in local disk storage on the portal server itself.

        To use this API, you must have "commit"-level access on the portal.

        Args:
            zip_path: The path to the session archive ZIP.
            unit_types: A list of length N, where N is the number of neural units recorded during the experiment.
                The n-th element specifies a recognized neuron type to be assigned to the n-th unit. For behavior-only
                sessions, this must be an empty list.
            experimenter: Username of the registered portal user that conducted the experiment. Note that the
                experimenter need not be the same as the user committing the experiment session to the database.
            subject: ID of the subject of the experiment.
            rec_date: Recording date in ISO format - 'YYYY-MM-DD'.
            suffix: Session suffix in [1..9]. Typically 1, but you must use different suffixes to distinguish multiple
                sessions recorded in the same subject by the same experimenter on the same date.
            rig: ID of the rig on which experiment was conducted.
            study: The research study to which experiment belongs -- specify either the study title or the unique
                integer key identifying the study in the portal database.
            notes: Session notes. Can be an empty string.
            brain_area: The region of brain in which neural units were recorded -- specify either the brain area name or
                the unique integer key identifying it in the portal database. None for behavioral session.
            src: The electrophysiology recording source. Must be one of 'Omniplex', 'Omniplex clips', 'Plexon MAP',
                'Maestro Waveform', 'Maestro Spike Ch'; currently, only 'Omniplex' supported. None for behavioral
                session.
            probe: The probe type. Must be one of 'single', '32-channel', 'other'. None for behavioral session.
            rate: The probe sampling rate in Hz. None for behavioral session.
            x: The X-coordinate of probe location within implant cylinder, in mm. None for behavioral sesion.
            y: The Y-coordinate of probe location within implant cylinder, in mm. None for behavioral sesion.
            z: Probe insertion depth in mm. None for behavioral sesion.
            show_progress: If True, a progress message is updated on the Python console (STDOUT) while the archive is
                uploaded.
        Returns:
            A 2-tuple (True, job_id) if archive file is successfully uploaded to the portal and a background job is
                queued to preprocess the archive, where `job_id` is the unique ID assigned to the session commit job on
                the server. Otherwise: (False, string describing the error).
        """
        if not (isinstance(zip_path, Path) and zip_path.is_file()):
            return False, "Archive file missing or path not specified"
        err_msg = check_session_archive(zip_path)
        if len(err_msg) > 0:
            return False, err_msg

        zip_size = zip_path.stat().st_size

        if (out := self.authenticate()) is not None:
            return False, out

        # start the commit job
        if not isinstance(unit_types, list):
            unit_types = list()
        session = dict(experimenter=experimenter, subject=subject, rec_date=rec_date, suffix=suffix,
                       rig=rig, study=study, notes=notes)
        if len(unit_types) > 0:
            session.update(dict(brain_area=brain_area, src=src, probe=probe, rate=rate, x=x, y=y, z=z))

        req_body = dict(action='start', session=session, unit_types=unit_types, size=zip_size)
        try:
            response = requests.post(f"{self._base_url}{Route.COMMIT}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.COMMIT, response.content)
            if response.status_code != 200:
                return False, f"Failed to start commit job [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return False, f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return False, f"Request failed on send: {str(e)}"
        job_id: str = content['job_id']
        chunk_size: int = content['chunk_size']
        urls: List[str] = content['urls']

        # execute multipart upload to transfer session archive file to portal repo in S3
        upload_error, parts = None, []
        try:
            with zip_path.open('rb') as f:
                if show_progress:
                    sys.stdout.write("\nStarting upload...")
                session = requests.Session()
                for num, url in enumerate(urls):
                    part = num + 1
                    t0 = time.time()
                    file_data = f.read(chunk_size)
                    res = session.put(url, data=file_data)
                    t_elapsed = time.time() - t0
                    if res.status_code != 200:
                        raise Exception(f"Archive upload failed on chunk {part} [{res.status_code}]")
                    etag = res.headers['ETag']
                    parts.append({'ETag': etag, 'PartNumber': part})
                    if show_progress:
                        sys.stdout.write(f"\r{zip_path.name}: Uploaded {part} of {len(urls)} chunks "
                                         f"[in {t_elapsed: .1f}s]")
                        sys.stdout.flush()
                if show_progress:
                    sys.stdout.write(" finishing up.\n")
                    sys.stdout.flush()
        except Exception as e:
            upload_error = str(e)

        # if an error occurred, abort the multipart upload, which also terminates the commit.
        if upload_error is not None:
            req_body = dict(action="upload_abort", job_id=job_id)
            abort_error = None
            try:
                response = requests.post(f"{self._base_url}{Route.COMMIT}",
                                         json=req_body,
                                         headers={'Authorization': f"Bearer {self._token}"},
                                         allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
                content = Route.deserialize_api_response(Route.COMMIT, response.content)
                if response.status_code != 200:
                    abort_error = content['error']
            except APISerializeError as e:
                abort_error = f"Failed to decode server response: {str(e)}"
            except RequestException as e:
                abort_error = f"Request failed on send: {str(e)}"

            err_msg = f"ERROR: {upload_error}"
            if abort_error is not None:
                err_msg = f"{err_msg}\n   Failed to abort multipart upload [{abort_error}]. Contact portal admin."
            return False, err_msg

        # complete multipart upload on server, transitioning commit job to preprocessing phase.
        req_body = dict(action="upload_done", job_id=job_id, parts=parts)
        try:
            response = requests.post(f"{self._base_url}{Route.COMMIT}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.COMMIT, response.content)
            if response.status_code != 200:
                upload_error = content['error']
        except APISerializeError as e:
            upload_error = f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            upload_error = f"Request failed on send: {str(e)}"

        if upload_error is not None:
            return False, f"Failed after archive upload: {upload_error}.\nCheck job status and cancel job."
        else:
            return True, job_id

    def commit_status(self, job_id: Optional[str] = None) -> Tuple[bool, Union[str, List[dict]]]:
        """
        Retrieve status information for a specified pending commit job or all pending commit jobs belonging to the
        authenticated portal user.

        You can only use this method to check the progress of session commits that you started, whether using this
        clientside API or the 'commit' page on the portal's web site. Per-job status information is returned as a
        dictionary with the following keys:
            - `job_id [str]`: The commit job's unique ID.
            - `messages [List[str]]`: The job's progress history, with messages in reverse chronological order.
            - `started [float]`: The timestamp (seconds since the "epoch") when the commit job was initiated.
            - `updated [float]`: The timestamp when the commit job's progress was last updated.
            - `state [str]`: The job's current state, one of 'UPLOADING', 'PREPROCESS', 'REVIEW', 'CANCEL', 'COMMIT',
              'FAIL', or 'DONE'. If a commit job is in the 'REVIEW' state, you must use the portal's web site to review
              and validate one or more trial protocols before committing the session data to the portal database.
            - `api_triggered [bool]`: Indicates whether the commit job was initiated via this API rather than the
              'commit' page on the portal web site.

        Args:
            job_id: The commit job ID, as returned by start_commit(). If an empty string or None, the method will
                retrieve job status for all of your pending commit jobs (if any).
        Returns:
            A 2-tuple (True, jobs), where the `jobs` is a list of job status dictionaries (empty if no jobs found), as
                described above. On failure, returns (False, error message)
        """
        if (out := self.authenticate()) is not None:
            return False, out

        req_body = dict(action='status', job_id="" if not isinstance(job_id, str) else job_id)
        try:
            response = requests.post(f"{self._base_url}{Route.COMMIT}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.COMMIT, response.content)
            if response.status_code == 200:
                return True, content['jobs']
            else:
                return False, content['error']
        except APISerializeError as e:
            return False, f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return False, f"Request failed on send: {str(e)}"

    def commit_remove(self, job_id: str) -> Tuple[bool, str, bool]:
        """
        Cancel and/or remove a pending session commit job on the portal server. You can only remove commit jobs that
        belong to you; the server will deny the request if the specified job belongs to another portal user.

        If the commit job has failed or completed successfully, this merely removes the completed job from your commit
        job registry on the server -- a completed commit is NOT rolled back. If the commit job is currently in the
        upload phase, this will cancel the upload and delete the cancelled job. However, if a background task on the
        server is either preprocessing the session archive or committing the experiment data to the portal database,
        the job is cancelled but not removed. The background task will eventually detect the cancellation and stop
        working, transitioning the job to the "failed" state. At that point, you can call this function again to
        delete the cancelled job from your commit job registry.

        Args:
            job_id: The commit job ID, as returned by start_commit().
        Returns:
            A 3-tuple (True, "", removed), where removed=True if the specified commit has been deleted, False if the
                commit was cancelled but not removed because a background task was still working on it. On failure,
                returns (False, error description, False).
        """
        if (out := self.authenticate()) is not None:
            return False, out, False

        req_body = dict(action='remove', job_id=str(job_id))
        try:
            response = requests.post(f"{self._base_url}{Route.COMMIT}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = Route.deserialize_api_response(Route.COMMIT, response.content)
            if response.status_code == 200:
                return True, "", content['removed']
            else:
                return False, content['error'], False
        except APISerializeError as e:
            return False, f"Failed to decode server response: {str(e)}", False
        except RequestException as e:
            return False, f"Request failed on send: {str(e)}", False
