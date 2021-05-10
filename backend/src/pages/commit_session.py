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
import dash_uploader as du
from dash.dependencies import Input, Output, State
import plotly.express as px
from app import app
import json
import uuid

from database import maestro
from database.manager import DataBaseManager, OmniplexUnit
import database.table_info as ti
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
        session_builder = DataBaseManager()
        session_info = session_builder.get_session_info(task_id)
        entry_form = session_builder.entry_form(ti.DBTable.SESSION, None, session_info, None)
        session_info_tab_content = dbc.Card(dbc.CardBody(entry_form), className="mt-3")

        proto_names = session_builder.get_protocol_candidate_names(task_id)
        select_proto = dbc.Select(
            id='stage3_proto_select',
            options=[{'label': name, 'value': str(i)} for i, name in enumerate(proto_names)],
            value=str(0)
        )
        n_unvalidated = session_builder.num_protocol_candidates_needing_validation(task_id)
        proto_alert = dbc.Alert(f"{n_unvalidated} trial protocols require user review and validation! These are "
                                f"marked by '**' in the dropdown menu below.",
                                id="stage3_proto_alert", color='danger', is_open=(n_unvalidated > 0))
        initial_proto = session_builder.get_protocol_candidate(task_id, 0)
        proto_div = html.Div(_SessionCommitter.stage3_display_protocol(initial_proto), id="stage3_protocol_div")
        proto_tab_content = dbc.Card(dbc.CardBody([proto_alert, select_proto, proto_div]), className="mt-3")

        # note: this tab will be disabled if session does not include neural units recordings
        ephys_info = session_builder.get_ephys_info(task_id)
        ephys_form = session_builder.entry_form(ti.DBTable.SESSION_EPHYS, None, ephys_info, None)
        ephys_info_tab_content = dbc.Card(dbc.CardBody(ephys_form), className="mt-3")

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

        # displays error message if user-entered session or ephys metadata is invalid
        alert = dbc.Alert(id="stage3_alert", color="info", is_open=False, className="mt-3")
        return [tabs, alert]

    @staticmethod
    def stage3_display_protocol(proto_candidate: maestro.ProtocolCandidate) -> List[Any]:
        protocol = maestro.Protocol.from_candidate(proto_candidate)
        needs_validation = (proto_candidate.num_reps == 1) or \
                           (proto_candidate.num_reps == 2 and not proto_candidate.matches_existing)
        needs_validation = needs_validation and not proto_candidate.user_validated
        valid_btn = dbc.Button("Validate" if needs_validation else "\u2713 Validated", id='stage3_proto_validate',
                               color='primary', disabled=(not needs_validation), size='sm')
        tool_tip = dbc.Tooltip(
            "Any trial protocol based on fewer than 3 reps and not matching an existing protocol in the database must "
            "be manually verified by the user. Add any missing random variables (eg, a random-duration fixation "
            "segment) to the definition (if any), then press this button to validate the protocol.",
            target='stage3_proto_validate')
        reps_badge = dbc.Badge(
            f"# reps = {proto_candidate.num_reps} "
            f"{'; found match' if proto_candidate.matches_existing else ''}",
            color='info', className='ml-2 mr-5'
        )
        add_rv_btn = dbc.Button("Add Random Var:", id='stage3_add_rv', color='primary', size='sm', className='mr-2')
        n_segs = len(proto_candidate.trial.segments)
        n_tgts = len(proto_candidate.trial.targets)
        select_rv_type = dbc.InputGroup([
            dbc.InputGroupAddon("Type", addon_type='prepend'),
            dbc.Select(
                id='stage3_rv_type_select',
                options=[{'label': t.name, 'value': str(t.value)} for t in maestro.SegParamType if
                         t.can_vary_randomly()],
                value=str(maestro.SegParamType.DURATION.value)
            )
        ], size='sm', className='mr-2')
        seg_select = dbc.InputGroup([
            dbc.InputGroupAddon("Segment", addon_type='prepend'),
            dbc.Select(
                id='stage3_rv_seg_select',
                options=[{'label': str(i), 'value': str(i)} for i in range(n_segs)],
                value='0'
            )
        ], size='sm', className='mr-2')
        tgt_select = dbc.InputGroup([
            dbc.InputGroupAddon("Target", addon_type='prepend'),
            dbc.Select(
                id='stage3_rv_tgt_select',
                options=[{'label': proto_candidate.trial.targets[i].name, 'value': str(i)} for i in range(n_tgts)],
                value='0'
            )
        ], size='sm')
        validate_form = dbc.Form([
            valid_btn, reps_badge, tool_tip,
            dbc.FormGroup([add_rv_btn, select_rv_type, seg_select, tgt_select], id='stage3_rv_group',
                          style={} if needs_validation else {'display': 'none'})
        ], inline=True, className='mt-3')

        cmpt_list = protocol.display_definition()
        cmpt_list.insert(0, validate_form)
        return cmpt_list

    @staticmethod
    def stage3_display_unit(unit: OmniplexUnit) -> List[Any]:
        # dropdown lets user assign neuron type to the unit
        neuron_types = DataBaseManager().fetch_rows(ti.DBTable.NEURON_TYPE)
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

    @staticmethod
    def stage4_body() -> Any:
        markdown = dcc.Markdown('''

        *PLEASE WAIT while session data is inserted into the laboratory database. Depending on the size of the data
        archive and server traffic, this could take many minutes...*

        ''')
        alert = dbc.Alert("Checking progress on server...", id="stage4_alert", color="info", is_open=True)
        intv_check = dcc.Interval(id="stage4_check_progress", disabled=False, interval=1000)
        return [markdown, alert, intv_check]

    __STAGE_HEADERS = {
        1: 'Step 1: Prepare session data archive',
        2: 'Step 2: Upload session data archive and pre-process',
        3: 'Step 3: Review and confirm',
        4: 'Step 4: Committing session to database'
    }

    @staticmethod
    def header(stage: int) -> str:
        return _SessionCommitter.__STAGE_HEADERS[stage]

    @staticmethod
    def body(stage: int, substage: int, task_id: str) -> Any:
        if stage == 4:
            return _SessionCommitter.stage4_body()
        if stage == 3:
            return _SessionCommitter.stage3_body(task_id)
        elif stage == 2:
            return _SessionCommitter.stage2_body(substage, task_id)
        else:
            return _SessionCommitter.stage1_body()

    @staticmethod
    def footer(stage: int) -> List[dbc.Button]:
        if stage == 4:
            out = [dbc.Button("Cancel", id="stage4_cancel_btn", color='primary')]
        elif stage == 3:
            out = [dbc.Button("Finish", id="stage3_continue_btn", color='primary', className='mr-3'),
                   dbc.Button("Cancel", id="stage3_cancel_btn", color='primary')]
        elif stage == 2:
            out = [dbc.Button("Next", id="stage2_continue_btn", color='primary', className='mr-3', disabled=True),
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

            session_builder = DataBaseManager()

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
                session_builder = DataBaseManager()
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
            session_builder = DataBaseManager()
            next_state = None
            task_id = client_state['task_id']
            if n_cancel is not None:
                session_builder.cancel(task_id)
                next_state = {'stage': 1, 'task_id': ""}
            elif n_continue is not None and (3 == session_builder.get_commit_task_stage(task_id)[0]):
                next_state = {'stage': 3, 'task_id': task_id}
            return dash.no_update if (next_state is None) else json.dumps(next_state)

        @dash_app.callback(Output('stage3_protocol_div', 'children'),
                           [Input('stage3_proto_select', 'value'), Input('stage3_add_rv', 'n_clicks')],
                           [State('commit_state', 'data'), State('stage3_proto_select', 'value'),
                            State('stage3_rv_type_select', 'value'),
                            State('stage3_rv_seg_select', 'value'), State('stage3_rv_tgt_select', 'value')])
        def on_stage3_update_proto(*args):
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate
            task_id = args[2]['task_id']
            session_builder = DataBaseManager()
            trigger = ctx.triggered[0]['prop_id'].split('.')[0]
            if trigger == 'stage3_proto_select':
                proto_candidate = session_builder.get_protocol_candidate(task_id, int(args[0]))
                if proto_candidate:
                    return _SessionCommitter.stage3_display_protocol(proto_candidate)
            elif trigger == 'stage3_add_rv':
                proto_index = int(args[3])
                # noinspection PyArgumentList
                rv: maestro.SegParam = maestro.SegParam(
                    maestro.SegParamType(int(args[4])), int(args[5]), int(args[6]))
                proto_candidate = session_builder.add_random_var_to_protocol_candidate(task_id, proto_index, rv)
                if proto_candidate:
                    return _SessionCommitter.stage3_display_protocol(proto_candidate)
            return dash.no_update

        @dash_app.callback([Output('stage3_proto_validate', 'children'), Output('stage3_proto_validate', 'disabled'),
                            Output('stage3_rv_group', 'style'), Output('stage3_proto_alert', 'children'),
                            Output('stage3_proto_alert', 'is_open'), Output('stage3_proto_select', 'options')],
                           [Input('stage3_proto_validate', 'n_clicks')],
                           [State('commit_state', 'data'), State('stage3_proto_select', 'value')])
        def on_stage3_validate_proto(*args):
            if args[0] is None:
                raise dash.exceptions.PreventUpdate
            session_builder = DataBaseManager()
            task_id = args[1]['task_id']
            if not session_builder.validate_protocol_candidate(task_id, int(args[2])):
                raise dash.exceptions.PreventUpdate
            options = [{'label': name, 'value': str(i)} for i, name in
                       enumerate(session_builder.get_protocol_candidate_names(task_id))]
            n_unvalidated = session_builder.num_protocol_candidates_needing_validation(task_id)
            alert_msg = f"{n_unvalidated} trial protocols require user review and validation! These are " \
                        f"marked by '**' in the dropdown menu below."
            return "\u2713 Validated", True, {'display': 'none'}, alert_msg, (n_unvalidated > 0), options

        @dash_app.callback(Output('stage3_unit_div', 'children'), [Input('stage3_unit_select', 'value')],
                           [State('commit_state', 'data')])
        def on_stage3_unit_select(value, client_state):
            unit_idx = int(value) if isinstance(value, str) else -1
            session_builder = DataBaseManager()
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
            nt_id = int(type_str) if isinstance(type_str, str) else -1
            if (unit_idx > -1) and (nt_id > -1):
                session_builder = DataBaseManager()
                task_id = client_state['task_id']
                session_builder.set_neural_unit_type(task_id, unit_idx, nt_id)
            return dash.no_update

        state_vector = [State(f"{attr_id}_input", "value") for attr_id in ti.attributes_of(ti.DBTable.SESSION)]
        state_vector.extend([State(f"{attr_id}_input", "value")
                             for attr_id in ti.attributes_of(ti.DBTable.SESSION_EPHYS)])
        state_vector.append(State('commit_state', 'data'))

        @dash_app.callback([Output('stage3_next_state', 'children'), Output('stage3_alert', 'children'),
                            Output('stage3_alert', 'is_open')],
                           [Input('stage3_cancel_btn', 'n_clicks'), Input('stage3_continue_btn', 'n_clicks')],
                           state_vector)
        def on_stage3_transition(n_cancel, n_continue, *args):
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate

            session_builder = DataBaseManager()
            client_state = args[-1]
            task_id = client_state['task_id']
            if n_cancel is not None:
                session_builder.cancel(task_id)
                return json.dumps({'stage': 1, 'task_id': ""}), dash.no_update, dash.no_update
            elif n_continue is not None:
                session_info = dict()
                ephys_info = None
                for i, attr_id in enumerate(ti.attributes_of(ti.DBTable.SESSION)):
                    session_info[attr_id] = args[i]
                if session_builder.get_ephys_info(task_id) is not None:
                    ofs = len(session_info.items())
                    ephys_info = dict()
                    for i, attr_id in enumerate(ti.attributes_of(ti.DBTable.SESSION_EPHYS)):
                        ephys_info[attr_id] = args[ofs+i]

                # FIX to handle BUG in dbc.Select: the 'value' will be a string if the user changes the selection from
                # the initial value, even though I use integers for the option values. This leads to downstream issue
                # when the DataBaseManager checks the foreign key attributes in session_info and ephys_info. So here
                # I force them to int:
                try:
                    session_info['study_id'] = int(session_info['study_id'])
                    if ephys_info:
                        ephys_info['ba_id'] = int(ephys_info['ba_id'])
                except Exception:
                    pass

                error_msg = session_builder.start_commit(task_id, session_info, ephys_info)
                if not error_msg:
                    return json.dumps({'stage': 4, 'task_id': task_id}), dash.no_update, dash.no_update
                else:
                    return dash.no_update, error_msg, True
            else:
                return dash.no_update, dash.no_update, dash.no_update

        @dash_app.callback([Output('stage4_next_state', 'children'), Output('stage4_alert', 'children'),
                            Output('stage4_cancel_btn', 'children')],
                           [Input('stage4_cancel_btn', 'n_clicks'), Input('stage4_check_progress', 'n_intervals')],
                           [State('commit_state', 'data')])
        def on_stage4_update(n_cancel, n_intervals, client_state):
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate

            session_builder = DataBaseManager()
            task_id = client_state['task_id']
            trigger = ctx.triggered[0]['prop_id'].split('.')[0]
            if n_cancel is not None:
                session_builder.cancel(task_id)
                return json.dumps({'stage': 1, 'task_id': ""}), dash.no_update, dash.no_update
            elif (trigger.find('stage4_check_progress') > -1) and (n_intervals is not None):
                stage, message, result = session_builder.progress_update(task_id)
                if stage == 4:
                    return dash.no_update, message, "Done" if result is True else dash.no_update
                else:
                    return json.dumps({'stage': 1, 'task_id': ""}), dash.no_update, dash.no_update
            else:
                return dash.no_update, dash.no_update, dash.no_update


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
            dbc.CardHeader(__session_committer.header(1), id="stage_hdr"),
            dbc.CardBody(__session_committer.body(1, 0, ""), id="stage_body"),
            dbc.CardFooter(__session_committer.footer(1), id="stage_footer")
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
def update_layout_on_client_state_change(ts, client_state):
    if ts is None:
        raise dash.exceptions.PreventUpdate

    # if there is no state dictionary in the store, then we're in stage 1. Else, sync with the server. Also need to
    # be careful that the stored client state has the two keys 'stage' and 'task_id'
    stage, substage, task_id = (1, 0, "")
    if client_state and ('stage' in client_state) and ('task_id' in client_state):
        stage, task_id = (client_state['stage'], client_state['task_id'])
    if len(task_id) > 0:
        stage, substage = DataBaseManager().get_commit_task_stage(task_id)
        if stage == 1:
            task_id = ""
    return __session_committer.header(stage), __session_committer.body(stage, substage, task_id), \
        __session_committer.footer(stage)
