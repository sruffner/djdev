"""
curate_panels: The HTML/Dash panels for the "curate" page in web-based interface to the Lisberger laboratory database.

The "curate" page (see curate.py) lets the user view and manage the content of most "manual" tables in the laboratory
database. These are small tables that organize information about lab members, experiment subjects, research projects,
brain regions, etc.

The "curate" page provides a tab panel for accessing five distinct manual table views: lab members, experiment rigs,
experiment subjects, brain regions, and research projects. The information in these tables is used to further describe
and categorize the actual experimental data recorded in the laboratory.

The panels defined here actually implement an HTML/Dash-based web interface to the various table views. Almost all of
the implementation is in the base class _BasePanel. Subclasses of _BasePanel define the actual panels embedded in
the "curate" page.

Created on Wed Aug  5 18:06:14 2020

@author: sruffner
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

from dataclasses import dataclass

import dash
import dash_html_components as html
import dash_bootstrap_components as dbc
import dash_core_components as dcc
import dash_table as dt
from dash.dependencies import Input, Output, State
from typing import List, Any, Optional, Dict, Set

import database.table_info as ti
from database.manager import DataBaseManager


@dataclass(frozen=True)
class _RowAlias:
    """
    An alternative representation of a row in a database table that is intended for use when selecting entities from
    a list- or dropdown-style widget in the user interface. It has 3 fields:
        'pk' -  The primary key for the row (PK attribute-value pairs only).

        'label' - The row's user-facing label. Ideally it should be no more than 50 characters long and it should
        uniquely identify the row in a way that is meaningful to the end user.

        'tip' - A longer description of the entity, which would appear in a tooltip for the list- or dropdown-style
        widget. Can be None, in which case no tip is shown.
    """
    pk: Dict[str, ti.AttributeValue]
    label: str
    tip: Optional[str] = None


# noinspection PyUnusedLocal,PyShadowingNames
class _BasePanel:
    """
    Base class for all panels laid out on the "curate" page.

    _BasePanel defines common and optional functionality for all of the curate panels. Concrete subclasses tailor its
    behavior for a particular panel by overriding select methods.

    The base implementation displays the contents of the underlying table in a Dash DataTable with single-row
    selection. An "Add" button raises a modal form by which the user can add one or more entries to the table. A
    "Remove" button -- enabled only when an entry is selected in the DataTable -- triggers removal of the selected
    entity.

    In addition, _BasePanel supports the display of one or more subpanels, each of which houses another database table
    related to the parent panel's table via a "cross-reference" table. These subpanels, which derive from _MappingPanel,
    are managed by an "accordion"-style widget below the DataTable rendering of the parent table. In this scheme, the
    parent table is the "source table" for  the mapping relationship, and each table in a subpanel is the "destination
    table". When such subpanels are included, _BasePanel includes a "Related information" button that raises a modal
    form by which the user can select which entities in a "destination" table are related to a selected entity from the
    source table.

    Any caught errors are displayed in a Dash Bootstrap "Alert" element either below the DataTable in the panel or on
    one of the modal entry forms.

    The implementation of _BasePanel is rather complex in order to accommodate the various manual tables in the
    Lisberger lab database and their relationships. On the other hand, that serves to simplify the definition of the
    concrete subclasses that implement each of the panels that appears on the "curate" page.
    """

    def __init__(self, app: dash.Dash, table_id: ti.DBTable, prefix: str, subpanels: List[_MappingSubPanel] = None):
        """
        Construct a user-facing panel associated with a manually curated table in the laboratory database. The panel
        displays the table contents in a Dash DataTable widget and provides mechanisms for adding new entities to
        the table or removing a selected entity.

        When the table is related to another database table through a separate cross-reference or "mapping" table,
        the second table can be displayed in a subpanel that, in turn, is embedded in an accordion-style widget within
        this panel. In this scenario, the table in this panel is the "source", while the table housed in the subpanel
        is the "destination" in the mapping. In addition to managing the destination table itself, the subpanel
        provides methods to access and modify the rows of cross-reference table. The parent _BasePanel() provides the
        infrastructure for selecting what entity(ies) in the destination table are related to a given entity in the
        source table. See _MappingSubPanel for details.

        Args:
            app: The Dash application object.
            table_id: ID of a database table conducive to form-based entry and a tabular presentation on the GUI.
            prefix: A short string used to define unique IDs for HTML/Dash components rendered in and managed by
                this panel.
            subpanels: Subpanels that should be encapsulated in this panel. The base implementation uses an accordion
                widget to display any subpanels. Each subpanel houses a database table that is associated with table_id
                through a separate "cross-reference" or "mapping" table in the database.
        """
        self._app = app
        """ The Dash application object. """
        self._table_id = table_id
        """ ID of database table that is displayed and modified on this panel. """
        self._prefix = prefix
        """ The panel prefix, used to define unique IDs for HTML/Dash components rendered on the panel. """
        self._subpanels = list(subpanels) if subpanels else []
        """ List of any subordinate panels presenting database tables related to the main table for this panel. """
        if hasattr(self, '_callbacks'):
            self._callbacks(self._app)

    def id_prefix(self) -> str:
        """ Return a short string used to uniquely identify the tab or other component associated with this panel. It
        is also used as a prefix to uniquely label the various HTML/Dash components within the panel layout."""
        return self._prefix

    def tab_label(self) -> str:
        """ Return a short (less than 20 chars) user-facing label for this panel."""
        return ti.table_label(self._table_id)

    def _attributes_exposed(self) -> List[str]:
        """
        Get the IDs of those table attributes that are exposed on this panel's modal entry form (used to add a new row
        to the table) and in the panel's Dash DataTable rendering of the table's contents.

        The base class implementation includes all table attributes EXCEPT an auto-incrementing primary key (the value
        of which is set by the database itself, not the user) and any blob-valued attributes (for which manual entry and
        tabular presentation make no sense!).

        Returns:
            List containing IDs of the table attributes exposed in this panel, as described.
        """
        return [attr_id for attr_id in ti.attributes_of(self._table_id, False)
                if ti.attribute_info(self._table_id, attr_id).type not in [ti.AttrTypeEnum.AUTO, ti.AttrTypeEnum.BLOB]]

    def layout(self) -> List[Any]:
        """
        Get the layout for this panel.

        This base implementation renders a Dash DataTable displaying the contents of the underlying database table,
        Add/Remove buttons, and a modal entry form for adding a new table entity. In addition, any subpanels specified
        in the constructor are embedded in an accordion widget. Furthermore, if any subpanel table is related to the
        main panel table through a cross-reference table, then the base implementation includes an additional modal form
        to modify the mapping(s), as well as an additional button to raise that form. The Dash callbacks encapsulated by
         _callbacks() specifically handle this layout.

        Returns:
            List[Any]: List of HTML/Dash/Dash Bootstrap components comprising this panel.
        """
        pfx = self._prefix
        layout = [
            html.Div(id=f"{pfx}_table_div", children=[self._data_table()]),
            dbc.Alert("", id=f"{self._prefix}_alert", color="danger", dismissable=True, is_open=False,
                      className="mt-3 mb-1"),
            dbc.Button("Add", id=f"add_{pfx}_btn", color="primary", className="mr-2 mt-3"),
            dbc.Button("Remove", id=f"del_{pfx}_btn", color="primary", className="mr-2 mt-3", disabled=True),
            dbc.Modal(
                [
                    dbc.ModalHeader(f"Add {ti.table_row_label(self._table_id)}"),
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
        for subpanel in self._subpanels:
            sub_pfx = subpanel.id_prefix()
            card = dbc.Card([
                dbc.CardHeader(
                    dbc.Button(f"{subpanel.tab_label()}", color="link", id=f"{pfx}_{sub_pfx}_toggle",
                               style={'padding': '.1rem .2rem'}),
                    style={'padding': '.25rem'}),
                dbc.Collapse(dbc.CardBody(subpanel.layout()), id=f"{pfx}_{sub_pfx}_collapse")
            ], style={'overflow': 'visible'})
            accordion_cards.append(card)
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
                dbc.Alert("", id=f"{self._prefix}_xref_alert", color="success", dismissable=True, is_open=False,
                          className="mt-2 mb-1")
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

    def _entry_form(self) -> dbc.Form:
        """
        Helper method for layout() generates the modal entry form by which the user can add an entry (aka, row) to the
        database table exposed in this panel.

        The base implementation simply returns a form which exposes all table attributes except for an auto-incrementing
        primary key (which is not set by the user) and any boolean-valued or blob-valued attributes (not supported). It
        also includes an Alert component in which an error message can be displayed when the user enters an invalid\
        value in the form. For a full description, see DataBaseManger.entry_form().

        Returns:
            A Dash Bootstrap Form component.
        """
        return DataBaseManager().entry_form(self._table_id, self._attributes_exposed(), None,
                                            f"{self._prefix}_entry_alert")

    def _data_table(self) -> dt.DataTable:
        """
        Generate a Dash DataTable that displays the entire contents of the primary database table exposed in this panel.
        Each row is a separate entity in the table. Selection of any single row is enabled.

        Returns:
           A Dash DataTable rendering a database table's contents.

        """
        cols = self._columns()
        rows = self._rows()
        tooltips = self._tooltip_data_for(rows)
        max_ht = self._num_text_lines_per_row() * 18
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
            cell_selectable=False,
            selected_rows=[],
            style_header={'fontWeight': 'bold'},
            style_cell={'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'lineHeight': '18px'},
            style_data={'whiteSpace': 'pre-wrap'},
            style_data_conditional=[
                {
                    "if": {"state": "active"},  # 'active' | 'selected'
                    "backgroundColor": "rgba(135, 206, 250, 0.4)",
                    "border": "1px solid blue",
                },
            ],
            style_cell_conditional=[{'if': {'column_id': col.id}, 'width': col.width} for col in cols],
            tooltip_data=tooltips, tooltip_duration=None,
            css=css_selectors,
            style_table={'height': '200px', 'overflowY': 'scroll', 'border': '1px solid lightgray'},
            # fixed_rows={'headers': True} UNABLE TO DO THIS B/C IT LEADS TO MYSTERIOUS FLICKERING OF
            # BROWSER WINDOW'S VERTICAL SCROLLBAR WHEN MOUSE EXITS OR ENTERS THE DATATABLE
        )
        return data_table

    def _columns(self) -> List[ti.Column]:
        """
        Helper method for _data_table() prepares the list of columns included in a Dash DataTable rendering of the
        database table displayed in this panel.

        The base class implementation returns a column for each table attribute, excluding an auto-incrementing primary
        key attribute and any blob-valued attribute. A subclass must override this method to construct a different
        user-facing view of the table's content. Possible use cases include: hiding one or more non-primary key
        attributes; changing the column order; adding a column that displays non-attribute content, tagging an attribute
        that is formatted as HTML markdown and should be displayed as such.

        IMPORTANT: If a subclass overrides this method to alter the user-facing table columns, then it must also
        override _rows() to transform the rows fetched from the database table to match the columns supplied here.

        Returns:
            The list of displayed table columns.
        """
        out: List[ti.Column] = list()
        for attr_id in ti.attributes_of(self._table_id, False):
            attr_info = ti.attribute_info(self._table_id, attr_id)
            if attr_info.type not in [ti.AttrTypeEnum.AUTO, ti.AttrTypeEnum.BLOB]:
                out.append(ti.Column(attr_id, attr_info.label, attr_info.col_width))
        return out

    def _rows(self) -> List[Dict[str, ti.AttributeValue]]:
        """
        Helper method for _data_table() fetches all rows from the database table displayed in this panel and transforms
        them, if necessary, for rendering in a Dash DataTable.

        The base implementation simply returns all rows in the table as fetched directly from the database; the rows are
        not transformed in any way. Each row returned includes the value for every table attribute, including an
        auto-incrementing primary key. This is important so that a user-facing client can implement operations like
        "remove" that require specifying a particular row in the table.

        If a subclass panel implementation wishes to alter the tabular presentation or transform the fetched rows in any
        way, then it must override this method and _cols(). Regardless, each row must include the full primary key for
        the database table, even if some attributes are hidden in the Dash DataTable rendering.

        Returns:
            The table contents in dictionary form. Each element of the list is a dictionary representing one table row,
                eg: {'attr1': value1, 'attr2': value2, ... }.

        Raises:
            ValueError: If any attribute ID in *condition* is not a recognized attribute of this table.
        """
        return DataBaseManager().fetch_rows(self._table_id)

    def _num_text_lines_per_row(self) -> int:
        """
        Helper method for _data_table() returns the number of text lines per row in a Dash DataTable rendering of the
        database table displayed in this panel.

        The base class implementation returns 0 -- meaning each row may have any number of text lines. A subclass can
        override, returning a positive integer N, indicating that all rows should display N text lines.

        Returns:
            Number of text lines per row; 0 = no restriction.
        """
        return 0

    def _tooltip_data_for(self, data: List[Dict[str, ti.AttributeValue]]) -> List[dict]:
        """
        Helper method for _data_table() generates the tooltip contents for the specified rows in a Dash DataTable
        rendering of the database table displayed in this panel.

        The base class implementation returns an empty list, so no tooltips are displayed.

        Args:
            data (List[Dict[str, Any]]): The current table data, as would be returned by _rows().
        Returns:
            The tooltip data -- compatible with the Dash DataTable's 'tooltip_data' property. If no tooltips are needed,
                return an empty list. Otherwise, the list length must match the number of rows in the supplied table
                data (so that any tooltip will match the corresponding table cell).
        """
        return []

    def _to_row_alias(self, row: Dict[str, ti.AttributeValue]) -> _RowAlias:
        """
        Generate a representation of a table row that is geared toward presentation in the user interface within a
        list- or dropdown-style widget.

        This base class implementation merely joins the values of the primary key attributes (comma-separated) to form
        the label, and truncates the result to 50 characters. The associated tooltip string (RowAlias.tip) is set to
        None. Subclasses containing more than one attribute in their primary key, or a primary key that is not
        meaningful to the user (such as an auto-incrementing integer), should override this method.

        Args:
            row: A complete entity in this panel's database table, in dictionary format.
        Returns:
            RowAlias: A compact representation of that entity for UI purposes, as described.
        Raises:
            ValueError: If row is missing any table attributes.
        """
        if not isinstance(row, dict):
            raise ValueError("Row must be a dict")
        for attr_id in ti.attributes_of(self._table_id, False):
            if attr_id not in row:
                raise ValueError(f"Missing table attribute: '{attr_id}'")
        pk_dict = {k: row[k] for k in ti.primary_key_of(self._table_id, False)}
        label = ','.join([str(v) for _, v in pk_dict.items()])
        return _RowAlias(pk_dict, label if len(label) < 53 else (label[:50] + '...'))

    def _remove_row(self, row_pk: Dict[str, ti.AttributeValue]) -> str:
        """
        Delete the specified entry (aka, row) from the database table displayed in this panel.

        Args:
            row_pk: Must contain, at a minimum, the primary key attribute-value pairs that uniquely identify a single
                row in the table row. Any other attributes are ignored!

        Returns:
            str: A description of the error if operation fails on database. An empty string if operation succeeds.

        Raises:
            ValueError: If row_pk is missing any of the table's primary key attributes.
            NotImplementedError: If this is a part table. Deletions from a part table are handled automatically when
                an entity in its master table is removed
        """
        if self._table_id.is_part_table():
            raise NotImplementedError("Cannot delete a row from a part table. Operate on master table instead. ")
        table_pk = ti.primary_key_of(self._table_id)
        for key in table_pk:
            if not (key in row_pk):
                raise ValueError(f"Delete failed: Missing primary key '{key}'")
        restriction = {key: row_pk[key] for key in table_pk}
        error_msg = DataBaseManager().delete_from_table(self._table_id, restriction)
        return error_msg if error_msg else ""

    def _add_row(self, row: Dict[str, ti.AttributeValue]) -> str:
        """
        Insert a proposed entry (aka, row) into the database table displayed in this panel.

        The base implementation will first validate the proposed entry before trying to insert it into the database
        table. It the table uses an auto-incrementing single-attribute primary key, any value supplied for that
        attribute will be ignored, since its value is automatically supplied by the database when the new entry is
        inserted.

        Args:
            row: The new entry. It must contain a valid attribute value for each table attribute -- with the exception
                of an auto-incrementing primary key.

        Returns:
            An empty string if operation succeeds, else a user-facing description of the error (missing attribute,
            invalid attribute value, attempt to add an already existing row, database error).
        """
        try:
            # this call will remove auto-incrementing PK from argument, if present.
            db_mgr = DataBaseManager()
            error_msg = db_mgr.check_row(self._table_id, row)
            if not error_msg:
                error_msg = db_mgr.insert_into_table(self._table_id, row)
            if error_msg:
                raise Exception(error_msg)
        except Exception as err:
            error_msg = f"Add failed: {str(err)}"
        return error_msg if error_msg else ""

    def _callbacks(self, app: dash.Dash):
        """
        Define the Dash callbacks that implement the user interactive functionality of the panel.

        Args:
            app (dash.Dash): The Dash application object. This is used to apply the Dash callback decorator to each
            callback function
        """
        pfx = self._prefix

        # this clientside callback highlights all cells in the selected row
        app.clientside_callback(
            """
            function(rows) {
                let style = [];
                if (Array.isArray(rows) && (rows.length > 0) && Number.isInteger(rows[0])) {
                    style = [{"if": {"row_index": rows[0]}, "background-color": "rgba(176, 196, 222, 0.5)"}];
                }
                return style;
            }
            """,
            Output(f"{pfx}_table", "style_data_conditional"),
            Input(f"{pfx}_table", "selected_rows")
        )

        num_xref_dropdowns = 0

        input_vector = [Input(f"del_{pfx}_btn", "n_clicks"), Input(f"{pfx}_entry_done_btn", "n_clicks")]
        state_vector = [State(f"{pfx}_table", "selected_rows"), State(f"{pfx}_table", "data"),
                        State(f"{pfx}_entry_added_div", "children")]

        for subpanel in self._subpanels:
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
            hides another modal by which the user updates one or more cross-reference (present only if panel's table is
            associated with another table via a separate mapping table). Changes in the tables rendered in any subpanel
            will also trigger the callback, since changes in those subpanels may alter the current contents of the main
            panel's data table.

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
                    error_msg = self._remove_row(selected_row)
                    update = clear_selection = (len(error_msg) == 0)

            table_data = self._rows() if update else dash.no_update
            tooltip_data = self._tooltip_data_for(table_data) if update else dash.no_update
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

        attr_ids = self._attributes_exposed()
        state_vector = [State(f"{attr_id}_input", "value") for attr_id in attr_ids]
        output_vector = [Output(f"{pfx}_entry_form", "is_open"), Output(f"{pfx}_entry_added_div", "children"),
                         Output(f"{pfx}_entry_alert", "children"), Output(f"{pfx}_entry_alert", "is_open")]
        output_vector.extend([Output(f"{attr_id}_input", "value") for attr_id in attr_ids])

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

            attr_ids = self._attributes_exposed()
            out = [True, 0, "", False]
            for attr_id in attr_ids:
                attr_info = ti.attribute_info(self._table_id, attr_id)
                out.append(attr_info.options[0] if attr_info.type == ti.AttrTypeEnum.ENUM else "")

            btn_id = ctx.triggered[0]['prop_id'].split('.')[0]
            if btn_id.find(f"{pfx}_entry_done_btn") > -1:
                out[0] = False
                out[1] = dash.no_update
            elif btn_id.find(f"{pfx}_entry_submit_btn") > -1:
                entry = dict()
                for i, attr_id in enumerate(attr_ids):
                    entry[attr_id] = str(args[i])
                error_msg = self._add_row(entry)

                # on successful add, put 1 (true) in the invisible DIV so that data table will be refreshed when modal
                # is extinguished; else show error msg and ensure that widget values are not cleared
                if len(error_msg) == 0:
                    out[1] = 1
                else:
                    out = [dash.no_update] * (4 + len(attr_ids))
                    out[2] = error_msg
                    out[3] = True

            return tuple(out)

        if len(self._subpanels) > 0:
            output_vector = []
            input_vector = []
            state_vector = []
            for subpanel in self._subpanels:
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

                n_subpanels = len(self._subpanels)
                out = n_subpanels * [False]
                if ctx.triggered:
                    btn_id = ctx.triggered[0]["prop_id"].split(".")[0]
                    for i, subpanel in enumerate(self._subpanels):
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
            for subpanel in self._subpanels:
                sub_pfx = subpanel.id_prefix()
                output_vector1.append(Output(f"{pfx}_{sub_pfx}_drop", "options"))
                output_vector2.append(Output(f"{pfx}_{sub_pfx}_drop", "value"))
                state_vector.append(State(f"{pfx}_{sub_pfx}_drop", "value"))
            state_vector.append(State(f"{pfx}_xref_for", "value"))

            @app.callback(output_vector1, input_vector, state_vector)
            def callback_on_raise_xref_modal(*args):
                """
                Optional callback -- present only if the panel includes one or more subpanels having a table that is
                related to the main panel table through a separate cross-reference table. Raises/lowers the modal window
                by which user updates the cross-reference for any row in the main database table, and also handles an
                update triggered by pressing the "Update" button in the modal's footer.

                Triggers and actions:
                1) "Related ..." button on the main panel: This raises the modal window "upd_xref_modal" by which user
                updates the mapping(s) for any row in the main data table. The "children" property of the invisible DIV
                "mapping_updated_div" is reset to 0 to indicate that no changes have been made via this modal window so
                far. The single-select dropdown menu "xref_for" is populated with the primary key values of every row in
                the main table, and the first row or the currently selected row in the main table is chosen as the
                dropdown initial value V. The multi-select dropdown(s) that define the cross-references for V are
                populated with the primary key values for all rows in the cross-reference table(s). Setting the value V
                in "xref_for" will, in turn, trigger a call to callback_on_select_xref_key(), which will update the
                current value for each multi-select dropdown to display the entities in the corresponding subpanel table
                that map to V.
                2) "Update" button in the modal footer: The user presses this button to confirm any changes made in the
                multi-select dropdowns. If the relevant mapping tables are successfully updated in the database, the
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
                    src_pk = ti.auto_primary_key_for(self._table_id)
                    out[0] = True
                    row_aliases = sorted([self._to_row_alias(row) for row in rows], key=lambda x: x.label)
                    out[1] = [{'label': r.label, 'value': r.pk[src_pk], 'title': r.tip} for r in row_aliases]
                    out[2] = selected_row[src_pk] if selected_row else rows[0][src_pk]
                    out[6] = 0
                    ofs = 7
                    for subpanel in self._subpanels:
                        all_options = subpanel.mappings_for(None)
                        if all_options:
                            dst_pk = subpanel.to_key()
                            out[ofs] = [{'label': opt.label, 'value': opt.pk[dst_pk], 'title': opt.tip}
                                        for opt in all_options]
                        else:
                            out[ofs] = list()
                        ofs += 1
                elif btn_id.find('upd_xref_btn') > -1:
                    src_pk_val = args[-1]
                    ofs = 5  # points to State(first_drop, "value")
                    error_msg = ""
                    for subpanel in self._subpanels:
                        assoc_set = set(args[ofs])
                        ofs += 1
                        error_msg = subpanel.update_mappings_for(src_pk_val, assoc_set)
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
                related to the main panel table through a cross-reference table. Whenever the main table entity selected
                in the "xref_for" dropdown in the modal window changes, the current value(s) in the multi-select
                dropdown(s) in that same window are updated to reflect the current mapping table view(s).
                """
                ctx = dash.callback_context
                if not ctx.triggered:
                    raise dash.exceptions.PreventUpdate
                out = []
                for subpanel in self._subpanels:
                    curr_mapped_list = subpanel.mappings_for(src_pk_val) if src_pk_val else None
                    dst_pk = subpanel.to_key()
                    out.append([alias.pk[dst_pk] for alias in curr_mapped_list] if curr_mapped_list else list())
                return tuple(out)


class _MappingSubPanel(_BasePanel):
    """
    Base class for a subpanel that is embedded in a parent panel that implements an associative relationship between
    the table in the parent panel -- the "source" table -- and the table in the subpanel -- the "destination" table. The
    two tables are associated through a separate cross-reference table. The implementation only supports the following
    scenario:
        1) The primary key for the source table and the destination table consists of a single auto-incrementing integer
           attribute. These attributes are essentially opaque ID numbers assigned by the database on insert and serve
           solely as compact-valued primary keys.
        2) The cross-reference table that defines the mapping has a primary key of two attributes, namely foreign keys
           referencing the source and destination table primary keys. And the ID of each foreign key attribute must
           match the ID of the corresponding primary key in the source or destination table.

    Since it derives from _BasePanel, it supports the usual operations on its own table, which is the destination table
    in the mapping. But it also implements methods that the parent _BasePanel requires to access and update the set of
    destination table rows associated with any given source table row via the cross-reference table.

    Note that current_mappings() and mappings_for() return each mapped entity in the destination table as a _RowAlias,
    which includes a user-facing label and tooltip as well as the entity's primary key value.
    """
    def __init__(self, app: dash.Dash, map_table_id: ti.DBTable, prefix: str):
        """
        Subpanel housing the destination table for the specified cross-reference table. It is intended only for
        embedding in a panel that houses the source table for that mapping.

        Args:
            app: The Dash application object.
            map_table_id: ID of the cross-reference table in the database. Its primary key is ASSUMED to consist of
                two foreign keys, namely, the auto-incrementing PKs of the source and destination tables.
            prefix: A short string used to define unique IDs for HTML/Dash components rendered in and managed by
                this panel.
        """
        if not map_table_id.is_mapping_table():
            raise ValueError(f"Not a cross-reference table: {str(map_table_id)}")
        if len(ti.primary_key_of(map_table_id)) != 2:
            raise ValueError(f"Invalid cross-reference table: {str(map_table_id)}")
        src_pk, dst_pk = ti.primary_key_of(map_table_id)
        src_info, dst_info = ti.attribute_info(map_table_id, src_pk), ti.attribute_info(map_table_id, dst_pk)
        ok = (src_info.type == ti.AttrTypeEnum.FKEY) and (src_info.fkey_id == src_pk) and \
             (len(ti.primary_key_of(src_info.fkey_table)) == 1) and \
             (dst_info.type == ti.AttrTypeEnum.FKEY) and (dst_info.fkey_id == dst_pk) and \
             (len(ti.primary_key_of(dst_info.fkey_table)) == 1)
        if not ok:
            raise ValueError(f"Invalid cross-reference table: {str(map_table_id)}")

        self._src_pk = src_pk
        """ Auto-incrementing primary key in the source table. """
        self._dst_pk = dst_pk
        """ Auto-incrementing primary key in the destination table. """
        self._src_table_id = src_info.fkey_table
        """ ID of the source table. """
        self._dst_table_id = dst_info.fkey_table
        """ ID of the destination table. """
        self._map_table_id = map_table_id
        """ ID of the cross-reference table. """

        super().__init__(app, self._dst_table_id, prefix)

    def to_key(self) -> str:
        """ID of the auto-incrementing primary key for the destination table housed in this mapping subpanel."""
        return self._dst_pk

    def current_mappings(self) -> Dict[int, List[_RowAlias]]:
        """
        Return all source-destination entity associations stored in the cross-reference table managed by this
        mapping subpanel.

        Returns:
            Each key in this dictionary is a PK value identifying an entity in the source table that is associated with
                at least one entity in the destination table, while the corresponding value is a list of _RowAlias
                identifying entities in the destination table associated with that source entity. If an entity in the
                source table is not associated with any entity in the destination table, its primary key value will not
                appear in this dictionary. Each _RowAlias list is sorted alphabetically IAW the alias's label field. The
                dictionary will be empty if there are no current mappings or if a database access error occurs.
        """
        map_rows = DataBaseManager().fetch_rows(self._map_table_id)
        dst_map = {row[self._dst_pk]: self._to_row_alias(row) for row in self._rows()}
        src_to_dst: Dict[int, List[_RowAlias]] = dict()
        for row in map_rows:
            src_pk_val = row[self._src_pk]
            if not (src_pk_val in src_to_dst):
                src_to_dst[src_pk_val] = list()
            src_to_dst[src_pk_val].append(dst_map[row[self._dst_pk]])
        for k, v in src_to_dst.items():
            src_to_dst[k] = sorted(v, key=lambda alias: alias.label)
        return src_to_dst

    def mappings_for(self, src_pk_val: Optional[int]) -> Optional[List[_RowAlias]]:
        """
        Return all entities in this mapping's destination table that map to the specified entity in the source table.

        Args:
            src_pk_val: Primary key value for an entity in the source table (which must be an integer, by convention).
                If None, then the method retrieves all existing entities in the destination table.
        Returns:
            The list of all entities in the destination table that map to the specified entity in the source table, OR
                the list of all entities in the destination table. In either case, each destination table row is
                represented by a _RowAlias, geared for compact representation in a list- or dropdown-style UI widget.
                The returned list is sorted alphabetically by the row alias's label field. Returns None if the
                destination table is empty, if src_pk_val is not None but does not identify an existing entity in the
                source table, or if a database access error occurs.
        """
        dst_map = {row[self._dst_pk]: self._to_row_alias(row) for row in self._rows()}
        if src_pk_val is None:
            result = [v for k, v in dst_map.items()]
        else:
            restriction = {self._src_pk: src_pk_val}
            dst_pks = DataBaseManager().fetch_attribute_values(self._map_table_id, self._dst_pk, restriction)
            result = [dst_map[pk] for pk in dst_pks]
        if len(result) > 0:
            return sorted(result, key=lambda alias: alias.label)
        else:
            return None

    def update_mappings_for(self, src_pk_val: int, assoc_entities: Set[int]) -> str:
        """
        Map the specified entities in this mapping's destination table to the specified entity in the source table.
        This method updates the cross-reference table in the database that maps entities in the source table to
        entities in the destination table.

        Args:
            src_pk_val (int): Primary key value identifying an entity in the source table.
            assoc_entities (Set[int]): Set of primary key values identifying entities in the destination table that
                should be mapped to the specified source table entity. Any existing mappings for the source table
                entity are deleted from the cross-reference table before inserting the mappings specified.

        Returns:
            str: A user-facing error description if operation fails; else an empty string. Possible errors include:
            non-existent entity in either the source or destination table; database error.

        """
        error_msg = DataBaseManager().update_mapping_table(self._map_table_id, src_pk_val, assoc_entities)
        return error_msg if error_msg else ""


class RigPanel(_BasePanel):
    """ This panel provides interactive access to experiment rigs in the lab database. """
    def __init__(self, app: dash.Dash):
        super().__init__(app, ti.DBTable.RIG, 'rig')


class SubjectImplantPanel(_BasePanel):
    """
    This panel provides interactive access to the table of subject implant records in the lab database. However, rather
    than displaying all implant records for all subjects, it restricts the display to the implant history of a single
    specified subject. The panel is intended for embedding in the SubjectPanel.
    """
    def __init__(self, app: dash.Dash):
        self._curr_subj = None
        """ The panel shows the implant history for this experiment subject. If None, panel is empty. """
        super().__init__(app, ti.DBTable.IMPLANT, 'impl')

    def select_subject(self, subj_id: Optional[str]) -> None:
        """
        Select the subject whose implant history is displayed and edited in this panel. Be sure to layout the panel
        again after changing the current subject!
        Args:
            subj_id: The subject ID. If None or the specified subject does not exist in the database, then the panel
                will be emptied.
        """
        self._curr_subj = None
        if isinstance(subj_id, str) and DataBaseManager().attribute_exists(ti.DBTable.SUBJECT, 'subj_id', subj_id):
            self._curr_subj = subj_id

    def _attributes_exposed(self) -> List[str]:
        """
        Overridden to exclude the 'subj_id' attribute from the data table and entry form, since the panel only
        shows the implants for a specified subject.
        """
        attr_ids = super()._attributes_exposed()
        attr_ids.remove('subj_id')
        return attr_ids

    def layout(self) -> List[Any]:
        """
        Overridden to add a header line above the table, since this appears as a sub-panel of the main
        "Subjects" panel.
        """
        layout_cmpts = super().layout()
        layout_cmpts.insert(0, html.H5(f"{self.tab_label()}"))
        return layout_cmpts

    def _columns(self) -> List[ti.Column]:
        """  Overridden to hide the 'subj_id' attribute (the first column). """
        cols = super()._columns()
        cols.pop(0)
        return cols

    def _rows(self) -> List[Dict[str, ti.AttributeValue]]:
        """
        Overridden to only return implants for the currently selected subject. If there is no selected subject,
        then an empty list is returned.
        """
        return DataBaseManager().fetch_rows(self._table_id, {'subj_id': self._curr_subj}) if self._curr_subj else list()

    def _add_row(self, row: Dict[str, ti.AttributeValue]) -> str:
        """
        Since the panel only displays the implant history for a selected subject, this override makes sure that the
        'subj_id' key is set to that subject before adding the new implant record to the database.
        """
        if not self._curr_subj:
            raise Exception('ID of subject not specified for new implant record')
        row['subj_id'] = self._curr_subj
        return super()._add_row(row)


class SubjectPanel(_BasePanel):
    """
    This panel provides interactive access to experiment subjects in the lab database through the table view class
    tv.SubjectView. The implant history for any selected subject is displayed in a collapsible subpanel below the
    subject table. That subpanel automatically expands whenever a subject is selected and collapses when the
    selection is cleared (for example, when a subject is deleted).
    """
    def __init__(self, app: dash.Dash):
        self._implant_subpanel = SubjectImplantPanel(app)
        super().__init__(app, ti.DBTable.SUBJECT, 'subj')

    def layout(self) -> List[Any]:
        """
        Overridden to add the collapsible subpanel which shows the implant history for a selected subject in the
        main panel.
        """
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
            self._implant_subpanel.select_subject(subj_id)
            return self._implant_subpanel.layout(), is_open


class BrainAreaPanel(_BasePanel):
    """ This panel provides interactive access to the various brain regions characterized in the lab database. """
    def __init__(self, app: dash.Dash):
        super().__init__(app, ti.DBTable.BRAIN_AREA, 'brain')


class NeuronTypePanel(_BasePanel):
    """ This panel provides interactive access to the various neuron types characterized in the lab database. """
    def __init__(self, app: dash.Dash):
        super().__init__(app, ti.DBTable.NEURON_TYPE, 'n_typ')


class StudyPanel(_BasePanel):
    """
    This panel provides interactive access to research projects in the lab database, along with the publications
    associated with those projects.
    """
    def __init__(self, app: dash.Dash):
        subpanels = [StudyPanel.PublicationPanel(app)]
        super().__init__(app, ti.DBTable.STUDY, 'study', subpanels)

    def _columns(self) -> List[ti.Column]:
        """ Overridden to add a column indicating how many publications are associated with the study. """
        return([ti.Column('study_title', 'Project Title', '200px', False),
                ti.Column('study_lead', 'Prj Lead', '100px', False),
                ti.Column('study_desc', 'Description', '700px', False),
                ti.Column('n_pubs', 'Pubs', '50px', False)])

    def _rows(self) -> List[Dict[str, ti.AttributeValue]]:
        """
        Override to include information in the publication cross-reference table: The number of related publications for
        a study is displayed in the 'n_pubs' column. The related publications are listed in abbreviated form in a
        Markdown string in another undisplayed column, and that serves as the tooltip for the 'n_pubs' column.
        """
        rows = super()._rows()
        study_to_pub = self._subpanels[0].current_mappings()
        for row in rows:
            study_id = row['study_id']
            row['n_pubs'] = str(len(study_to_pub[study_id])) if study_id in study_to_pub else "0"
            row['n_pubs_tip'] = ""
            if study_id in study_to_pub:
                row['n_pubs_tip'] = ""
                for item in [alias.label for alias in study_to_pub[study_id]]:
                    row['n_pubs_tip'] = row['n_pubs_tip'] + f"- {item}\n"
        return rows

    def _num_text_lines_per_row(self) -> int:
        return 3

    def _tooltip_data_for(self, data: List[Dict[str, ti.AttributeValue]]) -> List[dict]:
        """
        Overridden to prepare tooltips for selected columns. For the 'study_desc' column, the tip lists the full text of
        the description. For the 'n_pubs' column, the tip lists the publication citations in an abbreviated format. The
        publication citations are prepared and stored in an undisplayed column in _rows().
        """
        tips = []
        for row in data:
            entry = dict()
            if len(row['study_desc']) > 0:
                desc = row['study_desc'].replace('\n', ' \n')
                entry['study_desc'] = {'value': f"{desc}", 'type': 'markdown'}
            if row['n_pubs_tip']:
                entry['n_pubs'] = {'value': row['n_pubs_tip'], 'type': 'markdown'}
            tips.append(entry)
        return tips

    def _to_row_alias(self, row: Dict[str, ti.AttributeValue]) -> _RowAlias:
        super()._to_row_alias(row)  # to validate argument
        return _RowAlias({'study_id': row['study_id']}, row['study_title'], None)

    class PublicationPanel(_MappingSubPanel):
        def __init__(self, app: dash.Dash):
            super().__init__(app, ti.DBTable.STUDY_TO_PUB, 'pub')

        def _columns(self) -> List[ti.Column]:
            """"
            Overridden to hide DOI column, replacing it with a 'link' column that uses HTML markdown to present the DOI
            as a link to the online publication. This requires that the 'link' column be tagged for markdown
            presentation in the Dash DataTable.
            """
            cols = super()._columns()
            cols[1] = ti.Column('link', "", '10px', True)
            return cols

        def _rows(self) -> List[Dict[str, ti.AttributeValue]]:
            """
            Override uses HTML markdown to embed the DOI in the 'link' column as a clickable link. This requires that
            the  'link' column be tagged for markdown presentation in the Dash DataTable.
            """
            rows = super()._rows()
            for row in rows:
                if len(row['doi']) > 0:
                    row['link'] = f"[[&#x21d7;]]({row['doi']})"
                else:
                    row['link'] = ""
            return rows

        def _to_row_alias(self, row: Dict[str, ti.AttributeValue]) -> _RowAlias:
            super()._to_row_alias(row)  # to validate argument
            citation = row['citation']
            truncated = (len(citation) > 50)
            if truncated:
                comma_idx = citation.find(',')
                quote_idx = citation.rfind('"')
                if quote_idx == -1:
                    quote_idx = citation.rfind("'")
                if comma_idx > -1 and quote_idx > -1:
                    citation = (citation[0:comma_idx] + '...' + citation[quote_idx + 1:]).strip()
                    citation = (citation[:50] + '...') if len(citation) > 50 else citation
                else:
                    citation = (citation[:50] + '...')
            return _RowAlias({'pub_id': row['pub_id']}, citation, row['citation'] if truncated else None)
