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
can take advantage of the API with as little "fuss" as possible.

Author: saruffner
"""
import pickle
from datetime import date
from typing import Tuple, Optional, List, Dict, Any

from flask import Response, request
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity

from api.data_containers import API_VERSION
from app import app
from config.app_logging import get_application_logger
from config.config import get_config
from database.table_ops import fetch_restrict_proj, fetch_rows
import database.table_info as ti
from database.user_ops import authenticate_portal_user


@app.server.route('/api', methods=['POST'])
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


@app.server.route('/api/sessions', methods=['POST'])
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
    'sessions' is a list of dictionaries, where each dictionary contains summary information on the experiment sessions
    in the database that match the specified constraints.

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
        get_application_logger().info(f"/api/sessions: {current_user} retrieved metadata on "
                                      f"{len(session_list)} sessions")
    return Response(pickle.dumps(out)), status_code


def _retrieve_session_info(experimenter: Optional[str], subj_id: Optional[str], when: Optional[str]) -> \
        Tuple[int, str, List[Dict[str, Any]]]:
    """
    Helper method for sessions(). Handles the details of fetching information from the portal database IAW the
    restrictions specified and preparing the list of session information dictionaries (which could be an empty one) to
    be returned to the client. This method ensures each session information dictionary is in the form required to
    repackage as a SessionInfo object on the client side.

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
        msg = "A database error occurred while fetching experiment session information from database"
        get_application_logger().error(msg, exc_info=True)
        return 501, msg, []
    study_map = {r['study_id']: r['study_title'] for r in studies}
    area_map = {r['ba_id']: r['ba_name'] for r in brain_areas}

    # combine any Session.EPhys record with the corresponding Session record, map study ID to human readable study
    # title, map brain area ID to area name, and convert session date to ISO formatted string 'YYYY-MM-DD'
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

    return 200, '', rows
