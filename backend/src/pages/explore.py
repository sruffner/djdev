"""
explore.py: The "explore database" page in the Lisberger lab's database portal app (/explore endpoint).

TODO: UNDER DEVELOPMENT - For now I'm using this to develop a panel for selecting any neural unit recording in the
database and examine that unit's response data. Eventually, this will be a generic starting point for searching
database content.

@author: sruffner
@created: 22mar2021
"""
from typing import List, Dict, Any

import dash
import dash_html_components as html
import dash_bootstrap_components as dbc
import dash_core_components as dcc
import dash_table as dt
from dash.dependencies import Input, Output, State
import numpy as np
import plotly.express as px

from app import app
import database.table_info as ti
from database.manager import DataBaseManager
from database.table_info import DBTable


_NEURON_TABLE_ID: str = "neuron_list"
""" The ID assigned to the Dash DataTable presenting the list of neurons in the database. """

_NEURON_TABLE_ATTRS: List[str] = [
    'experimenter', 'subj_id', 'session_date', 'session_sfx', 'unit_id', 'unit_type', 'unit_rate'
]
""" DBTable.SESSION_NEURON attributes that are fetched from database for display in neuron table. """

_NEURON_TABLE_COLS: List[ti.Column] = [
    ti.Column('full_name', 'Experimenter', '100px', False),
    ti.Column('subj_id', 'Subject', '100px', False),
    ti.Column('session', 'Session', '100px', False),
    ti.Column('unit_id', 'Unit #', '100px', False),
    ti.Column('nt_name', 'Neuron Type', '100px', False),
    ti.Column('unit_rate', 'Rate (Hz)', '100px', False)
]
""" Defined columns for the neuron table."""

_NEURON_TABLE_DIV_ID: str = "nt_div"
""" ID assigned to the Dash Bootstrap CardBody in which the table of neurons is embedded. """
_REFRESH_BTN_ID: str = "refresh_btn"
""" Clicking this button refreshes the contents of the table of neurons. """


def _table_of_neurons() -> dt.DataTable:
    rows = DataBaseManager.fetch_proj(DBTable.SESSION_NEURON, _NEURON_TABLE_ATTRS)
    # prepare values in "composed" columns
    neuron_type_map = {r['nt_id']: r['nt_name'] for r in DataBaseManager.fetch_rows(DBTable.NEURON_TYPE)}
    user_map = {r['username']: r['full_name'] for r in DataBaseManager.fetch_rows(DBTable.USER)}
    for row in rows:
        row['nt_name'] = neuron_type_map[row['unit_type']]
        row['full_name'] = user_map[row['experimenter']]
        row['session'] = f"{row['session_date']} ({row['session_sfx']})"

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


def _unit_summary(row_selected: Dict[str, Any]) -> html.Div:
    # TODO: What if a database error occurs -- need to fail gracefully
    # TODO: It's kind of a pain that you have to assemble the full primary key for a part table, as we do here
    # retrieve the sampling rate for the neural recording, then retrieve the full unit record
    primary_key = {k: row_selected[k] for k in ti.primary_key_of(DBTable.SESSION)}
    sampling_rate = DataBaseManager.fetch_attribute_values(DBTable.SESSION_EPHYS, 'sampling_rate', primary_key)[0]
    primary_key['unit_id'] = row_selected['unit_id']
    unit = DataBaseManager.fetch_rows(DBTable.SESSION_NEURON, primary_key)[0]

    unit_template: np.ndarray = unit['unit_template']
    peak_to_peak = max(unit_template) - min(unit_template)

    table_rows = [
        ["Unit # :", f"{unit['unit_id']}"],
        ["Neuron Type :", f"{row_selected['nt_name']}"],
        ["Date Recorded :", f"{unit['session_date']}"],
        ["Subject ID :", f"{unit['subj_id']}"],
        ["Experimenter :", f"{row_selected['full_name']}"],
        ["Omniplex Channel :", f"{unit['unit_channel']}"],
        ["Firing Rate :", f"{unit['unit_rate']:.1f} Hz"],
        ["Signal-to-Noise:", f"{unit['unit_snr']:.2f}"],
        ["Peak-to-Peak :", f"{peak_to_peak:.1f} \u00B5V"]
    ]
    table_body = html.Tbody([
        html.Tr([html.Td(row[0], className='text-right'), html.Td(row[1], className='text-left text-info')])
        for row in table_rows
    ])
    info_table = dbc.Table([table_body], striped=True, bordered=True)

    # simple graph of template waveform.
    to_msecs = 1000.0 / sampling_rate
    graph = dcc.Graph(figure=px.line(x=[i * to_msecs for i in range(len(unit_template))], y=unit_template,
                                     labels={'x': 'time (ms)', 'y': '\u00B5V'},
                                     title='Average spike waveform (1-ms pre, 9-ms post)'))

    return html.Div([
        dbc.Row([
            dbc.Col(info_table, width=4),
            dbc.Col(graph)
        ])
    ])


_SUMMARY_TAB_ID: str = "unit_summary_tab"
""" ID of tab panel in which the selected neuron's summary information is displayed. """
_RESPONSE_TAB_ID: str = "unit_response_tab"
""" ID of tab panel in which the selected neuron's response data is displayed. """
_TAB_SEL_ID: str = "unit_sel_tabs"
""" ID of Dash Bootstrap Tabs component rendering the two tabs in which unit data is displayed. """
_TAB_BODY_ID: str = "unit_sel_tab_body"
""" ID of container in which currently selected tab panel is rendered. """
_COLLAPSE_ID: str = "unit_collapse_id"
""" ID of Dash Bootstrap Collapse element wrapping the tabbed panel displaying details for a selected neuron. The
element is hidden when no neuron is selected. """

_tabs_card = dbc.Card(
    [
        dbc.CardHeader(
            dbc.Tabs(
                [dbc.Tab(label="Summary", tab_id=_SUMMARY_TAB_ID),
                 dbc.Tab(label="Trial Responses", tab_id=_RESPONSE_TAB_ID)],
                id=_TAB_SEL_ID,
                card=True, persistence=True, persistence_type="session",
                active_tab=_SUMMARY_TAB_ID,
            )
        ),
        dbc.CardBody(id=_TAB_BODY_ID, children=[])
    ]
)
""" Rendering of the tabbed panel in which a selected neuron's summary and response data are displayed. """

layout = html.Div([
    dbc.Container([
        dbc.Row(dbc.Col(html.H3("Explore neuron recordings in the laboratory database", className="text-center")),
                className="mb-3 mt-3"),
        dbc.Row(dbc.Col(html.Div(id=_NEURON_TABLE_DIV_ID, children=_table_of_neurons())), className="mb-3"),
        dbc.Row(dbc.Col(dbc.Button("Refresh", id=_REFRESH_BTN_ID, color="primary", className="mr-2")),
                className="mb-3"),
        dbc.Row(dbc.Col(dbc.Collapse(_tabs_card, id=_COLLAPSE_ID)))
    ])
])


@app.callback(
    [Output(_COLLAPSE_ID, "is_open"), Output(_TAB_BODY_ID, "children")],
    [Input(_NEURON_TABLE_ID, "selected_rows"), Input(_TAB_SEL_ID, "active_tab")],
    [State(_NEURON_TABLE_ID, "selected_rows"), State(_NEURON_TABLE_ID, "data"), State(_TAB_SEL_ID, "active_tab")])
def update_neuron_detail_pane(selected_rows, active_tab, *args):
    ctx = dash.callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate

    curr_tab = None
    selected_row = None
    rows = args[1]
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger_id == _TAB_SEL_ID:
        sel_rows = args[0]
        idx = sel_rows[0] if (sel_rows is not None) and (len(sel_rows) > 0) else -1
        curr_tab = active_tab
        selected_row = rows[idx] if (-1 < idx < len(rows)) else None
    elif trigger_id == _NEURON_TABLE_ID:
        idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
        rows = args[1]
        curr_tab = args[2]
        selected_row = rows[idx] if (-1 < idx < len(rows)) else None
    is_open = (selected_row is not None)
    tab_pane = dash.no_update
    if is_open:
        tab_pane = _unit_summary(selected_row) if curr_tab == _SUMMARY_TAB_ID else html.Div("NOT YET IMPLEMENTED")
    return is_open, tab_pane
