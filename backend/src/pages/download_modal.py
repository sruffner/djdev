"""
download_modal.py: Implementation of a modal window in which the user can download data from an experiment session.

    A "helper module" for the /explore endpoint (explore.py), this module implements the layout and callbacks for a
Bootstrap Modal component by which the user submits a request to download data from a selected experiment session,
monitor progress while the data file is prepared by a background process on the server, and finally download the
generated file. The button that raises the window is rendered on the "Trial Data" tab pane for the currently selected
experiment session on the /explore page.

@author: sruffner
@created: 23feb2022
"""
import json
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import flask_login
from dash import html, dcc, callback, Output, Input, callback_context, State, no_update
import dash_bootstrap_components as dbc

from config.app_logging import get_application_logger
from database.download_ops import MAX_UNITS_PER_DOWNLOAD, DOWNLOAD_FORMATS, request_data_download, \
    pending_download_request_status, DOWNLOAD_PREPPING, DOWNLOAD_READY, cancel_pending_download_request
from database.table_info import AttributeValue, primary_key_of, DBTable
from database.table_ops import fetch_restrict_proj, fetch_rows


_DOWNLOAD_MODAL_OPEN_ID: str = 'td_download_btn'
""" 
ID of button that raises the Bootstrap Modal component to request a data download. This button is rendered
on the 'Trial Data' tab pane at the /explore endpoint.
"""
_DOWNLOAD_MODAL_ID: str = 'td_download_modal'
""" ID of Bootstrap Modal component on which a dataset download is requested. """
_DOWNLOAD_MODAL_BODY_ID: str = 'td_download_modal_content'
""" ID of the Bootstrap ModalBody on which a dataset download is requested. """
_DOWNLOAD_MODAL_CLOSE_ID: str = 'td_download_modal_close'
""" ID of button that extinguishes the Bootstrap Modal component on which a dataset download is requested. """

_DOWNLOAD_SESSION_KEY_DIV: str = 'down_session'
""" ID of hidden DIV holding primary key (as Pickle string) of the experiment session from which data is downloaded. """
_DOWNLOAD_UNIT_DROP_ID: str = 'down_unit_dropdown'
""" ID of Dash Dropdown component by which user selects up to 10 neural units to include in download. """
_DOWNLOAD_OK_TRIALS_CHK: str = 'down_ok_trials_chk'
""" ID of Bootstrap Checkbox to restrict download to only successfully completed trials. """
_DOWNLOAD_RMVSACC_CHK: str = 'down_rmvsacc_chk'
""" ID of Bootstrap Checkbox to remove saccades from all eye velocity responses prior to download. """
_DOWNLOAD_FMT_RADIO: str = 'down_format_radio'
""" ID of Bootstrap RadioItems group to select one of several download file formats. """
_DOWNLOAD_ACTION: str = 'down_action_btn'
""" 
ID of action button that initiates a dataset download request, cancels an in-progress request, or triggers the
data file download once the file is ready. The button label indicates the action taken.
"""
_ACTION_SUBMIT: str = 'Submit download request'
""" Label on action button before a request is submitted. """
_ACTION_CANCEL: str = 'Cancel'
""" Label on action button while a download request is being processed on server. """
_ACTION_RESET: str = 'Try again'
""" Label on action button when a download request has failed (so user can view error message before resetting). """
_ACTION_DOWNLOAD: str = 'Download data file'
""" 
Label on action button once the file is ready for download. The button is configured with the download URL so that
clicking it will trigger the download.
"""
_DOWNLOAD_PROG_BAR: str = 'down_prog_bar'
""" ID of Bootstrap Progress component to display progress of a pending download request after submission. """
_DOWNLOAD_PROG_LABEL: str = 'down_prog_label'
""" ID of Bootstrap Label reflecting most recent status message for an in-progress download request. """
_DOWNLOAD_PROG_INTV: str = 'down_prog_intv'
""" ID of Dash Interval component that fires once per second to update progress on a pending download request. """
_DOWNLOAD_REQID_DIV: str = 'down_reqid'
""" ID of hidden DIV that holds the unique ID of an in-progress download request. """
_DOWNLOAD_PROG_DIV: str = 'down_progress_div'
""" ID of the DIV containing the widgets displaying progress of a pending download request (so they can be hidden). """

_DOWNLOAD_PROG_INTV_DUR_MS: int = 500
""" Interval between progress updates while a pending download request is being fulfilled (in ms). """


def render_download_modal_and_button(session: Dict[str, AttributeValue]) -> Tuple[dbc.Modal, dbc.Button]:
    """
    Render the content of a Bootstrap Modal component that by which the user can submit a request for response data
    from the specified experiment session, monitor progress as the data file is prepared on the server, and finally
    download the generated data file.

    Args:
        session: A dictionary corresponding to one row in the search results table. At a minimum, it must include the
            primary key-value pairs that uniquely identify an experiment session in the database.
    Returns:
        A 2-tuple: The Bootstrap Modal component (initially hidden), along with a Bootstrap button labelled "Download
            data". Pressing that button will raise the modal window; the button is located on the 'Trial Data' tab pane
            at the /explore endpoint.
    """
    # gather neuron type, SNR, and #spikes for each neural unit recorded during session
    session_key = {k: session[k] for k in primary_key_of(DBTable.SESSION, False)}
    units: Optional[List[Dict[str, AttributeValue]]]
    neuron_type_map: Dict[int, str] = dict()
    try:
        units = fetch_restrict_proj([DBTable.SESSION_NEURON], [session_key], ['unit_type', 'unit_spikes', 'unit_snr'])
        neuron_types = fetch_rows(DBTable.NEURON_TYPE)
        if (units is not None) and (neuron_types is not None):
            for nt in neuron_types:
                neuron_type_map[nt['nt_id']] = nt['nt_name']
        else:
            raise Exception("Database retrieval failed")
    except Exception as e:
        get_application_logger().error(
            f"Error while fetching information needed to prepare download modal: {str(e)}", exc_info=True)
        units = []

    # keep the session key in a hidden DIV because we need it in some callbacks
    session_key_div = html.Div(children=json.dumps(session_key),
                               id=_DOWNLOAD_SESSION_KEY_DIV, style=dict(display='none'))

    markdown = dcc.Markdown(
        '''Per-trial response data from the current experiment session is available for download here. Behavioral 
        (eye position and velocity traces) and neural response data (spike timestamps relative to trial start) are 
        included for each trial presented during the session, along with fixation target position traces. You can 
        specify which neural units to include in the download (up to 5), restrict the download to successfully 
        completed trials only, elect to remove saccades from the eye velocity traces, and choose the output format.'''
    )

    options = [
        {'label': f"Unit {u['unit_id']}", 'value': u['unit_id'],
         'title': f"{neuron_type_map[u['unit_type']]}, SNR={u['unit_snr']:.2f}, #spikes={u['unit_spikes']}"}
        for u in units
    ]
    select_units_row = dbc.Row([
        dcc.Dropdown(id=_DOWNLOAD_UNIT_DROP_ID, multi=True, clearable=True, searchable=False,
                     options=options, value=[]),
        dbc.Label(f"Select up to {MAX_UNITS_PER_DOWNLOAD} neural units. Hover over any entry to see "
                  f"neuron type, stats.", size='sm')
    ], class_name='mb-2')
    restrict_trials_chk_row = dbc.Row([
        dbc.Col([
            dbc.Checkbox(id=_DOWNLOAD_OK_TRIALS_CHK, label='Include only successfully completed trials?', value=False)
        ], width='auto')
    ], class_name='mb-2')
    rmv_sacc_chk_row = dbc.Row([
        dbc.Col([
            dbc.Checkbox(id=_DOWNLOAD_RMVSACC_CHK, label='Remove saccades from eye velocity traces?', value=False)
        ], width='auto')
    ], class_name='mb-2')
    output_fmt_row = dbc.Row([
        dbc.Col(dbc.Label("Output Format:"), width='auto'),
        dbc.Col(dbc.RadioItems(
            options=[{"label": v, "value": k} for k, v in DOWNLOAD_FORMATS.items()],
            value=None, id=_DOWNLOAD_FMT_RADIO, inline=True
        ), width='auto')
    ], class_name='mb-2')

    # to monitor progress of pending download request once submitted.
    prog_bar = dbc.Progress(id=_DOWNLOAD_PROG_BAR, value=0, label='0%', color='info', striped=True, animated=True,
                            style=dict(height='16px'))
    prog_label = dbc.Label([html.I(className="bi bi-info-circle-fill me-2"), "Status message goes here"],
                           id=_DOWNLOAD_PROG_LABEL, size='sm')
    prog_reqid = html.Div(id=_DOWNLOAD_REQID_DIV, style=dict(display='none'))
    prog_intv = dcc.Interval(id=_DOWNLOAD_PROG_INTV, interval=_DOWNLOAD_PROG_INTV_DUR_MS, disabled=True)
    prog_div = html.Div([
        dbc.Row(dbc.Col(prog_bar, width=12), class_name='mb-1'),
        dbc.Row(dbc.Col(prog_label, width='auto')),
        prog_reqid, prog_intv
    ], id=_DOWNLOAD_PROG_DIV, className='mt-2', style=dict(display='none'))

    action_row = dbc.Row([
        dbc.Col([
            dbc.Button(_ACTION_SUBMIT, id=_DOWNLOAD_ACTION, n_clicks=0, size='sm', external_link=True, href=None)
        ], width='auto'),
        dbc.Col(prog_div)
    ], align='center')

    download_panel = html.Div([
        session_key_div, markdown, html.Hr(), select_units_row, restrict_trials_chk_row, rmv_sacc_chk_row,
        output_fmt_row, action_row
    ])

    download_btn = dbc.Button("Download data", id=_DOWNLOAD_MODAL_OPEN_ID, size='sm',
                              disabled=not flask_login.current_user.is_authenticated)
    download_modal = dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle("Request dataset download"), close_button=False),
            dbc.ModalBody(download_panel, id=_DOWNLOAD_MODAL_BODY_ID),
            dbc.ModalFooter(dbc.Row([dbc.Button("Close", id=_DOWNLOAD_MODAL_CLOSE_ID)]))
        ],
        id=_DOWNLOAD_MODAL_ID, backdrop="static", keyboard=False, size="xl", centered=True
    )

    return download_modal, download_btn


# noinspection PyUnusedLocal
@callback(
    Output(_DOWNLOAD_MODAL_ID, "is_open"),
    [Input(_DOWNLOAD_MODAL_OPEN_ID, "n_clicks"), Input(_DOWNLOAD_MODAL_CLOSE_ID, "n_clicks")]
)
def on_show_hide_download_request_form(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    return trigger_id == _DOWNLOAD_MODAL_OPEN_ID


@callback(Output(_DOWNLOAD_UNIT_DROP_ID, "value"), [Input(_DOWNLOAD_UNIT_DROP_ID, "value")])
def restrict_num_units_in_download_request(sel_units):
    if isinstance(sel_units, list) and (len(sel_units) > MAX_UNITS_PER_DOWNLOAD):
        sel_units = sel_units[0:MAX_UNITS_PER_DOWNLOAD]
        return sel_units
    return no_update


@callback(
    [Output(_DOWNLOAD_REQID_DIV, 'children'), Output(_DOWNLOAD_PROG_DIV, "style"),
     Output(_DOWNLOAD_PROG_INTV, 'disabled'), Output(_DOWNLOAD_PROG_BAR, "value"),
     Output(_DOWNLOAD_PROG_BAR, "label"), Output(_DOWNLOAD_PROG_BAR, 'color'), Output(_DOWNLOAD_PROG_BAR, 'animated'),
     Output(_DOWNLOAD_PROG_LABEL, 'children'), Output(_DOWNLOAD_PROG_LABEL, 'class_name'),
     Output(_DOWNLOAD_ACTION, "children"), Output(_DOWNLOAD_ACTION, 'href'),
     Output(_DOWNLOAD_MODAL_CLOSE_ID, 'disabled')],
    [Input(_DOWNLOAD_ACTION, 'n_clicks'), Input(_DOWNLOAD_MODAL_CLOSE_ID, 'n_clicks'),
     Input(_DOWNLOAD_PROG_INTV, 'n_intervals')],
    [State(_DOWNLOAD_UNIT_DROP_ID, "value"), State(_DOWNLOAD_OK_TRIALS_CHK, "value"),
     State(_DOWNLOAD_RMVSACC_CHK, "value"), State(_DOWNLOAD_FMT_RADIO, "value"), State(_DOWNLOAD_ACTION, "children"),
     State(_DOWNLOAD_SESSION_KEY_DIV, 'children'), State(_DOWNLOAD_REQID_DIV, 'children')]
)
def on_submit_cancel_or_update_progress(*args):
    out = [no_update] * 12

    # user must be logged into portal to perform data download
    requester = flask_login.current_user.get_id() if flask_login.current_user.is_authenticated else None
    if requester is None:
        return tuple(out)

    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else ""
    action_label = args[-3]
    # IMPORTANT: A Python date is converted to string form when JSONified, so convert back since a date is expected
    session_key = json.loads(args[-2])
    session_key['session_date'] = datetime.strptime(session_key['session_date'], '%Y-%m-%d').date()
    req_id = args[-1]
    if (trigger_id == _DOWNLOAD_ACTION) and (action_label == _ACTION_SUBMIT):
        # submit a new download request
        unit_list = args[3] if isinstance(args[3], list) else []
        ok, req_id = request_data_download(requester, session_key, unit_list, output_fmt=args[6],
                                           remove_sacc=args[5], complete_reps=args[4])
        action_label = _ACTION_CANCEL if ok else _ACTION_RESET
        status = [html.I(className="bi bi-info-circle-fill me-2"), "Request submitted successfully"] if ok else \
            [html.I(className="bi bi-x-octagon-fill me-2"), req_id]
        out[0:12] = req_id if ok else "", None, not ok, 0 if ok else 100, "0%" if ok else "FAILED", \
            "info" if ok else "danger", ok, status, 'text-primary' if ok else 'text-danger', action_label, None, ok
    elif (trigger_id == _DOWNLOAD_ACTION) and (action_label == _ACTION_CANCEL):
        # cancel a download request while it is being fulfilled on server, then reset GUI
        if len(req_id) > 0:
            cancel_pending_download_request(requester, req_id)
        out[0:12] = "", dict(display='none'), True, 0, "0%", "info", False, "", None, _ACTION_SUBMIT, None, False
    elif (trigger_id == _DOWNLOAD_ACTION) and (action_label == _ACTION_RESET):
        if len(req_id) > 0:
            cancel_pending_download_request(requester, req_id)
        out[0:12] = "", dict(display='none'), True, 0, "0%", "info", False, "", None, _ACTION_SUBMIT, None, False
    elif (trigger_id == _DOWNLOAD_ACTION) and (action_label == _ACTION_DOWNLOAD):
        # record that user has initiated file download, then reset form
        out[0:12] = "", dict(display='none'), True, 0, "0%", "info", False, "", None, _ACTION_SUBMIT, None, False
    elif trigger_id == _DOWNLOAD_MODAL_CLOSE_ID:
        # if there's a pending download request, let it continue. Reset all widgets to start a new request.
        out[0:12] = "", dict(display='none'), True, 0, "0%", "info", False, "", None, _ACTION_SUBMIT, None, False
    elif trigger_id == _DOWNLOAD_PROG_INTV:
        # get status update for an in-progress request and update widgets accordingly
        req_status = pending_download_request_status(requester, req_id)
        if req_status is None:
            # an error occurred while retrieving status update
            status = [html.I(className="bi bi-x-octagon-fill me-2"),
                      "Unknown error occurred while checking status of download request"]
            out[0:12] = "", no_update, True, 100, "FAILED", "danger", False, status, 'text-danger', _ACTION_RESET, \
                None, False
        elif req_status.state == DOWNLOAD_PREPPING:
            out[3:5] = req_status.pct_complete, f"{req_status.pct_complete}%"
            out[7] = [html.I(className="bi bi-info-circle-fill me-2"), req_status.message]
        elif req_status.state == DOWNLOAD_READY:
            # file is ready for download. Embed URL in action button so that clicking it again triggers the download.
            # We disable the modal's close button to emphasize that the user needs to complete the download -- once the
            # file has been generated, the requester "owns" the download.
            status = [html.I(className="bi bi-check-circle-fill me-2"), req_status.message]
            out[2:12] = True, 100, "100%", "success", False, status, 'text-success', _ACTION_DOWNLOAD, \
                req_status.presigned_url, True
        else:  # DOWNLOAD_FAIL
            status = [html.I(className="bi bi-x-octagon-fill me-2"), req_status.message]
            out[0:12] = "", no_update, True, 100, "FAILED", "danger", False, status, 'text-danger',  _ACTION_RESET, \
                None, False

    return tuple(out)
