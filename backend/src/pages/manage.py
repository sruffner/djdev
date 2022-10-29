"""
manage.py: Portal administration functions.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
exposes various portal administation functions using an accordion-style widget. The content of each accordion item is
rendered in a different module:

    - curate.py: Curate database tables with lab metadata (subjects, experiment rigs, research projects, etc).
    - manage_users.py: User management functions.
    - manage_repo.py: View the contents of the portal backup repository on S3.
    - manage_app_log.py: View the contents of the application message log, including older logs that have been saved
      to the portal backup repository.
    - manage_api_requests_log.py: View the contents of the API requests log.

@created: 18apr2022
@author: sruffner
"""
from typing import Optional

import flask_login
from dash import html, callback, Output, Input
import dash_bootstrap_components as dbc

from app import PortalUser, load_authorized_user
from pages import manage_users, manage_repo, curate, manage_app_log, manage_api_requests_log


_CURATE, _USERS, _REPO, _APP_LOG, _API_REQ = 'curate', 'users', 'repo', 'app-log', 'api-req'
_ADMIN_SECTIONS = [_CURATE, _USERS, _REPO, _APP_LOG, _API_REQ]
""" The different sections on the Portal Administration page. """
_ADMIN_SECTION_LABELS = ['Curate Portal Content', 'Manage Users', 'View Backup Repository on S3',
                         'Portal Server Messages', 'Portal API Request History']
""" User-friendly labels for the different admin sections. """

_SECTION_SELECTOR: str = 'admin_sect_select'
""" ID of mutually exclusive Bootstrap RadioItems group used to select the admin section to display. """

_SECTION_DIV: str = 'admin_sect_div'
""" ID of HTML Div in which selected admin section is rendered. """


def serve_layout() -> html.Div:
    # client must be authenticated with 'admin'-level privileges to view this page
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()
    if not is_admin:
        return html.Div("Access denied. You must be logged in with administrator privileges to view this content.",
                        className='mx-5 my-5')

    section_selector = dbc.RadioItems(
        id=_SECTION_SELECTOR,
        class_name="btn-group radio-group mb-3",
        inputClassName="btn-check",
        labelClassName="btn btn-outline-primary",
        labelCheckedClassName="active",
        options=[{"label": _ADMIN_SECTION_LABELS[i], "value": _ADMIN_SECTIONS[i]} for i in range(len(_ADMIN_SECTIONS))],
        value=_CURATE,
        style=dict(display='block', borderBottom='1.5px solid rgb(176,196,222)'),
        inline=True,
    )
    section_div = html.Div(id=_SECTION_DIV, children=curate.layout)

    card = dbc.Card([
        dbc.CardHeader("Portal Administration"),
        dbc.CardBody([section_selector, section_div]),
    ], class_name='mx-5 my-5')

    return html.Div(card)


@callback(Output(_SECTION_DIV, "children"), [Input(_SECTION_SELECTOR, "value")], prevent_initial_call=True)
def on_select_option(value):
    out = "Select one of the options above."
    if value == _CURATE:
        out = curate.layout
    elif value == _USERS:
        out = manage_users.serve_layout()
    elif value == _REPO:
        out = manage_repo.serve_layout()
    elif value == _APP_LOG:
        out = manage_app_log.serve_layout()
    elif value == _API_REQ:
        out = manage_api_requests_log.serve_layout()
    return out
