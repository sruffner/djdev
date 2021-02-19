"""
commit_session.py: The "commit session" page in web-based interface to the Lisberger laboratory database.

This web page governs a step-by-step, stateful process by which the user commits an experiment session's worth of data
to the laboratory database.

Details of the session commit process are outlined in the session_builder.py module, which implements the server-side
functionality of a session commit. This Dash web page implements the client-side functionality.

The page layout essentially consists of a single Dash Bootstrap Card element in which the header, body and footer of the
card are updated for each stage in the commit process. The _SessionCommitter class implements the per-stage card
content.

Since committing an experiment session to the lab database requires a multi-stage interaction between client and server
and will take an indeterminate amount of time to complete on the server side, the server assigns a unique "task
identifier" when the commit task initiated. Once a task is initiated, the client must supply this task ID in each
server request to check task progress, retrieve intermediate results, or supply additional information required for the
commit. A Dash Store element in the page layout is used to preserve this task ID, along with current stage index, as a
simple dictionary: {'stage': int, 'task_id': str}. In stage 1 the task ID is undefined and will be set to an empty
string. In stages 2-4, the task ID is that assigned by the server after the session information is successfully
submitted in stage 1. Since the Store element uses 'local' browser memory, the dictionary is preserved even if the
browser tab is closed or the browser itself shutdown. That way, if the user returns to the session commit page later,
the client can query the server for the current state of the commit task and refresh the page content accordingly.

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
import uuid

from database import maestro
from database.session_builder import SessionBuilder, OmniplexUnit
from database.table_views import SessionView, SessionEPhysView, NeuronTypeView
from pages.curate_panels import entry_form
from typing import Any, List


def _create_dash_upload_component(
        component_id='dash-uploader',
        text='Drag and Drop Here to upload!',
        text_completed='Uploaded: ',
        cancel_button=True,
        pause_button=False,
        filetypes=None,
        max_file_size=1024,
        chunk_size=1,
        default_style=None,
        upload_id=None,
        max_files=1,
):
    """
    This is a revision of the Upload() function in dash-uploader to allow specification of the file upload chunk size
    in MB. The default value of 1MB is just too small for giga-byte file uploads. In addition, knitting together all of
    the chunks on the server will take too long when you have thousands of 1MB chunks. Use the 'chunk_size' parameter
    to specify the chunk size in MB; it will be range-restricted to [1..100]
    """
    # limit allowed range for chunk_size
    chunk_size = min(max(1, chunk_size), 100)

    # Handle styling
    default_style = du.upload.combine(default_style, du.upload.DEFAULT_STYLE)
    upload_style = du.upload.combine({'lineHeight': '0px'}, default_style)

    if upload_id is None:
        upload_id = uuid.uuid1()

    service = du.upload.update_upload_api(du.upload.settings.requests_pathname_prefix,
                                          du.upload.settings.upload_api)

    arguments = dict(
        id=component_id,
        # Have not tested if using many files
        # is reliable -> Do not allow
        maxFiles=max_files,
        maxFileSize=max_file_size * 1024 * 1024,
        chunkSize=chunk_size * 1024 * 1024,
        textLabel=text,
        service=service,
        startButton=False,
        # Not tested so default to one.
        simultaneousUploads=1,
        completedMessage=text_completed,
        cancelButton=cancel_button,
        pauseButton=pause_button,
        defaultStyle=default_style,
        uploadingStyle=upload_style,
        completeStyle=default_style,
        upload_id=str(upload_id),
    )

    if filetypes:
        arguments['filetypes'] = filetypes

    return du.Upload_ReactComponent(**arguments)


class _SessionCommitter:
    def __init__(self, dash_app: dash.Dash):
        self._app = dash_app
        self._callbacks()

    @staticmethod
    def stage1_body() -> Any:
        markdown = dcc.Markdown('''
        * Before you begin, all session data files (Maestro and Omniplex) must be compressed into a single, flat ZIP
        archive (no subdirectories). Maximum supported file size is 10GB.
        * If the session includes behavioral data only, the archive should contain only the Maestro data files.
        * There is no support at this time for automatic spike sorting. If the experiment includes electrophysiological 
        recordings, the experimenter must supply neural unit data (spike trains) in a pickle file (.pkl or .pickle). 
        This must be the only pickle file in the archive.
        * The pickle file must contain a single dictionary: {'filename': [...], 'channel': [...], 'spiketimes': [...]},
        where each value is a list of length N, where N is the number of neural units. These contain the Omniplex PL2
        filenames, the source channel IDs ('WBnn' or 'SPKCnn'), and the spike timestamps (in seconds since the Omniplex
        recording started) for each neural unit. The 'filename' field may be omitted if all units were recorded in a
        single Omniplex file.
        * You will upload the ZIP archive in the next step, after which the archive is pre-processed on the server. You
        will then review the results and edit the session metadata before committing the session to the database.
        * The upload, pre-processing and final commit stages may take a considerable amount of time. Regular progress
        messages are displayed, and you will have the option to "Cancel" the commit entirely.
        ''')
        return [markdown]

    @staticmethod
    def stage2_body(substage: int, task_id: str) -> Any:
        # there are 3 substages in stage 2: 0 = waiting, 1 = uploading, 2 = preprocessing. If upload has already
        # started or finished, we need to hide the uploader and show the alert and enable progress updates. NOTE that
        # if the user closes the page in the middle of the upload, it will not resume...
        upload_started = substage > 0

        markdown = dcc.Markdown('''
        
        *Drag and drop the ZIP file onto the upload component below, or click on the component to browse the file
        system for the file. The upload should start automatically. **Do NOT close browser tab while upload is in
        progress. Large (>1GB) will take a significant amount of time to upload, depending on network speed**.*
        
        ''')
        uploader = _create_dash_upload_component(
            component_id="session_archive_uploader", max_file_size=10000, chunk_size=100, max_files=1,
            cancel_button=False, filetypes=['zip'], upload_id=f"{task_id}")
        upload_div = html.Div(uploader, id="uploader_container", className="mb-3",
                              style={'display': 'none'} if upload_started else {})
        intv_check = dcc.Interval(id="stage2_check_progress", disabled=not upload_started, interval=1000)
        alert = dbc.Alert(id="stage2_alert", color="info", is_open=upload_started)
        return [markdown, upload_div, intv_check, alert]

    @staticmethod
    def stage3_body(task_id: str) -> Any:
        session_builder = SessionBuilder()
        session_info = session_builder.get_session_info(task_id)
        session_info_tab_content = dbc.Card(
            dbc.CardBody(entry_form(SessionView(), None, session_info, None)), className="mt-3"
        )

        proto_map = session_builder.get_trial_protocol_paths(task_id)
        first_key = next(iter(proto_map.keys()))
        initial_protocol: maestro.Protocol = session_builder.get_trial_protocol(task_id, first_key)
        select_protocol = dbc.Select(
            id='stage3_proto_select',
            options=[{'label': v, 'value': k} for k, v in proto_map.items()],
            value=first_key
        )
        protocol_div = html.Div(_SessionCommitter.stage3_display_protocol(initial_protocol), id="stage3_protocol_div")
        proto_tab_content = dbc.Card(dbc.CardBody([select_protocol, protocol_div]), className="mt-3")

        ephys_info = session_builder.get_ephys_info(task_id)
        if ephys_info:
            ephys_view = SessionEPhysView()
            omit_attrs = ephys_view.attributes_in_master()
            ephys_info_tab_content = dbc.Card(
                dbc.CardBody(entry_form(ephys_view, omit_attrs, ephys_info, None)), className="mt-3"
            )
        else:
            ephys_info_tab_content = dbc.Card([], className="mt-3")

        num_units = session_builder.get_num_neural_units(task_id)
        if num_units is None:
            num_units = 0
        first_unit = None if num_units == 0 else session_builder.get_neural_unit_metrics(task_id, 0)
        select_unit = dbc.Select(
            id='stage3_unit_select',
            options=[{'label': f"Unit {i + 1}", 'value': str(i)} for i in range(num_units)],
            value="0" if num_units > 0 else []
        )
        unit_div = html.Div([] if num_units == 0 else _SessionCommitter.stage3_display_unit(first_unit),
                            id="stage3_unit_div")
        unit_tab_content = dbc.Card(dbc.CardBody([select_unit, unit_div]), className="mt-3")

        tabs = dbc.Tabs(
            [
                dbc.Tab(session_info_tab_content, label="Session Information"),
                dbc.Tab(proto_tab_content, label="Trial Protocols"),
                dbc.Tab(ephys_info_tab_content, label="EPhys Recording", disabled=(ephys_info is None)),
                dbc.Tab(unit_tab_content, label="Neural Units", disabled=(num_units == 0))
            ]
        )
        return tabs

    @staticmethod
    def stage3_display_protocol(protocol: maestro.Protocol) -> List[Any]:
        proto_summary = protocol.summary()
        segments = proto_summary['segments']
        targets = proto_summary['targets']
        target_names = [target_desc.split(':')[0] for target_desc in targets]  # THIS IS A HACK
        perts = proto_summary['perts']
        sections = proto_summary['sections']
        diffs = proto_summary['diffs']

        badges = [
            dbc.Badge(f"Record Seg: {proto_summary['record_seg']}", color="primary", className="mr-3"),
            dbc.Badge(f"Transform: {proto_summary['transform']}", color="primary", className="mr-3"),
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
    def stage3_display_unit(unit: OmniplexUnit) -> List[Any]:
        # dropdown lets user assign neuron type to the unit
        neuron_types = NeuronTypeView().rows()
        initial_selection = str(unit.neuron_type) if unit.neuron_type in [nt['nt_id'] for nt in neuron_types] else None
        select_type = dbc.InputGroup(
            [
                dbc.InputGroupAddon("Neuron Type", addon_type="prepend"),
                dbc.Select(
                    id='stage3_neuron_type_select',
                    options=[{'label': nt['nt_name'], 'value': str(nt['nt_id'])} for nt in neuron_types],
                    value=initial_selection
                )
            ], className='mb-3')
        header_kids = [html.Hr(), select_type]

        peak_to_peak = max(unit.template) - min(unit.template)
        header_kids.extend([
            dbc.Badge(f"Omniplex Channel: {unit.channel}", color="primary", className="mr-3"),
            dbc.Badge(f"Mean firing rate: {unit.firing_rate:.1f} Hz", color="primary", className="mr-3"),
            dbc.Badge(f"#Spikes: {len(unit.spike_times)}", color="primary", className="mr-3"),
            dbc.Badge(f"SNR: {unit.snr:.2f}", color="primary", className="mr-3"),
            dbc.Badge(f"Peak-to-peak: {peak_to_peak:.1f} \u00B5V", color="primary", className="mr-3"),
        ])

        # simple graph of template waveform. Note I'm assuming 40KHz sampling rate here!
        graph = dcc.Graph(figure=px.line(x=[i/40.0 for i in range(len(unit.template))], y=unit.template,
                                         labels={'x': 'time (ms)', 'y': '\u00B5V'},
                                         title='Average spike waveform (1-ms pre, 9-ms post)'))

        return [html.Div(header_kids, className='mt-3 mb-1'), graph]

    __STAGE_HEADERS = {
        1: 'Step 1: Prepare session data archive',
        2: 'Step 2: Upload session data archive and pre-process',
        3: 'Step 3: Review and confirm',
        4: 'Step 4: Commit session to database'
    }

    @staticmethod
    def header(stage: int) -> str:
        return _SessionCommitter.__STAGE_HEADERS[stage]

    @staticmethod
    def body(stage: int, substage: int, task_id: str) -> Any:
        if stage == 3:
            return _SessionCommitter.stage3_body(task_id)
        elif stage == 2:
            return _SessionCommitter.stage2_body(substage, task_id)
        else:
            return _SessionCommitter.stage1_body()

    @staticmethod
    def footer(stage: int) -> List[dbc.Button]:
        if stage == 3:
            out = [dbc.Button("Continue", id="stage3_continue_btn", color='primary', className='mr-3', disabled=True),
                   dbc.Button("Cancel", id="stage3_cancel_btn", color='primary')]
        elif stage == 2:
            out = [dbc.Button("Continue", id="stage2_continue_btn", color='primary', className='mr-3', disabled=True),
                   dbc.Button("Cancel", id="stage2_cancel_btn", color='primary')]
        else:
            out = [dbc.Button("Start", id="stage1_continue_btn", color='primary')]

        return out

    def _callbacks(self):
        dash_app = self._app

        @dash_app.callback(Output('stage1_next_state', 'children'),
                           [Input('stage1_continue_btn', 'n_clicks')], [State('commit_state', 'data')])
        def on_stage1_submit(start_btn, state):
            if start_btn is None:
                raise dash.exceptions.PreventUpdate

            session_builder = SessionBuilder()

            # check to see if a commit task ID is in the local store. If so, then we should not be in stage 1. Sync
            # with server and switch to the correct stage.
            client_state = state if isinstance(state, dict) else {'stage': 1, 'task_id': ""}
            if client_state['stage'] > 1:
                stage, _ = session_builder.get_commit_task_stage(client_state['task_id'])
                if stage > 1:
                    client_state['stage'] = stage
                    return json.dumps(client_state), dash.no_update, dash.no_update
                else:
                    client_state['stage'] = 1
                    client_state['task_id'] = ""

            ok, task_id_or_err = session_builder.initiate_session_commit()
            if ok:
                client_state = {'stage': 2, 'task_id': task_id_or_err}
                return json.dumps(client_state)
            else:
                return dash.no_update

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
                stage, message, result = session_builder.progress_update(client_state['task_id'])
                if stage == 1:
                    out[0] = "Client out of sync; please cancel and try again"
                    out[2] = True
                    out[3] = False
                    out[4] = True
                else:
                    out[0] = message
                    out[2] = (stage > 2) or (result is False)
                    out[3] = False
                    out[4] = (stage == 2)
            elif trigger.find('session_archive_uploader') > -1:
                if (not is_completed) and (file_names is not None):
                    out[3] = True
                elif is_completed:
                    out[0] = "Checking progress..."  # this message will be replaced shortly by first progress update
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
            task_id = client_state['task_id']
            if n_cancel is not None:
                session_builder.cancel(task_id)
                next_state = {'stage': 1, 'task_id': ""}
            elif n_continue is not None and (3 == session_builder.get_commit_task_stage(task_id)[0]):
                next_state = {'stage': 3, 'task_id': task_id}
            return dash.no_update if (next_state is None) else json.dumps(next_state)

        @dash_app.callback(Output('stage3_protocol_div', 'children'), [Input('stage3_proto_select', 'value')],
                           [State('commit_state', 'data')])
        def on_stage3_proto_select(proto_key, client_state):
            session_builder = SessionBuilder()
            task_id = client_state['task_id']
            protocol = session_builder.get_trial_protocol(task_id, proto_key)
            if protocol:
                return _SessionCommitter.stage3_display_protocol(protocol)
            return dash.no_update

        @dash_app.callback(Output('stage3_unit_div', 'children'), [Input('stage3_unit_select', 'value')],
                           [State('commit_state', 'data')])
        def on_stage3_unit_select(value, client_state):
            unit_idx = int(value) if isinstance(value, str) else -1
            session_builder = SessionBuilder()
            task_id = client_state['task_id']
            unit = session_builder.get_neural_unit_metrics(task_id, unit_idx)
            if unit:
                return _SessionCommitter.stage3_display_unit(unit)
            return dash.no_update

        # note that we never update the output here, but the current unit's neuron type is updated on server side
        @dash_app.callback(Output('stage3_unit_select', 'options'), [Input('stage3_neuron_type_select', 'value')],
                           [State('stage3_unit_select', 'value'), State('commit_state', 'data')])
        def on_stage3_neuron_type_select(type_str, unit_idx_str, client_state):
            unit_idx = int(unit_idx_str) if isinstance(unit_idx_str, str) else -1
            session_builder = SessionBuilder()
            task_id = client_state['task_id']
            session_builder.set_neural_unit_type(task_id, unit_idx, int(type_str))
            return dash.no_update

        @dash_app.callback(Output('stage3_next_state', 'children'),
                           [Input('stage3_cancel_btn', 'n_clicks'), Input('stage3_continue_btn', 'n_clicks')],
                           [State('commit_state', 'data')])
        def on_stage3_transition(n_cancel, n_continue, client_state):
            session_builder = SessionBuilder()
            next_state = None
            task_id = client_state['task_id']
            if n_cancel is not None:
                session_builder.cancel(task_id)
                next_state = {'stage': 1, 'task_id': ""}
            elif n_continue is not None:
                next_state = None  # TODO: Transition to stage 4 once it's implemented
            return dash.no_update if (next_state is None) else json.dumps(next_state)


__session_committer = _SessionCommitter(app)

layout = html.Div([
    dcc.Store(id="commit_state", storage_type='local'),
    html.Div("", id="stage1_next_state", style={"display": "none"}),
    html.Div("", id="stage2_next_state", style={"display": "none"}),
    html.Div("", id="stage3_next_state", style={"display": "none"}),
    dbc.Container([
        dbc.Row([dbc.Col(html.H3("Commit experiment sessions to the laboratory database", className="text-center"),
                className="mb-3 mt-3")]),
        dbc.Row([dbc.Col(html.H5(children='*** UNDER CONSTRUCTION ***'), className="mb-3")]),
        dbc.Card([
            dbc.CardHeader(__session_committer.header(1), id="stage_hdr"),
            dbc.CardBody(__session_committer.body(1, 0, ""), id="stage_body"),
            dbc.CardFooter(__session_committer.footer(1), id="stage_footer")
        ])
    ])
])


@app.callback(Output('commit_state', 'data'),
              [Input(f"stage{i+1}_next_state", 'children') for i in range(3)])
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
    else:
        client_state = dash.no_update
    return client_state


# NOTE: We monitor the 'modified_timestamp' on dcc.Store rather than 'data' in order to get the contents of the
# store at initial load. This is due to a limitation in Dash.
@app.callback([Output('stage_hdr', 'children'), Output('stage_body', 'children'), Output('stage_footer', 'children')],
              [Input('commit_state', 'modified_timestamp')], [State('commit_state', 'data')])
def update_layout_on_client_state_change(ts, client_state):
    if ts is None:
        raise dash.exceptions.PreventUpdate

    # if there is no state dictionary in the store, then we're in stage 1. Else, sync with the server. Also need to
    # be careful that the stored client state has the two keys 'stage' and 'task_id'
    stage, substage, task_id = (1, 0, "")
    if client_state and ('stage' in client_state) and ('task_id' in client_state):
        stage, task_id = (client_state['stage'], client_state['task_id'])
    if len(task_id) > 0:
        stage, substage = SessionBuilder().get_commit_task_stage(task_id)
        if stage == 1:
            task_id = ""
    return __session_committer.header(stage), __session_committer.body(stage, substage, task_id), \
        __session_committer.footer(stage)
