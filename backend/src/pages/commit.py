"""
commit.py: The "commit session" page in web-based interface to the Lisberger laboratory database.

This web page displays all pending session commit jobs initiated by the current login user with 'commit' level access.
The user can check the status of any pending jobs, initiate a new commit, review a candidate session after the archive
has been pre-processed, submit the reviewed session to be committed to the database, cancel any failed or in-progress
commit, or remove any completed commit jobs.

Preprocessing very large session archives and committing the data to the portal database can take many seconds or even
minutes, depending on the archive size. In between is a user-interactive "review" stage -- necessary only if the
preprocessed session includes one or more trial protocols requiring manual validation. To keep the web front end
responsive, the server queues background "workers" (Redis Queue, or RQ) to handle the pre-processing and final commit
stages. When the review phase can be skipped, the job proceeds to completion without any user intervention.

TODO: COMPLETE DESCRIPTION. Also, drop description of issue with dash_uploader -- if our new approach works!

TODO: IMPLEMENTATION ISSUES --
 - Working with dash_uploader is a real pain in the ass. The problem is that you can't easily give it a new "upload_id"
   for each separate upload, so it's hard to reuse. Even though I create a NEW uploader component when the user presses
   the button to start a commit, behind the scenes the upload ID assigned to the previous uploader component gets used,
   and so the file ends up in the wrong folder ($REPO_HOME/staging/stale_upload_id) on the server. I always have to
   have an uploader in the layout, or the Dash callback definitions don't work.
     -- New approach #1: Since we only allow the user to upload one archive at a time, we assign a UUID as the upload ID
     ON THE CLIENT SIDE and, when the upload is done, pass that to the server so that it can find the folder containing
     the uploaded file, which it then renames with the commit job ID (so we don't have to move a huge file).
     -- Slight variation: Just use the authenticated username as the upload ID. Then it's easy for server to check for
     the uploaded file, and to report progress during the upload.
     -- Using the username as upload ID would expose the username in plain-text in the Resumable JS component. Perhaps
     we should stick with a UUID, but pass that UUID to the server when a new commit is started -- and the server could
     cache it so that it can report on upload progress.

@author: sruffner
@created: 18oct2021
"""
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from dash import callback_context, callback, exceptions as dash_exc, no_update, dash_table as dt, html, dcc, Input, \
    Output, State
import dash_bootstrap_components as dbc
import dash_uploader as du
import flask_login

from app import load_authorized_user
from config.app_logging import get_application_logger
from database.commit_ops import CommitStateEnum, initiate_session_commit, get_pending_commit_jobs_for, \
    cancel_or_remove_commit_job, CommitJobStatus, ready_to_commit, protocol_names, protocol_definition, \
    add_rv_to_protocol, validate_protocol, commit_to_database, commit_job_status, on_archive_uploaded_to_workspace
from sglportalapi.maestro import Protocol, SegParam, SegParamType, Target, Point2D
from database.table_info import Column, DBTable, attribute_info
from database.table_ops import fetch_restrict_proj, fetch_attribute_values


def serve_layout() -> html.Div:
    """
    Serve the layout for the "commit" page displaying the queue of pending commit jobs initiated by the currently
    authenticated user. The page includes a means of starting a new commit job. Status information on pending jobs are
    displayed in tabular form. Button contols above the table let the user refresh the table contents, show the message
    history of a selected job, cancel/remove a selected job, and -- if necessary -- review and validate trial protocols
    from a preprocessed commit prior to committing the experiment session to the database.

    Returns:
        An HTML Div rendering the "commit" page.
    """
    err_msg, jobs = _commit_jobs_for_current_user()

    jobs_table = _table_of_pending_commit_jobs(jobs)
    alert = dbc.Alert(err_msg if err_msg else "", id=_ALERT_ID, color="danger", dismissable=True, fade=True,
                      duration=10000, is_open=(err_msg is not None))
    err1_div = html.Div("", id=_ERR_DIV1_ID, style=dict(display='none'))
    err2_div = html.Div("", id=_ERR_DIV2_ID, style=dict(display='none'))
    refresh_btn = dbc.Button("Refresh", id=_REFRESH_BTN_ID, n_clicks=0, class_name='me-4')
    start_btn = dbc.Button("New commit...", id=_START_BTN_ID, n_clicks=0)
    message_btn = dbc.Button("Messages...", id=_MESSAGE_BTN_ID, n_clicks=0, disabled=True, class_name='me-2')
    next_btn = dbc.Button("Review & Commit", id=_REVIEW_BTN_ID, n_clicks=0, disabled=True, class_name='me-2')
    remove_btn = dbc.Button("Cancel/Remove", id=_REMOVE_BTN_ID, n_clicks=0, disabled=True)
    button_row = dbc.Row([
        dbc.Col([refresh_btn, start_btn], width='auto', class_name="me-4"),
        dbc.Col([message_btn, next_btn, remove_btn], width='auto')
    ], justify='between', class_name="mt-2 mb-2")

    commit_modal = _create_commit_modal()
    upload_modal = _create_upload_modal()
    review_modal = dbc.Modal(_layout_review_modal(), id=_REVIEW_ID, backdrop="static", size="xl", is_open=False)
    messages_modal = dbc.Modal(
        [
            dbc.ModalHeader(id=_HISTORY_HEADER_ID),
            dbc.ModalBody(dcc.Markdown(id=_HISTORY_MARKDOWN_ID)),
            dbc.ModalFooter(dbc.Row([dbc.Button("Close", id=_CLOSE_HISTORY_ID, n_clicks=0)]))
        ],
        id=_HISTORY_ID, backdrop=False, size="lg", is_open=False
    )

    card = dbc.Card([
        dbc.CardHeader("Experiment session commits in progress"),
        dbc.CardBody([alert, err1_div, err2_div, button_row, jobs_table]),
    ], class_name='mx-5 my-5')

    return html.Div([card, commit_modal, upload_modal, review_modal, messages_modal])


_ALERT_ID = 'commit-alert'
""" ID of Bootstrap Alert located at top of page that displays an error message if a request on this page fails. """
_ERR_DIV1_ID = 'commit-error1'
"""
ID of a hidden Div that is populated with an error message if an error occurs during the callback associated
with the 'Refresh' or 'Remove' buttons.
"""
_ERR_DIV2_ID = 'commit-error2'
"""
ID of a hidden Div that is populated with an error message if an error occurs during the callback associated
with the Modal component that uploads the experiment archive via Dash uploader.
"""
_START_BTN_ID = 'commit-start-btn'
""" ID of the button that raises the Modal component that initiates a session commit job. """
_REFRESH_BTN_ID = 'jobs-refresh'
""" User presses this button to refresh status information on all pending commit jobs. """
_MESSAGE_BTN_ID = 'job-messages'
""" User presses this button to view the progress message history for the currently selected commit job. """
_REVIEW_BTN_ID = 'job-review'
"""
User presses this button to review a job after preprocessing. This raises a modal by which user interactively
validates any trial protocols requiring it, then starts the actual database commit.
"""
_REMOVE_BTN_ID = 'job-remove'
""" User presses this button to cancel and/or remove the currently selected commit job. """

_HISTORY_ID = 'message-history'
""" ID of Modal component displaying the progress message history for a pending commit job. """
_HISTORY_HEADER_ID = 'message-history-header'
""" ID of the header of the Modal component displaying the progress message history. The commit job ID goes here. """
_HISTORY_MARKDOWN_ID = 'message-history-markdown'
""" ID of the Dash Markdown component in which the progress message history for a pending commit job is listed. """
_CLOSE_HISTORY_ID = 'message-history-close'
""" ID of button that closes the Modal component displaying the progress message history for a pending commit job. """


def _commit_jobs_for_current_user() -> Tuple[Optional[str], List[Dict[str, Any]]]:
    # if no portal user or user does not have commit access, then this page displays nothing of use
    err_msg = None
    job_rows: List[Dict[str, Any]] = []
    username = _get_current_username()
    if username is None:
        err_msg = "Access denied - You must be logged into the portal with the appropriate privileges to commit " \
                  "experiment data to the portal and view in-progress commits."
    else:
        jobs = get_pending_commit_jobs_for(username)
        if isinstance(jobs, str):
            err_msg = f"Server error: {jobs}. Try again later, or contact the portal administrator."
        else:
            for job in jobs:
                job_rows.append(_job_table_row_from_job_status_info(job))
    return err_msg, job_rows


def _get_current_username() -> Optional[str]:
    """
    Helper method retrieves the username for the currently authenticated user from Flask Login. That user must have
    the required privileges to access this page.

    Returns:
        The username, or None if client is not authenticate or lacks the required privileges to access this page
    """
    username = None
    if flask_login.current_user.is_authenticated:
        portal_user = load_authorized_user(flask_login.current_user.get_id())
        if portal_user and portal_user.can_commit_to_database():
            username = portal_user.get_id()
        else:
            get_application_logger().debug("On commit page, but client not authenticated or lacks commit access.")
    return username


def _job_table_row_from_job_status_info(job: CommitJobStatus) -> Dict[str, Any]:
    start_ts = datetime.fromtimestamp(job.started).strftime("%Y-%m-%d %I:%M:%S %p")
    update_ts = datetime.fromtimestamp(job.updated).strftime("%Y-%m-%d %I:%M:%S %p")
    return {
        'id': job.id,
        'state': job.state.value,  # this field is not displayed
        'started': f"{start_ts}",
        'status_desc': f"**{job.state.get_state_descriptor()}**",
        'last_update': f"[**{update_ts}**] {job.msg}"
    }


def _table_of_pending_commit_jobs(job_rows: List[Dict[str, Any]]) -> dt.DataTable:
    jobs_table = dt.DataTable(
        id=_JOBS_TABLE_ID,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in _JOBS_TABLE_COLS],
        data=job_rows,
        row_selectable='single',
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in _JOBS_TABLE_COLS],
        tooltip_data=None, tooltip_duration=None,
        css=[],
        style_table={'height': '300px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )
    return jobs_table


_JOBS_TABLE_ID = 'jobs-table'
""" A Dash DataTable listing all pending commit jobs for the currently authenticated user. """

_JOBS_TABLE_COLS: List[Column] = [
    Column('id', 'Job ID', '170px', True),
    Column('started', 'Started', '100px', True),
    Column('status_desc', 'Status', '100px', True),
    Column('last_update', 'Last Update', '330px', True)
]
""" Defined columns for the pending commit jobs table. """


@callback(
    [Output(_MESSAGE_BTN_ID, 'disabled'), Output(_REVIEW_BTN_ID, 'disabled'), Output(_REMOVE_BTN_ID, 'disabled')],
    [Input(_JOBS_TABLE_ID, 'selected_rows'), Input(_REFRESH_BTN_ID, "n_clicks")],
    [State(_JOBS_TABLE_ID, 'selected_rows'), State(_JOBS_TABLE_ID, 'data')]
)
def select_row_callback(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    arg_idx = 2 if trigger == _REFRESH_BTN_ID else 0
    idx = args[arg_idx][0] if isinstance(args[arg_idx], list) and (len(args[arg_idx]) > 0) else -1
    row = args[3][idx] if isinstance(args[3], list) and (-1 < idx < len(args[3])) else None
    disable_review = (row is None) or (row['state'] != CommitStateEnum.REVIEW.value)
    return row is None, disable_review, row is None


@callback(
    [Output(_HISTORY_ID, 'is_open'), Output(_HISTORY_HEADER_ID, "children"), Output(_HISTORY_MARKDOWN_ID, "children")],
    [Input(_MESSAGE_BTN_ID, 'n_clicks'), Input(_CLOSE_HISTORY_ID, 'n_clicks')],
    [State(_JOBS_TABLE_ID, 'data'), State(_JOBS_TABLE_ID, 'selected_rows')]
)
def display_hide_progress_history(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _CLOSE_HISTORY_ID:
        return False, no_update, no_update
    else:  # _MESSAGE_BTN_ID
        selection = args[-1]
        idx = selection[0] if (selection is not None) and (len(selection) > 0) else -1
        job_rows = args[-2]
        sel_row = job_rows[idx] if ((job_rows is not None) and (-1 < idx < len(job_rows))) else None
        if sel_row is None:
            raise dash_exc.PreventUpdate
        job_status = commit_job_status(sel_row['id'])
        if isinstance(job_status, str):
            # A server error happened. Display the error message in the modal.
            markdown = f"**{job_status}**. Try again later or contact portal administrator."
        else:
            # Display progress message list in markdown
            markdown = "* " + "\n* ".join(job_status.message_history)
        header = f"Progress history for: {sel_row['id']}"
        return True, header, markdown


@callback(
    [Output(_ALERT_ID, 'is_open'), Output(_ALERT_ID, "children")],
    [Input(_ERR_DIV1_ID, 'children'), Input(_ERR_DIV2_ID, 'children')]
)
def raise_page_alert(err_msg1, err_msg2):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if (trigger == _ERR_DIV1_ID) and isinstance(err_msg1, str) and (len(err_msg1) > 0):
        return True, err_msg1
    elif (trigger == _ERR_DIV2_ID) and isinstance(err_msg2, str) and (len(err_msg2) > 0):
        return True, err_msg2
    else:
        return no_update, no_update


# Must define this constant prior to the callback that uses it as an Input.
_UPLOAD_ID = 'commit-upload-modal'
""" ID of Bootstrap Modal component by which user uploads the experiment archive for a new commit job. """


@callback(
    [Output(_JOBS_TABLE_ID, "data"), Output(_JOBS_TABLE_ID, "selected_rows"), Output(_ERR_DIV1_ID, "children")],
    [Input(_REFRESH_BTN_ID, "n_clicks"), Input(_REMOVE_BTN_ID, "n_clicks"), Input(_UPLOAD_ID, "is_open")],
    [State(_JOBS_TABLE_ID, "data"), State(_JOBS_TABLE_ID, "selected_rows")]
)
def update_jobs_table(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate

    # there must be a logged-in user with 'commit' access
    username = _get_current_username()
    if not username:
        return no_update, no_update, "Access denied. You must be logged into portal with commit privileges."

    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if (trigger == _REFRESH_BTN_ID) or (trigger == _UPLOAD_ID):
        # refresh jobs table when Refresh button clicked or upload modal is opened or closed
        err_msg, jobs = _commit_jobs_for_current_user()
        if err_msg is not None:
            return no_update, no_update, err_msg
        else:
            return jobs, no_update, ""
    elif trigger == _REMOVE_BTN_ID:
        job_rows = args[-2]
        selection = args[-1]
        idx = selection[0] if (isinstance(selection, list)) and (len(selection) > 0) else -1
        sel_row = job_rows[idx] if ((job_rows is not None) and (-1 < idx < len(job_rows))) else None
        job_id = sel_row['id'] if sel_row else None
        if job_id is None:
            raise dash_exc.PreventUpdate
        removed, err_msg, job_info = cancel_or_remove_commit_job(job_id)
        if len(err_msg) > 0:
            return no_update, no_update, err_msg
        elif removed:
            job_rows.pop(idx)
        else:
            job_rows[idx] = _job_table_row_from_job_status_info(job_info)
        return job_rows, [] if removed else no_update, ""

    get_application_logger().debug(f"Failed to identify callback trigger: {trigger}")
    raise dash_exc.PreventUpdate


def _layout_review_modal(job_id: Optional[str] = None) -> Tuple[dbc.ModalHeader, dbc.ModalBody, dbc.ModalFooter]:
    proto_card, err_msg = _layout_protocol_review_card(job_id)

    if err_msg:
        alert_color, alert_msg = 'danger', err_msg
    elif job_id:
        ok, ready, alert_msg = ready_to_commit(job_id)
        alert_color = 'danger' if (not ok) else ('success' if ready else 'warning')
    else:
        alert_color, alert_msg = 'danger', 'No job loaded'
    alert = dbc.Alert(alert_msg, id=_REVIEW_ALERT_ID, color=alert_color, dismissable=False, is_open=True,
                      class_name='mb-2')

    # we use these hidden DIVs to trigger updates to the Bootstrap Alert and the "Commit" button enable state in
    # response to user interactions on the Review modal.
    proto_alert_div = html.Div("", id=_REVIEW_PROTO_ALERT_DIV, style=dict(display='none'))
    commit_alert_div = html.Div("", id=_REVIEW_COMMIT_ALERT_DIV, style=dict(display='none'))

    header = dbc.ModalHeader(dbc.ModalTitle(f"Validate selected trial protocols for commit job: {job_id}",
                                            id=_REVIEW_TITLE_ID))
    body = dbc.ModalBody([alert, proto_card, proto_alert_div, commit_alert_div])
    footer = dbc.ModalFooter([
        dbc.Button("Commit", id=_REVIEW_COMMIT_ID, n_clicks=0, disabled=(alert_color != 'success'), class_name='mr-2'),
        dbc.Button("Close", id=_REVIEW_CLOSE_ID, n_clicks=0)
    ])
    return header, body, footer


_REVIEW_ID = 'review-modal'
""" 
ID of Modal "Review" component by which user reviews and validates trial protocols for a pending commit job, then
initiates the actual commit.
"""
_REVIEW_TITLE_ID = 'review-title'
""" ID of the ModalTitle inside the header of the Modal "Review" component. The title text contains the job ID. """
_REVIEW_CLOSE_ID = 'review-close-btn'
""" ID of button that closes the Modal "Review" component. """
_REVIEW_COMMIT_ID = 'review-commit-btn'
""" ID of button that triggers the actual commit of the experiment session to the database. """
_REVIEW_PROTO_ALERT_DIV = "review-proto-alert-div"
"""
Hidden DIV used to trigger refresh of the Alert content and Commit button enable state when something changes (or
an error occurs) while user is validating trial protocols in the Review modal.
"""
_REVIEW_COMMIT_ALERT_DIV = 'review-commit-alert-div'
"""
Hidden DIV used to trigger refresh of the Alert content and Commit button enable state when the user clicks the 
Commit button on the Review modal to finalize a commit, but something goes wrong on the server.
"""
_REVIEW_ALERT_ID = 'review-alert'
""" ID of Bootstrap alert that displays messages in the body of the Modal "Review" component. """


def _layout_protocol_review_card(job_id: Optional[str]) -> Tuple[dbc.Card, Optional[str]]:
    proto_names: List[str]
    initial_proto: Optional[Protocol] = None
    err_msg: Optional[str] = None
    if job_id:
        proto_names = protocol_names(job_id)
        if not proto_names:
            err_msg = "Error - Failed to retrieve cached trial protocol names from server"
            proto_names = []
        else:
            initial_proto = protocol_definition(job_id, 0)
            if not initial_proto:
                err_msg = "Error - Failed to retrieve a cached trial protcol definition from server"
    else:
        proto_names = []

    select_proto = dbc.Select(
        id=_PROTO_SELECT_ID,
        options=[{'label': name, 'value': str(i)} for i, name in enumerate(proto_names)],
        value=str(0) if len(proto_names) > 0 else None
    )

    proto_div = html.Div(_layout_protocol_div(initial_proto), id=_PROTO_DIV_ID)

    return dbc.Card(dbc.CardBody([select_proto, proto_div]), class_name="mt-2"), err_msg


_PROTO_DIV_ID = "review-proto-div"
""" ID of HTML Div on which trial protocols are displayed and edited during review phase. """
_PROTO_SELECT_ID = "review-proto-select"
""" ID of Bootstrap Select by which user selects which trial protocol to display for review and validation. """
_PROTO_VALID_BTN_ID = "review-proto-valid"
""" ID of button by which user validates a protocol definition that requires manual validation. """
_PROTO_ADD_RV_BTN_ID = "review-proto-add-rv"
""" ID of button by which user adds a random variable to the currently displayed trial protocol. """
_PROTO_RV_TYPE_SELECT_ID = "review-proto-rv-type-select"
""" ID of Bootstrap Select component by which user selects type of random variable for a displayed trial protocol. """
_PROTO_RV_SEG_SELECT_ID = "review-proto-rv-seg-select"
""" ID of Bootstrap Select component by which user selects trial segment to which random variable applies. """
_PROTO_RV_TGT_SELECT_ID = "review-proto-rv-tgt-select"
""" ID of Bootstrap Select component by which user selects trial target to which random variable applies. """
_PROTO_RV_GROUP_ID = "review--proto-rv-form"
""" ID of Bootstrap Form Group containing widgets for adding a random variable to the displayed trial protocol. """


def _layout_protocol_div(proto: Optional[Protocol]) -> List[Any]:
    # NOTE: This has to work even if argument is None, so that all widgets are realized -- since they appear
    # in callbacks.
    needs_validation = False if (proto is None) else proto.is_candidate
    valid_btn = dbc.Button("Validate" if needs_validation else "\u2713 Validated", id=_PROTO_VALID_BTN_ID,
                           disabled=(not needs_validation), size='sm')
    tool_tip = dbc.Tooltip(
        "Any trial protocol based on fewer than 3 reps and not matching an existing protocol in the database must "
        "be manually verified by the user. Add any missing random variables (eg, a random-duration fixation "
        "segment) to the definition (if any), then press this button to validate the protocol.",
        target=_PROTO_VALID_BTN_ID)
    reps_badge = dbc.Badge(f"# reps = {proto.num_reps if proto else 0} ", color='info', class_name='ms-2 me-5')
    add_rv_btn = dbc.Button("Add Random Var:", id=_PROTO_ADD_RV_BTN_ID, size='sm')
    n_segs = proto.trial.num_segments if proto else 0
    n_tgts = proto.trial.num_targets if proto else 0
    select_rv_type = dbc.InputGroup([
        dbc.InputGroupText("Type"),
        dbc.Select(
            id=_PROTO_RV_TYPE_SELECT_ID,
            options=[{'label': t.name, 'value': str(t.value)} for t in SegParamType if
                     t.can_vary_randomly()],
            value=str(SegParamType.DURATION.value)
        )
    ], size='sm')
    seg_select = dbc.InputGroup([
        dbc.InputGroupText("Segment"),
        dbc.Select(
            id=_PROTO_RV_SEG_SELECT_ID,
            options=[{'label': str(i), 'value': str(i)} for i in range(n_segs)],
            value='0' if n_segs > 0 else None
        )
    ], size='sm')
    tgt: Target
    opts = [{'label': tgt.name, 'value': str(i)} for i, tgt in enumerate(proto.trial.targets)] if n_tgts > 0 else []
    tgt_select = dbc.InputGroup([
        dbc.InputGroupText("Target"),
        dbc.Select(
            id=_PROTO_RV_TGT_SELECT_ID,
            options=opts,
            value='0' if n_tgts > 0 else None
        )
    ], size='sm')
    validate_row = dbc.Row([
        dbc.Col([valid_btn, tool_tip], width='auto', class_name='me-1'),
        dbc.Col(reps_badge, width='auto', class_name='me-3'),
        dbc.Col(dbc.Row([
            dbc.Col(add_rv_btn, width='auto', class_name='me-2'),
            dbc.Col(select_rv_type, width='auto', class_name='me-1'),
            dbc.Col(seg_select, width='auto', class_name='me-1'),
            dbc.Col(tgt_select, width='auto')
        ], justify='start', class_name='g-0', id=_PROTO_RV_GROUP_ID,
            style={} if needs_validation else dict(display='none')))
    ], justify='start', class_name='g-0 mt-3')

    cmpt_list = display_trial_protocol_definition(proto) if proto else list()
    cmpt_list.insert(0, validate_row)
    return cmpt_list


def display_trial_protocol_definition(proto: Protocol) -> List[Any]:
    """
    Generate an Dash-based presentation of a Maestro trial protocol's definition. A Dash Datatable component displays
    the segment table for the protocol, and an HTML Div component houses a series of Bootstrap badges that display key
    information like: segment index at which recording begins, the target transform, targets, perturbations, tagged
    sections, and random variables. Tooltips associated with some of the badges reveal more detailed information
    like target definitions, perturbation details, and so on.

    Returns:
        A list of two components, a div and a Dash Datable, which should be embedded as children of an outer div.
    """
    badges = [
        dbc.Badge(f"Record Seg: {proto.trial.record_seg}", color="primary", class_name="me-3"),
        dbc.Badge(f"Transform: {str(proto.trial.global_transform)}", color="primary", class_name="me-3"),
        dbc.Badge(f"Targets: {proto.trial.num_targets}", id="disp_proto_targets", color="primary", class_name="me-3"),
        dbc.Badge(f"Perturbations: {proto.trial.num_perturbations}", id="disp_proto_perts", color="primary",
                  class_name="me-3"),
        dbc.Badge(f"Tagged Sects: {proto.trial.num_tagged_sections}", id="disp_proto_sections", color="primary",
                  class_name="me-3"),
        dbc.Badge(f"Random Vars: {len(proto.random_variables)}", id="disp_proto_random_vars", color="primary"),
        dbc.Tooltip([html.Div(f"{str(target)}") for target in proto.trial.targets],
                    target="disp_proto_targets", style={'max-width': '600px'})
    ]
    if proto.trial.num_perturbations > 0:
        badges.append(
            dbc.Tooltip([html.Div(f"{str(pert)}") for pert in proto.trial.perturbations],
                        target="disp_proto_perts", style={'max-width': '600px'})
        )
    if proto.trial.num_tagged_sections > 0:
        badges.append(
            dbc.Tooltip([html.Div(f"{str(section)}") for section in proto.trial.tagged_sections],
                        target="disp_proto_sections", style={'max-width': '600px'})
        )
    if len(proto.random_variables) > 0:
        badges.append(
            dbc.Tooltip([html.Div(f"{str(rv)}") for rv in proto.random_variables],
                        target="disp_proto_random_vars", style={'max-width': '600px'})
        )

    columns = [{"name": "", "id": "param"}]
    columns.extend([
        {"name": f"Segment {i}", "id": f"seg_{i}"} for i in range(proto.trial.num_segments)
    ])

    # NOTE: Any parameter that varies randomly in the trial protocol is represented by an asterisk '*' in the
    # segment table rendering rather than its value in the representative trial.
    duration = {"param": "Duration (ms)"}
    fix_tgts = {"param": "Fixation Targets #1, #2"}
    fix_accuracy = {"param": "Fix Accuracy H,V (deg)"}
    grace_period = {"param": "Grace Period (ms)"}
    xy_delta = {"param": "XYScope Intv (ms)"}
    marker = {"param": "Marker Pulse"}

    target_names = [tgt.name for tgt in proto.trial.targets]
    tgt_on = [{"param": name} for name in target_names]
    tgt_vstab = [{"param": "VStab"} for _ in target_names]
    tgt_pos = [{"param": "Position (deg)"} for _ in target_names]
    tgt_vel_acc = [{"param": "Vel (d/s), Acc (d/s^2)"} for _ in target_names]
    tgt_pat = [{"param": "Pattern Vel, Acc"} for _ in target_names]
    for i, seg in enumerate(proto.trial.segments):
        seg_id = f"seg_{i}"
        duration[seg_id] = "***" if SegParam(SegParamType.DURATION, i, -1) in proto.random_variables else seg.dur
        fix_tgts[seg_id] = f"{'NONE' if seg.fix1 < 0 else target_names[seg.fix1]} , " \
                           f"{'NONE' if seg.fix2 < 0 else target_names[seg.fix2]}"
        fix_accuracy[seg_id] = f"({seg.fixacc_h:.1f}, {seg.fixacc_v:.1f})"
        grace_period[seg_id] = f"{seg.grace}"
        xy_delta[seg_id] = seg.xy_update_intv
        marker[seg_id] = "NONE" if seg.pulse_ch < 0 else f"DO{seg.pulse_ch}"
        pt = Point2D()
        for tgt_idx in range(proto.trial.num_targets):
            tgt_on[tgt_idx][seg_id] = "ON" if seg.tgt_on(tgt_idx) else 'OFF'
            tgt_vstab[tgt_idx][seg_id] = seg.tgt_vel_stab_as_string(tgt_idx)
            pt.set_coords(seg.tgt_pos(tgt_idx))
            tgt_pos[tgt_idx][seg_id] = pt.as_string_with_wildcard(
                (SegParam(SegParamType.TGT_POS_H, i, tgt_idx) in proto.random_variables),
                (SegParam(SegParamType.TGT_POS_V, i, tgt_idx) in proto.random_variables))
            tgt_pos[tgt_idx][seg_id] += " rel" if seg.tgt_rel(tgt_idx) else " abs"
            pt.set_coords(seg.tgt_vel(tgt_idx))
            tgt_vel_out = pt.as_string_with_wildcard(
                (SegParam(SegParamType.TGT_VEL_H, i, tgt_idx) in proto.random_variables),
                (SegParam(SegParamType.TGT_VEL_V, i, tgt_idx) in proto.random_variables))
            pt.set_coords(seg.tgt_acc(tgt_idx))
            tgt_acc_out = pt.as_string_with_wildcard(
                (SegParam(SegParamType.TGT_ACC_H, i, tgt_idx) in proto.random_variables),
                (SegParam(SegParamType.TGT_ACC_V, i, tgt_idx) in proto.random_variables))
            tgt_vel_acc[tgt_idx][seg_id] = f"{tgt_vel_out}  {tgt_acc_out}"
            pt.set_coords(seg.tgt_pat_vel(tgt_idx))
            tgt_pat_vel_out = pt.as_string_with_wildcard(
                (SegParam(SegParamType.TGT_PAT_VEL_H, i, tgt_idx) in proto.random_variables),
                (SegParam(SegParamType.TGT_PAT_VEL_V, i, tgt_idx) in proto.random_variables))
            pt.set_coords(seg.tgt_pat_acc(tgt_idx))
            tgt_pat_acc_out = pt.as_string_with_wildcard(
                (SegParam(SegParamType.TGT_PAT_ACC_H, i, tgt_idx) in proto.random_variables),
                (SegParam(SegParamType.TGT_PAT_ACC_V, i, tgt_idx) in proto.random_variables))
            tgt_pat[tgt_idx][seg_id] = f"{tgt_pat_vel_out}  {tgt_pat_acc_out}"
    rows = [duration, fix_tgts, fix_accuracy, grace_period, xy_delta, marker]
    for i in range(proto.trial.num_targets):
        rows.extend([tgt_on[i], tgt_vstab[i], tgt_pos[i], tgt_vel_acc[i], tgt_pat[i]])

    # the segment table rendered as a Dash DataTable...
    # right-align first column displaying parameter descriptions, but left-align and underline the target names
    # that appear in that column. Use a brownish-yellow background to highlight the target name rows, which separate
    # the target trajectory sections in the segment table. Finally, use a green background to highlight any cell in
    # the segment table that houses a random variable.
    tgt_name_row_indices = [6 + i*5 for i in range(proto.trial.num_targets)]
    style_data_conditional = [
        {'if': {'column_id': 'param'}, 'textAlign': 'right'},
        {'if': {'column_id': 'param', 'row_index': tgt_name_row_indices},
         'textDecoration': 'underline', 'textAlign': 'left'},
        {'if': {'row_index': tgt_name_row_indices}, 'backgroundColor': 'rgba(218,165,32,128)', 'color': 'black'}
    ]
    for rv in proto.random_variables:
        seg_id = f"seg_{rv.seg_idx}"
        row_idx = 0
        if rv.type != SegParamType.DURATION:
            if rv.type in [SegParamType.TGT_POS_H, SegParamType.TGT_POS_V]:
                ofs = 2
            elif rv.type in [SegParamType.TGT_VEL_H, SegParamType.TGT_VEL_V, SegParamType.TGT_ACC_H,
                             SegParamType.TGT_ACC_V]:
                ofs = 3
            else:
                ofs = 4
            row_idx = 6 + rv.tgt_idx*5 + ofs
        style_data_conditional.append(
            {'if': {'column_id': seg_id, 'row_index': [row_idx]}, 'backgroundColor': 'limegreen', 'color': 'black'}
        )
    segment_table = dt.DataTable(
        columns=columns,
        data=rows,
        cell_selectable=False,
        style_header={'fontWeight': 'bold', 'textAlign': 'center', 'fontSize': 14, 'font-family': 'sans-serif'},
        style_cell={'textAlign': 'center', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px',
                    'fontSize': 14, 'font-family': 'sans-serif'},
        style_cell_conditional=[
            {'if': {'column_id': 'param'}, 'width': '200px', 'fontWeight': 'bold'}
        ],
        style_data_conditional=style_data_conditional,
        style_data={'whiteSpace': 'pre-wrap'},
        style_table={'height': '330px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
        fixed_rows={'headers': True, 'data': 0},
        fixed_columns={'headers': True, 'data': 0}
    )
    return [html.Div(badges, className='mt-3 mb-1'), segment_table]


@callback(
    [Output(_REVIEW_ID, 'is_open'), Output(_REVIEW_ID, "children")],
    [Input(_REVIEW_BTN_ID, 'n_clicks'), Input(_REVIEW_CLOSE_ID, 'n_clicks')],
    [State(_JOBS_TABLE_ID, 'data'), State(_JOBS_TABLE_ID, 'selected_rows')]
)
def display_hide_session_review(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate

    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _REVIEW_CLOSE_ID:
        return False, no_update
    elif trigger == _REVIEW_BTN_ID:
        # get job ID from the selected row in jobs table
        selection = args[-1]
        idx = selection[0] if (selection is not None) and (len(selection) > 0) else -1
        job_rows = args[-2]
        sel_row = job_rows[idx] if ((job_rows is not None) and (-1 < idx < len(job_rows))) else None
        if sel_row is None:
            raise dash_exc.PreventUpdate
        return True, _layout_review_modal(sel_row['id'])


@callback(
    [Output(_REVIEW_ALERT_ID, 'children'), Output(_REVIEW_ALERT_ID, 'color'), Output(_REVIEW_COMMIT_ID, 'disabled')],
    [Input(_REVIEW_PROTO_ALERT_DIV, 'children'), Input(_REVIEW_COMMIT_ALERT_DIV, 'children')]
)
def update_review_modal_alert(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    try:
        idx = [_REVIEW_PROTO_ALERT_DIV, _REVIEW_COMMIT_ALERT_DIV].index(trigger)
    except ValueError:
        raise dash_exc.PreventUpdate
    pos = args[idx].find('-') if isinstance(args[idx], str) else -1
    if pos > -1:
        alert_color = args[idx][0:pos]
        return args[idx][pos + 1:], alert_color, alert_color != 'success'
    else:
        raise dash_exc.PreventUpdate


@callback(
    [Output(_PROTO_DIV_ID, 'children'), Output(_REVIEW_PROTO_ALERT_DIV, 'children'),
     Output(_PROTO_VALID_BTN_ID, 'children'), Output(_PROTO_VALID_BTN_ID, 'disabled'),
     Output(_PROTO_RV_GROUP_ID, 'style'), Output(_PROTO_SELECT_ID, 'options'), Output(_PROTO_SELECT_ID, 'value')],
    [Input(_PROTO_SELECT_ID, 'value'), Input(_PROTO_ADD_RV_BTN_ID, 'n_clicks'), Input(_PROTO_VALID_BTN_ID, 'n_clicks')],
    [State(_PROTO_SELECT_ID, 'value'), State(_PROTO_RV_TYPE_SELECT_ID, 'value'),
     State(_PROTO_RV_SEG_SELECT_ID, 'value'), State(_PROTO_RV_TGT_SELECT_ID, 'value'),
     State(_PROTO_SELECT_ID, 'options'), State(_REVIEW_TITLE_ID, "children")])
def update_proto(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    out = [no_update] * 7
    # job ID is in the modal header title text
    idx = args[-1].find(":")
    job_id = args[-1][idx + 2:]
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _PROTO_SELECT_ID:
        proto_candidate = protocol_definition(job_id, int(args[0]))
        if proto_candidate:
            out[0] = _layout_protocol_div(proto_candidate)
        else:
            out[1] = "danger-An error occurred while retrieving protocol definition from server"
    elif trigger == _PROTO_ADD_RV_BTN_ID:
        ofs = 3
        proto_index = int(args[ofs])
        # noinspection PyArgumentList
        rv: SegParam = SegParam(SegParamType(int(args[ofs + 1])), int(args[ofs + 2]), int(args[ofs + 3]))
        proto_candidate = add_rv_to_protocol(job_id, proto_index, rv)
        if proto_candidate:
            out[0] = _layout_protocol_div(proto_candidate)
        else:
            out[1] = "danger-An error occurred while modifying protocol definition"
    elif trigger == _PROTO_VALID_BTN_ID:
        proto_index = int(args[3])
        if not validate_protocol(job_id, proto_index):
            out[1] = "danger-An error occurred while validating protocol definition on server"
        else:
            # strip the leading '** ' off the label for the just-validated protocol
            options = args[-2]
            proto_name = options[proto_index]['label'][3:]
            options[proto_index] = {'label': proto_name, 'value': str(proto_index)}
            out[5] = options

            # update alert to reflect fact that we just validated a protocol
            ok, ready, msg = ready_to_commit(job_id)
            out[1] = f"{'danger' if not ok else ('success' if ready else 'warning')}-{msg}"

            # preferably, load a different protocol that's not yet validated. However, if there aren't any unvalidated
            # protocols left or an error occurs retrieving it, just update the display for the current protocol to
            # reflect that it's now validated.
            next_proto: Optional[Protocol] = None
            proto_index = 0
            while proto_index < len(options) and not options[proto_index]['label'].startswith('** '):
                proto_index += 1
            if proto_index < len(options):
                next_proto = protocol_definition(job_id, proto_index)
            if next_proto is None:
                out[2] = "\u2713 Validated"
                out[3] = True
                out[4] = dict(display='none')
            else:
                out[0] = _layout_protocol_div(next_proto)
                out[6] = str(proto_index)
    return tuple(out)


@callback(
    [Output(_REVIEW_COMMIT_ALERT_DIV, 'children'), Output(_REVIEW_CLOSE_ID, 'n_clicks'),
     Output(_REFRESH_BTN_ID, 'n_clicks')],
    [Input(_REVIEW_COMMIT_ID, 'n_clicks')],
    [State(_REVIEW_CLOSE_ID, 'n_clicks'), State(_REFRESH_BTN_ID, 'n_clicks'), State(_REVIEW_TITLE_ID, 'children')]
)
def on_trigger_commit_to_database(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate

    # Each time the review modal is shown, it is laid out again. So this method will be invoked on initial load,
    # but the "n_clicks" attribute will be at its initial value of 0. In this case, do nothing. This is
    # imperative!
    if args[0] == 0:
        raise dash_exc.PreventUpdate
    # get job ID from the Review modal header title text
    idx = args[-1].find(":")
    job_id = args[-1][idx + 2:]
    err_msg = commit_to_database(job_id)
    if err_msg:
        return f"danger-{err_msg}", no_update, no_update
    else:
        n_close = (args[-3] + 1) if args[-3] else 1
        n_refresh = (args[-2] + 1) if args[-2] else 1
        return no_update, n_close, n_refresh


def _create_commit_modal() -> dbc.Modal:
    hdr = dbc.ModalHeader(dbc.ModalTitle("Enter required information to commit a new experiment session"))
    card, err_msg = _layout_session_metadata_card()
    alert = dbc.Alert(err_msg, id=_COMMIT_ALERT_ID, color='danger', is_open=(err_msg is not None), class_name='mb-1')
    jobid_div = html.Div("", id=_COMMIT_JOBID_DIV, style=dict(display='none'))
    cancel_btn = dbc.Button("Cancel", id=_COMMIT_CANCEL_BTN, n_clicks=0, class_name='mr-2')
    submit_btn = dbc.Button("Submit", id=_COMMIT_SUBMIT_BTN, n_clicks=0)
    return dbc.Modal([hdr, dbc.ModalBody([alert, jobid_div, card]), dbc.ModalFooter([cancel_btn, submit_btn])],
                     id=_COMMIT_ID, backdrop="static", size="xl", is_open=False)


_COMMIT_ID = 'commit-modal'
""" ID of Bootstrap 'new commit' Modal component, by which user initiates a new experiment session commit job. """
_COMMIT_ALERT_ID = 'commit-modal-alert'
""" ID of Bootstrap Alert for displaying an error message within the 'new commit' modal. """
_COMMIT_JOBID_DIV = 'commit-jobid-div'
""" 
ID of hidden Div in which the job ID for a newly created commit job is stored temporarily. Setting the job ID here
triggers raising the upload modal so that user can upload session archive for the new commit.
"""
_COMMIT_CANCEL_BTN = 'commit-modal-cancel'
""" ID of 'Cancel' button in the footer of the 'new commit' modal. """
_COMMIT_SUBMIT_BTN = 'commit-modal-submit'
""" ID of 'Submit' button in the footer of the 'new commit' modal. """


def _layout_session_metadata_card() -> Tuple[dbc.Card, Optional[str]]:
    err_msg: Optional[str] = None

    experimenters = fetch_attribute_values(DBTable.USER, 'username')
    experimenters.sort()
    if len(experimenters) == 0:
        err_msg = "Error - Falied to retrieve user list from database."
    subjects = fetch_attribute_values(DBTable.SUBJECT, "subj_id")
    subjects.sort()
    if (not err_msg) and (len(subjects) == 0):
        err_msg = "Error - Failed to retrieve subject list from database."
    rigs = fetch_attribute_values(DBTable.RIG, "rig_id")
    rigs.sort()
    if (not err_msg) and (len(rigs) == 0):
        err_msg = "Error - Failed to retrieve rig list from database."
    studies = fetch_restrict_proj([DBTable.STUDY], None, ['study_title'])
    if studies is None:
        studies = []
        if not err_msg:
            err_msg = "Error - Failed to retrieve study list from database."
    studies.sort(key=lambda x: x['study_title'])
    brain_areas = fetch_restrict_proj([DBTable.BRAIN_AREA], None, ['ba_name'])
    if brain_areas is None:
        brain_areas = []
        if not err_msg:
            err_msg = "Error - Failed to retrieve brain area list from database."
    brain_areas.sort(key=lambda x: x['ba_name'])
    neuron_types = fetch_attribute_values(DBTable.NEURON_TYPE, "nt_name")
    if len(neuron_types) == 0:
        err_msg = "Error - Failed to retreive neuron types list from database."

    # Widgets for attributes in Session table... NOTE that this has to work even if an error occurs above while
    # retrieving information.
    initial_value = experimenters[0] if (len(experimenters) > 0) else None
    experimenter_group = dbc.InputGroup([
        dbc.InputGroupText("Experimenter"),
        dbc.Select(id=_EXPERIMENTER_SELECT_ID, options=[{"label": user, "value": user} for user in experimenters],
                   value=initial_value)
    ], size='sm')
    initial_value = subjects[0] if (len(subjects) > 0) else None
    subject_group = dbc.InputGroup([
        dbc.InputGroupText("Subject"),
        dbc.Select(id=_SUBJECT_SELECT_ID, options=[{"label": subject, "value": subject} for subject in subjects],
                   value=initial_value)
    ], size='sm')
    initial_value = rigs[0] if (len(rigs) > 0) else None
    rig_group = dbc.InputGroup([
        dbc.InputGroupText("Rig"),
        dbc.Select(id=_RIG_SELECT_ID, options=[{"label": rig, "value": rig} for rig in rigs], value=initial_value)
    ], size='sm')
    initial_value = studies[0]['study_id'] if (len(studies) > 0) else None
    study_group = dbc.InputGroup([
        dbc.InputGroupText("Study"),
        dbc.Select(id=_STUDY_SELECT_ID,
                   options=[{"label": opt['study_title'], "value": opt['study_id']} for opt in studies],
                   value=initial_value)
    ], size='sm')
    date_group = dbc.InputGroup([
        dbc.InputGroupText("Recorded On"),
        dcc.DatePickerSingle(id=_RECORD_DATE_ID, display_format='YYYY-MM-DD')
    ], size='sm')
    suffix_group = dbc.InputGroup([
        dbc.InputGroupText("Suffix (0-9)"),
        dbc.Input(id=_SUFFIX_INPUT_ID, type='number', minlength=1, maxlength=1, value=1)
    ], size='sm')
    notes_group = dbc.InputGroup([
        dbc.InputGroupText("Session Notes"),
        dbc.Textarea(id=_NOTES_AREA_ID, minlength=0, maxlength=2048, rows=4,
                     value=None,
                     placeholder='Enter any notes about this particular session (optional, up to 2048 chars)')
    ], size='sm')

    row_1 = dbc.Row([dbc.Col(date_group, width=3), dbc.Col(suffix_group, width=2),
                     dbc.Col(subject_group, width=3), dbc.Col(rig_group, width=2)], className='mx-1 mt-2 mb-2')
    row_2 = dbc.Row([dbc.Col(experimenter_group, width=4), dbc.Col(study_group, width=8)], className='mx-1 mb-2')
    row_3 = dbc.Row(dbc.Col(notes_group, width=12), className='mx-1 mb-3')

    # Widgets for attributes in Session.EPhys, plus an editable Datatable to specify the neuron type for each recorded
    # neural unit. All EPhys attribute widgets are disabled when # of recorded units is 0.
    num_units_group = dbc.InputGroup([
        dbc.InputGroupText("# Units Recorded"),
        dbc.Input(id=_NUM_UNITS_INPUT_ID,  type='number', minlength=1, maxlength=3, value=0)
    ], size='sm')

    # the neuron types table: The neuron type for each recorded unit is editable via a dropdown menu which lets user
    # select any neuron tye present in the database. We use the DataTable's 'css' property to modify the default
    # styling of the embedded dropdown renderer.
    nt_table = dt.DataTable(
        id=_NT_TABLE_ID,
        columns=[
            {"name": "Unit", "id": "index", "presentation": "markdown", "editable": False},
            {"name": "Neuron Type", "id": "nt_name", "presentation": "dropdown"}
        ],
        data=[],
        dropdown={
            "nt_name": {
                "clearable": False,
                "options": [{'label': t, 'value': t} for t in neuron_types]
            }
        },
        editable=True,
        row_selectable=False,
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[
            {'if': {'column_id': 'index'}, 'width': 100},
            {'if': {'column_id': 'nt_name'}, 'width': 300}
        ],
        tooltip_data=None, tooltip_duration=None,
        css=[
            {"selector": ".dash-spreadsheet-container .Select-value-label",
             "rule": "font-family: sans-serif; color: var(--bs-body-color)"},
            {"selector": ".dash-spreadsheet .Select-option", "rule": "font-family: sans-serif; color: steelblue"},
            {"selector": ".dash-spreadsheet .Select-menu-outer", "rule": "border: thin steelblue solid"},
            {"selector": ".dash-spreadsheet .Select-arrow", "rule": "border-top-color: var(--muted)"},
            {"selector": ".dash-spreadsheet .Select-control:hover .Select-arrow",
             "rule": "border-top-color: steelblue"},
            {"selector": ".dash-spreadsheet .is-open > .Select-control .Select-arrow",
             "rule": "border-bottom-color: steelblue"},
            {"selector": ".dash-spreadsheet .Select-option.is-focused",
             "rule": "background-color: steelblue; color: white"}
        ],
        style_table={'height': '300px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )
    nt_div_with_dropdown_container = html.Div([
        nt_table,
        html.Div(id=f"{_NT_TABLE_ID}-container")
    ])

    source_options = attribute_info(DBTable.SESSION_EPHYS, 'ephys_src').options
    rec_src_group = dbc.InputGroup([
        dbc.InputGroupText("Recording Source"),
        dbc.Select(id=_RECORDING_SRC_SELECT_ID, disabled=True,
                   options=[{"label": opt, "value": opt} for opt in source_options], value=source_options[0])
    ], size='sm')
    probe_type_options = attribute_info(DBTable.SESSION_EPHYS, 'probe_type').options
    probe_type_group = dbc.InputGroup([
        dbc.InputGroupText("Probe Type"),
        dbc.Select(id=_PROBE_TYPE_SELECT_ID, disabled=True,
                   options=[{"label": opt, "value": opt} for opt in probe_type_options], value=probe_type_options[0])
    ], size='sm')
    rate_group = dbc.InputGroup([
        dbc.InputGroupText("Sampling Rate (Hz)"),
        dbc.Input(id=_PROBE_RATE_INPUT_ID, disabled=True, type='number', minlength=2, maxlength=10, value=40000)
    ], size='sm')
    probe_x_group = dbc.InputGroup([
        dbc.InputGroupText("Probe X (mm)"),
        dbc.Input(id=_PROBE_X_INPUT_ID, disabled=True, type='number', minlength=2, maxlength=10, value=10)
    ], size='sm')
    probe_y_group = dbc.InputGroup([
        dbc.InputGroupText("Probe Y (mm)"),
        dbc.Input(id=_PROBE_Y_INPUT_ID, disabled=True, type='number', minlength=2, maxlength=10, value=10)
    ], size='sm')
    probe_z_group = dbc.InputGroup([
        dbc.InputGroupText("Probe Depth (mm)"),
        dbc.Input(id=_PROBE_Z_INPUT_ID, disabled=True, type='number', minlength=2, maxlength=10, value=10)
    ], size='sm')
    initial_value = brain_areas[0]['ba_id'] if (len(brain_areas) > 0) else None
    brain_area_group = dbc.InputGroup([
        dbc.InputGroupText("Brain Area"),
        dbc.Select(id=_AREA_SELECT_ID, disabled=True,
                   options=[{"label": opt['ba_name'], "value": opt['ba_id']} for opt in brain_areas],
                   value=initial_value)
    ], size='sm')

    ephys_label = "Electrophysiology (SKIP if no neural units were recorded)"
    divider = dbc.Row([
        dbc.Col(dbc.Label(ephys_label, size='sm'), width=5), dbc.Col(html.Hr(), width=7)
    ], class_name='mx-1 mb-2')

    unit_rows = [
        dbc.Row(dbc.Col(num_units_group, width="auto"), class_name='mx-1 mb-2'),
        dbc.Row(dbc.Col(nt_div_with_dropdown_container), class_name='mx-1 mb-2')
    ]
    ephys_rows = [
        dbc.Row(dbc.Col(brain_area_group, width="auto"), class_name='mx-1 mb-3'),
        dbc.Row(dbc.Col(rec_src_group, width="auto"), class_name='mx-1 mb-3'),
        dbc.Row(dbc.Col(probe_type_group, width="auto"), class_name='mx-1 mb-3'),
        dbc.Row(dbc.Col(rate_group, width="auto"), class_name='mx-1 mb-3'),
        dbc.Row(dbc.Col(probe_x_group, width="auto"), class_name='mx-1 mb-3'),
        dbc.Row(dbc.Col(probe_y_group, width="auto"), class_name='mx-1 mb-3'),
        dbc.Row(dbc.Col(probe_z_group, width="auto"), class_name='mx-1 mb-3')
    ]

    row_4 = dbc.Row([dbc.Col(unit_rows, width=6), dbc.Col(ephys_rows, width=6)], class_name='mx-1 mt-2')

    return dbc.Card([row_1, row_2, row_3, divider, row_4], class_name='mt-2'), err_msg


_EXPERIMENTER_SELECT_ID = 'commit-experimenter-select'
""" ID of the Bootstrap Select that chooses the session experimenter (a username) for a new session commit job. """
_SUBJECT_SELECT_ID = 'commit-subject-select'
""" ID of the Bootstrap Select that chooses the ID of the experiment subject for a new session commit job. """
_RIG_SELECT_ID = 'commit-rig-select'
""" ID of the Bootstrap Select that chooses the ID of the experiment rig for a new session commit job. """
_STUDY_SELECT_ID = 'commit-study-select'
""" ID of the Bootstrap Select that chooses the ID of the research study for a new session commit job. """
_RECORD_DATE_ID = 'commit-date-picker'
""" ID of the Dash DatePicker component that sets the session recording date for a new session commit job.  """
_SUFFIX_INPUT_ID = 'commit-suffix-input'
""" ID of the Bootstrap Input component that sets the session suffix for a new session commit job. """
_NOTES_AREA_ID = 'commit-notes-area'
""" ID of the Bootstrap TextArea component for entering session notes for a new session commit job. """
_NUM_UNITS_INPUT_ID = 'commit-numunits-input'
""" ID of the Bootstrap Input that sets how many neural units were recorded in the session (0 = behavior only). """
_NT_TABLE_ID = 'commit-ntype-table'
""" ID of Dash Datatable in which user selects the neuron type assigned to each neural unit recorded during session. """
_AREA_SELECT_ID = 'commit-area-select'
""" ID of the Bootstrap Select that chooses the relevant brain region for a new session commit job. """
_RECORDING_SRC_SELECT_ID = 'commit-rec-src-select'
""" ID of the Bootstrap Select that chooses the EPhys recording source for a new session commit job. """
_PROBE_TYPE_SELECT_ID = 'commit-probe-type-select'
""" ID of the Bootstrap Select that chooses the EPhys probe type for a new session commit job. """
_PROBE_RATE_INPUT_ID = 'commit-probe-rate-input'
""" ID of the Bootstrap Input component that sets the electrode sampling rate for a new session commit job. """
_PROBE_X_INPUT_ID = 'commit-probe-x-input'
""" ID of the Bootstrap Input component that sets the probe x-coordinate for a new session commit job. """
_PROBE_Y_INPUT_ID = 'commit-probe-y-input'
""" ID of the Bootstrap Input component that sets the probe y-coordinate for a new session commit job. """
_PROBE_Z_INPUT_ID = 'commit-probe-z-input'
""" ID of the Bootstrap Input component that sets the probe depth for a new session commit job. """


@callback(
    [Output(_NT_TABLE_ID, 'data'), Output(_AREA_SELECT_ID, 'disabled'), Output(_RECORDING_SRC_SELECT_ID, 'disabled'),
     Output(_PROBE_TYPE_SELECT_ID, 'disabled'), Output(_PROBE_RATE_INPUT_ID, 'disabled'),
     Output(_PROBE_X_INPUT_ID, 'disabled'), Output(_PROBE_Y_INPUT_ID, 'disabled'),
     Output(_PROBE_Z_INPUT_ID, 'disabled')],
    [Input(_NUM_UNITS_INPUT_ID, 'value')], [State(_NT_TABLE_ID, 'data')]
)
def on_num_units_changed(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    num_units: Optional[int] = None
    try:
        num_units = int(args[0])
    except ValueError:
        pass
    if num_units is None:
        raise dash_exc.PreventUpdate

    table_rows: List[Dict] = args[1]
    if len(table_rows) == num_units:
        raise dash_exc.PreventUpdate

    if len(table_rows) > num_units:
        table_rows = table_rows[0:num_units]
    else:
        # we're assuming here that the database contains neuron type 'Unspecified'
        table_rows.extend([{'index': i+1, 'nt_name': 'Unspecified'} for i in range(len(table_rows), num_units)])

    out: List[Any] = [table_rows]
    for _ in range(7):
        out.append((num_units == 0))
    return tuple(out)


def _create_upload_modal() -> dbc.Modal:
    hdr, body, footer = _layout_upload_modal()
    return dbc.Modal([hdr, body, footer], id=_UPLOAD_ID, backdrop="static", size="xl", is_open=False)


def _layout_upload_modal(job_id: Optional[str] = None) -> Tuple[dbc.ModalHeader, dbc.ModalBody, dbc.ModalFooter]:
    hdr = dbc.ModalHeader(dbc.ModalTitle(f"Upload session archive for: {job_id}"))
    body_kids = []
    if isinstance(job_id, str) and (len(job_id) > 0):
        body_kids = [
            dcc.Markdown('''
            * Before you begin, all session data files (Maestro and Omniplex) must be compressed into a single, flat
            ZIP archive (no subdirectories). Maximum supported file size is 10GB.
            * If the session includes behavioral data only, the archive should contain only the Maestro data files.
            * There is no support at this time for automatic spike sorting. For electrophysiological recordings, the
            experimenter must supply neural unit data (spike trains) in a pickle file (.pkl or .pickle). This must be
            the only pickle file in the archive.
            * The pickle file must contain a single dictionary with 3 keys: 'filename', 'channel', and 'spiketimes'.
            Each key value is a list of length N = the number of neural units. These contain the Omniplex PL2
            filenames, the source channel IDs ('WBnn' or 'SPKCnn'), and the spike timestamps (in seconds since the
            Omniplex recording started) for each neural unit. The 'filename' field may be omitted if all units were
            recorded in a single Omniplex file.

            *Drag and drop the ZIP file onto the upload component below, or click on the component to browse the file
            system for the file. The upload should start automatically. Large (>1GB) archives will take a significant
            amount of time to upload, depending on network speed. This pop-up window will close automatically when the
            upload finishes. **Do NOT close this pop-up window and do NOT close the browser tab while the upload is in
            progress**.*
            '''),
            html.Div(du.Upload(id=_UPLOADER_ID, max_file_size=10000, chunk_size=100, max_files=1, cancel_button=False,
                               filetypes=['zip'], upload_id=job_id), className="mt-2")
        ]
    body = dbc.ModalBody(body_kids)
    footer = dbc.ModalFooter([
        dbc.Button("Cancel", id=_UPLOAD_CANCEL_BTN, n_clicks=0, class_name='mr-2'),
        dbc.Button("Done", id=_UPLOAD_DONE_BTN, n_clicks=0, disabled=True)
    ])
    return hdr, body, footer


_UPLOAD_CANCEL_BTN = 'commit-upload-cancel'
""" ID of 'Cancel' button in the footer of the 'upload archive' modal. """
_UPLOAD_DONE_BTN = 'commit-upload-done'
""" ID of 'Done' button in the footer of the 'upload archive' modal. """
_UPLOADER_ID = 'commit-uploader'
""" ID of the Dash Uploader component that manages the uploading of a session archive ZIP. """


@callback(
    [Output(_COMMIT_ID, 'is_open'), Output(_COMMIT_ALERT_ID, 'is_open'), Output(_COMMIT_ALERT_ID, 'children'),
     Output(_COMMIT_JOBID_DIV, 'children')],
    [Input(_START_BTN_ID, 'n_clicks'), Input(_COMMIT_CANCEL_BTN, 'n_clicks'), Input(_COMMIT_SUBMIT_BTN, 'n_clicks')],
    [State(_EXPERIMENTER_SELECT_ID, "value"), State(_SUBJECT_SELECT_ID, "value"), State(_RIG_SELECT_ID, "value"),
     State(_RECORD_DATE_ID, "date"), State(_SUFFIX_INPUT_ID, "value"), State(_STUDY_SELECT_ID, "value"),
     State(_NOTES_AREA_ID, "value"), State(_RECORDING_SRC_SELECT_ID, "value"), State(_PROBE_TYPE_SELECT_ID, "value"),
     State(_PROBE_RATE_INPUT_ID, "value"), State(_PROBE_X_INPUT_ID, "value"), State(_PROBE_Y_INPUT_ID, "value"),
     State(_PROBE_Z_INPUT_ID, "value"), State(_AREA_SELECT_ID, "value"), State(_NT_TABLE_ID, "data")]
)
def update_commit_modal(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]

    # go ahead and close the commit modal if current user is not authorized to do commits. Should never happen!
    committer = _get_current_username()
    if committer is None:
        return False, False, "", no_update

    if trigger == _START_BTN_ID:
        return True, False, "", no_update
    elif trigger == _COMMIT_CANCEL_BTN:
        return False, False, "", no_update
    else:   # _COMMIT_SUBMIT_BTN -- submit request to start a new commit
        rows = args[-1]
        unit_types = [r['nt_name'] for r in rows]
        ofs = 3
        ok, job_id = initiate_session_commit(
            is_api=False, committer=committer, unit_types=unit_types, experimenter=args[ofs], subject=args[ofs+1],
            rec_date=args[ofs+3], suffix=int(args[ofs+4]), rig=args[ofs+2], study=int(args[ofs+5]),
            notes="None" if not isinstance(args[ofs+6], str) else args[ofs+6],
            brain_area=int(args[ofs+13]), src=args[ofs+7], probe=args[ofs+8], rate=float(args[ofs+9]),
            x=float(args[ofs+10]), y=float(args[ofs+11]), z=float(args[ofs+12])
        )
        if not ok:
            return no_update, True, job_id, no_update
        else:
            return False, False, "", job_id


@callback(
    [Output(_UPLOAD_DONE_BTN, 'disabled'), Output(_UPLOAD_DONE_BTN, 'children')],
    [Input(_UPLOADER_ID, 'isCompleted'), Input(_UPLOADER_ID, 'fileNames')], [State(_UPLOADER_ID, "upload_id")]
)
def on_upload_started_or_finished(is_completed, file_names, upload_id):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    if not is_completed:
        if file_names is not None:
            get_application_logger().debug(f"Upload initiated on client, upload_id={upload_id}, "
                                           f"file_names={file_names}")
        return True, "...Uploading..."
    else:
        get_application_logger().debug(f"Upload completed on client, upload_id={upload_id}, file={file_names}")
        return False, "Done"


@callback(
    [Output(_UPLOAD_ID, "is_open"), Output(_UPLOAD_ID, "children"), Output(_ERR_DIV2_ID, "children")],
    [Input(_COMMIT_JOBID_DIV, "children"), Input(_UPLOAD_CANCEL_BTN, "n_clicks"), Input(_UPLOAD_DONE_BTN, "n_clicks")],
    [State(_COMMIT_JOBID_DIV, "children")]
)
def show_hide_upload_modal(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _COMMIT_JOBID_DIV:
        job_id = args[0]
        if not (isinstance(job_id, str) and (len(job_id) > 0)):
            raise dash_exc.PreventUpdate
        else:
            return True, _layout_upload_modal(job_id), no_update
    else:
        job_id = args[-1]
        if trigger == _UPLOAD_CANCEL_BTN:
            _, err_msg, _ = cancel_or_remove_commit_job(job_id)
            return False, [], err_msg if len(err_msg) > 0 else no_update
        else:   # _UPLOAD_DONE_BTN
            res = on_archive_uploaded_to_workspace(job_id)
            return False, [], res if isinstance(res, str) else no_update
