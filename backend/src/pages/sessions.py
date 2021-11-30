"""
sessions.py: The page in the Lisberger lab's database portal app devoted to reviewing any experiment session stored in
    the lab database. (/sessions endpoint)

This page presents a filterable table of all recorded experiment sessions for which trial response data have been stored
in the lab database. A Boostrap Popover element encapsulates a group of widgets for filtering the table's content by
recording date, experimenter, subject, or research project. The user can select a session in the table to display a
"detail panel" below the table. The session detail panel is TODO.

TODO: It would likely be useful if the session table had columns for total # of trials and total # of neural units
 recorded during the session. But it is expensive to collect that information by querying the database for each and
 every session -- eventually there will be hundreds of sessions stored. This argues for adding two attributes to the
 Session table, 'num_units' and 'num_trials'.

@author: sruffner
@created: 29nov2021
"""
import logging
from typing import List, Optional, Dict

import dash
import dash_table as dt
import dash_html_components as html
import dash_bootstrap_components as dbc
import dash_core_components as dcc
from dash.dependencies import Output, Input, State

import database.table_info as ti
from app import app
from database.table_ops import fetch_restrict_proj, fetch_attribute_values, num_table_rows

logger = logging.getLogger(__name__)


_SESSION_TABLE_ID: str = "session_list"
""" The ID assigned to the Dash DataTable presenting the list of experiment sessions in the database. """

_SESSION_TABLE_ATTRS: List[str] = [
    'experimenter', 'subj_id', 'session_date', 'session_sfx', 'study_id', 'committed'
]
""" DBTable.SESSION attributes that are fetched from database for display in sessions table. """

_SESSION_TABLE_COLS: List[ti.Column] = [
    ti.Column('session', 'Date/Suffix', '125px', False),
    ti.Column('full_name', 'Experimenter', '150px', False),
    ti.Column('subj_id', 'Subject', '75px', False),
    ti.Column('study_title', 'Research Project', '150px', False),
    ti.Column('committed', 'Added', '125px', False)   # TODO: Temporary -- just want to verify sort by when committed
]
""" Defined columns for the sessions table."""

_SESSION_TABLE_DIV_ID: str = "nt_div"
""" ID assigned to the HTML Div element in which the table of experiment sessions is embedded. """


def _fetch_sessions(restriction: Optional[List[str]]) -> List[Dict[str, ti.AttributeValue]]:
    """
    Fetch information about some or all experiment sessions stored in the lab database.

    Args:
        restriction: A list of string conditions (in DataJoint syntax) that all retrieved experiment session entries
            must satisfy. If None, the method retrieves all sessions currently in the database.
    Returns:
        A list of dictionaries, one for each experiment session retrieved. Each dictionary includes the attributes that
            are displayed in the table of sessions -- see _NEURON_TABLE_COLS.
    """
    rows = fetch_restrict_proj([ti.DBTable.SESSION], [restriction], _SESSION_TABLE_ATTRS)
    studies = fetch_restrict_proj([ti.DBTable.STUDY], None, ['study_title'])
    users = fetch_restrict_proj([ti.DBTable.USER], None, ['full_name'])
    if (rows is None) or (studies is None) or (users is None):
        logger.error("A database error occurred while fetching experiment session information from database",
                     exc_info=True)
        return []

    # sort in reverse chrono order by date that each session was added to the database (so most recent adds are first!)
    rows.sort(key=lambda x: x['committed'], reverse=True)

    # prepare values in "composed" columns
    study_map = {r['study_id']: r['study_title'] for r in studies}
    user_map = {r['username']: r['full_name'] for r in users}
    for row in rows:
        row['full_name'] = user_map[row['experimenter']]
        row['study_title'] = study_map[row['study_id']]
        row['session'] = f"{row['session_date']} ({row['session_sfx']})"
    return rows


def _table_of_sessions() -> dt.DataTable:
    """
    Prepare the Dash DataTable displaying information on all experiment sessions stored in the lab database.
    """
    rows = _fetch_sessions(None)
    data_table = dt.DataTable(
        id=_SESSION_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _SESSION_TABLE_COLS],
        data=rows,
        row_selectable='single',
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _SESSION_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '200px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )
    return data_table


_FILTER_UNUSED: str = "<none>"
""" Pseudo-value in any filter select widget indicating that filter is unused. """
_FILTER_RAISE_ID: str = "session_filter_raise"
""" ID of button widget that raises the Bootstrap Popover element in which the filter controls are arranged. """
_FILTER_POPOVER_ID: str = "session_filter_popover"
""" ID of Bootstrap Popover element in which the filter controls are arranged. """
_FILTER_EXP_ID: str = "session_filter_exp"
""" ID of Bootstrap Select element to filter session table by the experimenter. """
_FILTER_SUBJ_ID: str = "session_filter_subj"
""" ID of Bootstrap Select element to filter session table by the experiment subject. """
_FILTER_STUDY_ID: str = "session_filter_study"
""" ID of Bootstrap Select element to filter session table by research project/study. """
_FILTER_CLEAR_ID: str = "session_filter_clear"
""" ID of button widget that resets all filter controls. """
_FILTER_COUNT_ID: str = "session_filter_count"
""" ID of label that reflects how many sessions were found given the current state of the filter controls. """


def _filter_group() -> dbc.Row:
    """
    Helper method prepares the filter control group - a collection of widgets in a Bootstrap Popover element that
    control filtering of the experiment session entries displayed in the main table on this panel. The Popover is raised
    when the mouse hovers over the "Filter" button. The control group lets the user filter the results by experimenter,
    subject, or research study.

    Returns:
        A Bootstrap Row container holding the "Filter" button and filter widgets embedded in a Popover.
    """
    # if None is returned, it's a database error. In that case, we disable the Filter button
    experimenters = fetch_restrict_proj([ti.DBTable.USER], None, ['username', 'full_name']) or []
    experimenters.sort(key=lambda x: x['full_name'])
    experimenters.insert(0, {'username': _FILTER_UNUSED, 'full_name': _FILTER_UNUSED})
    subjects = fetch_attribute_values(ti.DBTable.SUBJECT, "subj_id")
    subjects.sort()
    subjects.insert(0, _FILTER_UNUSED)
    studies = fetch_restrict_proj([ti.DBTable.STUDY], None, ['study_id', 'study_title']) or []
    studies.sort(key=lambda x: x['study_title'])
    studies.insert(0, {'study_id': _FILTER_UNUSED, 'study_title': _FILTER_UNUSED})

    num_sessions = num_table_rows(ti.DBTable.SESSION)

    experimenter_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupAddon("Experimenter =", addon_type="prepend"),
        dbc.Select(id=_FILTER_EXP_ID,
                   options=[{"label": opt['full_name'], "value": opt['username']} for opt in experimenters],
                   value=_FILTER_UNUSED)
    ], size='sm'), className='mr-1 ml-1 mb-2')
    subject_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupAddon("Subject =", addon_type="prepend"),
        dbc.Select(id=_FILTER_SUBJ_ID,
                   options=[{"label": opt, "value": opt} for opt in subjects], value=_FILTER_UNUSED)
    ], size='sm'), className='mr-1 ml-1 mb-2')
    study_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupAddon("Study =", addon_type="prepend"),
        dbc.Select(id=_FILTER_STUDY_ID,
                   options=[{"label": opt['study_title'], "value": str(opt['study_id'])} for opt in studies],
                   value=_FILTER_UNUSED)
    ], size='sm'), className='mr-1 ml-1 mb-2')
    control_row = dbc.Row([
        dbc.Button("Clear", id=_FILTER_CLEAR_ID, className="mr-3", size='sm', color='primary'),
        dbc.Label(f"{num_sessions} sessions found", id=_FILTER_COUNT_ID, className='my-auto')
    ], className='mr-1 ml-1')

    return dbc.Row([
        dbc.Col(dbc.Button("Filter", id=_FILTER_RAISE_ID, size='sm', color='primary')),
        dbc.Popover(
            [
                dbc.PopoverBody([experimenter_row, subject_row, study_row, control_row]),
            ],
            id=_FILTER_POPOVER_ID,
            target=_FILTER_RAISE_ID,
            trigger="hover", placement='right-start'
        )
    ], className="mb-3")


_COLLAPSE_ID: str = "session_detail_collapse_id"
""" ID of Dash Bootstrap Collapse element wrapping the tabbed panel displaying details for a selected neuron. The
element is hidden when no neuron is selected. """


def serve_layout() -> html.Div:
    """
    Generate the HTML Div that lays out the page on which clients can explore data from any experiment session stored in
    the lab database.
    """
    detail_panel = html.Div("*** Session Detail Panel - NOT YET IMPLEMENTED ***", className='mt-3')
    """ Rendering of a detail panel for the experiment session selected from the sessions list. """

    markdown = dcc.Markdown(
        '''Click on the radio button next to a row in the table to display a detail panel for the selected experiment
        session. Hover or click on the **Filter** button to filter the list of experiments by recorded date, researcher,
        subject, or research study. **Recently added sessions are listed in.**'''
    )
    layout = html.Div([
        dbc.Container([
            dbc.Row(dbc.Col(html.H3("Review experiment sessions stored in the laboratory database",
                                    className="text-center")),
                    className="mb-3 mt-3"),
            dbc.Row(dbc.Col(html.Div(markdown), className="mb-3")),
            dbc.Row(dbc.Col(html.Div(id=_SESSION_TABLE_DIV_ID, children=_table_of_sessions())), className="mb-3"),
            _filter_group(),
            dbc.Row(dbc.Col(dbc.Collapse(detail_panel, id=_COLLAPSE_ID)))
        ])
    ])
    return layout


# this clientside callback highlights all cells in the selected row
app.clientside_callback(
    """
    function(rows) {
        let style = [];
        if (Array.isArray(rows) && (rows.length > 0) && Number.isInteger(rows[0])) {
            style = [{"if": {"row_index": rows[0]}, "background-color": "rgba(176, 196, 222, 0.5)"}];
        }
        return style;
    }
    """,
    Output(_SESSION_TABLE_ID, "style_data_conditional"),
    Input(_SESSION_TABLE_ID, "selected_rows")
)


def _session_filter_restrictions(username: str, subj_id: str, study_id: str) -> Optional[List[str]]:
    """
    Helper method prepares a list of string conditions -- in DataJoint syntax -- defining the filters that should be
    applied to restrict the set of experiment sessions displayed in the main table on this panel.

    Args:
        username: If not _FILTER_UNUSED, restrict to sessions recorded by this experimenter.
        subj_id: If not _FILTER_UNUSED, restrict to sessions recorded in this subject.
        study_id: If not _FILTER_UNUSED, restrict to sessions belonging to the research study with this ID. This should
            be an integer-valued string; it will be cast to int.
    Returns:
        The list of restriction conditions. For example, ["username = 'sar'", "subj_id = 'yolo'"]. If no filter
            restrictions are set, returns None.
    """
    restrictions = list()
    if username != _FILTER_UNUSED:
        restrictions.append(f"experimenter = '{username}'")
    if subj_id != _FILTER_UNUSED:
        restrictions.append(f"subj_id = '{subj_id}'")
    if study_id != _FILTER_UNUSED:
        restrictions.append(f"study_id = {int(study_id)}")
    return restrictions if (len(restrictions) > 0) else None


@app.callback([Output(_FILTER_EXP_ID, "value"), Output(_FILTER_SUBJ_ID, "value"), Output(_FILTER_STUDY_ID, "value")],
              [Input(_FILTER_CLEAR_ID, "n_clicks")])
def clear_filters(n_clear):
    if not n_clear:
        raise dash.exceptions.PreventUpdate
    return _FILTER_UNUSED, _FILTER_UNUSED, _FILTER_UNUSED


filter_ids = [_FILTER_EXP_ID, _FILTER_SUBJ_ID, _FILTER_STUDY_ID]
input_vector = [Input(sel_id, "value") for sel_id in filter_ids]
state_vector = [State(sel_id, "value") for sel_id in filter_ids]


@app.callback([Output(_FILTER_COUNT_ID, "children"), Output(_SESSION_TABLE_ID, "selected_rows"),
               Output(_SESSION_TABLE_ID, "data")], input_vector, state_vector)
def update_filter_result_count(*args):
    ctx = dash.callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate
    restrictions = _session_filter_restrictions(args[3], args[4], args[5])
    rows = _fetch_sessions(restrictions)
    return f"{len(rows)} sessions found", [], rows


@app.callback(Output(_COLLAPSE_ID, "is_open"),
              [Input(_SESSION_TABLE_ID, "selected_rows")], [State(_SESSION_TABLE_ID, "data")])
def show_hide_detail_pane(selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    # TODO: Prepare session detail panel for selected session
    #  summary_tab = _unit_summary(selected_row) if selected_row else html.Div("Not available.")
    #  response_tab = _response_panel(selected_row) if selected_row else html.Div("Not available.")
    return selected_row is not None

