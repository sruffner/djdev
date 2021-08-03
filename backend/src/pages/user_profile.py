"""
user_profile.py: Simple page on which the currently logged-in user can update selected account information and change
    the account password.

The page should only be accessible when a user is currently logged into the portal. If no user is logged in, all
fields in the profile will be blank, and all action buttons disabled.

@created: 09jul2021
@author: sruffner
"""
from typing import Optional

import dash_html_components as html
import dash_bootstrap_components as dbc
import flask_login
import dash
from dash.dependencies import Output, Input, State

from app import app, PortalUser, load_authorized_user

from database.manager import DataBaseManager

_FULL_NAME_ID: str = "full_name_in"
""" ID of form widget for user's full name. """
_EMAIL_ID: str = "email_in"
""" ID of form widget for user's email address. """
_TITLE_ID: str = "title_in"
""" ID of form widget for user's title or position description. """
_ORG_ID: str = "org_in"
""" ID of form widget for user's organization name. """
_PROFILE_ALERT_ID: str = "save_profile_alert"
""" ID of Bootstrap Alert that displays error or success message after an update profile operation. """
_SAVE_PROFILE_BTN: str = "save_profile_btn"
""" ID of button that triggers an update profile operation. """
_CURRENT_PWD_ID: str = "curr_pwd_in"
""" ID of form widget for user's current password. """
_NEW_PWD_ID: str = "new_pwd_in"
""" ID of form widget for user's new password. """
_CONFIRM_PWD_ID: str = "confirm_pwd_in"
""" ID of form widget to verify user's new password. """
_PWD_ALERT_ID: str = "pwd_alert"
""" ID of Bootstrap Alert that displays error or success message after a change password operation. """
_CHANGE_PWD_BTN: str = "change_pwd_btn"
""" ID of button that triggers a change password operation. """


def serve_layout() -> html.Div:
    """
    Serve the layout for the currently authenticated user's "Profile" page. The page includes a form for updating the
    user's full name, email address, title and organization. It also lets the user change their password.

    Returns:
        An HTML Div rendering the user's "profile" page.
    """
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())

    form_groups = list()
    entry_widget = dbc.Input(
        id=_FULL_NAME_ID, type='text', minLength=5, maxLength=50,
        value=portal_user.full_name() if portal_user else None,
        placeholder="Enter full name (5-50 chars; eg. 'John J. Doe', 'Jane Smith, PhD')"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("Full Name", width=2), dbc.Col(entry_widget, width=10)], row=True))
    entry_widget = dbc.Input(
        id=_EMAIL_ID, type='email', minLength=0, maxLength=80,
        value=portal_user.contact_email() if portal_user else None,
        placeholder="Enter a valid email address up to 80 chars long"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("Email Address", width=2), dbc.Col(entry_widget, width=10)], row=True))
    entry_widget = dbc.Input(
        id=_TITLE_ID, type='text', minLength=0, maxLength=50,
        value=portal_user.title() if portal_user else None,
        placeholder="Enter title/position (optional, 0-50 chars; eg, 'PostDoc')"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("Position", width=2), dbc.Col(entry_widget, width=10)], row=True))
    entry_widget = dbc.Input(
        id=_ORG_ID, type='text', minLength=0, maxLength=50,
        value=portal_user.organization() if portal_user else None,
        placeholder="Enter organization name (optional, 0-50 chars; eg, 'Duke University')"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("Organization", width=2), dbc.Col(entry_widget, width=10)], row=True))

    # alert raised when system fails to save a profile change - displays a brief error message. Otherwise hidden.
    form_groups.append(dbc.FormGroup(
        dbc.Alert("", id=_PROFILE_ALERT_ID, dismissable=True, is_open=False)
    ))

    profile_card = dbc.Card([
        dbc.CardHeader("User Profile"),
        dbc.CardBody(form_groups),
        dbc.CardFooter(dbc.Button("Save Changes", id=_SAVE_PROFILE_BTN, disabled=(portal_user is None), n_clicks=0,
                                  color='primary', className='ml-auto'))
    ], className='w-50 mt-3 mb-3 mx-auto')

    form_groups = list()
    entry_widget = dbc.Input(
        id=_CURRENT_PWD_ID, type='password', minLength=8, maxLength=32,
        placeholder="Verify current password"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("Current password", width=2), dbc.Col(entry_widget, width=10)],
                                     row=True))
    entry_widget = dbc.Input(
        id=_NEW_PWD_ID, type='password', minLength=8, maxLength=32,
        placeholder="Enter new password (8-32 chars with at least 1 digit and 1 uppercase letter)"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("New password", width=2), dbc.Col(entry_widget, width=10)], row=True))
    entry_widget = dbc.Input(
        id=_CONFIRM_PWD_ID, type='password', minLength=8, maxLength=32,
        placeholder="Reenter new password"
    )
    form_groups.append(dbc.FormGroup([dbc.Label("", width=2), dbc.Col(entry_widget, width=10)], row=True))

    # alert raised when a password change fails - displays a brief error message. Otherwise hidden.
    form_groups.append(dbc.FormGroup(
        dbc.Alert("", id=_PWD_ALERT_ID, dismissable=True, is_open=False)
    ))

    password_card = dbc.Card([
        dbc.CardHeader("Change Password"),
        dbc.CardBody(form_groups),
        dbc.CardFooter(dbc.Button("Change", id=_CHANGE_PWD_BTN, disabled=(portal_user is None), n_clicks=0,
                                  color='primary', className='ml-auto'))
    ], className='w-50 mx-auto')

    return html.Div([profile_card, password_card])


@app.callback(
    [Output(_PROFILE_ALERT_ID, 'children'), Output(_PROFILE_ALERT_ID, 'color'),
     Output(_PROFILE_ALERT_ID, 'is_open')],
    [Input(_SAVE_PROFILE_BTN, 'n_clicks')],
    [State(_FULL_NAME_ID, 'value'), State(_EMAIL_ID, 'value'), State(_TITLE_ID, 'value'), State(_ORG_ID, 'value')]
)
def update_profile_callback(*args):
    ctx = dash.callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    if trigger_id != _SAVE_PROFILE_BTN:
        return dash.no_update, dash.no_update, dash.no_update

    msg: Optional[str]
    portal_user = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    if portal_user is None:
        msg = "You must be logged in to update your profile."
    else:
        msg = DataBaseManager().update_portal_user_profile(portal_user.get_id(), args[1], args[2], args[3], args[4])
    return ("Profile updated.", "success", True) if (msg is None) else (msg, "danger", True)


@app.callback(
    [Output(_PWD_ALERT_ID, 'children'), Output(_PWD_ALERT_ID, 'color'), Output(_PWD_ALERT_ID, 'is_open'),
     Output(_CURRENT_PWD_ID, 'value'), Output(_NEW_PWD_ID, 'value'), Output(_CONFIRM_PWD_ID, 'value')],
    [Input(_CHANGE_PWD_BTN, 'n_clicks')],
    [State(_CURRENT_PWD_ID, 'value'), State(_NEW_PWD_ID, 'value'), State(_CONFIRM_PWD_ID, 'value')]
)
def change_password_callback(*args):
    ctx = dash.callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    if trigger_id != _CHANGE_PWD_BTN:
        return "", "danger", False, "", "", ""

    msg: Optional[str]
    portal_user = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    if portal_user is None:
        msg = "You must be logged in to change your password."
    elif args[2] != args[3]:
        msg = "Reentered password does not match new password. Try again."
    else:
        msg = DataBaseManager().change_portal_user_password(portal_user.get_id(), args[1], args[2])
    return ("Password changed.", "success", True, "", "", "") if (msg is None) else (msg, "danger", True, "", "", "")
