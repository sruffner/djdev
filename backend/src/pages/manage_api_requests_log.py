"""
manage_api_requests_log.py: Management page that displays the portal's API requests logs.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
displays the contents of the portal's API requests history, which is stored in one or more binary log files:
    - The most recent API requests are logged in a file in the portal server's local workspace. This log is tagged as
      the "Recent" logs.
    - Timestamped logs stored in the portal repository. When the dedicated log file on the portal server exceeds a
      certain size, it is backed up to the portal repository in S3 at /logs/api_requests.log-<TS>, where <TS> is a
      timestamp string. The log file on the server itself is then truncated to 0 to accept new log entries. The older
      logs are tagged byh their timestamp, which has the form "YYYYMonDD-HH:MM".

Currently, this page provides access to any of these API request logs, displaying an unfiltered tabular view of all
entries in the selected log, listed in reverse chronological order. The tabular view is very terse and intended only for
administrative use. It has 4 columns:
    - 'User': The username of the registered portal user that made the request.
    - 'Time': The date/time stamp of the request.
    - 'Request': The request type. One of 'API client download', 'sessions', 'session_neurons', 'session_protocols',
      'commit', and so on. With the exception of the first, these are the names of the clientside API function that
      sends the request.
    - 'Params': List of request parameters -- varies with the request type.
"""
from datetime import datetime
from typing import Optional, List, Dict, Any

import dash.exceptions
from dash import html, dash_table as dt, Output, Input, callback, State, no_update, callback_context
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database.log_ops import get_api_request_log_contents, list_api_request_logs, \
    delete_api_request_log
from database.table_ops import fetch_attribute_values
from sglportalapi.data_containers import Route

_SELECT_ID: str = "api-log-select"
""" ID of Bootstrap Select used to select a particular API request log for viewing. """
_DELETE_ID: str = "api-log-delete-btn"
""" ID of button that deletes the currently selected API request log. """
_LOADING_SPINNER_ID: str = "api-log-spinner"
""" 
ID of a Bootstrap Spinner component that appears when the application log list is being retrieved or a selected log's 
content is being retrieved for display. These operations may take a little while.
"""
_API_LOG_TABLE_ID: str = "api-log-table"
""" ID of Dash DataTable listing all entries read from a selected API requests log. """
_API_LOG_TABLE_COLS: List[ti.Column] = [
    ti.Column('uname', 'User', '100px', True),
    ti.Column('ts', 'Time', '200px', True),
    ti.Column('desc', 'Request', '200px', True),
    ti.Column('params', 'Parameters', '500px', True)
]
""" Defined columns for the API requests log table. """

_FILTER_USER_SEL: str = "api-filt-user-sel"
""" ID of Bootstrap Select used to filter API requests log table by the requesting user. """
_FILTER_REQ_SEL: str = "api-filt-req-sel"
""" ID of Bootstrap Select used to filter API requests log table by the request route. """
_CLEAR_FILTERS_BTN: str = "api-filt-clr"
""" ID of button that clears any filters applied to the API requests log table. """
_FILTER_UNUSED: str = "<none>"
""" Selection value indicating that the filter is not used. """


def serve_layout() -> html.Div:
    """
    Serve the layout for the "API Requests Log" page. The page includes a dropdown menu to select which log to view, a
    Dash DataTable for perusing the content of the selected log file, and a button to delete any older log files backed
    up in the portal repository. Only authorized users with administrative privileges should have access to this page.

    Returns:
        An HTML Div rendering the page.
    """
    select_grp = dbc.InputGroup([
        dbc.InputGroupText("Select log"),
        dbc.Select(
            id=_SELECT_ID,
            options=[],
            value=None
        )
    ])

    remove_btn = dbc.Button("Delete permanently", id=_DELETE_ID, disabled=True, class_name='me-2')
    control_row = dbc.Row([
        dbc.Col(dbc.Row([dbc.Col(select_grp, width='auto')], class_name='g-0'), width='auto', class_name='me-4'),
        dbc.Col([remove_btn], width='auto')
    ], justify='between', class_name='mb-3')

    data_table = dt.DataTable(
        id=_API_LOG_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _API_LOG_TABLE_COLS],
        data=[],
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

    clear_btn = dbc.Button("Clear Filters", id=_CLEAR_FILTERS_BTN, disabled=True, size='sm')

    # note: fail silently if we can't get user list; just won't be able to filter by user.
    users = fetch_attribute_values(ti.DBTable.USER, 'username')
    users.sort()
    users.insert(0, _FILTER_UNUSED)
    filter_user_sel_grp = dbc.InputGroup([
        dbc.InputGroupText("User ="),
        dbc.Select(
            id=_FILTER_USER_SEL,
            options=[{"label": u, "value": u} for u in users],
            value=_FILTER_UNUSED
        )
    ], size='sm')

    options = [{'label': v, 'value': k} for k, v in Route.route_descriptors().items()]
    options.sort(key=lambda x: x['label'])
    options.insert(0, {'label': _FILTER_UNUSED, 'value': _FILTER_UNUSED})
    filter_req_sel_grp = dbc.InputGroup([
        dbc.InputGroupText("Request ="),
        dbc.Select(
            id=_FILTER_REQ_SEL,
            options=options,
            value=_FILTER_UNUSED
        )
    ], size='sm')

    filter_row = dbc.Row([
        dbc.Col(clear_btn, width='auto', class_name='me-4'),
        dbc.Col(filter_user_sel_grp, width='auto', class_name='me-2'),
        dbc.Col(filter_req_sel_grp, width='auto')
    ], class_name='g-0 mt-3')

    return html.Div([
        control_row,
        dbc.Spinner(data_table, id=_LOADING_SPINNER_ID, type='border', delay_hide=250, delay_show=250, color='info'),
        filter_row
    ])


@callback([Output(_API_LOG_TABLE_ID, "data"), Output(_DELETE_ID, "disabled"), Output(_CLEAR_FILTERS_BTN, "disabled")],
          [Input(_SELECT_ID, "value"), Input(_FILTER_USER_SEL, "value"), Input(_FILTER_REQ_SEL, "value")],
          [State(_SELECT_ID, "value"), State(_FILTER_USER_SEL, "value"), State(_FILTER_REQ_SEL, "value")])
def on_select_log(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if trigger_id == "":
        raise dash.exceptions.PreventUpdate
    else:

        sel_value = args[0 if trigger_id == _SELECT_ID else 3]
        username = args[1 if trigger_id == _FILTER_USER_SEL else 4]
        route = args[2 if trigger_id == _FILTER_REQ_SEL else 5]
        clear_disabled = (username == _FILTER_UNUSED) and (route == _FILTER_UNUSED)

        portal_user: Optional[PortalUser] = None
        if flask_login.current_user.is_authenticated:
            portal_user = load_authorized_user(flask_login.current_user.get_id())
        is_admin = (portal_user is not None) and portal_user.is_admin()
        if not is_admin:
            return [], True, clear_disabled
        table_rows = _fetch_api_requests_log(sel_value,
                                             username=None if username == _FILTER_UNUSED else username,
                                             route=None if route == _FILTER_UNUSED else route)
        return table_rows, sel_value == 'Recent', clear_disabled


@callback([Output(_FILTER_USER_SEL, "value"), Output(_FILTER_REQ_SEL, "value")],
          Input(_CLEAR_FILTERS_BTN, "n_clicks"), prevent_initial_call=True)
def on_clear_filters(n_clear):
    if n_clear:
        return _FILTER_UNUSED, _FILTER_UNUSED
    else:
        return no_update, no_update


@callback(
    [Output(_SELECT_ID, "value"), Output(_SELECT_ID, "options")],
    [Input(_DELETE_ID, "n_clicks")], [State(_SELECT_ID, "value"), State(_SELECT_ID, "options")]
)
def on_delete_selected_log(n_clicks, sel_value, curr_options):
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()

    if n_clicks is None:
        # initial load -- get list of API request logs, but only if user has admin access.
        if not is_admin:
            return "Recent", [{'label': 'Recent', 'value': 'Recent'}]
        log_names = list_api_request_logs()
        options = [{'label': name, 'value': name} for name in log_names]
        return "Recent", options
    elif sel_value is not None:
        if not is_admin:
            return no_update, no_update
        ok = delete_api_request_log(sel_value)
        if not ok:
            return no_update, no_update
        else:
            # remove the just-deleted log from the existing list of app logs
            options = [opt for opt in curr_options if opt != sel_value]
            return "Recent", options
    else:
        return no_update, no_update


def _fetch_api_requests_log(
        log_name: str, username: Optional[str] = None, route: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Helper method fetches all entries from the specified API requests log file and prepares the information for display
    in the Dash DataTable on this page, optionally filtering entries by username and/or route name

    The method discards consecutive entries that differ only in timestamp, keeping only the last in the sequence. This
    "hack fix" was introduced primarily to get rid of many repeat "commit" status requests from the same user, since a
    user script might poll a commit job's status frequently to determine when it has finished. Eventually, such repeat
    requests should not be logged in the first place (or only the last request in the sequence should be logged).

    Args:
        log_name: Name of the API request log to display, as listed in the selection widget on this page.
        username: If not None, only include API requests from the specified portal user.
        route: If not None, only include entries corresponding to the the specified API route. Note that for
            Route.COMMIT, only entries corresponding to the **start** of a session commit job are included.
    Returns:
        List of all API request log entries, ready to be loaded into the DataTable on this page. If an error occurs,
            returns a brief error description. On error, returns empty list.
    """
    entries: List[Dict[str, Any]]
    try:
        entries = get_api_request_log_contents(log_name)
    except Exception:
        return []

    rows = list()
    last_entry: Optional[Dict[str, Any]] = None
    for entry in reversed(entries):
        same = (last_entry is not None) and (entry.keys() == last_entry.keys())
        same = same and all([((k == 'ts') or (entry[k] == last_entry[k])) for k in entry.keys()])
        if not same:
            last_entry = entry
            if ((username is not None) and (entry['username'] != username)) or \
                    ((route is not None) and (entry['route'] != route)):
                continue
            if route == Route.COMMIT and entry['action'] != 'start':
                continue
            desc, params = Route.describe_api_request(entry)
            timestamp = datetime.fromisoformat(entry['ts']).isoformat(sep=' ', timespec='seconds')
            rows.append(dict(uname=f"***{entry['username']}***", ts=timestamp, desc=desc, params=params))

    return rows
