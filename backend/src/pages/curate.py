"""
curate.py: A page in the Lisberger lab's database portal app devoted to adding, removing or modifying select "metadata"
tables containing information used to categorize and search database content (/curate endpoint).

    Only logged-in portal administrators should have access to this page, which allows the user to make changes to the
following database tables: Subject, SubjectImplant (experiment subjects and their implant history); Rig (experiment
rigs); BrainArea; NeuronType; Study (research projects in the lab); Publications (citations of research articles
published by lab members). Compared to the database tables storing information and datasets from actual experiments,
these tables are quite small, and their content will be updated very infrequently.

    Removing content from any of these tables is supported, but once an entry in one of these tables is referenced (via
foreign key) in another table, the portal app may not allow deletion and/or update of that entry.

@author: sruffner
@created: 28jan2022
"""
import json
from typing import List, Dict, Optional

from dash import callback, callback_context, no_update, Input, Output, State, html, dcc, dash_table as dt
import dash_bootstrap_components as dbc

from config.config import get_application_logger
from database.table_info import DBTable, Column, AttributeValue, attribute_info
from database.table_ops import fetch_rows, delete_from_table, row_exists, update_table_row, insert_into_table, \
    update_mapping_table

tabs: List[dbc.Tab] = [
    dbc.Tab(label="Experiment Subjects", tab_id='subj_tab'),
    dbc.Tab(label="Experiment Rigs", tab_id='rig_tab'),
    dbc.Tab(label="Brain Nomenclature", tab_id='brain_tab'),
    dbc.Tab(label="Research", tab_id='research_tab')
]

tabs_card = dbc.Card(
    [
        dbc.CardHeader(
            dbc.Tabs(
                tabs,
                id="tabs",
                persistence=True, persistence_type="session",
                active_tab="subj_tab",
            )
        ),
        dbc.CardBody(id="sel-tab-content", children=[])
    ]
)

markdown = dcc.Markdown('''
    Users with administrative-level access to the portal can add and/or update information used to categorize and 
    search for datasets stored in the lab database. **The system will generally prevent you from deleting any 
    information that would indirectly cause the removal of experimental data; nevertheless, avoid removing any 
    metadata unless you are sure it is safe!**
    ''', className='mt-1 mb-2')
container_card = dbc.Card([
    dbc.CardHeader("Curate the laboratory database"),
    dbc.CardBody([markdown, tabs_card])
], class_name='w-75 mx-auto mt-5')

layout = html.Div([container_card])


@callback(Output("sel-tab-content", "children"), [Input("tabs", "active_tab")])
def update_tab_content(active_tab):
    if active_tab == 'rig_tab':
        out = _rig_tabpane()
    elif active_tab == 'brain_tab':
        out = _brain_tabpane()
    elif active_tab == 'subj_tab':
        out = _subject_tabpane()
    elif active_tab == 'research_tab':
        out = _research_tabpane()
    else:
        out = html.Div("No tab selected", className='mt-5 mb-5')
    return out


def _make_table_for(table_id: str, rows: List[Dict[str, AttributeValue]], cols: List[Column],
                    height: int = 300, num_lines_per_row: int = 0) -> dt.DataTable:
    """
    Helper method generates a Dash DataTable. It is intended to ensure a consistent appearance among the tables located
    on the 'curate' page.

    Args:
        table_id: The ID assigned to the DataTable component.
        rows: The database rows with which the DataTable is populated.
        cols: The DataTable column descriptors.
        height: Visible table height in pixels. It is configured to scroll if not all rows are visible. Default = 300.
        num_lines_per_row: If nonzero, then the height of each table row is fixed to accommodate this many text lines.
            Otherwise, each table row is as tall as necessary to accommodate its content. Default = 0.
    Returns:
        The Dash DataTable.
    """
    max_ht = num_lines_per_row * 18
    css_selectors = [] if (max_ht <= 0) else [{
        'selector': '.dash-spreadsheet td div',
        'rule': f'''
                    line-height: 18px;
                    max-height: {max_ht}px; min-height: {max_ht}px; height: {max_ht}px;
                    display: block;
                    overflow-y: hidden;
                    '''
    }]

    return dt.DataTable(
        id=table_id,
        columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                 for col in cols],
        data=rows,
        row_selectable='single',
        cell_selectable=False,
        selected_rows=[],
        style_header={'fontWeight': 'bold'},
        style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
        style_data={'whiteSpace': 'pre-wrap'},
        style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in cols],
        tooltip_data=None, tooltip_duration=None,
        css=css_selectors,
        style_table={'height': f"{height}px", 'overflowY': 'scroll', 'border': '1px solid lightgray'},
    )


_SUBJ_TABLE_ID: str = "subj-table"
""" The ID assigned to the Dash DataTable presenting all experiment subjects in the Lisberger lab database. """
_SUBJ_TABLE_COLS: List[Column] = [
    Column('subj_id', 'Subject ID', '100px', False),
    Column('species', 'Species', '125px', False),
    Column('dob', 'Date of Birth', '75px', False),
    Column('sex', 'Sex', '50px', False)
]
""" Defined columns for the experiment subjects table. """
_SUBJ_ID_INPUT: str = "subj-id-input"
""" ID of text input widget for displaying/setting the 'subj_id' attribute of a new or existing subject. """
_SUBJ_SPECIES_SEL: str = "subj-species-sel"
""" ID of Bootstrap Select widget for displaying/setting the 'species' attribute of a new or existing subject. """
_SUBJ_DOB_INPUT: str = "subj-dob-input"
""" ID of text input for displaying/setting the 'dob' attribute of a new or existing subject (as 'YYYY-MM-DD'). """
_SUBJ_SEX_SEL: str = "subj-sex-sel"
""" ID of Bootstrap Select widget for displaying/setting the 'sex' attribute of a new or existing subject. """
_CLEAR_SEL_SUBJ_BTN: str = "subj-clear"
""" ID of button that clears the current selection in the subjects table (if any). """
_ADD_MOD_SUBJ_BTN = "subj-add-mod"
""" ID of button that adds a new subject to the database or modifies the secondary attributes of an existing one. """
_DEL_SUBJ_BTN: str = "subj-del"
""" ID of button that deletes the selected experiment subject (if possible). """
_SUBJ_ALERT: str = "subj-alert"
""" ID of Bootstrap Alert that appears when an operation on the experiment subjects table fails."""
_TABLE_RETRIEVE_ERROR: str = "Failed to retrieve table content from database."
""" Generic user-facing error message when an attempt to retrieve the rows of a database table fails."""
_SELECTED_SUBJ_ROW_STORE: str = "subj-selected-store"
""" ID of Dash Store component that holds a JSON representation of the currently selected row in the subjects table."""


def _subject_tabpane() -> html.Div:
    """
    Generate the contents of the 'Experiment Subjects' tab on this page.

    Returns:
        An HTML Div rendering the content of the tab pane.
    """
    error_msg = None
    rows = fetch_rows(DBTable.SUBJECT)
    if rows is None:
        rows, error_msg = [], _TABLE_RETRIEVE_ERROR

    subj_table = _make_table_for(_SUBJ_TABLE_ID, rows, _SUBJ_TABLE_COLS, height=300)

    id_grp = dbc.InputGroup([
        dbc.InputGroupText("Subject ID"),
        dbc.Input(id=_SUBJ_ID_INPUT, type='text', minlength=3, maxlength=20,
                  placeholder="Unique ID/nickname (3-20 letters)"),
    ])
    species_options = [opt for opt in attribute_info(DBTable.SUBJECT, 'species').options]
    species_grp = dbc.InputGroup([
        dbc.InputGroupText("Species"),
        dbc.Select(
            id=_SUBJ_SPECIES_SEL,
            options=[{'label': v, 'value': v} for v in species_options],
            value=species_options[0]
        ),
    ])
    sex_options = [opt for opt in attribute_info(DBTable.SUBJECT, 'sex').options]
    sex_grp = dbc.InputGroup([
        dbc.InputGroupText("Sex"),
        dbc.Select(
            id=_SUBJ_SEX_SEL,
            options=[{'label': v, 'value': v} for v in sex_options],
            value=sex_options[0]
        ),
    ])
    dob_grp = dbc.InputGroup([
        dbc.InputGroupText("DOB"),
        dbc.Input(id=_SUBJ_DOB_INPUT, type='text', minlength=10, maxlength=10, placeholder="YYYY-MM-DD"),
    ])

    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_SUBJ_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_SUBJ_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_SUBJ_BTN, disabled=True)
    alert = dbc.Alert("" if error_msg is None else error_msg, id=_SUBJ_ALERT, color='danger',
                      dismissable=True, is_open=(error_msg is not None))
    edit_form = dbc.Form([
        dbc.Row(dbc.Col(id_grp, width=8), class_name='g-0 mb-2'),
        dbc.Row([
            dbc.Col(species_grp, width='auto', class_name='me-3'),
            dbc.Col(sex_grp, width='auto')
        ], class_name='g-0 mb-2'),
        dbc.Row(dbc.Col(dob_grp, width='auto'), class_name='g-0 mb-3'),
        dbc.Row([
            dbc.Col(clear_btn, width='auto', class_name='me-2'),
            dbc.Col(add_btn, width='auto', class_name='me-2'),
            dbc.Col(del_btn, width='auto')
        ], class_name='g-0 mb-3'),
        dbc.Row(dbc.Col(alert, width=12)),
    ])

    return html.Div([
        dbc.Row([
            dbc.Col(subj_table, width=6), dbc.Col(edit_form, width=6)
        ]),
        dcc.Store(id=_SELECTED_SUBJ_ROW_STORE),
        _implant_history_panel()
    ])


_IMPLANT_TABLE_ID: str = "implant-table"
""" 
ID assigned to the Dash DataTable presenting the implant history for a given experiment subject in the Lisberger 
lab database.
"""
_IMPLANT_TABLE_COLS: List[Column] = [
    Column('implant_date', 'Surgery Date', '125px', False),
    Column('st_ap', 'AP(mm)', '50px', False),
    Column('st_ml', 'ML(mm)', '50px', False),
    Column('st_dv', 'DV(mm)', '50px', False),
    Column('ap_angle', 'AP \u03b8\u00b0', '50px', False),
    Column('ml_angle', 'ML \u03b8\u00b0', '50px', False)
]
""" 
Defined columns for the experiment subjects table. The subject ID is not shown, because the table only lists the
implant surgeries for a single subject.
"""
_IMPLANT_DATE_INPUT: str = "implant-date-input"
""" ID of text input for displaying/setting the 'implant_date' attribute of a new or existing implant. """
_IMPLANT_AP_INPUT: str = "implant-ap-input"
""" ID  of text input displaying/setting anterior-posterior stereotaxic coordinate of implant in mm. """
_IMPLANT_ML_INPUT: str = "implant-ml-input"
""" ID  of text input displaying/setting medial-lateral stereotaxic coordinate of implant in mm. """
_IMPLANT_DV_INPUT: str = "implant-dv-input"
""" ID  of text input displaying/setting dorsal-ventral stereotaxic coordinate of implant in mm. """
_IMPLANT_AP_ANGLE_INPUT: str = "implant-ap-angle-input"
""" ID  of text input displaying/setting cylinder implant angle WRT AP axis (deg CCW). """
_IMPLANT_ML_ANGLE_INPUT: str = "implant-ml-angle-input"
""" ID  of text input displaying/setting cylinder implant angle WRT ML axis (deg CCW). """
_CLEAR_SEL_IMPLANT_BTN: str = "implant-clear"
""" ID of button that clears the current selection in the implant history table (if any). """
_ADD_MOD_IMPLANT_BTN = "implant-add-mod"
""" 
ID of button that adds a new cylinder implant record to the database or modifies the secondary attributes of an 
existing one.
"""
_DEL_IMPLANT_BTN: str = "implant-del"
""" ID of button that deletes the selected cylinder implant record (if possible). """
_IMPLANT_ALERT: str = "implant-alert"
""" ID of Bootstrap Alert that appears when an operation on the implant history table fails."""
_IMPLANT_LABEL: str = 'implant-subj-label'
""" ID of Bootstrap Label reflecting the ID of the animal subject to which the displayed implant history applies. """
_IMPLANT_COLLAPSE: str = 'implant-collapse'
""" 
ID of Boostrap Collapse that encapsulates the implant history for the currently selected subject. The implant
history table and associated widgets are hidden when no subject is selected.
"""


def _implant_history_panel() -> dbc.Collapse:
    """
    Helper method for _subject_tabpane() constructs the layout of the implant history "subpanel" for a selected animal
    subject. It is wrapped in a Bootstrap Collapse because it should only be visible when a particular subject is
    selected on the subject panel.
    """
    implant_table = _make_table_for(_IMPLANT_TABLE_ID, [], _IMPLANT_TABLE_COLS, height=200)

    implant_date_grp = dbc.InputGroup([
        dbc.InputGroupText("Surgery Date"),
        dbc.Input(id=_IMPLANT_DATE_INPUT, type='text', minlength=10, maxlength=10, placeholder="YYYY-MM-DD"),
    ])

    st_ap_grp = dbc.InputGroup([
        dbc.InputGroupText("Coords (mm):"),
        dbc.InputGroupText("AP"),
        dbc.Input(id=_IMPLANT_AP_INPUT, type='number', minlength=1, maxlength=10),
    ])
    st_ml_grp = dbc.InputGroup([
        dbc.InputGroupText("ML"),
        dbc.Input(id=_IMPLANT_ML_INPUT, type='number', minlength=1, maxlength=10),
    ])
    st_dv_grp = dbc.InputGroup([
        dbc.InputGroupText("DV"),
        dbc.Input(id=_IMPLANT_DV_INPUT, type='number', minlength=1, maxlength=10),
    ])
    ap_angle_grp = dbc.InputGroup([
        dbc.InputGroupText("\u03b8 (\u00b0):"),
        dbc.InputGroupText("AP"),
        dbc.Input(id=_IMPLANT_AP_ANGLE_INPUT, type='number', minlength=1, maxlength=10),
    ])
    ml_angle_grp = dbc.InputGroup([
        dbc.InputGroupText("ML"),
        dbc.Input(id=_IMPLANT_ML_ANGLE_INPUT, type='number', minlength=1, maxlength=10),
    ])

    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_IMPLANT_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_IMPLANT_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_IMPLANT_BTN, disabled=True)
    alert = dbc.Alert("", id=_IMPLANT_ALERT, color='danger', dismissable=True, is_open=False)

    edit_form = dbc.Form([
        dbc.Row(dbc.Col(implant_date_grp, width=5), class_name='g-0 mb-2'),
        dbc.Row([
            dbc.Col(st_ap_grp, width=5, class_name='me-2'),
            dbc.Col(st_ml_grp, width=3, class_name='me-2'),
            dbc.Col(st_dv_grp, width=3)
        ], class_name='g-0 mb-2'),
        dbc.Row([
            dbc.Col(ap_angle_grp, width=4, class_name='me-2'),
            dbc.Col(ml_angle_grp, width=3),
        ], class_name='g-0 mb-3'),
        dbc.Row([
            dbc.Col(clear_btn, width='auto', class_name='me-2'),
            dbc.Col(add_btn, width='auto', class_name='me-2'),
            dbc.Col(del_btn, width='auto')
        ], class_name='g-0 mb-3'),
        dbc.Row(dbc.Col(alert, width=12))
    ])

    history_label = dcc.Markdown("Implant history for ...", id=_IMPLANT_LABEL)
    table_div = html.Div([
        dbc.Row(dbc.Col(history_label, width='auto'), class_name='g-0 mb-1'),
        dbc.Row(dbc.Col(implant_table, width=12), class_name='g-0')
    ])
    implant_history_div = html.Div([
        dbc.Row([dbc.Col(table_div, width=6), dbc.Col(edit_form, width=6)])
    ])

    return dbc.Collapse(dbc.Card(dbc.CardBody(implant_history_div)), id=_IMPLANT_COLLAPSE, class_name='mt-3',
                        is_open=False)


@callback(
    Output(_SELECTED_SUBJ_ROW_STORE, "value"), [Input(_SUBJ_TABLE_ID, "selected_rows")], [State(_SUBJ_TABLE_ID, "data")]
)
def save_selected_subject_row(selected_rows, rows):
    idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
    selected_row = rows[idx] if ((rows is not None) and (-1 < idx < len(rows))) else None
    return json.dumps(selected_row)


@callback(
    [Output(_SUBJ_TABLE_ID, 'data'), Output(_SUBJ_TABLE_ID, 'selected_rows'),
     Output(_SUBJ_TABLE_ID, 'style_data_conditional'),
     Output(_SUBJ_ALERT, 'children'), Output(_SUBJ_ALERT, 'is_open'), Output(_DEL_SUBJ_BTN, 'disabled'),
     Output(_ADD_MOD_SUBJ_BTN, 'children'), Output(_SUBJ_ID_INPUT, 'disabled'), Output(_SUBJ_ID_INPUT, 'value'),
     Output(_SUBJ_SPECIES_SEL, 'value'), Output(_SUBJ_SEX_SEL, 'value'), Output(_SUBJ_DOB_INPUT, 'value')],
    [Input(_CLEAR_SEL_SUBJ_BTN, 'n_clicks'), Input(_ADD_MOD_SUBJ_BTN, 'n_clicks'), Input(_DEL_SUBJ_BTN, 'n_clicks'),
     Input(_SUBJ_TABLE_ID, 'selected_rows')],
    [State(_SUBJ_TABLE_ID, 'selected_rows'), State(_SUBJ_TABLE_ID, 'data'), State(_SUBJ_ID_INPUT, 'value'),
     State(_SUBJ_SPECIES_SEL, 'value'), State(_SUBJ_SEX_SEL, 'value'), State(_SUBJ_DOB_INPUT, 'value')]
)
def _subject_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 12

    if trigger_id == _CLEAR_SEL_SUBJ_BTN and isinstance(args[4], list) and len(args[4]) > 0:
        out = no_update, [], [], "", False, True, "Add", False, None, no_update, no_update, None
    elif trigger_id == _SUBJ_TABLE_ID:
        idx = (args[3][0] if (args[3] is not None) and (len(args[3]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            out = no_update, no_update, style, "", False, False, "Update", True, row['subj_id'], row['species'], \
                row['sex'], str(row['dob'])
    elif trigger_id == _DEL_SUBJ_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.SUBJECT, dict(subj_id=row['subj_id']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", False, \
                    None, no_update, no_update, None
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_SUBJ_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        tid = DBTable.SUBJECT
        subject = dict(subj_id=args[6], species=args[7], sex=args[8], dob=args[9])
        is_add = (row is None)
        error_msg = insert_into_table(tid, subject) if is_add else update_table_row(tid, subject)
        if error_msg is None:
            if is_add:
                rows = fetch_rows(DBTable.SUBJECT)
                error_msg = None if rows else _TABLE_RETRIEVE_ERROR
                out = rows if rows else [], [], [], "" if rows else error_msg, rows is None, \
                    True, "Add", False, None, no_update, no_update, None
            else:
                rows[idx] = subject
                out = rows, [], [], "", False, True, "Add", False, None, no_update, no_update, None
        else:
            out[3:5] = (error_msg, True)

    return tuple(out)


@callback(
    [Output(_IMPLANT_COLLAPSE, "is_open"), Output(_IMPLANT_LABEL, "children")],
    [Input(_SELECTED_SUBJ_ROW_STORE, "value")]
)
def show_hide_implant_history(json_str):
    subj_row = json_str and json.loads(json_str)
    label = f"Implant history for _**{subj_row['subj_id']}**_" if subj_row else ""
    return subj_row is not None, label


@callback(
    [Output(_IMPLANT_TABLE_ID, 'data'), Output(_IMPLANT_TABLE_ID, 'selected_rows'),
     Output(_IMPLANT_TABLE_ID, 'style_data_conditional'),
     Output(_IMPLANT_ALERT, 'children'), Output(_IMPLANT_ALERT, 'is_open'), Output(_DEL_IMPLANT_BTN, 'disabled'),
     Output(_ADD_MOD_IMPLANT_BTN, 'children'), Output(_IMPLANT_DATE_INPUT, 'disabled'),
     Output(_IMPLANT_DATE_INPUT, 'value'), Output(_IMPLANT_AP_INPUT, 'value'), Output(_IMPLANT_ML_INPUT, 'value'),
     Output(_IMPLANT_DV_INPUT, 'value'), Output(_IMPLANT_AP_ANGLE_INPUT, 'value'),
     Output(_IMPLANT_ML_ANGLE_INPUT, 'value')],
    [Input(_SELECTED_SUBJ_ROW_STORE, 'value'), Input(_CLEAR_SEL_IMPLANT_BTN, 'n_clicks'),
     Input(_ADD_MOD_IMPLANT_BTN, 'n_clicks'), Input(_DEL_IMPLANT_BTN, 'n_clicks'),
     Input(_IMPLANT_TABLE_ID, 'selected_rows')],
    [State(_IMPLANT_TABLE_ID, 'selected_rows'), State(_IMPLANT_TABLE_ID, 'data'), State(_IMPLANT_DATE_INPUT, 'value'),
     State(_IMPLANT_AP_INPUT, 'value'), State(_IMPLANT_ML_INPUT, 'value'), State(_IMPLANT_DV_INPUT, 'value'),
     State(_IMPLANT_AP_ANGLE_INPUT, 'value'), State(_IMPLANT_ML_ANGLE_INPUT, 'value'),
     State(_SELECTED_SUBJ_ROW_STORE, 'value')]
)
def _implant_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 14

    if trigger_id == _SELECTED_SUBJ_ROW_STORE:
        subj_row = args[0] and json.loads(args[0])
        rows = []
        error_msg = ""
        if subj_row:
            subj_pk = dict(subj_id=subj_row['subj_id'])
            rows = fetch_rows(DBTable.IMPLANT, subj_pk)
            if rows is None:
                error_msg = _TABLE_RETRIEVE_ERROR
                rows = []
        out = rows, [], [], error_msg, len(error_msg) > 0, True, "Add", False, None, None, None, None, None, None
    elif trigger_id == _CLEAR_SEL_IMPLANT_BTN and isinstance(args[5], list) and len(args[5]) > 0:
        out = no_update, [], [], "", False, True, "Add", False, None, None, None, None, None, None
    elif trigger_id == _IMPLANT_TABLE_ID:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[6]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            out = no_update, no_update, style, "", False, False, "Update", True, str(row['implant_date']), \
                row['st_ap'], row['st_ml'], row['st_dv'], row['ap_angle'], row['ml_angle']
    elif trigger_id == _DEL_IMPLANT_BTN:
        idx = (args[5][0] if (args[5] is not None) and (len(args[5]) > 0) else -1)
        rows: List[Dict] = args[6]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.IMPLANT,
                                          dict(subj_id=row['subj_id'], implant_date=row['implant_date']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", False, None, None, None, None, None, None
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_IMPLANT_BTN:
        idx = (args[5][0] if (args[5] is not None) and (len(args[5]) > 0) else -1)
        rows: List[Dict] = args[6]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        tid = DBTable.IMPLANT
        implant = dict(implant_date=args[7], st_ap=args[8], st_ml=args[9], st_dv=args[10], ap_angle=args[11],
                       ml_angle=args[12])
        is_add = (row is None)
        if is_add:
            subj_row = args[-1] and json.loads(args[-1])
            implant['subj_id'] = subj_row['subj_id']
        else:
            implant['subj_id'] = row['subj_id']
        error_msg = insert_into_table(tid, implant) if is_add else update_table_row(tid, implant)
        if error_msg is None:
            if is_add:
                rows = fetch_rows(DBTable.IMPLANT, restriction=dict(subj_id=implant['subj_id']))
                error_msg = None if rows else _TABLE_RETRIEVE_ERROR
                out = rows if rows else [], [], [], "" if rows else error_msg, rows is None, \
                    True, "Add", False, None, None, None, None, None, None
            else:
                rows[idx] = implant
                out = rows, [], [], "", False, True, "Add", False, None, None, None, None, None, None
        else:
            out[3:5] = (error_msg, True)

    return tuple(out)


_RIG_TABLE_ID: str = "rig-table"
""" The ID assigned to the Dash DataTable presenting all experiment rigs in the Lisberger lab database. """
_RIG_TABLE_COLS: List[Column] = [
    Column('rig_id', 'Rig ID', '100px', False),
    Column('rig_loc', 'Location', '300px', False)
]
""" Defined columns for the experiment rigs table. """
_RIG_ID_INPUT: str = "rig-id-input"
""" ID of text input widget for displaying/setting the 'rig_id' attribute of a new or existing rig. """
_RIG_LOC_INPUT: str = "rig-loc-input"
""" ID of text input widget for displaying/setting the 'rig_loc' attribute of a new or existing rig. """
_CLEAR_SEL_RIG_BTN: str = "rig-clear"
""" ID of button that clears the current selection in the rigs table (if any). """
_ADD_MOD_RIG_BTN = "rig-add-mod"
""" ID of button that adds a new rig to the database or modifies the location of an existing rig. """
_DEL_RIG_BTN: str = "rig-del"
""" ID of button that deletes the selected rig (if possible). """
_RIG_ALERT: str = "rig-alert"
""" ID of Bootstrap Alert that appears when an operation on the rigs table fails."""


def _rig_tabpane() -> html.Div:
    """
    Generate the contents of the 'Experiment Rigs' tab on this page.

    Returns:
        An HTML Div rendering the content of the tab pane.
    """
    error_msg = None
    rows = fetch_rows(DBTable.RIG)
    if rows is None:
        rows, error_msg = [], _TABLE_RETRIEVE_ERROR

    rig_table = _make_table_for(_RIG_TABLE_ID, rows, _RIG_TABLE_COLS)

    id_widget = dbc.FormFloating([
        dbc.Input(id=_RIG_ID_INPUT, type='text', minlength=1, maxlength=10, placeholder="Rig A"),
        dbc.Label("Rig ID (unique, 1-10 chars)"),
    ])
    loc_widget = dbc.FormFloating([
        dbc.Input(id=_RIG_LOC_INPUT, type='text', minlength=0, maxlength=50, placeholder="Room 125A"),
        dbc.Label("Rig Location (optional, up to 50 chars)"),
    ])
    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_RIG_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_RIG_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_RIG_BTN, disabled=True)
    widget_row = dbc.Row([
        dbc.Col(clear_btn, width='auto', class_name='me-3'),
        dbc.Col(add_btn, width='auto', class_name='me-1'),
        dbc.Col(del_btn, width='auto', class_name='me-3'),
        dbc.Col(id_widget, width=3, class_name='me-1'),
        dbc.Col(loc_widget, width=3)
    ], align='center', class_name='g-0 mt-2 mb-2')

    # alert raised when an error occurs in response to a user action. Otherwise hidden.
    alert_row = dbc.Row(
        dbc.Col(dbc.Alert("" if error_msg is None else error_msg, id=_RIG_ALERT, color='danger',
                          dismissable=True, is_open=(error_msg is not None)), width=10)
    )

    return html.Div([rig_table, dbc.Form([widget_row, alert_row])])


@callback(
    [Output(_RIG_TABLE_ID, 'data'), Output(_RIG_TABLE_ID, 'selected_rows'),
     Output(_RIG_TABLE_ID, 'style_data_conditional'),
     Output(_RIG_ALERT, 'children'), Output(_RIG_ALERT, 'is_open'), Output(_DEL_RIG_BTN, 'disabled'),
     Output(_ADD_MOD_RIG_BTN, 'children'), Output(_RIG_ID_INPUT, 'disabled'), Output(_RIG_ID_INPUT, 'value'),
     Output(_RIG_LOC_INPUT, 'value')],
    [Input(_CLEAR_SEL_RIG_BTN, 'n_clicks'), Input(_ADD_MOD_RIG_BTN, 'n_clicks'), Input(_DEL_RIG_BTN, 'n_clicks'),
     Input(_RIG_TABLE_ID, 'selected_rows')],
    [State(_RIG_TABLE_ID, 'selected_rows'), State(_RIG_TABLE_ID, 'data'), State(_RIG_ID_INPUT, 'value'),
     State(_RIG_LOC_INPUT, 'value')]
)
def rig_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 10

    if trigger_id == _CLEAR_SEL_RIG_BTN and isinstance(args[4], list) and len(args[4]) > 0:
        out = no_update, [], [], "", False, True, "Add", False, None, None
    elif trigger_id == _RIG_TABLE_ID:
        idx = (args[3][0] if (args[3] is not None) and (len(args[3]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            out = no_update, no_update, style, "", False, False, "Update", True, row['rig_id'], row['rig_loc']
    elif trigger_id == _DEL_RIG_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.RIG, dict(rig_id=row['rig_id']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", False, None, None
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_RIG_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        tid = DBTable.RIG
        rig = dict(rig_id=args[6], rig_loc=args[7])
        is_add = (row is None) or not row_exists(tid, rig)
        error_msg = insert_into_table(tid, rig) if is_add else update_table_row(tid, rig)
        if error_msg is None:
            if is_add:
                rows = fetch_rows(DBTable.RIG)
                error_msg = None if rows else _TABLE_RETRIEVE_ERROR
                out = rows if rows else [], [], [], "" if rows else error_msg, rows is None, \
                    True, "Add", False, None, None
            else:
                rows[idx] = rig
                out = rows, [], [], "", False, True, "Add", False, None, None
        else:
            out[3:5] = (error_msg, True)

    return tuple(out)


_BA_TABLE_ID: str = "ba-table"
""" The ID assigned to the Dash DataTable presenting the table of brain areas in the lab database. """
_BA_TABLE_COLS: List[Column] = [Column('ba_name', 'Brain Area', '200px', False)]
""" Defined columns for the brain areas table. """
_BA_NAME_INPUT: str = "ba-name-input"
""" ID of text input widget for displaying/setting the 'ba_name' attribute of a new or existing brain area. """
_CLEAR_SEL_BA_BTN: str = "ba-clear"
""" ID of button that clears the current selection in the brain areas table (if any). """
_ADD_MOD_BA_BTN = "ba-add-mod"
""" ID of button that adds a new brain area to the database or modifies the name of an existing one. """
_DEL_BA_BTN: str = "ba-del"
""" ID of button that deletes the selected brain area (if possible). """
_BA_ALERT: str = "ba-alert"
""" ID of Bootstrap Alert that appears when an operation fails on the brain areas table. """

_NT_TABLE_ID: str = "nt-table"
""" The ID assigned to the Dash DataTable presenting the table of neuron types in the lab database. """
_NT_TABLE_COLS: List[Column] = [
    Column('nt_name', 'Neuron Type', '200px', False)
]
""" Defined columns for the brain areas table. """
_NT_NAME_INPUT: str = "nt-name-input"
""" ID of text input widget for displaying/setting the 'ba_name' attribute of a new or existing brain area. """
_CLEAR_SEL_NT_BTN: str = "nt-clear"
""" ID of button that clears the current selection in the neuron types table (if any). """
_ADD_MOD_NT_BTN = "nt-add-mod"
""" ID of button that adds a new neuron type to the database or modifies the name of an existing one. """
_DEL_NT_BTN: str = "nt-del"
""" ID of button that deletes the selected neuron type (if possible). """
_NT_ALERT: str = "nt-alert"
""" ID of Bootstrap Alert that appears when an operation fails on the neuron types table. """


def _brain_tabpane() -> html.Div:
    """
    Generate the contents of the 'Brain Areas & Neuron Types' tab on this page. This tab pane manages the content of
    two simple database tables, one displaying the names of brain regions studied in the lab and the other displaying
    neuron types. Both tables have the same structure, with an auto-incrementing integer ID that is the primary key and
    is opaque to the user, plus a user-facing name for the brain area or neuron type.

    Returns:
        An HTML Div rendering the content of the tab pane.
    """
    # the Brain Areas table
    error_msg = None
    rows = fetch_rows(DBTable.BRAIN_AREA)
    if rows is None:
        rows, error_msg = [], _TABLE_RETRIEVE_ERROR

    ba_table = _make_table_for(_BA_TABLE_ID, rows, _BA_TABLE_COLS)

    ba_name_widget = dbc.FormFloating([
        dbc.Input(id=_BA_NAME_INPUT, type='text', minlength=3, maxlength=50, placeholder="Cortex"),
        dbc.Label("Brain area name (unique, 3-50 characters)"),
    ])
    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_BA_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_BA_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_BA_BTN, disabled=True)
    widget_row = dbc.Row([
        dbc.Col(clear_btn, width='auto', class_name='me-2'),
        dbc.Col(add_btn, width='auto', class_name='me-1'),
        dbc.Col(del_btn, width='auto', class_name='me-3'),
        dbc.Col(ba_name_widget, width=7)
    ], align='center', class_name='g-0 mt-2 mb-2')

    alert_row = dbc.Row(
        dbc.Col(dbc.Alert("" if error_msg is None else error_msg, id=_BA_ALERT, color='danger',
                          dismissable=True, is_open=(error_msg is not None)), width=12)
    )

    ba_div = html.Div([ba_table, dbc.Form([widget_row, alert_row])])

    # the Neuron Types table
    error_msg = None
    rows = fetch_rows(DBTable.NEURON_TYPE)
    if rows is None:
        rows, error_msg = [], _TABLE_RETRIEVE_ERROR

    nt_table = _make_table_for(_NT_TABLE_ID, rows, _NT_TABLE_COLS)

    nt_name_widget = dbc.FormFloating([
        dbc.Input(id=_NT_NAME_INPUT, type='text', minlength=3, maxlength=50, placeholder="Purkinje Cell"),
        dbc.Label("Neuron type name (unique, 3-50 characters)"),
    ])
    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_NT_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_NT_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_NT_BTN, disabled=True)
    widget_row = dbc.Row([
        dbc.Col(clear_btn, width='auto', class_name='me-2'),
        dbc.Col(add_btn, width='auto', class_name='me-1'),
        dbc.Col(del_btn, width='auto', class_name='me-3'),
        dbc.Col(nt_name_widget, width=7)
    ], align='center', class_name='g-0 mt-2 mb-2')

    # alert raised when an error occurs in response to a user action. Otherwise hidden.
    alert_row = dbc.Row(
        dbc.Col(dbc.Alert("" if error_msg is None else error_msg, id=_NT_ALERT, color='danger',
                          dismissable=True, is_open=(error_msg is not None)), width=12)
    )

    nt_div = html.Div([nt_table, dbc.Form([widget_row, alert_row])])

    # each table (and associated widgets) occupy one half of the tab pane, split horizontally
    return html.Div(
        dbc.Row([
            dbc.Col(ba_div, width=6), dbc.Col(nt_div, width=6)
        ])
    )


@callback(
    [Output(_BA_TABLE_ID, 'data'), Output(_BA_TABLE_ID, 'selected_rows'),
     Output(_BA_TABLE_ID, 'style_data_conditional'),
     Output(_BA_ALERT, 'children'), Output(_BA_ALERT, 'is_open'), Output(_DEL_BA_BTN, 'disabled'),
     Output(_ADD_MOD_BA_BTN, 'children'), Output(_BA_NAME_INPUT, 'value')],
    [Input(_CLEAR_SEL_BA_BTN, 'n_clicks'), Input(_ADD_MOD_BA_BTN, 'n_clicks'), Input(_DEL_BA_BTN, 'n_clicks'),
     Input(_BA_TABLE_ID, 'selected_rows')],
    [State(_BA_TABLE_ID, 'selected_rows'), State(_BA_TABLE_ID, 'data'), State(_BA_NAME_INPUT, 'value')]
)
def brain_area_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 8

    if trigger_id == _CLEAR_SEL_BA_BTN and isinstance(args[4], list) and len(args[4]) > 0:
        out = no_update, [], [], "", False, True, "Add", None
    elif trigger_id == _BA_TABLE_ID:
        idx = (args[3][0] if (args[3] is not None) and (len(args[3]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            out = no_update, no_update, style, "", False, False, "Update", row['ba_name']
    elif trigger_id == _DEL_BA_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.BRAIN_AREA, dict(ba_id=row['ba_id']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", None
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_BA_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row is None:
            # must be adding a new brain area
            error_msg = insert_into_table(DBTable.BRAIN_AREA, dict(ba_name=args[6]))
            if error_msg is None:
                rows = fetch_rows(DBTable.BRAIN_AREA)
                error_msg = None if rows else _TABLE_RETRIEVE_ERROR
            out = rows if rows else [], [], [], error_msg if error_msg else "", error_msg is not None, True, "Add", None
        else:
            # must be modifying the name of the currently selected brain area
            error_msg = update_table_row(DBTable.BRAIN_AREA, dict(ba_id=row['ba_id'], ba_name=args[6]))
            if error_msg is None:
                rows[idx]['ba_name'] = args[6]
                out = rows, [], [], "", False, True, "Add", None
            else:
                out[3:5] = (error_msg, True)

    return tuple(out)


@callback(
    [Output(_NT_TABLE_ID, 'data'), Output(_NT_TABLE_ID, 'selected_rows'),
     Output(_NT_TABLE_ID, 'style_data_conditional'),
     Output(_NT_ALERT, 'children'), Output(_NT_ALERT, 'is_open'), Output(_DEL_NT_BTN, 'disabled'),
     Output(_ADD_MOD_NT_BTN, 'children'), Output(_NT_NAME_INPUT, 'value')],
    [Input(_CLEAR_SEL_NT_BTN, 'n_clicks'), Input(_ADD_MOD_NT_BTN, 'n_clicks'), Input(_DEL_NT_BTN, 'n_clicks'),
     Input(_NT_TABLE_ID, 'selected_rows')],
    [State(_NT_TABLE_ID, 'selected_rows'), State(_NT_TABLE_ID, 'data'), State(_NT_NAME_INPUT, 'value')]
)
def neuron_type_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 8

    if trigger_id == _CLEAR_SEL_NT_BTN and isinstance(args[4], list) and len(args[4]) > 0:
        out = no_update, [], [], "", False, True, "Add", None
    elif trigger_id == _NT_TABLE_ID:
        idx = (args[3][0] if (args[3] is not None) and (len(args[3]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            out = no_update, no_update, style, "", False, False, "Update", row['nt_name']
    elif trigger_id == _DEL_NT_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.NEURON_TYPE, dict(nt_id=row['nt_id']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", None
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_NT_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row is None:
            # must be adding a new neuron type
            error_msg = insert_into_table(DBTable.NEURON_TYPE, dict(nt_name=args[6]))
            if error_msg is None:
                rows = fetch_rows(DBTable.NEURON_TYPE)
                error_msg = None if rows else _TABLE_RETRIEVE_ERROR
            out = rows if rows else [], [], [], error_msg if error_msg else "", error_msg is not None, True, "Add", None
        else:
            # must be modifying the name of the currently selected neuron type
            error_msg = update_table_row(DBTable.NEURON_TYPE, dict(nt_id=row['nt_id'], nt_name=args[6]))
            if error_msg is None:
                rows[idx]['nt_name'] = args[6]
                out = rows, [], [], "", False, True, "Add", None
            else:
                out[3:5] = (error_msg, True)

    return tuple(out)


_STUDY_TABLE_ID: str = "study-table"
""" The ID assigned to the Dash DataTable presenting the table of research studies in the lab database. """
_STUDY_TABLE_COLS: List[Column] = [
    Column('study_title', 'Title', '200px', False),
    Column('study_lead_full', 'Project Lead', '200px', False),
    Column('study_desc', 'Description', '700px', False),
    Column('n_pubs', '#Pubs', '30px', False)   # how many publications are associated with this study?
]
""" Defined columns for the research studies table. """
_STUDY_TITLE_INPUT: str = "study-title-input"
""" ID of text input widget for displaying/setting the 'study_title' attribute of a new or existing study. """
_STUDY_LEAD_SEL: str = "study-lead-sel"
""" ID of Bootstrap Select widget for displaying/setting the 'study_lead' attribute of a new or existing study. """
_STUDY_DESC_AREA: str = "study-desc-area"
""" ID of Bootstrap Textarea for displaying/setting the 'study_desc' attribute of a new or existing study. """
_CLEAR_SEL_STUDY_BTN: str = "study-clear"
""" ID of button that clears the current selection in the research studies table (if any). """
_ADD_MOD_STUDY_BTN = "study-add-mod"
""" ID of button that adds a new study to the database or modifies the seconary attributes of an existing one. """
_DEL_STUDY_BTN: str = "study-del"
""" ID of button that deletes the selected research study (if possible). """
_STUDY_ALERT: str = "study-alert"
""" ID of Bootstrap Alert that appears when an operation fails on the research studies table. """
_STUDY_PUBS_DROP: str = "study-pubs-drop"
""" ID of Dash Dropdown component for displaying/selecting publications related to the currently selected study. """
_STUDY_PUBS_CARD: str = "study-pubs_card"
""" ID of Bootstrap Card holding the related publications dropdown; it is hidden if no study is currently selected. """

_PUB_TABLE_ID: str = "pub-table"
""" The ID assigned to the Dash DataTable presenting the table of research publications in the lab database. """
_PUB_TABLE_COLS: List[Column] = [
    Column('citation', 'Citation', '700px', False),
    Column('link', 'Link', '50px', True)   # uses Markdown to present the pub DOI as a link to the online publication
]
""" Defined columns for the brain areas table. """
_PUB_CITE_AREA: str = "pub-cite-area"
""" ID of Bootstrap Textarea for displaying/setting the 'citation' attribute of a new or existing publication. """
_PUB_DOI_AREA: str = "pub-doi-area"
""" ID of Bootstrap Textarea for displaying/setting the 'doi' attribute of a new or existing publication. """
_CLEAR_SEL_PUB_BTN: str = "pub-clear"
""" ID of button that clears the current selection in the research publications table (if any). """
_ADD_MOD_PUB_BTN = "pub-add-mod"
""" ID of button that adds a new publication to the database or modifies secondary attributes of an existing one. """
_DEL_PUB_BTN: str = "pub-del"
""" ID of button that deletes the selected research publication (if possible). """
_PUB_ALERT: str = "pub-alert"
""" ID of Bootstrap Alert that appears when an operation fails on the research publications table. """


def _prepare_rows_for_study_table() -> Optional[List[Dict[str, AttributeValue]]]:
    """
    Helper method retrieves the contents of the Study, User, and StudyPub tables from the lab database and uses the
    results to generate the contents of the table listing all research studies in the lab database. The StudyPub mapping
    table is needed to determine how many publications are associated with each study, and the User table is needed to
    access the full name (rather than username) of a study lead.

    Returns:
        The list of table rows that are displayed in the research studies table. Each row is a dictionary with these
            keys: 'study_id' (the integer ID, not shown in table); 'study_title'; 'study_lead' (the username for the
            project lead, not shown in table); 'study_lead_full' (the full name of the project lead); 'study_desc';
            and 'n_pubs' (# of publications related to study). If an error occurs while retrieving database content,
            returns None.
    """
    rows = fetch_rows(DBTable.STUDY)
    study_to_pub_rows = fetch_rows(DBTable.STUDY_TO_PUB)
    user_rows = fetch_rows(DBTable.USER)
    if (rows is None) or (study_to_pub_rows is None) or (user_rows is None):
        get_application_logger().debug("Unable to prepare rows for research studies table; database retrieval error")
        return None
    for r in rows:
        r['n_pubs'] = [(k['study_id'] == r['study_id']) for k in study_to_pub_rows].count(True)
        for user_row in user_rows:
            if user_row['username'] == r['study_lead']:
                r['study_lead_full'] = user_row['full_name']
                break
        if 'study_lead_full' not in r:
            get_application_logger().debug(
                f"Database inconsistency! Did not find study lead with username '{r['study_lead']}'.")
            return None
    return rows


def _prepare_rows_for_publication_table() -> Optional[List[Dict[str, AttributeValue]]]:
    """
    Helper method retrieves the contents of the Publication table from the lab database and generates the contents of
    the user-facing Dash Datatable listing the publications. The Digital Object ID ('doi' key) is turned into a link
    that the user can click to go to the online publication.

    Returns:
        The list of table rows that are displayed in the research publications table. Each row is a dictionary with
            these keys: 'pub_id' (the integer ID, not shown); 'citation'; 'doi' (not shown); and 'link' (a Markdown-
            formatted string that turns the DOI into a URL. If an error occurs while retrieving database content,
            returns None.
    """
    rows = fetch_rows(DBTable.PUB)
    if rows is None:
        get_application_logger().debug("Unable to prepare rows for publications table; database retrieval error")
        return None
    for r in rows:
        r['link'] = f"[[&#x21d7;]]({r['doi']})" if len(r['doi']) > 0 else ""
    return rows


def pub_dropdown_options(pub_rows: List[Dict[str, AttributeValue]]) -> List[Dict]:
    """
    Helper method fetches all publications currently in the database and generates the 'options' attribute for a Dash
    Dropdown component to select one or more of those publications. This attribute is a list of dictionaries, each with
    keys 'label', 'value', and 'title'. The 'label' is set to the citation string truncated to roughly 50 characters if
    necessary, 'value' is set to the publication ID (an integer), and 'title' is set to the full citation string if it
    was truncated. The Dropdown component will display the 'title' in a tooltip on hover.

    Args:
        pub_rows: The list of rows from the publications table in the lab database.
    Returns:
        The options list for a Dash Dropdown component, as described. The list will be an empty if an error occurs
            while retrieving the publication info from the database.
    """
    options: List[Dict] = list()
    for pub in pub_rows:
        citation = pub['citation']
        truncated = None
        if len(citation) > 50:
            comma_idx = citation.find(',')
            quote_idx = citation.rfind('"')
            if quote_idx == -1:
                quote_idx = citation.rfind("'")
            if comma_idx > -1 and quote_idx > -1:
                truncated = (citation[0:comma_idx] + '...' + citation[quote_idx + 1:]).strip()
                if len(truncated) > 50:
                    truncated = f"{truncated[:50]}..."
            else:
                truncated = f"{citation[:50]}..."
        options.append({'label': truncated if truncated else citation, 'value': pub['pub_id'],
                        'title': citation if truncated else None})
    return options


def _research_tabpane() -> html.Div:
    """
    Generate the contents of the 'Research' tab on this page. This tab pane manages the content of two database tables,
    one displaying the lab's past or ongoing research projects (Study table) and the other displaying research
    publications (Publication table). A mapping table (StudyPub) is also exposed on the table; this is how one or more
    publications are associated with a particular study. For compactness, we use a Bootstrap Accordion component; each
    table and its associated edit form are encapsulated in a separate accordion item.

    Returns:
        An HTML Div rendering the content of the tab pane.
    """
    # retrieve all studies and publications. We also need the users table so we can display full name of a project lead
    # in the studies table
    error_msg = None
    rows = _prepare_rows_for_study_table()
    pub_rows = _prepare_rows_for_publication_table()
    user_rows = fetch_rows(DBTable.USER)
    if (rows is None) or (user_rows is None) or (pub_rows is None):
        rows, pub_rows, error_msg = [], [], _TABLE_RETRIEVE_ERROR

    study_table = _make_table_for(_STUDY_TABLE_ID, rows, _STUDY_TABLE_COLS, height=500, num_lines_per_row=8)

    title_grp = dbc.InputGroup([
        dbc.InputGroupText("Title"),
        dbc.Input(id=_STUDY_TITLE_INPUT, type='text', minlength=3, maxlength=50,
                  placeholder="Concise, unique project title (3-50 chars)"),
    ])
    lead_grp = dbc.InputGroup([
        dbc.InputGroupText("Project Lead"),
        dbc.Select(
            id=_STUDY_LEAD_SEL,
            options=[{'label': r['full_name'], 'value': r['username']} for r in user_rows],
            value=None if len(user_rows) == 0 else user_rows[0]['username']
        )
    ])
    desc_grp = dbc.InputGroup([
        dbc.InputGroupText("Description"),
        dbc.Textarea(id=_STUDY_DESC_AREA, minlength=0, maxlength=2048, rows=4, value=None,
                     placeholder='Enter a description of the research project (optional, up to 2048 chars)')
    ])
    related_pubs_card = dbc.Card([
        dbc.CardHeader("Related publications"),
        dbc.CardBody(dbc.Row([
            dcc.Dropdown(id=_STUDY_PUBS_DROP, multi=True, clearable=True, searchable=False,
                         options=pub_dropdown_options(pub_rows), value=[]),
            dbc.Label("Select one or more publications. Hover over any entry to see the full citation.", size='sm')
        ]))
    ], id=_STUDY_PUBS_CARD, style=dict(display='none'))

    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_STUDY_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_STUDY_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_STUDY_BTN, disabled=True)
    alert = dbc.Alert("" if error_msg is None else error_msg, id=_STUDY_ALERT, color='danger',
                      dismissable=True, is_open=(error_msg is not None))

    edit_form = dbc.Form([
        dbc.Row(dbc.Col(title_grp, width=12), class_name='g-0 mb-2'),
        dbc.Row(dbc.Col(lead_grp, width=12), class_name='g-0 mb-2'),
        dbc.Row(dbc.Col(desc_grp, width=12), class_name='g-0 mb-2'),
        dbc.Row(dbc.Col(related_pubs_card, width=12), class_name='g-0 mb-3'),
        dbc.Row([
            dbc.Col(clear_btn, width='auto', class_name='me-2'),
            dbc.Col(add_btn, width='auto', class_name='me-2'),
            dbc.Col(del_btn, width='auto')
        ], class_name='g-0 mb-3'),
        dbc.Row(dbc.Col(alert, width=12), class_name='g-0')
    ])

    study_div = html.Div(dbc.Row([dbc.Col(study_table, width=6), dbc.Col(edit_form, width=6)]))

    # the research publications table.
    pub_table = _make_table_for(_PUB_TABLE_ID, pub_rows, _PUB_TABLE_COLS, height=500, num_lines_per_row=4)

    cite_grp = dbc.InputGroup([
        dbc.InputGroupText("Citation"),
        dbc.Textarea(id=_PUB_CITE_AREA, minlength=50, maxlength=500, rows=4, value=None,
                     placeholder='Article citation (required; 50-500 chars)')
    ])
    doi_grp = dbc.InputGroup([
        dbc.InputGroupText("DOI"),
        dbc.Textarea(id=_PUB_DOI_AREA, minlength=0, maxlength=100, rows=2, value=None,
                     placeholder='Digital object identifier (optional, 100 chars max)')
    ])
    clear_btn = dbc.Button("Clear selection", id=_CLEAR_SEL_PUB_BTN)
    add_btn = dbc.Button("Add", id=_ADD_MOD_PUB_BTN)
    del_btn = dbc.Button("Delete", id=_DEL_PUB_BTN, disabled=True)
    alert = dbc.Alert("" if error_msg is None else error_msg, id=_PUB_ALERT, color='danger',
                      dismissable=True, is_open=(error_msg is not None))

    edit_form = dbc.Form([
        dbc.Row(dbc.Col(cite_grp, width=12), class_name='g-0 mb-2'),
        dbc.Row(dbc.Col(doi_grp, width=12), class_name='g-0 mb-3'),
        dbc.Row([
            dbc.Col(clear_btn, width='auto', class_name='me-2'),
            dbc.Col(add_btn, width='auto', class_name='me-2'),
            dbc.Col(del_btn, width='auto')
        ], class_name='g-0 mb-3'),
        dbc.Row(dbc.Col(alert, width=12)),
    ])

    pub_div = html.Div(dbc.Row([dbc.Col(pub_table, width=6), dbc.Col(edit_form, width=6)]))

    return html.Div(dbc.Accordion([
        dbc.AccordionItem(study_div, title='Projects'),
        dbc.AccordionItem(pub_div, title='Publications')
    ]))


@callback(
    [Output(_STUDY_TABLE_ID, 'data'), Output(_STUDY_TABLE_ID, 'selected_rows'),
     Output(_STUDY_TABLE_ID, 'style_data_conditional'),
     Output(_STUDY_ALERT, 'children'), Output(_STUDY_ALERT, 'is_open'), Output(_DEL_STUDY_BTN, 'disabled'),
     Output(_ADD_MOD_STUDY_BTN, 'children'), Output(_STUDY_TITLE_INPUT, 'value'), Output(_STUDY_LEAD_SEL, 'value'),
     Output(_STUDY_DESC_AREA, 'value'), Output(_STUDY_PUBS_CARD, 'style'), Output(_STUDY_PUBS_DROP, 'options'),
     Output(_STUDY_PUBS_DROP, 'value')],
    [Input(_PUB_TABLE_ID, 'data'), Input(_CLEAR_SEL_STUDY_BTN, 'n_clicks'), Input(_ADD_MOD_STUDY_BTN, 'n_clicks'),
     Input(_DEL_STUDY_BTN, 'n_clicks'), Input(_STUDY_TABLE_ID, 'selected_rows')],
    [State(_STUDY_TABLE_ID, 'selected_rows'), State(_STUDY_TABLE_ID, 'data'), State(_STUDY_TITLE_INPUT, 'value'),
     State(_STUDY_LEAD_SEL, 'value'), State(_STUDY_DESC_AREA, 'value'), State(_STUDY_PUBS_DROP, 'value')]
)
def study_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 13

    if trigger_id == _PUB_TABLE_ID:
        # a deletion from the pubs table can affect the 'n_pubs' derived field in the study table. Any change in the
        # pubs table affects contents of the related pubs dropdown.
        study_rows = _prepare_rows_for_study_table()
        drop_options = pub_dropdown_options(args[0])
        if study_rows is not None:
            out[0], out[11] = study_rows, drop_options
        else:
            out[3:5] = (_TABLE_RETRIEVE_ERROR, True)
    elif trigger_id == _CLEAR_SEL_STUDY_BTN and isinstance(args[5], list) and len(args[5]) > 0:
        out = no_update, [], [], "", False, True, "Add", None, no_update, None, dict(display='none'), no_update, \
            no_update
    elif trigger_id == _STUDY_TABLE_ID:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[6]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            map_rows = fetch_rows(DBTable.STUDY_TO_PUB, restriction=dict(study_id=row['study_id']))
            related_pub_ids = [r['pub_id'] for r in map_rows] if map_rows else []
            out = no_update, no_update, style, "", False, False, "Update", row['study_title'], row['study_lead'], \
                row['study_desc'], None, no_update, related_pub_ids
    elif trigger_id == _DEL_STUDY_BTN:
        idx = (args[5][0] if (args[5] is not None) and (len(args[5]) > 0) else -1)
        rows: List[Dict] = args[6]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.STUDY, dict(study_id=row['study_id']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", None, no_update, None, dict(display='none'), no_update, \
                    no_update
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_STUDY_BTN:
        idx = (args[5][0] if (args[5] is not None) and (len(args[5]) > 0) else -1)
        rows: List[Dict] = args[6]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        study_entry = dict(study_title=args[7], study_lead=args[8], study_desc=args[9])
        if row is None:
            error_msg = insert_into_table(DBTable.STUDY, study_entry)
        else:
            # user may edit study title, lead, description, OR the set of related publications
            study_entry['study_id'] = row['study_id']   # need PK since we're modifying an existing study!
            error_msg = update_table_row(DBTable.STUDY, study_entry)
            if error_msg is None:
                related_pub_ids = {v for v in args[10]} if isinstance(args[10], list) else set()
                error_msg = update_mapping_table(DBTable.STUDY_TO_PUB, row['study_id'], related_pub_ids)

        # whether it is an add or modify, we repopulate all table rows to make sure we correctly set the derived fields
        if error_msg is None:
            rows = _prepare_rows_for_study_table()
            if rows is None:
                rows = []
                error_msg = _TABLE_RETRIEVE_ERROR
        out = rows, [], [], error_msg, error_msg is not None, True, "Add", None, no_update, None, \
            dict(display='none'), no_update, no_update

    return tuple(out)


'''
@callback(
    [Output(_STUDY_PUBS_CARD, 'style'), Output(_STUDY_PUBS_DROP, 'value')],
    [Input(_STUDY_TABLE_ID, 'selected_rows')], [State(_STUDY_TABLE_ID, 'data')]
)
def on_study_row_selected(*args):
    if callback_context.triggered is None:
        return no_update, no_update
    idx = (args[0][0] if (args[0] is not None) and (len(args[0]) > 0) else -1)
    rows: List[Dict] = args[1]
    row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
    if row is None:
        return dict(display='none'), no_update
    map_rows = fetch_rows(DBTable.STUDY_TO_PUB, restriction=dict(study_id=row['study_id']))
    if map_rows is None:
        return dict(display='none'), no_update
    return None, [r['pub_id'] for r in map_rows]


@callback(
    Output(_STUDY_PUBS_DROP, 'options'), [Input(_PUB_TABLE_ID, 'data')]
)
def on_change_in_pubs_table(pub_rows):
    return None if callback_context.triggered is None else pub_dropdown_options(pub_rows)
'''


@callback(
    [Output(_PUB_TABLE_ID, 'data'), Output(_PUB_TABLE_ID, 'selected_rows'),
     Output(_PUB_TABLE_ID, 'style_data_conditional'),
     Output(_PUB_ALERT, 'children'), Output(_PUB_ALERT, 'is_open'), Output(_DEL_PUB_BTN, 'disabled'),
     Output(_ADD_MOD_PUB_BTN, 'children'), Output(_PUB_CITE_AREA, 'value'), Output(_PUB_DOI_AREA, 'value')],
    [Input(_CLEAR_SEL_PUB_BTN, 'n_clicks'), Input(_ADD_MOD_PUB_BTN, 'n_clicks'), Input(_DEL_PUB_BTN, 'n_clicks'),
     Input(_PUB_TABLE_ID, 'selected_rows')],
    [State(_PUB_TABLE_ID, 'selected_rows'), State(_PUB_TABLE_ID, 'data'), State(_PUB_CITE_AREA, 'value'),
     State(_PUB_DOI_AREA, 'value')]
)
def pub_table_callback(*args):
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if (ctx.triggered is not None) else ""
    out = [no_update] * 9

    if trigger_id == _CLEAR_SEL_PUB_BTN and isinstance(args[4], list) and len(args[4]) > 0:
        out = no_update, [], [], "", False, True, "Add", None, None
    elif trigger_id == _PUB_TABLE_ID:
        idx = (args[3][0] if (args[3] is not None) and (len(args[3]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            style = [{"if": {"row_index": idx}, "background-color": "rgba(176, 196, 222, 0.5)"}]
            out = no_update, no_update, style, "", False, False, "Update", row['citation'], row['doi']
    elif trigger_id == _DEL_PUB_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        if row:
            error_msg = delete_from_table(DBTable.PUB, dict(pub_id=row['pub_id']))
            if error_msg is None:
                rows.pop(idx)
                out = rows, [], [], "", False, True, "Add", None, None
            else:
                out[3:5] = (error_msg, True)
    elif trigger_id == _ADD_MOD_PUB_BTN:
        idx = (args[4][0] if (args[4] is not None) and (len(args[4]) > 0) else -1)
        rows: List[Dict] = args[5]
        row = rows[idx] if (isinstance(rows, list) and (-1 < idx < len(rows))) else None
        pub_entry = dict(citation=args[6], doi=args[7])
        if row is None:
            error_msg = insert_into_table(DBTable.PUB, pub_entry)
        else:
            pub_entry['pub_id'] = row['pub_id']   # need PK if modifying an existing publication!
            error_msg = update_table_row(DBTable.PUB, pub_entry)

        # whether it is an add or modify, we repopulate all table rows to make sure we correctly set the derived fields
        if error_msg is None:
            rows = _prepare_rows_for_publication_table()
            if rows is None:
                rows = []
                error_msg = _TABLE_RETRIEVE_ERROR
        out = rows, [], [], error_msg, error_msg is not None, True, "Add", None, None

    return tuple(out)
