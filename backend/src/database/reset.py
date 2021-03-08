"""
reset.py: A script that resets the Lisberger lab database and reseeds it with some manual table entries.

This script is primarily for use during development of the database and web portal. Specifically, whenever the database
schema defined in sgl_schema.py changes, we really need to drop the entire database and start over. That is the sole
purpose of this script.

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.reset'. Once the script completes, resume the normal
backend service with 'docker-compose restart backend'.

NOTE: The main method dynamically imports the database schema in 'sgl_schema.py' after dropping the schema from the
database. The schema is declared on the database the FIRST time the sgl_schema module is imported in a running
python shell. If we imported sgl_schema in the normal manner (with the import statement), the import would happen
before the schema was dropped, and so the schema would not get declared on the database.

@author: sruffner
@created: 04mar2021
"""
import importlib
import os
import time
from functools import partial
from json import JSONDecoder
from typing import Optional, Any

import datajoint as dj
from .manager import DataBaseManager


def seed_database(sgl: Any) -> None:
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

    THIS METHOD IS INTENDED ONLY FOR USE DURING DEVELOPMENT, so that we can populate some the manual tables with some
    entries after dropping and recreating the database.

    The entries listed in the seed file are assumed to be presented in a valid order. For example, a subject is added
    to the Subject table before any implants for that subject are added to the SubjectImplant table. Also, each entry's
    attribute name-value pairs are assumed to be valid. If not, the add operation may fail. An error message is
    printed if any insertion fails, and a summary message is printed indicating how many entries were successfully
    inserted into the database.

    Args:
        sgl: The 'sgl_schema' module object, dynamically imported.
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
    database_manager = DataBaseManager()
    try:
        with open('./assets/seed_data.txt', 'r') as file_obj:
            for add_dict in json_parse(file_obj, json_decoder):
                n_parsed += 1
                if isinstance(add_dict, dict) and ("table" in add_dict) and ("entry" in add_dict)\
                        and (add_dict["table"] in name_to_table):
                    try:
                        name_to_table[add_dict["table"]].insert1(add_dict["entry"], replace=False)
                        msg = database_manager.log_add_table_row(add_dict["table"], add_dict["entry"])
                        if msg:
                            print(f"   Backup log error: {str(msg)}", flush=True)
                        n_added += 1
                    except Exception as e:
                        print(f"    Insert into {add_dict['table']} failed: {str(e)}", flush=True)
    except Exception as e:
        print(f"====> Unexpected error while seeding database: {e}")

    print(f"Processed {n_parsed} entries; {n_added} successfully inserted into database.", flush=True)


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


if __name__ == '__main__':
    print("reset.py: Reset the Lisberger lab database...", flush=True)
    print("==> Attempting to connect to the database...")
    dj.config['database.host'] = 'db'
    dj.config['database.user'] = 'root'
    dj.config['safemode'] = False
    dj.config['enable_python_native_blobs'] = True
    if 'MYSQL_ROOT_PASSWORD' not in os.environ:
        print("====> ERROR: The environment variable MYSQL_ROOT_PASSWORD is missing... BYE!", flush=True)
        exit(1)
    dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']

    n_tries = 0
    db_connection: Optional[dj.Connection] = None
    while n_tries < 12:
        try:
            db_connection = dj.conn()
            break
        except Exception as err:
            print(f"    Failed to connect ({str(err)}). Trying again in 5 seconds...", flush=True)
            time.sleep(5)
    if db_connection is None:
        print("====> ERROR: Failed to connect to the database for 60+ seconds. Giving up.", flush=True)
        exit(1)

    if 'sgl' in dj.list_schemas(db_connection):
        print("==> Found 'sgl' schema in database. Dropping it...", flush=True)
        try:
            dj.schema('sgl').drop()
        except Exception as err:
            print(f"====> ERROR: Failed to drop the database - {str(err)}.", flush=True)
            exit(1)

    print("==> Creating and seeding 'sgl' database...", flush=True)
    try:
        sgl_module = importlib.import_module('.sgl_schema', package='database')
        seed_database(sgl_module)
    except Exception as err:
        print(f"====> ERROR: Failed to import sgl_schema.py - {str(err)}.", flush=True)

    print("BYE!", flush=True)
    exit(0)
