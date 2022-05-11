"""
manage_repo.py: Management page for the portal's S3-based backing repository.

This page should only be accessible when a user with 'admin'-level privileges is currently logged into the portal. It
displays the contents of the portal's backing repository, which is maintained in a Lisberger lab-provisioned "bucket"
in Amazon Web Services's Simple Storage Service (AWS S3).

While an S3 bucket is a non-hierarchical storage system, each file is associated with a unique string key, and the
portal repository design uses the keys to implement a file system-like hierarchy. There are 3 "folders" in the
repository.

The /downloads folder is temporary storage for data download packages prepared at the request of registered users. Once
generated in response to a download request, the data file is uploaded to /downloads/<req_id>.<ext> and a presigned URL
is passed to the client so the file can be downloaded directly from S3. Files in the /downloads folder "expire" and are
automatically removed after 1 day.

The /repo folder contains a ZIP archive for every experiment session that has been successfully committed to the portal
databases. The archive key is uniquely defined by a session's primary key: /repo/<experimenter>/<subj>_<date>_<sfx>.zip,
where <experimenter> is the registered username of the contributor, <subj> is the experiment subject's ID in the
database, <date> is the session date in the string format 'YYYY-MM-DD', and 'sfx' is the session's integer suffix. Note
that there's a separate subfolder for each portal user that has committed experiment data to the portal.

Finally, the /logs folder contains a backup of the database operations log, database_ops.log. This log file records
every operation on the portal database so that, in the event of a catastrophic failure, an administrative script can
reconstruct the database with minimal user intervention, by "playing back" the database operations stored sequentially
in the log. Of course, the time-consuming operations are the session commits, and these, of course, require the
corresponding session archives under the /repo node.

Currently, this page offers only a "read-only" view of the backup repository content.

@created: 21mar2022
@author: sruffner
"""
from typing import Optional, List, Dict, Union

from dash import html, dash_table as dt
import dash_bootstrap_components as dbc
import flask_login

from app import PortalUser, load_authorized_user

import database.table_info as ti
from database import repo
from sglportalapi.util import size_with_units

_REPO_TABLE_ID: str = "repo-table"
""" ID of Dash DataTable presenting a pseudo filelisting of the portal's backup repository contents. """

_REPO_TABLE_COLS: List[ti.Column] = [
    ti.Column('name', 'File', '300px', True),
    ti.Column('last_modified', 'Last Modified', '200px', True),
    ti.Column('storage_class', 'Storage Class', '100px', True),
    ti.Column('size', 'Size', '100px', True),
]
""" Defined columns for the repository contents table. """

_OP_ALERT_ID: str = "op-alert"
""" ID of Bootstrap Alert that displays error message at top of page if an error occurs. """


def _fetch_repo_contents() -> Union[str, List[Dict[str, str]]]:
    """
    Helper method fetches the contents of the portal's backup repository and prepares the information for display in a
    Dash DataTable.
    """
    folders = repo.listing()
    if folders is None:
        return "Unable to retrieve contents of portal's repository. Consult application logs."
    rows = list()
    for folder_key in sorted(folders.keys()):
        folder_size = sum([float(file_info['size']) for file_info in folders[folder_key]])
        rows.append(dict(name=f"***{folder_key}***", last_modified="--", storage_class="--",
                         size=f"***{size_with_units(folder_size)}***"))
        for info in folders[folder_key]:
            rows.append(dict(name=f"\u21b3 {info['name']}",
                             last_modified=info['last_modified'].strftime('%m-%d-%Y %H:%M:%S %Z'),
                             storage_class=info['storage_class'], size=size_with_units(info['size'])))
    return rows


def _table_of_repo_contents(user_is_admin: bool) -> html.Div:
    """
    Prepare the Dash DataTable displaying information on files stored in the portal's backup repository.
    """
    rows = []
    error_msg = None
    if not user_is_admin:
        error_msg = "You are not authorized to view the contents of the backup repository."
    else:
        rows = _fetch_repo_contents()
        if isinstance(rows, str):
            error_msg = rows
            rows = []

    data_table = dt.DataTable(
        id=_REPO_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _REPO_TABLE_COLS],
        data=rows,
        row_selectable=False,
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _REPO_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '500px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    alert = dbc.Alert(error_msg, id=_OP_ALERT_ID, color='danger', dismissable=True, fade=True,
                      is_open=(error_msg is not None), class_name="mb-3")
    return html.Div([alert, data_table])


def serve_layout() -> html.Div:
    """
    Serve the layout for the "backup repository contents" page. The page includes a table listing all files stored in
    the portal's backup repository, in a pseudo hierarchical fashion. The page content is read-only at this time. Only
    authorized users with administrative privileges should have access to this page.

    Returns:
        An HTML Div rendering the user account management page.
    """
    portal_user: Optional[PortalUser] = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
    is_admin = (portal_user is not None) and portal_user.is_admin()
    return _table_of_repo_contents(is_admin)
