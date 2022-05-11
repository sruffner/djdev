"""
endpoints.py: RESTful-like API endpoints for retrieving information and data from the portal database.

The portal database is primarily an archive for experimental data collected in the Lisberger laboratory. While the web
portal offers graphical views of the data, including aggregate response statistics across repeated trial presentations,
it cannot possibly provide a full analytical suite.

Lab researchers would like to be able to retrieve preprocessed trial data from the database and perform their own
analyses. This module defines API endpoints by which arbitrary trial data sets can be retrieved by analysis scripts.
These are "read-only" endpoints, unlike the web portal pages, they do notexpose any functionality that would alter the
contents of the database.

In order to protect/track data provenance, access to these API endpoints is restricted to authorized users only. A user
must first provide a username and password to the root '/api' endpoint to obtain a JWT (JSON Web Token) access token
that must be included in every subsequent request to all other endpoints. The access token must be included in the
request header: 'Authorization: Bearer <token>'. For security reasons, the access token expires after a relatively
short time (set in application configuration and should be in hours or minutes, not longer). After expiration, access
is denied until a new token is acquired.

The JWT access token is symmetrically signed using the HS256 algorithm. While an asymmetric algorithm is recommended,
the API endpoints will only be accessible via HTTPS, so hopefully that provides sufficient security. We use the
Flask-JWT-Extended library for access token generation and verification.

A companion module, clientside.py, provides Python function calls to access these API endpoints so that lab researchers
can take advantage of the API with as little "fuss" as possible. Another module, data_containers.py, defines simple
data containers for the various kinds of information that are retrieved by the API, sent "over the wire" in pickled
form, and reconstituted on the client side.

Author: saruffner
"""
import pickle
from datetime import date
from typing import Tuple, Optional, List, Dict, Any

from flask import Response, request
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity

from sglportalutils.data_containers import API_VERSION, SessionInfo, NeuronInfo, ROUTE_AUTHENTICATE, ROUTE_SESSIONINFO, \
    ROUTE_SESSION_NEURONS, ROUTE_SESSION_PROTOCOLS, ROUTE_SESSION_TRIAL, TrialRep, ROUTE_SESSION_BLOCK, \
    ROUTE_PROTOCOL_REPS
from app import app
from config.app_logging import get_application_logger
from config.config import get_config
from sglportalutils.maestro import Protocol
from database.table_ops import fetch_restrict_proj, fetch_rows, fetch_any_proj, fetch_one_row
import database.table_info as ti
from database.trial_data_ops import trial_protocols_for_session
from database.user_ops import authenticate_portal_user


@app.server.route(ROUTE_AUTHENTICATE, methods=['POST'])
def api_access() -> Tuple[Response, int]:
    """
    API access point to authenticate a user and obtain an access token for querying all other access points. The request
    body should be a JSON object with fields 'username' and 'password'.

    Returns:
        Tuple with Flask Response object and HTML status code. Upon successful authentication, the status code is 200
            and the response content is a pickled dictionary with 2 fields: 'token' = <access token string>, and
            'expires_in' = <token lifetime in seconds>. If user cannot be authenticated, the status code is 400 (bad
            request) and the dictionary has 1 field: 'error' = <error description string>.
    """
    username = request.json.get('username', None)
    password = request.json.get('password', None)
    if (username is None) or (password is None):
        status_code, out = 400, dict(error="Invalid or missing request body")
    else:
        err_msg = authenticate_portal_user(username, password, api_access=True)
        if err_msg is None:
            status_code, out = 200, dict(token=create_access_token(identity=dict(username=username)),
                                         expires_in=int(get_config().jwt_access_token_lifetime.total_seconds()))
        else:
            status_code, out = 400, dict(error=f"Access denied - {err_msg}")
    return Response(pickle.dumps(out)), status_code


@app.server.route(ROUTE_SESSIONINFO, methods=['POST'])
@jwt_required()
def sessions() -> Tuple[Response, int]:
    """
    API access point that retrieves all or a subset of experiment sessions stored in the portal database. The request
    body is a JSONified dictionary with 3 keys defining possible restrictions on the set of sessions returned:
        * experiments = None | <registered portal username; restrict to sessions owned by this user>.
        * subj_id = None | <subject ID; restrict to sessions involving this experiment subject>.
        * when = None | <date restriction of the form 'op YYYY-MM-DD', where op = '='|'>'|'<'; restrict to sessions
            recorded on, after or before the data specified>.

    If successful, the response is a pickled dictionary with 2 fields: 'version' is the API version number (int), and
    'sessions' is a list of :py:class:`api.data_containers.SessionInfo` objects, each of which contains summary
    information on an experiment session.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a pickled dictionary with a single key: error = <error description string>.
    """
    experimenter = request.json.get('experimenter')
    subj_id = request.json.get('subj_id')
    when = request.json.get('when')
    status_code, err_msg, session_list = _retrieve_session_info(experimenter, subj_id, when)
    out = dict(version=API_VERSION, sessions=session_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        current_user = get_jwt_identity()
        get_application_logger().info(f"{ROUTE_SESSIONINFO}: {current_user} retrieved metadata on "
                                      f"{len(session_list)} sessions")
    return Response(pickle.dumps(out)), status_code


def _retrieve_session_info(experimenter: Optional[str], subj_id: Optional[str], when: Optional[str]) -> \
        Tuple[int, str, List[SessionInfo]]:
    """
    Helper method for sessions(). Handles the details of fetching information from the portal database IAW the
    restrictions specified and preparing the list of session information objects (which could be an empty) to be
    returned to the client.

    Args:
        experimenter: If not None, restrict to sessions belonging to this portal user.
        subj_id: If not None, restrict to sessions involving the experiment subject with this ID.
        when: If not None, restrict to sessions with a recording date satisfying this restriction. It is assumed to
            have the correct format: 'op YYYY-MM-DD', where 'op' is '='|'>'|'<'.

    Returns:
        A 3-tuple: (HTTP response status code, error description string, session list). On failure, the status code is
            400 (bad request) or 501 (internal server error), an error description is provided, and the session list
            is empty. On success: (200, '', session list).
    """
    # retrieve requested information from the database tables
    restrictions = list()
    if isinstance(experimenter, str):
        restrictions.append(f"experimenter = '{experimenter}'")
    if isinstance(subj_id, str):
        restrictions.append(f"subj_id =' {subj_id}'")
    if isinstance(when, str):
        restrictions.append(f"session_date {when}")
    if len(restrictions) == 0:
        restrictions = None

    rows = fetch_restrict_proj([ti.DBTable.SESSION], [restrictions], ['study_id', 'num_units', 'num_trials'])
    ephys_rows = fetch_restrict_proj([ti.DBTable.SESSION_EPHYS], [restrictions], [])
    studies = fetch_restrict_proj([ti.DBTable.STUDY], None, ['study_title'])
    brain_areas = fetch_rows(ti.DBTable.BRAIN_AREA)
    if any([(r is None) for r in [rows, ephys_rows, studies, brain_areas]]):
        return 501, "A database error occurred while fetching experiment session information from database", []
    study_map = {r['study_id']: r['study_title'] for r in studies}
    area_map = {r['ba_id']: r['ba_name'] for r in brain_areas}

    # combine any Session.EPhys record with the corresponding Session record, map study ID to human readable study
    # title, map brain area ID to area name, and convert session date to ISO formatted string 'YYYY-MM-DD'. For
    # behavior-only sessions, all EPhys-related fields are set to None.
    for r in rows:
        r['study_title'] = study_map[r['study_id']]
        r.pop('study_id', None)
        found = -1
        for i, ephys in enumerate(ephys_rows):
            if (ephys['experimenter'] == r['experimenter']) and (ephys['subj_id'] == r['subj_id']) and \
                    (ephys['session_date'] == r['session_date']) and (ephys['session_sfx'] == r['session_sfx']):
                found = i
                break
        if found > -1:
            ephys = ephys_rows.pop(found)
            r['brain_area'] = area_map[ephys['ba_id']]
            r['ephys_src'] = ephys['ephys_src']
            r['probe_type'] = ephys['probe_type']
            r['sampling_rate'] = ephys['sampling_rate']
            r['probe_x'] = ephys['probe_x']
            r['probe_y'] = ephys['probe_y']
            r['probe_depth'] = ephys['probe_depth']

        if isinstance(r['session_date'], date):
            r['session_date'] = r['session_date'].isoformat()

    return 200, '', [SessionInfo(r) for r in rows]


@app.server.route(ROUTE_SESSION_NEURONS, methods=['POST'])
@jwt_required()
def session_neurons() -> Tuple[Response, int]:
    """
    API access point that retrieves information on all or a subset of the neural units recorded during a particular
    experiment session committed to the portal database. The request body is a JSONified dictionary that includes the
    primary key identifying the session, plus two optional keys defining possible restrictions on the set of neural
    units returned:
        * session_key = primary key uniquely identifying an experiment session (dict; required)
        * min_spikes = None | <int; restrict to neural units with at least this many total spikes during session>.
        * min_snr = None | <float; restrict to neural units with estimated SNR equal to or greater than this value>.

    If successful, the response is a pickled dictionary with 2 fields: 'version' is the API version number (int), and
    'neurons' is a list of :py:class:`api.data_containers.NeuronInfo` objects, each of which contains summary info
    on a recorded neural unit that satisifes the specified constraints.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a pickled dictionary with a single key: error = <error description string>.
    """
    session_key = request.json.get('session_key')
    min_spikes = request.json.get('min_spikes')
    min_snr = request.json.get('min_snr')

    status_code, err_msg, neuron_list = _retrieve_session_neurons(session_key, min_spikes, min_snr)
    out = dict(version=API_VERSION, neurons=neuron_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        current_user = get_jwt_identity()
        get_application_logger().info(f"{ROUTE_SESSION_NEURONS}: {current_user} retrieved metadata on "
                                      f"{len(neuron_list)} neurons from session {session_key}")
    return Response(pickle.dumps(out)), status_code


def _retrieve_session_neurons(session_key: Dict[str, Any], min_spikes: Optional[int], min_snr: Optional[float]) -> \
        Tuple[int, str, List[NeuronInfo]]:
    """
    Helper method for session_neurons(). Handles the details of fetching information from the portal database IAW the
    restrictions specified and preparing the list of neuron information objects (which could be an empty one) to be
    returned to the client.

    Args:
        session_key: The primary key identifying an experiment session in the database.
        min_spikes: If not None, restrict to neurons for which total # of recorded spikes >= this value.
        min_snr: If not None, restrict to neurons for which estimated signal-to-noise ratio >= this value.

    Returns:
        A 3-tuple: (HTTP response status code, error description string, neuron list). On failure, the status code is
            400 (bad request) or 501 (internal server error), an error description is provided, and the neuron list
            is empty. On success: (200, '', neuron list).
    """
    # retrieve requested information from the database tables
    restrictions = list()
    restrictions.extend([
        f"experimenter = '{session_key['experimenter']}'", f"subj_id = '{session_key['subj_id']}'",
        f"session_date = '{session_key['session_date']}'", f"session_sfx = '{session_key['session_sfx']}'"
    ])
    if isinstance(min_spikes, (float, int)):
        restrictions.append(f"unit_spikes >= {int(min_spikes)}")
    if isinstance(min_snr, (float, int)):
        restrictions.append(f"unit_snr >= {float(min_snr)}")

    rows = fetch_restrict_proj([ti.DBTable.SESSION_NEURON], [restrictions], [])
    neuron_types = fetch_rows(ti.DBTable.NEURON_TYPE)
    if any([(r is None) for r in [rows, neuron_types]]):
        return 501, f"A database error occurred while fetching info on neural units from session {session_key}", []
    n_type_map = {r['nt_id']: r['nt_name'] for r in neuron_types}

    # for each Session.Neuron record in the result, replace neuron type ID with the human readable name, and convert the
    # session date to an ISO formatted string 'YYYY-MM-DD'
    for r in rows:
        r['neuron_type'] = n_type_map[r['unit_type']]
        r.pop('unit_type', None)
        if isinstance(r['session_date'], date):
            r['session_date'] = r['session_date'].isoformat()

    return 200, '', [NeuronInfo(r) for r in rows]


@app.server.route(ROUTE_SESSION_PROTOCOLS, methods=['POST'])
@jwt_required()
def session_protocols() -> Tuple[Response, int]:
    """
    API access point that retrieves all distinct trial protocols presented during a particular experiment session
    committed to the portal database. The request body is a JSONified dictionary specifing the primary key of the
    session.

    If successful, the response is a pickled dictionary with 2 fields: 'version' is the API version number (int), and
    'protocols' is a list of :py:class:`database.maestro.Protocol` objects, each of which defines a Maestro trial
    protocol presented at least once during the specified experiment.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a pickled dictionary with a single key: error = <error description string>.
    """
    session_key = request.json.get('session_key')
    status_code, err_msg, proto_list = _retrieve_session_protocols(session_key)
    out = dict(version=API_VERSION, protocols=proto_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        current_user = get_jwt_identity()
        get_application_logger().info(f"{ROUTE_SESSION_PROTOCOLS}: {current_user} retrieved the {len(proto_list)} "
                                      f"trial protocols presented during session {session_key}")
    return Response(pickle.dumps(out)), status_code


def _retrieve_session_protocols(session_key: Dict[str, Any]) -> Tuple[int, str, List[Protocol]]:
    """
    Helper method for session_protocols().

    Args:
        session_key: The primary key identifying an experiment session in the database.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, protocol list). On failure, the status code is
            400 (bad request) or 501 (internal server error), an error description is provided, and the protocol list
            is empty. On success: (200, '', protocol list). Each element in the list is the pickled representation of
            a maestro.Protocol object.
    """
    # retrieve requested information from the database tables
    proto_hashes = trial_protocols_for_session(session_key)
    if proto_hashes is None:
        return 501, f"A database error occurred while fetching trial protocols for session {session_key}", []
    restrictions = [f"proto_hash = '{h}'" for h in proto_hashes.keys()]

    rows = fetch_any_proj(ti.DBTable.TRIAL_PROTOCOL, restrictions, ['proto_def'])
    if rows is None:
        return 501, f"A database error occurred while fetching trial protocols for session {session_key}", []

    out = [pickle.loads(r['proto_def']) for r in rows]
    return 200, '', out


@app.server.route(ROUTE_SESSION_TRIAL, methods=['POST'])
@jwt_required()
def session_trial() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for a single trial presented during a specified
    experiment session committed to the portal database. The responses of up to 5 distinct neural units recorded during
    the experiment may be requested.

    The request body is a JSONified dictionary specifing the primary key of the session, the trial index, and a list of
    up to 5 neural unit IDs. The unit IDs are simply integers in 1..N, where N is the number of neural units that were
    recorded in the session.

    If successful, the response is a pickled dictionary with 2 fields: 'version' is the API version number (int), and
    'trial' is a :py:class:`api.data_containers.TrialRep` object, the data container for the trial information and
    recorded responses.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a pickled dictionary with a single key: error = <error description string>.
    """
    session_key = request.json.get('session_key')
    trial_index = request.json.get('trial_index')
    unit_ids = request.json.get('unit_ids')
    status_code, err_msg, trial_rep = _retrieve_session_trial(session_key, trial_index, unit_ids)
    out = dict(version=API_VERSION, trial=trial_rep) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        current_user = get_jwt_identity()
        get_application_logger().info(f"{ROUTE_SESSION_TRIAL}: {current_user} retrieved data for trial {trial_index} "
                                      f"from session {session_key}. Units requested = {unit_ids}")
    return Response(pickle.dumps(out)), status_code


def _retrieve_session_trial(
        session_key: Dict[str, Any], trial_index: int, unit_ids: List[int]) -> Tuple[int, str, Optional[TrialRep]]:
    """
    Helper method for session_trial(). The behavioral and neuronal responses, along with other metadata about the
    specified trial rep, are packaged in a :py:class:`api.data_containers.TrialRep` object.

    Args:
        session_key: The primary key identifying an experiment session in the database. The session date is a string
            in ISO format.
        trial_index: Index of the trial rep to be retrieved.
        unit_ids: List of IDs of up to 5 neural units for which response data (ie, spike trains) is requested.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, trial rep). On failure, the status code is
            400 (bad request) or 501 (internal server error), an error description is provided, and the trial rep is
            None. On success: (200, '', trial rep).
    """
    if len(unit_ids) > 5:
        return 400, "Too many neural units requested (max is 5)", None
    try:
        trial_pk = session_key.copy()
        trial_pk['trial_idx'] = trial_index
        trial_row = fetch_one_row(ti.DBTable.TRIAL, trial_pk)
        if trial_row is None:
            get_application_logger().error(f"Trial ({trial_pk}) not found in database!")
            return 501, "Requested trial rep not found, or database error", None
        proto_info = fetch_one_row(ti.DBTable.TRIAL_PROTOCOL, dict(proto_hash=trial_row['proto_hash']))
        if proto_info is None:
            get_application_logger().error(f"Trial protocol (hash={trial_row['proto_hash']}) not found in database!")
            return 501, "Protocol for trial rep not found, or database error", None
        behavioral_responses = fetch_rows(ti.DBTable.TRIAL_BEHAVIORAL, trial_pk)
        neuronal_responses = fetch_rows(ti.DBTable.TRIAL_NEURONAL, trial_pk)
        events = fetch_rows(ti.DBTable.TRIAL_EVENT, trial_pk)
        if (behavioral_responses is None) or (neuronal_responses is None) or (events is None):
            get_application_logger().error(f"DB error occurred while retrieving response data for trial {trial_pk}")
            return 501, "An internal database error occured", None

        # fix the fields of trial_info to match what is expected for the TrialRep container
        trial_info: Dict[str, Any] = dict()
        for k in trial_row.keys():
            trial_info[k] = trial_row[k]
        trial_info['session_date'] = trial_pk['session_date']  # want the date as an ISO-formatted string
        trial_info.pop('trial_header', None)  # don't need the trial header object
        trial_info['protocol'] = pickle.loads(proto_info['proto_def'])  # want the Protocol, not just its MD5 digest
        trial_info.pop('proto_hash', None)
        trial_info['trial_rvs'] = pickle.loads(trial_info['trial_rvs'])  # unpickle the RV values list
        trial_info['trial_success'] = (trial_info['trial_success'] != 0)   # DJ stores bool as int
        trial_info['trial_rewarded'] = (trial_info['trial_rewarded'] != 0)

        trial_info['hgpos'], trial_info['vepos'], trial_info['hevel'], trial_info['vevel'] = None, None, None, None
        for response in behavioral_responses:
            if response['response_id'] == 'HEPOS':
                trial_info['hgpos'] = response['response_trace']
            elif response['response_id'] == 'VEPOS':
                trial_info['vepos'] = response['response_trace']
            elif response['response_id'] == 'HEVEL':
                trial_info['hevel'] = response['response_trace']
            elif response['response_id'] == 'VEVEL':
                trial_info['vevel'] = response['response_trace']

        trial_info['spike_trains'] = dict()
        if len(unit_ids) > 0:
            # for any unit requested that was not recorded during trial, spike times array must be None!
            for i in unit_ids:
                trial_info['spike_trains'][i] = None
            for response in neuronal_responses:
                if response['unit_id'] in unit_ids:
                    trial_info['spike_trains'][response['unit_id']] = response['spike_times']

        trial_info['events'] = dict()
        for event_row in events:
            trial_info['events'][event_row['event_ch']] = event_row['event_times']

        return 200, '', TrialRep(trial_info)
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return 501, f"An internal database error occured [{str(e)}]", None


@app.server.route(ROUTE_SESSION_BLOCK, methods=['POST'])
@jwt_required()
def session_block() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for a sequential block of trials presented
    during a specified experiment session committed to the portal database. The responses of up to 5 distinct neural
    units recorded during the experiment may be requested with the trial data.

    The request body is a JSONified dictionary specifing the primary key of the session, the starting trial index, the
    number of trial reps to retrieve, and a list of up to 5 neural unit IDs. The unit IDs are simply integers in 1..N,
    where N is the number of neural units that were recorded in the session.

    If successful, the response is a pickled dictionary with 2 fields: 'version' is the API version number (int), and
    'trials' is a list of :py:class:`api.data_containers.TrialRep` objects.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a pickled dictionary with a single key: error = <error description string>.
    """
    session_key = request.json.get('session_key')
    start = request.json.get('start')
    end = request.json.get('end')
    unit_ids = request.json.get('unit_ids')
    status_code, err_msg, trial_list = _retrieve_session_trial_reps(
        session_key, start=start, end=end, completed=False, unit_ids=unit_ids)
    out = dict(version=API_VERSION, trials=trial_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        current_user = get_jwt_identity()
        get_application_logger().info(
            f"{ROUTE_SESSION_BLOCK}: {current_user} retrieved data for a {len(trial_list)}-trial block starting at "
            f"index {start} from session {session_key}. Units requested = {unit_ids}")
    return Response(pickle.dumps(out)), status_code


@app.server.route(ROUTE_PROTOCOL_REPS, methods=['POST'])
@jwt_required()
def session_protocol_reps() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for all recorded presentations of a specified
    trial protocol during a specified experiment session committed to the portal database. The responses of up to 5
    distinct neural units recorded during the experiment may be requested with the trial data.

    The request body is a JSONified dictionary specifing the primary key of the session, the MD5 digest hash identifying
    the trial protocol, a flag to restrict the results to successfully completed trial reps only, and a list of up to 5
    neural unit IDs. The unit IDs are simply integers in 1..N, where N is the number of neural units that were recorded
    in the session.

    If successful, the response is a pickled dictionary with 2 fields: 'version' is the API version number (int), and
    'trials' is a list of :py:class:`api.data_containers.TrialRep` objects.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a pickled dictionary with a single key: error = <error description string>.
    """
    session_key = request.json.get('session_key')
    proto_hash = request.json.get('proto_hash')
    completed = request.json.get('completed')
    unit_ids = request.json.get('unit_ids')
    status_code, err_msg, trial_list = _retrieve_session_trial_reps(
        session_key, proto_hash=proto_hash, start=1, end=1, completed=completed, unit_ids=unit_ids)
    out = dict(version=API_VERSION, trials=trial_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        current_user = get_jwt_identity()
        get_application_logger().info(
            f"{ROUTE_PROTOCOL_REPS}: {current_user} retrieved data for {len(trial_list)} reps of trial protocol "
            f"(md5={proto_hash}) preented during session {session_key}. Units requested = {unit_ids}")
    return Response(pickle.dumps(out)), status_code


def _retrieve_session_trial_reps(
        session_key: Dict[str, Any], proto_hash: Optional[str] = None, start: int = 1, end: int = -1,
        completed: bool = True, unit_ids: Optional[List[int]] = None) -> Tuple[int, str, List[TrialRep]]:
    """
    Helper method for session_block() and session_protocol_reps(). The behavioral/neuronal responses and metadata for
    each rep of the specified trial block -- OR belonging to the specified protocol -- are packaged in a
    :py:class:`api.data_containers.TrialRep` object.

    Args:
        session_key: The primary key identifying an experiment session in the database. The session date is a string
            in ISO format.
        proto_hash: If not None, restrict trial list to all reps of the trial protocol identified by this MD5 hash. In
            this case, the arguments defining a sequential block of trials are ignored. Default = None.
        start: Index of first trial in a sequential trial block. Ignored if 'proto_hash' is specified.
        end: Index of last trial in a sequential trial block. Ignored if 'proto_hash' is specified.
        completed: If true, only successfully completed trial reps are included in the results. Default = True.
        unit_ids: List of IDs of up to 5 neural units for which response data (ie, spike trains) is requested, or None.
            Default = None (no neural unit response data requested).
    Returns:
        A 3-tuple: (HTTP response status code, error description string, list of trial reps). On failure, the status
            code is 400 (bad request) or 501 (internal server error), an error description is provided, and the list is
            empty. On success, the status code is 200 and the error string is empty.
    """
    if isinstance(unit_ids, list) and len(unit_ids) > 5:
        return 400, "Too many neural units requested (max is 5)", []
    try:
        session_info = fetch_one_row(ti.DBTable.SESSION, session_key)
        if session_info is None:
            raise ValueError("Session not found, or internal database error")

        # we're either getting all reps of a single protocol, or all reps in a sequential block
        proto: Optional[Protocol] = None
        if proto_hash is not None:
            proto_row = fetch_one_row(ti.DBTable.TRIAL_PROTOCOL, dict(proto_hash=proto_hash))
            if proto_row is None:
                return 501, f"Trial protocol (hash={proto_hash}) not found in database!", []
            proto = pickle.loads(proto_row['proto_def'])
            trial_restrictions = session_key.copy()
            trial_restrictions['proto_hash'] = proto_hash
            if completed:
                trial_restrictions['trial_success'] = True
        else:
            if (start < 1) or (start > session_info['num_trials']) or (start > end):
                return 400, f"Bad trial block range: [{start} .. {end}]", []
            trial_restrictions = [
                f'experimenter = "{session_key["experimenter"]}"',
                f'subj_id = "{session_key["subj_id"]}"',
                f'session_date = "{str(session_key["session_date"])}"',
                f'session_sfx = {session_key["session_sfx"]}',
                f'trial_idx >= {start}', f"trial_idx <= {min(end, session_info['num_trials'])}"
            ]

        relevant_trials = fetch_restrict_proj([ti.DBTable.TRIAL], [trial_restrictions], [])
        if relevant_trials is None:
            return 501, "An internal error occurred while retrieving trial reps", []
        relevant_trials.sort(key=lambda x: x['trial_idx'], reverse=True)
        behavioral_responses = \
            fetch_restrict_proj([ti.DBTable.TRIAL_BEHAVIORAL, ti.DBTable.TRIAL], [None, trial_restrictions], [])
        if behavioral_responses is None:
            return 501, "An internal error occurred while retrieving behavioral responses for trial reps", []
        behavioral_responses.sort(key=lambda x: x['trial_idx'], reverse=True)
        events = \
            fetch_restrict_proj([ti.DBTable.TRIAL_EVENT, ti.DBTable.TRIAL], [None, trial_restrictions], [])
        if events is None:
            return 501, "An internal error occurred while retrieving marker events for trial reps", []
        events.sort(key=lambda x: x['trial_idx'], reverse=True)

        units: Dict[int, List[Dict[str, Any]]] = dict()
        if isinstance(unit_ids, list) and (len(unit_ids) > 0):
            neuron_pk = session_key.copy()
            for unit_id in unit_ids:
                neuron_pk['unit_id'] = unit_id
                res = fetch_restrict_proj([ti.DBTable.TRIAL_NEURONAL, ti.DBTable.TRIAL],
                                          [neuron_pk, trial_restrictions], [])
                if res is None:
                    return 501, "An internal error occurred while retrieving neural response data for trial reps", []
                res.sort(key=lambda x: x['trial_idx'], reverse=True)
                units[unit_id] = res

        protocols: Dict[str, Protocol] = dict()
        trial_reps: List[TrialRep] = list()
        while len(relevant_trials) > 0:
            trial_row = relevant_trials.pop()
            trial_info: Dict[str, Any] = dict()
            for k in trial_row.keys():
                trial_info[k] = trial_row[k]
            trial_info['session_date'] = session_key['session_date']  # want the date as an ISO-formatted string
            trial_info.pop('trial_header', None)  # don't need the trial header object
            trial_info['trial_rvs'] = pickle.loads(trial_info['trial_rvs'])  # unpickle the RV values list
            trial_info['trial_success'] = (trial_info['trial_success'] != 0)   # DJ stores bool as int
            trial_info['trial_rewarded'] = (trial_info['trial_rewarded'] != 0)

            # when retrieving a sequential block of trials, the protocol will typically be different for each rep
            if proto is None:
                if not (trial_info['proto_hash'] in protocols):
                    proto_row = fetch_one_row(ti.DBTable.TRIAL_PROTOCOL, dict(proto_hash=trial_info['proto_hash']))
                    if proto_row is None:
                        return 501, "An internal error occurred while retrieving a trial protocol object", []
                    protocols[trial_info['proto_hash']] = pickle.loads(proto_row['proto_def'])
                trial_info['protocol'] = protocols[trial_info['proto_hash']]
            else:
                trial_info['protocol'] = proto
            trial_info.pop('proto_hash', None)

            trial_info['hgpos'], trial_info['vepos'], trial_info['hevel'], trial_info['vevel'] = None, None, None, None
            while (len(behavioral_responses) > 0) and \
                    (behavioral_responses[-1]['trial_idx'] == trial_info['trial_idx']):
                response = behavioral_responses.pop()
                if response['response_id'] == 'HEPOS':
                    trial_info['hgpos'] = response['response_trace']
                elif response['response_id'] == 'VEPOS':
                    trial_info['vepos'] = response['response_trace']
                elif response['response_id'] == 'HEVEL':
                    trial_info['hevel'] = response['response_trace']
                elif response['response_id'] == 'VEVEL':
                    trial_info['vevel'] = response['response_trace']

            trial_info['events'] = dict()
            while (len(events) > 0) and (events[-1]['trial_idx'] == trial_info['trial_idx']):
                event_row = events.pop()
                trial_info['events'][event_row['event_ch']] = event_row['event_times']

            trial_info['spike_trains'] = dict()
            for unit_id in (unit_ids if isinstance(unit_ids, list) else []):
                u = units[unit_id]
                # IMPORTANT: A given unit may not have been recorded during a given trial.
                if (len(u) > 0) and u[-1]['trial_idx'] == trial_info['trial_idx']:
                    spike_times = u.pop()['spike_times']
                else:
                    spike_times = None
                trial_info['spike_trains'][unit_id] = spike_times

            trial_reps.append(TrialRep(trial_info))

        return 200, '', trial_reps
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return 501, f"An internal error occured [{str(e)}]", []
