"""
manage_commits.py: Management page for session commit jobs across all portal users.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
displays a list of all session commit jobs currently pending on the portal server, across all registered portal users.
The portal administrator can use this page to remove any commit jobs that have been in the system for too long. For
example, a user might forgot to remove a completed or failed job from his/her commit job history; or a pending commit
might require manual review, but user has failed to do so.

Created 05jan2023 (saruffner).
"""
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

import dash.exceptions
from dash import html, dash_table as dt, Output, Input, callback, callback_context, State, no_update, dcc
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database.commit_ops import get_pending_commit_jobs_for, CommitJobStatus, cancel_or_remove_commit_job, \
    queue_task_to_clean_commit_staging_areas

_JOBS_TABLE_ID: str = "mngcmt-job-table"
""" ID of Dash DataTable presenting a list of all pending session commit jobs on the portal server. """
_JOBS_TABLE_COLS: List[ti.Column] = [
    ti.Column('job', 'Job/Status', '160px', True),
    ti.Column('committer', 'Committer', '80px', True),
    ti.Column('started', 'Started', '80px', True),
    ti.Column('updated', 'Last Updated', '80px', True),
    ti.Column('progress', 'Progress', '400px', True),
]
""" Defined columns for the pending commit jobs table. """
_JOBS_TABLE_UPDATING: str = "mngcmt-updating"
""" 
The jobs table is wrapped in this Bootstrap Spinner to indicate when the table is being loaded/updated. Removing a
job will take a noticeable amount of time on the server.
"""
_ALERT_ID: str = "mngcmt-alert"
""" ID of a Bootstrap Alert in which an error message is displayed. """
_DELETE_BTN: str = "mngcmt-delete"
""" ID of button widget by which admin user can permanently delete a job selected from the pending commits table. """
_CLEAN_BTN: str = "mngcmt-clean"
""" 
ID of button widget by which admin user can initiate a background task to clean stale folders/files from the session
commit staging areas, both in the local portal workspace and in the S3 repository.
"""
_CLEAN_TOAST: str = "mngcmt-clean-toast"
"""
ID of a Bootstrap Toast that is show whenever the user clicks the button triggering a task to clean the portal's 
session commit staging areas.
"""


def serve_layout() -> html.Div:
    """
    Serve the layout for the "manage commits" page. The page includes a table listing all session commit jobs still
    pending on the portal server. Only authorized users with administrative privileges should have access.

    Returns:
        An HTML Div rendering the user account management page.
    """
    err_msg, job_rows = _fetch_pending_commit_jobs()
    alert = dbc.Alert(err_msg, id=_ALERT_ID, color='danger', dismissable=True, fade=True, duration=10000,
                      is_open=isinstance(err_msg, str), class_name='mb-3')

    jobs_table = dt.DataTable(
        id=_JOBS_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _JOBS_TABLE_COLS],
        data=job_rows,
        row_selectable=False,
        cell_selectable=True,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_data_conditional=[
            {
                "if": {"state": "selected"},
                "backgroundColor": "inherit !important",
                "border": "inherit !important",
            }
        ],
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _JOBS_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '500px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    delete_btn = dbc.Button("Cancel/remove selected job", id=_DELETE_BTN, disabled=True)
    clean_btn = dbc.Button("Clean commit staging areas", id=_CLEAN_BTN, n_clicks=0, class_name='me-3')
    markdown = dcc.Markdown('''
            *A task has been queued to scan the portal's local and remote staging areas for any stale files and/or
            folders pertaining to in-progress commit jobs that were dropped because of a prior server crash. Any 
            orphaned files/folders will be removed. Check the server application logs for results.*
            ''')
    clean_toast = dbc.Toast(markdown, id=_CLEAN_TOAST, header="Clean commit staging areas", is_open=False,
                            dismissable=True, duration=10000, style=dict(width=600), class_name='mt-3')
    delete_row = dbc.Row([
        dbc.Col(clean_btn, width='auto'),
        dbc.Col(delete_btn, width='auto')
    ], justify='between', class_name='mt-3')

    loading_table = dbc.Spinner(jobs_table, id=_JOBS_TABLE_UPDATING, delay_hide=250, delay_show=250, color='primary')
    return html.Div([alert, loading_table, delete_row, clean_toast])


def _fetch_pending_commit_jobs() -> Tuple[Optional[str], List[Dict[str, Any]]]:
    # if no portal user or user does not have admin access, then this page displays nothing of use
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()
    if not is_admin:
        return "Access denied. You must have admin-level access to view this page.", []
    err_msg = None
    job_rows: List[Dict[str, Any]] = []

    jobs = get_pending_commit_jobs_for(None)
    if isinstance(jobs, str):
        err_msg = f"Server error: {jobs}. Try again later, or contact the portal administrator."
    elif len(jobs) == 0:
        err_msg = f"No pending session commits found on server."
    else:
        for job in jobs:
            job_rows.append(_job_table_row_from_job_status(job))
    return err_msg, job_rows


def _job_table_row_from_job_status(job: CommitJobStatus) -> Dict[str, Any]:
    return {
        'id': job.id,   # so that we can delete a job
        'job': "\n".join([f"*{job.id}*", f"**{job.state.get_state_descriptor()}**"]),
        'committer': f"**{job.committer}**",
        'started': f"{datetime.fromtimestamp(job.started).strftime('%Y-%m-%d %I:%M %p')}",
        'updated': f"{datetime.fromtimestamp(job.updated).strftime('%Y-%m-%d %I:%M %p')}",
        'progress': "\n".join(job.message_history[0:3])
    }


# noinspection PyUnusedLocal
@callback(
    [Output(_JOBS_TABLE_ID, "data"), Output(_JOBS_TABLE_ID, "style_data_conditional"),
     Output(_JOBS_TABLE_ID, "active_cell"), Output(_DELETE_BTN, "disabled"), Output(_ALERT_ID, "children"),
     Output(_ALERT_ID, "is_open")],
    [Input(_JOBS_TABLE_ID, "active_cell"), Input(_DELETE_BTN, "n_clicks")],
    [State(_JOBS_TABLE_ID, "data"), State(_JOBS_TABLE_ID, "active_cell")],
    prevent_initial_call=True)
def on_delete_row_or_selection_changed(active_cell, n_delete, curr_rows, curr_active_cell):
    ctx = callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""

    if trigger_id == _DELETE_BTN:
        if not (isinstance(curr_active_cell, dict) and (0 <= curr_active_cell['row'] < len(curr_rows))):
            raise dash.exceptions.PreventUpdate
        del_idx = curr_active_cell['row']
        job_id = curr_rows[del_idx]['id']
        removed, err_msg, job_status = cancel_or_remove_commit_job(job_id)
        if removed:
            style_data_conditional = [
                {"if": {"state": "selected"}, "background-color": "inherit !important", "border": "inherit !important"}
            ]
            del_disabled = True
            curr_rows.pop(del_idx)
            curr_active_cell['row'] = -1
            return curr_rows, style_data_conditional, curr_active_cell, del_disabled, "", False
        elif isinstance(err_msg, str):
            return no_update, no_update, no_update, no_update, err_msg, True
        else:
            curr_rows[del_idx] = _job_table_row_from_job_status(job_status)
            return curr_rows, no_update, no_update, no_update, "", False
    else:
        if isinstance(active_cell, dict) and (active_cell['row'] >= 0):
            style_data_conditional = [
                {"if": {"row_index": active_cell['row']}, "background-color": "rgba(176, 196, 222, 0.5)"},
                {"if": {"state": "selected"}, "background-color": "rgba(176, 196, 222, 0.5)",
                 "border": "inherit !important"}
            ]
            del_disabled = False
        else:
            style_data_conditional = [
                {"if": {"state": "selected"}, "background-color": "inherit !important", "border": "inherit !important"}
            ]
            del_disabled = True
        return no_update, style_data_conditional, no_update, del_disabled, "", False


@callback(Output(_CLEAN_TOAST, "is_open"), [Input(_CLEAN_BTN, "n_clicks")], prevent_initial_call=True)
def on_queue_clean_task(n_clean):
    if n_clean:
        queue_task_to_clean_commit_staging_areas()
        return True
    return False


@callback(Output(_CLEAN_BTN, "disabled"), [Input(_CLEAN_TOAST, "is_open")], prevent_initial_call=True)
def on_show_hide_clean_notification(is_open):
    return is_open
