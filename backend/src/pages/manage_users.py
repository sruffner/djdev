"""
manage_users.py: Management page for the table of registered user accounts on the Lisberger lab data portal

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
displays a table listing all registered portal user accounts and supports a number of user management operations:
register a new portal user account, delete an existing account (admin accounts may not be deleted via this page), or
change an existing account's access level.

@created: 12jul2021
@author: sruffner
"""
import logging
from typing import Optional, List, Dict, Union

from dash import callback, clientside_callback, callback_context, no_update, html, dash_table as dt, Input, \
    Output, State
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database.user_ops import ACCESS_LEVELS, get_all_portal_user_records, DOWNLOAD_ACCESS, remove_portal_user, \
    change_portal_user_access_level, register_new_portal_user, ADMIN_ACCESS


logger = logging.getLogger(__name__)

_USER_TABLE_ID: str = "user-account-table"
""" The ID assigned to the Dash DataTable presenting all user accounts registered on the Lisberger lab portal. """

_USER_TABLE_COLS: List[ti.Column] = [
    ti.Column('username', 'Username', '100px', False),
    ti.Column('profile', 'Profile', '300px', True),
    ti.Column('access', 'Privileges', '60px', False),
    ti.Column('registered', 'Registered', '75px', False),
    ti.Column('last_login', 'Last Login', '150px', False),
    ti.Column('pwd_changed', 'Password Updated', '75px', False)
]
""" Defined columns for the user accounts table. Multiple attributes listed in 'profile' column. """

_REGISTER_USERS_BTN = "reg-users-btn"
""" ID of button that raises 'Register new users' modal window. """
_DELETE_BTN: str = "delete-btn"
""" ID of button that deletes a selected user account. """
_UPDATE_ACCESS_SELECT_ID: str = "update-access-select"
""" ID of Bootstrap Select used to change the access level of a selected user account. """
_OP_ALERT_ID: str = "op-alert"
""" ID of Bootstrap Alert that displays error message at top of page after a user account management operation. """

_REG_MODAL_ID: str = "reg-modal"
""" ID of the Bootstrap Modal window in which new user accounts are registered. """
_USERNAME_ID: str = "user-name-in"
""" ID of form widget to enter a new user's ID/username. """
_ACCESS_SELECT_ID: str = "access-sel"
""" ID of form widget to select a new user's access level. """
_FULL_NAME_ID: str = "full-name-in"
""" ID of form widget to enter a new user's full name. """
_EMAIL_ID: str = "email-in"
""" ID of form widget to enter a new user's email address. """
_PASSWORD_ID: str = "password-in"
""" ID of form widget for user's initial password. """
_CONFIRM_PWD_ID: str = "confirm-pwd-in"
""" ID of form widget to verify the initial password entered. """
_REGISTER_BTN: str = "register-btn"
""" ID of button on modal button that registers a new user account. """
_DONE_BTN: str = "done-reg-btn"
""" ID of button that extinguishes the 'Register new users' modal window. """
_REG_ALERT_ID = "reg-alert"
""" ID of Bootstrap Alert that displays error/success message after each new-user registration attempt. """


def _fetch_user_table_rows() -> Union[str, List[Dict[str, str]]]:
    """
    Helper method fetches all registered user account records and formats each record for display in the table
    on this page. Returns an error description on failure.
    """
    rows = get_all_portal_user_records()
    if isinstance(rows, list):
        for row in rows:
            row['registered'] = str(row['registered']).split()[0]  # only want the registration date
            row['pwd_changed'] = str(row['pwd_changed']).split()[0]  # same for last password change
            row['last_login'] = "Never" if (row['last_login'] is None) else str(row['last_login'])
            row['profile'] = f"**{row['full_name']}** ({row['contact_email']})\n" \
                             f"{row['title'] if row['title'] is not None else '--'}, " \
                             f"{row['organization'] if row['organization'] is not None else '--'}"
    return rows


def _table_of_user_accounts(user_is_admin: bool) -> html.Div:
    """
    Prepare the Dash DataTable displaying information on all registered user accounts on the Lisberger lab data portal.
    """
    rows = []
    error_msg = None
    if not user_is_admin:
        error_msg = "You are not authorized to manage portal user accounts."
    else:
        rows = _fetch_user_table_rows()
        if isinstance(rows, str):
            error_msg = rows
            rows = []

    data_table = dt.DataTable(
        id=_USER_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _USER_TABLE_COLS],
        data=rows,
        row_selectable='single',
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _USER_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '500px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    alert = dbc.Alert(error_msg, id=_OP_ALERT_ID, color='danger', dismissable=True, fade=True,
                      is_open=(error_msg is not None), class_name="mb-3")
    return html.Div([alert, data_table])


def serve_layout() -> html.Div:
    """
    Serve the layout for the "user account management" page. The page includes a table listing all existing user
    accounts, as well as controls for registering a new account, removing an existing account, or changing the access
    level for an account. Only authorized users with administrative privileges should have access to this page.

    Returns:
        An HTML Div rendering the user account management page.
    """
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()
    user_table_div = _table_of_user_accounts(is_admin)

    raise_reg_btn = dbc.Button("Register new user(s)", id=_REGISTER_USERS_BTN, disabled=not is_admin, n_clicks=0)
    remove_btn = dbc.Button("Remove selected user", id=_DELETE_BTN, disabled=True, n_clicks=0, class_name='me-2')
    access_grp = dbc.InputGroup([
        dbc.InputGroupText("Set access"),
        dbc.Select(
            id=_UPDATE_ACCESS_SELECT_ID,
            options=[{'label': level, 'value': level} for level in ACCESS_LEVELS],
            value=DOWNLOAD_ACCESS,
            disabled=True
        )
    ])
    control_row = dbc.Row([
        dbc.Col(dbc.Row([dbc.Col(remove_btn, width='auto'), dbc.Col(access_grp, width='auto')], class_name='g-0'),
                width='auto', class_name='me-4'),
        dbc.Col([raise_reg_btn], width='auto')
    ], justify='between', class_name='mt-3')

    # form that gathers essential information to register a new user account
    form_rows = list()
    entry_widget = dbc.Input(
        id=_USERNAME_ID, type='text', minlength=3, maxlength=20,
        value=None,
        placeholder="Enter account user ID (3-20 lowercase letters or digits, starting with a letter)"
    )
    form_rows.append(dbc.Row([dbc.Label("Username", width=2), dbc.Col(entry_widget, width=8)], class_name='mb-2'))
    entry_widget = dbc.Input(
        id=_FULL_NAME_ID, type='text', minlength=5, maxlength=50,
        value=None,
        placeholder="Enter full name (5-50 chars; eg. 'John J. Doe', 'Jane Smith, PhD')"
    )
    form_rows.append(dbc.Row([dbc.Label("Full Name", width=2), dbc.Col(entry_widget, width=10)], class_name='mb-2'))
    entry_widget = dbc.Input(
        id=_EMAIL_ID, type='email', minlength=0, maxlength=80,
        value=None,
        placeholder="Enter a valid email address up to 80 chars long"
    )
    form_rows.append(dbc.Row([dbc.Label("Email Address", width=2), dbc.Col(entry_widget, width=10)], class_name='mb-2'))
    entry_widget = dbc.Select(
        id=_ACCESS_SELECT_ID,
        options=[{"label": opt, "value": opt} for opt in ACCESS_LEVELS],
        value=DOWNLOAD_ACCESS
    )
    form_rows.append(dbc.Row([dbc.Label("Access Level", width=2), dbc.Col(entry_widget, width='auto')],
                             class_name='mb-2'))
    entry_widget = dbc.Input(
        id=_PASSWORD_ID, type='password', minlength=8, maxlength=32,
        placeholder="Enter user's password (8-32 chars with at least 1 digit and 1 uppercase letter)"
    )
    form_rows.append(dbc.Row([dbc.Label("Initial password", width=2), dbc.Col(entry_widget, width=8)],
                             class_name='mb-2'))
    entry_widget = dbc.Input(
        id=_CONFIRM_PWD_ID, type='password', minlength=8, maxlength=32,
        placeholder="Reenter the same password to confirm"
    )
    form_rows.append(dbc.Row([dbc.Label("", width=2), dbc.Col(entry_widget, width=8)], class_name='mb-2'))

    form_rows.append(dbc.Row([
        dbc.Col(dbc.Alert("", id=_REG_ALERT_ID, color='danger', dismissable=True, fade=True, is_open=False), width=10)
    ]))
    user_form = dbc.Form(form_rows)

    register_modal = dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle("Register new users")),
            dbc.ModalBody(user_form),
            dbc.ModalFooter(
                dbc.Row([
                    dbc.Col(dbc.Button("Register", id=_REGISTER_BTN), width='auto', class_name='me-2'),
                    dbc.Col(dbc.Button("Done", id=_DONE_BTN), width='auto')
                ], justify='end')
            )
        ],
        id=_REG_MODAL_ID, backdrop="static", size="xl", centered=True
    )

    card = dbc.Card([
        dbc.CardHeader("Portal user account management"),
        dbc.CardBody([user_table_div, control_row]),
    ], class_name='w-75 mx-auto mt-5')

    return html.Div([card, register_modal])


# this clientside callback highlights all cells in the selected row
clientside_callback(
    """
    function(rows) {
        let style = [];
        if (Array.isArray(rows) && (rows.length > 0) && Number.isInteger(rows[0])) {
            style = [{"if": {"row_index": rows[0]}, "background-color": "rgba(176, 196, 222, 0.5)"}];
        }
        return style;
    }
    """,
    Output(_USER_TABLE_ID, "style_data_conditional"),
    Input(_USER_TABLE_ID, "selected_rows")
)


@callback(
    [Output(_USER_TABLE_ID, 'data'), Output(_USER_TABLE_ID, 'selected_rows'),
     Output(_OP_ALERT_ID, 'children'), Output(_OP_ALERT_ID, 'is_open'), Output(_DELETE_BTN, 'disabled'),
     Output(_UPDATE_ACCESS_SELECT_ID, 'value'), Output(_UPDATE_ACCESS_SELECT_ID, 'disabled')],
    [Input(_DELETE_BTN, 'n_clicks'), Input(_REG_MODAL_ID, 'is_open'), Input(_UPDATE_ACCESS_SELECT_ID, 'value'),
     Input(_USER_TABLE_ID, 'selected_rows')],
    [State(_USER_TABLE_ID, 'selected_rows'), State(_USER_TABLE_ID, 'data')]
)
def on_select_user_or_delete_or_change_access(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    triggers = [_DELETE_BTN, _REG_MODAL_ID, _UPDATE_ACCESS_SELECT_ID, _USER_TABLE_ID]
    if trigger_id not in triggers:
        return tuple([no_update] * 7)

    # stop any user managment task if login session times out or logged-in user lacks admin-level access
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    if (portal_user is None) or (not portal_user.is_admin()):
        return [], [], "You are not authorized to manage portal user accounts.", True, True, DOWNLOAD_ACCESS, True

    # a different user was selected in reg. users table; update dropdown to match the access level for that user. Note
    # that one may never delete or change the access of an administrator.
    if trigger_id == _USER_TABLE_ID:
        idx = args[3][0] if (args[3] is not None) and (len(args[3]) > 0) else -1
        row = args[-1][idx] if ((args[-1] is not None) and (-1 < idx < len(args[-1]))) else None
        if row is None:
            return tuple([no_update] * 7)
        else:
            is_admin = (row['access'] == ADMIN_ACCESS)
            return no_update, no_update, "", False, is_admin, row['access'], is_admin

    # we refresh the user table contents whenever the modal user registration window is extinguished
    if trigger_id == _REG_MODAL_ID:
        if not args[1]:
            updated_rows = _fetch_user_table_rows()
            if isinstance(updated_rows, str):
                return [], [], updated_rows, True, True, DOWNLOAD_ACCESS, True
            else:
                return updated_rows, [], "", False, True, DOWNLOAD_ACCESS, True
        else:
            return tuple([no_update] * 7)

    # there must be a user row selected in table to delete or change access level
    rows = args[-1]
    idx = args[-2][0] if (args[-2] is not None) and (len(args[-2]) > 0) else -1
    user_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    if user_row is None:
        return no_update, no_update, "", False, True, DOWNLOAD_ACCESS, True

    # perform requested operation on selected user. Note we update table data if operation is successful RATHER than
    # fetching the data again after the operation!
    if trigger_id == _DELETE_BTN:
        error_msg = remove_portal_user(user_row['username'])
        if error_msg is None:
            rows.pop(idx)
    else:
        access_level = args[2]
        if access_level == user_row['access']:
            # no change!
            return no_update, no_update, "", False, False, no_update, False
        else:
            error_msg = change_portal_user_access_level(user_row['username'], access_level)
            if error_msg is None:
                user_row['access'] = access_level

    if error_msg is None:
        return rows, [] if trigger_id == _DELETE_BTN else [idx], "", False, \
               trigger_id == _DELETE_BTN, no_update, trigger_id == _DELETE_BTN
    else:
        return no_update, no_update, error_msg, True, False, no_update, False


@callback(
    Output(_REG_MODAL_ID, "is_open"),
    [Input(_REGISTER_USERS_BTN, 'n_clicks'), Input(_DONE_BTN, 'n_clicks')], [State(_REG_MODAL_ID, 'is_open')]
)
def toggle_register_users_modal(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    if trigger_id not in [_REGISTER_USERS_BTN, _DONE_BTN]:
        return no_update
    return not args[2]


@callback(
    [Output(_USERNAME_ID, 'value'), Output(_FULL_NAME_ID, 'value'), Output(_EMAIL_ID, 'value'),
     Output(_ACCESS_SELECT_ID, 'value'), Output(_PASSWORD_ID, 'value'), Output(_CONFIRM_PWD_ID, 'value'),
     Output(_REG_ALERT_ID, 'children'), Output(_REG_ALERT_ID, 'color'), Output(_REG_ALERT_ID, 'is_open')],
    [Input(_REGISTER_BTN, 'n_clicks'), Input(_REG_MODAL_ID, 'is_open')],
    [State(_USERNAME_ID, 'value'), State(_FULL_NAME_ID, 'value'), State(_EMAIL_ID, 'value'),
     State(_ACCESS_SELECT_ID, 'value'), State(_PASSWORD_ID, 'value'), State(_CONFIRM_PWD_ID, 'value')]
)
def register_user_callback(*args):
    out = [no_update] * 9
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    if trigger_id not in [_REGISTER_BTN, _REG_MODAL_ID]:
        return tuple(out)

    if trigger_id == _REG_MODAL_ID:
        out = ["", "", "", DOWNLOAD_ACCESS, "", "", "", "success", False]
    else:
        if args[6] != args[7]:
            out[6:9] = ['Password mismatch. Try again.', 'danger', True]
        else:
            error_msg = register_new_portal_user(args[2], args[6], args[5], args[3], args[4])
            if error_msg is None:
                out = ["", "", "", DOWNLOAD_ACCESS, "", "", "User registered successfully.", 'success', True]
            else:
                out[6:9] = [error_msg, 'danger', True]
    return tuple(out)
