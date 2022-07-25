"""
manage_api_requests_log.py: Management page that displays the portal's API requests log.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
displays the contents of the portal's API requests log, a dedicated file in the portal workspace in which all API client
package downloads and all successful requests to the portal's API endpoints are recorded.

Currently this page displays an unfiltered tabular view of all entries in the API requests log, listed in reverse
chronological order. As the log grows with use, we will likely need to revisit both the implementation of the log and
this view.

The tabular view of the API request entries is very terse and intended only for administrative use. It has 4 columns:
 - 'User': The username of the registered portal user that made the request.
 - 'Time': The date/time stamp of the request.
 - 'Request': The request type. One of 'API client download', 'sessions', 'session_neurons', 'session_protocols',
   'session_trial', 'sesion_trial_block', 'session_protocol_reps'. With the exception of the first, these are the
   names of the clientside API function that sends the request.
 - 'Params': List of request parameters -- varies with the request type.
"""
from datetime import datetime
from typing import Optional, List, Dict, Union, Any

from dash import html, dash_table as dt
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database.log_ops import read_log_entries
from sglportalapi.data_containers import Route

_API_LOG_TABLE_ID: str = "api-log-table"
""" ID of Dash DataTable listing all entries read from the API requests log. """

_API_LOG_TABLE_COLS: List[ti.Column] = [
    ti.Column('uname', 'User', '100px', True),
    ti.Column('ts', 'Time', '200px', True),
    ti.Column('desc', 'Request', '200px', True),
    ti.Column('params', 'Parameters', '500px', True)
]
""" Defined columns for the API requests log table. """

_OP_ALERT_ID: str = "api-log-op-alert"
""" ID of Bootstrap Alert that displays error message at top of page if an error occurs. """


def _fetch_api_requests_log() -> Union[str, List[Dict[str, str]]]:
    """
    Helper method fetches all entries from the portal's API requests log and prepares the information for display in a
    Dash DataTable.
    """
    entries: List[Dict[str, Any]]
    try:
        entries = read_log_entries(is_api_log=True)
    except Exception:
        return "Unable to retrieve contents of portal's API requests log. Consult application logs."

    rows = list()
    for entry in reversed(entries):
        route = entry['route']
        if route == '/api_client':
            desc, params = 'API client package download', ''
        elif route == Route.AUTHENTICATE:
            desc, params = 'API access granted', ''
        elif route == Route.SESSIONINFO:
            desc = "**sessions**"
            params = f"**experimenter**={entry['experimenter']}, **subj_id**={entry['subj_id']}, " \
                     f"**when**={entry['when']}"
        elif route == Route.METADATA_TABLE:
            desc = f"**metadata_table**"
            params = f"**table**={entry['table']}"
        else:
            session_key = f"**session**={entry['session_key']}"
            if route == Route.SESSION_NEURONS:
                desc = "**session_neurons**"
                params = f"{session_key}, **min_spikes**={entry['min_spikes']}, **min_snr**={entry['min_snr']}"
            elif route == Route.SESSION_PROTOCOLS:
                desc = f"**session_protocols**"
                params = f"{session_key}"
            elif route == Route.SESSION_TRIAL:
                desc = f"**session_trial**"
                params = f"{session_key}, **trial_index**={entry['trial_index']}, **unit_ids**={entry['unit_ids']}"
            elif route == Route.SESSION_BLOCK:
                desc = f"**session_trial_block**"
                params = f"{session_key}, **start**={entry['start']}, **end**={entry['end']}, " \
                         f"**unit_ids**={entry['unit_ids']}"
            elif route == Route.SESSION_PROTOCOL_REPS:
                desc = f"**session_protocol_reps**"
                params = f"{session_key}, **proto_hash**={entry['proto_hash']}, **completed**={entry['completed']}, " \
                         f"**unit_ids**={entry['unit_ids']}"
            else:
                desc, params = "unknown", ""
        timestamp = datetime.fromisoformat(entry['ts']).isoformat(sep=' ', timespec='seconds')
        rows.append(dict(uname=f"***{entry['username']}***", ts=timestamp, desc=desc, params=params))

    return rows


def _table_of_api_log_entries(user_is_admin: bool) -> html.Div:
    """
    Prepare the Dash DataTable displaying all entries from the API requests log.

    Args:
        user_is_admin: True only if current user has admin privileges on portal.
    Returns:
        An HTML Div with a read-only Dash Datatable listing all entries in the portal's API requests log.
    """
    rows = []
    error_msg = None
    if not user_is_admin:
        error_msg = "You are not authorized to view the API requests log."
    else:
        rows = _fetch_api_requests_log()
        if isinstance(rows, str):
            error_msg = rows
            rows = []

    data_table = dt.DataTable(
        id=_API_LOG_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _API_LOG_TABLE_COLS],
        data=rows,
        row_selectable=False,
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _API_LOG_TABLE_COLS],
        style_data_conditional=[
            {
                'if': {'row_index': 'odd'},
                'backgroundColor': 'rgba(176, 196, 222, 0.25)',
            }
        ],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '500px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    alert = dbc.Alert(error_msg, id=_OP_ALERT_ID, color='danger', dismissable=True, fade=True,
                      is_open=(error_msg is not None), class_name="mb-3")
    return html.Div([alert, data_table])


def serve_layout() -> html.Div:
    """
    Serve the layout for the "API Requests Log" page. The page includes a table listing all entries from the portal's
    API requests log. The page content is read-only at this time. Only authorized users with administrative privileges
    should have access to this page.

    Returns:
        An HTML Div rendering the user account management page.
    """
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()
    return _table_of_api_log_entries(is_admin)
