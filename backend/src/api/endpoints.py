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

Committing experiment sessions to the portal database: Authorized users with 'commit'-level access can commit new
experiment sessions to the portal database interactively through a dedicated web page in the portal application, or they
can do it programmatically by sending requests to the /api/commit endpoint. One method in the clientside API handles
initiating a new commit job and uploading the session archive to a staging area in the portal repo in S3; other methods
allow the Python client to check the progress of any pending commit, and cancel/remove a commit job.

Author: saruffner
"""
from datetime import date
from typing import Tuple, Optional, List, Dict, Any, Union

from flask import Response, request
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity

from database.log_ops import log_api_request
from database.repo import initialize_multipart_upload, abort_multipart_upload
from sglportalapi.data_containers import SessionInfo, NeuronInfo, Route, MetadataTable, RequestedData
from app import app
from config.config import get_config
from sglportalapi.maestro import Protocol
from database.table_ops import fetch_restrict_proj, fetch_rows, fetch_any_proj, row_exists
import database.table_info as ti
from database.trial_data_ops import trial_protocols_for_session, retrieve_session_trial_rep, retrieve_session_trial_reps
from database.user_ops import authenticate_portal_user, get_portal_user_record, COMMIT_ACCESS
from database.commit_ops import initiate_session_commit, get_pending_commit_jobs_for, commit_job_status, \
    CommitJobStatus, cancel_or_remove_commit_job, staged_archive_key_in_repo, on_archive_mupload_initialized, \
    abort_archive_mupload, complete_archive_mupload


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
    return Response(Route.serialize_api_response(Route.AUTHENTICATE, **out)), status_code


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
    return Response(Route.serialize_api_response(Route.METADATA_TABLE, **out)), status_code


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
    return Response(Route.serialize_api_response(Route.SESSIONINFO, **out)), status_code


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
        restrictions.append(f"subj_id = '{subj_id}'")
    if isinstance(when, str):
        restrictions.append(f"session_date {when}")
    if len(restrictions) == 0:
        restrictions = None

    rows = fetch_restrict_proj([ti.DBTable.SESSION], [restrictions], [])
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
        r.pop('committed')   # not exposed in SessionInfo.
        found = -1
        for i, ephys in enumerate(ephys_rows):
            if (ephys['experimenter'] == r['experimenter']) and (ephys['subj_id'] == r['subj_id']) and \
                    (ephys['session_date'] == r['session_date']) and (ephys['session_sfx'] == r['session_sfx']):
                found = i
                break
        if found > -1:
            ephys = ephys_rows.pop(found)
            r['ephys_src'] = ephys['ephys_src']
            r['probe_type'] = ephys['probe_type']
            r['sampling_rate'] = ephys['sampling_rate']
            r['probe_x'] = ephys['probe_x']
            r['probe_y'] = ephys['probe_y']
            r['probe_depth'] = ephys['probe_depth']
            r['brain_area'] = area_map[ephys['ba_id']]
            r['ba_id'] = ephys['ba_id']
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
    return Response(Route.serialize_api_response(Route.SESSION_NEURONS, **out)), status_code


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
    return Response(Route.serialize_api_response(Route.SESSION_PROTOCOLS, **out)), status_code


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

    The request body is a JSONified dictionary specifing the primary key of the session, the trial index, a list of
    up to 5 neural unit IDs, and a bit flag set that tailors what recorded data are retrieved. The unit IDs are simply
    integers in 1..N, where N is the number of neural units that were recorded in the session.

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
    what = request.json.get('what')
    what = RequestedData.ALL if not what else RequestedData(what & RequestedData.ALL)

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
                        session_key=session_key, trial_index=trial_index, unit_ids=unit_ids, what=what)
    return Response(Route.serialize_api_response(Route.SESSION_TRIAL, **out)), status_code


@app.server.route(Route.SESSION_BLOCK, methods=['POST'])
@jwt_required()
def session_block() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for a sequential block of trials presented
    during a specified experiment session committed to the portal database. The responses of up to 5 distinct neural
    units recorded during the experiment may be requested with the trial data.

    The request body is a JSONified dictionary specifing the primary key of the session, the first trial index in the
    bloack, the last trial index in the block, and a list of up to 5 neural unit IDs, and a bit flag set that tailors
    what recorded data are retrieved. The unit IDs are simply integers in 1..N, where N is the number of neural units
    that were recorded in the session.

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
    what = request.json.get('what')
    what = RequestedData.ALL if not what else RequestedData(what & RequestedData.ALL)

    status_code, err_msg, trial_list = 200, '', None
    if len(unit_ids) > 5:
        status_code, err_msg = 400, "Too many neural units requested (max is 5)"
    else:
        trial_list = retrieve_session_trial_reps(
            session_key, start=start, end=end, completed=False, unit_ids=unit_ids, what=what)
        if isinstance(trial_list, str):
            status_code, err_msg = 501, trial_list
    out = dict(trials=trial_list) if status_code == 200 else dict(error=err_msg)

    if status_code == 200:
        log_api_request(route=Route.SESSION_BLOCK, username=get_jwt_identity()['username'],
                        session_key=session_key, start=start, end=end, unit_ids=unit_ids, what=what)
    return Response(Route.serialize_api_response(Route.SESSION_BLOCK, **out)), status_code


@app.server.route(Route.SESSION_PROTOCOL_REPS, methods=['POST'])
@jwt_required()
def session_protocol_reps() -> Tuple[Response, int]:
    """
    API access point that retrieves response data and other information for all recorded presentations of a specified
    trial protocol during a specified experiment session committed to the portal database. The responses of up to 5
    distinct neural units recorded during the experiment may be requested with the trial data.

    The request body is a JSONified dictionary specifing the primary key of the session, the MD5 digest hash identifying
    the trial protocol, a flag to restrict the results to successfully completed trial reps only, a list of up to 5
    neural unit IDs, and a bit flag set that tailors what recorded data are retrieved. The unit IDs are simply integers
    in 1..N, where N is the number of neural units that were recorded in the session.

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
    what = request.json.get('what')
    what = RequestedData.ALL if not what else RequestedData(what & RequestedData.ALL)

    status_code, err_msg, trial_list = 200, '', None
    if len(unit_ids) > 5:
        status_code, err_msg = 400, "Too many neural units requested (max is 5)"
    else:
        trial_list = retrieve_session_trial_reps(session_key, proto_hash=proto_hash, completed=completed,
                                                 unit_ids=unit_ids, what=what)
        if isinstance(trial_list, str):
            status_code, err_msg = 501, trial_list
    out = dict(trials=trial_list) if status_code == 200 else dict(error=err_msg)

    if status_code == 200:
        log_api_request(route=Route.SESSION_PROTOCOL_REPS, username=get_jwt_identity()['username'],
                        session_key=session_key, proto_hash=proto_hash, completed=completed, unit_ids=unit_ids,
                        what=what)
    return Response(Route.serialize_api_response(Route.SESSION_PROTOCOL_REPS, **out)), status_code


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
    return Response(Route.serialize_api_response(Route.NEURONS, **out)), status_code


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


@app.server.route(Route.COMMIT, methods=['POST'])
@jwt_required()
def commit() -> Tuple[Response, int]:
    """
    API access point for committing experiment sessions to the portal database, monitoring the progress of pending
    commit jobs, cancelling a commit job in progress, and removing a failed or completed job from the user's commit
    job registry.

    The authenticated user sending a request to this endpoint must have commit-level access to the portal.

    The request body is a dictionary that includes the key 'action', defining the action to be taken. The remaining
    request parameters vary with the action:
       - Start a new session commit: `dict(action='start', session=Dict[str, Any], unit_types=List[str], size=int)`
       - Abort the upload of a session archive after starting a new commit: `dict(action='upload_abort', job_id=str)`.
         This will also cancel the commit job.
       - Complete the upload of a session archive and start preprocessing the commit: `dict(action='upload_done',
         job_id=str, parts=List[Dict])`.
       - Check the progress of a pending commit job, or all jobs belonging to user: `dict(action='status', job_id=str)`.
       - Cancel and/or remove a pending or completed commit job: `dict(action='remove', job_id=str)`.

    For a full discussion of each of these actions in the commit workflow, the required request parameters, and what
    is returned in the response if the operation succeeds, see the relevant helper method for each action.

    Returns:
        Tuple with Flask Response object and HTML status code. On success, the status code is 200 and the response is
            prepared IAW the specific request. Otherwise, the status code is 400 (bad request) or 501 (internal server
            error) and the response body is a serialized dictionary including the field 'error' = <error description
            string>.
    """
    # fail if authenticated user lacks commit-level access
    committer = get_jwt_identity()['username']
    if not _can_commit_to_database(committer):
        out = dict(error="You do not have commit access to database")
        return Response(Route.serialize_api_response(Route.COMMIT, **out)), 400

    action = request.json.get('action')
    req_args = dict(action=action)
    if action == 'start':
        req_args.update([(k, request.json.get(k)) for k in ['session', 'unit_types', 'size']])
        status_code, err_msg, out = _commit_start(committer=committer, session=req_args['session'],
                                                  unit_types=req_args['unit_types'], size=req_args['size'])
    elif action == 'upload_abort':
        req_args['job_id'] = request.json.get('job_id')
        status_code, err_msg, out = _commit_upload_abort(committer=committer, job_id=req_args['job_id'])
    elif action == 'upload_done':
        req_args.update([(k, request.json.get(k)) for k in ['job_id', 'parts']])
        status_code, err_msg, out = \
            _commit_upload_done(committer=committer, job_id=req_args['job_id'], parts=req_args['parts'])
    elif action == 'status':
        req_args['job_id'] = request.json.get('job_id')
        status_code, err_msg, out = _commit_status(committer=committer, job_id=req_args['job_id'])
    elif action == 'remove':
        req_args['job_id'] = request.json.get('job_id')
        status_code, err_msg, out = _commit_cancel_or_remove(committer=committer, job_id=req_args['job_id'])
    else:
        status_code, err_msg, out = 400, f"Unrecognized session commit job request: action={action}", {}

    if status_code == 200:
        log_api_request(route=Route.COMMIT, username=committer, **req_args)
    else:
        out = dict(error=err_msg)
    return Response(Route.serialize_api_response(Route.COMMIT, **out)), status_code


def _can_commit_to_database(committer: str) -> bool:
    """
    Does the specified user have permission to commit experiment sessions to the portal database?

    Args:
        committer: Username of portal user that sent a request to the Route.COMMIT endpoint.
    Returns:
        True if user has commit-level access, else False.
    """
    user_rec = get_portal_user_record(committer)
    return isinstance(user_rec, dict) and (user_rec['access'] in COMMIT_ACCESS)


def _commit_start(committer: str, session: Dict[str, Any], unit_types: List[str], size: int) -> \
        Tuple[int, str, Dict[str, Any]]:
    """
    Helper method for commit() handles the 'start' action, initiating a new session commit job on the portal server.

    Committing experimental data via the commit API endpoint is a multi-step process. First the 'start' request is
    sent, with the required metadata describing the session, along with the size of the session archive ZIP. Then the
    archive file must be uploaded directly to a staging area in the portal's S3-based repository. Upon signaling the
    completion of that upload, the portal server queues a background task to preprocess the archive and commit the
    experimental data to the database. However, if the session includes any trial protocols requiring manual validation
    by the user, the commit job enters an interactive review phase, which must be completed on the 'commit' web page;
    the 'commit' API endpoint does not support the review phase.

    This method handles the first step in the commit workflow -- initiating a new commit job. First, it creates the new
    commit job and adds it to the requesting user's commit job registry on the server. It then initializes a multipart
    upload task on S3. The upload task ID is stored in the commit job's status information so that, once the upload has
    finished, the client side can send the appropriate request to complete the upload, transitioning the commit job to
    the preprocessing phase.

    The session metadata dictionary has the following keys. Note that, if no neural units were recorded -- a so-called
    behavior-only session --, then the electrophysiology-related fields may be omitted.
        - experimenter: str = Username of the registered portal user that conducted the experiment.
        - subject: str = ID of the experiment subject.
        - rec_date: str = Recorded date of experiment in string form as 'YYYY-MM-DD'.
        - suffix: int = Session suffix in 1..9.
        - rig: str = The experiment rig's ID.
        - study: str | int = The title or the unique integer ID of the research study to which the experiment belongs.
        - notes: str = Session notes (free-form).
        - brain_area: Optional[str | int] = The unique name or integer key identifying the brain area in which units
          were recorded.
        - src: Optional[str] = The EPhys recording source. Normally, this is "Omniplex".
        - probe: Optional[str] = The probe type: 'single', '32-channel', or 'other'.
        - rate: Optional[float] = The probe sampling rate in Hz. This is typically 40000 for the Omniplex.
        - x, y, z: Optional[float] = Probe (X,Y) location within implant cylinder, and its insertion depth. In mm.


    If successful, the response dictionary will contain the following keys:
        - action='start'.
        - job_id: str. The unique ID assigned to the new session commit job on the server.
        - urls: List[str]. A list of presigned URLs by which the clientside can upload the session archive in sequential
          "chunks" to a  designated staging area in S3.
        - chunk_size: int. The size of each file chunk (except the last, typically), in bytes.

    Args:
        committer: Username of the requester.
        session: Required session metadata. See description above.
        unit_types: List L such that L[i] is the neuron type assigned to the i-th recorded neural unit as defined in
           a pickle file, prepared by the experimenter, that is part of the session ZIP. The length of L must equal the
           number of recorded units in the archive, and each neuron type name in the list must exist in the portal
           database.
        size: The exact size of the session archive file to be uploaded by the client once the commit job is created.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, response dictionary). On failure, the status
            code is 400 (bad request) or 501 (internal server error), an error description is provided, and the response
            dictionary is empty. On success, the HTTP status code is 200, the error string is empty, and the response
            dictionary is as described above.
    """
    if any([(k not in session) for k in ['experimenter', 'subject', 'rec_date', 'suffix', 'rig', 'study', 'notes']]):
        return 400, "Incomplete session metadata", {}

    ok, job_id = initiate_session_commit(is_api=True, committer=committer, unit_types=unit_types, **session)
    if not ok:
        return 501, job_id, {}

    repo_key = staged_archive_key_in_repo(job_id)
    ok, upload_id, chunk_size, urls = initialize_multipart_upload(size, repo_key)
    if not ok:
        cancel_or_remove_commit_job(job_id)
        return 501, f"Failed to initialize multipart upload task [{upload_id}]", {}

    err_msg = on_archive_mupload_initialized(job_id, upload_id)
    if err_msg is not None:
        abort_multipart_upload(repo_key, upload_id)
        cancel_or_remove_commit_job(job_id)
        return 501, err_msg, {}

    return 200, "", dict(action='start', job_id=job_id, chunk_size=chunk_size, urls=urls)


def _commit_upload_abort(committer: str, job_id: str) -> Tuple[int, str, Dict[str, Any]]:
    """
    Helper method for commit() handles the 'upload_abort' action, aborting the S3 multipart upload task for a session
    commit job, then removing the commit job from the user's job registry on the portal server.

    If successful, the response dictionary is trivial: dict(action='upload_abort'). A successful return indicates that
    the upload was successfully terminated AND the commit job deleted.

    Args:
        committer: Username of the requester.
        job_id: The commit job ID.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, response dictionary). On failure, the status
            code is 400 (bad request) or 501 (internal server error), an error description is provided, and the response
            dictionary is empty. On success, the HTTP status code is 200, the error string is empty, and the response
            dictionary is as described above.
    """
    # make sure user owns the commit job
    job_status: Union[str, CommitJobStatus] = commit_job_status(job_id)
    if isinstance(job_status, str):
        return 501, job_status, {}
    elif job_status.committer != committer:
        return 501, "You do not own this pending commit job", {}

    err_msg = abort_archive_mupload(job_id)
    if err_msg is None:
        _, err_msg, _ = cancel_or_remove_commit_job(job_id)

    if err_msg is not None:
        return 501, err_msg, {}
    else:
        return 200, "", dict(action='upload_abort')


def _commit_upload_done(committer: str, job_id: str, parts: List[Dict]) -> Tuple[int, str, Dict[str, Any]]:
    """
    Helper method for commit() handles the 'upload_done' action, finalizing the S3 multipart upload task for a session
    commit job, then tranistioning the commit job to the preprocessing phase.

    If successful, the response dictionary is trivial: dict(action='upload_done'). A successful return indicates that
    the server has queued a background task to preprocess the session archive.

    Args:
        committer: Username of the requester.
        job_id: The commit job ID.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, response dictionary). On failure, the status
            code is 400 (bad request) or 501 (internal server error), an error description is provided, and the response
            dictionary is empty. On success, the HTTP status code is 200, the error string is empty, and the response
            dictionary is as described above.
    """
    # make sure user owns the commit job
    job_status: Union[str, CommitJobStatus] = commit_job_status(job_id)
    if isinstance(job_status, str):
        return 501, job_status, {}
    elif job_status.committer != committer:
        return 501, "You do not own this pending commit job", {}

    err_msg = complete_archive_mupload(job_id, parts)
    if err_msg is not None:
        return 501, err_msg, {}
    else:
        return 200, "", dict(action='upload_done')


def _commit_status(committer: str, job_id: str) -> Tuple[int, str, Dict[str, Any]]:
    """
    Helper method for commit() handles the 'status' action, retrieving status information for a specified commit job or
    for all pending commit jobs belonging to the requesting user.

    On success, the response dictionary has two keys: action='status' and 'jobs'=List[Dict], a list of job status
    dictionaries. When status for a particular job is requested, 'jobs' is a list containing exactly one status
    dictionary. When retrieving status on all jobs belonging to the user specified, the result could be an empty list if
    no jobs were found.

    Per-job status information is returned as a dictionary with the following keys:
        - job_id: str is the commit job's unique ID.
        - messages: List[str] is the job's progress history, a list of progress messages in reverse chronological order.
        - started: float is the timestamp (seconds since the "epoch") when the commit job was initiated.
        - updated: float is the timestamp when the commit job's progress was last updated.
        - state: str is a string description of the job's current state.
        - api_triggered: bool indicates whether the commit job was initiated via the API endpoint rather than the commit
          web page.

    Args:
        committer: Username of the requester. Only retrieves status info for commit jobs initiated by this user.
        job_id: If this is a non-empty string, then retrieves status information for the specified commit job. Else,
            returns status information on all pending jobs belonging to the user.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, response dictionary). On failure, the status
            code is 400 (bad request) or 501 (internal server error), an error description is provided, and the response
            dictionary is empty. On success, the HTTP status code is 200, the error string is empty, and the response
            dictionary is as described above.
    """
    if (not isinstance(job_id, str)) or (len(job_id) == 0):
        jobs: Union[str, List[CommitJobStatus]] = get_pending_commit_jobs_for(committer)
    else:
        out = commit_job_status(job_id)
        jobs: Union[str, List[CommitJobStatus]] = out if isinstance(out, str) else [out]

    if isinstance(jobs, str):
        return 501, jobs, {}
    else:
        status_dicts = list()
        for j in jobs:
            if j.committer == committer:
                status_dicts.append(dict(
                    job_id=j.id, messages=j.message_history, started=j.started, updated=j.updated,
                    state=j.state.get_state_descriptor(), api_triggered=j.api_triggered
                ))
        return 200, "", dict(action='status', jobs=status_dicts)


def _commit_cancel_or_remove(committer: str, job_id: str) -> Tuple[int, str, Dict[str, Any]]:
    """
    Helper method for commit() handles the 'remove' action, cancelling the specified session commit job and, if
    possible, removing it from the user's commit job registry. If the commit job has already completed successfully,
    this merely removes the job from the user's job registry; the commit is not rolled back.

    If the specified job is still in the upload phase, the S3 multipart upload task associated with the job is
    aborted to ensure that any already uploaded parts are removed from S3.

    On success, the response dictionary has two keys: action='remove' and 'removed'=bool. If the latter is True, then
    either the specified commit job was not found, or it was successfully removed. Otherwise, the job is cancelled but
    could not yet be removed from the server.

    Args:
        committer: Username of the requester. Users can only cancel/remove their own session commit jobs.
        job_id: The job ID.
    Returns:
        A 3-tuple: (HTTP response status code, error description string, response dictionary). On failure, the status
            code is 400 (bad request) or 501 (internal server error), an error description is provided, and the response
            dictionary is empty. On success, the HTTP status code is 200, the error string is empty, and the response
            dictionary is as described above.
    """
    job_status: Union[str, CommitJobStatus] = commit_job_status(job_id)
    if isinstance(job_status, str):
        return 501, job_status, {}
    elif job_status.committer != committer:
        return 501, "You do not have permission to remove this pending commit job", {}
    else:
        # NOTE - if job is in the uploading phase, cancelling the job will also abort the multpart upload task.
        removed, err_msg, _ = cancel_or_remove_commit_job(job_id)
        if len(err_msg) > 0:
            return 501, err_msg, {}
        else:
            return 200, "", dict(action='remove', removed=removed)
