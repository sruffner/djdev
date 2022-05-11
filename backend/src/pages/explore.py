"""
explore.py: The starting page in the Lisberger lab's database portal app devoted to searching database content
(/explore endpoint).

    This is the only "public" page in the portal (does not require login), and it serves as the landing page when one
navigates to the portal website.  It provides two ways to search for datasets -- by experiment session or by neural
unit. Either way, the user selects a session or neural unit, and a detail pane appears with two tabs:

    Session Info -- Summary information about the experiment session selected.

    Trial Data -- Here is where you can view any trial data recorded during the experiment. You can choose to view the
    target and eye trajectories for a single trial rep, or the mean eye trajectory ("behavioral response") across
    all successful reps of a given trial protocol. If neural units were recorded during the experiment, you can
    choose which unit to display alongside the behavioral response. Mean neuronal response is characterized as
    mean firing rate +/-1 SEM. Alternatively, you can display discharge statistics (auto-correlogram, inter-spike
    interval histogram) for the selected unit. Mean firing rate and discharge statistics are only available for
    trial protocols that are conducive to aggregating response data. Such protocols will have at most one random
    variable defined, and that random variable must control the duration of a single segment in the trial (not
    necessarily the first one).

@author: sruffner
@created: 15jun2021
"""
import json
from datetime import date
from typing import List, Optional, Dict, Any, Tuple, Union

from dash import callback, callback_context, html, dcc, dash_table as dt, exceptions as dash_exc, no_update, Input, \
    Output, State
import dash_bootstrap_components as dbc
import numpy as np
import plotly.express as px

import database.table_info as ti
from config.app_logging import get_application_logger
from database.data_plots import average_response_figure, single_trial_response_figure, trial_target_trajectory_figure, \
    discharge_statistics_figure
from database.table_ops import fetch_restrict_proj, fetch_rows, fetch_attribute_values, num_table_rows, fetch_one_row, \
    fetch_any_proj
from database.trial_data_ops import trial_protocols_for_session, trial_protocols_for_neuron, trials_for_session, \
    trials_for_neuron, get_trial_protocol_definition, retrieve_trial_reps_for_neuron
from pages.commit import display_trial_protocol_definition
from pages.download_modal import render_download_modal_and_button
from sglportalapi.util import check_date


_SESSION_TAB_ID: str = "explore_session_tab"
""" ID of 'Session Info' tab panel in which the selected session's summary information is displayed. """
_DATA_TAB_ID: str = "explore_data_tab"
""" ID of 'Trial Data' tab panel displaying trial data recorded during the selected session. """
_COLLAPSE_ID: str = "explore_detail_collapse_id"
""" ID of Dash Bootstrap Collapse element wrapping the panel displaying details for a selected experiment
session. The element is hidden when no session is selected. """

_SELECTED_ROW_ID: str = "explore_selected_row_id"
""" ID of Dash Store component that holds the dictionary defining the currently selected row in search results table."""

_SEARCH_MODE_RADIO_ID: str = 'search-mode-radio'
""" ID of the Bootstrap RadioItems widget that selects the search mode. """
_SESSION_MODE: int = 1
""" In this search mode, the results table displays individual experiment sessions. """
_UNIT_MODE: int = 2
""" In this search mode, the results table displays individual neural units. """


def serve_layout() -> html.Div:
    """
    Generate the HTML Div that lays out the page on which clients can interactively explore the lab database content.
    """
    detail_panel = dbc.Tabs(
        [
            dbc.Tab(dbc.Card(dbc.CardBody(children=[], id=_SESSION_TAB_ID), class_name='mt-2'), label="Session Info"),
            dbc.Tab(dbc.Card(dbc.CardBody(children=[], id=_DATA_TAB_ID), class_name='mt-2'), label="Trial Data")
        ]
    )
    """ Rendering of a tabbed panel in which details about an experiment session are displayed. """

    search_mode_radio = dbc.RadioItems(
        options=[
            {"label": "Experiment sessions", "value": _SESSION_MODE},
            {"label": "Neural recordings", "value": _UNIT_MODE}
        ],
        value=_SESSION_MODE, id=_SEARCH_MODE_RADIO_ID, inline=True,
        label_checked_style=dict(fontWeight='bold')
    )

    usage_instructions = dcc.Markdown(
        '''*Instructions:* Search the set of experiment sessions or individual neuron recordings stored in the lab 
        database. Hover or click on the **Filter** button to specify filter criteria to narrow the search results. Then 
        click on the radio button next to a row in the results table to display details on the selected experiment 
        session or neural unit recording.'''
    )

    # initially, the search results table lists all experiment sessions stored in database
    rows = _fetch_search_results()
    table_cols = _SESSION_TABLE_COLS
    data_table = dt.DataTable(
        id=_SEARCH_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in table_cols],
        data=rows,
        row_selectable='single',
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in table_cols],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '200px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )

    card = dbc.Card([
        dbc.CardHeader("Explore datasets and other content within the lab database"),
        dbc.CardBody([
            dbc.Row(dbc.Col(usage_instructions, width=12)),
            dbc.Row([
                dbc.Col(search_mode_radio, width='auto'),
                dbc.Col(_filter_group(), width='auto')
            ], align='center', class_name='mb-2'),
            dbc.Row(dbc.Col(html.Div(children=data_table)), class_name="mb-3"),
            dbc.Row(dbc.Col(dbc.Collapse(detail_panel, id=_COLLAPSE_ID)))
        ]),
    ], class_name='mx-5 my-5')

    stored_selection = dcc.Store(id=_SELECTED_ROW_ID)
    return html.Div([card, stored_selection])


_SEARCH_TABLE_ID: str = "search_table"
""" The ID assigned to the Dash DataTable presenting filtered search results. """

_SESSION_TABLE_COLS: List[ti.Column] = [
    ti.Column('session_date', 'Date', '125px', False),
    ti.Column('full_name', 'Experimenter', '150px', False),
    ti.Column('subj_id', 'Subject', '75px', False),
    ti.Column('study_title', 'Research Project', '250px', False),
    ti.Column('num_units', '#Units', '75px', False),
    ti.Column('num_trials', '#Trials', '75px', False),
    ti.Column('committed', 'Added on', '125px', False),
]
""" Defined columns for the search results table when searching by experiment session. """

_NEURON_TABLE_COLS: List[ti.Column] = [
    ti.Column('session_date', 'Recorded On', '100px', False),
    ti.Column('unit_id', 'Unit #', '50px', False),
    ti.Column('nt_name', 'Neuron Type', '100px', False),
    ti.Column('unit_rate', 'Rate (Hz)', '100px', False),
    ti.Column('unit_spikes', '#Spikes', '100px', False),
    ti.Column('full_name', 'Experimenter', '150px', False),
    ti.Column('subj_id', 'Subject', '100px', False)
]
""" Defined columns for the search results table when searching by neuron. """


def _fetch_search_results(mode: int = _SESSION_MODE, restrictions: Optional[List[str]] = None) -> \
        List[Dict[str, ti.AttributeValue]]:
    """
    Fetch information from the database for display in the Dash Datatable on this panel. If searching "by session",
    the method returns selected information on each experiment session in the Session database table: experimenter,
    subj_id, session_date, session_sfx, study_id, committed. If searching "by neuron", it returns selected information
    on each neural unit stored in the Session.Neuron database table: experimenter, subj_id, session_date, session_sfx,
    unit_id, unit_rate, unit_spikes. In both cases, certain filter criteria may be applied to narrow the search results.

    On the filter criteria -- see _filter_restrictions(). In session search mode, all the defined criteria are
    applied to the Session table. In neuron search mode, all the defined criteria are applied to Session.Neuron with
    one exceptioni. To find all neurons recorded as part of a specific research study, we restrict the search result to
    experiment sessions belonging to that study.

    Args:
        mode: The search mode -- either by session or by neural unit. Default = _SESSION_MODE.
        restrictions: A list of string conditions (in DataJoint syntax) that all retrieved sessions or neural units in
            the search result must satisfy. If None, the method retrieves all sessions or neural units currently in the
            database. Default = None.
    Returns:
        A list of dictionaries, one for each entity (experiment session or recorded neural unit) retrieved. Each
            dictionary includes the primary key that uniquely identifies the entity, plus some additional attributes
            displayed in the search results table.
    """
    if mode == _UNIT_MODE:
        study_id_restriction = None
        if restrictions is not None:
            for r in restrictions:
                if r.startswith('study_id'):
                    study_id_restriction = r
                    break
            if study_id_restriction is not None:
                restrictions.remove(study_id_restriction)
        if study_id_restriction is None:
            rows = fetch_restrict_proj([ti.DBTable.SESSION_NEURON], [restrictions],
                                       ['unit_type', 'unit_rate', 'unit_spikes'])
        else:
            rows = fetch_restrict_proj([ti.DBTable.SESSION_NEURON, ti.DBTable.SESSION],
                                       [restrictions, [study_id_restriction]],
                                       ['unit_type', 'unit_rate', 'unit_spikes'])
        n_types = fetch_rows(ti.DBTable.NEURON_TYPE)
        users = fetch_restrict_proj([ti.DBTable.USER], None, ['full_name'])
        if (rows is None) or (n_types is None) or (users is None):
            get_application_logger().error(
                "A database error occurred while fetching neural unit information from database", exc_info=True)
            return []

        # prepare values in "composed" columns
        neuron_type_map = {r['nt_id']: r['nt_name'] for r in n_types}
        user_map = {r['username']: r['full_name'] for r in users}
        for row in rows:
            row['nt_name'] = neuron_type_map[row['unit_type']]
            row['full_name'] = user_map[row['experimenter']]
        return rows
    else:
        rows = fetch_restrict_proj([ti.DBTable.SESSION], [restrictions],
                                   ['study_id', 'num_units', 'num_trials', 'committed'])
        studies = fetch_restrict_proj([ti.DBTable.STUDY], None, ['study_title'])
        users = fetch_restrict_proj([ti.DBTable.USER], None, ['full_name'])
        if any([(r is None) for r in [rows, studies, users]]):
            get_application_logger().error(
                "A database error occurred while fetching experiment session information from database", exc_info=True)
            return []

        # sort in reverse chrono order by date that session was added to the database (so most recent adds are first!)
        rows.sort(key=lambda x: x['committed'], reverse=True)

        # prepare values in "composed" columns
        study_map = {r['study_id']: r['study_title'] for r in studies}
        user_map = {r['username']: r['full_name'] for r in users}
        for row in rows:
            row['full_name'] = user_map[row['experimenter']]
            row['study_title'] = study_map[row['study_id']]

    return rows


_FILTER_UNUSED: str = "<none>"
""" Pseudo-value in any filter select widget indicating that filter is unused. """
_FILTER_RAISE_ID: str = "explore_filter_raise"
""" ID of button widget that raises the Bootstrap Popover element in which the filter controls are arranged. """
_FILTER_POPOVER_ID: str = "explore_filter_popover"
""" ID of Bootstrap Popover element in which the filter controls are arranged. """
_FILTER_NTYPE_ID: str = "explore_filter_ntype"
""" ID of Bootstrap Select element to filter search results by the neuron type (hidden in session search mode). """
_FILTER_SPIKES_ID: str = "explore_filter_spikes"
""" 
ID of Bootstrap Input element to filter search results by total spike count exceeding value in this element (hidden in
session search mode).
"""
_FILTER_EXP_ID: str = "explore_filter_exp"
""" ID of Bootstrap Select element to filter search results by the experimenter. """
_FILTER_SUBJ_ID: str = "explore_filter_subj"
""" ID of Bootstrap Select element to filter search results by the experiment subject. """
_FILTER_STUDY_ID: str = "explore_filter_study"
""" ID of Bootstrap Select element to filter search results by research study. """
_FILTER_DATE_ID: str = "explore_filter_date"
""" ID of Bootstrap Select element to choose how search results are filtered by a specified date. """
_DATE_PICKER_ID: str = "explore_filter_date_picker"
""" ID of Dash date picker widget that specifies the date for filtering the search table results. """
_FILTER_CLEAR_ID: str = "explore_filter_clear"
""" ID of button widget that resets all filter controls. """
_FILTER_COUNT_ID: str = "explore_filter_count"
""" ID of label that reflects how many search results were found given the current state of the filter controls. """


def _filter_group() -> dbc.Row:
    """
    Helper method prepares the filter control group - a collection of widgets in a Bootstrap Popover element that
    control filtering of the search results table. It is initially configured for searching by session -- controls
    specific to searching by neuron are hidden. The Popover is raised when the mouse hovers over the "Filter Results"
    button.

    Returns:
        A Bootstrap Row container holding the "Filter Results" button and filter widgets embedded in a Popover.
    """
    # if None is returned, it's a database error (we're silent about that here)
    experimenters = fetch_restrict_proj([ti.DBTable.USER], None, ['full_name']) or []
    experimenters.sort(key=lambda x: x['full_name'])
    experimenters.insert(0, {'username': _FILTER_UNUSED, 'full_name': _FILTER_UNUSED})
    subjects = fetch_attribute_values(ti.DBTable.SUBJECT, "subj_id")
    subjects.sort()
    subjects.insert(0, _FILTER_UNUSED)
    studies = fetch_restrict_proj([ti.DBTable.STUDY], None, ['study_title']) or []
    studies.sort(key=lambda x: x['study_title'])
    studies.insert(0, {'study_id': _FILTER_UNUSED, 'study_title': _FILTER_UNUSED})
    neuron_types = fetch_rows(ti.DBTable.NEURON_TYPE) or []  # again, protect against a DB error
    neuron_types.sort(key=lambda x: x['nt_name'])
    neuron_types.insert(0, {'nt_id': _FILTER_UNUSED, 'nt_name': _FILTER_UNUSED})
    date_choices = [_FILTER_UNUSED, 'on', 'before', 'after']

    num_sessions = num_table_rows(ti.DBTable.SESSION)

    # these two filter controls are specific to searching individual neurons, so they are hidden initially
    neuron_type_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupText("Neuron Type"),
        dbc.Select(id=_FILTER_NTYPE_ID,
                   options=[{"label": opt['nt_name'], "value": str(opt['nt_id'])} for opt in neuron_types],
                   value=_FILTER_UNUSED, disabled=True)
    ], size='sm'), class_name='g-0 mx-1 mb-2')
    num_spikes_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupText("#Spikes >="),
        dbc.Input(id=_FILTER_SPIKES_ID, type='number', min=0, debounce=True, value=0, disabled=True)
    ], size='sm'), class_name='g-0 mx-1 mb-2')

    experimenter_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupText("Experimenter"),
        dbc.Select(id=_FILTER_EXP_ID,
                   options=[{"label": opt['full_name'], "value": opt['username']} for opt in experimenters],
                   value=_FILTER_UNUSED)
    ], size='sm'), class_name='g-0 mx-1 mb-2')
    subject_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupText("Subject"),
        dbc.Select(id=_FILTER_SUBJ_ID,
                   options=[{"label": opt, "value": opt} for opt in subjects], value=_FILTER_UNUSED)
    ], size='sm'), class_name='g-0 mx-1 mb-2')
    study_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupText("Study"),
        dbc.Select(id=_FILTER_STUDY_ID,
                   options=[{"label": opt['study_title'], "value": opt['study_id']} for opt in studies],
                   value=_FILTER_UNUSED)
    ], size='sm'), class_name='g-0 mx-1 mb-2')
    date_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupText("Recorded: "),
        dbc.Select(id=_FILTER_DATE_ID,
                   options=[{"label": opt, "value": opt} for opt in date_choices], value=_FILTER_UNUSED),
        dcc.DatePickerSingle(id=_DATE_PICKER_ID, date=date.today(), display_format='YYYY-MM-DD', className='ms-2')
    ], size='sm'), className='g-0 mx-1 mb-3')
    control_row = dbc.Row([
        dbc.Col(dbc.Button("Clear", id=_FILTER_CLEAR_ID, size='sm'), width='auto', class_name='me-2'),
        dbc.Col(dbc.Label(f"{num_sessions} sessions found", id=_FILTER_COUNT_ID, size='sm'), width='auto')
    ], align='center', class_name='g-0 mx-1')

    return dbc.Row([
        dbc.Col(dbc.Button("Filters", id=_FILTER_RAISE_ID, size='sm')),
        dbc.Popover(
            [
                dbc.PopoverBody([neuron_type_row, num_spikes_row, experimenter_row, subject_row, study_row,
                                 date_row, control_row]),
            ],
            id=_FILTER_POPOVER_ID,
            target=_FILTER_RAISE_ID,
            trigger="hover", placement='right-start'
        )
    ])


def _filter_restrictions(mode: int, nt_id: str, min_spikes: int, username: str, subj_id: str,
                         study_id: str, date_op: str, date_iso: str) \
        -> Optional[List[str]]:
    """
    Helper method prepares a list of string conditions -- in DataJoint syntax -- defining the filters that should be
    applied to restrict the set of search results displayed in the main table on this panel.

    Args:
        mode: Search mode -- by session or by neural unit.
        nt_id: If searching by neural unit and this is not _FILTER_UNUSED, restrict to neural units with this type ID
            (will be cast to int). Argument ignored when searching by session.
        min_spikes: If searching by neural unit, restrict to neural units for which the total recorded spike count
            matches or exceeds this number. Argument ignored when searching by session.
        username: If not _FILTER_UNUSED, restrict to sessions recorded by this experimenter.
        subj_id: If not _FILTER_UNUSED, restrict to sessions recorded in this subject.
        study_id: If not _FILTER_UNUSED, restrict to sessions belonging to research study with this ID (cast ot int).
        date_op: If not _FILTER_UNUSED, restrict by recording session date ("on", "before", or "after")
        date_iso: The date in ISO format at 'YYYY-MM-DD'. If invalid, no date restriction is prepared.
    Returns:
        The list of restriction conditions. For example, ["username = 'sar'", "session_date < '2020-03-05'"]. If no
            filter restrictions are set, returns None.
    """
    restrictions = list()
    if (mode == _UNIT_MODE) and (nt_id != _FILTER_UNUSED):
        restrictions.append(f"unit_type = {int(nt_id)}")
    if (mode == _UNIT_MODE) and isinstance(min_spikes, int) and (min_spikes > 0):
        restrictions.append(f"unit_spikes >= {min_spikes}")
    if username != _FILTER_UNUSED:
        restrictions.append(f"experimenter ='{username}'")
    if subj_id != _FILTER_UNUSED:
        restrictions.append(f"subj_id ='{subj_id}'")
    if study_id != _FILTER_UNUSED:
        restrictions.append(f"study_id = {int(study_id)}")
    if (date_op != _FILTER_UNUSED) and check_date(date_iso):
        op_map = {'on': '=', 'before': '<', 'after': '>'}
        restrictions.append(f"session_date {op_map[date_op]} '{str(date_iso)}'")
    return restrictions if (len(restrictions) > 0) else None


filter_ids = [_FILTER_NTYPE_ID, _FILTER_SPIKES_ID, _FILTER_EXP_ID, _FILTER_SUBJ_ID, _FILTER_STUDY_ID,
              _FILTER_DATE_ID]
input_vector = [Input(sel_id, "value") for sel_id in filter_ids]
input_vector.append(Input(_DATE_PICKER_ID, "date"))
input_vector.append(Input(_SEARCH_MODE_RADIO_ID, "value"))
state_vector = [State(sel_id, "value") for sel_id in filter_ids]
state_vector.append(State(_DATE_PICKER_ID, "date"))
state_vector.append(State(_SEARCH_MODE_RADIO_ID, "value"))


@callback(
    [Output(_FILTER_COUNT_ID, "children"),  Output(_FILTER_NTYPE_ID, 'disabled'), Output(_FILTER_SPIKES_ID, 'disabled'),
     Output(_SEARCH_TABLE_ID, "selected_rows"), Output(_SEARCH_TABLE_ID, "data"),
     Output(_SEARCH_TABLE_ID, "columns"), Output(_SEARCH_TABLE_ID, "style_cell_conditional")],
    input_vector, state_vector)
def update_search_results(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate

    # no need for update when user changes the date but does not filter on that date
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger_id == _DATE_PICKER_ID and args[13] == _FILTER_UNUSED:
        raise dash_exc.PreventUpdate

    mode = int(args[7]) if trigger_id == _SEARCH_MODE_RADIO_ID else int(args[-1])  # current search mode
    min_spikes = 0 if (args[9] is None) else int(args[9])
    restrictions = _filter_restrictions(mode, args[8], min_spikes, args[10], args[11], args[12], args[13], args[14])
    rows = _fetch_search_results(mode, restrictions)
    table_cols = _SESSION_TABLE_COLS if mode == _SESSION_MODE else _NEURON_TABLE_COLS
    columns = [{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
               for col in table_cols]
    style_cell_conditional = [{'if': {'column_id': col.id}, 'width': col.width} for col in table_cols]

    return f"{len(rows)} {'units' if mode == _UNIT_MODE else 'sessions'} found", \
           mode == _SESSION_MODE, mode == _SESSION_MODE, [], rows, columns, style_cell_conditional


@callback(
    [Output(_FILTER_NTYPE_ID, "value"), Output(_FILTER_SPIKES_ID, "value"), Output(_FILTER_EXP_ID, "value"),
     Output(_FILTER_SUBJ_ID, "value"), Output(_FILTER_STUDY_ID, "value"), Output(_FILTER_DATE_ID, "value")],
    [Input(_FILTER_CLEAR_ID, "n_clicks")]
)
def clear_filters(n_clear):
    if not n_clear:
        raise dash_exc.PreventUpdate
    return _FILTER_UNUSED, 0, _FILTER_UNUSED, _FILTER_UNUSED, _FILTER_UNUSED, _FILTER_UNUSED


_EXP_POPOVER_TGT: str = 'exp-popover-tgt'
""" 
ID of HTML span in session summary table that displays the experimenter's name and serves as a target for the 
Bootstrap Popover displaying additional information on that experimenter.
"""
_SUBJ_POPOVER_TGT: str = 'subj-popover-tgt'
""" 
ID of HTML span in session summary table that displays the experiment subject's ID/nickname; it serves as the target for
the Bootstrap Popover displaying additional information on that subject.
"""
_STUDY_POPOVER_TGT: str = 'study-popover-tgt'
""" 
ID of HTML span in session summary table that displays the research project to which session belongs; it serves as the
target for the Bootstrap Popover displaying additional information on that research project.
"""


def _session_info_tabpane(session: Dict[str, Any]) -> html.Div:
    """
    Helper method prepares summary information for the specified experiment session and prepares the content of the
    "Session Info" tab in the detail panel that appears below the search results table.

    Args:
        session: A dictionary corresponding to one row in the search results table. At a minimum, it must include the
            primary key-value pairs that uniquely identify an experiment session in the database.
    Returns:
        An HTML Div that renders the contents of the "Session Info" tab in the detail panel.
    """
    # retrieve the full session record, including info about the electrode recording (if applicable)
    try:
        pk = {k: session[k] for k in ti.primary_key_of(ti.DBTable.SESSION, False)}
        session = fetch_one_row(ti.DBTable.SESSION, pk)
        experimenter = fetch_one_row(ti.DBTable.USER, dict(username=session['experimenter']))
        study = fetch_one_row(ti.DBTable.STUDY, dict(study_id=session['study_id']))
        rig_loc = fetch_attribute_values(ti.DBTable.RIG, 'rig_loc', dict(rig_id=session['rig_id']))[0]
        num_trials_presented = num_table_rows(ti.DBTable.TRIAL, restriction=[pk])
        num_trials_completed = num_table_rows(ti.DBTable.TRIAL, restriction=[pk, dict(trial_success=True)])
        num_units = num_table_rows(ti.DBTable.SESSION_NEURON, restriction=[pk])
        if num_units > 0:
            ephys = fetch_one_row(ti.DBTable.SESSION_EPHYS, pk)
            brain_area = fetch_attribute_values(ti.DBTable.BRAIN_AREA, 'ba_name', dict(ba_id=ephys['ba_id']))[0]
        else:
            ephys = None
            brain_area = None
    except Exception as e:
        get_application_logger().error(
            f"Error while fetching info for experiment session summary: {str(e)}", exc_info=True)
        return html.Div(dbc.Alert("Failed to retrieve information on selected session from the database", is_open=True))

    info_table_rows = [
        ["Recorded on", f"{session['session_date']} [subsession ID:  {session['session_sfx']}]"],
        ["Committed on", f"{session['committed']}"],
        ["Experimenter", html.Span(f"{experimenter['full_name']}", id=_EXP_POPOVER_TGT,
                                   style={"textDecoration": "underline", "cursor": "pointer"})],
        ["Subject", html.Span(f"{session['subj_id']}", id=_SUBJ_POPOVER_TGT,
                              style={"textDecoration": "underline", "cursor": "pointer"})],
        ["Research Project", html.Span(f"{study['study_title']}", id=_STUDY_POPOVER_TGT,
                                       style={"textDecoration": "underline", "cursor": "pointer"})],
        ["Rig", f"{session['rig_id']} [{rig_loc}]"],
        ["Trials", f"{num_trials_completed} out of {num_trials_presented} completed"]
    ]
    info_table_body = html.Tbody([
        html.Tr([html.Td(row[0], className='text-right p-2'), html.Td(row[1], className='text-left text-info p-2')])
        for row in info_table_rows
    ], className='small')
    info_table = dbc.Table([info_table_body], striped=True, bordered=True)

    notes_grp = dbc.InputGroup([
        dbc.InputGroupText("Session Notes"),
        dbc.Textarea(value=session['session_notes'], rows=4, readonly=True)
    ])

    if num_units > 0:
        ephys_table_rows = [
            ["# Units Recorded", f"{num_units}"],
            ["Brain Area", f"{brain_area}"],
            ["Electrode Source", f"{ephys['ephys_src']}"],
            ["Probe Type", f"{ephys['probe_type']}"],
            ["Probe Location", f"X={ephys['probe_x']} mm, Y={ephys['probe_y']} mm, Z={ephys['probe_depth']} mm"],
            ["Sampling Rate", f"{ephys['sampling_rate']} Hz"]
        ]
    else:
        ephys_table_rows = [
            ["# Units Recorded", "0"],
            ["Brain Area", "N/A"],
            ["Electrode Source", "N/A"],
            ["Probe Type", "N/A"],
            ["Probe Location", "N/A"],
            ["Sampling Rate", "N/A"]
        ]
    ephys_table_body = html.Tbody([
        html.Tr([html.Td(row[0], className='text-right p-2'), html.Td(row[1], className='text-left text-info p-2')])
        for row in ephys_table_rows
    ], className='small')
    ephys_table = dbc.Table([ephys_table_body], striped=True, bordered=True)

    # NOTE: popovers display some additional info about the session's experimenter, subject, and research study
    return html.Div([
        dbc.Row([
            dbc.Col(info_table, width=6),
            dbc.Col(ephys_table, width=6)
        ]),
        dbc.Row(dbc.Col(notes_grp, width=12)),
        _experimenter_popover(experimenter),
        _subject_popover(session['subj_id']),
        _study_popover(study)
    ])


def _experimenter_popover(experimenter: Dict[str, ti.AttributeValue]) -> dbc.Popover:
    """
    Helper method for _session_info_tabpane(). Prepares the Bootstrap Popover that lists some additional information
    on the researcher that conducted the experiment session displayed in that tab pane: Full name, title, organization,
    email address (as a 'mailto' link), total sessions uploaded to portal (and date of most recent upload)

    Args:
        experimenter: A row in the database User table containing information about the experimenter.
    Returns:
        A Bootstrap Popover component displaying the information described.
    """
    # get total # of sessions belonging to experimenter, and date of most recent upload (NOT session recording date)
    sessions_line = '#Sessions uploaded: N/A'
    try:
        restriction = dict(experimenter=experimenter['username'])
        sessions = fetch_restrict_proj([ti.DBTable.SESSION], restrict=[restriction], attributes=['committed'])
        num_sessions = sessions and len(sessions)
        if num_sessions > 0:
            sessions.sort(key=lambda x: x['committed'], reverse=True)
            last_commit = sessions[0]['committed'].strftime("%Y-%m-%d")
            sessions_line = f"#Sessions uploaded: {num_sessions} (last upload on: {last_commit})"
    except Exception as e:
        get_application_logger().error(
            f"Error while fetching sessions for experimenter {experimenter['username']}: {str(e)}", exc_info=True)

    title, org, email = experimenter['title'], experimenter['organization'], experimenter['contact_email']
    if title and (len(title) > 0):
        title_org = f"{title} - {org}" if (org and len(org) > 0) else f"{title}"
    elif org and (len(org) > 0):
        title_org = f"{org}"
    else:
        title_org = "Researcher - Lisberger lab"
    markdown = f"{title_org}  \nEmail inquiries: [{email}](mailto:{email})  \n\n_{sessions_line}_"
    return dbc.Popover([
        dbc.PopoverHeader(f"{experimenter['full_name']}"),
        dbc.PopoverBody(dcc.Markdown(markdown))
    ], target=_EXP_POPOVER_TGT, trigger='legacy')


def _subject_popover(subj_id: str) -> dbc.Popover:
    """
    Helper method for _session_info_tabpane(). Prepares the Bootstrap Popover that lists some additional information
    on the subject of the experiment session displayed in that tab pane: ID/nickname, species name, DOB, sex, and
    implant history (if applicable).

    Args:
        subj_id: The subject ID (primary key of Subject table in database).
    Returns:
        A Bootstrap Popover component displaying the information described.
    """
    ok, subject, implants = False, None, None
    try:
        restriction = dict(subj_id=subj_id)
        subject = fetch_one_row(ti.DBTable.SUBJECT, restriction)
        implants = fetch_rows(ti.DBTable.IMPLANT, restriction)
        ok = subject and isinstance(implants, list)
        if ok and len(implants) > 0:
            implants.sort(key=lambda x: x['implant_date'], reverse=True)
    except Exception as e:
        get_application_logger().error(f"Error while fetching info on subject {subj_id}: {str(e)}", exc_info=True)
    markdown = f"_ID/Nickname_: **{subj_id}**  \n"
    if ok:
        markdown += f"_Species_: {subject['species']}  \n_DOB_: {subject['dob']} (sex: {subject['sex']})  \n\n"
        if len(implants) == 0:
            markdown += "_No recorded implants_"
        else:
            markdown += "_Implant history:_  \n"
            for implant in implants:
                markdown += f"  * {implant['implant_date']} (AP={implant['st_ap']:.2f}mm, " \
                            f"ML={implant['st_ml']:.2f}mm, DV={implant['st_dv']:.2f}mm; " \
                            f"\u03b8(AP)={implant['ap_angle']:.1f}\u00b0, " \
                            f"\u03b8(ML)={implant['ml_angle']:.1f}\u00b0)  \n"
    else:
        markdown += "_No information available_"
    return dbc.Popover([
        dbc.PopoverHeader("Subject Details"),
        dbc.PopoverBody(dcc.Markdown(markdown))
    ], target=_SUBJ_POPOVER_TGT, trigger='legacy')


def _study_popover(study: Dict[str, ti.AttributeValue]) -> dbc.Popover:
    """
    Helper method for _session_info_tabpane(). Prepares the Bootstrap Popover that lists some additional information
    on the research project of which the currently displayed experiment session is a part: title, lead author, full
    description, total sessions (and date of most recent), and list of related publications.

    Args:
        study: A row in the database Study table containing information about the research project.
    Returns:
        A Bootstrap Popover component displaying the information described.
    """
    # retrieve user record of study's lead investigator, and list of related publications. Also get # of experiment
    # sessions uploaded for this study, and total number of neural units recorded.
    study_lead: Optional[Dict[str, ti.AttributeValue]] = None
    pubs: Optional[List[Dict[str, ti.AttributeValue]]] = None
    num_sessions: Optional[int] = None
    num_units: Optional[int] = None
    try:
        study_lead = fetch_one_row(ti.DBTable.USER, dict(username=study['study_lead']))
        study_to_pub_rows = fetch_rows(ti.DBTable.STUDY_TO_PUB, dict(study_id=study['study_id']))
        if isinstance(study_to_pub_rows, list) and len(study_to_pub_rows) > 0:
            restriction = list()
            for r in study_to_pub_rows:
                restriction.append(f"pub_id = {r['pub_id']}")
            pubs = fetch_any_proj(ti.DBTable.PUB, restriction, attributes=[])
        restriction = dict(study_id=study['study_id'])
        num_sessions = num_table_rows(ti.DBTable.SESSION, [restriction])
        units = fetch_restrict_proj([ti.DBTable.SESSION_NEURON, ti.DBTable.SESSION], [None, restriction], ['unit_id'])
        if isinstance(units, list):
            num_units = len(units)
    except Exception as e:
        get_application_logger().error(
            f"Error while fetching additional info on study {study['study_title']}: {str(e)}", exc_info=True)

    study_lead_contact = \
        f"{study_lead['full_name']}, [{study_lead['contact_email']}](mailto:{study_lead['contact_email']})" \
        if study_lead else "Not available"

    stats_line = f"_# of experiment sessions_: {'Not available' if (not num_sessions) else num_sessions}, " \
                 f"_# neural units_: {'Not available' if (not num_units) else num_units}"
    pubs_markdown = "_Related Publications_:  "
    if pubs is None:
        pubs_markdown += "Not available."
    elif len(pubs) == 0:
        pubs_markdown += "None."
    else:
        for p in pubs:
            pubs_markdown += f"\n  * {p['citation']}  " + (f"[[&#x21d7;]]({p['doi']})" if len(p['doi']) > 0 else "")

    return dbc.Popover([
        dbc.PopoverHeader("Project Details"),
        dbc.PopoverBody([
            dcc.Markdown(f"_Title_: {study['study_title']}  \n_Lead Investigator_: {study_lead_contact}  "
                         f"\n{stats_line}  \n  \n_Project Description_:"),
            dbc.Textarea(value=study['study_desc'], rows=6, cols=120, readonly=True, class_name='mb-3'),
            dcc.Markdown(pubs_markdown, dangerously_allow_html=True)
        ])
    ], target=_STUDY_POPOVER_TGT, trigger='legacy')


_UNIT_SELECT_ID: str = 'td_unit_select'
""" ID of Bootstrap Select component that selects the neural unit to display in the 'Trial Data' tab pane. """
_UNIT_STATS_OPEN_ID: str = 'td_unit_stats_open'
""" ID of button on 'Trial Data' panel that raises the unit stats modal window. """
_UNIT_STATS_MODAL_ID: str = 'td_unit_stats_modal'
""" ID of Bootstrap Modal component in 'Trial Data' panel in which a neural unit stats summary is displayed. """
_UNIT_STATS_BODY_ID: str = 'td_unit_stats_content'
""" ID of the Bootstrap ModalBody ('Trial Data' panel) in which a neural unit stats summary is embedded. """
_UNIT_STATS_CLOSE_ID: str = 'td_unit_stats_close'
""" ID of button that extinguishes the Bootstrap Modal window in which a neural unit stats summary is displayed. """

_PROTO_SELECT_ID: str = 'td_proto_select'
""" ID of dropdown that selects the trial protocol to view in the 'Trial Data' panel. """
_PROTO_VIEW_OPEN_ID: str = 'td_proto_view_open'
""" ID of button on 'Trial Data' panel that raises the protocol definition modal window. """
_PROTO_VIEW_MODAL_ID: str = 'td_proto_view_modal'
""" ID of Bootstrap Modal component in 'Trial Data' panel on which a protocol definition is displayed. """
_PROTO_VIEW_BODY_ID: str = 'td_proto_view_content'
""" ID of the Bootstrap ModalBody ('Trial Data' panel) in which the protocol definition is embedded. """
_PROTO_VIEW_CLOSE_ID: str = 'td_proto_view_close'
""" ID of button that extinguishes the Bootstrap Modal window in which a protocol is displayed. """

_DISP_SELECT_ID: str = 'td_display_select'
"""
ID of dropdown in 'Trial Data' panel that selects what to display (response to a single trial,
mean response across all trial reps, or discharge statistics view for the selected neural unit).
"""
_DISP_VIEW_ID: str = "td_display_view"
""" ID of HTML Div in the 'Trial Data' panel in which the selected response data is displayed. """
_DISP_VIEW_LOADING_ID: str = "td_display_view_loading"
""" 
ID of the Dash Loading component encapsulating the response graph(s) to display a loading indicator when it takes a
significant amount of time to prepare those graphs.
"""
_TD_HELP_BADGE_ID: str = 'td_help_badge'
""" ID of Bootstrap Badge to which a Popover is attached displaying help on the 'Trial Data' panel. """
_TD_HELP_POPOVER_ID: str = 'td_help_popover'
""" ID of Bootstrap Popover encapsulating instructions on how to use widgets on the 'Trial Data' panel. """


def _trial_data_tabpane(session: Dict[str, Any]) -> html.Div:
    """
    Helper method generates the HTML Div element that renders the content of the "Trial Data" tab in the detail panel.
    The tab content includes a "navigation control row" and a Plotly figure. The control row includes dropdowns for
    selecting a trial protocol, a recorded neural unit (or "None" for behavioral response data only), and the type of
    response displayed (single-trial response, mean response across all completed reps of the trial protocol, or the
    discharge statistics for the selected neuron accumulated across all completed reps of the trial protocol).

    Args:
        session: A dictionary corresponding to one row in the search results table. At a minimum, it must include the
            primary key-value pairs that uniquely identify an experiment session in the database. If it includes the
            ID of a neural unit recorded during the session (when search results table lists neurons instead of
            sessions), then that unit is displayed initially. Otherwise, the first recorded unit is displayed (unless
            no neurons were recorded during the session).
    Returns:
        An HTML Div that renders the contents of the "Trial Data" tab in the detail panel.
    """
    # how many units, N, were recorded in the session? The unit IDs are 1 to N.
    num_units = 0
    try:
        pk = {k: session[k] for k in ti.primary_key_of(ti.DBTable.SESSION, False)}
        num_units = num_table_rows(ti.DBTable.SESSION_NEURON, restriction=[pk])
    except Exception as e:
        get_application_logger().error(f"Error while fetching #units recorded during session: {str(e)}", exc_info=True)
        pass

    # ID of unit initiallly displayed: When search table row selected includes a unit ID, select that unit. Else, select
    # unit 1 -- unless the session is behavior only. An ID of 0 == "None" (no unit displayed)
    initial_unit_id = session['unit_id'] if ('unit_id' in session) else 1 if num_units > 0 else 0
    options = [{'label': f"{i + 1}", 'value': str(i+1)} for i in range(num_units)]
    options.insert(0, {'label': 'None', 'value': '0'})
    select_unit = dbc.Select(
        id=_UNIT_SELECT_ID,
        options=options,
        value=str(initial_unit_id),  # Bootstrap BUG: Select 'value' cannot be int
        disabled=(num_units <= 0)
    )
    unit_grp = dbc.InputGroup([
        dbc.InputGroupText("Unit"),
        select_unit
    ], size='sm')
    view_unit_btn = dbc.Button("View stats", id=_UNIT_STATS_OPEN_ID, size='sm')
    unit_stats_modal = dbc.Modal(
        [
            dbc.ModalBody(id=_UNIT_STATS_BODY_ID),
            dbc.ModalFooter(
                dbc.Row([
                    dbc.Button("Close", id=_UNIT_STATS_CLOSE_ID)
                ])
            )
        ],
        id=_UNIT_STATS_MODAL_ID, backdrop="static", size="xl", centered=True
    )

    # the trial protocol selection dropdown reflects all the different trial protocols presented during the session
    proto_options, init_proto = _proto_select_options_and_initial_value(session, unit_id=initial_unit_id)
    proto_grp = dbc.InputGroup([
        dbc.InputGroupText("Trial Protocol"),
        dbc.Select(id=_PROTO_SELECT_ID, options=proto_options, value=init_proto)
    ], size='sm', class_name='mr-1')
    view_proto_btn = dbc.Button("View details", id=_PROTO_VIEW_OPEN_ID, size='sm')
    proto_modal = dbc.Modal(
        [
            dbc.ModalBody(id=_PROTO_VIEW_BODY_ID),
            dbc.ModalFooter(
                dbc.Row([
                    dbc.Button("Close", id=_PROTO_VIEW_CLOSE_ID)
                ])
            )
        ],
        id=_PROTO_VIEW_MODAL_ID, backdrop="static", size="xl", centered=True
    )

    # the display select dropdown selects among individual trial reps or one of two possible aggregate responses
    disp_options, init_disp = _disp_select_options_and_initial_value(session, init_proto, initial_unit_id)
    disp_grp = dbc.InputGroup([
        dbc.InputGroupText("Display"),
        dbc.Select(id=_DISP_SELECT_ID, options=disp_options, value=init_disp)
    ], size='sm')

    figure_view = html.Div(id=_DISP_VIEW_ID,
                           children=_generate_trial_data_view(session, initial_unit_id, init_proto, init_disp))

    # a tooltip is presented in a Bootstrap Popover element attached to a pill badge on the navigation row.
    help_badge = dbc.Badge("?", pill=True, id=_TD_HELP_BADGE_ID, class_name='float-end', color='info',
                           style={'font-size': 18})
    markdown = dcc.Markdown(
        '''Select a trial protocol, then select an individual trial response or an aggregate response statistic. 
        *Aggregate response data is available only for those trial protocols for which 3 or more **successfully 
        completed** trial reps were recorded. In addition, the trial protocol can have no random variables, or a 
        single random-duration segment (highlighted in red in the figures for "Mean firing rate" and "Discharge 
        statistics").*  \n  \nClick the "Download" button if you wish to download response data for this session 
        (you must be logged into the portal with download access).'''
    )
    help_popover = dbc.Popover(
        [dbc.PopoverBody(markdown)],
        id=_TD_HELP_POPOVER_ID, target=_TD_HELP_BADGE_ID, trigger='hover', placement='top-end')

    # the modal component for requesting downloads and the invoking button are handled in a submodule
    download_modal, download_btn = render_download_modal_and_button(session)

    nav_row = dbc.Row([
        dbc.Col(dbc.Row([
            dbc.Col([help_badge, help_popover], width='auto', class_name='me-4'),
            dbc.Col(unit_grp, width='auto', class_name='me-1'),
            dbc.Col([view_unit_btn, unit_stats_modal], width='auto', class_name='me-5'),
            dbc.Col(proto_grp, width='auto', class_name='me-1'),
            dbc.Col([view_proto_btn, proto_modal], width='auto', class_name='me-5'),
            dbc.Col(disp_grp, width='auto')
        ], class_name='g-0'), width=10),
        dbc.Col([download_btn, download_modal], width='auto')
    ], align='center', justify='between', class_name='mb-2')

    loading_figure = dcc.Loading(id=_DISP_VIEW_LOADING_ID, children=figure_view, type='circle')
    return html.Div([nav_row, loading_figure])


def _proto_select_options_and_initial_value(session: Dict[str, Any], unit_id: Optional[int] = None,
                                            old_proto: Optional[str] = None) -> Tuple[List[Dict], Optional[str]]:
    """
    Helper method prepares the set of options and the initial value for the dropdown that selects the trial protocol
    in the "Trial Data" tab pane. If a neural unit is identified, the options list will include all trial protocols
    presented during the specified experiment session in which the specified neural unit was being recorded; otherwise,
    it includes any trial protocol presented at least once during the session. The initially selected value is the
    first value in the options list, or the previously selected value of the dropdown -- if that value is still in the
    options list.

    Args:
        session: Dictionary contains the primary key of the relevant experiment session.
        unit_id: The ID of the relevant neural unit. None or 0 => no unit selected; behavioral data only.
        old_proto: The MD5 hash digest identifying the protocol last selected in the dropdown.
    Returns:
        The options list and initial value for the Bootstrap Select widget by which user chooses a trial protocol.
    """
    if (unit_id is None) or (unit_id <= 0):
        proto_map = trial_protocols_for_session(session)
    else:
        unit_pk = session.copy()
        unit_pk['unit_id'] = unit_id
        proto_map = trial_protocols_for_neuron(unit_pk)
    none_found = (proto_map is None) or (len(proto_map) == 0)
    proto_options = [] if none_found else [{'label': v, 'value': k} for k, v in proto_map.items()]
    init_proto = None if none_found else (old_proto if (old_proto in proto_map.keys()) else proto_options[0]['value'])
    return proto_options, init_proto


# noinspection PyTypeChecker
def _disp_select_options_and_initial_value(session: Dict[str, Any], proto_hash: str, unit_id: int) -> \
        Tuple[List[Dict], Optional[str]]:
    """
    Helper method prepares the set of options and the initial value for the dropdown that selects what response data
    set to display in the "Trial Data" tab pane. If a neural unit is identified, the options list will include all trial
    reps of the specified protocol presented during the specified experiment session in which the specified neural unit
    was recorded. Otherwise, all reps of the protocol are included in the list. If no trial reps are found (this is
    possible because a given unit is not necessarily recorded over the entire duration of an experiment session), then
    the options list is empty -- there is no data to display. Otherwise, two additional options are included - "Mean
    response" and "Discharge Statistics". These are enabled only if the protocol allows aggregating response data and at
    least 3 trial reps are available for averaging response data; the latter is enabled only if aggregation is possible
    and a valid neural unit was specified.

    Args:
        session: Dictionary contains the primary key of the relevant experiment session.
        proto_hash: The MD5 digest identifying the relevant trial protocol. If None, then there's no data to display,
            and the display options list will be empty.
        unit_id: The ID of the relevant neural unit. None or 0 => no unit selected; behavioral data only.
    Returns:
        The options list and initial value for the Bootstrap Select widget by which user chooses the response data to
            display.
    """
    if proto_hash is None:
        return [], None
    # retrieve indices of all trial reps in the session for the chosen trial protocol, or all trial reps in which the
    # currently selected neuron responded.
    if (unit_id is None) or (unit_id <= 0):
        trial_indices = trials_for_session(session, proto_hash)
        proto_map = trial_protocols_for_session(session, aggregate=True)
    else:
        unit_pk = session.copy()
        unit_pk['unit_id'] = unit_id
        trial_indices = trials_for_neuron(unit_pk, proto_hash)
        proto_map = trial_protocols_for_neuron(unit_pk, aggregate=True)
    none_found = (trial_indices is None) or (len(trial_indices) == 0)

    options = [] if none_found else [{'label': f"Trial {k}", 'value': str(k)} for k in trial_indices]
    can_aggregate = (not none_found) and (proto_hash in proto_map.keys())
    if not none_found:
        options.append({'label': "Mean response", 'value': 'mean', 'disabled': not can_aggregate})
        options.append({'label': "Discharge statistics", 'value': 'ds',
                        'disabled': not (can_aggregate and (unit_id > 0))})
    sel_value = 'mean' if can_aggregate else \
        (str(trial_indices[0]) if trial_indices and (len(trial_indices) > 0) else None)
    return options, sel_value


def _unit_summary(unit_pk: Dict[str, Any]) -> html.Div:
    """
    Helper method retrieves summary information on a specific neuron recorded during an experiment session and renders
    that information in an HTML Div container. A tabular listing of neural unit information appears on the right, and a
    graph of the unit's 10-ms template waveform is on the left.

    Args:
        unit_pk: The primary key of the neural unit.
    Returns:
        An HTML Div that renders a table of information about the neural unit, alongside its template waveform.
    """
    # retrieve the sampling rate for the neural recording, the full unit record, and the neuron type assigned to unit.
    try:
        unit = fetch_one_row(ti.DBTable.SESSION_NEURON, unit_pk)
        sampling_rate = fetch_attribute_values(ti.DBTable.SESSION_EPHYS, 'sampling_rate', unit_pk)[0]
        neuron_type = fetch_attribute_values(ti.DBTable.NEURON_TYPE, 'nt_name', dict(nt_id=unit['unit_type']))[0]
    except Exception as e:
        get_application_logger().error(f"Error while fetching info for unit summary: {str(e)}", exc_info=True)
        return html.Div(dbc.Alert("Failed to retrieve information on selected neuron from the database", is_open=True))

    unit_template: np.ndarray = unit['unit_template']
    peak_to_peak = max(unit_template) - min(unit_template)

    table_rows = [
        ["Unit #", f"{unit['unit_id']}"],
        ["Neuron Type", f"{neuron_type}"],
        ["Date Recorded", f"{unit['session_date']}"],
        ["Omniplex Ch", f"{unit['unit_channel']}"],
        ["Firing Rate", f"{unit['unit_rate']:.1f} Hz"],
        ["Total #Spikes", f"{unit['unit_spikes']}"],
        ["Signal-to-Noise:", f"{unit['unit_snr']:.2f}"],
        ["Peak-to-Peak", f"{peak_to_peak:.1f} \u00B5V"]
    ]
    table_body = html.Tbody([
        html.Tr([html.Td(row[0], className='text-right'), html.Td(row[1], className='text-left text-info')])
        for row in table_rows
    ], className='small')
    info_table = dbc.Table([table_body], striped=True, bordered=True)

    # simple graph of template waveform.
    to_msecs = 1000.0 / sampling_rate
    graph = dcc.Graph(figure=px.line(x=[i * to_msecs for i in range(len(unit_template))], y=unit_template,
                                     labels={'x': 'time (ms)', 'y': '\u00B5V'},
                                     title='Average spike waveform (1-ms pre, 9-ms post)'))

    return html.Div([
        dbc.Row([
            dbc.Col(info_table, width=3),
            dbc.Col(graph)
        ], align='center')
    ])


def _generate_trial_data_view(
        session: Dict[str, Any], unit_id: int, proto_hash: str, disp_sel: str) -> Union[html.Div, dcc.Graph]:
    """
    Helper method prepares a figure displaying trial data recorded during a specified experiment session, with the
    specified trial protocol, and optionally including the response of the specified neural unit.

    Args:
        session: Dictionary contains the primary key of the relevant experiment session.
        unit_id: The ID of the relevant neural unit. None or 0 => no unit selected; behavioral data only.
        proto_hash: The MD5 digest identifying the relevant trial protocol. If None, then there's no data to display.
        disp_sel: This will be an integer string identifying the index os a single trial rep, or one of two possible
            aggregate response plots - "mean" for mean response across all reps of the protocol, or "ds" for the
            discharge statistics for the specified unit computed across all reps of the protocol.
    Returns:
        A Plotly figure displaying the response data specified by the arguments. If the arguments are invalid or the
            response data is unavailabe, the method returns an HTML Div with an error message.
    """
    if (session is None) or (proto_hash is None):
        out = None
    elif disp_sel == 'mean':
        out = average_response_figure(session, proto_hash, unit_id if (unit_id and (unit_id > 0)) else None)
    elif disp_sel == 'ds':
        if not (unit_id and (unit_id > 0)):
            out = html.Div(dbc.Alert(f"Select a neural unit to compute its discharge statistics.", is_open=True),
                           className='mt-5 mb-5')
        else:
            unit_key = session.copy()
            unit_key['unit_id'] = unit_id
            out = _discharge_statistics_panel(unit_key, proto_hash)
    else:
        out = None
        try:
            trial_idx = int(disp_sel) if isinstance(disp_sel, str) else -1
            if trial_idx >= 0:
                out = single_trial_response_figure(session, trial_idx, unit_id if (unit_id and (unit_id > 0)) else None)
        except Exception:
            pass
    if out is None:
        out = html.Div("No data available.", className='mt-5 mb-5')
    return out


_DS_RANGE_ID: str = 'td_ds_range_slider'
""" ID of widget selecting the trial interval over which the discharge statistics are computed. """
_DS_GRAPH_ID: str = 'td_ds_graph'
""" ID of dcc.Graph in which discharge statistics are plotted. """


def _discharge_statistics_panel(unit_key: Dict[str, Any], proto_hash: str) -> html.Div:
    """
    Helper method prepares a two-figure plot displaying the specified neuron's discharge statistics (autocorrelogram and
    inter-spike interval histogram) computed across all SUCCESSFULLY COMPLETED reps of the specified trial protocol. The
    top figure is a simple representation of the trial protocol showing only the position trajectory of the target
    designated as "Fixation Target #1". A series of alternating blue and gray bars along the top of this plot indicate
    the spans of the trial segments.

    Below this is a range slider that lets the user select the contiguous interval of trial segments over which the
    statistics are computed; initially, the slider covers all segments in the trial protocol. We select the interval in
    terms of segments rather than trial time because the trial protocol may include a random-duration segment.

    The bottom figure has two side-by-side plots: the ACG and the ISI histogram. Note that the discharge statistics are
    only generated if: (1) there are at least 3 successfully completed reps of the given protocol during the experiment
    session; (2) the protocol definition is conducive to averaging (no random variables, or a single random-duration
    segment -- not necessarily the first one).

    Args:
        unit_key: Dictionary containing the primary key-value pairs that uniquely identify a neuron in the database.
        proto_hash: The MD5 hash digest that uniquely identifies the trial protocol in the lab database.
    Returns:
        An HTML Div displaying the specified neuron's discharge statistics as described. If an error occurs while
            retrieving response data, the method instead returns an HTML Div with an error message.
    """
    trial_data = retrieve_trial_reps_for_neuron(unit_key, proto_hash)
    if trial_data is None:
        return html.Div(dbc.Alert(f"Failed to retrieve trial data for neuron (internal error).", is_open=True))
    elif len(trial_data) < 3:
        return html.Div(dbc.Alert(f"Fewer than 3 successful trial reps ({len(trial_data)} found for selected protocol",
                                  is_open=True))
    elif not trial_data[0].protocol.can_aggregate_responses():
        return html.Div(dbc.Alert("Selected protocol is not conducive to averaging across trial reps", is_open=True))

    # figure displaying fixation target position trajectories and ON epochs represents the trial rep with the minimum
    # observed duration for the random-duration segment (if the protocol has a random-duration segment)
    protocol = trial_data[0].protocol
    min_dur = int(min([td.trial_rvs[0] for td in trial_data])) if len(protocol.rvs) > 0 else 0
    proto_plot = trial_target_trajectory_figure(protocol, min_dur)

    # range slider selects the contiguous interval of trial segments over which the ACG and ISI are computed.
    # Initially set to cover the entire trial.
    num_segs = len(protocol.trial.segments)
    range_min = 0
    range_max = num_segs   # last index == trial's end
    range_marks = {i: {"label": f"Seg{i}"} for i in range(num_segs)}
    range_marks[num_segs] = {"label": "END"}

    slider = dcc.RangeSlider(
        id=_DS_RANGE_ID,
        min=range_min, max=range_max, step=1,
        value=[range_min, range_max],
        marks=range_marks, allowCross=False,
        pushable=1,  # range must include one segment at least
        tooltip={'always_visible': False, 'placement': 'bottom'}
    )
    instruction = dcc.Markdown('''**Select trial segments over which discharge statistics are computed:**''')
    slider_row = html.Div([instruction, slider])

    # note: the dcc.Graph displaying discharge statistics is populated by a chained callback
    return html.Div([
        dcc.Graph(figure=proto_plot, config=dict(staticPlot=True)),
        slider_row,
        dcc.Graph(id=_DS_GRAPH_ID, config=dict(staticPlot=True))
    ])


# When a row is selected (or deselected) in the search results table, we store the dictionary defining that row in a
# Dash Store component on the page. This is because many callbacks need the identity of the currently selected session,
# and we don't want to pass the state of 'data' and 'selected_rows' attributes of the DataTable for all of those
# callbacks. The 'data' attribute could be rather large!
@callback(
    Output(_SELECTED_ROW_ID, "value"), [Input(_SEARCH_TABLE_ID, "selected_rows")], [State(_SEARCH_TABLE_ID, "data")]
)
def on_search_row_selected(selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    return json.dumps(selected_row)


@callback(
    [Output(_COLLAPSE_ID, "is_open"), Output(_SESSION_TAB_ID, "children"), Output(_DATA_TAB_ID, "children")],
    [Input(_SELECTED_ROW_ID, "value")]
)
def show_hide_detail_pane(json_str):
    selected_row = json_str and json.loads(json_str)
    summary_tab = _session_info_tabpane(selected_row) if selected_row else html.Div("Not available.")
    data_tab = _trial_data_tabpane(selected_row) if selected_row else html.Div("Not available.")
    return selected_row is not None, summary_tab, data_tab


@callback(
    [Output(_UNIT_STATS_MODAL_ID, "is_open"), Output(_UNIT_STATS_BODY_ID, "children")],
    [Input(_UNIT_STATS_OPEN_ID, "n_clicks"), Input(_UNIT_STATS_CLOSE_ID, "n_clicks")],
    [State(_UNIT_SELECT_ID, "value"), State(_SELECTED_ROW_ID, "value")]
)
def on_show_hide_unit_stats(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if trigger_id == _UNIT_STATS_CLOSE_ID:
        return False, no_update
    elif trigger_id == _UNIT_STATS_OPEN_ID:
        selected_row = args[-1] and json.loads(args[-1])
        unit_id = int(args[-2]) if isinstance(args[-2], str) else None
        if selected_row and unit_id:
            selected_row['unit_id'] = unit_id
            summary_div = _unit_summary(selected_row)
            if summary_div:
                return True, summary_div
    return False, no_update


@callback(
    [Output(_PROTO_VIEW_MODAL_ID, "is_open"), Output(_PROTO_VIEW_BODY_ID, "children")],
    [Input(_PROTO_VIEW_OPEN_ID, "n_clicks"), Input(_PROTO_VIEW_CLOSE_ID, "n_clicks")],
    [State(_PROTO_SELECT_ID, "value")]
)
def on_show_hide_protocol_definition(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if trigger_id == _PROTO_VIEW_CLOSE_ID:
        return False, no_update
    elif trigger_id == _PROTO_VIEW_OPEN_ID:
        protocol = get_trial_protocol_definition(args[2])
        if protocol:
            return True, display_trial_protocol_definition(protocol)
    return False, no_update


@callback(
    [Output(_PROTO_SELECT_ID, "options"), Output(_PROTO_SELECT_ID, "value"),
     Output(_DISP_SELECT_ID, "options"), Output(_DISP_SELECT_ID, "value"), Output(_UNIT_STATS_OPEN_ID, "disabled")],
    [Input(_UNIT_SELECT_ID, "value"), Input(_PROTO_SELECT_ID, "value")],
    [State(_UNIT_SELECT_ID, "value"), State(_PROTO_SELECT_ID, "value"), State(_SELECTED_ROW_ID, "value")],
    prevent_initial_call=True
)
def on_update_selected_unit_or_proto(*args):
    selected_row = args[-1] and json.loads(args[-1])
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if (selected_row is None) or (trigger_id == ""):
        raise dash_exc.PreventUpdate

    proto_opts, proto_sel, disp_opts, disp_sel, stats_disabled = no_update, no_update, no_update, no_update, no_update
    if trigger_id == _UNIT_SELECT_ID:
        unit_id = 0 if (args[0] is None) else int(args[0])
        stats_disabled = (unit_id == 0)
        proto_opts, proto_sel = \
            _proto_select_options_and_initial_value(selected_row, unit_id, args[3])
        disp_opts, disp_sel = \
            _disp_select_options_and_initial_value(selected_row, proto_sel, unit_id)
    elif trigger_id == _PROTO_SELECT_ID:
        unit_id = 0 if (args[2] is None) else int(args[2])
        stats_disabled = (unit_id == 0)
        disp_opts, disp_sel = \
            _disp_select_options_and_initial_value(selected_row, args[1], unit_id)
    return proto_opts, proto_sel, disp_opts, disp_sel, stats_disabled


@callback(
    Output(_DISP_VIEW_ID, "children"),
    [Input(_DISP_SELECT_ID, "value")],
    [State(_UNIT_SELECT_ID, "value"), State(_PROTO_SELECT_ID, "value"), State(_SELECTED_ROW_ID, "value")],
    prevent_initial_call=True
)
def on_update_trial_data_view(disp_sel, unit_sel, proto_sel, json_str):
    selected_row = json_str and json.loads(json_str)
    if (selected_row is None) or not callback_context.triggered:
        raise dash_exc.PreventUpdate
    return _generate_trial_data_view(selected_row, 0 if (unit_sel is None) else int(unit_sel), proto_sel, disp_sel)


@callback(
    Output(_DS_GRAPH_ID, "figure"), [Input(_DS_RANGE_ID, "value")],
    [State(_SELECTED_ROW_ID, "value"), State(_UNIT_SELECT_ID, "value"), State(_PROTO_SELECT_ID, "value")])
def on_update_discharge_stats_figure(range_value, json_str, unit_sel, proto_hash_value):
    selected_row = json_str and json.loads(json_str)
    unit_id = int(unit_sel) if isinstance(unit_sel, str) else 0
    return discharge_statistics_figure(selected_row, proto_hash_value, unit_id, range_value)
