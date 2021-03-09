"""
reset.py: A script that completely drops the Lisberger lab database.

This script is primarily for use during development of the database and web portal. Specifically, whenever the database
schema defined in sgl_schema.py changes, we really need to drop the entire database and start over. That is the sole
purpose of this script.

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.reset'. Once the script completes, resume the normal
backend service with 'docker-compose restart backend'. As part of the backend server resuming, the Lisberger lab
schema should be recreated on the DB and possibly reseeded with initial manual table entries.

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
from typing import Optional

import datajoint as dj


if __name__ == '__main__':
    print("reset.py: Drops the Lisberger lab database...", flush=True)
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

    print("==> Creating empty 'sgl' database...", flush=True)
    try:
        sgl_module = importlib.import_module('.sgl_schema', package='database')
    except Exception as err:
        print(f"====> ERROR: Failed to import sgl_schema.py - {str(err)}.", flush=True)

    print("BYE!", flush=True)
    exit(0)
