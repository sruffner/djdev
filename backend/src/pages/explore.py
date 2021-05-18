"""
explore.py: The "explore database" page in the Lisberger lab's database portal app (/explore endpoint).

TODO: UNDER DEVELOPMENT - For now I'm using this to develop a panel for selecting any neural unit recording in the
database and examine that unit's response data. Eventually, this will be a generic starting point for searching
database content.

@author: sruffner
@created: 22mar2021
"""
from datetime import date
from typing import List, Dict, Any, Optional, Union

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
from database import stats
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
    db_mgr = DataBaseManager()
    rows = db_mgr.fetch_proj(DBTable.SESSION_NEURON, _NEURON_TABLE_ATTRS, restriction)
    # prepare values in "composed" columns
    neuron_type_map = {r['nt_id']: r['nt_name'] for r in db_mgr.fetch_rows(DBTable.NEURON_TYPE)}
    user_map = {r['username']: r['full_name'] for r in db_mgr.fetch_rows(DBTable.USER)}
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
    db_mgr = DataBaseManager()
    experimenters = list(db_mgr.fetch_proj(DBTable.USER, ['username', 'full_name']))
    experimenters.sort(key=lambda x: x['full_name'])
    experimenters.insert(0, {'username': _FILTER_UNUSED, 'full_name': _FILTER_UNUSED})
    subjects = db_mgr.fetch_attribute_values(DBTable.SUBJECT, "subj_id")
    subjects.sort()
    subjects.insert(0, _FILTER_UNUSED)
    neuron_types = list(db_mgr.fetch_rows(DBTable.NEURON_TYPE))
    neuron_types.sort(key=lambda x: x['nt_name'])
    neuron_types.insert(0, {'nt_id': _FILTER_UNUSED, 'nt_name': _FILTER_UNUSED})
    date_choices = [_FILTER_UNUSED, 'on', 'before', 'after']

    num_neurons = db_mgr.num_table_rows(DBTable.SESSION_NEURON)

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
        db_mgr = DataBaseManager()
        primary_key = {k: row_selected[k] for k in ti.primary_key_of(DBTable.SESSION_NEURON, False)}
        sampling_rate = db_mgr.fetch_attribute_values(DBTable.SESSION_EPHYS, 'sampling_rate', primary_key)[0]
        unit = db_mgr.fetch_rows(DBTable.SESSION_NEURON, primary_key)[0]
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
""" ID of dropdown that selects the trial protocol to view in the 'Response Data' panel. """
_RESP_RESP_SELECT_ID: str = 'resp_response_select'
""" ID of dropdown in 'Response Data' panel that selects the type of response displayed (response to a single trial,
mean firing rate across all trial reps, or discharge statistics). """
_RESP_PROTO_VIEW_OPEN_ID: str = 'resp_proto_view_open'
""" ID of button on 'Response Data' panel that raises the protocol definition modal window. """
_RESP_PROTO_VIEW_MODAL_ID: str = 'resp_proto_view_modal'
""" ID of Bootstrap Modal component in 'Response Data' panel on which a protocol definition is displayed. """
_RESP_PROTO_VIEW_BODY_ID: str = 'resp_proto_view_content'
""" ID of the Bootstrap ModalBody ('Response Data' panel) in which the protocol definition is embedded. """
_RESP_PROTO_VIEW_CLOSE_ID: str = 'resp_proto_view_close'
""" ID of button that extinguishes the Bootstrap Modal window in which a protocol is displayed. """
_RESP_RESP_VIEW_ID: str = "resp_resp_view"
""" ID of HTML Div in the 'Response Data' panel in which the selected response data is displayed. """


def _response_panel(row_selected: Dict[str, Any]) -> html.Div:
    proto_map = DataBaseManager().trial_protocols_for_neuron(row_selected)
    if proto_map is None:
        return html.Div(dbc.Alert(f"Failed to retrieve trial information for neuron (internal error).", is_open=True))

    first_proto_key = next(iter(proto_map.keys()))
    select_protocol = dbc.Select(
        id=_RESP_PROTO_SELECT_ID,
        options=[{'label': v, 'value': k} for k, v in proto_map.items()],
        value=first_proto_key,
        className="mb-2"
    )
    view_btn = dbc.Button("View details", id=_RESP_PROTO_VIEW_OPEN_ID, color='primary')
    proto_modal = dbc.Modal(
        [
            dbc.ModalBody(id=_RESP_PROTO_VIEW_BODY_ID),
            dbc.ModalFooter(
                dbc.Row([
                    dbc.Button("Close", id=_RESP_PROTO_VIEW_CLOSE_ID, color="primary")
                ])
            )
        ],
        id=_RESP_PROTO_VIEW_MODAL_ID, backdrop="static", size="xl", centered=True
    )

    # These get populated via chained callbacks.
    select_response = dbc.Select(id=_RESP_RESP_SELECT_ID)
    figure_view = html.Div(id=_RESP_RESP_VIEW_ID, children=[])

    markdown = dcc.Markdown(
        '''Select a trial protocol, then select an individual trial response or an aggregate response statistic. 
        *Aggregate response data is available only for those trial protocols for which 3 or more trial reps were 
        recorded. In addition, the trial protocol can have no random variables, or a single random-duration segment at
        the start of the trial.*'''
    )

    nav_row = dbc.Row([
        dbc.Col(select_protocol, width=6),
        dbc.Col([view_btn, proto_modal]),
        dbc.Col(select_response, width=3, className='mr-1')
    ])
    return html.Div([markdown, nav_row, figure_view])


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


def _single_trial_response_figure(unit_key: Dict[str, Any], trial_idx: int) -> Union[html.Div, dcc.Graph]:
    trial_pk = {'experimenter': unit_key['experimenter'], 'subj_id': unit_key['subj_id'],
                'session_date': unit_key['session_date'], 'session_sfx': unit_key['session_sfx'],
                'trial_idx': trial_idx}
    trial_data = DataBaseManager().data_for_trial(trial_pk, unit_ids=[unit_key['unit_id']])
    if trial_data is None:
        return html.Div(f"Failed to retrieve trial data for trial index {trial_idx}")

    fig = make_subplots(rows=2, cols=1, specs=[[{"secondary_y": True}], [{"secondary_y": True}]])
    hevel, vevel = trial_data.eye_velocity_saccades_removed()
    for response_id, trace in trial_data.behavior.items():
        adj_trace = hevel if response_id == 'HEVEL' else (vevel if response_id == 'VEVEL' else trace)
        fig.add_trace(
            go.Scatter(x=[i for i in range(len(adj_trace))], y=adj_trace, name=response_id, mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP[response_id], connectgaps=False,
                       yaxis='y2' if response_id.find('VEL') > -1 else None),
            row=1, col=1, secondary_y=(response_id.find('VEL') > -1)
        )
    fix1_pos, fix2_pos = trial_data.protocol.compute_fixation_target_trajectories(trial_data.trial_rvs)
    if fix1_pos is not None:
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix1_pos.shape[0])], y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix1_pos.shape[0])], y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
    if fix2_pos is not None:
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix2_pos.shape[0])], y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=[i for i in range(fix2_pos.shape[0])], y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
    for unit_id in trial_data.neuronal.keys():
        firing_rate_trace = trial_data.instantaneous_firing_rate(unit_id, smooth=True)
        fig.add_trace(
            go.Scatter(x=[i for i in range(len(firing_rate_trace))], y=firing_rate_trace, name=f"Unit #{unit_id}",
                       mode='lines', connectgaps=False, line=dict(color='black', width=2), yaxis='y3'),
            row=2, col=1, secondary_y=False
        )

        x_spikes = list()
        y_spikes = list()
        for t in trial_data.neuronal[unit_id]:
            x_spikes.extend([t * 1000, t * 1000, None])
            y_spikes.extend([9, 10, None])
        fig.add_trace(
            go.Scatter(x=x_spikes, y=y_spikes, name=f"Unit #{unit_id} spikes", mode='lines', connectgaps=False,
                       line=dict(color='blue', width=2), yaxis='y4'),
            row=2, col=1, secondary_y=True
        )

    fig.update_layout(
        margin=dict(l=20, r=20, t=30, b=20),
        height=800,
        xaxis=dict(domain=[0, 0.95], title='time (milliseconds)'),
        yaxis=dict(title='position (degrees)'),
        yaxis2=dict(title='velocity (degrees/second)', anchor="x", overlaying="y", side="right"),
        yaxis3=dict(title='firing rate (Hz)'),
        yaxis4=dict(range=[0, 10], anchor="x", overlaying="y3", visible=False)
    )

    if (len(trial_data.protocol.trial.segments) > 1) and (len(trial_data.trial_rvs) == 1) and \
            (trial_data.protocol.rvs[0].type == maestro.SegParamType.DURATION) and \
            (trial_data.protocol.rvs[0].seg_idx == 0):
        fig.add_vrect(x0=0, x1=trial_data.trial_rvs[0], fillcolor="red", opacity=0.2)

    return dcc.Graph(figure=fig)


def _retrieve_trial_data(unit_key: Dict[str, Any], proto_hash: str) -> Optional[List[TrialData]]:
    """
    Retrieve the data for all trial reps of the selected protocol for the selected neuron.

    Args:
        unit_key: Dictionary that includes the primary key of a selected neural unit in the database.
        proto_hash: The selected trial protocol's MD5 hash digest (the primary key in protocol database table).

    Returns:
        A list of trial data objects, one for each rep of the specified trial protocol during which specified neuron
            was recorded. Returns None if an error occurs while retrieving the dat.
    """
    db_mgr = DataBaseManager()
    trial_indices = db_mgr.trials_for_neuron(unit_key, proto_hash=proto_hash)
    ok = not (trial_indices is None)
    trial_data: List[TrialData] = list()
    trial_pk = unit_key.copy()
    if ok:
        for trial_idx in trial_indices:
            trial_pk['trial_idx'] = trial_idx
            td = db_mgr.data_for_trial(trial_pk, unit_ids=[unit_key['unit_id']])
            if td is None:
                ok = False
                break
            trial_data.append(td)
    return trial_data if ok else None


def _average_response_figure(unit_key: Dict[str, Any], proto_hash: str) -> Union[html.Div, dcc.Graph]:
    trial_data = _retrieve_trial_data(unit_key, proto_hash)
    if trial_data is None:
        return html.Div(dbc.Alert(f"Failed to retrieve trial data for neuron (internal error).", is_open=True))
    elif len(trial_data) < 3:
        return html.Div(dbc.Alert(f"Fewer than 3 trial reps ({len(trial_data)} found for selected protocol",
                                  is_open=True))

    unit_id = unit_key['unit_id']
    protocol = trial_data[0].protocol
    prelude = 0
    hevel_list = list()
    vevel_list = list()
    for td in trial_data:
        h, v = td.eye_velocity_saccades_removed()
        hevel_list.append(h)
        vevel_list.append(v)
    if len(protocol.rvs) == 0:
        hevel = np.nanmean(hevel_list, axis=0)
        vevel = np.nanmean(vevel_list, axis=0)
        firing_rate = np.nanmean([td.instantaneous_firing_rate(unit_id, smooth=True) for td in trial_data], axis=0)
        std_fr = np.nanstd([td.instantaneous_firing_rate(unit_id, smooth=True) for td in trial_data], axis=0)
        fix1_pos, fix2_pos = protocol.compute_fixation_target_trajectories([])
        t_vec = [i for i in range(len(hevel))]
    else:
        # assumption: the RV is the duration of segment 0. Prelude P is the minimum seg 0 duration across trial reps.
        # We average responses starting P ticks before the end of segment 0.
        prelude = int(min([td.trial_rvs[0] for td in trial_data]))
        hevel = np.nanmean([hevel_list[i][td.trial_rvs[0]-prelude:] for i, td in enumerate(trial_data)], axis=0)
        vevel = np.nanmean([vevel_list[i][td.trial_rvs[0]-prelude:] for i, td in enumerate(trial_data)], axis=0)
        firing_rate = np.nanmean([td.instantaneous_firing_rate(unit_id, smooth=True)[td.trial_rvs[0]-prelude:]
                                  for td in trial_data], axis=0)
        std_fr = np.nanstd([td.instantaneous_firing_rate(unit_id, smooth=True)[td.trial_rvs[0]-prelude:]
                            for td in trial_data], axis=0)
        fix1_pos, fix2_pos = protocol.compute_fixation_target_trajectories(trial_data[0].trial_rvs)
        if fix1_pos is not None:
            fix1_pos = fix1_pos[trial_data[0].trial_rvs[0]-prelude:, :]
        if fix2_pos is not None:
            fix2_pos = fix2_pos[trial_data[0].trial_rvs[0]-prelude:, :]
        t_vec = [i-prelude for i in range(len(hevel))]

    fig = make_subplots(rows=2, cols=1, specs=[[{"secondary_y": True}], [{"secondary_y": False}]])
    fig.add_trace(
        go.Scatter(x=t_vec, y=hevel, name='HEVEL', mode='lines', line=_BEHAVIOR_TRACE_STYLE_MAP['HEVEL'], yaxis='y2'),
        row=1, col=1, secondary_y=True
    )
    fig.add_trace(
        go.Scatter(x=t_vec, y=vevel, name='VEVEL', mode='lines', line=_BEHAVIOR_TRACE_STYLE_MAP['VEVEL'], yaxis='y2'),
        row=1, col=1, secondary_y=True
    )
    if fix1_pos is not None:
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
    if fix2_pos is not None:
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
        fig.add_trace(
            go.Scatter(x=t_vec, y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False),
            row=1, col=1, secondary_y=False
        )
    fig.add_trace(
        go.Scatter(x=t_vec, y=firing_rate, mode='lines', connectgaps=False, line=dict(color='black', width=2),
                   name=f"Unit #{unit_id}", yaxis='y3'),
        row=2, col=1, secondary_y=False
    )
    fig.add_trace(
        go.Scatter(x=t_vec, y=firing_rate+std_fr, mode='lines', connectgaps=True, line=dict(width=0),
                   name="+1STD", yaxis='y3', showlegend=False),
        row=2, col=1, secondary_y=False
    )
    fig.add_trace(
        go.Scatter(x=t_vec, y=firing_rate-std_fr, mode='lines', connectgaps=True, line=dict(width=0),
                   name="-1STD", yaxis='y3', fillcolor='rgba(68, 68, 68, 0.3)', fill='tonexty', showlegend=False),
        row=2, col=1, secondary_y=False
    )

    fig.update_layout(
        margin=dict(l=20, r=20, t=30, b=20),
        height=800,
        xaxis=dict(domain=[0, 0.95], title='time (milliseconds)'),
        yaxis=dict(title='position (degrees)'),
        yaxis2=dict(title='velocity (degrees/second)', anchor="x", overlaying="y", side="right"),
        yaxis3=dict(title='firing rate (Hz) [mean +/- 1STD]')
    )

    if prelude > 0:
        fig.add_vrect(x0=-prelude, x1=0, fillcolor="red", opacity=0.2)

    return dcc.Graph(figure=fig)


_RESP_DS_RANGE_ID: str = 'resp_ds_range_slider'
""" ID of widget selecting the trial interval over which the discharge statistics are computed. """
_RESP_DS_GRAPH_ID: str = 'resp_ds_graph'
""" ID of dcc.Graph in which discharge statistics are plotted. """


def _discharge_statistics_panel(unit_key: Dict[str, Any], proto_hash: str) -> html.Div:
    trial_data = _retrieve_trial_data(unit_key, proto_hash)
    if trial_data is None:
        return html.Div(dbc.Alert(f"Failed to retrieve trial data for neuron (internal error).", is_open=True))
    elif len(trial_data) < 3:
        return html.Div(dbc.Alert(f"Fewer than 3 trial reps ({len(trial_data)} found for selected protocol",
                                  is_open=True))

    protocol = trial_data[0].protocol
    # since we only compute aggregate response data for trial protocols with no RVs or a single random-duration segment
    # at the start of trial, this prelude calcluation is valid
    prelude_ms = 0 if len(protocol.rvs) == 0 else int(min([td.trial_rvs[0] for td in trial_data]))
    fix1_pos, fix2_pos = protocol.compute_fixation_target_trajectories(trial_data[0].trial_rvs)
    if (fix1_pos is not None) and (prelude_ms > 0):
        fix1_pos = fix1_pos[trial_data[0].trial_rvs[0] - prelude_ms:, :]
    if (fix2_pos is not None) and (prelude_ms > 0):
        fix2_pos = fix2_pos[trial_data[0].trial_rvs[0] - prelude_ms:, :]
    t_vec_proto = [i-prelude_ms for i in range(len(fix1_pos))] if (fix1_pos is not None) else None

    proto_plot = None
    if fix1_pos is not None:
        proto_plot = go.Figure()
        proto_plot.add_trace(
            go.Scatter(x=t_vec_proto, y=fix1_pos[:, 0], name='FIX1_HPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_HPOS'], connectgaps=False))
        proto_plot.add_trace(
            go.Scatter(x=t_vec_proto, y=fix1_pos[:, 1], name='FIX1_VPOS', mode='lines',
                       line=_BEHAVIOR_TRACE_STYLE_MAP['FIX1_VPOS'], connectgaps=False))
        if fix2_pos is not None:
            proto_plot.add_trace(
                go.Scatter(x=t_vec_proto, y=fix2_pos[:, 0], name='FIX2_HPOS', mode='lines',
                           line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_HPOS'], connectgaps=False))
            proto_plot.add_trace(
                go.Scatter(x=t_vec_proto, y=fix2_pos[:, 1], name='FIX2_VPOS', mode='lines',
                           line=_BEHAVIOR_TRACE_STYLE_MAP['FIX2_VPOS'], connectgaps=False))
        proto_plot.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=300,
            xaxis=dict(title='time (milliseconds)'),
            yaxis=dict(title='position (degrees)'),
            showlegend=False
        )

    # range slider selects the interval for the computation. Initially set to the entire trial timeline that is
    # shared by all reps (in case seg 0 has random duration).
    if prelude_ms == 0:
        range_min = 0
        range_max = protocol.trial.duration()
        range_marks = {0: {"label": "0"}, range_max: {"label": f"{range_max}"}}
    else:
        range_min = -prelude_ms
        range_max = protocol.trial.duration() - protocol.trial.segments[0].dur
        range_marks = {range_min: {"label": f"{range_min}"}, 0: {"label": "0"}, range_max: {"label": f"{range_max}"}}

    slider = dcc.RangeSlider(
        id=_RESP_DS_RANGE_ID,
        min=range_min, max=range_max, step=1,
        value=[range_min, range_max],
        marks=range_marks, allowCross=False,
        pushable=100,  # at least 100ms range
        tooltip={'always_visible': False, 'placement': 'bottom'}
    )
    instruction = dcc.Markdown('''**Select trial interval over which discharge statistics are computed:**''')
    slider_row = html.Div([instruction, slider])

    # note: the dcc.Graph displaying discharge statistics is populated by a chained callback
    if proto_plot is not None:
        return html.Div([
            dcc.Graph(figure=proto_plot, config=dict(staticPlot=True)),
            slider_row,
            dcc.Graph(id=_RESP_DS_GRAPH_ID, config=dict(staticPlot=True))
        ])
    else:
        return html.Div([slider_row, dcc.Graph(id=_RESP_DS_GRAPH_ID, config=dict(staticPlot=True))])


def _discharge_statistics_figure(
        unit_id: int, trial_data: List[TrialData], range_ms: Optional[List[int]] = None) -> go.Figure:
    isi: Optional[np.ndarray] = None
    acg: Optional[np.ndarray] = None
    num_spikes_in_acg = 0
    try:
        protocol = trial_data[0].protocol
        prelude_ms = 0 if len(protocol.rvs) == 0 else int(min([td.trial_rvs[0] for td in trial_data]))
        for td in trial_data:
            spike_times = td.neuronal[unit_id]
            if range_ms is None:
                if prelude_ms > 0:
                    spike_times = spike_times[spike_times >= 1e-3*(td.trial_rvs[0]-prelude_ms)]
            else:
                start = td.trial_rvs[0] + range_ms[0] if prelude_ms > 0 else range_ms[0]
                end = start + (range_ms[1] - range_ms[0])
                spike_times = spike_times[np.logical_and(spike_times >= 1e-3*start, spike_times < 1e-3*end)]
            if len(spike_times) < 2:
                continue
            isi_for_trial = stats.generate_isi_histogram(spike_times)
            isi = isi_for_trial if isi is None else (isi + isi_for_trial)
            acg_for_trial, n = stats.generate_cross_correlogram(spike_times, spike_times)
            acg = acg_for_trial if acg is None else (acg + acg_for_trial)
            num_spikes_in_acg += n
    except Exception:
        isi = None
        acg = None
        num_spikes_in_acg = 0

    ds_plot = make_subplots(
        rows=1, cols=2, subplot_titles=(f"Autocorrelogram (N={num_spikes_in_acg})", 'Inter-spike Interval Histogram')
    )
    if not (acg is None):
        ds_plot.add_trace(go.Scatter(x=[i for i in range(-100, 101)], y=acg, mode='lines', connectgaps=False),
                          row=1, col=1)
    if not (isi is None):
        ds_plot.add_trace(go.Scatter(x=[i for i in range(0, 101)], y=isi, mode='lines', xaxis='x2', yaxis='y2'),
                          row=1, col=2)
    ds_plot.update_layout(
        margin=dict(l=20, r=20, t=30, b=20),
        xaxis=dict(title='lag (milliseconds)'),
        yaxis=dict(title='counts per bin'),
        xaxis2=dict(title='ISI (ms)'),
        yaxis2=dict(title='counts per bin'),
        showlegend=False
    )

    return ds_plot


_SUMMARY_TAB_ID: str = "unit_summary_tab"
""" ID of 'Summary' tab panel in which the selected neuron's summary information is displayed. """
_RESPONSE_TAB_ID: str = "unit_response_tab"
""" ID of 'Trial Response Data' tab panel in which the selected neuron's response data is displayed. """
_COLLAPSE_ID: str = "unit_collapse_id"
""" ID of Dash Bootstrap Collapse element wrapping the tabbed panel displaying details for a selected neuron. The
element is hidden when no neuron is selected. """


def serve_layout() -> html.Div:
    detail_panel = dbc.Tabs(
        [
            dbc.Tab(dbc.Card(dbc.CardBody(children=[], id=_SUMMARY_TAB_ID), className='mt-2'), label="Summary"),
            dbc.Tab(dbc.Card(dbc.CardBody(children=[], id=_RESPONSE_TAB_ID), className='mt-2'), label="Response Data")
        ]
    )
    """ Rendering of a tabbed panel in which a selected neuron's summary and response data are displayed. """

    layout = html.Div([
        dbc.Container([
            dbc.Row(dbc.Col(html.H3("Explore neuron recordings in the laboratory database", className="text-center")),
                    className="mb-3 mt-3"),
            dbc.Row(dbc.Col(html.Div(id=_NEURON_TABLE_DIV_ID, children=_table_of_neurons())), className="mb-3"),
            _filter_group(),
            dbc.Row(dbc.Col(dbc.Collapse(detail_panel, id=_COLLAPSE_ID)))
        ])
    ])
    return layout


@app.callback([Output(_COLLAPSE_ID, "is_open"), Output(_SUMMARY_TAB_ID, "children"),
               Output(_RESPONSE_TAB_ID, "children")],
              [Input(_NEURON_TABLE_ID, "selected_rows")], [State(_NEURON_TABLE_ID, "data")])
def show_hide_detail_pane(selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    summary_tab = _unit_summary(selected_row) if selected_row else html.Div("Not available.")
    response_tab = _response_panel(selected_row) if selected_row else html.Div("Not available.")
    return selected_row is not None, summary_tab, response_tab


@app.callback([Output(_RESP_RESP_SELECT_ID, "options"), Output(_RESP_RESP_SELECT_ID, "value")],
              [Input(_RESP_PROTO_SELECT_ID, "value")],
              [State(_NEURON_TABLE_ID, "selected_rows"), State(_NEURON_TABLE_ID, "data")])
def on_response_panel_proto_select(proto_hash_value, selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    if (proto_hash_value is None) or (selected_rows is None):
        raise dash.exceptions.PreventUpdate
    db_mgr = DataBaseManager()
    trial_indices = db_mgr.trials_for_neuron(selected_row, proto_hash_value)
    session_pk = {'experimenter': selected_row['experimenter'], 'subj_id': selected_row['subj_id'],
                  'session_date': selected_row['session_date'], 'session_sfx': selected_row['session_sfx']}
    total_trials = db_mgr.num_table_rows(DBTable.TRIAL, session_pk)
    options = [{'label': f"Trial {k} of {total_trials}", 'value': str(k)} for k in trial_indices]
    can_aggregate = (proto_hash_value in DataBaseManager().trial_protocols_for_neuron(selected_row, aggregate=True))
    options.append({'label': "Mean firing rate", 'value': 'mfr', 'disabled': not can_aggregate})
    options.append({'label': "Discharge statistics", 'value': 'ds', 'disabled': not can_aggregate})
    sel_value = 'mfr' if can_aggregate else \
        (str(trial_indices[0]) if trial_indices and (len(trial_indices) > 0) else None)
    return options, sel_value


@app.callback(Output(_RESP_RESP_VIEW_ID, "children"), [Input(_RESP_RESP_SELECT_ID, "value")],
              [State(_NEURON_TABLE_ID, "selected_rows"), State(_NEURON_TABLE_ID, "data"),
               State(_RESP_PROTO_SELECT_ID, "value")])
def on_response_panel_response_select(resp_sel_value, selected_rows, rows, proto_hash_value):
    # resp_sel_value is trial index OR 'mfr' (mean firing rate) OR 'ds' (discharge statistics)
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_unit = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    out = dash.no_update
    if not ((selected_unit is None) or (proto_hash_value is None)):
        if resp_sel_value == 'mfr':
            out = _average_response_figure(selected_unit, proto_hash_value)
        elif resp_sel_value == 'ds':
            out = _discharge_statistics_panel(selected_unit, proto_hash_value)
        else:
            try:
                trial_idx = int(resp_sel_value) if isinstance(resp_sel_value, str) else -1
                if trial_idx >= 0:
                    out = _single_trial_response_figure(selected_unit, trial_idx)
            except Exception:
                pass
    return out


@app.callback(Output(_RESP_DS_GRAPH_ID, "figure"), [Input(_RESP_DS_RANGE_ID, "value")],
              [State(_NEURON_TABLE_ID, "selected_rows"), State(_NEURON_TABLE_ID, "data"),
               State(_RESP_PROTO_SELECT_ID, "value")])
def on_response_panel_ds_range(range_value, selected_rows, rows, proto_hash_value):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_unit = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    ok = isinstance(range_value, list) and not ((selected_unit is None) or (proto_hash_value is None))
    return dash.no_update if not ok else _discharge_statistics_figure(
        selected_unit['unit_id'], _retrieve_trial_data(selected_unit, proto_hash_value), range_value)


@app.callback([Output(_RESP_PROTO_VIEW_MODAL_ID, "is_open"), Output(_RESP_PROTO_VIEW_BODY_ID, "children")],
              [Input(_RESP_PROTO_VIEW_OPEN_ID, "n_clicks"), Input(_RESP_PROTO_VIEW_CLOSE_ID, "n_clicks")],
              [State(_RESP_PROTO_SELECT_ID, "value")])
def on_response_panel_show_hide_protocol_definition(*args):
    ctx = dash.callback_context

    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    if trigger_id == _RESP_PROTO_VIEW_CLOSE_ID:
        return False, dash.no_update
    elif trigger_id == _RESP_PROTO_VIEW_OPEN_ID:
        protocol = DataBaseManager().get_trial_protocol_definition(args[2])
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
