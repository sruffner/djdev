"""
endpoints.py: RESTful-like API endpoints for retrieving information and data from the portal database.

The portal database is primarily an archive for experimental data collected in the Lisberger laboratory. While the web
portal offers graphical views of the data, including aggregate response statistics across repeated trial presentations,
it cannot possibly provide a full analytical suite.

Lab researchers would like to be able to retrieve preprocessed trial data from the database and perform their own
analyses. This module defines API endpoints by which arbitrary trial data sets can be retrieved by analysis scripts.
These are "read-only" endpoints, unlike the web portal pages, they do not expose any functionality that would alter the
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
data containers for the various kinds of information that are retrieved by the API, sent "over the wire" in serialized
form, and reconstituted on the client side.

Author: saruffner
"""
from datetime import date
from typing import Tuple, Optional, List, Dict, Any

from flask import Response, request
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity

from database.log_ops import log_api_request
from sglportalapi.data_containers import SessionInfo, NeuronInfo, Route, serialize_api_response, MetadataTable
from app import app
from config.config import get_config
from sglportalapi.maestro import Protocol
from database.table_ops import fetch_restrict_proj, fetch_rows, fetch_any_proj, row_exists
import database.table_info as ti
from database.trial_data_ops import trial_protocols_for_session, retrieve_session_trial_rep, retrieve_session_trial_reps
from database.user_ops import authenticate_portal_user


@app.server.route(Route.AUTHENTICATE, methods=['POST'])
def api_access() -> Tuple[Response, int]:
    """
    API access point to authenticate a user and obtain an access token for querying all other access points. The request
    body should be a JSON object with fields 'username' and 'password'.

    Returns:
        Tuple with Flask Response object and HTML status code. Upon successful authentication, the status code is 200
            and the response content is a serialized dictionary including fields 'token' = <access token string>, and
            'expires_in' = <token lifetime in seconds>. If user cannot be authenticated, the status code is 400 (bad
            request) and the dictionary includes the field 'error' = <error description string>.
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
            log_api_request(route=Route.AUTHENTICATE, username=username)
        else:
            status_code, out = 400, dict(error=f"Access denied - {err_msg}")
    return Response(serialize_api_response(Route.AUTHENTICATE, **out)), status_code


@app.server.route(Route.METADATA_TABLE, methods=['POST'])
@jwt_required()
def metadata_table() -> Tuple[Response, int]:
    """
    API access point that retrieves the contents of one of several small metadata tables in the portal database. These
    tables contain information used to describe, categorize and search for experimental data sets, such as: experiment
    subjects, subject implants, experiment rigs, research studies, neuron types, and brain areas. The very large
    database tables storing experiment sessions, neural units, trial protocols, and recorded trial response data are
    **not** exposed through this access point.

    The request body is a JSONified dictionary with a single key identifying the information table requested:
        * **table** = <table name>. Must be one of the recognized metadata table names.

    The 'metatable' key in the response dictionary is a :py:class:`sglportalapi.data_containers.MetadataTable` object
    encapsulating the table contents.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' (error description string).
    """
    table_name = request.json.get('table')
    status_code, err_msg, metatable = _retrieve_metadata_table_entries(table_name)
    out = dict(metatable=metatable) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        log_api_request(route=Route.METADATA_TABLE, username=get_jwt_identity()['username'], table=table_name)
    return Response(serialize_api_response(Route.METADATA_TABLE, **out)), status_code


_METATABLE_NAME_TO_DB_TABLE = {
    MetadataTable.SUBJECTS: ti.DBTable.SUBJECT,
    MetadataTable.IMPLANTS: ti.DBTable.IMPLANT,
    MetadataTable.RIGS: ti.DBTable.RIG,
    MetadataTable.STUDIES: ti.DBTable.STUDY,
    MetadataTable.NEURON_TYPES: ti.DBTable.NEURON_TYPE,
    MetadataTable.BRAIN_AREAS: ti.DBTable.BRAIN_AREA
}
""" Maps metadata table nickname to actual database table ID. """


def _retrieve_metadata_table_entries(table_name: str) -> Tuple[int, str, Optional[MetadataTable]]:
    """
    Helper method for metadata_table(). Handles the details of fetching the contents of the specified table from the
    portal database and encapsulating the contents in a MetadataTable object for transfer to the client.

    Args:
        table_name: The name of the metadata table requested.

    Returns:
        A 3-tuple: (status, emsg, table). On failure, the `status` code is 400 (bad request) or 501 (internal server
            error), `emsg` is an error description, and `table` is None. On success, the status code is 200, `emsg` is
            an empty string, and `table` is the MetadataTable object.
    """
    if not MetadataTable.is_supported_table_name(table_name):
        return 400, 'Metadata table name not recognized', None

    table_rows = fetch_rows(_METATABLE_NAME_TO_DB_TABLE[table_name])
    if table_rows is None:
        return 501, "A database error occurred while fetching metadata table contents", None

    try:
        table = MetadataTable.from_database_rows(table_name, table_rows)
        return 200, '', table
    except ValueError as e:
        return 501, f"Unable to prepare metadata table object: {str(e)}", None


@app.server.route(Route.SESSIONINFO, methods=['POST'])
@jwt_required()
def sessions() -> Tuple[Response, int]:
    """
    API access point that retrieves all or a subset of experiment sessions stored in the portal database. The request
    body is a JSONified dictionary with 3 keys defining possible restrictions on the set of sessions returned:
        * **experiments** = None | <registered portal username; restrict to sessions owned by this user>.
        * **subj_id** = None | <subject ID; restrict to sessions involving this experiment subject>.
        * **when** = None | <date restriction of the form 'op YYYY-MM-DD', where op = '=' or '>' or '<'; restrict to
          sessions recorded on, after or before the data specified>.

    If successful, the 'sessions' key in the response dictionary is a list of
    :py:class:`api.data_containers.SessionInfo` objects, each of which contains summary information on an experiment
    session.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    experimenter = request.json.get('experimenter')
    subj_id = request.json.get('subj_id')
    when = request.json.get('when')
    status_code, err_msg, session_list = _retrieve_session_info(experimenter, subj_id, when)
    out = dict(sessions=session_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        log_api_request(route=Route.SESSIONINFO, username=get_jwt_identity()['username'],
                        experimenter=experimenter, subj_id=subj_id, when=when)
    return Response(serialize_api_response(Route.SESSIONINFO, **out)), status_code


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


@app.server.route(Route.SESSION_NEURONS, methods=['POST'])
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

    If successful, the response is a serialized dictionary including the field 'neurons', a list of
    :py:class:`api.data_containers.NeuronInfo` objects, each of which contains summary info on a recorded neural unit
    that satisifes the specified constraints.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    session_key = request.json.get('session_key')
    min_spikes = request.json.get('min_spikes')
    min_snr = request.json.get('min_snr')

    status_code, err_msg, neuron_list = _retrieve_session_neurons(session_key, min_spikes, min_snr)
    out = dict(neurons=neuron_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        log_api_request(route=Route.SESSION_NEURONS, username=get_jwt_identity()['username'],
                        session_key=session_key, min_spikes=min_spikes, min_snr=min_snr)
    return Response(serialize_api_response(Route.SESSION_NEURONS, **out)), status_code


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


@app.server.route(Route.SESSION_PROTOCOLS, methods=['POST'])
@jwt_required()
def session_protocols() -> Tuple[Response, int]:
    """
    API access point that retrieves all distinct trial protocols presented during a particular experiment session
    committed to the portal database. The request body is a JSONified dictionary specifing the primary key of the
    session.

    If successful, the response is a serialized dictionary that includes the field 'protocols', which is a list of
    :py:class:`database.maestro.Protocol` objects, each of which defines a Maestro trial protocol presented at least
    once during the specified experiment.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    session_key = request.json.get('session_key')
    status_code, err_msg, proto_list = _retrieve_session_protocols(session_key)
    out = dict(protocols=proto_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        log_api_request(route=Route.SESSION_PROTOCOLS, username=get_jwt_identity()['username'],
                        session_key=session_key)
    return Response(serialize_api_response(Route.SESSION_PROTOCOLS, **out)), status_code


def _retrieve_session_protocols(session_key: Dict[str, Any]) -> Tuple[int, str, List[Protocol]]:
    """
    Helper method for session_protocols().

    Args:
        session_key: The primary key identifying an experiment session in the database.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, protocol list). On failure, the status code is
            400 (bad request) or 501 (internal server error), an error description is provided, and the protocol list
            is empty. On success: (200, '', protocol list). Each element in the list is a protocol definition.
    """
    # retrieve requested information from the database tables
    proto_hashes = trial_protocols_for_session(session_key)
    if proto_hashes is None:
        return 501, f"A database error occurred while fetching trial protocols for session {session_key}", []
    restrictions = [f"proto_hash = '{h}'" for h in proto_hashes.keys()]

    rows = fetch_any_proj(ti.DBTable.TRIAL_PROTOCOL, restrictions, ['proto_def'])
    if rows is None:
        return 501, f"A database error occurred while fetching trial protocols for session {session_key}", []

    out = [Protocol.from_bytes(r['proto_def']) for r in rows]
    return 200, '', out


@app.server.route(Route.SESSION_TRIAL, methods=['POST'])
@jwt_required()
def session_trial() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for a single trial presented during a specified
    experiment session committed to the portal database. The responses of up to 5 distinct neural units recorded during
    the experiment may be requested.

    The request body is a JSONified dictionary specifing the primary key of the session, the trial index, and a list of
    up to 5 neural unit IDs. The unit IDs are simply integers in 1..N, where N is the number of neural units that were
    recorded in the session.

    If successful, the response is a serialized dictionary including the field 'trial',
    a :py:class:`api.data_containers.TrialRep` object, the data container for the trial information and recorded
    response data.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    session_key = request.json.get('session_key')
    trial_index = request.json.get('trial_index')
    unit_ids = request.json.get('unit_ids')

    status_code, err_msg, trial_rep = 200, '', None
    if len(unit_ids) > 5:
        status_code, err_msg = 400, "Too many neural units requested (max is 5)"
    else:
        trial_rep = retrieve_session_trial_rep(session_key, trial_index, unit_ids)
        if isinstance(trial_rep, str):
            status_code, err_msg = 501, trial_rep
    out = dict(trial=trial_rep) if status_code == 200 else dict(error=err_msg)

    if status_code == 200:
        log_api_request(route=Route.SESSION_TRIAL, username=get_jwt_identity()['username'],
                        session_key=session_key, trial_index=trial_index, unit_ids=unit_ids)
    return Response(serialize_api_response(Route.SESSION_TRIAL, **out)), status_code


@app.server.route(Route.SESSION_BLOCK, methods=['POST'])
@jwt_required()
def session_block() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for a sequential block of trials presented
    during a specified experiment session committed to the portal database. The responses of up to 5 distinct neural
    units recorded during the experiment may be requested with the trial data.

    The request body is a JSONified dictionary specifing the primary key of the session, the starting trial index, the
    number of trial reps to retrieve, and a list of up to 5 neural unit IDs. The unit IDs are simply integers in 1..N,
    where N is the number of neural units that were recorded in the session.

    If successful, the response is a serialized dictionary including the 'trials' field, which is a list of
    :py:class:`api.data_containers.TrialRep` objects.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    session_key = request.json.get('session_key')
    start = request.json.get('start')
    end = request.json.get('end')
    unit_ids = request.json.get('unit_ids')

    status_code, err_msg, trial_list = 200, '', None
    if len(unit_ids) > 5:
        status_code, err_msg = 400, "Too many neural units requested (max is 5)"
    else:
        trial_list = retrieve_session_trial_reps(session_key, start=start, end=end, completed=False, unit_ids=unit_ids)
        if isinstance(trial_list, str):
            status_code, err_msg = 501, trial_list
    out = dict(trials=trial_list) if status_code == 200 else dict(error=err_msg)

    if status_code == 200:
        log_api_request(route=Route.SESSION_BLOCK, username=get_jwt_identity()['username'],
                        session_key=session_key, start=start, end=end, unit_ids=unit_ids)
    return Response(serialize_api_response(Route.SESSION_BLOCK, **out)), status_code


@app.server.route(Route.SESSION_PROTOCOL_REPS, methods=['POST'])
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

    If successful, the response is a serialized dictionary including the field 'trials', which is a list of
    :py:class:`api.data_containers.TrialRep` objects.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    session_key = request.json.get('session_key')
    proto_hash = request.json.get('proto_hash')
    completed = request.json.get('completed')
    unit_ids = request.json.get('unit_ids')

    status_code, err_msg, trial_list = 200, '', None
    if len(unit_ids) > 5:
        status_code, err_msg = 400, "Too many neural units requested (max is 5)"
    else:
        trial_list = retrieve_session_trial_reps(session_key, proto_hash=proto_hash, completed=completed,
                                                 unit_ids=unit_ids)
        if isinstance(trial_list, str):
            status_code, err_msg = 501, trial_list
    out = dict(trials=trial_list) if status_code == 200 else dict(error=err_msg)

    if status_code == 200:
        log_api_request(route=Route.SESSION_PROTOCOL_REPS, username=get_jwt_identity()['username'],
                        session_key=session_key, proto_hash=proto_hash, completed=completed, unit_ids=unit_ids)
    return Response(serialize_api_response(Route.SESSION_PROTOCOL_REPS, **out)), status_code


@app.server.route(Route.NEURONS, methods=['POST'])
@jwt_required()
def neurons() -> Tuple[Response, int]:
    """
    API access point that searches the portal database, across all experiment sessions, for any neural units satisfying
    a set of criteria. The request body is a JSONified dictionary defining the filtering criteria:
     - min_spikes = None | int. Include only those units with a total number of recorded spikes >= this value.
     - min_snr = None | float. Include only those units with SNR >= this value.
     - min_rate = None | float. Include only those units with mean firing rate >= this value.
     - neuron_type = None | str. Include only those units classified as this neuron type.
     - subj_id = None | str. Include only neural units recorded in this experiment subject.
     - study_title = None | str. Include only neural units recorded as a part of this research study.
     - proto_hash = None | str. Include only neural units with trial responses recorded for this trial protocol.
     - min_complete = None | int. Ignored unless proto_hash is specified. Otherwise, include only neural units for
       which response data is available from at least this many successfully completed reps of the specified trial
       protocol.

    If successful, the response is a serialized dictionary including the field 'neurons', a list of
    :py:class:`api.data_containers.NeuronInfo` objects, each of which contains summary info on a recorded neural unit
    that satisifes the specified filter criteria. An empty list is returned if no neuron in the database satisfies the
    criteria. **If NO criteria are specified, this method will return information on every neural unit in the
    database!**

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared as described above. Otherwise, the status code is 400 (bad request) or 501 (internal server error)
            and the response body is a serialized dictionary including the field 'error' = <error description string>.
    """
    min_spikes = request.json.get('min_spikes')
    min_snr = request.json.get('min_snr')
    min_rate = request.json.get('min_rate')
    neuron_type = request.json.get('neuron_type')
    subj_id = request.json.get('subj_id')
    study_title = request.json.get('study_title')
    proto_hash = request.json.get('proto_hash')
    min_complete = request.json.get('min_complete')

    status_code, err_msg, neuron_list = _retrieve_neurons(min_spikes, min_snr, min_rate, neuron_type, subj_id,
                                                          study_title, proto_hash, min_complete)
    out = dict(neurons=neuron_list) if status_code == 200 else dict(error=err_msg)
    if status_code == 200:
        log_api_request(route=Route.NEURONS, username=get_jwt_identity()['username'], min_spikes=min_spikes,
                        min_snr=min_snr, min_rate=min_rate, neuron_type=neuron_type, subj_id=subj_id,
                        study_title=study_title, proto_hash=proto_hash, min_complete=min_complete)
    return Response(serialize_api_response(Route.NEURONS, **out)), status_code


def _retrieve_neurons(min_spikes: Optional[int], min_snr: Optional[float], min_rate: Optional[float],
                      neuron_type: Optional[str], subj_id: Optional[str], study_title: Optional[str],
                      proto_hash: Optional[str], min_complete: Optional[int]) -> Tuple[int, str, List[NeuronInfo]]:
    """
    Helper method for neurons(). Handles the details of fetching information from the portal database IAW the
    restrictions specified and preparing the list of neuron information objects (which could be an empty one) to be
    returned to the client.

    Args:
        min_spikes: If not None, restrict to neurons for which total # of recorded spikes >= this value.
        min_snr: If not None, restrict to neurons for which estimated signal-to-noise ratio >= this value.
        min_rate: If not None, restrict to neurons for which mean firing rate >= this value.
        neuron_type: If not None, restrict to neurons classified as this neuron type.
        subj_id: If not None, restrict to neurons recorded in this experiment subject.
        study_title: If not None, restrict to neurons recorded in experiments for this research study.
        proto_hash: If not None, restrict to neurons recorded during one or more reps of this trial protocol.
        min_complete: If not None AND a protocol is specified, restrict to neurons recorded during at least this many
            successfully completed reps of the specified protocol.

    Returns:
        A 3-tuple: (HTTP response status code, error description string, neuron list). On failure, the status code is
            400 (bad request) or 501 (internal server error), an error description is provided, and the neuron list
            is empty. On success: (200, '', neuron list).
    """
    # need neuron type map in order to prepare response and to possibly filter on neuron type
    neuron_type_rows = fetch_rows(ti.DBTable.NEURON_TYPE)
    if neuron_type_rows is None:
        return 501, f"A database error occurred while fetching neuron types metadata", []
    nt_name_to_id: Dict[str, int] = {r['nt_name']: r['nt_id'] for r in neuron_type_rows}
    nt_id_to_name: Dict[int, str] = {r['nt_id']: r['nt_name'] for r in neuron_type_rows}

    neuron_restrictions = list()
    session_restriction = None
    if isinstance(min_spikes, (float, int)):
        neuron_restrictions.append(f"unit_spikes >= {int(min_spikes)}")
    if isinstance(min_snr, (float, int)):
        neuron_restrictions.append(f"unit_snr >= {float(min_snr)}")
    if isinstance(min_rate, (float, int)):
        neuron_restrictions.append(f"unit_rate >= {float(min_rate)}")
    if isinstance(subj_id, str):
        neuron_restrictions.append(f"subj_id = '{subj_id}'")
    if isinstance(neuron_type, str):
        if not (neuron_type in nt_name_to_id):
            return 400, f"Invalid neuron type specified: {neuron_type}", []
        neuron_restrictions.append(f"unit_type = {nt_name_to_id[neuron_type]}")
    if isinstance(study_title, str):
        studies = fetch_restrict_proj([ti.DBTable.STUDY], [[f"study_title = '{study_title}'"]], None)
        if studies is None:
            return 501, f"A database error occurred while fetching research studies metadata", []
        elif len(studies) == 0:
            return 400, f"Research study '{study_title}' not found in database.", []
        else:
            session_restriction = [f"study_id = {studies[0]['study_id']}"]

    # fetch all Session.Neurons satisfying the constraints set up thus far.
    rows = fetch_restrict_proj([ti.DBTable.SESSION_NEURON, ti.DBTable.SESSION],
                               [neuron_restrictions, session_restriction], [])
    if rows is None:
        return 501, f"A database error occurred while fetching filtered set of neural units", []

    # further restrict to neural units with responses recorded to reps of specified trial protocol
    if isinstance(proto_hash, str):
        if not row_exists(ti.DBTable.TRIAL_PROTOCOL, dict(proto_hash=proto_hash)):
            return 400, f"Specified trial protocol not found in database.", []
        trial_restrictions = dict(proto_hash=proto_hash)
        min_reps = 1
        if isinstance(min_complete, int):
            trial_restrictions['trial_success'] = True
            min_reps = max(1, min_complete)
        accepted_rows = list()
        for r in rows:
            neuron_pk = {k: r[k] for k in ti.primary_key_of(ti.DBTable.SESSION_NEURON, False)}
            reps = fetch_restrict_proj([ti.DBTable.TRIAL, ti.DBTable.TRIAL_NEURONAL], [trial_restrictions, neuron_pk])
            if reps is None:
                return 501, "A database error occurred while fetching protocol reps for a neural unit", []
            if len(reps) >= min_reps:
                accepted_rows.append(r)
    else:
        accepted_rows = rows

    # for each Session.Neuron record in the result, replace neuron type ID with the human readable name, and convert the
    # session date to an ISO formatted string 'YYYY-MM-DD'
    for r in accepted_rows:
        r['neuron_type'] = nt_id_to_name[r['unit_type']]
        r.pop('unit_type', None)
        if isinstance(r['session_date'], date):
            r['session_date'] = r['session_date'].isoformat()

    return 200, '', [NeuronInfo(r) for r in accepted_rows]
