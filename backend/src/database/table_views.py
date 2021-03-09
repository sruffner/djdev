"""
table_views.py: Module manages access to DataJoint-administered database tables defined in sgl_schema.

This module defines a set of wrapper classes to add, remove and modify content in the DataJoint tables that comprise
the Lisberger lab database

Currently under development, it is intended as a layer between the raw DataJoint schema tables and a Dash-based
web application by which a user can review and curate database content. To date, the development focus has been on
the "manual" tables in the schema that contain relatively static, low-volume content that serves to categorize and
describe the experimental data collected in the Lisberger lab.

The module essentially defines a "table view" which provides a user-facing representation of the underlying DataJoint
table. BaseTableView is a base class defining the common functionality of this "table view" for most of the independent
manual tables. Subclasses add table-specific information that the base class implementation relies upon: the DataJoint
table class, information about that table's attributes, and other user-facing information for the table view.

Some database tables are related through a "junction" or "mapping" table. For example, a given brain region -- the
BrainArea table) -- will typically have several related neuron types defined in the NeuronType table. The many-to-many
relationship is captured in a mapping table, BrainAreaNeuronType. Such relationships are embodied by the base class
MappingView.

By convention, the source and destination tables for a mapping each use a single auto-incrementing integer attribute as
their primary key. This PK value is not exposed to the user, since it has no real meaning and is set automatically by
the database upon insert. Another non-PK attribute in the table -- for which a uniqueness index is included in the DJ
table definition -- serves as a user-facing attribute that uniquely labels each entity in the table. For an example,
see the definitions of BrainArea, NeuronType, and BrainAreaNeuronType in sgl_schema.py.

Created on Thu Aug 6 09:36:00 2020

@author: sruffner
"""

import re
from datetime import date
from typing import List, Dict, Tuple, Set, Optional, Union
from collections import namedtuple

from common import check_date
from database.manager import DBTable, DataBaseManager, AttributeValue


_nt_TableAttr = namedtuple("TableAttr", [
    'id', 'label', 'type', 'pkey', 'options', 'textrange', 'regex', 'regex_hint', 'placeholder', 'col_width'
    ])


class TableAttr(_nt_TableAttr):
    """
    An attribute defined on a database table. The named tuple has the following fields. Note that some fields may be
    set to None, indicating that field does not apply to the attribute.
        'id' - (str) Unique ID.

        'label' - (str) User-facing label.

        'type' - (str) Data type: 'text', 'email', 'date', 'float', 'int', 'enum', 'fkey', 'auto'. NOTES: (1)
        The 'auto' type is shorthand for 'int auto_increment' and is applicable only to a PK attribute; by convention, a
        table PK containing an 'auto' attribute will have no other attributes in the primary key. Also, an 'auto'
        attribute is not really intended for user-facing display. (2) A text area should be used as an input widget for
        'text' if the maximum text range exceeds 100 characters. (3) The 'email' type refers to a string that matches
        the regular expression for a valid email address. (4) A 'date" attribute must match 'YYYY-MM-DD' exactly, where
        Y, M and D are digits. (5) The 'float' type is a string parsable as a floating-point or integer value, while the
        'int' type is a string parsable as an integer only. (6) The 'enum' type is an enumerated attribute with a fixed
        list of options; its value is one of those options. The default value is the first option in the list. (7)
        'fkey' is a foreign key attribute. Its value identifies a single existing entity in a parent table. Unlike the
        other attributes, its domain of values is dynamic -- determined by the contents of that parent table.

        'pkey' - (bool) True if attribute is part of the table's primary key; else False.

        'options' - (List[str], None) - List of available options for an 'enum' attribute; else None.

        'textrange' - (List[int], None) The [min, max] number of allowed characters in a valid attribute value.

        'regex' - (str, None) For certain 'text' attributes, this is a regular expression that must be satisfied by
        the attribute value. Will be None for all other types and for unrestricted 'text' attributes.

        'regex_hint' - (str, None) For 'text' attributes validated by a regular expression, this is a user-facing
        message that further describes the domain of valid attribute values. It will be included in the error message
        when a proposed attribute value does not satisfy the regular expression.

        'placeholder' - (str, None) Brief string intended as placeholder for attribute value in an input widget; it
        should characterize the domain of valid attribute values. Not applicable to all attribute types.

        'col_width' - (str) The suggested column width for the attribute value when displaying table contents in a
        user-facing tabular format like the Dash DataTable. Specified in pixels, eg, '50px'.
    """
    pass


_nt_Column = namedtuple('Column', 'id label width is_markdown')


class Column(_nt_Column):
    """
    A column in the user-facing presentation of the database table underlying a BaseTableView -- characterized by the
    following fields:
        'id' - (str) The ID of the corresponding table attribute.

        'label' - (str) User-facing label that serves as the column header.

        'width' - (str) The suggested column width in pixels -- eg, '50px'.

        'is_markdown' - (bool) Flag indicating if attribute value is formatted as HTML markdown rather than plain text.

    By convention, for a table view with an 'auto' attribute, that attribute should not be exposes in a Column, since
    it is not intended for user-facing display and is not user-specified.
    """
    pass


_nt_RowAlias = namedtuple('RowAlias', 'pk label tip')


class RowAlias(_nt_RowAlias):
    """
    An alternative representation of a row in a database table that is intended for use when selecting entities from
    a list- or dropdown-style widget in the user interface. It has 3 fields:
        'pk' - (Dict[str, Any]) The primary key for the row (PK attribute-value pairs only).

        'label' - (str) The row's user-facing label. Ideally it should be no more than 50 characters long and it should
        uniquely identify the row in a way that is meaningful to the end user.

        'tip' - (Optional(str)) A longer description of the entity, which would appear in a tooltip for the list- or
        dropdown-style widget. Can be None, in which case no tip is shown.
    """
    pass


class BaseTableView:
    """
    Base class implementing read/write access to a DataJoint-administered table in the Lisberger lab's
    research database.

    This base class implements basic low-level methods for accessing the underlying table. All table access and
    manipulation goes through DatabaseManager in manager.py. Users of this module should not instantiate this base table
    view directly.

    The implementation also supports exposing only a subset of the database table, by specifying a primary key
    restriction in the constructor.

    Current limitations of BaseTableView:
        1) If a table references another "parent table" via a foreign key attribute, then the implementation ASSUMES
        the primary key for the parent table consists of a single attribute. See _foreign_key_descriptor().
        2) There is no support for modifying an attribute of an existing entity (row) in the underlying table. This is
        by design, as DataJoint "updates" an entity by first deleting it and replacing it with the modified entity --
        but that deletion will trigger other deletions in the database if the entity is referenced by other tables.
        3) No support for "nullable" attributes.
        4) The attribute type 'auto' refers to an attribute declared as "id: int auto_increment" in the DJ table schema
        definition. This kind of attribute can only appear as a primary key attribute, and it must be the ONLY
        attribute comprising the table's primary key. Its value is assigned automatically in the MySQL database on each
        insert, rather than being specified manually. Such attributes are NOT user-facing, but they're important in
        implementing simple associative tables that map entities in one table to those in another.
        5) CANNOT be used for tables containing 'blob' attributes!
        5) Handling of DataJoint/database errors IS A WORK IN PROGRESS!
    """

    def __init__(self, table_id: DBTable, label: str, row_label: str, attrs: List[TableAttr],
                 restrict: Optional[Tuple[str]] = None, master_pk: Optional[List[str]] = None):
        """
        Construct a view for the specified DataJoint-administered table in the Lisberger lab's research database.
        Subclasses call this constructor to configure the table class, attributes, label, and row label.

        Args:
            table_id: ID of the DataJoint-administered database table represented by this view.
            label (str): User-facing label for the database table.
            row_label (str): User-facing generic label for any single entity (aka row) in the database table.
            attrs (List[TableAttr]): The table attributes. These should appear in the list in the same order in which
                they should be displayed in a user-facing tabular layout of the underlying table's contents.
            restrict (Optional[Tuple[str]]): If not None, this must be a 2-tuple (pk_id, pk_value), where pk_id is
                the attribute ID of a primary key attribute for the database table. In this case, the table view will
                only expose the subset of the underlying table for which pk_id = pk_value. All rows in the table for
                which pk_id != pk_value cannot be affected by this table view. In addition, the attribute pk_id itself
                is not exposed by the view. If the argument is not None but is not a tuple of two strings or if pk_id
                is not a primary key attribute of the database table, then no subset restriction is enforced.
            master_pk : If not None, then the table is a DataJoint part table, and this is a list of the attribute IDs
                that comprise the primary key of the part table's master. Each attribute ID must correspond to one of
                the attributes in the 'attrs' argument.

        Raises:
            ValueError: If any of the required arguments are found to be invalid.
        """
        if not isinstance(table_id, DBTable):
            raise ValueError("DataJoint database table ID must be specified")
        self._table_id = table_id
        """ ID of the DataJoint-administered database table represented by this view."""
        if not (isinstance(label, str) and len(label) > 0):
            raise ValueError("Invalid table label")
        self._label = label
        """ User-facing label for the database table. """
        if not (isinstance(row_label, str) and len(row_label) > 0):
            raise ValueError("Invalid table row label")
        self._row_label = row_label
        """ User-facing generic label for any single entity (aka, row) in the database table. """
        if not (isinstance(attrs, list) and len(attrs) > 0):
            raise ValueError("Invalid table attributes")

        has_auto_pk = False
        for attr in attrs:
            auto_pk_err = ValueError('Primary key with an auto-incremented attribute may contain no other attributes')
            if not isinstance(attr, TableAttr):
                raise ValueError("Invalid table attributes")
            if attr.type == 'auto':
                if not attr.pkey:
                    raise ValueError("An auto-incremented attribute must be in the table's primary key")
                elif has_auto_pk:
                    raise auto_pk_err
                else:
                    has_auto_pk = True
            elif attr.pkey and has_auto_pk:
                raise auto_pk_err
        self._has_auto_pk = has_auto_pk
        """ Flag set if database table uses an auto-incrementing primary key. """

        self._attrs = list(attrs)
        """ The table's attributes, listed in the order in which they should be displayed in a user-facing tabular
        layout of the table's contents. """

        ok = isinstance(restrict, tuple) and (len(restrict) == 2) and isinstance(restrict[1], str)
        if ok:
            for attr in self._attrs:
                if attr.id == restrict[0]:
                    if attr.pkey:
                        ok = True
                    break
        self._restrict = restrict if ok else None
        """ Optional subset restriction, a 2-tuple (pk_id, pk_value) listing the primary key attribute ID and value
        that defines the restriction. """

        ok = isinstance(master_pk, list) and (len(master_pk) > 0)
        if ok:
            attr_ids = [attr.id for attr in self._attrs if attr.pkey]
            for el in master_pk:
                if not (el in attr_ids):
                    raise ValueError(f"Master table attribute {el} missing from part table's primary key")
        self._master_pk = master_pk if ok else None
        """ For a part table, this is a list of the attribute IDs in the master table's primary key. By definition,
        they are also in the part table's primary key. Always None if the table is NOT a part table. """

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[DBTable, str]:
        """
        Get the DataJoint table ID of the parent table for a foreign key attribute defined on this table, along with
        the defined ID of the attribute in that table (it could be different than the attribute ID in this table).

        Consider these two foreign key attribute declarations:
            -> User
            (author) -> User

        In both cases, User is the parent table. In the former case, the ID of the User table's primary key is the ID
        used in this table; in the latter case, this table assigns a different ID ('author') to the foreign key.

        **It is ASSUMED** that the parent table's primary key consists of a single attribute.

        The base class implementation raises a ValueError. Any subclass representing a table view with one or more
        foreign key attributes must override this method appropriately.

        Args:
            fkey_attr: The foreign key attribute.

        Returns:
            A tuple (table_id, pk_id) containing the ID of the the parent table and the ID of its (single) primary key
                attribute, which maps to the specified foreign key attribute in this table.

        Raises:
            ValueError: If *fkey_attr* is not a recognized foreign key attribute for this table view. The method will
                always raise this error if the table view lacks any foreign key attributes.
        """
        raise ValueError("No foreign keys defined on this table")

    def label(self) -> str:
        """
        The user-facing label for this table view. If the view is restricted to a table subset, the label will
        reflect this fact.
        """
        if self._restrict:
            return f"{self._label} for {self._restrict[1]}"
        return self._label

    def row_label(self) -> str:
        """ The user-facing label for any entry (aka, row) in this table view. """
        return self._row_label

    def attributes(self) -> List[TableAttr]:
        """
        The attributes of the database table underlying this view. Any entity (aka row) in the database table has a
        value for each of these attributes. In order to add an entity to the database table, a valid value must be
        specified for each attribute -- see add_row().

        IMPORTANT: If the table has a single-attribute, auto-incrementing primary key (type 'auto'), that attribute is
        NOT included in the returned list because it is not intended for user-facing display AND is not user-specified.

        If a primary key-value restriction is specified in the table view constructor, then that primary key attribute
        is also omitted from the list of attributes.

        Returns:
            The attribute list, as described.
        """
        return [attr for attr in self._attrs
                if (attr.type != 'auto') and ((not self._restrict) or (attr.id != self._restrict[0]))]

    def auto_primary_key_id(self) -> Optional[str]:
        """
        Return attribute ID for this table's auto-incrementing primary key, or None if it does not have one. By
        convention, if a table has an auto-incrementing integer attribute in its primary key, then that is the sole
        constituent of the PK. NOTE that the auto-incrementing PK is NOT exposed by the attributes() method.
        """
        auto_id = None
        if self._has_auto_pk:
            for attr in self._attrs:
                if attr.type == 'auto':
                    auto_id = attr.id
                    break
        return auto_id

    def is_part_table(self) -> bool:
        """ Is this table a DataJoint part table? """
        return not (self._master_pk is None)

    def attributes_not_in_master(self) -> List[TableAttr]:
        """ Get the attributes in this part table that are NOT in the master table's primary key. If this is not a
        part table, this returns the same list as attributes()."""
        if self._master_pk:
            return [attr for attr in self.attributes() if not (attr.id in self._master_pk)]
        else:
            return self.attributes()

    def attributes_in_master(self) -> Optional[List[TableAttr]]:
        """ Get the attributes in this part table that comprise the master table's primary key. Returns None if this is
        NOT a part table. """
        return [attr for attr in self.attributes() if (attr.id in self._master_pk)] if self._master_pk else None

    def columns(self) -> List[Column]:
        """
        The columns that should be included in a user-facing presentation of this view's underlying database table.

        The base class implementation returns a column for each attribute exposed by attributes().

        A subclass must override this method to construct a different user-facing view of the table's content. Possible
        use cases include: hiding one or more non-primary key attributes; changing the column order; adding a column
        that displays non-attribute content, tagging an attribute that is formatted as HTML markdown and should be
        displayed as such.

        IMPORTANT: If a subclass overrides this method to alter the user-facing table columns, then it must also
        override _transform_rows() to transform the underlying database table data from rows() to match the columns
        supplied by this method.

        Returns:
            The list of displayed table columns.
        """
        return [Column(attr.id, attr.label, attr.col_width, False) for attr in self.attributes()]

    def _transform_rows(self, rows: List[Dict[str, AttributeValue]]) -> None:
        """
        Transform each row retrieved from the underlying database table to provide the user-facing view of the table's
        contents.

        This is a helper method for rows(), invoked to transform the raw tabular data retrieved directly from the
        database table. The base class implementation does nothing. This method and columns() must be overridden to
        alter the default user-facing view offered by BaseTableView.

        Args:
            rows: The list of rows as retrieved directly from the underlying database table. On return, each row is
                transformed in the same manner to match the user-facing table columns supplied by columns(). NOTE: Do
                NOT remove any primary key attribute (such as an auto-incrementing primary key), even if columns() is
                defined so that the attribute is hidden from the user. The primary key attribute values are needed in
                order to specify a row to delete via remove_row().
        """
        pass

    def num_text_lines_per_row(self) -> int:
        """
        Number of text lines per row in a user-facing presentation of this view's underlying database table.

        The base class implementation returns 0 -- meaning each row may have any number of text lines. A subclass can
        override, returning a positive integer N, indicating that all rows should display N text lines.

        Returns:
            Number of text lines per row; 0 = no restriction.
        """
        return 0

    def tooltip_data_for(self, data: List[Dict[str, AttributeValue]]) -> List[dict]:
        """
        Generate the tooltip contents for the specified rows in a user-facing presentation of this view's underlying
        database table.

        The base class implementation returns an empty list, so no tooltips are displayed.

        Args:
            data (List[Dict[str, Any]]): The current table data, as would be returned by rows().

        Returns:
            The tooltip data -- compatible with the Dash DataTable's 'tooltip_data' property. If no tooltips are needed,
                return an empty list. Otherwise, the list length must match the number of rows in the supplied table
                data (so that any tooltip will match the corresponding table cell).
        """
        return []

    def to_row_alias(self, row: Dict[str, AttributeValue]) -> RowAlias:
        """
        Generate a representation of the specified table row that is geared toward presentation in the user interface
        within a list- or dropdown-style widget.

        This base class implementation merely joins the values of the primary key attributes (comma-separated) to form
        the label, and truncates the result to 50 characters. The associated tooltip string (RowAlias.tip) is set to
        None. Subclasses containing more than one attribute in their primary key, or a primary key that is not
        meaningful to the user (such as an auto-incrementing integer), should override this method.

        Args:
            row: A complete entity in the underlying database table, in dictionary format.
        Returns:
            RowAlias: A compact representation of that entity for UI purposes, as described.
        Raises:
            ValueError if row is missing any table attributes.
        """
        if not isinstance(row, dict):
            raise ValueError("Row must be a dict")
        for attr in self._attrs:
            if attr.id not in row:
                raise ValueError(f"Missing row attribute: '{attr.id}'")
        pk_dict = {k: row[k] for k in self._primary_key_ids()}
        label = ','.join([str(v) for k, v in pk_dict.items()])
        return RowAlias(pk_dict, label if len(label) < 53 else (label[:50] + '...'), None)

    def _primary_key_ids(self) -> Set[str]:
        """Get the set of attribute IDs comprising the primary key for the underlying table."""
        return {attr.id for attr in self._attrs if attr.pkey}

    def foreign_key_choices(self, attr: TableAttr) -> List[Tuple[str, AttributeValue]]:
        """
        Retrieve the list of available choices for a 'fkey' attribute. To support using this list in a user-facing
        dropdown or list widget, each "choice" is represented by 2-tuple (label, fkey_value), where fkey_value is the
        actual value of the foreign key and label is a unique user-facing string identifying that value.

        This implementation assumes that each foreign key value is suitable as a user-facing label. Subclasses should
        override if this is not the case (eg., if the foreign key value is a meaningless number).

        Args:
            attr: The attribute.

        Returns:
            List of all available value choices for the foreign key table attribute, with companion label as described.
                Sorted alphabetically by the label. Returns an empty list if unable to access the database.

        Raises:
            ValueError: If the specified attribute is unrecognized or is not a foreign key attribute.
        """
        if not (attr in self._attrs):
            raise ValueError('Unrecognized attribute')
        if not (attr.type == 'fkey'):
            raise ValueError(f"{attr.id} is not a foreign key attribute!")

        parent_table: DBTable
        attr_id: str
        parent_table, attr_id = self._foreign_key_descriptor(attr)
        fkey_values = sorted(DataBaseManager().fetch_attribute_values(parent_table, attr_id))
        return [(v, v) for v in fkey_values]

    def num_rows(self) -> int:
        """
        Get the current number of entities in the underlying database table. Returns 0 if unable to access database.
        """
        restriction = {self._restrict[0]: self._restrict[1]} if self._restrict else None
        return DataBaseManager().num_table_rows(self._table_id, restriction)

    def rows(self, condition: Optional[Dict[str, AttributeValue]] = None) -> List[Dict[str, AttributeValue]]:
        """Get all or a subset of rows in the underlying table in dictionary form.

        NOTE: This method should only be used for the small "manual-entry" tables in the lab database, as it retrieves
        the entire contents of the underlying table.

        Each row returned includes the value for every table attribute, including an auto-incrementing primary key. This
        is important so that a user-facing client can implement operations like "remove" that require specifying a
        particular row in the table.

        Args:
            condition: The attribute ID-value pairs in this dictionary specify a condition that any row returned in the
                result must satisfy. Default value is None -- thereby retrieving all rows in the table.

        Returns:
            The table contents in dictionary form. Each element of the list is a dictionary representing one table row,
                eg: {'attr1': value1, 'attr2': value2, ... }. Returns an empty list if the table has no rows, if the
                specified condition is not satisfied by any table row, or if unable to retrieve table contents.

        Raises:
            ValueError: If any attribute ID in *condition* is not a recognized attribute of this table.
        """
        restriction = None
        if isinstance(condition, dict) and (len(condition) > 0):
            for key in condition.keys():
                if not (key in [attr.id for attr in self._attrs]):
                    raise ValueError(f"Invalid attribute ID: {key}")
            restriction = dict(condition)
        if self._restrict:
            if restriction:
                restriction[self._restrict[0]] = self._restrict[1]
            else:
                restriction = {self._restrict[0]: self._restrict[1]}

        rows = DataBaseManager().fetch_rows(self._table_id, restriction)
        if len(rows) > 0:
            if self._restrict:
                for row in rows:
                    row.pop(self._restrict[0], None)
            self._transform_rows(rows)
        return rows

    def add_row(self, row: Dict[str, AttributeValue]) -> str:
        """
        Add an entry (aka, row) to the underlying table.

        It the table uses an auto-incrementing single-attribute primary key, any value supplied for that attribute will
        be ignored, since its value is automatically supplied by the database when the new entry is inserted.

        Args:
            row: The new entry. It must contain a valid attribute value for each table attribute -- with the exception
                of an auto-incrementing primary key.

        Returns:
            An empty string if operation succeeds, else a user-facing description of the error (missing attribute,
            invalid attribute value, attempt to add an already existing row, database error).
        """
        if self._restrict:
            row[self._restrict[0]] = self._restrict[1]
        try:
            self._validate_row(row)   # this removes auto-incrementing PK from argument, if included
            error_msg = DataBaseManager().insert_into_table(self._table_id, row)
            if error_msg:
                raise Exception(error_msg)
        except Exception as err:
            error_msg = f"Add failed: {str(err)}"
        return error_msg if error_msg else ""

    def remove_row(self, row_pk: Dict[str, AttributeValue]) -> str:
        """
        Delete the specified entry (aka, row) from the underlying table.

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
        if self.is_part_table():
            raise NotImplementedError("Cannot delete a row from a part table. Operate on master table instead. ")
        table_pk = self._primary_key_ids()
        if self._restrict:
            row_pk[self._restrict[0]] = self._restrict[1]
        for key in table_pk:
            if not (key in row_pk):
                raise ValueError(f"Delete failed: Missing primary key '{key}'")
        restriction = {key: row_pk[key] for key in table_pk}
        error_msg = DataBaseManager().delete_from_table(self._table_id, restriction)
        return error_msg if error_msg else ""

    def row_exists(self, row_pk: Dict[str, AttributeValue]) -> bool:
        """Does the specified entry/row currently exist in the underlying table?

        Args:
            row_pk: This dictionary must contain, at a minimum, the primary key attribute ID-value pairs that uniquely
                identify a single table row. Any other attributes are ignored!

        Returns:
            True if row exists, false otherwise.

        Raises:
            ValueError: If row_pk is missing any of the table's primary key attributes.
        """
        table_pk = self._primary_key_ids()
        try:
            restriction = {key: row_pk[key] for key in table_pk}
            exists = (DataBaseManager().num_table_rows(self._table_id, restriction) == 1)
        except KeyError:
            raise ValueError("Incomplete primary key")
        return exists

    def check_row(self, row: Dict[str, AttributeValue], omit_master: bool = False) -> Optional[str]:
        """
        Check whether or not the proposed row entry is valid and does not yet exist in the underlying table.

        Args:
            row: The proposed entry. It must contain a valid attribute value for each table attribute -- except for an
                auto-incrementing primary key, and it must not yet exist in the database.
            omit_master: If True and this is a part table, attributes in 'row' that are part of the master table's
                primary key are NOT checked, and existence is not checked. This is a way to check a new entry in the
                part table without first inserting the corresponding entry in the master table. Default is False.
        Returns:
            None if operation succeeds, else a user-facing description of the error (missing attribute, invalid
                attribute value, entry already exists, database error).
        """
        err_msg = None
        try:
            self._validate_row(row, omit_master)
        except (Exception, ValueError) as err:
            err_msg = f"Invalid entry: {str(err)}"
        return err_msg

    def _validate_row(self, row: Dict[str, AttributeValue], omit_master: bool = False) -> None:
        """Validate a proposed new entry in the underlying table.

        Args:
            row: The new entry. NOTE: If the table uses an auto-incrementing attribute as its primary key, that
                attribute is removed from the entry, if specified. Its value is set by the database on insert.
            omit_master: If True and this is a part table, attributes in 'row' that are part of the master table's
                primary key are NOT checked, and existence is not checked. This is a way to check a new entry in the
                part table without first inserting the corresponding entry in the master table. Default is False.
        Raises:
            Exception: If the proposed entry already exists in table, or if entry is missing any attribute value.
            ValueError: If any attribute value is invalid.
        """
        # we never check existence when the table uses an auto-incrementing PK!
        if (not (self._has_auto_pk or omit_master)) and self.row_exists(row):
            raise Exception("Attempt to add a new entry with an existing primary key")
        for attr in self._attrs:
            if attr.type != 'auto':
                if not (omit_master and self.is_part_table() and (attr.id in self._master_pk)):
                    if attr.id not in row:
                        raise Exception(f"Missing attribute: {attr.id}")
                    if not (omit_master and self.is_part_table() and (attr.id in self._master_pk)):
                        self._validate_attribute_value(attr, row[attr.id])
            elif attr.id in row:
                row.pop(attr.id, None)

    def _validate_attribute_value(self, attr: TableAttr, attr_value: Union[str, int, float, date]) -> None:
        """Validate the proposed value for an attribute in the underlying table.

        Validation of the attribute value depends on the attribute type:
            'text': The value must satisfy any regular expression defined for the attribute (if any), as well as the
                min/max restriction on text length.
            'float': Can be str, int or float, but a string value must be parsable as a float. If string, it must
                satisfy min/max restriction on text length.
            'int': Can be str or int, but a string value must be parsable as an integer. If string, it must satisfy
                min/max restriction on text length.
            'date': Can be a date or string. A string value must satisfy the format 'YYYY-MM-DD'. The date must be
                after 12/31/1899 and before today.
            'enum': Value must be one of the valid options for the attribute.
            'fkey': The attribute value must identify an existing entity in the parent table.
            'auto': An auto-incrementing PK. This type of attribute is ignored. Its value is set by the database on
                insert, NOT by the user.

        Args:
            attr: The attribute.
            attr_value: The proposed value for the attribute.

        Raises:
            ValueError: If the proposed attribute value is not valid in any way. The error description is intended to
            provide a user-facing description of the problem.
        """
        if not isinstance(attr_value, (str, int, float, date)):
            raise ValueError(f"Attribute value must be a string, number, or date: '{attr.label}'")
        if attr.type == "auto":
            return
        if attr.pkey:
            if isinstance(attr_value, str) and (attr_value == ""):
                raise ValueError(f"Missing value for primary key attribute: '{attr.label}'")
        if attr.type == 'fkey':
            foreign_table_id: DBTable
            foreign_table_id, fk_attr_id = self._foreign_key_descriptor(attr)
            if not DataBaseManager().attribute_exists(foreign_table_id, fk_attr_id, attr_value):
                raise ValueError(f"Missing foreign key: '{attr.label}' = '{attr_value}'")
        elif attr.type == 'enum':
            if not (attr_value in attr.options):
                raise ValueError(f"Invalid option for '{attr.label}': '{attr_value}'")
        elif attr.type == 'date':
            if not check_date(attr_value):
                raise ValueError(f"'{attr.label}': Date is invalid, earlier than 1900-01-01, or in the future.")
        elif attr.type == 'float':
            try:
                num_value = float(attr_value)
            except(TypeError, ValueError):
                raise ValueError(f"'{attr.label}' = '{attr_value}' cannot be parsed as a floating-point value")
            self.check_numeric_attribute_value(attr, num_value)
        elif attr.type == 'int':
            try:
                num_value = int(attr_value)
            except(TypeError, ValueError):
                raise ValueError(f"'{attr.label}' = '{attr_value}' cannot be parsed as an integer")
            self.check_numeric_attribute_value(attr, num_value)
        else:  # 'text' or 'email'
            if not isinstance(attr_value, str):
                raise ValueError(f"'{attr.label}': Value must be a string")
            if attr.textrange:
                min_len, max_len = attr.textrange
                if (len(attr_value) < min_len) | (len(attr_value) > max_len):
                    raise ValueError(f"'{attr.label}': Value must be {min_len}-{max_len} characters long.")
            if attr.regex:
                if re.fullmatch(attr.regex, attr_value) is None:
                    raise ValueError(f"Invalid value for {attr.label}: {attr.regex_hint}")

    def check_numeric_attribute_value(self, attr: TableAttr, value: Union[int, float]) -> None:
        """
        Validate a table attribute that has an integer or floating-point value (excluding auto-incrementing primary
        keys). The base implementation does nothing, but it is invoked by the base class when validating a proposed
        entry to the underlying table.

        A subclass can override this method to restrict the domain of valid values for any numeric attribute. By
        convention, it must raise a ValueError (with a user-facing descriptive error) if the attribute value is invalid.

        Args:
            attr: The attribute
            value: The proposed numeric value (integer or floating-point) for the attribute.

        Raises:
            ValueError: If the attribute value is out of range or otherwise rejected.
        """
        pass


class MappingView:
    """
    Base class represents the mapping from a source database table to a destination database table. The implementation
    only supports the following common scenario:
        1) The primary key for the source table and the destination table consists of a single auto-incrementing integer
           attribute. These attributes are essentially opaque ID numbers assigned by the database on insert and serve
           solely as compact-valued primary keys.
        2) The cross-reference table that defines the mapping has a primary key of two attributes, namely foreign keys
           referencing the source and destination table primary keys. And the ID of each foreign key attribute must
           match the ID of the corresponding primary key in the source or destination table.

    The view supports several operations: Accessing all primary keys in the destination table; accessing or updating
    the set of destination entities associated with a given source entity via the cross-reference table.

    Note that current_mappings() and mappings_for() return each mapped entity in the destination table as a RowAlias,
    which includes a user-facing label and tooltip as well as the entity's primary key value.
    """

    def __init__(self, src_table_view: BaseTableView, dst_table_view: BaseTableView, map_table_id: DBTable):
        """
        Construct an associative mapping between a source and destination table via a cross-reference table.

        Args:
            src_table_view: The table view for the source table in the database.
            dst_table_view: The table view for the destination table in the database.
            map_table_id: ID of the cross-reference table in the database. Its primary key is ASSUMED to consist of
                two foreign keys, namely, the auto-incrementing PKs of the source and destination tables.
        """
        self._src_pk = MappingView.__check_table_view(src_table_view)
        self._src_table_view = src_table_view
        self._dst_pk = MappingView.__check_table_view(dst_table_view)
        self._dst_table_view = dst_table_view

        if not isinstance(map_table_id, DBTable):
            raise ValueError("Invalid cross-reference table")
        self._map_table_id = map_table_id

    @staticmethod
    def __check_table_view(table_view: BaseTableView) -> str:
        """
        Helper method validates a table view serving as the source or destination in this mapping view and returns the
        attribute ID of the underlying table's auto-incrementing primary key.
        """
        if not isinstance(table_view, BaseTableView):
            raise ValueError("Invalid table view")
        auto_id = table_view.auto_primary_key_id()
        if not auto_id:
            raise ValueError("Source or destination table view requires auto-incrementing PK for cross-reference")
        return auto_id

    def from_key(self) -> str:
        """Attribute ID of the single auto-incrementing primary key in the source table for this mapping view."""
        return self._src_pk

    def to_key(self) -> str:
        """Attribute ID of the single auto-incrementing primary key in the destination table for this mapping view."""
        return self._dst_pk

    def current_mappings(self) -> Dict[int, List[RowAlias]]:
        """
        Return all source-destination entity associations stored in this mapping's cross-reference table

        Returns:
            Each key in this dictionary is a PK identifying an entity in the source table that is associated with at
                least one entity in the destination table, while the corresponding value is a list of RowAlias
                identifying entities in the destination table associated with that source entity. If an entity in the
                source table is not associated with any entity in the destination table, its primary key will not appear
                in this dictionary. Each RowAlias list is sorted alphabetically IAW the alias's label field. The
                dictionary will be empty if there are no current mappings or if a database access error occurs.
        """
        map_rows = DataBaseManager().fetch_rows(self._map_table_id)
        dst_map = {row[self._dst_pk]: self._dst_table_view.to_row_alias(row) for row in self._dst_table_view.rows()}
        src_to_dst = dict()
        for row in map_rows:
            src_pk_val = row[self._src_pk]
            if not (src_pk_val in src_to_dst):
                src_to_dst[src_pk_val] = list()
            src_to_dst.get(src_pk_val).append(dst_map[row[self._dst_pk]])
        for k, v in src_to_dst.items():
            src_to_dst[k] = sorted(v, key=lambda alias: alias.label)
        return src_to_dst

    def mappings_for(self, src_pk_val: Optional[int]) -> Optional[List[RowAlias]]:
        """
        Return all entities in this mapping's destination table that map to the specified entity in the source table.

        Args:
            src_pk_val: Primary key value for an entity in the source table (which must be an integer, by convention).
                If None, then the method retrieves all existing entities in the destination table.
        Returns:
            The list of all entities in the destination table that map to the specified entity in the source table, OR
                the list of all entities in the destination table. In either case, each destination table row is
                represented by a RowAlias, geared for compact representation in a list- or dropdown-style UI widget.
                The returned list is sorted alphabetically by the row alias's label field. Returns None if src_pk_val is
                not None but does not identify an existing entity in the source table. Also returns None on a database
                access error.
        """
        dst_map = {row[self._dst_pk]: self._dst_table_view.to_row_alias(row) for row in self._dst_table_view.rows()}
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
        error_msg = DataBaseManager().update_xref_table(self._map_table_id, self._src_pk, src_pk_val,
                                                        self._dst_pk, assoc_entities)
        return error_msg if error_msg else ""


class UserView(BaseTableView):
    """
    View implementing read/write access to the DataJoint-administered table of users within the Lisberger lab.

    The user table is a relatively small manual table that lists username, full name, email address, and laboratory
    role for lab members. The username serves as the primary key.
    """

    def __init__(self):
        attrs = [
            TableAttr('username', 'Username', 'text', True, None, [3, 20], r"^[a-z]{1}[a-z0-9]{2,19}$",
                      'Contains an invalid character or does not start with lowercase a-z',
                      'Enter username (unique, lowercase a-z or digit, 3-20 characters)', '100px'),
            TableAttr('full_name', 'Full Name', 'text', False, None, [3, 50],
                      r"^[A-Z][a-zA-Z'-]{3,}(?: [A-Z][a-zA-Z'-]*){0,2}$",
                      "Too many names, not capitalized, or contains a character other than [A-Za-z'-]",
                      'Enter full name (as it would appear in publication; 50 chars max)', '150px'),
            TableAttr('contact_email', 'Email Address', 'email', False, None, [7, 80],
                      r'^[A-Za-z0-9._+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6}$',
                      'Does not appear to be a valid email address',
                      'Enter email address (80 chars max)', '200px'),
            TableAttr('role', 'Role', 'enum', False,
                      ["Principal Investigator", "Post Doctoral Researcher", "Graduate Student", "Administrator"],
                      None, None, None, None, '150px')
        ]
        super().__init__(DBTable.USER, 'Lab members', 'member', attrs)


class RigView(BaseTableView):
    """
    View implementing read/write access to the DataJoint-administered table of experiment apparatuses (aka "rigs") on
    which studies are conducted in the Lisberger lab.

    The rig table is a relatively small manual table that includes an abbreviated rig name -- which serves as the
    primary key -- along with the rig's location.
    """

    def __init__(self):
        attrs = [
            TableAttr('rig_id', 'Rig ID', 'text', True, None, [1, 10], r'^[A-Z]{1}[\w .-]{0,9}$',
                      'Does not start with capital A-Z or contains an invalid character',
                      'Enter short rig name, eg "Rm 1A" (unique, 1-10 characters)', '100px'),
            TableAttr('rig_loc', 'Location', 'text', False, None, [0, 50], r'[\s\S]*',
                      'Enter rig location (eg, building and room number) [optional, up to 50 chars]', '', '500px'),
        ]
        super().__init__(DBTable.RIG, 'Experiment rigs', 'rig', attrs)


class _SubjectImplantView(BaseTableView):
    """
    View implementing read/write to the subject implants table in the Lisberger lab research database. The view only
    exposes a subset of the table, ie, the implant records for a particular experiment subject.

    This view is intended for internal use only within SubjectView.
    """

    def __init__(self, subj_id: Optional[str] = None):
        """
        Construct a view of the subject implants table. The view can optionally be restricted to expose only those
        implants in the table applicable to a specified subject. The subject ID is a foreign primary key of the table.

        Args:
            subj_id (Optional[str]): If not None, then table view is restricted to the subset of implants for the
            specified experiment subject. If that subject does not exist, the table subset is empty. If None, then
            table view exposes the entire table. Defaults to None.
        """
        attrs = [
            TableAttr('subj_id', 'Subject ID', 'fkey', True, None, None, None, None, None, None),
            TableAttr('implant_date', 'Surgery Date', 'date', True, None, [10, 10], None, None,
                      'YYYY-MM-DD', '100px'),
            TableAttr('st_ap', 'AP (mm)', 'float', False, None, [1, 10], None, None,
                      'Enter anterior-posterior coordinate of cylinder implant, in millimeters', '75px'),
            TableAttr('st_ml', 'ML (mm)', 'float', False, None, [1, 10], None, None,
                      'Enter medial-lateral coordinate of cylinder implant, in millimeters', '75px'),
            TableAttr('st_dv', 'DV (mm)', 'float', False, None, [1, 10], None, None,
                      'Enter dorsal-ventral coordinate of cylinder implant, in millimeters', '75px'),
            TableAttr('ap_angle', 'AP angle (deg)', 'float', False, None, [1, 10], None, None,
                      'Enter cylinder angle relative to anterior-posterior axis, in degrees CCW', '75px'),
            TableAttr('ml_angle', 'ML angle (deg)', 'float', False, None, [1, 10], None, None,
                      'Enter cylinder angle relative to medial-dorsal axis, in degrees CCW', '75px'),
        ]
        restrict = ('subj_id', subj_id) if subj_id else None
        super().__init__(DBTable.IMPLANT, "Implant history", "implant record", attrs, restrict)

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[DBTable, str]:
        """
        Overridden to supply the necessary information for the foreign key 'subj_id'
        """
        if fkey_attr and (fkey_attr.id == 'subj_id'):
            return DBTable.SUBJECT, 'subj_id'
        else:
            raise ValueError("Not a recognized foreign key on this table")


class SubjectView(BaseTableView):
    """
    View implementing read/write access to information about experiment subjects in the Lisberger lab research
    database. This information is found in two database tables -- the primary subjects table, and a subject implants
    table that separately holds information on implant surgeries for each subject. Since a given subject may undergo
    more than one surgery over time, there could be multiple entries in the subject implants table for any given
    subject in the subjects table.

    By design, the view only exposes the implant history for a given subject. The subject ID -- attribute ID 'subj_id'
    -- is the primary key for the subject table and a foreign primary key in the subject implants table. See the
    method implant_history_for().

    Any user-facing panel housing this view should present one table listing all existing experiment subjects (along
    with facilities for adding or removing a subject), and a second, hideable table that lists all implants for the
    currently selected subject. When no subject is selected, this implant history table should be hidden; otherwise,
    it should only show the implants for the selected subject and omit the subject's ID. Add/remove operations for
    the subject's implant history are supported via the methods add/remove_implant_for_subject().
    """

    def __init__(self):
        attrs = [
            TableAttr('subj_id', 'Subject ID', 'text', True, None, [3, 20], r"^[a-zA-z]{3,20}$",
                      'May only contain the letters A-Z (uppercase or lowercase)',
                      'Enter subject ID/nickname (unique, 3-20 characters)', '150px'),
            TableAttr('species', 'Species', 'enum', False, ["Macaca mulatta", "Homo sapiens"],
                      None, None, None, None, '150px'),
            TableAttr('dob', 'Date of Birth', 'date', False, None, [10, 10], None, None, 'YYYY-MM-DD', '150px'),
            TableAttr('sex', 'Sex', 'enum', False, ['M', 'F', '?'], None, None, None, None, '150px')
        ]
        super().__init__(DBTable.SUBJECT, 'Experiment subjects', 'subject', attrs)

    @staticmethod
    def implant_history_for(subj_id: str) -> BaseTableView:
        """
        Get the implant history table view for the specified experiment subject.

        Args:
            subj_id (str): ID uniquely identifying the experiment subject (attribute ID = 'subj_id').

        Returns:
            BaseTableView: A table view exposing the implant records for the specified subject. If the subject does
                not exist, then the table view is empty
        """
        return _SubjectImplantView(subj_id if isinstance(subj_id, str) else "???")


class NeuronTypeView(BaseTableView):
    """
    View implementing read/write to the neuron types table in the Lisberger lab research database.
    """

    def __init__(self):
        attrs = [
            TableAttr('nt_id', 'ID#', 'auto', True, None, None, None, None,
                      '(neuron type ID# is auto-generated and not user-facing)', '50px'),
            TableAttr('nt_name', 'Neuron Type', 'text', False, None, [3, 50], r'^[\w .-]{3,50}$',
                      'May only contain Unicode word characters, digits, and select punctuation',
                      'Enter a concise name or abbreviation for neuron cell type (unique, 3-50 characters)', '550px')
        ]
        super().__init__(DBTable.NEURON_TYPE, 'Neuron Types', 'neuron type', attrs)

    def to_row_alias(self, row: Dict[str, AttributeValue]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        return RowAlias({'nt_id': row['nt_id']}, row['nt_name'], None)


class BrainRegionView(BaseTableView):
    """
    View implementing read/write access to information about brain regions in the Lisberger lab research database.

    The user-facing representation of the brain regions table includes an additional column displaying the neuron
    types associated with each region. Therefore, it is important that the client refresh that representation whenever
    a neuron type is added or removed from the neuron types table, or whenever the set of neuron types associated with
    a given brain region changes (see BrainRegionToNeuronTypeView).
    """

    def __init__(self):
        attrs = [
            TableAttr('ba_id', 'ID#', 'auto', True, None, None, None, None,
                      '(brain region ID# is auto-generated and not user-facing)', '50px'),
            TableAttr('ba_name', 'Brain Region', 'text', False, None, [3, 50], r'^[\w .-]{3,50}$',
                      'May only contain Unicode word characters, digits, and select punctuation',
                      'Enter a concise name or abbreviation for brain region (unique, 3-50 characters)', '550px')
        ]
        super().__init__(DBTable.BRAIN_AREA, 'Brain regions', 'region', attrs)

    def to_row_alias(self, row: Dict[str, AttributeValue]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        return RowAlias({'ba_id': row['ba_id']}, row['ba_name'], None)

    def columns(self) -> List[Column]:
        """
        Override appends an additional column listing neuron types associated with each brain region.
        """
        return([Column('ba_name', 'Brain Region', '300px', False),
                Column('assoc_ntypes', 'Associated Neuron Types', '300px', False)])

    def _transform_rows(self, rows: List[Dict[str, AttributeValue]]) -> None:
        """
        Override appends an additional column reflecting the neuron types associated with each brain region.

        Raises:
            DataJointError if an error occurs while accessing the contents of the cross-reference table associating
            brain regions with neuron types.
        """
        if isinstance(rows, list) and (len(rows) > 0):
            area_to_ntypes = BrainRegionToNeuronTypeView().current_mappings()
            for row in rows:
                if row['ba_id'] in area_to_ntypes:
                    row['assoc_ntypes'] = ', '.join([alias.label for alias in area_to_ntypes[row['ba_id']]])
                else:
                    row['assoc_ntypes'] = ''
        return


class BrainRegionToNeuronTypeView(MappingView):
    """
    View managing the map of brain regions (BrainRegionView) to neuron types (NeuronTypeView) via a cross-reference
    table in the lab database.
    """

    def __init__(self):
        super().__init__(BrainRegionView(), NeuronTypeView(), DBTable.BRAIN_AREA_TO_NEURON_TYPE)


class PublicationView(BaseTableView):
    """
    View implementing read/write access to the DataJoint-administered table of research publications from the
    Lisberger lab.

    The publication table is a relatively small manual table that lists publication ID, its digital object identifier,
    a formal citation, and optional abstract excerpt. The publication ID is a lab moniker or nickname for the
    publication and serves as the table's primary key.
    """

    def __init__(self):
        attrs = [
            TableAttr('pub_id', 'ID#', 'auto', True, None, None, None, None,
                      '(publication ID# is auto-generated and not user-facing)', '50px'),
            TableAttr('citation', 'Citation', 'text', False, None, [50, 500], r'^[\s\S]{50,500}$', '',
                      'Enter formal citation (50-500 chars)', '480px'),
            TableAttr('doi', 'DOI', 'text', False, None, [0, 100], r'[\s\S]*', '',
                      "Enter publication's digital object ID (optional; 100 chars max)", '0px')
        ]
        super().__init__(DBTable.PUB, 'Publications', 'publication', attrs)

    def to_row_alias(self, row: Dict[str, AttributeValue]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        citation = row['citation']
        truncated = (len(citation) > 50)
        if truncated:
            comma_idx = citation.find(',')
            quote_idx = citation.rfind('"')
            if quote_idx == -1:
                quote_idx = citation.rfind("'")
            if comma_idx > -1 and quote_idx > -1:
                citation = (citation[0:comma_idx] + '...' + citation[quote_idx+1:]).strip()
                citation = (citation[:50] + '...') if len(citation) > 50 else citation
            else:
                citation = (citation[:50] + '...')
        return RowAlias({'pub_id': row['pub_id']}, citation, row['citation'] if truncated else None)

    def columns(self):
        """Overridden to hide DOI column, replacing it with a 'link' column that uses HTML markdown to present the DOI
        as a link to the online publication. This requires that the 'link' column be tagged for markdown presentation
        in the Dash DataTable."""
        cols = super().columns()
        cols[1] = Column('link', "", '10px', True)
        return cols

    def _transform_rows(self, rows: List[Dict[str, AttributeValue]]) -> None:
        """
        Override uses HTML markdown to embed the DOI in the 'link' column as a clickable link. This requires that the
        'link' column be tagged for markdown presentation in the Dash DataTable.
        """
        for row in rows:
            if len(row['doi']) > 0:
                row['link'] = f"[[&#x21d7;]]({row['doi']})"
            else:
                row['link'] = ""
        return

    def tooltip_data_for(self, data: List[Dict[str, AttributeValue]]) -> List[dict]:
        """ Override includes a tooltip for the 'citation' attribute that also includes the DOI in text form."""
        tips = []
        for row in data:
            doi = 'N/A' if len(row['doi']) == 0 else row['doi']
            tip = f"**Citation**: {row['citation']}\n\n**DOI**: {doi}"
            tips.append({'citation': {'value': tip, 'type': 'markdown'}})
        return tips


class KeywordView(BaseTableView):
    """
    View implementing read/write access to a table of research keywords or key phrases in the Lisberger lab research
    database.
    """

    def __init__(self):
        attrs = [
            TableAttr('kw_id', 'ID#', 'auto', True, None, None, None, None,
                      '(keyword ID# is auto-generated and not user-facing)', '50px'),
            TableAttr('keyword', 'Keyword', 'text', False, None, [3, 50], r'^[\w .-]{3,50}$',
                      'May only contain Unicode word characters, digits, and select punctuation',
                      'Enter new, unique keyword or phrase (3-50 characters)', '550px')
        ]
        super().__init__(DBTable.KEYWORD, 'Research keywords', 'keyword', attrs)

    def to_row_alias(self, row: Dict[str, AttributeValue]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        return RowAlias({'kw_id': row['kw_id']}, row['keyword'], None)


class StudyView(BaseTableView):
    """
    View implementing read/write access to the research projects table in the Lisberger lab research database.
    """

    def __init__(self):
        attrs = [
            TableAttr('study_id', 'ID#', 'auto', True, None, None, None, None,
                      '(study ID# is auto-generated and not user-facing)', '50px'),
            TableAttr('study_title', 'Project Title', 'text', False, None, [3, 50], r'^[\w .-]{3,50}$',
                      'May only contain Unicode word characters, digits, and select punctuation',
                      'Enter a concise descriptive project title (unique, 3-50 characters)', '200px'),
            TableAttr('study_lead', 'Prj Lead', 'fkey', False, None, None, None, None, None, '150px'),
            TableAttr('study_desc', 'Description', 'text', False, None, [0, 2048], r'[\s\S]*', '',
                      'Enter a description of the research project (optional, up to 2048 chars)', '700px')
        ]
        super().__init__(DBTable.STUDY, 'Research projects', 'project', attrs)

    def to_row_alias(self, row: Dict[str, AttributeValue]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        return RowAlias({'study_id': row['study_id']}, row['study_title'], None)

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[DBTable, str]:
        """
        Overridden to supply the necessary information for the foreign key 'study_lead'
        """
        if fkey_attr and (fkey_attr.id == 'study_lead'):
            return DBTable.USER, 'username'
        else:
            raise ValueError("Not a recognized foreign key on this table")

    def num_text_lines_per_row(self) -> int:
        return 3

    def columns(self):
        """
        Overridden to add a column indicating how many publications are associated with the study. Also, the
        study description column is labelled 'Description - Keywords', and the keywords related to a study are listed
        in the tooltip for each description cell.
        """
        return([Column('study_title', 'Project Title', '200px', False),
                Column('study_lead', 'Prj Lead', '100px', False),
                Column('study_desc', 'Description - Keywords', '700px', False),
                Column('n_pubs', 'Pubs', '50px', False)])

    def _transform_rows(self, rows: List[Dict[str, AttributeValue]]) -> None:
        """
        Override to include information in cross-reference tables: The number of related publications is displayed in
        the 'n_pubs' column. Keywords related to a research project are stored as as a comma-separated list in an
        undisplayed column, and that keyword list is included in the tooltip for the 'study_desc' column. Publications
        related to a project are listed in abbreviated form in a Markdown string in another undisplayed column, and
        that serves as the tooltip for the 'n_pubs' column.

        Raises:
            DataJointError if an error occurs while accessing the contents of the cross-reference tables.
        """
        if isinstance(rows, list) and (len(rows) > 0):
            study_to_key = StudyToKeywordView().current_mappings()
            study_to_pub = StudyToPublicationView().current_mappings()
            for row in rows:
                study_id = row['study_id']
                row['n_pubs'] = str(len(study_to_pub[study_id])) if study_id in study_to_pub else "0"
                row['keywords'] = row['n_pubs_tip'] = ""
                if study_id in study_to_key:
                    row['keywords'] = ', '.join([alias.label for alias in study_to_key[study_id]])
                if study_id in study_to_pub:
                    row['n_pubs_tip'] = ""
                    for item in [alias.label for alias in study_to_pub[study_id]]:
                        row['n_pubs_tip'] = row['n_pubs_tip'] + f"- {item}\n"

    def tooltip_data_for(self, data: List[Dict[str, AttributeValue]]) -> List[dict]:
        """
        Overridden to prepare tooltips for selected columns. For the 'study_desc' column, the tip lists the keywords
        related to the study, followed by the full description text. For the 'n_pubs' column, the tip lists the
        publication citations in an abbreviated format. The keyword and publication lists are prepared and stored in
        an undisplayed column by _transform_rows().
        """
        tips = []
        for row in data:
            desc = 'N/A' if len(row['study_desc']) == 0 else row['study_desc'].replace('\n', '  \n')
            keywords = 'N/A' if not row['keywords'] else row['keywords']
            tip = f"**Keywords**: {keywords}\n\n**Description**: {desc}"
            entry = {'study_desc': {'value': tip, 'type': 'markdown'}}
            if row['n_pubs_tip']:
                entry['n_pubs'] = {'value': row['n_pubs_tip'], 'type': 'markdown'}
            tips.append(entry)
        return tips


class StudyToKeywordView(MappingView):
    """
    View managing the map of research projects (StudyView) to research keywords (KeywordView) via a cross-reference
    table (sgl.StudyKeyword) in the lab database.
    """

    def __init__(self):
        super().__init__(StudyView(), KeywordView(), DBTable.STUDY_TO_KEY)


class StudyToPublicationView(MappingView):
    """
    View managing the map of research projects (StudyView) to research publications (PublicationView) via a
    cross-reference table (sgl.StudyPublication) in the lab database.
    """

    def __init__(self):
        super().__init__(StudyView(), PublicationView(), DBTable.STUDY_TO_PUB)


class SessionView(BaseTableView):
    """
    View implementing read/write to the Sessions table that persists all experiment sessions in the Lisberger lab
    research database.
    """

    def __init__(self):
        attrs = [
            TableAttr('experimenter', 'Experimenter', 'fkey', True, None, None, None, None, None, None),
            TableAttr('subj_id', 'Subject ID', 'fkey', True, None, None, None, None, None, None),
            TableAttr('session_date', 'Session Date', 'date', True, None, [10, 10], None, None,
                      'YYYY-MM-DD', '100px'),
            TableAttr('session_sfx', 'Suffix', 'int', True, None, [1, 1], None, None,
                      'Enter an integer in [0..9] to distinguish multiple sessions on the same date', '50px'),
            TableAttr('rig_id', 'Rig', 'fkey', False, None, None, None, None, None, None),
            TableAttr('study_id', 'Study', 'fkey', False, None, None, None, None, None, None),
            TableAttr('session_notes', 'Notes', 'text', False, None, [0, 2048], r'[\s\S]*', '',
                      'Enter any notes about this particular session (optional, up to 2048 chars)', '500px')
        ]
        super().__init__(DBTable.SESSION, "Experiment Sessions", "session", attrs)

    __fkey_info = {'experimenter': (DBTable.USER, 'username'), 'subj_id': (DBTable.SUBJECT, 'subj_id'),
                   'rig_id': (DBTable.RIG, 'rig_id'), 'study_id': (DBTable.STUDY, 'study_id')}

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[DBTable, str]:
        """ Overridden to supply the necessary information for all foreign keys in the Session table. """
        if fkey_attr and (fkey_attr.id in SessionView.__fkey_info):
            return SessionView.__fkey_info[fkey_attr.id]
        else:
            raise ValueError("Not a recognized foreign key on this table")

    def foreign_key_choices(self, attr: TableAttr) -> List[Tuple[str, AttributeValue]]:
        """ Overridden to supply a user-facing label for each research study in the foreign table sgl.Study. The actual
        foreign key value is an integer ID. Otherwise, defers to base class. """
        if attr and (attr.id == 'study_id'):
            studies = sorted(DataBaseManager().fetch_proj(DBTable.STUDY, ['study_id', 'study_title']),
                             key=lambda study: study['study_title'])
            return [] if len(studies) == 0 else [(study['study_title'], study['study_id']) for study in studies]
        else:
            return super().foreign_key_choices(attr)

    def check_numeric_attribute_value(self, attr: TableAttr, value: Union[int, float]) -> None:
        """ Override restricts the 'session_sfx' attribute to the integer range [0..9]. """
        if attr.id == 'session_sfx' and ((value < 0) or (value > 9)):
            raise ValueError("Invalid value for session suffix (must lie in 0..9)")


class SessionEPhysView(BaseTableView):
    """
    View implementing read/write to the Session.EPhys part table that persists electrophysiological recording parameters
    for any experiment session in which neuronal responses were recorded. Behavior-only sessions will not have a
    corresponding entry in this part table.
    """

    def __init__(self):
        attrs = [
            TableAttr('experimenter', 'Experimenter', 'fkey', True, None, None, None, None, None, None),
            TableAttr('subj_id', 'Subject ID', 'fkey', True, None, None, None, None, None, None),
            TableAttr('session_date', 'Session Date', 'fkey', True, None, None, None, None, None, None),
            TableAttr('session_sfx', 'Suffix', 'fkey', True, None, None, None, None, None, None),
            TableAttr('ephys_src', 'Recording Source', 'enum', False,
                      ['Omniplex', 'Omniplex clips', 'Plexon MAP', 'Maestro Waveform', 'Maestro Spike Ch'],
                      None, None, None, None, '100px'),
            TableAttr('probe_type', 'Probe Type', 'enum', False, ['single', '32-channel', 'other'], None, None, None,
                      None, '100px'),
            TableAttr('sampling_rate', 'Sample Rate (Hz)', 'float', False, None, [2, 10], None, None,
                      'Enter the electrode sampling rate in Hz', '100px'),
            TableAttr('probe_x', 'Probe X', 'float', False, None, [2, 10], None, None,
                      'Enter the X-coordinate of probe within recording cylinder implant (mm)', '100px'),
            TableAttr('probe_y', 'Probe Y', 'float', False, None, [2, 10], None, None,
                      'Enter the Y-coordinate of probe within recording cylinder implant (mm)', '100px'),
            TableAttr('probe_depth', 'Probe Depth', 'float', False, None, [2, 10], None, None,
                      'Enter insertion depth of probe (mm)', '100px'),
            TableAttr('ba_id', 'Target Region', 'fkey', False, None, None, None, None, None, None)
        ]
        super().__init__(DBTable.SESSION_EPHYS, "EPhys recording", "ephys", attrs, restrict=None,
                         master_pk=['experimenter', 'subj_id', 'session_date', 'session_sfx'])

    __fkey_info = {'experimenter': (DBTable.USER, 'username'), 'subj_id': (DBTable.SUBJECT, 'subj_id'),
                   'rig_id': (DBTable.RIG, 'rig_id'), 'study_id': (DBTable.STUDY, 'study_id'),
                   'session_date': (DBTable.SESSION, 'session_date'), 'session_sfx': (DBTable.SESSION, 'session_sfx'),
                   'ba_id': (DBTable.BRAIN_AREA, 'ba_id')}

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[DBTable, str]:
        """ Overridden to supply the necessary information for all foreign keys in the SessionEPhys table. """
        if fkey_attr and (fkey_attr.id in SessionEPhysView.__fkey_info):
            return SessionEPhysView.__fkey_info[fkey_attr.id]
        else:
            raise ValueError("Not a recognized foreign key on this table")

    def foreign_key_choices(self, attr: TableAttr) -> List[Tuple[str, AttributeValue]]:
        """ Overridden to supply a user-facing label for each brain region in the foreign table sgl.BrainArea. The
        actual foreign key value is an integer ID. Otherwise, defers to base class. """
        if attr and (attr.id == 'ba_id'):
            regions = sorted(DataBaseManager().fetch_proj(DBTable.BRAIN_AREA, ['ba_id', 'ba_name']),
                             key=lambda region: region['ba_name'])
            return [] if len(regions) == 0 else [(region['ba_name'], region['ba_id']) for region in regions]
        else:
            return super().foreign_key_choices(attr)
