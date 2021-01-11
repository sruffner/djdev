"""
curate_panels: The HTML/Dash panels for the "curate" page in web-based interface to the Lisberger laboratory database.

The "curate" page (see curate.py) lets the user view and manage the content of all "manual" tables in the laboratory
database. These are small tables that organize information about lab members, experiment subjects, research projects,
brain regions, etc.

The "curate" page provides a tab panel for accessing five distinct manual table views: lab members, experiment rigs,
experiment subjects, brain regions, and research projects. The information in these tables is used to further describe
and categorize the actual experimental data recorded in the laboratory.

A "table view" defines how the underlying database table should be presented to the user while encapsulating the
DataJoint code for accessing and modifying table content. The table views are defined in the database.table_view
module.

The panels defined here actually implement an HTML/Dash-based web interface to the various table views. Almost all of
the implementation is in the base class _BasePanel. Subclasses of _BasePanel define the actual panels embedded in
the "curate" page.

Created on Wed Aug  5 18:06:14 2020

@author: sruffner
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import dash
import dash_html_components as html
import dash_bootstrap_components as dbc
import dash_core_components as dcc
import dash_table as dt
from dash.dependencies import Input, Output, State
from typing import List, Any, Optional

import database.table_views as tv


# noinspection PyUnusedLocal,PyShadowingNames
class _BasePanel:
    """Base class for all panels laid out on the "curate" page.

    _BasePanel defines common and optional functionality for all of the curate panels. Concrete subclasses tailor its
    behavior for a particular table view by overriding select methods.

    The base implementation displays the contents of the underlying table view in a Dash DataTable with single-row
    selection. An "Add" button raises a modal form by which the user can add one or more entries to the table. A
    "Remove" button -- enabled only when an entry is selected in the DataTable -- triggers removal of the selected
    entity.

    In addition, _BasePanel supports the display of subpanels -- which also descend from _BasePanel -- within an
    "accordion"-style widget below the main table. This design compactly encapsulates table views that are related to
    the parent table view, typically ia a cross-reference or mapping table view -- see the MappingView base class and
    related subclasses in database.table_views. In this scheme, the parent table view is the "source table" for the
    mapping relationship, and each table view in a subpanel is the "destination table". When such subpanels are
    included, _BasePanel includes a "Related information" button that raises a modal form by which the user can select
    which entities in a "destination" table are related to a selected entity from the source table.

    Any caught errors are displayed in a Dash Bootstrap "Alert" element either below the DataTable in the panel or on
    one of the modal entry forms.

    The implementation of _BasePanel is rather complex in order to accommodate the various manual tables in the
    Lisberger lab database and their relationships. On the other hand, that serves to simplify the definition of the
    concrete subclasses that implement each of the panels that appears on the "curate" page.
    """

    def __init__(self, app: dash.Dash, view: tv.BaseTableView, prefix: str, subpanels: List[_BasePanel] = None):
        """
        Construct a user-facing panel associated with a manually curated table in the laboratory database. The panel
        displays the table contents IAW the specified table view, and provides mechanisms for adding new entities to
        the table or removing a selected entity.

        If the main table view is related to another table view, that latter view can be displayed in a subpanel that,
        in turn, is embedded in an accordion-style widget within the main panel. Furthermore, if the two views are
        related through a cross-reference or associative table, the subpanel should expose that mapping via the
        mapping_view_for_subpanel() method. In that scenario, the parent _BasePanel() will provide the infrastructure
        for selecting what entity(ies) in the subpanel table view are related to a given entity in the parent panel
        table view.

        Args:
            app (dash.Dash): The Dash application object.
            view (tv.BaseTableView): The database table view exposed in this panel.
            prefix (str): A short string used to define unique IDs for HTML/Dash components rendered in and managed by
                this panel.
            subpanels (List[_BasePanel]): Subpanels that should be encapsulated in this panel. The base implementation
                uses an accordion widget to display any subpanels.
        """
        self._app = app
        self._table_view = view
        self._prefix = prefix
        self.__subpanels = list(subpanels) if subpanels else []
        if hasattr(self, '_callbacks'):
            self._callbacks(self._app)

    def id_prefix(self) -> str:
        """ Return a short string used to uniquely identify the tab or other component associated with this panel. It
        is also used as a prefix to uniquely label the various HTML/Dash components within the panel layout."""
        return self._prefix

    def tab_label(self) -> str:
        """ Return a short (less than 20 chars) user-facing label for this panel."""
        return self._table_view.label()

    def mapping_view_for_subpanel(self) -> Optional[tv.MappingView]:
        """
        If this panel represents a subpanel encapsulated within another _BasePanel, and the database table in the
        subpanel is related to the table in the parent panel via an associative mapping, this method returns the
        corresponding mapping table view (see tv.MappingView)

        The _BasePanel implementation returns None. Any subclass representing a subpanel as described should override
        the method and supply the necessary mapping view. The _BasePanel implementation requires this mapping view to
        add layout elements and callbacks (in the parent panel) to access and update the relevant entity mapping.

        Returns:
            tv.MappingView: The mapping table view as described, or None if this is not a subpanel or, if it is, its
                database table is not related to the parent panel table through an associative mapping.
        """
        return None

    def layout(self) -> List[Any]:
        """
        Get the layout for this panel.

        This base implementation renders a Dash DataTable displaying the contents of the primary table exposed by the
        underlying table view, Add/Remove buttons, and a modal entry form for adding a new table entity. In addition,
        any subpanels specified in the constructor are embedded in an accordion widget. Furthermore, if any subpanel is
        related to the main panel through an associative table mapping defined by tv.MappingView, then the base
        implementation includes an additional modal form to modify the mapping(s), as well as an additional button to
        raise that form. The Dash callbacks encapsulated by _callbacks() specifically handle this layout.

        Returns:
            List[Any]: List of HTML/Dash/Dash Bootstrap components comprising this panel.
        """
        pfx = self._prefix
        layout = [
            html.Div(id=f"{pfx}_table_div", children=[self._data_table()]),
            dbc.Alert("", id=f"{self._prefix}_alert", color="danger", dismissable=True, duration=10000, fade=True,
                      is_open=False, className="mt-3 mb-1"),
            dbc.Button("Add", id=f"add_{pfx}_btn", color="primary", className="mr-2 mt-3"),
            dbc.Button("Remove", id=f"del_{pfx}_btn", color="primary", className="mr-2 mt-3", disabled=True),
            dbc.Modal(
                [
                    dbc.ModalHeader(f"Add {self._table_view.row_label()}"),
                    dbc.ModalBody(self._entry_form()),
                    dbc.ModalFooter(
                        dbc.Row([
                            dbc.Button("Add", id=f"{pfx}_entry_submit_btn", color="primary"),
                            dbc.Button("Done", id=f"{pfx}_entry_done_btn", color="primary", className="ml-3")
                        ])
                    )
                ],
                id=f"{pfx}_entry_form", backdrop="static", size="xl", centered=True
            ),
            # the number inside this invisible DIV is reset to 0 when the add-entry modal window is raised and set to
            # 1 if any changes are made in the add-entry modal window while it is open
            html.Div(children=0, id=f"{pfx}_entry_added_div", style={"display": "none"}),
        ]

        accordion_cards = []
        assoc_form_grps = []
        related_btn_labels = []
        for subpanel in self.__subpanels:
            sub_pfx = subpanel.id_prefix()
            card = dbc.Card([
                dbc.CardHeader(
                    dbc.Button(f"{subpanel.tab_label()}", color="link", id=f"{pfx}_{sub_pfx}_toggle",
                               style={'padding': '.1rem .2rem'}),
                    style={'padding': '.25rem'}),
                dbc.Collapse(dbc.CardBody(subpanel.layout()), id=f"{pfx}_{sub_pfx}_collapse")
            ], style={'overflow': 'visible'})
            accordion_cards.append(card)
            map_view = subpanel.mapping_view_for_subpanel()
            if map_view:
                assoc_form_grps.append(dbc.FormGroup([
                    dbc.Label(f"Related {subpanel.tab_label()}", html_for=f"{pfx}_{sub_pfx}_drop"),
                    dcc.Dropdown(id=f"{pfx}_{sub_pfx}_drop", multi=True, clearable=True, searchable=False),
                    dbc.FormText(f"Use the dropdown to select any related {subpanel.tab_label().lower()}")
                ]))
                related_btn_labels.append(subpanel.tab_label().lower())

        if len(assoc_form_grps) > 0:
            xref_selector = dbc.InputGroup([
                dbc.InputGroupAddon("Related information for:", addon_type="prepend"),
                dbc.Select(id=f"{pfx}_xref_for")
            ])
            form_kids = []
            for i, grp in enumerate(assoc_form_grps):
                form_kids.append(grp)
                if i < (len(assoc_form_grps) - 1):
                    form_kids.append(html.Hr())
            form_kids.append(
                dbc.Alert("", id=f"{self._prefix}_xref_alert", color="success", dismissable=True, duration=10000,
                          fade=True, is_open=False, className="mt-2 mb-1")
            )
            layout.append(
                dbc.Button("Related " + ", ".join(related_btn_labels),
                           id=f"{pfx}_raise_xref_btn", color="primary", className="mr-2 mt-3")
            )
            layout.append(
                dbc.Modal(
                    [
                        dbc.ModalHeader(xref_selector),
                        dbc.ModalBody(dbc.Form(form_kids)),
                        dbc.ModalFooter(dbc.Row([
                            dbc.Button("Update", id=f"{pfx}_upd_xref_btn", color="primary", className='mr-3'),
                            dbc.Button("Done", id=f"{pfx}_lower_xref_btn", color="primary"),
                        ]))
                    ],
                    id=f"{pfx}_upd_xref_modal", backdrop="static", size="xl", centered=True)
            )
            # the number inside this invisible DIV is set to 1 if any changes are made in the mapping view modal window
            layout.append(html.Div(children=0, id=f"{pfx}_mapping_updated_div", style={"display": "none"}))

        if len(accordion_cards) > 0:
            layout.append(html.Div(accordion_cards, className="mt-3 accordion"))

        return layout

    def _data_table(self) -> dt.DataTable:
        """
        Generate a Dash DataTable that displays the entire contents of the primary database table exposed by the
        underlying table view. The table's columns are defined IAW the table view's specification, and each row is a
        separate entity in the table. Selection of any single row is enabled.

        Returns:
           A Dash DataTable rendering a database table's contents.

        """
        cols = self._table_view.columns()
        rows = self._table_view.rows()
        tooltips = self._table_view.tooltip_data_for(rows)
        max_ht = self._table_view.num_text_lines_per_row() * 18
        css_selectors = [] if (max_ht <= 0) else [{
            'selector': '.dash-spreadsheet td div',
            'rule': f'''
                line-height: 18px;
                max-height: {max_ht}px; min-height: {max_ht}px; height: {max_ht}px;
                display: block;
                overflow-y: hidden;
                '''
        }]

        data_table = dt.DataTable(
            id=f"{self._prefix}_table",
            columns=[{"name": col.label, "id": col.id, "presentation": "markdown" if col.is_markdown else "input"}
                     for col in cols],
            data=rows,
            row_selectable='single',
            selected_rows=[],
            style_header={'fontWeight': 'bold'},
            style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
            style_data={'whiteSpace': 'pre-wrap'},
            style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in cols],
            tooltip_data=tooltips, tooltip_duration=None,
            css=css_selectors,
            style_table={'height': '200px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
            # fixed_rows={'headers': True} UNABLE TO DO THIS B/C IT LEADS TO MYSTERIOUS FLICKERING OF
            # BROWSER WINDOW'S VERTICAL SCROLLBAR WHEN MOUSE EXITS OR ENTERS THE DATATABLE
        )
        return data_table

    def _entry_form(self) -> dbc.Form:
        """
        Generate the Dash Bootstrap form used to gather information from the user to add a new entity (aka, row) to
        the underlying database table. Each attribute defining a table entity is represented by a form group
        consisting of a label and an input widget appropriate to the attribute's data type:
            1) 'enum': A Bootstrap Select widget populated with the fixed set of options for that
            attribute and with the first option selected initially.
            2) 'fkey' (foreign key): Similar to 'enum', except that the table view is queried for the available choices
            for that foreign key, and no value is selected initially.
            3) 'text' (length > 100): A Bootstrap Textarea widget with 2 or 4 rows (depending on max text length).
            4) Otherwise: A Bootstrap Input widget of type 'number', 'email', or 'text'.

        The form includes a Bootstrap Alert component in which an error message can be displayed in the event that an
        error occurs while trying to add an entry to the underlying database table.

        Returns:
            A Dash Bootstrap Form component, as described.
        """
        form_groups = []
        for attr in self._table_view.attributes():
            entry_widget = None
            if attr.type == 'enum':
                entry_widget = dbc.Select(
                    id=f"{attr.id}_input",
                    options=[{"label": opt, "value": opt} for opt in attr.options],
                    value=attr.options[0]
                )
            elif attr.type == 'fkey':
                entry_widget = dbc.Select(
                    id=f"{attr.id}_input",
                    options=[{"label": opt[0], "value": opt[1]} for opt in self._table_view.foreign_key_choices(attr)],
                    value=""
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
                input_type = 'number' if (attr.type == 'float') else ('email' if 'email' in attr.id else 'text')
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

        # alert raised when an add operation fails - displays a brief error message. Otherwise hidden.
        form_groups.append(dbc.FormGroup(
            dbc.Alert("", id=f"{self._prefix}_entry_alert", dismissable=True, duration=10000, fade=True, is_open=False)
        ))

        return dbc.Form(form_groups)

    def _callbacks(self, app: dash.Dash):
        """
        Define the Dash callbacks that implement the user interactive functionality of the panel.

        Args:
            app (dash.Dash): The Dash application object. This is used to apply the Dash callback decorator to each
            callback function
        """

        pfx = self._prefix
        num_xref_dropdowns = 0

        input_vector = [Input(f"del_{pfx}_btn", "n_clicks"), Input(f"{pfx}_entry_done_btn", "n_clicks")]
        state_vector = [State(f"{pfx}_table", "selected_rows"), State(f"{pfx}_table", "data"),
                        State(f"{pfx}_entry_added_div", "children")]

        for subpanel in self.__subpanels:
            if subpanel.mapping_view_for_subpanel():
                input_vector.append(Input(f"{subpanel.id_prefix()}_table", "data"))
                num_xref_dropdowns += 1
        if num_xref_dropdowns > 0:
            input_vector.append(Input(f"{pfx}_lower_xref_btn", "n_clicks"))
            state_vector.append(State(f"{pfx}_mapping_updated_div", "children"))

        @app.callback([Output(f"{pfx}_table", "data"), Output(f"{pfx}_table", "tooltip_data"),
                       Output(f"{pfx}_table", "selected_rows"),
                       Output(f"{pfx}_alert", "children"), Output(f"{pfx}_alert", "is_open")],
                      input_vector, state_vector)
        def callback_update_data_table(*args):
            """
            Update the row and tooltip data for the panel's Dash DataTable component, as well as the error message
            string and open/hidden state of the Bootstrap Alert component that appears immediately below the data table
            on the panel.

            This callback is triggered by clicking the 'Remove' button on the main panel, the 'Done' button that hides
            the modal form by which the user adds entries to the underlying database table, or the 'Done' button that
            hides another modal by which the user updates one or more mapping views (present only if panel contains a
            subpanel with a mapping table view). Changes in the tables rendered in any subpanel will also trigger the
            callback, since changes in those subpanels may alter the current contents of the main panel's data table.

            In addition to the input triggers, the current state of several component properties are also supplied:
                1) The row data of the main table and the index of the currently selected row -- in order to perform
                the "Remove" operation.
                2) The current value of the "children" property of two invisible DIVs in the layout, "entry_added_div"
                and "mapping_updated_div". Each property is reset to 0 when the corresponding modal window is raised,
                and set to 1 if any changes are successfully completed while the modal window is up. This method checks
                the property when the modal window is extinguished to determine if any changes were made, in which case
                it is necessary to refresh the contents of the panel's data table.
            """
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate

            update = False
            clear_selection = False
            error_msg = ""
            btn_id = ctx.triggered[0]['prop_id'].split('.')[0]
            neg_ofs = -3 if num_xref_dropdowns == 0 else -4
            selected_rows = args[neg_ofs]
            rows = args[neg_ofs + 1]
            entry_added = (args[neg_ofs + 2] != 0)
            mapping_updated = False if num_xref_dropdowns == 0 else (args[-1] != 0)

            if (num_xref_dropdowns > 0) and (btn_id.find('_table') > -1):
                update = True
            elif btn_id.find("entry_done_btn") > -1:
                update = entry_added
            elif btn_id.find("lower_xref_btn") > -1:
                update = mapping_updated
            else:
                idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
                selected_row = rows[idx] if (-1 < idx < len(rows)) else None
                if selected_row:
                    error_msg = self._table_view.remove_row(selected_row)
                    update = clear_selection = (len(error_msg) == 0)

            table_data = self._table_view.rows() if update else dash.no_update
            tooltip_data = \
                self._table_view.tooltip_data_for(table_data) if isinstance(table_data, list) else dash.no_update
            return table_data, tooltip_data, [] if clear_selection else dash.no_update, error_msg, len(error_msg) > 0

        @app.callback(Output(f"del_{pfx}_btn", "disabled"), [Input(f"{pfx}_table", "selected_rows")])
        def callback_on_table_row_select(selected_rows):
            """
            Update the enabled/disabled state of the "Remove" button in the panel layout.

            Args:
                selected_rows: A list of indices identifying selected rows in the panel's Dash DataTable. Since only
                    single-selection is permitted, it will contain at most one index. It may be None.

            Returns:
                bool: True to disable the "Remove" button; False to enable.
            """
            idx = selected_rows[0] if (selected_rows and len(selected_rows) > 0) else -1
            return idx < 0

        state_vector = [State(f"{attr.id}_input", "value") for attr in self._table_view.attributes()]
        output_vector = [Output(f"{pfx}_entry_form", "is_open"), Output(f"{pfx}_entry_added_div", "children"),
                         Output(f"{pfx}_entry_alert", "children"), Output(f"{pfx}_entry_alert", "is_open")]
        output_vector.extend([Output(f"{attr.id}_input", "value") for attr in self._table_view.attributes()])

        @app.callback(output_vector,
                      [Input(f"add_{pfx}_btn", "n_clicks"), Input(f"{pfx}_entry_submit_btn", "n_clicks"),
                       Input(f"{pfx}_entry_done_btn", "n_clicks")],
                      state_vector)
        def callback_on_add_entry(add_btn, submit_btn, done_btn, *args):
            """
            Raise/lower the add-entry modal form by which user adds new entries to the database table represented in
            this panel, or add an entry when the user clicks the submit button in the modal body.

            Relevant triggers and actions taken:
            1) "Add" button on main panel: Entry widgets are cleared. The "children" property of an invisible DIV in
            the layout -- {pfx}_need_refresh_div -- is set to the number 0, indicating that no changes have yet been
            made to the underlying database table. The modal window is raised.
            2) "Add" button on the modal window: An attempt is made to add an entry to the database table based on the
            values collected from the form widgets. If successful, the widgets are cleared to indicate that the entry
            succeeded, and the "children" property of the invisible "entry_added_div" is set to 1. If an error occurred,
            the Alert component is raised to display the error message, and the widgets are left unchanged.
            3) "Done" button on the modal window: The modal window is extinguished. Clicking this button will also
            trigger callback_update_data_table(). That callback will check the "children" property of "entry_added_div"
            and, if it is not zero, refresh the contents of the panel's data table to reflect the changes made.
            """
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate

            attrs = self._table_view.attributes()
            out = [True, 0, "", False]
            for attr in attrs:
                out.append(attr.options[0] if attr.type == 'enum' else "")

            btn_id = ctx.triggered[0]['prop_id'].split('.')[0]
            if btn_id.find(f"{pfx}_entry_done_btn") > -1:
                out[0] = False
                out[1] = dash.no_update
            elif btn_id.find(f"{pfx}_entry_submit_btn") > -1:
                entry = dict()
                for i, attr in enumerate(attrs):
                    entry[attr.id] = str(args[i])
                error_msg = self._table_view.add_row(entry)

                # on successful add, put 1 (true) in the invisible DIV so that data table will be refreshed when modal
                # is extinguished; else show error msg and ensure that widget values are not cleared
                if len(error_msg) == 0:
                    out[1] = 1
                else:
                    out = [dash.no_update] * (4 + len(attrs))
                    out[2] = error_msg
                    out[3] = True

            return tuple(out)

        if len(self.__subpanels) > 0:
            output_vector = []
            input_vector = []
            state_vector = []
            for subpanel in self.__subpanels:
                sub_pfx = subpanel.id_prefix()
                output_vector.append(Output(f"{pfx}_{sub_pfx}_collapse", "is_open"))
                input_vector.append(Input(f"{pfx}_{sub_pfx}_toggle", "n_clicks"))
                state_vector.append(State(f"{pfx}_{sub_pfx}_collapse", "is_open"))

            @app.callback(output_vector, input_vector, state_vector)
            def toggle_accordion(*args):
                """
                Optional callback -- present only if the panel includes one or more subpanels within an accordion-style
                layout -- updates the open state of each of the subpanels within the accordion widget.
                """
                ctx = dash.callback_context

                n_subpanels = len(self.__subpanels)
                out = n_subpanels * [False]
                if ctx.triggered:
                    btn_id = ctx.triggered[0]["prop_id"].split(".")[0]
                    for i, subpanel in enumerate(self.__subpanels):
                        if btn_id == f"{pfx}_{subpanel.id_prefix()}_toggle" and args[i]:
                            out[i] = not args[n_subpanels + i]
                            break
                return tuple(out)

        if num_xref_dropdowns > 0:
            input_vector = [Input(f"{pfx}_raise_xref_btn", "n_clicks"), Input(f"{pfx}_lower_xref_btn", "n_clicks"),
                            Input(f"{pfx}_upd_xref_btn", "n_clicks")]
            output_vector1 = [Output(f"{pfx}_upd_xref_modal", "is_open"), Output(f"{pfx}_xref_for", "options"),
                              Output(f"{pfx}_xref_for", "value"), Output(f"{pfx}_xref_alert", "children"),
                              Output(f"{pfx}_xref_alert", "color"), Output(f"{pfx}_xref_alert", "is_open"),
                              Output(f"{pfx}_mapping_updated_div", "children")]
            output_vector2 = []
            state_vector = [State(f"{pfx}_table", "selected_rows"), State(f"{pfx}_table", "data")]
            for subpanel in self.__subpanels:
                sub_pfx = subpanel.id_prefix()
                map_view = subpanel.mapping_view_for_subpanel()
                if map_view:
                    output_vector1.append(Output(f"{pfx}_{sub_pfx}_drop", "options"))
                    output_vector2.append(Output(f"{pfx}_{sub_pfx}_drop", "value"))
                    state_vector.append(State(f"{pfx}_{sub_pfx}_drop", "value"))
            state_vector.append(State(f"{pfx}_xref_for", "value"))

            @app.callback(output_vector1, input_vector, state_vector)
            def callback_on_raise_xref_modal(*args):
                """
                Optional callback -- present only if the panel includes one or more subpanels having a table that is
                related to the main panel table through an associative view. Raises/lowers the modal window by which
                user updates the mapping view(s) for any row in the main database table, and also handles an update
                triggered by pressing the "Update" button in the modal's footer.

                Triggers and actions:
                1) "Related ..." button on the main panel: This raises the modal window "upd_xref_modal" by which user
                updates the mapping(s) for any row in the main data table. The "children" property of the invisible DIV
                "mapping_updated_div" is reset to 0 to indicate that no changes have been made via this modal window so
                far. The single-select dropdown menu "xref_for" is populated with the primary key values of every row in
                the main table, and the first row or the currently selected row in the main table is chosen as the
                dropdown initial value V. The multi-select dropdown(s) that define the mappings for V are populated with
                the primary key values for all rows in the associative table(s). Setting the value V in "xref_for" will,
                in turn, trigger a call to callback_on_select_xref_key(), which will update the current value for each
                multi-select dropdown to display the entities in the corresponding subpanel table that map to V.
                2) "Update" button in the modal footer: The user presses this button to confirm any changes made in the
                multi-select dropdowns. If the relevant mapping views are successfully updated in the database, the
                Alert component is shown to indicate this fact. If the update fails, the Alert displays the error
                message and is styled to indicate the failure. On success, the "children" property of "mapping_updated"
                DIV is set to 1; otherwise it is left unchanged.
                3) "Done" button in the modal footer: This extinguishes the modal window without changing most of the
                other output properties -- particularly, the "children" property of the "mapping_updated" DIV. Clicking
                this button will also trigger callback_update_data_table(), which checks the "mapping_updated" DIV to
                see if any changes occurred while the modal window was raised.
                """
                ctx = dash.callback_context
                if not ctx.triggered:
                    raise dash.exceptions.PreventUpdate

                # output vector is initialized for the correct response to extinguishing the modal window
                out = [False, dash.no_update, "", "", "success", False, dash.no_update]
                out.extend([dash.no_update for _ in range(num_xref_dropdowns)])

                btn_id = ctx.triggered[0]['prop_id'].split('.')[0]
                selected_rows = args[3]
                rows = args[4]
                idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
                selected_row = rows[idx] if (-1 < idx < len(rows)) else None
                if (len(rows) > 0) and (btn_id.find('raise_xref_btn') > -1):
                    src_pk = self._table_view.auto_primary_key_id()
                    out[0] = True
                    row_aliases = sorted([self._table_view.to_row_alias(row) for row in rows], key=lambda x: x.label)
                    out[1] = [{'label': r.label, 'value': r.pk[src_pk], 'title': r.tip} for r in row_aliases]
                    out[2] = selected_row[src_pk] if selected_row else rows[0][src_pk]
                    out[6] = 0
                    ofs = 7
                    for subpanel in self.__subpanels:
                        map_view = subpanel.mapping_view_for_subpanel()
                        if map_view:
                            all_options = map_view.mappings_for(None)
                            if all_options:
                                dst_pk = map_view.to_key()
                                out[ofs] = [{'label': opt.label, 'value': opt.pk[dst_pk], 'title': opt.tip}
                                            for opt in all_options]
                            else:
                                out[ofs] = list()
                            ofs += 1
                elif btn_id.find('upd_xref_btn') > -1:
                    src_pk_val = args[-1]
                    ofs = 5  # points to State(first_drop, "value")
                    error_msg = ""
                    for subpanel in self.__subpanels:
                        map_view = subpanel.mapping_view_for_subpanel()
                        if map_view:
                            assoc_set = set(args[ofs])
                            ofs += 1
                            error_msg = map_view.update_mappings_for(src_pk_val, assoc_set)
                            if len(error_msg) > 0:
                                break
                    ok = (len(error_msg) == 0)

                    out[0] = out[1] = out[2] = dash.no_update
                    out[3] = "Updated successfully." if ok else error_msg
                    out[4] = "success" if ok else "danger"
                    out[5] = True
                    out[6] = 1 if ok else dash.no_update

                return tuple(out)

            @app.callback(output_vector2, [Input(f"{pfx}_xref_for", "value")])
            def callback_on_select_xref_key(src_pk_val):
                """
                Optional callback -- present only if the panel includes one or more subpanels having a table that is
                related to the main panel table through an associative view. Whenever the main table entity selected
                in the "xref_for" dropdown in the modal window changes, the current value(s) in the multi-select
                dropdown(s) in that same window are updated to reflect the current mapping table view(s).
                """
                ctx = dash.callback_context
                if not ctx.triggered:
                    raise dash.exceptions.PreventUpdate
                out = []
                for subpanel in self.__subpanels:
                    map_view = subpanel.mapping_view_for_subpanel()
                    if map_view:
                        curr_mapped_list = map_view.mappings_for(src_pk_val) if src_pk_val else None
                        dst_pk = map_view.to_key()
                        out.append([alias.pk[dst_pk] for alias in curr_mapped_list] if curr_mapped_list else list())
                return tuple(out)


class UserPanel(_BasePanel):
    """This panel provides interactive access to the manual table listing users in the Lisberger lab database."""

    def __init__(self, app: dash.Dash):
        super().__init__(app, tv.UserView(), 'usr')


class RigPanel(_BasePanel):
    """
    This panel provides interactive access to experiment rigs in the lab database
    """

    def __init__(self, app: dash.Dash):
        super().__init__(app, tv.RigView(), 'rig')


class SubjectPanel(_BasePanel):
    """
    This panel provides interactive access to experiment subjects in the lab database through the table view class
    tv.SubjectView. The implant history for any selected subject is displayed in a collapsible subpanel below the
    subject table. That subpanel automatically expands whenever a subject is selected and collapses when the
    selection is cleared (for example, when a subject is deleted).
    """

    def __init__(self, app: dash.Dash):
        view = tv.SubjectView()
        self._implant_history = SubjectPanel.ImplantHistorySubPanel(app, view.implant_history_for("???"))
        super().__init__(app, view, 'subj')

    def layout(self) -> List[Any]:
        base_layout = super().layout()
        base_layout.extend([
            dbc.Collapse(
                dbc.Card(
                    dbc.CardBody([], id="implhist_panel")
                ),
                id=f"implhist_collapse", className="mt-3")
        ])
        return base_layout

    def _callbacks(self, app: dash.Dash):
        super()._callbacks(app)
        pfx = self._prefix

        @app.callback(
            [Output("implhist_panel", "children"), Output("implhist_collapse", "is_open")],
            [Input(f"{pfx}_table", "selected_rows")], [State(f"{pfx}_table", "data")])
        def show_hide_implhist(selected_rows, rows):
            ctx = dash.callback_context
            if not ctx.triggered:
                raise dash.exceptions.PreventUpdate

            idx = selected_rows[0] if (selected_rows is not None) and (len(selected_rows) > 0) else -1
            selected_row = rows[idx] if (-1 < idx < len(rows)) else None
            is_open = (selected_row is not None)
            subj_id = selected_row['subj_id'] if is_open else None
            self._implant_history._table_view = tv.SubjectView.implant_history_for(subj_id)
            return self._implant_history.layout(), is_open

    class ImplantHistorySubPanel(_BasePanel):
        def __init__(self, app: dash.Dash, view: tv.BaseTableView):
            super().__init__(app, view, 'impl')

        def layout(self) -> List[Any]:
            """
            Overridden to add a header line above the table, since this appears as a sub-panel of the main
            "Subjects" panel.
            """
            layout_cmpts = super().layout()
            layout_cmpts.insert(0, html.H5(f"{self.tab_label()}"))
            return layout_cmpts


class BrainRegionPanel(_BasePanel):
    """
    This panel provides interactive access to the various brain regions characterized in the lab database, along with
    the neuron types associated with those brain regions (a many-to-many relationship).
    """

    def __init__(self, app: dash.Dash):
        subpanels = [BrainRegionPanel.NeuronTypePanel(app)]
        super().__init__(app, tv.BrainRegionView(), 'brain', subpanels)

    class NeuronTypePanel(_BasePanel):
        def __init__(self, app: dash.Dash):
            super().__init__(app, tv.NeuronTypeView(), 'ntyp')

        def mapping_view_for_subpanel(self):
            return tv.BrainRegionToNeuronTypeView()


class StudyPanel(_BasePanel):
    """
    This panel provides interactive access to research projects in the lab database, along with the publications and
    research keywords associated with those projects.
    """

    def __init__(self, app: dash.Dash):
        subpanels = [StudyPanel.PublicationPanel(app), StudyPanel.KeywordPanel(app)]
        super().__init__(app, tv.StudyView(), 'study', subpanels)

    class PublicationPanel(_BasePanel):
        def __init__(self, app: dash.Dash):
            super().__init__(app, tv.PublicationView(), 'pub')

        def mapping_view_for_subpanel(self):
            return tv.StudyToPublicationView()

    class KeywordPanel(_BasePanel):
        def __init__(self, app: dash.Dash):
            super().__init__(app, tv.KeywordView(), 'key')

        def mapping_view_for_subpanel(self):
            return tv.StudyToKeywordView()
