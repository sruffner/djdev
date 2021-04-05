"""
explore.py: The "explore database" page in the Lisberger lab's database portal app (/explore endpoint).

TODO: UNDER DEVELOPMENT - For now I'm using this to develop a panel for selecting any neural unit recording in the
database and examine that unit's response data. Eventually, this will be a generic starting point for searching
database content.

@author: sruffner
@created: 22mar2021
"""
import sys
from datetime import date
from typing import List, Dict, Any, Optional

import dash_html_components as html
import dash_bootstrap_components as dbc
import dash_core_components as dcc
import dash_table as dt
import dash
from dash.dependencies import Input, Output, State
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from app import app
import database.table_info as ti
from common import check_date
from database.manager import DataBaseManager, TrialData
from database.table_info import DBTable
import database.maestro as maestro


_NEURON_TABLE_ID: str = "neuron_list"
""" The ID assigned to the Dash DataTable presenting the list of neurons in the database. """

_NEURON_TABLE_ATTRS: List[str] = [
    'experimenter', 'subj_id', 'session_date', 'session_sfx', 'unit_id', 'unit_type', 'unit_rate'
]
""" DBTable.SESSION_NEURON attributes that are fetched from database for display in neuron table. """

_NEURON_TABLE_COLS: List[ti.Column] = [
    ti.Column('full_name', 'Experimenter', '150px', False),
    ti.Column('subj_id', 'Subject', '100px', False),
    ti.Column('session', 'Session', '100px', False),
    ti.Column('unit_id', 'Unit #', '50px', False),
    ti.Column('nt_name', 'Neuron Type', '100px', False),
    ti.Column('unit_rate', 'Rate (Hz)', '100px', False)
]
""" Defined columns for the neuron table."""

_NEURON_TABLE_DIV_ID: str = "nt_div"
""" ID assigned to the Dash Bootstrap CardBody in which the table of neurons is embedded. """


def _fetch_neurons(restriction: Optional[List[str]]) -> List[Dict[str, ti.AttributeValue]]:
    rows = DataBaseManager.fetch_proj(DBTable.SESSION_NEURON, _NEURON_TABLE_ATTRS, restriction)
    # prepare values in "composed" columns
    neuron_type_map = {r['nt_id']: r['nt_name'] for r in DataBaseManager.fetch_rows(DBTable.NEURON_TYPE)}
    user_map = {r['username']: r['full_name'] for r in DataBaseManager.fetch_rows(DBTable.USER)}
    for row in rows:
        row['nt_name'] = neuron_type_map[row['unit_type']]
        row['full_name'] = user_map[row['experimenter']]
        row['session'] = f"{row['session_date']} ({row['session_sfx']})"
    return rows


def _table_of_neurons() -> dt.DataTable:
    rows = _fetch_neurons(None)
    data_table = dt.DataTable(
        id=_NEURON_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _NEURON_TABLE_COLS],
        data=rows,
        row_selectable='single',
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _NEURON_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '200px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )
    return data_table


_FILTER_UNUSED: str = "<none>"
""" Pseudo-value in any filter select widget indicating that filter is unused. """
_FILTER_RAISE_ID: str = "filter_raise"
_FILTER_POPOVER_ID: str = "filter_popover"
_FILTER_EXP_ID: str = "filter_exp"
_FILTER_SUBJ_ID: str = "filter_subj"
_FILTER_TYPE_ID: str = "filter_type"
_FILTER_DATE_ID: str = "filter_date"
_DATE_PICKER_ID: str = "filter_date_picker"
_FILTER_CLEAR_ID: str = "filter_clear"
_FILTER_COUNT_ID: str = "filter_count"


def _filter_group() -> dbc.Row:
    """
    Helper method prepares the filter control group - a collection of widgets in a Bootstrap Popover element that
    control filtering of the neural units displayed in the main table on this panel. The Popover is raised when the
    mouse hovers over the "Filter Results" button. The control group lets the user filter the results by experimenter,
    neuron type, subject, and date recorded.
    Returns:
        A Bootstrap Row container holding the "Filter Results" button and filter widgets embedded in a Popover.
    """
    experimenters = list(DataBaseManager.fetch_proj(DBTable.USER, ['username', 'full_name']))
    experimenters.sort(key=lambda x: x['full_name'])
    experimenters.insert(0, {'username': _FILTER_UNUSED, 'full_name': _FILTER_UNUSED})
    subjects = DataBaseManager.fetch_attribute_values(DBTable.SUBJECT, "subj_id")
    subjects.sort()
    subjects.insert(0, _FILTER_UNUSED)
    neuron_types = list(DataBaseManager.fetch_rows(DBTable.NEURON_TYPE))
    neuron_types.sort(key=lambda x: x['nt_name'])
    neuron_types.insert(0, {'nt_id': _FILTER_UNUSED, 'nt_name': _FILTER_UNUSED})
    date_choices = [_FILTER_UNUSED, 'on', 'before', 'after']

    num_neurons = DataBaseManager.num_table_rows(DBTable.SESSION_NEURON)

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
    neuron_type_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupAddon("Neuron Type =", addon_type="prepend"),
        dbc.Select(id=_FILTER_TYPE_ID,
                   options=[{"label": opt['nt_name'], "value": str(opt['nt_id'])} for opt in neuron_types],
                   value=_FILTER_UNUSED)
    ], size='sm'), className='mr-1 ml-1 mb-2')
    date_row = dbc.Row(dbc.InputGroup([
        dbc.InputGroupAddon("Recorded: ", addon_type="prepend"),
        dbc.Select(id=_FILTER_DATE_ID,
                   options=[{"label": opt, "value": opt} for opt in date_choices], value=_FILTER_UNUSED),
        dcc.DatePickerSingle(id=_DATE_PICKER_ID, date=date.today(), display_format='YYYY-MM-DD', className='ml-2')
    ], size='sm'), className='mr-1 ml-1 mb-3')
    control_row = dbc.Row([
        dbc.Button("Clear", id=_FILTER_CLEAR_ID, className="mr-3", size='sm', color='primary'),
        dbc.Label(f"{num_neurons} units found", id=_FILTER_COUNT_ID, className='my-auto')
    ], className='mr-1 ml-1')

    return dbc.Row([
        dbc.Col(dbc.Button("Filters", id=_FILTER_RAISE_ID, size='sm', color='primary')),
        dbc.Popover(
            [
                dbc.PopoverBody([neuron_type_row, subject_row, experimenter_row, date_row, control_row]),
            ],
            id=_FILTER_POPOVER_ID,
            target=_FILTER_RAISE_ID,
            trigger="hover", placement='right-start'
        )
    ], className="mb-3")


def _unit_summary(row_selected: Dict[str, Any]) -> html.Div:
    # retrieve the sampling rate for the neural recording, then retrieve the full unit record
    try:
        primary_key = {k: row_selected[k] for k in ti.primary_key_of(DBTable.SESSION_NEURON, False)}
        sampling_rate = DataBaseManager.fetch_attribute_values(DBTable.SESSION_EPHYS, 'sampling_rate', primary_key)[0]
        unit = DataBaseManager.fetch_rows(DBTable.SESSION_NEURON, primary_key)[0]
    except Exception:
        return html.Div(dbc.Alert("Failed to retrieve information on selected neuron from the database", is_open=True))

    unit_template: np.ndarray = unit['unit_template']
    peak_to_peak = max(unit_template) - min(unit_template)

    table_rows = [
        ["Unit #", f"{unit['unit_id']}"],
        ["Neuron Type", f"{row_selected['nt_name']}"],
        ["Date Recorded", f"{unit['session_date']}"],
        ["Subject ID", f"{unit['subj_id']}"],
        ["Experimenter", f"{row_selected['full_name']}"],
        ["Omniplex Ch", f"{unit['unit_channel']}"],
        ["Firing Rate", f"{unit['unit_rate']:.1f} Hz"],
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
        ])
    ])


_RESP_PROTO_SELECT_ID: str = 'resp_proto_select'
""" ID of dropdown that selects the trial protocol to view in the neuron's trial responses pane. """
_RESP_TRIAL_SELECT_ID: str = 'resp_trial_select'
""" ID of dropdown that selects the particular trial to view among reps of the selected trial protocol. """
_PROTO_VIEW_OPEN_ID: str = 'proto_view_open'
""" ID of button that raises a modal window in which the definition of the selected trial protocol is displayed. """
_PROTO_VIEW_MODAL_ID: str = 'proto_view_modal'
""" ID of Bootstrap Modal component in which a protocol definition is displayed. """
_PROTO_VIEW_BODY_ID: str = 'proto_view_content'
""" ID of the Bootstrap ModalBody in which the protocol definition is embedded. """
_PROTO_VIEW_CLOSE_ID: str = 'proto_view_close'
""" ID of button that extinguishes the Bootstrap Modal window in which a protocol definition is displayed. """
_TRIAL_VIEW_ID: str = "trial_view"
""" ID of HTML Div in which the response data for the selected trial is displayed. """


def _trial_responses_panel(row_selected: Dict[str, Any]) -> html.Div:
    try:
        proto_map = DataBaseManager.trial_protocols_for_neuron(row_selected)
        first_proto_key = next(iter(proto_map.keys()))
        select_protocol = dbc.Select(
            id=_RESP_PROTO_SELECT_ID,
            options=[{'label': v, 'value': k} for k, v in proto_map.items()],
            value=first_proto_key,
            className="mb-2"
        )
        view_btn = dbc.Button("View details", id=_PROTO_VIEW_OPEN_ID, color='primary')
        proto_modal = dbc.Modal(
            [
                dbc.ModalBody(id=_PROTO_VIEW_BODY_ID),
                dbc.ModalFooter(
                    dbc.Row([
                        dbc.Button("Close", id=_PROTO_VIEW_CLOSE_ID, color="primary")
                    ])
                )
            ],
            id=_PROTO_VIEW_MODAL_ID, backdrop="static", size="xl", centered=True
        )

        # These get populated via chained callbacks.
        select_trial = dbc.Select(id=_RESP_TRIAL_SELECT_ID)
        trial_view = html.Div(id=_TRIAL_VIEW_ID, children=[])

        nav_row = dbc.Row([
            dbc.Col(select_protocol, width=6),
            dbc.Col([view_btn, proto_modal]),
            dbc.Col(select_trial, width=3, className='mr-1')
        ])
        return html.Div([nav_row, trial_view])
    except Exception as err:
        return html.Div(dbc.Alert(f"Failed to retrieve response data: {str(err)}", is_open=True))


_BEHAVIOR_TRACE_STYLE_MAP = {
    'HEPOS': dict(color='royalblue'),
    'HEVEL': dict(color='royalblue', dash='dot'),
    'HDVEL': dict(color='deepskyblue', dash='dot'),
    'VEPOS': dict(color='firebrick'),
    'VEVEL': dict(color='firebrick', dash='dot'),
    'FIX1_HPOS': dict(color='forestgreen'),
    'FIX1_VPOS': dict(color='gold'),
    'FIX2_HPOS': dict(color='orange'),
    'FIX2_VPOS': dict(color='orchid')
}


def _trial_figure(trial_data: TrialData) -> dcc.Graph:
    fig = go.Figure()
    for response_id, trace in trial_data.behavior.items():
        fig.add_trace(
            go.Scatter(x=[i for i in range(len(trace))], y=trace, name=response_id, mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP[response_id],
                       yaxis='y2' if response_id.find('VEL') > -1 else None)
        )
    fix1_pos, fix2_pos = trial_data.protocol.compute_fixation_target_trajectories(trial_data.trial_rvs)
    if fix1_pos is not None:
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix1_pos.shape[0])], y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False)
        )
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix1_pos.shape[0])], y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False)
        )
    if fix2_pos is not None:
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix2_pos.shape[0])], y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False)
        )
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix2_pos.shape[0])], y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False)
        )
    for unit_id, trace in trial_data.neuronal.items():
        x_spikes = list()
        y_spikes = list()
        for t in trace:
            x_spikes.extend([t*1000, t*1000, None])
            y_spikes.extend([0.5, 2.5, None])
        fig.add_trace(
            go.Scatter(x=x_spikes, y=y_spikes, name=f"Unit #{unit_id}", mode='lines', connectgaps=False,
                       line=dict(color='black', width=2),  yaxis='y3')
        )
    fig.update_layout(
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(domain=[0, 0.95], title='time (milliseconds)'),
        yaxis=dict(title='position (degrees)'),
        yaxis2=dict(title='velocity (degrees/second)', anchor="x", overlaying="y", side="right"),
        yaxis3=dict(range=[0, 10], anchor="x", overlaying="y", visible=False)
    )

    if (len(trial_data.protocol.trial.segments) > 1) and (len(trial_data.trial_rvs) == 1) and \
            (trial_data.protocol.diffs[0].type == maestro.SegParamType.DURATION) and \
            (trial_data.protocol.diffs[0].seg_idx == 0):
        fig.add_vrect(x0=0, x1=trial_data.trial_rvs[0], fillcolor="red", opacity=0.2)

    return dcc.Graph(figure=fig)


_SUMMARY_TAB_ID: str = "unit_summary_tab"
""" ID of tab panel in which the selected neuron's summary information is displayed. """
_RESPONSE_TAB_ID: str = "unit_response_tab"
""" ID of tab panel in which the selected neuron's response data is displayed. """
_COLLAPSE_ID: str = "unit_collapse_id"
""" ID of Dash Bootstrap Collapse element wrapping the tabbed panel displaying details for a selected neuron. The
element is hidden when no neuron is selected. """

_detail_panel = dbc.Tabs(
    [
        dbc.Tab(dbc.Card(dbc.CardBody(children=[], id=_SUMMARY_TAB_ID), className='mt-2'), label="Summary"),
        dbc.Tab(dbc.Card(dbc.CardBody(children=[], id=_RESPONSE_TAB_ID), className='mt-2'), label="Trial Responses")
    ]
)
""" Rendering of a tabbed panel in which a selected neuron's summary and response data are displayed. """

layout = html.Div([
    dbc.Container([
        dbc.Row(dbc.Col(html.H3("Explore neuron recordings in the laboratory database", className="text-center")),
                className="mb-3 mt-3"),
        dbc.Row(dbc.Col(html.Div(id=_NEURON_TABLE_DIV_ID, children=_table_of_neurons())), className="mb-3"),
        _filter_group(),
        dbc.Row(dbc.Col(dbc.Collapse(_detail_panel, id=_COLLAPSE_ID)))
    ])
])


@app.callback([Output(_COLLAPSE_ID, "is_open"), Output(_SUMMARY_TAB_ID, "children"),
               Output(_RESPONSE_TAB_ID, "children")],
              [Input(_NEURON_TABLE_ID, "selected_rows")], [State(_NEURON_TABLE_ID, "data")])
def show_hide_detail_pane(selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    summary_tab = _unit_summary(selected_row) if selected_row else html.Div("Not available.")
    response_tab = _trial_responses_panel(selected_row)
    return selected_row is not None, summary_tab, response_tab


@app.callback([Output(_RESP_TRIAL_SELECT_ID, "options"), Output(_RESP_TRIAL_SELECT_ID, "value")],
              [Input(_RESP_PROTO_SELECT_ID, "value")],
              [State(_NEURON_TABLE_ID, "selected_rows"), State(_NEURON_TABLE_ID, "data")])
def on_proto_select(proto_hash_value, selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    if (proto_hash_value is None) or (selected_rows is None):
        raise dash.exceptions.PreventUpdate
    trial_indices = DataBaseManager.trials_for_neuron(selected_row, proto_hash_value)
    session_pk = {'experimenter': selected_row['experimenter'], 'subj_id': selected_row['subj_id'],
                  'session_date': selected_row['session_date'], 'session_sfx': selected_row['session_sfx']}
    total_trials = DataBaseManager.num_table_rows(DBTable.TRIAL, session_pk)
    options = [{'label': f"Trial {k} of {total_trials}", 'value': str(k)} for k in trial_indices]
    sel_value = str(trial_indices[0]) if trial_indices and (len(trial_indices) > 0) else None
    return options, sel_value


@app.callback(Output(_TRIAL_VIEW_ID, "children"),
              [Input(_RESP_TRIAL_SELECT_ID, "value")],
              [State(_NEURON_TABLE_ID, "selected_rows"), State(_NEURON_TABLE_ID, "data")])
def on_trial_select(trial_idx_value, selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    try:
        trial_idx = int(trial_idx_value) if isinstance(trial_idx_value, str) else -1
    except Exception:
        trial_idx = -1
    if (trial_idx < 0) or (selected_rows is None):
        raise dash.exceptions.PreventUpdate

    trial_pk = {'experimenter': selected_row['experimenter'], 'subj_id': selected_row['subj_id'],
                'session_date': selected_row['session_date'], 'session_sfx': selected_row['session_sfx'],
                'trial_idx': trial_idx}
    trial_data = DataBaseManager.data_for_trial(trial_pk, unit_ids=[selected_row['unit_id']])
    if trial_data is None:
        return html.Div(f"Failed to retrieve trial data for trial index {trial_idx}")
    else:
        return _trial_figure(trial_data)


@app.callback([Output(_PROTO_VIEW_MODAL_ID, "is_open"), Output(_PROTO_VIEW_BODY_ID, "children")],
              [Input(_PROTO_VIEW_OPEN_ID, "n_clicks"), Input(_PROTO_VIEW_CLOSE_ID, "n_clicks")],
              [State(_RESP_PROTO_SELECT_ID, "value")])
def show_hide_protocol_definition(*args):
    ctx = dash.callback_context

    # no need for update when user changes the date but does not filter on that date
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if trigger_id == _PROTO_VIEW_CLOSE_ID:
        return False, dash.no_update
    elif trigger_id == _PROTO_VIEW_OPEN_ID:
        protocol = DataBaseManager.get_trial_protocol_definition(args[2])
        if protocol:
            return True, protocol.display_definition()
    return False, dash.no_update


def _filter_restrictions(username: str, subj_id: str, nt_id: str, date_op: str, date_iso: str) -> Optional[List[str]]:
    """
    Helper method prepares a list of string conditions -- in DataJoint syntax -- defining the filters that should be
    applied to restrict the set of neural units displayed in the main table on this panel.

    Args:
        username: If not _FILTER_UNUSED, restrict to neurons recorded by this experimenter.
        subj_id: If not _FILTER_UNUSED, restrict to neurons recorded in this subject.
        nt_id: If not _FILTER_UNUSED, restrict to neurons with this type ID (will be cast to int).
        date_op: If not _FILTER_UNUSED, restrict by recording session date ("on", "before", or "after")
        date_iso: The date in ISO format at 'YYYY-MM-DD'. If invalid, no date restriction is prepared.
    Returns:
        The list of restriction conditions. For example, ["username = 'sar'", "session_date < '2020-03-05'"]. If no
            filter restrictions are set, returns None.
    """
    restrictions = list()
    if username != _FILTER_UNUSED:
        restrictions.append(f"experimenter ='{username}'")
    if subj_id != _FILTER_UNUSED:
        restrictions.append(f"subj_id ='{subj_id}'")
    if nt_id != _FILTER_UNUSED:
        restrictions.append(f"unit_type = {int(nt_id)}")
    if (date_op != _FILTER_UNUSED) and check_date(date_iso):
        op_map = {'on': '=', 'before': '<', 'after': '>'}
        restrictions.append(f"session_date {op_map[date_op]} '{str(date_iso)}'")
    return restrictions if (len(restrictions) > 0) else None


@app.callback([Output(_FILTER_EXP_ID, "value"), Output(_FILTER_SUBJ_ID, "value"), Output(_FILTER_TYPE_ID, "value"),
               Output(_FILTER_DATE_ID, "value")], [Input(_FILTER_CLEAR_ID, "n_clicks")])
def clear_filters(n_clear):
    if not n_clear:
        raise dash.exceptions.PreventUpdate
    return _FILTER_UNUSED, _FILTER_UNUSED, _FILTER_UNUSED, _FILTER_UNUSED


filter_ids = [_FILTER_EXP_ID, _FILTER_SUBJ_ID, _FILTER_TYPE_ID, _FILTER_DATE_ID]
input_vector = [Input(sel_id, "value") for sel_id in filter_ids]
input_vector.append(Input(_DATE_PICKER_ID, "date"))
state_vector = [State(sel_id, "value") for sel_id in filter_ids]
state_vector.append(State(_DATE_PICKER_ID, "date"))


@app.callback([Output(_FILTER_COUNT_ID, "children"), Output(_NEURON_TABLE_ID, "selected_rows"),
               Output(_NEURON_TABLE_ID, "data")], input_vector, state_vector)
def update_filter_result_count(*args):
    ctx = dash.callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate

    # no need for update when user changes the date but does not filter on that date
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger_id == _DATE_PICKER_ID and args[8] == _FILTER_UNUSED:
        raise dash.exceptions.PreventUpdate

    restrictions = _filter_restrictions(args[5], args[6], args[7], args[8], args[9])
    rows = _fetch_neurons(restrictions)
    return f"{len(rows)} units found", [], rows
