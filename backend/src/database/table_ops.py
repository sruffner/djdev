"""
table_ops.py: Datajoint-mediated access to the database tables for the Lisberger lab portal.

All Datajoint-mediated manipulations of the Lisberger lab portal database (single-row insertions, removals, and
updates; so-called mapping table updates; and various types of retrieval fetches) are encapsulated in this module.

This is the only module that imports database.sgl_schema, and it is that import which will trigger a connection to the
database server if it is not already there. That is why it is very important to open the connection early in application
startup -- see config.config.AppConfig.

IMPORTANT: DataJoint's persistent connection to the database is NOT thread-safe. If multiple threads try to use it,
internal packet communictions errors will occur in the PYMYSQL module. Either protect the calls to this module with a
threading lock or ensure single-threaded usage.

Currently, the strategy is to use a single-threaded Dash/Flask server for testing on the local development machine. When
deployed in production, we'll use GUnicorn or other production-ready WSGI server that runs single-threaded, and deploy
replicas behind an NGINX proxy to maintain responsiveness. If this proves problematic, we will have to reinstitute
a threading lock....

@author: sruffner
@created: 11oct2021
"""
import re
from typing import Optional, List, Union, Dict, Set, Tuple
from datetime import date, datetime
import datajoint as dj
import numpy as np
from datajoint.expression import QueryExpression

from config.app_logging import get_application_logger
from database.log_ops import log_add_table_row, log_update_table_row, log_delete_from_table, log_mapping_table_update
from database.table_info import DBTable, AttributeValue, attributes_of, primary_key_of, has_auto_primary_key, \
    attribute_info, AttrTypeEnum, validate_numeric_attribute_value
from sglportalapi.util import check_date
import database.sgl_schema as sgl


_table_map: Dict[DBTable, dj.Table] = {
    DBTable.USER: sgl.User(),
    DBTable.SUBJECT: sgl.Subject(),
    DBTable.IMPLANT: sgl.SubjectImplant(),
    DBTable.RIG: sgl.Rig(),
    DBTable.BRAIN_AREA: sgl.BrainArea(),
    DBTable.NEURON_TYPE: sgl.NeuronType(),
    DBTable.STUDY: sgl.Study(),
    DBTable.PUB: sgl.Publication(),
    DBTable.STUDY_TO_PUB: sgl.StudyPublication(),
    DBTable.SESSION: sgl.Session(),
    DBTable.SESSION_EPHYS: sgl.Session.EPhys(),
    DBTable.SESSION_NEURON: sgl.Session.Neuron(),
    DBTable.TRIAL_PROTOCOL: sgl.TrialProtocol(),
    DBTable.TRIAL: sgl.Trial(),
    DBTable.TRIAL_EVENT: sgl.Trial.Event(),
    DBTable.TRIAL_BEHAVIORAL: sgl.Trial.BehavioralResponse(),
    DBTable.TRIAL_NEURONAL: sgl.Trial.NeuronalResponse(),
    DBTable.DATA_DOWNLOAD: sgl.DataDownload()
}
""" Maps enumerated database table ID to the corresponding DataJoint table class in the Lisberger lab schema. """


def database_empty() -> Optional[str]:
    """
    Utility method verifies whether or not all tables in the Lisberger lab database are empty.

    Returns:
        None if database is empty; else an error message indicating first table found that is not empty.
    """
    for table_id in _table_map.keys():
        if len(_table_map[table_id]) > 0:
            return f"Found non-empty database table: {str(table_id)}"
    return None


def num_table_rows(table_id: DBTable,
                   restriction: Optional[List[Union[str, Dict[str, AttributeValue]]]] = None) -> int:
    """
    Get the current number of entities in the specified database table.

    Args:
        table_id: ID of database table.
        restriction: If not None, then this argument specifies a list of restriction conditions; each condition is
            specified either as a dictionary of attribute ID-value pairs, or as a string (see DataJoint docs). The
            result reflects the number of rows in the table that satisfy ALL conditions. The restriction conditions
            are not checked for validity.
    Returns:
        Number of rows in the table that satisfy the conditions specified (if any). Returns 0 if unable to access
            database, if specified table does not exist, or if a restriction condition is specified that includes an
            attribute not defined on the table (or if the table has no rows satisfying that condition).
    """
    try:
        table: dj.Table = _table_map[table_id]
        query = (table & dj.AndList(restriction)) if isinstance(restriction, list) else table
        n = len(query)
    except Exception as e:
        n = 0
        get_application_logger().error(str(e), exc_info=True)
    return n


def attribute_exists(table_id: DBTable, attr_id: str, attr_value: AttributeValue) -> bool:
    """
    Does the specified attribute have the specified value in any row in the specified table?
    Args:
        table_id: ID of database table.
        attr_id: Attribute ID.
        attr_value: Attribute value.

    Returns:
        False if attribute value not found in table (or if an error occurs).
    """
    existing_values = fetch_attribute_values(table_id, attr_id)
    return attr_value in existing_values


def fetch_attribute_values(table_id: DBTable, attr_id: str,
                           restriction: Optional[Dict[str, AttributeValue]] = None) -> List[AttributeValue]:
    """
    Fetch all existing values of the specified attribute within the specified database table, optionally restricted
    to a defined subset of the table's rows.

    Args:
        table_id: ID of database table
        attr_id:  Attribute ID.
        restriction: The attribute ID-value pairs in this dictionary define a restricted subset of rows within the
            table from which to retrieve the attribute's value. Default value is None -- in which case the attribute
            value is retrieved for every row in the table.

    Returns:
        List of values found for the specified attribute (one value per row in the table, or table subset). The list
            is not sorted. Returns an empty list if specified table does not exist or if a database error occurs.
    """
    try:
        table: dj.Table = _table_map[table_id]
        _restriction = None if not restriction else \
            {attr: restriction[attr] for attr in attributes_of(table_id, False) if attr in restriction}
        query = (table & _restriction) if _restriction else table
        attr_values = list(query.fetch(attr_id))
    except Exception as e:
        attr_values = []
        get_application_logger().error(str(e), exc_info=True)
    return attr_values


def foreign_key_choices(table_id: DBTable, fkey_id: str) -> List[Tuple[str, AttributeValue]]:
    """
    Retrieve the list of available choices for the specified foreign key attribute in the specified table. To
    support using this list in a user-facing dropdown or list widget, each "choice" is represented by 2-tuple
    (label, fkey_value), where fkey_value is the actual value of the foreign key and label is a unique user-facing
    string identifying that value.

    Args:
        table_id: ID of database table.
        fkey_id: ID of foreign key attribute.

    Returns:
        List of all available value choices for the foreign key table attribute, with companion label as described.
            Sorted alphabetically by the label. Returns an empty list if unable to access the database.

    Raises:
        KeyError: If table_id is invalid, if fkey_id is invalid or is not a foreign key attribute.
    """
    attr_info = attribute_info(table_id, fkey_id)
    if attr_info.type != AttrTypeEnum.FKEY:
        raise KeyError(f"{fkey_id} is not a foreign key attribute!")

    # for select parent tables, we sort on a user-facing attribute rather than the primary key
    if attr_info.fkey_table == DBTable.STUDY:
        studies = sorted(fetch_restrict_proj([DBTable.STUDY], None, ['study_id', 'study_title']),
                         key=lambda study: study['study_title'])
        return [] if len(studies) == 0 else [(study['study_title'], study['study_id']) for study in studies]
    elif attr_info.fkey_table == DBTable.BRAIN_AREA:
        regions = sorted(fetch_restrict_proj([DBTable.BRAIN_AREA], None, ['ba_id', 'ba_name']),
                         key=lambda region: region['ba_name'])
        return [] if len(regions) == 0 else [(region['ba_name'], region['ba_id']) for region in regions]
    elif attr_info.fkey_table == DBTable.NEURON_TYPE:
        n_types = sorted(fetch_restrict_proj([DBTable.NEURON_TYPE], None, ['nt_id', 'nt_name']),
                         key=lambda n_type: n_type['nt_name'])
        return [] if len(n_types) == 0 else [(n_type['ba_name'], n_type['ba_id']) for n_type in n_types]

    fkey_values = sorted(fetch_attribute_values(attr_info.fkey_table, attr_info.fkey_id))
    return [(v, v) for v in fkey_values]


def fetch_rows(table_id: DBTable, restriction: Optional[Dict[str, AttributeValue]] = None) -> \
        Optional[List[Dict[str, AttributeValue]]]:
    """
    Fetch all or a subset of the rows in the specified database table.

    Args:
        table_id: ID of the database table.
        restriction: The attribute ID-value pairs in this dictionary specify a restriction that any row returned in
            the result must satisfy. Default value is None -- thereby retrieving all rows in the table.

    Returns:
        The requested rows. Each element in the list is a dictionary of attribute ID-value pairs representing a
            single table row. Returns an empty list if the table is empty, or has no rows satisfying the restriction
            (if any). Returns None if a database error occurs.
    """
    try:
        table: dj.Table = _table_map[table_id]
        query = (table & restriction) if restriction else table
        rows = query.fetch(as_dict=True)
    except Exception as e:
        rows = None
        get_application_logger().error(str(e), exc_info=True)
    return rows


def fetch_one_row(table_id: DBTable, pk: Dict[str, AttributeValue]) -> Optional[Dict[str, AttributeValue]]:
    """
    Fetch exactly one row from the specified database table.

    Args:
        table_id: ID of the database table.
        pk: The attribute ID-value pairs in this dictionary specifying the primary key of the desired row returned in
            the result must satisfy.

    Returns:
        A dictionary of the attribute ID-value pairs representing a single table row. Returns None if the row specified
            bu the primary key does not exist, or a database error occurs.
    Raises:
        KeyError: If the supplied primary key is not complete.
    """
    table_pk = primary_key_of(table_id, False)
    restriction = {key: pk[key] for key in table_pk}   # will throw KeyError if PK is incomplete
    try:
        row = (_table_map[table_id] & restriction).fetch1()
    except Exception as e:
        row = None
        get_application_logger().error(str(e), exc_info=True)
    return row


def fetch_any_proj(table_id: DBTable, conditions: Optional[List[str]] = None, attributes: Optional[List[str]] = None) \
        -> Optional[List[Dict[str, AttributeValue]]]:
    """
    Fetch selected attributes from some or all rows of the specified database table.

    Args:
        table_id: ID of the database table.
        conditions: Optional restriction on the set of table rows returned, expressed as a list of string conditions.
            They are not checked for validity. The result will include all rows that meet ANY of the conditions (unlike
            fetch_restrict_proj()). If None or empty list, all table rows are fetched.
        attributes: List of attribute IDs identifying the subset of secondary attributes to fetch from the specified
            table. The primary-key attributes of the table are always be included in the result. If this argument is
            None, no secondary attributes are included; if it is an empty list, then ALL secondary attributes are
            included; otherwise, valid secondary attribute IDs in the list are included. Default = None.
    Returns:
        List of all table rows satisyfing ANY of the string conditions specified. Each element in the list is a
            dictionary of attribute ID-value pairs including the primary key attributes, plus a subset of the secondary
            attributes. Returns None if a database error occurs.
    """
    try:
        table: dj.Table = _table_map[table_id]
        query = (table & conditions) if conditions else table
        if isinstance(attributes, list):
            final_query = query.proj(...) if (len(attributes) == 0) else query.proj(*attributes)
        else:
            final_query = query.proj()
        rows = final_query.fetch(as_dict=True)
    except Exception as e:
        rows = None
        get_application_logger().error(str(e), exc_info=True)
    return rows


def fetch_restrict_proj(
        table_ids: List[DBTable], restrict: Optional[List[Union[None, List[str], Dict[str, AttributeValue]]]] = None,
        attributes: Optional[List[str]] = None) -> Optional[List[Dict[str, AttributeValue]]]:
    """
    Compose a restriction query that selects a subset of rows from a database table and returns all attributes for each
    row, only the primary key attributes, or the primary key plus selected secondary attributes.

    The restriction query can involve up to 3 different tables in the database. A restriction condition may be applied
    to each of the tables. In its most general form: query = (T1 & R1) & ((T2 & R2) & (T3 & R3)). The query selects all
    rows in T1 that satisfy R1 and have matching rows in ((T2 & R2) & (T3 & R3)) -- ie, all the rows in T2 that satisfy
    R2 and have matching rows in T3 that satisfy R3.

    You can specify a variety of restriction queries depending on how many tables and restriction conditions are
    specified, such as: T1, (T1 & R1), T1 & (T2 & R2), (T1 & R1) & T2. However, the rows selected will always be from
    T1, the first table identified in 'table_ids' -- order matters in a compound restriction query!

    Use the 'attributes' argument to tailor which attributes (aka columns) are actually retrieved.

    Args:
        table_ids: The list of IDs identifying the database tables in the restriction query. Must contain at least one
            and at most 3 distinct table IDs. The tables must be join-compatible -- i.e., all attributes common to the
            tables (same name) must be part of the primary key or a foreign key and must be of compatible datatype for
            equality comparisons.
        restrict: A list containing the restriction condition to apply to each identified table. A restriction condition
            is specified either as a dictionary of attribute ID-value pairs (Dict[str, AttributeValue]) or as a list of
            string conditions (List[str]). The restrictions are not checked for validity. If a table should not be
            restricted, the corresponding restriction should be set to None. If this argument itself is None, then none
            of the tables are subsetted. If it is shorter, the remaining table are not subsetted. Default = None.
        attributes: List of attribute IDs identifying the subset of secondary attributes to fetch from the FIRST data
            table. The primary-key attributes of the first table will be always be included in the result. If this
            argument is None, no secondary attributes are included; if it is an empty list, then ALL secondary
            attributes are included; otherwise, valid secondary attribute IDs in the list are included. Default = None.

    Returns:
        A list of rows in the first database table that satisfy the defined restriction query. Each row is a dictionary
            that all or a subset of the attributes of that table (all primary key attributes are always included).
            Remember that this is NOT a join; the result rows do not include any attributes from the other database
            table(s), if any -- except those shared with the first. Returns an empty list if any restriction results in
            an empty set. Returns None if a databse error occurs.
    """
    if (len(table_ids) < 1) or (len(table_ids) > 3):
        raise ValueError("Invalid arg 'table_ids' - Must specify at least one and no more than 3 tables.")
    if restrict is None:
        restrict = [None] * len(table_ids)
    else:
        while len(restrict) < len(table_ids):
            restrict.append(None)
    try:
        query: Optional[QueryExpression] = None
        for i, tid in enumerate(table_ids):
            table = _table_map[tid]
            cond = restrict[i]
            query_part = table
            if isinstance(cond, dict):
                query_part = query_part & cond
            elif isinstance(cond, list):
                query_part = query_part & dj.AndList(cond)
            query = query_part if (query is None) else (query & query_part)
        if isinstance(attributes, list):
            final_query = query.proj(...) if (len(attributes) == 0) else query.proj(*attributes)
        else:
            final_query = query.proj()
        rows = final_query.fetch(as_dict=True)
    except Exception as e:
        rows = None
        get_application_logger().error(str(e), exc_info=True)
    return rows


def insert_into_table(table_id: DBTable, row: Dict[str, AttributeValue], log: bool = True) -> Optional[str]:
    """
    Insert an entry into a specified table in the laboratory database, and log the change in the repository
    database updates log. If the log update fails after insertion, the insertion is rolled back to maintain
    consistency between the database and the backup repository.

    Args:
        table_id: ID of database table.
        row: The new entry.
        log: If True, the insertion is logged in the backup updates log. Default = True.

    Returns:
        None if successful; else a user-facing description of the error (missing attribute, bad attribute value,
        entry already exists, database error).
    """
    if not (table_id in _table_map):
        error_msg = f"Unrecognized database table ID: {str(table_id)}"
        get_application_logger().error(error_msg)
        return error_msg

    err_msg = check_row(table_id, row)
    if err_msg is not None:
        get_application_logger().error(err_msg)
        return err_msg
    try:
        table: dj.Table = _table_map[table_id]
        with table.connection.transaction:
            table.insert1(row, replace=False)
            if log:
                err_msg = log_add_table_row(table_id, row)
                if err_msg:
                    raise Exception(err_msg)
    except Exception as e:
        err_msg = f"Insert failed: table={str(table_id)}, value={row} ===> {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
    return err_msg


def update_table_row(table_id: DBTable, row: Dict[str, AttributeValue], log: bool = True) -> Optional[str]:
    """
    Update a single row in the specified table of the lab database. Only secondary attributes may be updated with
    this method, as updating a primary key attribute could destroy database integrity.

    Args:
        table_id: ID of database table
        row: A dictionary of attribute ID-value pairs that identifies the row to update; additional key-value
            pairs identify the secondary attribute values to be updated and their new assigned values.
        log: If True, the row update is logged in the backup updates log. Default = True.

    Returns:
        None if successful; else a user-facing description of the error (row does not exist, bad attribute value,
            database error).
    """
    if not (table_id in _table_map):
        err_msg = f"Unrecognized database table ID: {str(table_id)}"
        get_application_logger().error(err_msg)
        return err_msg

    err_msg = None
    try:
        _validate_update_row(table_id, row)
        table: dj.Table = _table_map[table_id]
        with table.connection.transaction:
            table.update1(row)
            if log:
                err_msg = log_update_table_row(table_id, row)
                if err_msg:
                    raise Exception(err_msg)
    except (Exception, ValueError) as e:
        err_msg = f"Row update failed: table={str(table_id)} ===> {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
    return err_msg


def delete_from_table(table_id: DBTable, row_pk: Dict[str, AttributeValue], log: bool = True) -> Optional[str]:
    """
    Delete a single row from a specified table in the laboratory database, and log the change in the repository
    database updates log. If the log update fails after deletion, the deletion is rolled back to maintain
    consistency between the database and the backup repository.

    In DataJoint, deletion of a table row will, in turn, delete any rows in other database tables that are
    dependent on the deleted entity through a foreign key relationship. We never want this to happen, so any
    deletion which would cause other table deletions is forbidden. A practical example of this would be removing a
    lab portal user who is no longer active but has committed many experiment sessions to the database. All of those
    sessions (including all the trial datasets) would be removed if that user was removed.

    Args:
        table_id: ID of database table
        row_pk: This dictionary must contain, at a minimum, the primary key attribute ID-value pairs that uniquely
            identify a single table row. Any other attributes are ignored.
        log: If True, the deletion is logged in the backup updates log. Default = True.

    Returns:
        None if successful; else a user-facing description of the error (bad table or attribute ID, database error).
    """
    err_msg = None
    if not (table_id in _table_map):
        err_msg = f"Unrecognized database table ID: {str(table_id)}"
    elif not (table_id.allow_delete()):
        err_msg = f"User-initiated deletions from this table are not permitted: {str(table_id)}"
    if err_msg is not None:
        get_application_logger().error(err_msg)
        return err_msg

    try:
        table: dj.Table = _table_map[table_id]
        table_pk = primary_key_of(table_id, False)
        restriction = {key: row_pk[key] for key in table_pk}
        if not _check_row_deletion(table_id, restriction):
            raise Exception("Row deletion would trigger forbidden cascade deletes in database")
        with table.connection.transaction:
            (table & restriction).delete()
            if log:
                err_msg = log_delete_from_table(table_id, restriction)
                if err_msg:
                    raise Exception(err_msg)
    except KeyError:
        err_msg = f"Delete failed: table={str(table_id)} ===> Incomplete primary key"
        get_application_logger().error(err_msg, exc_info=True)
    except Exception as e:
        err_msg = f"Delete failed: table={str(table_id)} ===> {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
    return err_msg


def _check_row_deletion(table_id: DBTable, row_pk: Dict[str, AttributeValue]) -> bool:
    """
    Helper method for delete_from_table(). It checks whether or not the row deletion would result in a cascade
    deletion in other tables within the lab database. The following foreign-key relations are checked for possible
    cascade deletions. If a relation exists, the deletion is forbidden.
        User, Subject, Rig, Study -> Session.
        User -> Study.
        User -> DataDownload.
        BrainArea -> Session.EPhys
        NeuronType -> Session.Neuron
    Some foreign-key relations are not checked because the cascade deletions are permssible, or because deletions
    in the independent table are not allowed: Subject -> SubjectImplant; Study, Publication -> StudyPub; Session,
    TrialProtocol -> Trial; Session.Neuron -> Trial.NeuronalResponse; Session -> DataDownload.

    Args:
        table_id: ID of the database table.
        row_pk: The primary key of the table row to be deleted
    Returns:
        True if row deletion is safe; False if it would trigger a forbidden cascade deletion elsewhere in database.
    """
    # we have to be careful here because a number of primary keys are renamed when used as foreign keys!
    try:
        if table_id == DBTable.USER:
            if len(_table_map[DBTable.SESSION] & {'experimenter': row_pk['username']}) > 0:
                return False
            elif len(_table_map[DBTable.STUDY] & {'study_lead': row_pk['username']}) > 0:
                return False
            elif len(_table_map[DBTable.DATA_DOWNLOAD] & {'requester': row_pk['username']}) > 0:
                return False
        elif table_id in [DBTable.SUBJECT, DBTable.RIG, DBTable.STUDY]:
            if len(_table_map[DBTable.SESSION] & row_pk) > 0:
                return False
        elif table_id == DBTable.BRAIN_AREA:
            if len(_table_map[DBTable.SESSION_EPHYS] & row_pk) > 0:
                return False
        elif table_id == DBTable.NEURON_TYPE:
            if len(_table_map[DBTable.SESSION_NEURON] & {'unit_type': row_pk['nt_id']}) > 0:
                return False
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return False
    return True


def row_exists(table_id: DBTable, row_pk: Dict[str, AttributeValue]) -> bool:
    """
    Does the specified entry/row currently exist in the specified table?

    Args:
        table_id: ID of the database table.
        row_pk: This dictionary must contain, at a minimum, the primary key attribute ID-value pairs that uniquely
            identify a single table row. Any other attributes are ignored!

    Returns:
        True if row exists, false otherwise.

    Raises:
        ValueError: If row_pk is missing any of the table's primary key attributes.
    """
    table_pk = primary_key_of(table_id, False)
    try:
        restriction = {key: row_pk[key] for key in table_pk}
        exists = (num_table_rows(table_id, [restriction]) == 1)
    except KeyError as e:
        get_application_logger().error(str(e), exc_info=True)
        raise ValueError("Incomplete primary key")
    return exists


def check_row(table_id: DBTable, row: Dict[str, AttributeValue], omit_master: bool = False) -> Optional[str]:
    """
    Check whether or not the proposed row entry is valid and does not yet exist in the specified database table.

    Args:
        table_id: ID of the database table.
        row: The proposed entry. It must contain a valid attribute value for each table attribute -- except for an
            auto-incrementing primary key, and it must not yet exist in the database. SIDE EFFECT: If the entry
            includes a value for the auto primary key, that key-value pair is removed from the dictionary.
        omit_master: If True and this is a part table, attributes in 'row' that are part of the master table's
            primary key are NOT checked, and existence is not checked. This is a way to check a new entry in the
            part table without first inserting the corresponding entry in the master table. Default is False.
    Returns:
        None if operation succeeds, else a user-facing description of the error (bad table ID, missing attribute,
            bad attribute value, entry already exists, database error).
    """
    err_msg = None
    try:
        _validate_row(table_id, row, omit_master)
    except (Exception, ValueError) as err:
        err_msg = f"Invalid entry: {str(err)}"
    return err_msg


def _validate_row(table_id: DBTable, row: Dict[str, AttributeValue], omit_master: bool = False) -> None:
    """
    Validate an entry (aka, row) that is to be inserted into the database table specified.

    Args:
        table_id: ID of the database table.
        row: The new entry. NOTE: If the table uses an auto-incrementing attribute as its primary key, that
            attribute is removed from the entry, if specified. Its value is set by the database on insert.
        omit_master: If True and the specified table is a part table, attributes in 'row' that are part of the
            parent table's primary key are NOT checked, and existence is not checked. This is a way to check a new
            entry in the part table without first inserting the corresponding entry in the master table. Default is
            False.

    Raises:
        Exception: If the proposed entry already exists in table, or if entry is missing any attribute value.
        ValueError: If any attribute value is invalid.
    """
    # we never check existence when the table uses an auto-incrementing PK!
    if not (has_auto_primary_key(table_id) or omit_master):
        if row_exists(table_id, row):
            raise Exception("Attempt to add a new entry with an existing primary key")
    for attr_id in attributes_of(table_id, omit_master):
        attr_info = attribute_info(table_id, attr_id)
        if attr_info.type != AttrTypeEnum.AUTO:
            _validate_attribute_value(table_id, attr_id, row[attr_id] if attr_id in row else None)
        elif attr_id in row:
            row.pop(attr_id, None)


def _validate_update_row(table_id: DBTable, row: Dict[str, AttributeValue]) -> None:
    """
    Validate the values of any non-primary attributes in an existing row that is to be updated in place in the
    specified database table. No primary key values are checked -- since primary key attributes may not be updated
    in place. Also, existence of the row is not checked, since any attempt to update a non-existent table row will
    obviously fail.

    Args:
        table_id: ID of the database table.
        row: A dictionary containing one or more non-primary attribute-value pairs to be validated. It need not
            include every non-primary attribute, and any dictionary keys that do not correspond to a table attribute
            are simply ignored.

    Raises:
        ValueError: If 'row' contains an invalid value for any non-primary attribute of the table specified.
    """
    for attr_id in attributes_of(table_id):
        attr_info = attribute_info(table_id, attr_id)
        if (not attr_info.pkey) and (attr_id in row):
            _validate_attribute_value(table_id, attr_id, row[attr_id])


def _validate_attribute_value(table_id: DBTable, attr_id: str, attr_value: Optional[AttributeValue]) -> None:
    """
    Validate the proposed value for an attribute in the underlying table.

    Validation of the attribute value depends on the attribute type, AttrTypeEnum:
        TEXT: The value must satisfy any regular expression defined for the attribute (if any), as well as the
            min/max restriction on text length.
        FLOAT: Can be str, int or float, but a string value must be parsable as a float. If string, it must
            satisfy min/max restriction on text length.
        INT: Can be str or int, but a string value must be parsable as an integer. If string, it must satisfy
            min/max restriction on text length.
        DATE: Can be a date or string. A string value must satisfy the format 'YYYY-MM-DD'. The date must be
            after 12/31/1899 and before today.
        BOOL: Can be a number, or boolean. A non-zero number is considered True.
        ENUM: Value must be one of the valid options for the attribute.
        BLOB: The value must be a Numpy array or a bytes array. Its value is not otherwise checked.
        FKEY: The attribute value must identify an existing entity in the parent table.
        AUTO: An auto-incrementing PK. This type of attribute is ignored. Its value is set by the database on
            insert, NOT by the user.
        PWD: Special attribute referring to the user's encrypted password in the User table. The password has to
            be validated prior to encryption. This is NEVER checked.
        TIME: Timestamp attributes are never set manually by user, so they are not checked.

    Args:
        table_id: ID of the database table.
        attr_id: The attribute ID.
        attr_value: The proposed value for the attribute. Could be None for nullable attributes.

    Raises:
        ValueError: If the proposed attribute value is not valid in any way. The error description is intended to
        provide a user-facing description of the problem.
    """
    attr_info = attribute_info(table_id, attr_id)
    if attr_info.type in [AttrTypeEnum.AUTO, AttrTypeEnum.PWD, AttrTypeEnum.TIME]:
        return
    if attr_value is None:
        if attr_info.nullable:
            return
        else:
            raise ValueError(f"Missing attribute: {attr_info.label}")
    if not isinstance(attr_value, (str, bool, int, float, date, np.ndarray, bytes)):
        raise ValueError(f"Attribute value is an unsupported data type: '{attr_info.label}'")
    if attr_info.pkey:
        if isinstance(attr_value, str) and (attr_value == ""):
            raise ValueError(f"Missing value for primary key attribute: '{attr_info.label}'")
    if attr_info.type == AttrTypeEnum.FKEY:
        if not attribute_exists(attr_info.fkey_table, attr_info.fkey_id, attr_value):
            raise ValueError(f"Missing foreign key: '{attr_info.label}' = '{str(attr_value)}'")
    elif attr_info.type == AttrTypeEnum.ENUM:
        if not (attr_value in attr_info.options):
            raise ValueError(f"Invalid option for '{attr_info.label}': '{str(attr_value)}'")
    elif attr_info.type == AttrTypeEnum.DATE:
        if not check_date(attr_value):
            raise ValueError(f"'{attr_info.label}': Date is invalid, earlier than 1900-01-01, or in the future.")
    elif attr_info.type == AttrTypeEnum.FLOAT:
        if isinstance(attr_value, str) and (attr_info.textrange is not None):
            min_len, max_len = attr_info.textrange
            if (len(attr_value) < min_len) | (len(attr_value) > max_len):
                raise ValueError(f"'{attr_info.label}': Value must be {min_len}-{max_len} characters long.")
        try:
            num_value = float(attr_value)
        except(TypeError, ValueError):
            raise ValueError(f"'{attr_info.label}' = '{attr_value}' cannot be parsed as a floating-point value")
        validate_numeric_attribute_value(table_id, attr_id, num_value)
    elif attr_info.type == AttrTypeEnum.INT:
        if isinstance(attr_value, str) and (attr_info.textrange is not None):
            min_len, max_len = attr_info.textrange
            if (len(attr_value) < min_len) | (len(attr_value) > max_len):
                raise ValueError(f"'{attr_info.label}': Value must be {min_len}-{max_len} characters long.")
        try:
            num_value = int(attr_value)
        except(TypeError, ValueError):
            raise ValueError(f"'{attr_info.label}' = '{attr_value}' cannot be parsed as an integer")
        validate_numeric_attribute_value(table_id, attr_id, num_value)
    elif attr_info.type == AttrTypeEnum.BOOL:
        if not isinstance(attr_value, (int, float, bool)):
            raise ValueError(f"'{attr_info.label}' must be a number or boolean value")
    elif attr_info.type == AttrTypeEnum.BLOB:
        if not isinstance(attr_value, (np.ndarray, bytes)):
            raise ValueError(f"'{attr_info.label}' must be a Numpy array or byte string")
    else:  # 'text' or 'email'
        if not isinstance(attr_value, str):
            raise ValueError(f"'{attr_info.label}': Value must be a string")
        if attr_info.textrange:
            min_len, max_len = attr_info.textrange
            if (len(attr_value) < min_len) | (len(attr_value) > max_len):
                raise ValueError(f"'{attr_info.label}': Value must be {min_len}-{max_len} characters long.")
        if attr_info.regex:
            if re.fullmatch(attr_info.regex, attr_value) is None:
                raise ValueError(f"Invalid value for {attr_info.label}: {attr_info.regex_hint}")


def update_mapping_table(map_table_id: DBTable, src_pk_val: int, map_set: Set[int], log: bool = True) -> Optional[str]:
    """
    Update an associative mapping stored in a cross-reference table in the laboratory database, and log the change
    in the repository database updates log. If the log update fails, any changes are rolled back to maintain
    consistency between the database and the backup repository.

    Args:
        map_table_id: ID of the cross-reference table.
        src_pk_val: Integer value identifying a row in the source table for the cross-reference. Must exist in
            database or operation will fail.
        map_set: Set of integer primary key values identifying all rows in the destination table that map to the
            specified source entity. All must exist in the destination table or the operation will fail.
        log: If True, the change is logged in the backup updates log. Default = True.

    Returns:
        None if successful; else a user-facing description of the error.
    """
    error_msg = None
    try:
        if not (map_table_id in _table_map):
            raise Exception(f"Unrecognized database table ID: {str(map_table_id)}")
        if not map_table_id.is_mapping_table():
            raise Exception(f"Table is not a cross-reference table!: {str(map_table_id)}")
        map_table: dj.Table = _table_map[map_table_id]
        src_pk = map_table_id.source_key_for_mapping_table()
        dst_pk = map_table_id.destination_key_for_mapping_table()
        xref_rows = [{src_pk: src_pk_val, dst_pk: value} for value in map_set]
        with map_table.connection.transaction:
            (map_table & {src_pk: src_pk_val}).delete()
            if len(xref_rows) > 0:
                map_table.insert(xref_rows)
            if log:
                err_msg = log_mapping_table_update(map_table_id, src_pk_val, map_set)
                if err_msg:
                    raise Exception(err_msg)
    except Exception as err:
        error_msg = f"Failed to update cross-reference table {str(map_table_id)}: {str(err)}"
        get_application_logger().error(error_msg, exc_info=True)
    return error_msg


class SessionCommitter(sgl.TrialProducer):
    """
    This mixin class implements the various DataJoint table inserts required to commit a single experiment session to
    the lab database. It does NOT provide the actual session data to be inserted -- the relevant methods must be
    implemented by a derived class.

    Note that SessionCommitter derives from sgl.TrialProducer, which defines a method that is invoked during the
    Trial table's auto-populate cycle. In that method, all trial response data must be inserted into the Trial table
    and its part tables.

    A long experiment recording from multiple neural units could easily take many minutes to commit to the database, so
    this "interface" includes a method to post regular progress updates and also check whether or not the commit should
    be cancelled.

    The main purpose of this class is to hide the DataJoint details from other modules.
    """
    def session_table_entry(self) -> Dict[str, AttributeValue]:
        """
        The entry to be inserted into the Session table. Note that the 'committed' attribute will be replaced by
        the timestamp when the Session entry is inserted into the database.
        """
        raise NotImplementedError()

    def ephys_table_entry(self) -> Optional[Dict[str, AttributeValue]]:
        """
        The entry to be inserted into the Session.EPhys part table. If the experiment session is behavioral only,
        returns None.
        """
        raise NotImplementedError()

    def neurons(self) -> List[Dict[str, AttributeValue]]:
        """
        The entry(ies) to be inserted into the Session.Neuron part table, one for each neural unit recorded during the
        experiment session. For behavior-only experiment sessions, returns an empty list.
        """
        raise NotImplementedError()

    def trial_protocols(self) -> List[Dict[str, AttributeValue]]:
        """
        The entries to be inserted into the TrialProtocol table, one for each trial protocol presented during the
        experiment session that is not already stored in the database.
        """
        raise NotImplementedError()

    def update_progress(self, msg: str) -> None:
        """
        Post a progress message for the database commit operation in progress, and check whether or not the operation
        should be cancelled.

        Args:
            msg: The progress message to be posted

        Raises:
            Exception: If an error occurs while posting the progress message, OR if a cancel request was detected. In
                either case, the exception message should describe the reason why the commit operation was aborted.
        """
        raise NotImplementedError()

    def was_cancelled(self) -> bool:
        """ Returns True only if the commit failed because the operation was cancelled. """
        raise NotImplementedError()

    def commit(self) -> Optional[str]:
        """
        Commit the data for a single experiment session -- as encapsulated in this object -- into the portal database.

        All database table insertions are wrapped in a transaction to ensure that, if any insertion fails, the database
        is rolled back to a consistent state. Since it can take a significant amount of time to insert all trial
        response data for the session, progress messages are delivered periodically via update_progress(), which also
        checks whether or not the commit should be cancelled. The transaction will also be rolled back in the event
        that a cancel request is detected.

        Returns:
            None if session commit succeeded; else a brief error description.
        """
        phase1_committed = False
        try:
            session_table = sgl.Session()
            with session_table.connection.transaction:
                session_entry = self.session_table_entry()
                session_entry['committed'] = datetime.now().isoformat(sep=' ', timespec='seconds')
                session_table.insert1(session_entry, replace=False)
                ephys_entry = self.ephys_table_entry()
                if ephys_entry:
                    sgl.Session.EPhys().insert1(ephys_entry, replace=False)
                neuron_entries = self.neurons()
                if len(neuron_entries) > 0:
                    sgl.Session.Neuron().insert(neuron_entries, replace=False)
                proto_entries = self.trial_protocols()
                if len(proto_entries) > 0:
                    sgl.TrialProtocol().insert(proto_entries, replace=False)

            phase1_committed = True
            self.update_progress("Inserted session entry and any new trial protocols into database...")

            # because the populate() method starts a new transaction, we cannot already be in one -- so we have to
            # do the session commit in two chunks
            trial_table = sgl.Trial()
            trial_table.set_trial_producer(self)
            trial_table.populate()
            trial_table.set_trial_producer(None)
        except Exception as e:
            try:
                sgl.Trial().set_trial_producer(None)
                if phase1_committed:
                    (sgl.Session() & self.session_table_entry()).delete()
                    restriction = [f"proto_hash = '{p['proto_hash']}'" for p in self.trial_protocols()]
                    (sgl.TrialProtocol() & restriction).delete()
            except Exception as e2:
                get_application_logger().error(
                    f"Exception ({str(e2)}) occurred while rolling back after an"
                    f"aborted session commit. Database may be left in an inconsistent state!"
                )
                pass
            get_application_logger().error(f"Session commit to database failed: {str(e)}", exc_info=True)
            return f"Error during session commit: {str(e)}"

        return None

    @staticmethod
    def batch_insert_trials(
            trials: List[Dict[str, AttributeValue]], behavioral_entries: List[Dict[str, AttributeValue]],
            neuronal_entries: List[Dict[str, AttributeValue]], events: List[Dict[str, AttributeValue]]) -> None:
        """
        Batch-insert trial response data during a session commit. This helper method is called during a session
        commit to insert trial information and response data into the database's Trial table and its part tables.
        It should only be invoked within the auto-populate cyle for the Trial table, which is the most time-comsuming
        portion of the session commit operation.

        Args:
            trials: The list of trials to be inserted.
            behavioral_entries: The list of behavioral responses to be inserted.
            neuronal_entries: THe list of neuronal response to be inserted.
            events: The list of TTL event timestamps to be inserted
        """
        trial_table = sgl.Trial()
        behavioral_table = sgl.Trial.BehavioralResponse()
        neuronal_table = sgl.Trial.NeuronalResponse()
        event_table = sgl.Trial.Event()

        # NOTE: We don't use a database transaction here b/c this method is only called within a DataJoint populate()
        # call, which already has started a transaction...
        trial_table.insert(trials, replace=False)
        if len(behavioral_entries) > 0:
            behavioral_table.insert(behavioral_entries, replace=False)
        if len(neuronal_entries) > 0:
            neuronal_table.insert(neuronal_entries, replace=False)
        if len(events) > 0:
            event_table.insert(events, replace=False)


def rollback_session_commit(session_pk: Dict[str, AttributeValue], proto_hashes: List[str]) -> Optional[str]:
    """
    After an experiment session is committed to the database, the session data archive and preprocessing results file
    are moved to the experimenter's folder in the portal file repository and the commit is logged in the database update
    log. However, if any of those operations fail, we need to be able to rollback the changes to the database.

    Deleting the session table entry itself will automatically remove all the trial data, neural unit metrics, and
    electrophysiology metadata. Any added trial protocols must be removed separately.

    The rollback is not logged in the database update log, since we're undoing a failed session commit. If the rollback
    fails, the database may be left in an inconsistent state.

    Args:
        session_pk: This dictionary must contain, at least, the full primary key of the session to remove. Any
            additional attributes in the dictionary are ignored.
        proto_hashes: List of primary keys (MD5 digest hashes, key 'proto_hash') identifying any trial protocols that
            were added when the session was committed.
    Returns:
        None if both the session and any identified trial protocols were successfully deleted; else a brief error
            description.
    Raises:
        KeyError: if session primary key is incomplete
    """
    session_table = _table_map[DBTable.SESSION]
    proto_table = _table_map[DBTable.TRIAL_PROTOCOL]
    session_restriction = {key: session_pk[key] for key in primary_key_of(DBTable.SESSION)}
    proto_restriction = [f"proto_hash = '{h}'" for h in proto_hashes]
    try:
        with session_table.connection.transaction:
            (session_table & session_restriction).delete()
            if len(proto_restriction) > 0:
                (proto_table & proto_restriction).delete()
    except Exception as e:
        err_msg = f"Session rollback failed for session PK {session_pk} ===> {str(e)}"
        get_application_logger().error(err_msg, exc_info=True)
        return f"Rollback failed: {str(e)} - database may be left in an inconsistent state!"
    return None
