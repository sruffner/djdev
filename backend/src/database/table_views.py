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
from typing import List, Dict, Any, Tuple, Set, Optional, Type
from collections import namedtuple
from json import JSONDecoder
from functools import partial

from datajoint import DataJointError
import datajoint as dj
import database.sgl_schema as sgl


def reset_database() -> str:
    """
    Empty all of the 'manual' data tables in the Lisberger lab database schema. This effectively resets the database to
    its initially empty state. IT IS INTENDED ONLY FOR USE DURING DEVELOPMENT of the lab web portal application.

    Returns:
        An empty string if database reset was successful, else an error message.
    """
    user_table = sgl.User()
    err_msg = ""
    try:
        with user_table.connection.transaction:
            user_table.delete(False)
            sgl.Rig().delete(False)
            sgl.Subject().delete(False)
            sgl.BrainArea().delete(False)
            sgl.NeuronType().delete(False)
            sgl.Study().delete(False)
            sgl.Keyword().delete(False)
            sgl.Publication().delete(False)
            sgl.TrialProtocol().delete(False)
    except Exception as err:
        err_msg = f"Failed to reset database: {err}"
    return err_msg


def seed_database() -> str:
    """
    Seed the Lisberger lab database IAW the contents of the file ./assets/seed_data.txt. This file contains a sequence
    of JSON objects separated by whitespace (a linefeed or CRLF pair). Each object defines an entity to be added to the
    lab database. It has the following format:
        { "table": "<table name>", "entry": {<entry definition>}}
    The <table name> must exactly match one of the manual tables in the database schema: "User", "Subject", "Rig", etc.
    The <entry definition> is the set of attribute name-value pairs defining the new table "row". For example, to add
    a new user:
        { "table": "User", "entry": {"username": "sruffner", "full_name": "Scott A Ruffner",
        "contact_email": "sruffner@srscicomp.com", "role": "Administrator"}

    THIS METHOD IS INTENDED ONLY FOR USE DURING DEVELOPMENT. With it we can populate the database with some initial
    data whenever we redeploy the lab database portal or need to recover from a catastrophic failure.

    The entries listed in the seed file are assumed to be presented in a valid order. For example, a subject is added
    to the Subject table before any implants for that subject are added to the SubjectImplant table. Also, each entry's
    attribute name-value pairs are assumed to be valid. If not, the add operation may fail.

    Returns:
        An informational string indicating how many entries were processed and how many were successfully added. If a
        fatal error occurs, an error description is returned.
    """
    name_to_table = {
        "User": sgl.User(),
        "Subject": sgl.Subject(),
        "SubjectImplant": sgl.SubjectImplant(),
        "Rig": sgl.Rig(),
        "BrainArea": sgl.BrainArea(),
        "NeuronType": sgl.NeuronType(),
        "BrainAreaNeuronType": sgl.BrainAreaNeuronType(),
        "Study": sgl.Study(),
        "Publication": sgl.Publication(),
        "Keyword": sgl.Keyword(),
        "StudyKeyword": sgl.StudyKeyword(),
        "StudyPublication": sgl.StudyPublication()
    }
    json_decoder = JSONDecoder()
    n_parsed = n_added = 0
    try:
        with open('./assets/seed_data.txt', 'r') as file_obj:
            for add_dict in json_parse(file_obj, json_decoder):
                n_parsed += 1
                if isinstance(add_dict, dict) and ("table" in add_dict) and ("entry" in add_dict)\
                        and (add_dict["table"] in name_to_table):
                    try:
                        name_to_table[add_dict["table"]].insert1(add_dict["entry"], replace=False)
                        n_added += 1
                    except Exception:
                        pass
    except Exception as err:
        msg = f"Seed operation failed: {err}"
    else:
        msg = f"Processed {n_parsed} entries; {n_added} successfully inserted into database."

    return msg


def json_parse(file_obj, decoder: JSONDecoder = JSONDecoder(), buffer_size: int = 2048,
               delimiters: Optional[str] = None) -> Optional[Any]:
    """
    Generator function that parses zero or more JSON entities from a text IO stream.

    Args:
        file_obj: The text IO stream.
        decoder (JSONDecoder): Optional supplied JSON decoder used to parse JSON objects from the text stream. If none
            is supplied, one will be created for use by the generator
        buffer_size (int_: Desired buffer size for reading from the text stream; defaults to 2048
        delimiters (Optional[str]): String containing the set of characters separating consecutive JSON objects in the
            source text stream. If omitted, it is assumed that whitespace separates the objects

    Returns:
        Any: Python representation of the JSON entity (array, object) returned, or None if no objects remain.
    """
    remainder = ''
    for chunk in iter(partial(file_obj.read, buffer_size), ''):
        remainder += chunk
        while remainder:
            try:
                stripped = remainder.strip(delimiters)
                result, index = decoder.raw_decode(stripped)
                yield result
                remainder = stripped[index:]
            except ValueError:
                # Not enough data to decode, read more
                break


_nt_TableAttr = namedtuple("TableAttr", [
    'id', 'label', 'type', 'pkey', 'options', 'textrange', 'regex', 'regex_hint', 'placeholder', 'col_width'
    ])


class TableAttr(_nt_TableAttr):
    """
    An attribute defined on a database table. The named tuple has the following fields. Note that some fields may be
    set to None, indicating that field does not apply to the attribute.
        'id' - (str) Unique ID.

        'label' - (str) User-facing label.

        'type' - (str) Data type: 'text', 'email', 'date', 'float', 'enum', 'fkey', 'auto'. NOTES: (1) The 'auto' type
        is shorthand for 'int auto_increment' and is applicable only to a PK attribute; by convention, a table PK
        containing an 'auto' attribute will have no other attributes in the primary key. Also, an 'auto' attribute is
        not really intended for user-facing display. (2) A text area should be used as an input widget for 'text' if the
        maximum text range exceeds 100 characters. (3) The 'email' type refers to a string that matches the regular
        expression for a valid email address. (4) A 'date" attribute must match 'YYYY-MM-DD' exactly, where Y, M and D
        are digits. (5) The 'float' type is a string parsable as a floating-point or integer value. (6) The 'enum' type
        is an enumerated attribute with a fixed list of options; its value is one of those options. The default value is
        the first option in the list. (7) 'fkey' is a foreign key attribute. Its value identifies a single existing
        entity in a parent table. Unlike the other attributes, its domain of values is dynamic -- determined by the
        contents of that parent table.

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

    This base class implements basic low-level methods for accessing the underlying table. Users of this module
    should not instantiate this base table view directly.

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
        5) Handling of DataJoint/database errors IS A WORK IN PROGRESS!
    """

    def __init__(self, table: dj.Table, label: str, row_label: str, attrs: List[TableAttr],
                 restrict: Optional[Tuple[str]] = None):
        """
        Construct a view for the specified DataJoint-administered table in the Lisberger lab's research database.
        Subclasses call this constructor to configure the table class, attributes, label, and row label.

        Args:
            table (dj.Table): The DataJoint-administered database table represented by this view.
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

        Raises:
            ValueError: If any of the required arguments are found to be invalid.
        """
        if not isinstance(table, dj.Table):
            raise ValueError("DataJoint database table must be specified")
        self._table = table
        if not (isinstance(label, str) and len(label) > 0):
            raise ValueError("Invalid table label")
        self._label = label
        if not (isinstance(row_label, str) and len(row_label) > 0):
            raise ValueError("Invalid table row label")
        self._row_label = row_label
        if not (isinstance(attrs, list) and len(attrs) > 0):
            raise ValueError("Invalid table attributes")
        self._has_auto_pk = False
        for attr in attrs:
            auto_pk_err = ValueError('Primary key with an auto-incremented attribute may contain no other attributes')
            if not isinstance(attr, TableAttr):
                raise ValueError("Invalid table attributes")
            if attr.type == 'auto':
                if not attr.pkey:
                    raise ValueError("An auto-incremented attribute must be in the table's primary key")
                elif self._has_auto_pk:
                    raise auto_pk_err
                else:
                    self._has_auto_pk = True
            elif attr.pkey and self._has_auto_pk:
                raise auto_pk_err
        self._attrs = list(attrs)
        self._restrict = None
        if restrict and isinstance(restrict, tuple) and (len(restrict) == 2) and isinstance(restrict[1], str):
            for attr in self._attrs:
                if attr.id == restrict[0]:
                    if attr.pkey:
                        self._restrict = restrict
                    break

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[Type[dj.Table], str]:
        """
        Get the DataJoint table class of the parent table for a foreign key attribute defined on this table, along with
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
            fkey_attr (TableAttr): The foreign key attribute.

        Returns:
            Tuple[Type[dj.Table], str]: A tuple (table_cls, pk_id) containing the DataJoint table class for the parent
                table and the ID of its (single) primary key attribute, which maps to the specified foreign key
                attribute in this table.

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
            List[TableAttr]: The attribute list, as described.
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
            List[Column]: The list of displayed table columns.
        """
        return [Column(attr.id, attr.label, attr.col_width, False) for attr in self.attributes()]

    def _transform_rows(self, rows: List[Dict[str, Any]]) -> None:
        """
        Transform each row retrieved from the underlying database table to provide the user-facing view of the table's
        contents.

        This is a helper method for rows(), invoked to transform the raw tabular data retrieved directly from the
        database table. The base class implementation does nothing. This method and columns() must be overridden to
        alter the default user-facing view offered by BaseTableView.

        Args:
            rows (List[Dict[str, Any]]): The list of rows as retrieved directly from the underlying database table. On
                return, each row is transformed in the same manner to match the user-facing table columns supplied by
                columns(). NOTE: Do NOT remove any primary key attribute (such as an auto-incrementing primary key),
                even if columns() is defined so that the attribute is hidden from the user. The primary key attribute
                values are needed in order to specify a row to delete via remove_row().
        """
        pass

    def num_text_lines_per_row(self) -> int:
        """
        Number of text lines per row in a user-facing presentation of this view's underlying database table.

        The base class implementation returns 0 -- meaning each row may have any number of text lines. A subclass can
        override, returning a positive integer N, indicating that all rows should display N text lines.

        Returns:
            int: Number of text lines per row; 0 = no restriction.
        """
        return 0

    def tooltip_data_for(self, data: List[Dict[str, Any]]) -> List[dict]:
        """
        Generate the tooltip contents for the specified rows in a user-facing presentation of this view's underlying
        database table.

        The base class implementation returns an empty list, so no tooltips are displayed.

        Args:
            data (List[Dict[str, Any]]): The current table data, as would be returned by rows().

        Returns:
            List[dict]: The tooltip data -- compatible with the Dash DataTable's 'tooltip_data' property. If no
                tooltips are needed, return an empty list. Otherwise, the list length must match the number of rows
                in the supplied table data (so that any tooltip will match the corresponding table cell).
        """
        return []

    def to_row_alias(self, row: Dict[str, Any]) -> RowAlias:
        """
        Generate a representation of the specified table row that is geared toward presentation in the user interface
        within a list- or dropdown-style widget.

        This base class implementation merely joins the values of the primary key attributes (comma-separated) to form
        the label, and truncates the result to 50 characters. The associated tooltip string (RowAlias.tip) is set to
        None. Subclasses containing more than one attribute in their primary key, or a primary key that is not
        meaningful to the user (such as an auto-incrementing integer), should override this method.

        Args:
            row (Dict[str, Any]): A complete entity in the underlying database table, in dictionary format.
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

    def foreign_key_choices(self, attr: TableAttr) -> Set[object]:
        """
        Retrieve the set of available choices for a 'fkey' attribute. This is simply the set of all existing values of
        the foreign key attribute in the parent table.

        Args:
            attr (TableAttr): The attribute.

        Returns:
            Set[object]: Set of all available value choices for the specified table attribute. Returns an empty set
                if unable to access the database.

        Raises:
            ValueError: If the specified attribute is unrecognized or is not a foreign key attribute.
        """
        if not (attr in self._attrs):
            raise ValueError('Unrecognized attribute')
        if not (attr.type == 'fkey'):
            raise ValueError(f"{attr.id} is not a foreign key attribute!")

        parent_table: Type[dj.Table]
        attr_id: str
        parent_table, attr_id = self._foreign_key_descriptor(attr)

        try:
            res = set(parent_table().fetch(attr_id))
        except DataJointError:
            res = {}
        except Exception:
            res = {}
        return res

    def num_rows(self) -> int:
        """
        Get the current number of entities in the underlying database table. Returns 0 if unable to access database.
        """
        try:
            query = (self._table & {self._restrict[0]: self._restrict[1]}) if self._restrict else self._table
            n = len(query)
        except DataJointError:
            n = 0
        return n

    def rows(self, condition: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Get all or a subset of rows in the underlying table in dictionary form.

        NOTE: This method should only be used for the small "manual-entry" tables in the lab database, as it retrieves
        the entire contents of the underlying table.

        Each row returned includes the value for every table attribute, including an auto-incrementing primary key. This
        is important so that a user-facing client can implement operations like "remove" that require specifying a
        particular row in the table.

        Args:
            condition (Optional[Dict[str, Any]]): The attribute ID-value pairs in this dictionary specify a condition
            that any row returned in the result must satisfy. Default value is None -- thereby retrieving all rows in
            the table.

        Returns:
            List[Dict[str, Any]]: The table contents in dictionary form. Each element of the list is a dictionary
            representing one table row, eg: {'attr1': value1, 'attr2': value2, ... }. Returns an empty list if the
            table has no rows or if unable to retrieve table contents.

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

        try:
            query = (self._table & restriction) if self._restrict else self._table
            rows = query.fetch(as_dict=True)
            if self._restrict:
                for row in rows:
                    row.pop(self._restrict[0], None)
            self._transform_rows(rows)
        except DataJointError:
            rows = []
        return rows

    def add_row(self, row: Dict[str, Any]) -> str:
        """
        Add an entry (aka, row) to the underlying table.

        It the table uses an auto-incrementing single-attribute primary key, any value supplied for that attribute will
        be ignored, since its value is automatically supplied by the database when the new entry is inserted.

        Args:
            row (Dict[str, Any]): The new entry. It must contain a valid attribute value for each table attribute
                specified by attributes() -- with the exception of an auto-incrementing primary key.

        Returns:
            str: An empty string if operation succeeds, else a user-facing description of the error (missing attribute,
            invalid attribute value, attempt to add an already existing row, database error).
        """
        error_msg = ""
        if self._restrict:
            row[self._restrict[0]] = self._restrict[1]
        try:
            self._validate_row(row)   # this removes auto-incrementing PK from argument, if included
            self._table.insert1(row, replace=False)
        except Exception as err:
            error_msg = f"Add failed: {str(err)}"
        return error_msg

    def remove_row(self, row_pk: Dict[str, Any]) -> str:
        """Delete the specified entry (aka, row) from the underlying table.

        Args:
            row_pk (Dict[str, Any]): Must contain, at a minimum, the primary key attribute-value pairs that uniquely
            identify the table row. Any other attributes are ignored!

        Returns:
            str: A description of the error if operation fails on database. An empty string if operation succeeds.

        Raises:
            ValueError: If row_pk is missing any of the table's primary key attributes.
        """
        table_pk = self._primary_key_ids()
        error_msg = ""
        if self._restrict:
            row_pk[self._restrict[0]] = self._restrict[1]
        try:
            restriction = {key: row_pk[key] for key in table_pk}
            (self._table & restriction).delete(verbose=False)
        except DataJointError as err:
            error_msg = f"Delete failed: {str(err)}"
        except KeyError:
            raise ValueError("Incomplete primary key")
        return error_msg

    def _row_exists(self, row_pk: Dict[str, Any]) -> bool:
        """Does the specified entry/row currently exist in the underlying table?

        Args:
            row_pk (Dict[str, Any]): This dictionary must contain, at a minimum, the primary key attribute-value pairs
            that uniquely identify a single table row. Any other attributes are ignored!

        Returns:
            bool: True if row exists, false otherwise.

        Raises:
            ValueError: If row_pk is missing any of the table's primary key attributes.
        """
        table_pk = self._primary_key_ids()
        try:
            restriction = {key: row_pk[key] for key in table_pk}
            exists = (len(self._table & restriction) == 1)
        except DataJointError:
            exists = False
        except KeyError:
            raise ValueError("Incomplete primary key")
        return exists

    def _validate_row(self, row: Dict[str, Any]) -> None:
        """Validate a proposed new entry in the underlying table.

        Args:
            row (Dict[str, Any]): The new entry. NOTE: If the table uses an auto-incrementing attribute as its primary
                key, that attribute is removed from the entry, if specified. Its value is set by the database on insert.

        Raises:
            Exception: On an attempt to add an already existing row as new; if entry is missing any attribute value.
            ValueError: If any attribute value is invalid.
        """
        # we never check existence when the table uses an auto-incrementing PK!
        if (not self._has_auto_pk) and self._row_exists(row):
            raise Exception("Attempt to add a new entry with an existing primary key")
        for attr in self._attrs:
            if attr.type != 'auto':
                if attr.id not in row:
                    raise Exception(f"Missing attribute: {attr.id}")
                self._validate_attribute_value(attr, row[attr.id])
            elif attr.id in row:
                row.pop(attr.id, None)

    def _validate_attribute_value(self, attr: TableAttr, attr_value: str) -> None:
        """Validate the proposed value for an attribute in the underlying table.

        Validation of the string value depends on the attribute type:
            'text': The value must satisfy any regular expression defined for the attribute (if any), as well as the
                min/max restriction on text length.
            'float': Value must satisfy min/max restriction on text length and be parsable as a floating-point number.
            'date': Value must represent a valid date in the string format 'YYYY-MM-DD', and it must represent a date
                after 12/31/1899 and before today.
            'enum': Value must be one of the valid options for the attribute.
            'fkey': The attribute value must identify an existing entity in the parent table.
            'auto': An auto-incrementing PK. This type of attribute is ignored. Its value is set by the database on
                insert, NOT by the user.
        Args:
            attr (TableAttr): The attribute.
            attr_value (str]): The proposed value for the attribute, in string form.

        Raises:
            ValueError: If the proposed attribute value is not valid in any way. The error description is intended to
            provide a user-facing description of the problem.
        """
        if not isinstance(attr_value, str):
            attr_value = ""
        if attr.type == "auto":
            return
        if attr.pkey:
            if attr_value == "":
                raise ValueError(f"Missing value for primary key attribute: '{attr.label}'")
        if attr.type == 'fkey':
            foreign_table_class: dj.Table
            foreign_table_class, fk_attr_id = self._foreign_key_descriptor(attr)
            restriction = f'{fk_attr_id} = "{attr_value}"'
            try:
                if not bool(foreign_table_class & restriction):
                    raise ValueError(f"Missing foreign key: '{attr.label}' = '{attr_value}'")
            except DataJointError:
                raise ValueError(f"Database error. Unable to verify foreign key: '{attr.label}' = '{attr_value}'")
        elif attr.type == 'enum':
            if not (attr_value in attr.options):
                raise ValueError(f"Invalid option for '{attr.label}': '{attr_value}'")
        elif attr.type == 'date':
            try:
                date_obj = date.fromisoformat(attr_value)
                if not ((date_obj.year > 1899) and (date_obj < date.today())):
                    raise ValueError("out of range")
            except(TypeError, ValueError):
                raise ValueError(f"'{attr.label}': Date is invalid, earlier than 1900-01-01, or in the future.")
        elif attr.type == 'float':
            try:
                float(attr_value)
            except(TypeError, ValueError):
                raise ValueError(f"'{attr.label}' = '{attr_value}' cannot be parsed as a floating-point value")
        else:
            if attr.textrange:
                min_len, max_len = attr.textrange
                if (len(attr_value) < min_len) | (len(attr_value) > max_len):
                    raise ValueError(f"'{attr.label}': Value must be {min_len}-{max_len} characters long.")
            if attr.regex:
                if re.fullmatch(attr.regex, attr_value) is None:
                    raise ValueError(f"Invalid value for {attr.label}: {attr.regex_hint}")


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

    def __init__(self, src_table_view: BaseTableView, dst_table_view: BaseTableView, map_table: dj.Table):
        """
        Construct an associative mapping between a source and destination table via a cross-reference table.

        Args:
            src_table_view (BaseTableView): The table view for the source table in the database.
            dst_table_view (BaseTableView): The table view for the destination table in the database.
            map_table (dj.Table): The cross-reference table in the database. Its primary key is ASSUMED to consist of
                two foreign keys, namely, the auto-incrementing PKs of the source and destination tables.
        """
        self._src_pk = MappingView.__check_table_view(src_table_view)
        self._src_table_view = src_table_view
        self._dst_pk = MappingView.__check_table_view(dst_table_view)
        self._dst_table_view = dst_table_view

        if not isinstance(map_table, dj.Table):
            raise ValueError("Invalid cross-reference table")
        self._map_table = map_table

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
            Dict[int, List[RowAlias]]: Each key in this dictionary is a PK identifying an entity in the source table
            that is associated with at least one entity in the destination table, while the corresponding value is a
            list of RowAliases identifying entities in the destination table associated with that source entity. If an
            entity in the source table is not associated with any entity in the destination table, its primary key will
            not appear in this dictionary. Each RowAlias list is sorted alphabetically IAW the alias' label field.

        Raises:
            DataJointError: If a database error occurs while accessing the tables underlying this mapping view.
        """
        map_rows = self._map_table.fetch(as_dict=True)
        dst_map = {row[self._dst_pk]: self._dst_table_view.to_row_alias(row) for row in self._dst_table_view.rows()}
        src_to_dst = dict()
        for row in map_rows:
            src_pk_val = row[self._src_pk]
            if not (src_pk_val in src_to_dst):
                src_to_dst[src_pk_val] = list()
            src_to_dst[src_pk_val].append(dst_map[row[self._dst_pk]])
        for k, v in src_to_dst.items():
            src_to_dst[k] = sorted(v, key=lambda alias: alias.label)
        return src_to_dst

    def mappings_for(self, src_pk_val: Optional[int]) -> Optional[List[RowAlias]]:
        """
        Return all entities in this mapping's destination table that map to the specified entity in the source table.

        Args:
            src_pk_val (Optional[int]): Primary key value for an entity in the source table (which must be an integer,
            by convention). If None, then the method retrieves all existing entities in the destination table.
        Returns:
            Optional[List[RowAlias]]: The list of all entities in the destination table that map to the specified entity
            in the source table, OR the list of all entities in the destination table. In either case, each destination
            table row is represented by a RowAlias, geared for compact representation in a list- or dropdown-style
            UI widget. The returned list is sorted alphabetically by the row alias's label field. Returns None if
            src_pk_val is not None but does not identify an existing entity in the source table. Also returns None on a
            database error.
        """
        result = None
        try:
            dst_map = {row[self._dst_pk]: self._dst_table_view.to_row_alias(row) for row in self._dst_table_view.rows()}
            if src_pk_val is None:
                result = [v for k, v in dst_map.items()]
            else:
                restriction = {self._src_pk: src_pk_val}
                dst_pks = (self._map_table & restriction).fetch(self._dst_pk)
                result = [dst_map[pk] for pk in dst_pks]
        except DataJointError:
            pass
        if result:
            result = sorted(result, key=lambda alias: alias.label)
        return result

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
        xref_rows = [{self._src_pk: src_pk_val, self._dst_pk: key} for key in assoc_entities]
        error_msg = ""
        try:
            with self._map_table.connection.transaction:
                (self._map_table & {self._src_pk: src_pk_val}).delete(verbose=False)
                self._map_table.insert(xref_rows)
        except Exception as err:
            error_msg = f"Failed to add {self._src_table_view.row_label()} to {self._dst_table_view.row_label()} "
            error_msg += f"cross-references: {str(err)}"
        return error_msg


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
        super().__init__(sgl.User(), 'Lab members', 'member', attrs)


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
        super().__init__(sgl.Rig(), 'Experiment rigs', 'rig', attrs)


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
            TableAttr('implant_date', 'Surgery Date', 'data', True, None, [10, 10], None, None,
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
        super().__init__(sgl.SubjectImplant(), "Implant history", "implant record", attrs, restrict)

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[Type[dj.Table], str]:
        """
        Overridden to supply the necessary information for the foreign key 'subj_id'
        """
        if fkey_attr and (fkey_attr.id == 'subj_id'):
            return sgl.Subject, 'subj_id'
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
        super().__init__(sgl.Subject(), 'Experiment subjects', 'subject', attrs)

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
        super().__init__(sgl.NeuronType(), 'Neuron Types', 'neuron type', attrs)

    def to_row_alias(self, row: Dict[str, Any]) -> RowAlias:
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
        super().__init__(sgl.BrainArea(), 'Brain regions', 'region', attrs)

    def to_row_alias(self, row: Dict[str, Any]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        return RowAlias({'ba_id': row['ba_id']}, row['ba_name'], None)

    def columns(self) -> List[Column]:
        """
        Override appends an additional column listing neuron types associated with each brain region.
        """
        return([Column('ba_name', 'Brain Region', '300px', False),
                Column('assoc_ntypes', 'Associated Neuron Types', '300px', False)])

    def _transform_rows(self, rows: List[Dict[str, Any]]) -> None:
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
    table (sgl.BrainAreaNeuronType) in the lab database.
    """

    def __init__(self):
        super().__init__(BrainRegionView(), NeuronTypeView(), sgl.BrainAreaNeuronType())


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
        super().__init__(sgl.Publication(), 'Publications', 'publication', attrs)

    def to_row_alias(self, row: Dict[str, Any]) -> RowAlias:
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

    def _transform_rows(self, rows: List[Dict[str, Any]]) -> None:
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

    def tooltip_data_for(self, data: List[Dict[str, Any]]) -> List[dict]:
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
        super().__init__(sgl.Keyword(), 'Research keywords', 'keyword', attrs)

    def to_row_alias(self, row: Dict[str, Any]) -> RowAlias:
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
        super().__init__(sgl.Study(), 'Research projects', 'project', attrs)

    def to_row_alias(self, row: Dict[str, Any]) -> RowAlias:
        super().to_row_alias(row)  # to validate argument
        return RowAlias({'study_id': row['study_id']}, row['study_title'], None)

    def _foreign_key_descriptor(self, fkey_attr: TableAttr) -> Tuple[Type[dj.Table], str]:
        """
        Overridden to supply the necessary information for the foreign key 'study_lead'
        """
        if fkey_attr and (fkey_attr.id == 'study_lead'):
            return sgl.User, 'username'
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

    def _transform_rows(self, rows: List[Dict[str, Any]]) -> None:
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
                row['keywords'] = row['n_pubs_tip'] = None
                if study_id in study_to_key:
                    row['keywords'] = ', '.join([alias.label for alias in study_to_key[study_id]])
                if study_id in study_to_pub:
                    row['n_pubs_tip'] = ""
                    for item in [alias.label for alias in study_to_pub[study_id]]:
                        row['n_pubs_tip'] = row['n_pubs_tip'] + f"- {item}\n"

    def tooltip_data_for(self, data: List[Dict[str, Any]]) -> List[dict]:
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
        super().__init__(StudyView(), KeywordView(), sgl.StudyKeyword())


class StudyToPublicationView(MappingView):
    """
    View managing the map of research projects (StudyView) to research publications (PublicationView) via a
    cross-reference table (sgl.StudyPublication) in the lab database.
    """

    def __init__(self):
        super().__init__(StudyView(), PublicationView(), sgl.StudyPublication())
