"""
manage_app_log.py: Admin GUI for viewing application message logs.

The GUI exposed in this module should only be accessible to a client that is currently logged into the portal with
'admin'-level access. It allows the user to view a snapshot of the current application message log (located in the
portal workspace directory on the server, or the contents of older log files that have been moved to the portal
repository (in a dedicated AWS S3 bucket). A dropdown menu selects which log to view, and its contents are displayed
in a read-only text area. The older log files in the repository may be permanently removed using the "Delete" button.

@created: 19apr2022
@author: sruffner
"""
from typing import Optional

from dash import html, callback, Output, Input, State, no_update, dcc
import dash_bootstrap_components as dbc

import flask_login

from app import PortalUser, load_authorized_user

from config import app_logging


_SELECT_ID: str = "app-log-select"
""" ID of Bootstrap Select used to select an application message log for viewing. """
_CONTENT_AREA_ID: str = "app-log-content"
""" ID of a Textarea component that displays the content of the currently selected application message log. """
_DELETE_ID: str = "app-log-delete-btn"
""" ID of button that deletes the currently selected application message log. """
_APPLOG_LOADING_ID: str = "app-log-loading-indicator"
""" 
ID of a Dash Loading component that displays a spinner when the application log list is being retrieved or a
a selected log's contents is being retrieved for display. These operations may take a little while.

"""


def serve_layout() -> html.Div:
    """
    Serve the layout for the "application message log" admin page. The page includes a dropdown menu to select which
    log to view, a text area for perusing the content of the selected log file, and a button to delete older log files
    backed up in the portal repository. Only authorized users with administrative privileges should have access to
    this page.

    Returns:
        An HTML Div rendering the "application message log" admin page.
    """
    select_grp = dbc.InputGroup([
        dbc.InputGroupText("Select server log"),
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
    content_area = dbc.Textarea(id=_CONTENT_AREA_ID, rows=20, size='sm', readonly=True, wrap=False, value="Loading...")
    return html.Div([dcc.Loading(control_row, id=_APPLOG_LOADING_ID, type='circle', className='me-auto'), content_area])


@callback([Output(_CONTENT_AREA_ID, "value"), Output(_DELETE_ID, "disabled")], [Input(_SELECT_ID, "value")])
def on_select_log(sel_value):
    if sel_value is None:
        return "", True
    else:
        portal_user: Optional[PortalUser] = None
        if flask_login.current_user.is_authenticated:
            portal_user = load_authorized_user(flask_login.current_user.get_id())
        is_admin = (portal_user is not None) and portal_user.is_admin()
        if not is_admin:
            return "Access denied. You must be logged in with administrator privileges to view server logs.", True
        _, contents = app_logging.get_message_log_contents(sel_value)
        return contents, sel_value == 'Recent'


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
        # initial load -- get list of application message logs, but only if user has admin access.
        if not is_admin:
            return "Recent", [{'label': 'Recent', 'value': 'Recent'}]
        log_names = app_logging.list_application_message_logs()
        options = [{'label': name, 'value': name} for name in log_names]
        return "Recent", options
    elif sel_value is not None:
        if not is_admin:
            return no_update, no_update
        ok = app_logging.delete_application_message_log(sel_value)
        if not ok:
            return no_update, no_update
        else:
            # remove the just-deleted log from the existing list of app logs
            options = [opt for opt in curr_options if opt != sel_value]
            return "Recent", options
    else:
        return no_update, no_update
