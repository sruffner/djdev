"""
commit.py: The "commit session" page in web-based interface to the Lisberger laboratory database.

This web page displays all pending session commit jobs for the current login user with 'commit' level access. The user
can check the status of any pending jobs, initiate a new job by uploading an experiment session archive to the server,
review a candidate session after the archive has been pre-processed, submit the reviewed session to be committed to the
database, cancel any failed or in-progress commit, or remove any completed commit jobs.

Preprocessing very large session archives and committing the data to the portal database can take many seconds or even
minutes, depending on the archive size. In between is a user-interactive "review" stage in which the user must review
the results of the preprocessing stage and possibly add some additional information before committing the session to
the database. To keep the web front end responsive, the server queues background "workers" (Redis Queue, or RQ) to
handle the pre-processing and final commit stages.

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
import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from dash import callback_context, callback, exceptions as dash_exc, no_update, dash_table as dt, html, dcc, Input, \
    Output, State
import dash_bootstrap_components as dbc
import dash_uploader as du
import flask_login
import plotly.express as px

from app import load_authorized_user
from database import maestro
from database.commit_ops import CommitStateEnum, initiate_session_commit, get_pending_commit_jobs_for, \
    cancel_or_remove_commit_job, update_commit_job_on_archive_upload, commit_job_progress, CommitJobStatus, \
    SessionMetaData, session_metadata, update_session_metadata, ready_to_commit, protocol_names, protocol_definition, \
    add_rv_to_protocol, validate_protocol, OmniplexUnit, metrics_for_neural_unit, set_unit_type, commit_to_database
from database.table_info import Column, DBTable, attribute_info
from database.table_ops import fetch_restrict_proj, fetch_attribute_values, fetch_rows

logger = logging.getLogger(__name__)


_JOBS_TABLE_COLS: List[Column] = [
    Column('job', 'Job ID/Archive File', '170px', True),
    Column('started', 'Started', '100px', True),
    Column('status_desc', 'Status', '100px', True),
    Column('last_update', 'Last Update', '330px', True)
]
""" Defined columns for the pending commit jobs table. """

_JOBS_TABLE_ID = 'jobs-table'
""" A Dash DataTable listing all pending commit jobs for the currently authenticated user. """
_ALERT_ID = 'commit-alert'
""" ID of Bootstrap Alert that is used to display an error message if a request on this page fails. """
_START_BTN_ID = 'commit-start-btn'
""" ID of the button that initiates a new session commit job and reveals the uploader component. """
_REFRESH_BTN_ID = 'jobs-refresh'
""" User presses this button to refresh status information on all pending commit jobs. """
_MESSAGE_BTN_ID = 'job-messages'
""" User presses this button to view the progress message history for the currently selected commit job. """
_REVIEW_BTN_ID = 'job-review'
"""
User presses this button to review a job after preprocessing. This raises a modal by which user interactively
adds or edits selected session information, then starts the actual database commit.
"""
_REMOVE_BTN_ID = 'job-remove'
""" User presses this button to cancel and/or remove the currently selected commit job. """
_UPLOAD_ID = 'commit-upload-modal'
""" ID of Modal component by which user uploads the data archive for a new session commit job. """
_UPLOADER_ID = 'commit-uploader'
""" ID of the Dash Uploader component that manages the uploading of a session archive ZIP. """
_CLOSE_UPLOAD_ID = 'upload-modal-close'
"""
ID of button that closes the Modal component by which a new session archive is uploaded to server. The Modal should
never be closed WHILE an upload is in progress, as that will mess up the Uploader. If the upload finishes, the Modal
is closed automatically.
"""
_HISTORY_ID = 'message-history'
""" ID of Modal component displaying the progress message history for a pending commit job. """
_HISTORY_HEADER_ID = 'message-history-header'
""" ID of the header of the Modal component displaying the progress message history. The commit job ID goes here. """
_HISTORY_MARKDOWN_ID = 'message-history-markdown'
""" ID of the Dash Markdown component in which the progress message history fo ra pending commit job is listed. """
_CLOSE_HISTORY_ID = 'message-history-close'
""" ID of button that closes the Modal component displaying the progress message history for a pending commit job. """


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
            logger.debug("On commit page, but client not authenticated or lacks commit access.")
    return username


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


def _job_table_row_from_job_status_info(job: CommitJobStatus) -> Dict[str, Any]:
    start_ts = datetime.fromtimestamp(job.started).strftime("%Y-%m-%d %I:%M:%S %p")
    update_ts = datetime.fromtimestamp(job.updated).strftime("%Y-%m-%d %I:%M:%S %p")
    file_name = "---" if job.state == CommitStateEnum.UPLOADING else job.zip
    return {
        'id': job.id, 'state': job.state.value,  # these two fields are not displayed
        'job': f"{job.id}\nArchive: **{file_name}**",
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


def serve_layout() -> html.Div:
    """
    Serve the layout for the "commit" page displaying the queue of commit jobs belonging to the currently authenticated
    user. The page includes a means of starting a new commit job by uploading the session archive (ZIP file) to the
    portal. Status information on pending jobs are displayed in tabular form. Button contols under the table let the
    user refresh the table contents, show the message history of a selected job, cancel/remove a selected job, review
    the results of a pre-processed commit, and submit the commit to the database after review.

    Returns:
        An HTML Div rendering the "commit" page.
    """
    err_msg, jobs = _commit_jobs_for_current_user()
    upload_in_progress = any([j['state'] == CommitStateEnum.UPLOADING.value for j in jobs])

    jobs_table = _table_of_pending_commit_jobs(jobs)
    alert = dbc.Alert(err_msg if err_msg else "", id=_ALERT_ID, color="danger", dismissable=True, fade=True,
                      duration=10000, is_open=(err_msg is not None))
    start_btn = dbc.Button("New commit...", id=_START_BTN_ID, color="primary", n_clicks=0, disabled=upload_in_progress)
    refresh_btn = dbc.Button("Refresh", id=_REFRESH_BTN_ID, color="primary", n_clicks=0, className='mr-4')
    message_btn = dbc.Button("Messages...", id=_MESSAGE_BTN_ID, color="primary", n_clicks=0, disabled=True,
                             className='mr-1')
    next_btn = dbc.Button("Review & Commit", id=_REVIEW_BTN_ID, color="primary", n_clicks=0, disabled=True,
                          className='mr-1')
    remove_btn = dbc.Button("Cancel/Remove", id=_REMOVE_BTN_ID, color="primary", n_clicks=0, disabled=True)
    button_form = dbc.Form(
        [
            dbc.FormGroup([refresh_btn, start_btn], className="mt-3 mb-2 mr-4"),
            dbc.FormGroup([message_btn, next_btn, remove_btn], className="mt-2 mb-2 ml-auto")
        ],
        inline=True,
    )

    # NOTE that we assign a UUID as the upload ID. SO, if user reloads the page, that will change!
    upload_modal = dbc.Modal([
        dbc.ModalHeader("Upload session data archive"),
        dbc.ModalBody([
            dcc.Markdown('''
            * Before you begin, all session data files (Maestro and Omniplex) must be compressed into a single, flat
            ZIP archive (no subdirectories). Maximum supported file size is 10GB.
            * If the session includes behavioral data only, the archive should contain only the Maestro data files.
            * There is no support at this time for automatic spike sorting. For electrophysiological recordings, the
            experimenter must supply neural unit data (spike trains) in a pickle file (.pkl or .pickle). This must be
            the only pickle file in the archive.
            * The pickle file must contain a single dictionary: {'filename': [...], 'channel': [...],
            'spiketimes': [...]}, where each value is a list of length N = the number of neural units. These contain the
             Omniplex PL2 filenames, the source channel IDs ('WBnn' or 'SPKCnn'), and the spike timestamps (in seconds
             since the Omniplex recording started) for each neural unit. The 'filename' field may be omitted if all 
             units were recorded in a single Omniplex file.

            *Drag and drop the ZIP file onto the upload component below, or click on the component to browse the file
            system for the file. The upload should start automatically. Large (>1GB) archives will take a significant
            amount of time to upload, depending on network speed. This pop-up window will close automatically when the
            upload finishes. **Do NOT close this pop-up window and do NOT close the browser tab while the upload is in
            progress**.*
            '''),
            html.Div(du.Upload(id=_UPLOADER_ID, max_file_size=10000, chunk_size=100, max_files=1, cancel_button=True,
                               filetypes=['zip'], upload_id=str(uuid.uuid1())), className="mt-2")
        ]),
        dbc.ModalFooter(dbc.Row([dbc.Button("Close", id=_CLOSE_UPLOAD_ID, color="primary", n_clicks=0)]))
    ], id=_UPLOAD_ID, backdrop="static", size="xl", is_open=False)

    messages_modal = dbc.Modal(
        [
            dbc.ModalHeader(id=_HISTORY_HEADER_ID),
            dbc.ModalBody(dcc.Markdown(id=_HISTORY_MARKDOWN_ID)),
            dbc.ModalFooter(dbc.Row([dbc.Button("Close", id=_CLOSE_HISTORY_ID, color="primary", n_clicks=0)]))
        ],
        id=_HISTORY_ID, backdrop=False, size="lg", is_open=False
    )

    review_modal = dbc.Modal(_layout_review_modal(None), id=_REVIEW_ID, backdrop=False, size="xl", is_open=False)

    card = dbc.Card([
        dbc.CardHeader("Experiment session commits in progress"),
        dbc.CardBody([alert, button_form, jobs_table]),
    ], className='w-75 mx-auto mt-5')

    return html.Div([card, upload_modal, messages_modal, review_modal])


def _layout_review_modal(job_id: Optional[str] = None) -> Tuple[dbc.ModalHeader, dbc.ModalBody, dbc.ModalFooter]:
    info_tab_content, num_units, err_msg = _layout_session_info_tab_content(job_id)
    proto_tab_content, err_temp = _layout_trial_protocol_tab_content(job_id)
    if (not err_msg) and err_temp:
        err_msg = err_temp
    unit_tab_content, err_temp = _layout_neural_units_tab_content(job_id, num_units)
    if (not err_msg) and err_temp:
        err_msg = err_temp
    tabs = dbc.Tabs(
        [
            dbc.Tab(info_tab_content, label="General Info"),
            dbc.Tab(proto_tab_content, label="Trial Protocols"),
            dbc.Tab(unit_tab_content, label="Neural Units", disabled=(num_units == 0))
        ]
    )

    if err_msg:
        alert_color, alert_msg = 'danger', err_msg
    elif job_id:
        ok, ready, alert_msg = ready_to_commit(job_id)
        alert_color = 'danger' if (not ok) else ('success' if ready else 'warning')
    else:
        alert_color, alert_msg = 'danger', 'No job loaded'
    alert = dbc.Alert(alert_msg, id=_REVIEW_ALERT_ID, color=alert_color, dismissable=False, is_open=True,
                      className='mb-2')

    # we use these hidden DIVs to trigger updates to the Bootstrap Alert and the "Commit" button enable state in
    # response to user interactions on the Review modal.
    meta_alert_div = html.Div("", id=_META_ALERT_DIV, style=dict(display='none'))
    proto_alert_div = html.Div("", id=_PROTO_ALERT_DIV, style=dict(display='none'))
    unit_alert_div = html.Div("", id=_UNIT_ALERT_DIV, style=dict(display='none'))
    commit_alert_div = html.Div("", id=_COMMIT_ALERT_DIV, style=dict(display='none'))

    header = dbc.ModalHeader(f"Review & commit: {job_id}", id=_REVIEW_HEADER_ID)
    body = dbc.ModalBody([alert, tabs, meta_alert_div, proto_alert_div, unit_alert_div, commit_alert_div])
    footer = dbc.ModalFooter([
        dbc.Button("Commit", id=_REVIEW_COMMIT_ID, color="primary", n_clicks=0, disabled=(alert_color != 'success'),
                   className='mr-2'),
        dbc.Button("Close", id=_REVIEW_CLOSE_ID, color="primary", n_clicks=0)
    ])
    return header, body, footer


_REVIEW_ID = 'review-modal'
""" 
ID of Modal "Review" component by which user reviews and edits session information for a pending commit job, then
initiates the actual commit.
"""
_REVIEW_HEADER_ID = 'review-header'
""" ID of the header of the Modal "Review" component. """
_REVIEW_BODY_ID = 'review-body'
""" ID of the body of the Modal "Review" component. """
_REVIEW_CLOSE_ID = 'review-close-btn'
""" ID of button that closes the Modal "Review" component. """
_REVIEW_COMMIT_ID = 'review-commit-btn'
""" ID of button that triggers the actual commit of the experiment session to the database. """

_META_ALERT_DIV = "meta-alert-div"
"""
Hidden DIV used to trigger refresh of the Alert content and Commit button enable state when something changes (or
an error occurs) while user interacts with the General Info tab of the Review modal.
"""
_PROTO_ALERT_DIV = "proto-alert-div"
"""
Hidden DIV used to trigger refresh of the Alert content and Commit button enable state when something changes (or
an error occurs) while user interacts with the Trial Protocols tab of the Review modal.
"""
_UNIT_ALERT_DIV = "unit-alert-div"
"""
Hidden DIV used to trigger refresh of the Alert content and Commit button enable state when something changes (or
an error occurs) while user interacts with the Neural Units tab of the Review modal.
"""
_COMMIT_ALERT_DIV = 'commit-alert-div'
"""
Hidden DIV used to trigger refresh of the Alert content and Commit button enable state when the user clicks the 
Commit button on the Review modal to finalize a commit, but something goes wrong on the server.
"""

_REVIEW_ALERT_ID = 'review-alert'
""" ID of Bootstrap alert that displays messages in the body of the Modal "Review" component. """
_EXPERIMENTER_SELECT_ID = 'review-experimenter-select'
""" ID of the Bootstrap Select that chooses the session experimenter (a username) for the commit job under review. """
_SUBJECT_SELECT_ID = 'review-subject-select'
""" ID of the Bootstrap Select that chooses the ID of the experiment subject for the commit job under review. """
_RIG_SELECT_ID = 'review-rig-select'
""" ID of the Bootstrap Select that chooses the ID of the experiment rig for the commit job under review. """
_STUDY_SELECT_ID = 'review-study-select'
""" ID of the Bootstrap Select that chooses the ID of the research study for the commit job under review. """
_RECORD_DATE_ID = 'review-date-picker'
""" ID of the Dash DatePicker component that sets the session recording date for the commit job under review. """
_SUFFIX_INPUT_ID = 'review-suffix-input'
""" ID of the Bootstrap Input component that sets the session suffix for the commit job under review. """
_NOTES_AREA_ID = 'review-notes-area'
""" ID of the Bootstrap TextArea component displaying session notes for the commit job under review. """
_RECORDING_SRC_SELECT_ID = 'review-rec-src-select'
""" ID of the Bootstrap Select that chooses the EPhys recording source for the commit job under review. """
_PROBE_TYPE_SELECT_ID = 'review-probe-type-select'
""" ID of the Bootstrap Select that chooses the EPhys probe type for the commit job under review. """
_PROBE_RATE_INPUT_ID = 'review-probe-rate-input'
""" ID of the Bootstrap Input component that sets the electrode sampling rate for the commit job under review. """
_PROBE_X_INPUT_ID = 'review-probe-x-input'
""" ID of the Bootstrap Input component that sets the probe x-coordinate for the commit job under review. """
_PROBE_Y_INPUT_ID = 'review-probe-y-input'
""" ID of the Bootstrap Input component that sets the probe y-coordinate for the commit job under review. """
_PROBE_Z_INPUT_ID = 'review-probe-z-input'
""" ID of the Bootstrap Input component that sets the probe depth for the commit job under review. """
_AREA_SELECT_ID = 'review-area-select'
""" ID of the Bootstrap Select that chooses the relevant brain region for the commit job under review. """

_UPDATE_META_ID = 'review-update-meta-btn'
""" ID of button that triggers an update of session metadata, harvesting values in the "Review" modal. """


def _layout_session_info_tab_content(job_id: Optional[str]) -> Tuple[dbc.Card, int, Optional[str]]:
    info: Optional[SessionMetaData] = None
    err_msg: Optional[str] = None
    if job_id:
        info = session_metadata(job_id)
        if not info:
            err_msg = "Error - Failed to retrieve cached session metadata from server"

    experimenters = fetch_attribute_values(DBTable.USER, 'username')
    experimenters.sort()
    if (not err_msg) and (len(experimenters) == 0):
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

    # Widgets for attributes in Session table...
    experimenter_group = dbc.InputGroup([
        dbc.InputGroupAddon("Experimenter", addon_type="prepend"),
        dbc.Select(id=_EXPERIMENTER_SELECT_ID,
                   options=[{"label": user, "value": user} for user in experimenters],
                   value=info.experimenter if info else None)
    ], size='sm')
    subject_group = dbc.InputGroup([
        dbc.InputGroupAddon("Subject", addon_type="prepend"),
        dbc.Select(id=_SUBJECT_SELECT_ID,
                   options=[{"label": subject, "value": subject} for subject in subjects],
                   value=info.subj_id if info else None)
    ], size='sm')
    rig_group = dbc.InputGroup([
        dbc.InputGroupAddon("Rig", addon_type="prepend"),
        dbc.Select(id=_RIG_SELECT_ID,
                   options=[{"label": rig, "value": rig} for rig in rigs],
                   value=info.rig_id if info else None)
    ], size='sm')
    study_group = dbc.InputGroup([
        dbc.InputGroupAddon("Study", addon_type="prepend"),
        dbc.Select(id=_STUDY_SELECT_ID,
                   options=[{"label": opt['study_title'], "value": opt['study_id']} for opt in studies],
                   value=info.study_id if info else None)
    ], size='sm')
    date_group = dbc.InputGroup([
        dbc.InputGroupAddon("Recorded On", addon_type="prepend"),
        dcc.DatePickerSingle(id=_RECORD_DATE_ID, date=info.session_date if info else None, display_format='YYYY-MM-DD')
    ], size='sm')
    suffix_group = dbc.InputGroup([
        dbc.InputGroupAddon("Suffix (0-9)", addon_type="prepend"),
        dbc.Input(id=_SUFFIX_INPUT_ID, type='number', minLength=1, maxLength=1,
                  value=info.session_suffix if info else None)
    ], size='sm')
    notes_group = dbc.InputGroup([
        dbc.InputGroupAddon("Session Notes", addon_type="prepend"),
        dbc.Textarea(id=_NOTES_AREA_ID, minLength=0, maxLength=2048, rows=4, value=info.session_notes if info else None,
                     placeholder='Enter any notes about this particular session (optional, up to 2048 chars)')
    ], size='sm')

    row_1 = dbc.Row([dbc.Col(date_group, width=3), dbc.Col(suffix_group, width=2),
                     dbc.Col(subject_group, width=3, className='ml-auto'),
                     dbc.Col(rig_group, width=2, className='ml-auto')], className='mr-1 ml-1 mt-2 mb-2')
    row_2 = dbc.Row([dbc.Col(experimenter_group, width=4), dbc.Col(study_group, width=8)], className='mr-1 ml-1 mb-2')
    row_3 = dbc.Row(dbc.Col(notes_group, width=12), className='mr-1 ml-1 mb-3')

    # Widgets for attributes in Session.EPhys....
    no_ephys = (info is None) or (info.num_units <= 0)
    source_options = attribute_info(DBTable.SESSION_EPHYS, 'ephys_src').options
    rec_src_group = dbc.InputGroup([
        dbc.InputGroupAddon("Recording Source", addon_type="prepend"),
        dbc.Select(id=_RECORDING_SRC_SELECT_ID, disabled=no_ephys,
                   options=[{"label": opt, "value": opt} for opt in source_options],
                   value=info.ephys_src if info else None)
    ], size='sm')
    probe_type_options = attribute_info(DBTable.SESSION_EPHYS, 'probe_type').options
    probe_type_group = dbc.InputGroup([
        dbc.InputGroupAddon("Probe Type", addon_type="prepend"),
        dbc.Select(id=_PROBE_TYPE_SELECT_ID, disabled=no_ephys,
                   options=[{"label": opt, "value": opt} for opt in probe_type_options],
                   value=info.probe_type if info else None)
    ], size='sm')
    rate_group = dbc.InputGroup([
        dbc.InputGroupAddon("Sampling Rate (Hz)", addon_type="prepend"),
        dbc.Input(id=_PROBE_RATE_INPUT_ID, disabled=no_ephys, type='number', minLength=2, maxLength=10,
                  value=info.sampling_rate if info else None)
    ], size='sm')
    probe_x_group = dbc.InputGroup([
        dbc.InputGroupAddon("Probe Location: X (mm)", addon_type="prepend"),
        dbc.Input(id=_PROBE_X_INPUT_ID, disabled=no_ephys, type='number', minLength=2, maxLength=10,
                  value=info.probe_x if info else None)
    ], size='sm')
    probe_y_group = dbc.InputGroup([
        dbc.InputGroupAddon("Y (mm)", addon_type="prepend"),
        dbc.Input(id=_PROBE_Y_INPUT_ID, disabled=no_ephys, type='number', minLength=2, maxLength=10,
                  value=info.probe_y if info else None)
    ], size='sm')
    probe_z_group = dbc.InputGroup([
        dbc.InputGroupAddon("Depth (mm)", addon_type="prepend"),
        dbc.Input(id=_PROBE_Z_INPUT_ID, disabled=no_ephys, type='number', minLength=2, maxLength=10,
                  value=info.probe_depth if info else None)
    ], size='sm')
    brain_area_group = dbc.InputGroup([
        dbc.InputGroupAddon("Brain Area", addon_type="prepend"),
        dbc.Select(id=_AREA_SELECT_ID, disabled=no_ephys,
                   options=[{"label": opt['ba_name'], "value": opt['ba_id']} for opt in brain_areas],
                   value=info.ba_id if info else None)
    ], size='sm')

    ephys_label = f"Electrophysiology{' (NOT APPLICABLE - no neural unit recordings found)' if no_ephys else ''}"
    divider = dbc.Row([
        dbc.Col(dbc.Label(ephys_label, size='sm'), width=5 if no_ephys else 1),
        dbc.Col(html.Hr(), width=7 if no_ephys else 11)
    ], className='mr-1 ml-1 mb-2')

    row_4 = dbc.Row([dbc.Col(rec_src_group, width=4), dbc.Col(probe_type_group, width=4), dbc.Col(rate_group, width=4)],
                    className='mr-1 ml-1 mb-2')
    row_5 = dbc.Row([dbc.Col(probe_x_group, width=4), dbc.Col(probe_y_group, width=2), dbc.Col(probe_z_group, width=3)],
                    className='mr-1 ml-1 mb-2')
    row_6 = dbc.Row(dbc.Col(brain_area_group, width=4), className='mr-1 ml-1 mb-3')

    update_btn = dbc.Button("Update", id=_UPDATE_META_ID, color="primary", size='sm')
    update_tip = dbc.Tooltip("Be sure to press this button to confirm any changes on this tab!", target=_UPDATE_META_ID,
                             placement='right', delay=dict(show=100, hide=100))
    row_7 = dbc.Row([dbc.Col(update_btn, width=1), update_tip], className='mr-1 ml-1 mb-2')

    n = 0 if no_ephys else info.num_units
    return dbc.Card([row_1, row_2, row_3, divider, row_4, row_5, row_6, row_7], className='mt-2'), n, err_msg


def _layout_trial_protocol_tab_content(job_id: Optional[str]) -> Tuple[dbc.Card, Optional[str]]:
    proto_names: List[str]
    initial_proto: Optional[maestro.ProtocolCandidate] = None
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

    return dbc.Card(dbc.CardBody([select_proto, proto_div]), className="mt-2"), err_msg


_PROTO_DIV_ID = "review-proto-div"
""" ID of HTML Div on which a trial protocols are displayed and edited during review phase. """
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


def _layout_protocol_div(proto_candidate: Optional[maestro.ProtocolCandidate]) -> List[Any]:
    # NOTE: This has to work even if ProtocolCandidate is None, so that all widgets are realized -- since they appear
    # in callbacks.
    protocol: Optional[maestro.Protocol] = maestro.Protocol.from_candidate(proto_candidate) if proto_candidate else None
    needs_validation = False
    if proto_candidate:
        needs_validation = (proto_candidate.num_reps == 1) or (proto_candidate.num_reps == 2
                                                               and not proto_candidate.matches_existing)
        needs_validation = needs_validation and not proto_candidate.user_validated

    valid_btn = dbc.Button("Validate" if needs_validation else "\u2713 Validated", id=_PROTO_VALID_BTN_ID,
                           color='primary', disabled=(not needs_validation), size='sm')
    tool_tip = dbc.Tooltip(
        "Any trial protocol based on fewer than 3 reps and not matching an existing protocol in the database must "
        "be manually verified by the user. Add any missing random variables (eg, a random-duration fixation "
        "segment) to the definition (if any), then press this button to validate the protocol.",
        target=_PROTO_VALID_BTN_ID)
    reps_badge = dbc.Badge(
        f"# reps = {proto_candidate.num_reps if proto_candidate else 0} "
        f"{'; found match' if (proto_candidate and proto_candidate.matches_existing) else ''}",
        color='info', className='ml-2 mr-5'
    )
    add_rv_btn = dbc.Button("Add Random Var:", id=_PROTO_ADD_RV_BTN_ID, color='primary', size='sm', className='mr-2')
    n_segs = len(proto_candidate.trial.segments) if proto_candidate else 0
    n_tgts = len(proto_candidate.trial.targets) if proto_candidate else 0
    select_rv_type = dbc.InputGroup([
        dbc.InputGroupAddon("Type", addon_type='prepend'),
        dbc.Select(
            id=_PROTO_RV_TYPE_SELECT_ID,
            options=[{'label': t.name, 'value': str(t.value)} for t in maestro.SegParamType if
                     t.can_vary_randomly()],
            value=str(maestro.SegParamType.DURATION.value)
        )
    ], size='sm', className='mr-2')
    seg_select = dbc.InputGroup([
        dbc.InputGroupAddon("Segment", addon_type='prepend'),
        dbc.Select(
            id=_PROTO_RV_SEG_SELECT_ID,
            options=[{'label': str(i), 'value': str(i)} for i in range(n_segs)],
            value='0' if n_segs > 0 else None
        )
    ], size='sm', className='mr-2')
    tgt_select = dbc.InputGroup([
        dbc.InputGroupAddon("Target", addon_type='prepend'),
        dbc.Select(
            id=_PROTO_RV_TGT_SELECT_ID,
            options=[{'label': proto_candidate.trial.targets[i].name, 'value': str(i)} for i in range(n_tgts)],
            value='0' if n_tgts > 0 else None
        )
    ], size='sm')
    validate_form = dbc.Form([
        valid_btn, reps_badge, tool_tip,
        dbc.FormGroup([add_rv_btn, select_rv_type, seg_select, tgt_select], id=_PROTO_RV_GROUP_ID,
                      style={} if needs_validation else {'display': 'none'})
    ], inline=True, className='mt-3')

    cmpt_list = protocol.display_definition() if protocol else list()
    cmpt_list.insert(0, validate_form)
    return cmpt_list


def _layout_neural_units_tab_content(job_id: Optional[str], num_units: int) -> Tuple[dbc.Card, Optional[str]]:
    num_units = num_units if job_id and (num_units > 0) else 0
    err_msg: Optional[str] = None
    first_unit: Optional[OmniplexUnit] = None
    if job_id:
        first_unit = metrics_for_neural_unit(job_id, 0)
        if not first_unit:
            err_msg = "Error - Failed to retrieve cached neural unit metrics from server"
    select_unit = dbc.Select(
        id=_UNIT_SELECT_ID,
        options=[{'label': f"Unit {i + 1}", 'value': str(i)} for i in range(num_units)],
        value="0" if first_unit else None
    )
    unit_div = html.Div(_layout_unit_div(first_unit), id=_UNIT_DIV_ID)
    return dbc.Card(dbc.CardBody([select_unit, unit_div]), className="mt-3"), err_msg


_UNIT_DIV_ID = "review-unit-div"
""" ID of HTML Div in which a selected neural unit is displayed during the review phase. """
_UNIT_SELECT_ID = "review-unit-select"
""" ID of Bootstrap Select by which user selects which neural unit to display for review and validation. """
_UNIT_TYPE_SELECT_ID = "review-unit-type-select"
""" ID of Bootstrap Select component by which user selects the type for the currently displayed neural unit. """
_UNIT_TYPE_APPLY_ALL_ID = "review-unit-type-apply-all"
""" ID of button that, when clicked, applies currently selected neuron type to all recorded neural units. """


def _layout_unit_div(unit: Optional[OmniplexUnit]) -> List[Any]:
    # dropdown lets user assign neuron type to the unit
    neuron_types = fetch_rows(DBTable.NEURON_TYPE)
    if neuron_types is None:
        neuron_types = list()

    initial = None
    if unit and (unit.neuron_type in [nt['nt_id'] for nt in neuron_types]):
        initial = str(unit.neuron_type)
    select_type = dbc.InputGroup(
        [
            dbc.InputGroupAddon("Neuron Type", addon_type="prepend"),
            dbc.Select(
                id=_UNIT_TYPE_SELECT_ID,
                options=[{'label': nt['nt_name'], 'value': str(nt['nt_id'])} for nt in neuron_types],
                value=initial
            )
        ], className='mb-3')
    apply_all_btn = dbc.Button("Apply selected type to all units", color='primary', id=_UNIT_TYPE_APPLY_ALL_ID)
    header_kids = [html.Hr(), dbc.Row([dbc.Col(select_type, width=8), dbc.Col(apply_all_btn, width=4)])]

    peak_to_peak = max(unit.template) - min(unit.template) if unit else 0
    header_kids.extend([
        dbc.Badge(f"Omniplex Channel: {unit.channel if unit else '--'}", color="primary", className="mr-3"),
        dbc.Badge(f"Mean firing rate: {unit.firing_rate if unit else 0:.1f} Hz", color="primary", className="mr-3"),
        dbc.Badge(f"#Spikes: {unit.num_spikes if unit else 0}", color="primary", className="mr-3"),
        dbc.Badge(f"SNR: {unit.snr if unit else 0:.2f}", color="primary", className="mr-3"),
        dbc.Badge(f"Peak-to-peak: {peak_to_peak:.1f} \u00B5V", color="primary", className="mr-3"),
    ])

    # simple graph of template waveform. Note I'm assuming 40KHz sampling rate here!
    template = unit.template if unit else [0 for _ in range(20)]  # provide dummy template if none provided
    graph = dcc.Graph(figure=px.line(x=[i / 40.0 for i in range(len(template))], y=template,
                                     labels={'x': 'time (ms)', 'y': '\u00B5V'},
                                     title='Average spike waveform (1-ms pre, 9-ms post)'))

    return [html.Div(header_kids, className='mt-3 mb-1'), graph]


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
    [Output(_START_BTN_ID, 'disabled'), Output(_JOBS_TABLE_ID, "data"), Output(_JOBS_TABLE_ID, "selected_rows"),
     Output(_UPLOAD_ID, "is_open"), Output(_ALERT_ID, "children"), Output(_ALERT_ID, "is_open")],
    [Input(_START_BTN_ID, "n_clicks"), Input(_REFRESH_BTN_ID, "n_clicks"), Input(_REMOVE_BTN_ID, "n_clicks"),
     Input(_CLOSE_UPLOAD_ID, "n_clicks"), Input(_UPLOADER_ID, 'isCompleted'), Input(_UPLOADER_ID, 'fileNames')],
    [State(_UPLOADER_ID, "upload_id"), State(_JOBS_TABLE_ID, "data"), State(_JOBS_TABLE_ID, "selected_rows")]
)
def all_in_one_callback(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate

    # is an upload in progress -- that determines whether or not we can start a new commit job
    job_rows: List[Any] = args[-2]
    uploading = isinstance(job_rows, list) and any([j['state'] == CommitStateEnum.UPLOADING.value for j in job_rows])
    upload_id = args[-3]

    # there must be a logged-in user with 'commit' access
    username = _get_current_username()
    if not username:
        return uploading, no_update, no_update, no_update, \
               "Access denied. You must be logged into portal with commit privileges.", True

    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _START_BTN_ID:
        job_status = initiate_session_commit(username, upload_id)
        if isinstance(job_status, str):
            # operation failed on server; inform user
            return False, no_update, no_update, no_update, str(job_status), True
        else:
            job_row = _job_table_row_from_job_status_info(job_status)
            if not isinstance(job_rows, list):
                job_rows = [job_row]
            else:
                job_rows.insert(0, job_row)
            return True, job_rows, no_update, True, "", False
    elif trigger == _REFRESH_BTN_ID:
        err_msg, jobs = _commit_jobs_for_current_user()
        uploading = isinstance(jobs, list) and any([j['state'] == CommitStateEnum.UPLOADING.value for j in jobs])
        if err_msg is not None:
            return no_update, no_update, no_update, False, err_msg, True
        else:
            return uploading, jobs, no_update, False, "", False
    elif trigger == _REMOVE_BTN_ID or trigger == _CLOSE_UPLOAD_ID:
        # hitting "Cancel" button while Upload Modal is raised removes the relevant job. The currently selected job
        # should be the one that was in the uploading phase, but we don't take that for granted
        idx = -1
        job_id = None
        if trigger == _REMOVE_BTN_ID:
            selection = args[-1]
            idx = selection[0] if (selection is not None) and (len(selection) > 0) else -1
            job_id = job_rows[idx]['id'] if ((job_rows is not None) and (-1 < idx < len(job_rows))) else None
        else:
            if uploading:
                try:
                    idx = [j['state'] for j in job_rows].index(CommitStateEnum.UPLOADING.value)
                    job_id = job_rows[idx]['id']
                except ValueError:
                    pass
        if job_id is None:
            raise dash_exc.PreventUpdate
        removed, err_msg, job_info = cancel_or_remove_commit_job(job_id)
        if len(err_msg) > 0:
            return no_update, no_update, no_update, False, err_msg, True
        elif removed:
            job_rows.pop(idx)
        else:
            job_rows[idx] = _job_table_row_from_job_status_info(job_info)
        uploading = any([j['state'] == CommitStateEnum.UPLOADING.value for j in job_rows])
        return uploading, job_rows, [] if removed else no_update, False, "", False
    elif trigger == _UPLOADER_ID:
        is_completed = args[-5]
        file_names = args[-4]
        upload_id = args[-3]
        if not is_completed:
            if file_names is not None:
                logger.debug(f"Upload initiated on client, upload_id={upload_id}, file_names={file_names}")
            raise dash_exc.PreventUpdate
        else:
            logger.debug(f"Upload completed, upload_id={upload_id}, file={file_names}")
            fname = str(file_names[0] if isinstance(file_names, list) else file_names)
            upload_job_idx = -1
            for i, r in enumerate(job_rows):
                if r['state'] == CommitStateEnum.UPLOADING.value:
                    upload_job_idx = i
                    break
            if upload_job_idx == -1:
                logger.error(f"Upload just completed, but no current commit job is in the uploading phase!")
                err_msg = f"Internal error - no commit job is currently uploading"
                return False, no_update, no_update, False, err_msg, True

            job_status = update_commit_job_on_archive_upload(job_rows[upload_job_idx]['id'], fname)
            if isinstance(job_status, str):
                err_msg = f"Upload failed: {str(job_status)}"
                return True, no_update, no_update, False, err_msg, True
            job_rows[upload_job_idx] = _job_table_row_from_job_status_info(job_status)
            return False, job_rows, no_update, False, "", False
    logger.debug(f"On commit page, failed to identify trigger for all-in-one callback: {trigger}")
    raise dash_exc.PreventUpdate


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
        messages = commit_job_progress(sel_row['id'])
        if isinstance(messages, str):
            # A server error happened. Display the error message in the modal.
            markdown = f"**{messages}**. Try again later or contact portal administrator."
        else:
            # Display progress message list in markdown
            markdown = "* " + "\n* ".join(messages)
        header = f"Progress history for {sel_row['id']}"
        return True, header, markdown


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
    [Input(_META_ALERT_DIV, 'children'), Input(_PROTO_ALERT_DIV, 'children'), Input(_UNIT_ALERT_DIV, 'children'),
     Input(_COMMIT_ALERT_DIV, 'children')]
)
def update_review_modal_alert(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    try:
        idx = [_META_ALERT_DIV, _PROTO_ALERT_DIV, _UNIT_ALERT_DIV, _COMMIT_ALERT_DIV].index(trigger)
    except ValueError:
        raise dash_exc.PreventUpdate
    pos = args[idx].find('-') if isinstance(args[idx], str) else -1
    if pos > -1:
        alert_color = args[idx][0:pos]
        return args[idx][pos + 1:], alert_color, alert_color != 'success'


@callback(
    Output(_META_ALERT_DIV, 'children'),
    [Input(_UPDATE_META_ID, 'n_clicks')],
    [State(_EXPERIMENTER_SELECT_ID, "value"), State(_SUBJECT_SELECT_ID, "value"), State(_RIG_SELECT_ID, "value"),
     State(_RECORD_DATE_ID, "date"), State(_SUFFIX_INPUT_ID, "value"), State(_STUDY_SELECT_ID, "value"),
     State(_NOTES_AREA_ID, "value"), State(_RECORDING_SRC_SELECT_ID, "value"), State(_PROBE_TYPE_SELECT_ID, "value"),
     State(_PROBE_RATE_INPUT_ID, "value"), State(_PROBE_X_INPUT_ID, "value"), State(_PROBE_Y_INPUT_ID, "value"),
     State(_PROBE_Z_INPUT_ID, "value"), State(_AREA_SELECT_ID, "value"), State(_REVIEW_HEADER_ID, "children")]
)
def update_session_data(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _UPDATE_META_ID:
        # job ID is in the modal header text
        idx = args[-1].find(":")
        job_id = args[-1][idx + 2:]
        ofs = 1
        info = SessionMetaData(
            experimenter=args[ofs], subj_id=args[ofs + 1], rig_id=args[ofs + 2], session_date=args[ofs + 3],
            session_suffix=args[ofs + 4] and int(args[ofs + 4]), study_id=args[ofs + 5] and int(args[ofs + 5]),
            session_notes=args[ofs + 6],
            ephys_src=args[ofs + 7], probe_type=args[ofs + 8], sampling_rate=args[ofs + 9] and float(args[ofs + 9]),
            probe_x=args[ofs + 10] and float(args[ofs + 10]), probe_y=args[ofs + 11] and float(args[ofs + 11]),
            probe_depth=args[ofs + 12] and float(args[ofs + 12]), ba_id=args[ofs + 13] and int(args[ofs + 13]))
        ok = update_session_metadata(job_id, info)
        if not ok:
            alert_msg, alert_color = 'Error - Failed to update session metadata on server', 'danger'
        else:
            ok, ready, alert_msg = ready_to_commit(job_id)
            alert_color = 'danger' if (not ok) else ('success' if ready else 'warning')
        return f"{alert_color}-{alert_msg}"
    return no_update


@callback(
    [Output(_PROTO_DIV_ID, 'children'), Output(_PROTO_ALERT_DIV, 'children'),
     Output(_PROTO_VALID_BTN_ID, 'children'), Output(_PROTO_VALID_BTN_ID, 'disabled'),
     Output(_PROTO_RV_GROUP_ID, 'style'), Output(_PROTO_SELECT_ID, 'options')],
    [Input(_PROTO_SELECT_ID, 'value'), Input(_PROTO_ADD_RV_BTN_ID, 'n_clicks'), Input(_PROTO_VALID_BTN_ID, 'n_clicks')],
    [State(_PROTO_SELECT_ID, 'value'), State(_PROTO_RV_TYPE_SELECT_ID, 'value'),
     State(_PROTO_RV_SEG_SELECT_ID, 'value'), State(_PROTO_RV_TGT_SELECT_ID, 'value'),
     State(_PROTO_SELECT_ID, 'options'), State(_REVIEW_HEADER_ID, "children")])
def update_proto(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    out = [no_update] * 6
    # job ID is in the modal header text
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
        rv: maestro.SegParam = maestro.SegParam(
            maestro.SegParamType(int(args[ofs + 1])), int(args[ofs + 2]), int(args[ofs + 3]))
        proto_candidate = add_rv_to_protocol(job_id, proto_index, rv)
        if proto_candidate:
            out[0] = _layout_protocol_div(proto_candidate)
        else:
            out[1] = "danger-An error occurred while adding modifying protocol definition"
    elif trigger == _PROTO_VALID_BTN_ID:
        proto_index = int(args[3])
        if not validate_protocol(job_id, proto_index):
            out[1] = "danger-An error occurred while validating protocol definition on server"
        else:
            options = args[-2]
            proto_name = options[proto_index]['label'][3:]   # strip off the leading '** ' now that it's validated
            options[proto_index] = {'label': proto_name, 'value': str(proto_index)}
            out[2] = "\u2713 Validated"
            out[3] = True
            out[4] = dict(display='none')
            out[5] = options
            ok, ready, msg = ready_to_commit(job_id)
            out[1] = f"{'danger' if not ok else ('success' if ready else 'warning')}-{msg}"
    return tuple(out)


@callback(
    [Output(_UNIT_DIV_ID, 'children'), Output(_UNIT_ALERT_DIV, 'children')],
    [Input(_UNIT_SELECT_ID, 'value'), Input(_UNIT_TYPE_SELECT_ID, "value"), Input(_UNIT_TYPE_APPLY_ALL_ID, "n_clicks")],
    [State(_UNIT_TYPE_SELECT_ID, 'value'), State(_UNIT_SELECT_ID, 'value'), State(_REVIEW_HEADER_ID, "children")]
)
def update_unit(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    # job ID is in the modal header text
    idx = args[-1].find(":")
    job_id = args[-1][idx + 2:]
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _UNIT_SELECT_ID:
        unit: OmniplexUnit = metrics_for_neural_unit(job_id, int(args[0]))
        if unit:
            return _layout_unit_div(unit), no_update
        else:
            return no_update, "danger-An error occurred while retrieving neural unit metrics from server"
    elif (trigger == _UNIT_TYPE_SELECT_ID) or (trigger == _UNIT_TYPE_APPLY_ALL_ID):
        unit_idx = -1 if (trigger == _UNIT_TYPE_APPLY_ALL_ID) else int(args[-2])
        nt_id = int(args[1] if trigger == _UNIT_TYPE_SELECT_ID else args[-3])
        ok = set_unit_type(job_id, unit_idx, nt_id)
        if ok:
            ok, ready, msg = ready_to_commit(job_id)
            msg = f"{'danger' if not ok else ('success' if ready else 'warning')}-{msg}"
        else:
            msg = "danger-An error occurred while updating neural unit type on server"
        return no_update, msg
    return no_update, no_update


@callback(
    [Output(_COMMIT_ALERT_DIV, 'children'), Output(_REVIEW_CLOSE_ID, 'n_clicks'), Output(_REFRESH_BTN_ID, 'n_clicks')],
    [Input(_REVIEW_COMMIT_ID, 'n_clicks')],
    [State(_REVIEW_CLOSE_ID, 'n_clicks'), State(_REFRESH_BTN_ID, 'n_clicks'), State(_REVIEW_HEADER_ID, 'children')]
)
def on_trigger_commit_to_database(*args):
    ctx = callback_context
    if not ctx.triggered:
        raise dash_exc.PreventUpdate
    trigger = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger == _REVIEW_COMMIT_ID:
        # Each time the review modal is shown, it is laid out again. So this method will be invoked on initial load,
        # but the "n_clicks" attribute will be at its initial value of 0. In this case, do nothing. This is
        # imperative!
        if args[0] == 0:
            raise dash_exc.PreventUpdate
        # get job ID from the Review modal header
        idx = args[-1].find(":")
        job_id = args[-1][idx + 2:]
        err_msg = commit_to_database(job_id)
        if err_msg:
            return f"danger-{err_msg}", no_update, no_update
        else:
            n_close = (args[-3] + 1) if args[-3] else 1
            n_refresh = (args[-2] + 1) if args[-2] else 1
            return no_update, n_close, n_refresh
    raise dash_exc.PreventUpdate
