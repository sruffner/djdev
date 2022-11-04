"""
manage_repo.py: Management page for the portal's S3-based backing repository.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
displays the contents of the portal's backing repository, which is maintained in a Lisberger lab-provisioned "bucket"
in Amazon Web Services's Simple Storage Service (AWS S3).

While an S3 bucket is a non-hierarchical storage system, each file is associated with a unique string key, and the
portal repository design uses the keys to implement a file system-like hierarchy. There several "folders" in the
repository.

The /repo folder contains a ZIP archive for every experiment session that has been successfully committed to the portal
databases. The archive key is uniquely defined by a session's primary key: /repo/<experimenter>/<subj>_<date>_<sfx>.zip,
where <experimenter> is the registered username of the contributor, <subj> is the experiment subject's ID in the
database, <date> is the session date in the string format 'YYYY-MM-DD', and 'sfx' is the session's integer suffix. Note
that there's a separate "subfolder" for each experimenter contributing session data to the portal.

The /staging folder is a transient folder that will be present whenever there are session commit jobs pending on the
portal server. When a commit job is started, the session archive is uploaded to this staging area, under the key
/staging/<job_id>/archive.zip. The archive is kept in the S3 repo in order to support any number of pending commit
jobs. If these archives were kept in the portal server's local disk storage, that resource could easily be swamped if
as few as 10 jobs were pending (it cannot be too large, as local storage on Duke's Kubernetes cluster is far more
expensive than storage in S3). Once a commit job is removed from the server, the corresponding archive is removed from
the staging area.

Finally, the /logs folder contains a backup of several application logs:
   - The database operations log, database_ops.log. This log file records every operation on the portal database so
     that, in the event of a catastrophic failure, an administrative script can reconstruct the database with minimal
     user intervention, by "playing back" the database operations stored sequentially in the log. Of course, the
     time-consuming operations are the session commits, and these, of course, require the corresponding session archives
     under the /repo node.
   - The API requests log, api_requests.log. This records all client requests to the portal application's API
     endpoints.
   - Application message logs. Application log messages from the backend or an RQ worker task are streamed to a log
     file in the server's local workspace, appmessages.log. When this file reaches a certain size, it is backed up to
     /logs/appmessages.log-YYYYMmmDD-HH.MM on S3, then truncated to 0 bytes. Here, YYYYMmmDD-HH.MM is a date-time stamp
     indicating when the message log was backed up.

While the session archive files, the database operations log, and the API requests log should never be deleted from
the repository, the application message log backups are less critical and may be deleted from time to time. Also, if
a commit job fails and the uploaded session archive is left "dangling" at /staging/<job_id>/archive.zip, it would be
useful to be able to remove it. This page includes a Delete button for this purpose. Use with care -- never delete an
archive file under the /staging node for a commit job that is still running!


@created: 21mar2022
@author: sruffner
"""
from typing import Optional, List, Dict, Any, Tuple

import dash.exceptions
from dash import html, dash_table as dt, Output, Input, callback, no_update, dcc, State, callback_context
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database import repo
from database.commit_ops import commit_job_status
from sglportalapi.util import size_with_units

_REPO_TABLE_ID: str = "repo-table"
""" ID of Dash DataTable presenting a pseudo file listing of the portal's backup repository contents. """
_REPO_TABLE_COLS: List[ti.Column] = [
    ti.Column('name', 'File', '300px', True),
    ti.Column('last_modified', 'Last Modified', '200px', True),
    ti.Column('storage_class', 'Storage Class', '100px', True),
    ti.Column('size', 'Size', '100px', True),
]
""" Defined columns for the repository contents table. """
_REPO_ALERT_ID: str = "repo-alert"
""" ID of a Bootstrap Alert in which an error message is displayed. """
_REPO_STORE_ID: str = "repo-store"
""" ID of a Dash Store component in which the portal's repository content list is stored when the page loads. """
_REPO_DEL_BTN: str = "repo-delete"
""" ID of button widget by which admin user can permanently delete a selected file in the repository, if enabled. """
_REPO_FILE_COUNT_BADGE: str = "repo-file-count-badge"
""" 
ID of a Bootstrap Badge component that reflects how many file objects are in the repo. It is wrapped by a Loading 
component in order to display a loading spinner while the repository content listing is fetched.
"""
_REPO_LOADING: str = "repo-loading-indicatior"
""" ID of the Loading component that displays a spinner when the repository content listing is fetched. """


def serve_layout() -> html.Div:
    """
    Serve the layout for the "backup repository contents" page. The page includes a table listing all files stored in
    the portal's backup repository, in a pseudo hierarchical fashion. Certain non-essential file objects in the repo
    may be deleted via this page. Only authorized users with administrative privileges should have access.

    Returns:
        An HTML Div rendering the user account management page.
    """
    # the repository listing is cached in a Store component, but retrieving it takes a little while. So we delay
    # retrieval until after initial load
    store = dcc.Store(id=_REPO_STORE_ID, data={})
    loading_badge = dbc.Badge("Total files: ", id=_REPO_FILE_COUNT_BADGE, color='primary', )

    alert = dbc.Alert("", id=_REPO_ALERT_ID, color='danger', dismissable=True, fade=True, duration=10000,
                      is_open=False)
    alert_row = dbc.Row([
        dbc.Col(dbc.Spinner(html.H5(loading_badge), id=_REPO_LOADING, type='border',
                            delay_hide=250, delay_show=250, color='primary',
                            spinner_style=dict(position='absolute', left='0px')),
                width=2),
        dbc.Col(alert, width=10)
    ])

    data_table = dt.DataTable(
        id=_REPO_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _REPO_TABLE_COLS],
        data=[],
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
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _REPO_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '500px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    delete_btn = dbc.Button("Delete", id=_REPO_DEL_BTN, disabled=True)
    delete_row = dbc.Row(dbc.Col(delete_btn, width='auto'), justify='end', class_name='mt-3')

    return html.Div([store, alert_row, data_table, delete_row])


def _fetch_repo_contents() -> Tuple[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Fetch the portal repository's contents list. The 'last_modified' field for each file object is converted from a
    `datetime` to a string, since `datetime` cannot be JSONified for storage in the Dash Store component on this page.

    Returns:
        A 2-tuple ("", C) on successs, where C is the repository content listing, ready to be cached in the Store
            component. Returns (error message, {}) on failure.
    """
    out = repo.listing()
    if isinstance(out, str):
        return out, {}
    else:
        repo_contents = out
        # convert the "last modified" fields to strings, as they cannot be JSONified for the Store component
        for k in repo_contents.keys():
            for info in repo_contents[k]:
                info['last_modified'] = info['last_modified'].strftime('%m-%d-%Y %H:%M:%S %Z')
        return "", repo_contents


@callback(
    [Output(_REPO_TABLE_ID, "data"), Output(_REPO_TABLE_ID, "style_data_conditional"),
     Output(_REPO_TABLE_ID, "active_cell"), Output(_REPO_DEL_BTN, "disabled")],
    [Input(_REPO_TABLE_ID, "active_cell"), Input(_REPO_STORE_ID, 'modified_timestamp')],
    [State(_REPO_TABLE_ID, "data"), State(_REPO_STORE_ID, "data"), State(_REPO_TABLE_ID, 'active_cell')],
    prevent_initial_call=True)
def on_repo_listing_or_selection_changed(active_cell, store_ts, current_rows, repo_folders, curr_active_cell):
    style_data_conditional = [
        {"if": {"state": "selected"}, "background-color": "inherit !important", "border": "inherit !important"}
    ]
    del_disabled = True

    ctx = callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if trigger_id == _REPO_STORE_ID:
        if store_ts is None:
            raise dash.exceptions.PreventUpdate
        # handles two scenarios: (1) The repo listing is fetched and put in Store after initial layout. (2) The repo
        # listing has changed bc user deleted a file object. In the latter case, this updates the table rows, but keeps
        # expanded those folders that were expanded prior to the deletion.
        previously_expanded = set()
        for i, r in enumerate(current_rows):
            if r['storage_class'] == '--':
                if ((i + 1) < len(current_rows)) and (current_rows[i+1]['storage_class'] != '--'):
                    previously_expanded.add(r['s3_key'])
        rows = []
        for folder_key in sorted(repo_folders.keys()):
            folder_size = sum([float(file_info['size']) for file_info in repo_folders[folder_key]])
            arrow_char = "\u25be" if (folder_key in previously_expanded) else "\u25b8"
            rows.append(
                dict(s3_key=folder_key, name=f"{arrow_char}  ***{folder_key}***", last_modified="--",
                     storage_class="--",
                     size=f"***{size_with_units(folder_size)} [{len(repo_folders[folder_key])} files]***"))
            # is the folder currently expanded? If so, append all the file objects in that folder
            if folder_key in previously_expanded:
                for info in repo_folders[folder_key]:
                    rows.append(
                        dict(s3_key=f"{folder_key}/{info['name']}", name=f"\u21b3 {info['name']}",
                             last_modified=info['last_modified'], storage_class=info['storage_class'],
                             size=size_with_units(info['size'])))
        current_rows = rows

        # if the active cell is still among the rows of the updated table, be sure to set background for that
        # entire row. Also check if that cell corresponds to a deletable file
        if isinstance(curr_active_cell, dict) and 0 <= curr_active_cell['row'] < len(current_rows):
            active_cell = curr_active_cell
            style_data_conditional = [
                {"if": {"row_index": active_cell['row']}, "background-color": "rgba(176, 196, 222, 0.5)"},
                {"if": {"state": "selected"}, "background-color": "rgba(176, 196, 222, 0.5)",
                 "border": "inherit !important"}
            ]
            row = current_rows[active_cell['row']]
            s3_key, is_folder = row['s3_key'], row['storage_class'] == '--'
            del_disabled = is_folder or not (s3_key.startswith('/staging') or
                                             s3_key.startswith('/logs/appmessages.log'))
    elif isinstance(active_cell, dict) and (active_cell['row'] >= 0):
        style_data_conditional = [
            {"if": {"row_index": active_cell['row']}, "background-color": "rgba(176, 196, 222, 0.5)"},
            {"if": {"state": "selected"}, "background-color": "rgba(176, 196, 222, 0.5)",
             "border": "inherit !important"}
        ]

        row = current_rows[active_cell['row']]
        s3_key, collapsed, expanded = row['s3_key'], row['name'].startswith('\u25b8'), row['name'].startswith('\u25be')
        if not (collapsed or expanded):
            del_disabled = not (s3_key.startswith('/staging') or s3_key.startswith('/logs/appmessages.log'))
            return no_update, style_data_conditional, no_update, del_disabled

        if collapsed:
            # toggle right arrow down as an indication that folder node is expanded
            row['name'] = f"\u25be  ***{s3_key}***"
            # insert a row for each file object under that folder.
            idx = active_cell['row'] + 1
            for info in repo_folders[s3_key]:
                current_rows.insert(
                    idx,
                    dict(s3_key=f"{s3_key}/{info['name']}", name=f"\u21b3 {info['name']}",
                         last_modified=info['last_modified'], storage_class=info['storage_class'],
                         size=size_with_units(info['size']))
                )
                idx += 1
            active_cell['row'] = -1
        else:
            # toggle down arrow to right as an indication that folder node is collapsed
            row['name'] = f"\u25b8  ***{s3_key}***"
            # remove all file object nodes after the folder node (until we hit EOL or another folder node)
            idx = active_cell['row'] + 1
            while (idx < len(current_rows)) and not \
                    (current_rows[idx]['name'].startswith('\u25b8') or current_rows[idx]['name'].startswith('\u25be')):
                current_rows.pop(idx)
            active_cell['row'] = -1

    return current_rows, style_data_conditional, active_cell, del_disabled


@callback(
    [Output(_REPO_STORE_ID, "data"), Output(_REPO_FILE_COUNT_BADGE, "children"), Output(_REPO_ALERT_ID, "children"),
     Output(_REPO_ALERT_ID, "is_open")],
    [Input(_REPO_DEL_BTN, "n_clicks")], [State(_REPO_TABLE_ID, "active_cell"), State(_REPO_TABLE_ID, "data")])
def on_delete(n_delete, active_cell, current_rows):
    if n_delete is None:
        # initial call -- retrieve repo listing
        portal_user: Optional[PortalUser] = None
        if flask_login.current_user.is_authenticated:
            portal_user = load_authorized_user(flask_login.current_user.get_id())
        is_admin = (portal_user is not None) and portal_user.is_admin()
        if not is_admin:
            return no_update, no_update, "You are not authorized to view repository contents", True
        emsg, repo_contents = _fetch_repo_contents()
        if len(emsg) > 0:
            return no_update, no_update, emsg, True
        else:
            file_count = sum([len(v) for _, v in repo_contents.items()])
            return repo_contents, f"Total files: {file_count}", "", False
    else:
        row_idx = active_cell['row'] if isinstance(active_cell, dict) else -1
        if isinstance(current_rows, list) and (row_idx > -1) and (row_idx < len(current_rows)):
            row: dict = current_rows[row_idx]
            emsg, repo_contents = _delete_file_in_repo(row['s3_key'])
            if len(emsg) > 0:
                return no_update, no_update, emsg, True
            else:
                file_count = sum([len(v) for _, v in repo_contents.items()])
                return repo_contents, f"Total files: {file_count}", "", False

    raise dash.exceptions.PreventUpdate


def _delete_file_in_repo(file_key: str) -> Tuple[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Helper method for on_delete(). Deletes the specified file in the portal repository and retrieves the updated
    listing of the  repository contents.

    Args:
        file_key: The key of the file object to be removed from the portal repository in S3.
    Returns:
        A 2-tuple ("", listing) on success, where listing is the repository contents listing ready to be cached in the
            Store component on this page. On failure: (error message, {})

    """
    if not (file_key.startswith('/staging') or file_key.startswith('/logs/appmessages.log')):
        return "The selected file may not be removed from the portal repository", {}
    elif file_key.startswith('/staging'):
        parts = file_key.split('/')
        if (len(parts) == 4) and (parts[3] == 'archive.zip'):
            job_status = commit_job_status(parts[2])
            if not isinstance(job_status, str):
                return "Cannot remove archive for an in-progress session commit job", {}

    if not repo.delete_file(file_key):
        return "Unable to remove selected file from portal repository", {}

    return _fetch_repo_contents()
