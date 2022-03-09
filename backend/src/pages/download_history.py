"""
download_history.py: Page displaying history of response data downloads from the Lisberger lab data portal.

This page should only be accessible when a user with 'commit'-level privileges (or higher) is currently logged into the
portal. It displays a view of the DataDownload table in the portal database, which holds an entry for each time an
authenticated user downloads response data from the portal. This is primarily to track and protect the provenance of the
experimental data stored in the portal.

Behavioral response data (recorded eye velocity and position traces), spike train responses from up to 5 selected
neural units, and trial metadata (trial name, recorded duration, fixation target position trajectories, etc) are
collected from all trials (or, optionally, completed trials only) presented during a specified experiment session. The
generated data file may be a Numpy multi-array NPZ file (for those that prefer to do analysis in Python) or a Matlab
MAT file.

@created: 08mar2022
@author: sruffner
"""
import logging
from typing import Optional, List, Dict

from dash import html, dash_table as dt
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database.table_ops import fetch_rows, fetch_restrict_proj

logger = logging.getLogger(__name__)

_DNLD_HIST_TABLE: str = "downhist-table"
""" The ID assigned to the Dash DataTable presenting the response data downloads history. """

_DNLD_HIST_TABLE_COLS: List[ti.Column] = [
    ti.Column('requester_full', 'Requester', '100px', True),
    ti.Column('session', 'Session Date', '100px', True),
    ti.Column('subj_id', 'Subject ID', '100px', True),
    ti.Column('experimenter_full', 'Experimenter', '100px', True),
    ti.Column('selected_units', 'Unit IDs Included', '100px', True),
    ti.Column('out_format', 'File Format', '50px', True),
    ti.Column('downloaded', 'Downloaded On', '100px', True)
]
""" Defined columns for the download history table. Multiple attributes listed in 'session' column. """

_DNLD_HIST_ALERT_ID: str = "downhist-alert"
""" ID of Bootstrap Alert that displays error message at top of page if an error occurs. """


def _table_of_data_downloads(user_can_commit: bool) -> html.Div:
    """
    Prepare the Dash DataTable displaying all response data downloads from the Lisberger lab data portal.

    Args:
        user_can_commit: True if a user is currently logged in with 'commit'-level privileges
    Returns:
        An HTML Div containing the downloads history table and a hidden Bootstrap Alert. The table is empty and the
            Alert displays an error message if there's a problem retrieving the download history.
    """
    rows = []
    error_msg = None
    if not user_can_commit:
        error_msg = "You are not authorized to view the portal's download history."
    else:
        rows = fetch_rows(ti.DBTable.DATA_DOWNLOAD)
        if isinstance(rows, str):
            error_msg = rows
            rows = []
        else:
            user_rows = fetch_restrict_proj([ti.DBTable.USER], None, ['full_name']) or []
            user_to_full: Dict[str, str] = dict()
            for r in user_rows:
                user_to_full[r['username']] = r['full_name']
            for row in rows:
                row['session'] = f"**{str(row['session_date'])} [{row['session_sfx']}]**"
                row['requester_full'] = user_to_full[row['requester']] if (row['requester'] in user_to_full) \
                    else row['requester']
                row['experimenter_full'] = user_to_full[row['experimenter']] if (row['experimenter'] in user_to_full) \
                    else row['experimenter']
            rows.sort(key=lambda x: x['downloaded'], reverse=True)

    data_table = dt.DataTable(
        id=_DNLD_HIST_TABLE,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _DNLD_HIST_TABLE_COLS],
        data=rows,
        row_selectable=False,
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _DNLD_HIST_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '500px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    alert = dbc.Alert(error_msg, id=_DNLD_HIST_ALERT_ID, color='danger', dismissable=True, fade=True,
                      is_open=(error_msg is not None), class_name="mb-3")
    return html.Div([alert, data_table])


def serve_layout() -> html.Div:
    """
    Serve the layout for the "downloads history" page. A single table on this page displays all previous response data
    downloads from the portal, as stored in a dedicated table within the portal database. Only authorized users with
    administrative privileges should have access to this page.

    Returns:
        An HTML Div rendering the downloads history page.
    """
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    can_commit = (portal_user is not None) and portal_user.can_commit_to_database()
    downloads_table_div = _table_of_data_downloads(can_commit)

    card = dbc.Card([
        dbc.CardHeader("Download History"),
        dbc.CardBody([downloads_table_div]),
    ], class_name='w-75 mx-auto mt-5')

    return html.Div([card])
