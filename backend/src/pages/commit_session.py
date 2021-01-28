"""
commit_session.py: The "commit session" page in web-based interface to the Lisberger laboratory database.

This web page governs a step-by-step, stateful process by which the user commits an experiment session's worth of data
to the laboratory database.

Details of the session commit process are outlined in the session_builder.py module, which implements the server-side
functionality of a session commit. This Dash web page implements the client-side functionality.

The page layout essentially consists of a single Dash Bootstrap Card element in which the header, body and footer of the
card are updated for each stage in the commit process. The _SessionCommitter class implements the per-stage card
content. Because the commit process requires a multi-stage interaction between client and server, a Dash Store element
in the page layout is used to preserve the current state of the procedure. The store uses 'local' browser memory, which
means that the state is preserved even if the browser tab is closed or the browser itself shutdown. The state info in
the store is included in server callbacks in order to synchronize client and server. See session_builder.py for a
detailed explanation.

@author: sruffner
"""

import dash
import dash_html_components as html
import dash_core_components as dcc
import dash_bootstrap_components as dbc
import dash_table as dt
import dash_uploader as du
from dash.dependencies import Input, Output, State
import plotly.express as px
from app import app
import json
from database.session_builder import SessionBuilder, SessionBuilderError
from database.table_views import SessionView
from typing import Any, List, Dict


class _SessionCommitter:
    def __init__(self, dash_app: dash.Dash):
        self._app = dash_app
        self._callbacks()

    @staticmethod
    def stage1_body() -> Any:
        # TODO: NOTE that this is essentially a repeat of _BasePanel._entry_form() in curate_panels.py
        form_groups = []
        session_view = SessionView()
        for attr in session_view.attributes():
            if attr.type == 'enum':
                entry_widget = dbc.Select(
                    id=f"{attr.id}_input",
                    options=[{"label": opt, "value": opt} for opt in attr.options],
                    value=attr.options[0]
                )
            elif attr.type == 'fkey':
                fkey_choices = session_view.foreign_key_choices(attr)
                entry_widget = dbc.Select(
                    id=f"{attr.id}_input",
                    options=[{"label": opt[0], "value": opt[1]} for opt in fkey_choices],
                    value=fkey_choices[0][1]
                )
            elif attr.textrange[1] > 100:
                entry_widget = dbc.Textarea(
                    id=f"{attr.id}_input",
                    minLength=attr.textrange[0], maxLength=attr.textrange[1],
                    rows=2 if attr.textrange[1] < 400 else 4,
                    value="",
                    placeholder=attr.placeholder
                )
            else:
                if (attr.type == 'float') or (attr.type == 'int'):
                    input_type = 'number'
                else:
                    input_type = 'email' if 'email' in attr.id else 'text'
                entry_widget = dbc.Input(
                    id=f"{attr.id}_input",
                    type=input_type,
                    minLength=attr.textrange[0], maxLength=attr.textrange[1],
                    value="",
                    placeholder=attr.placeholder
                )

            form_groups.append(dbc.FormGroup(
                [
                    dbc.Label(attr.label, width=2),
                    dbc.Col(entry_widget, width=10)
                ],
                row=True,
            ))

        # alert raised if attempt to create new session fails - displays a brief error message. Otherwise hidden.
        form_groups.append(dbc.FormGroup(
            dbc.Alert("", id=f"stage1_alert", dismissable=True, duration=10000, fade=True, is_open=False)
        ))

        return dbc.Form(form_groups)

    @staticmethod
    def stage2_body(state: dict) -> Any:
        markdown = dcc.Markdown('''
        **Instructions**:

        * All session data files (Maestro and Plexon) must be compressed into a single, flat ZIP archive (containing no
        subdirectories). Maximum supported file size is 2GB.
        * If the session includes behavioral data only, the archive should contain only the Maestro data files.
        * There is no support at this time for automatic spike sorting. If the experiment includes electrophysiological 
        recordings, the experimenter must supply neural unit data (spike trains) in a separate MAT or Numpy file
        named **neural-units.mat** or **neural-units.npy**, respectively.
        
        *Drag and drop the ZIP file onto the upload component below, or click on the component to browse the file
        system for the file. The upload should start automatically. **Do NOT close browser tab while upload is in
        progress**.*
        
        ''')
        uploader = du.Upload(id="session_archive_uploader", max_file_size=10000, max_files=1, cancel_button=False,
                             filetypes=['zip'], upload_id=f"{state['experimenter']}-{state['uuid']}")
        upload_div = html.Div(uploader, id="uploader_container", className="mb-3")
        intv_check = dcc.Interval(id="stage2_check_progress", disabled=True, interval=1000)
        alert = dbc.Alert(id="stage2_alert", color="info", is_open=False)
        return [markdown, upload_div, intv_check, alert]

    __STAGE_HEADERS = {
        1: 'Step 1: Enter session information',
        2: 'Step 2: Upload session data archive',
        3: 'Step 3: Review trial protocols',
        4: 'Step 4: Review neural units'
    }

    @staticmethod
    def stage3_body(state: dict) -> Any:
        # NOTE - At this point, summaries of the trial protocols culled in stage 2 have been transferred to the
        # client and stored as part of the client state, in the field 'protocols', as a dictionary keyed by the
        # protocols' md5 digests
        markdown = dcc.Markdown('''
        **After reviewing the trial protocols here, click "Continue" to proceed to the next step. If you detect an
        issue, click "Cancel" to start over.**
        ''')
        proto_map: Dict[str, Dict[str, Any]] = state['protocols']
        protocols_by_path = [proto_map[k] for k in sorted(proto_map.keys(), key=lambda x: proto_map[x]['path_name'])]
        initial_selection = protocols_by_path[0]
        select_protocol = dbc.Select(
            id='stage3_proto_select',
            options=[{'label': p['path_name'], 'value': p['digest']} for p in protocols_by_path],
            value=initial_selection['path_name']
        )
        protocol_div = html.Div(_SessionCommitter.stage3_display_protocol(initial_selection), id="stage3_protocol_div")
        return [markdown, select_protocol, protocol_div]

    @staticmethod
    def stage3_display_protocol(protocol: Dict[str, Any]) -> List[Any]:
        segments = protocol['segments']
        targets = protocol['targets']
        target_names = [target_desc.split(':')[0] for target_desc in targets]  # THIS IS A HACK
        perts = protocol['perts']
        sections = protocol['sections']
        diffs = protocol['diffs']

        badges = [
            dbc.Badge(f"Record Seg: {protocol['record_seg']}", color="primary", className="mr-3"),
            dbc.Badge(f"Transform: {protocol['transform']}", color="primary", className="mr-3"),
            dbc.Badge(f"Targets: {len(target_names)}", id="stage3_targets", color="primary", className="mr-3"),
            dbc.Badge(f"Perturbations: {len(perts)}", id="stage3_perts", color="primary", className="mr-3"),
            dbc.Badge(f"Tagged Sections: {len(sections)}", id="stage3_sections", color="primary", className="mr-3"),
            dbc.Badge(f"Random Vars: {len(diffs)}", id="stage3_random_vars", color="primary"),
            dbc.Tooltip([html.Div(f"{str(target)}") for target in targets],
                        target="stage3_targets", style={'max-width': '600px'})
        ]
        if len(perts) > 0:
            badges.append(
                dbc.Tooltip([html.Div(f"{str(pert)}") for pert in perts],
                            target="stage3_perts", style={'max-width': '600px'})
            )
        if len(sections) > 0:
            badges.append(
                dbc.Tooltip([html.Div(f"{str(section)}") for section in sections],
                            target="stage3_sections", style={'max-width': '600px'})
            )
        if len(diffs) > 0:
            badges.append(
                dbc.Tooltip([html.Div(f"{str(diff)}") for diff in diffs],
                            target="stage3_random_vars", style={'max-width': '600px'})
            )

        columns = [{"name": "", "id": "param"}]
        columns.extend([{"name": f"Segment {i}", "id": f"seg_{i}"} for i in range(len(segments))])

        duration = {"param": "Duration (ms)"}
        fix1_tgt = {"param": "Fix Tgt #1"}
        fix2_tgt = {"param": "Fix Tgt #2"}
        xy_delta = {"param": "XYScope Intv (ms)"}
        marker = {"param": "Marker Pulse"}
        tgt_on = [{"param": name} for name in target_names]
        tgt_vstab = [{"param": "VStab"} for _ in target_names]
        tgt_pos = [{"param": "Position (deg)"} for _ in target_names]
        tgt_vel_acc = [{"param": "Vel (d/s), Acc (d/s^2)"} for _ in target_names]
        tgt_pat = [{"param": "Pattern Vel, Acc"} for _ in target_names]
        for i, seg in enumerate(segments):
            seg_id = f"seg_{i}"
            duration[seg_id] = seg['dur']
            fix1_tgt[seg_id] = "NONE" if seg['fix1'] < 0 else target_names[seg['fix1']]
            fix2_tgt[seg_id] = "NONE" if seg['fix2'] < 0 else target_names[seg['fix2']]
            xy_delta[seg_id] = seg['xy_update']
            marker[seg_id] = "NONE" if seg['marker'] < 0 else f"DO{seg['marker']}"
            for tgt_idx in range(len(target_names)):
                trajectory = seg['trajectories'][tgt_idx]
                tgt_on[tgt_idx][seg_id] = "ON" if trajectory['on'] else 'OFF'
                tgt_vstab[tgt_idx][seg_id] = trajectory['vstab']
                tgt_pos[tgt_idx][seg_id] = trajectory['pos']
                tgt_vel_acc[tgt_idx][seg_id] = f"{trajectory['vel']}  {trajectory['acc']}"
                tgt_pat[tgt_idx][seg_id] = f"{trajectory['patvel']}  {trajectory['patacc']}"
        rows = [duration, fix1_tgt, fix2_tgt, xy_delta, marker]
        for i in range(len(target_names)):
            rows.extend([tgt_on[i], tgt_vstab[i], tgt_pos[i], tgt_vel_acc[i], tgt_pat[i]])

        tgt_name_row_indices = [5 + i*5 for i in range(len(target_names))]
        segment_table = dt.DataTable(
            columns=columns,
            data=rows,
            style_header={'fontWeight': 'bold', 'textAlign': 'center'},
            style_cell={'textAlign': 'center', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
            style_cell_conditional=[
                {'if': {'column_id': 'param'}, 'width': '200px'}
            ],
            style_data_conditional=[
                {'if': {'column_id': 'param'}, 'textAlign': 'right'},
                {'if': {'column_id': 'param', 'row_index': tgt_name_row_indices},
                 'textDecoration': 'underline', 'textAlign': 'left'},
                {'if': {'row_index': tgt_name_row_indices}, 'backgroundColor': 'rgba(218,165,32,128)', 'color': 'black'}
            ],
            style_data={'whiteSpace': 'pre-wrap'},
            style_table={'height': '330px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
            fixed_rows={'headers': True, 'data': 0},
            fixed_columns={'headers': True, 'data': 0}
        )
        return [html.Div(badges, className='mt-3 mb-1'), segment_table]

    @staticmethod
    def stage4_body(state: dict) -> Any:
        # NOTE - At this point, summaries of the neural units culled in stage 2 have been transferred to the
        # client and stored as part of the client state, in the field 'units', as a list of dictionaries...
        markdown = dcc.Markdown('''
        **After reviewing the neural units here, click "Continue" to proceed to the next step. If you detect an
        issue, click "Cancel" to start over.**
        ''')
        unit_summaries: List[Dict[str, Any]] = state['units']
        initial_selection = unit_summaries[0]
        select_unit = dbc.Select(
            id='stage4_unit_select',
            options=[{'label': f"Unit {i+1}", 'value': str(i)} for i in range(len(unit_summaries))],
            value="0"
        )
        unit_div = html.Div(_SessionCommitter.stage4_display_unit(initial_selection), id="stage4_unit_div")
        return [markdown, select_unit, unit_div]

    @staticmethod
    def stage4_display_unit(unit_summary: Dict[str, Any]) -> List[Any]:
        spike_times = unit_summary['spike_times']
        template = unit_summary['template']
        for i in range(len(template)):
            template[i] = template[i] * 1000.0   # convert to micro-volts
        peak_to_peak = max(template) - min(template)

        badges = [
            dbc.Badge(f"Omniplex Channel: {unit_summary['channel_id']}", color="primary", className="mr-3"),
            dbc.Badge(f"Mean firing rate: {unit_summary['firing_rate']:.1f} Hz", color="primary", className="mr-3"),
            dbc.Badge(f"#Spikes: {len(spike_times)}", color="primary", className="mr-3"),
            dbc.Badge(f"SNR: {unit_summary['snr']:.2f}", color="primary", className="mr-3"),
            dbc.Badge(f"Peak-to-peak: {peak_to_peak:.1f} \u00B5V", color="primary", className="mr-3"),
        ]

        # simple graph of template waveform. Note I'm assuming 40KHz sampling rate here!
        graph = dcc.Graph(figure=px.line(x=[i/40.0 for i in range(len(template))], y=template,
                                         labels={'x': 'time (ms)', 'y': '\u00B5V'},
                                         title='Average spike waveform (1-ms pre, 9-ms post)'))

        return [html.Div(badges, className='mt-3 mb-1'), graph]

    @staticmethod
    def header(state: dict = None) -> str:
        stage = state['stage'] if state else 1
        return _SessionCommitter.__STAGE_HEADERS[stage]

    @staticmethod
    def body(state: dict = None) -> Any:
        if not state:
            state = {'stage': 1, 'experimenter': '', 'uuid': ''}
        if state['stage'] == 4:
            return _SessionCommitter.stage4_body(state)
        elif state['stage'] == 3:
            return _SessionCommitter.stage3_body(state)
        elif state['stage'] == 2:
            return _SessionCommitter.stage2_body(state)
        else:
            return _SessionCommitter.stage1_body()

    @staticmethod
    def footer(state: dict = None) -> List[dbc.Button]:
        if not state:
            state = {'stage': 1, 'experimenter': '', 'uuid': ''}
        if state['stage'] == 1:
            out = [dbc.Button("Submit", id="stage1_submit_btn", color='primary')]
        elif state['stage'] == 2:
            out = [dbc.Button("Continue", id="stage2_continue_btn", color='primary', className='mr-3', disabled=True),
                   dbc.Button("Cancel", id="stage2_cancel_btn", color='primary')]
        elif state['stage'] == 3:
            out = [dbc.Button("Continue", id="stage3_continue_btn", color='primary', className='mr-3'),
                   dbc.Button("Cancel", id="stage3_cancel_btn", color='primary')]
        else:
            out = [dbc.Button("Continue", id="stage4_continue_btn", color='primary', className='mr-3', disabled=True),
                   dbc.Button("Cancel", id="stage4_cancel_btn", color='primary')]
        return out

    def _callbacks(self):
        dash_app = self._app

        state_vector = [State('commit_state', 'data')]
        state_vector.extend([State(f"{attr.id}_input", "value") for attr in SessionView().attributes()])

        @dash_app.callback([Output('stage1_next_state', 'children'), Output('stage1_alert', 'is_open'),
                            Output('stage1_alert', 'children')],
                           [Input('stage1_submit_btn', 'n_clicks')], state_vector)
        def on_stage1_submit(next_btn, *args):
            if next_btn is None:
                raise dash.exceptions.PreventUpdate

            # if stored client_state indicates we're in a later stage, sync with server and switch to the correct stage
            # if confirmed.
            client_state = args[0] if isinstance(args[0], dict) else {'stage': 1, 'experimenter': '', 'uuid': ''}
            session_builder = SessionBuilder()
            corrected_state = session_builder.sync_client_state(client_state)
            if corrected_state:
                client_state = corrected_state
            if client_state['stage'] > 1:
                return json.dumps(client_state), dash.no_update, dash.no_update

            entry = dict()
            for i, attr in enumerate(SessionView().attributes()):
                entry[attr.id] = str(args[i + 1])
            try:
                client_state = session_builder.stage1_enter_session_info(client_state, entry)
            except SessionBuilderError as err:
                return dash.no_update, True, str(err)
            return json.dumps(client_state), dash.no_update, dash.no_update

        @dash_app.callback(
            [Output('stage2_alert', 'children'), Output('stage2_alert', 'is_open'),
             Output('stage2_check_progress', 'disabled'), Output('stage2_cancel_btn', 'disabled'),
             Output('stage2_continue_btn', 'disabled'), Output('uploader_container', 'style')],
            [Input('stage2_check_progress', 'n_intervals'), Input('session_archive_uploader', 'isCompleted'),
             Input('session_archive_uploader', 'fileNames')],
            [State('commit_state', 'data')]
        )
        def on_stage2_progress_check(n_intervals, is_completed, file_names, client_state):
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate

            out = [dash.no_update] * 6
            trigger = ctx.triggered[0]['prop_id'].split('.')[0]
            if (trigger.find('stage2_check_progress') > -1) and (n_intervals is not None):
                session_builder = SessionBuilder()
                try:
                    result, messages, server_state = session_builder.stage2_progress_update(client_state)
                except SessionBuilderError as err:
                    result, messages, server_state = None, [str(err)], client_state

                out[0] = messages[-1] if len(messages) > 0 else dash.no_update
                out[2] = (result is not None)
                out[3] = False
                out[4] = not result
            elif trigger.find('session_archive_uploader') > -1:
                if (not is_completed) and (file_names is not None):
                    out[3] = True
                elif is_completed:
                    out[0] = f"Upload complete: {file_names[0]}"
                    out[1] = True
                    out[2] = False
                    out[3] = False
                    out[5] = {'display': 'none'}
            return tuple(out)

        @dash_app.callback(Output('stage2_next_state', 'children'),
                           [Input('stage2_cancel_btn', 'n_clicks'), Input('stage2_continue_btn', 'n_clicks')],
                           [State('commit_state', 'data')])
        def on_stage2_transition(n_cancel, n_continue, client_state):
            session_builder = SessionBuilder()
            next_state = None
            if n_cancel is not None:
                session_builder.cancel(client_state)
                next_state = {'stage': 1, 'experimenter': '', 'uuid': ''}
            elif n_continue is not None:
                next_state = session_builder.stage2_next(client_state)

            return dash.no_update if (next_state is None) else json.dumps(next_state)

        @dash_app.callback(Output('stage3_protocol_div', 'children'), [Input('stage3_proto_select', 'value')],
                           [State('commit_state', 'data')])
        def on_stage3_proto_select(proto_key, state):
            if isinstance(proto_key, str) and ('protocols' in state) and (proto_key in state['protocols']):
                return _SessionCommitter.stage3_display_protocol(state['protocols'][proto_key])
            return dash.no_update

        @dash_app.callback(Output('stage3_next_state', 'children'),
                           [Input('stage3_cancel_btn', 'n_clicks'), Input('stage3_continue_btn', 'n_clicks')],
                           [State('commit_state', 'data')])
        def on_stage3_transition(n_cancel, n_continue, client_state):
            session_builder = SessionBuilder()
            next_state = None
            if n_cancel is not None:
                session_builder.cancel(client_state)
                next_state = {'stage': 1, 'experimenter': '', 'uuid': ''}
            elif n_continue is not None:
                next_state = SessionBuilder.stage3_next(client_state)
            return dash.no_update if (next_state is None) else json.dumps(next_state)

        @dash_app.callback(Output('stage4_unit_div', 'children'), [Input('stage4_unit_select', 'value')],
                           [State('commit_state', 'data')])
        def on_stage4_unit_select(value, state):
            unit_idx = int(value) if isinstance(value, str) else -1
            if ('units' in state) and (0 <= unit_idx < len(state['units'])):
                return _SessionCommitter.stage4_display_unit(state['units'][unit_idx])
            return dash.no_update

        @dash_app.callback(Output('stage4_next_state', 'children'), [Input('stage4_cancel_btn', 'n_clicks')],
                           [State('commit_state', 'data')])
        def on_stage4_cancel(n_cancel, client_state):
            next_state = dash.no_update
            if n_cancel is not None:
                session_builder = SessionBuilder()
                session_builder.cancel(client_state)
                next_state = json.dumps({'stage': 1, 'experimenter': '', 'uuid': ''})
            return next_state


__session_committer = _SessionCommitter(app)

layout = html.Div([
    dcc.Store(id="commit_state", storage_type='local'),
    html.Div("", id="stage1_next_state", style={"display": "none"}),
    html.Div("", id="stage2_next_state", style={"display": "none"}),
    html.Div("", id="stage3_next_state", style={"display": "none"}),
    html.Div("", id="stage4_next_state", style={"display": "none"}),
    dbc.Container([
        dbc.Row([dbc.Col(html.H3("Commit experiment sessions to the laboratory database", className="text-center"),
                className="mb-3 mt-3")]),
        dbc.Row([dbc.Col(html.H5(children='*** UNDER CONSTRUCTION ***'), className="mb-3")]),
        dbc.Card([
            dbc.CardHeader(__session_committer.header(), id="stage_hdr"),
            dbc.CardBody(__session_committer.body(), id="stage_body"),
            dbc.CardFooter(__session_committer.footer(), id="stage_footer")
        ])
    ])
])


@app.callback(Output('commit_state', 'data'),
              [Input(f"stage{i+1}_next_state", 'children') for i in range(4)])
def on_client_state_change(*args):
    ctx = dash.callback_context
    if not ctx.triggered:
        raise dash.exceptions.PreventUpdate

    btn_id = ctx.triggered[0]['prop_id'].split('.')[0]
    if btn_id.find('stage1') > -1:
        client_state = json.loads(args[0])
    elif btn_id.find('stage2') > -1:
        client_state = json.loads(args[1])
    elif btn_id.find('stage3') > -1:
        client_state = json.loads(args[2])
    elif btn_id.find('stage4') > -1:
        client_state = json.loads(args[3])
    else:
        client_state = dash.no_update
    return client_state


# NOTE: We monitor the 'modified_timestamp' on dcc.Store rather than 'data' in order to get the contents of the
# store at initial load. This is due to a limitation in Dash.
@app.callback([Output('stage_hdr', 'children'), Output('stage_body', 'children'), Output('stage_footer', 'children')],
              [Input('commit_state', 'modified_timestamp')], [State('commit_state', 'data')])
def update_layout_on_client_state_change(ts, state):
    if ts is None:
        raise dash.exceptions.PreventUpdate

    # the store will contain None initially -- so we initialize it if necessary
    if not isinstance(state, dict):
        state = {'stage': 1, 'experimenter': '', 'uuid': ''}
    session_builder = SessionBuilder()
    state = session_builder.sync_client_state(state)

    return __session_committer.header(state), __session_committer.body(state), __session_committer.footer(state)
