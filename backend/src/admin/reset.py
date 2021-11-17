"""
reset.py: A script that completely drops the Lisberger lab database.

This script is primarily for use during development of the database and web portal. Specifically, whenever the database
schema defined in sgl_schema.py changes, we really need to drop the entire database and start over. That is the sole
purpose of this script.

It is also useful in the event that the database server crashes and the database must be reconstructed. Call this script
to ensure the database is reset and completely empty. Then run the reconstruct.py script.

Usage - when deployed on local development machine using Docker Compose:
    1) docker-compose up  ==> Starts the portal application in the usual manner.
    2) docker-compose stop backend  ==> Stop the Dash/Flask backend server.
    3) docker-compose run backend python -m admin.reset  ==> Run this script.
    4) docker-compose restart backend  ==> To resume normal operation.

NOTE: The main method dynamically imports the database schema in 'sgl_schema.py' after dropping the schema from the
database. The schema is declared on the database the FIRST time the sgl_schema module is imported in a running
python shell. If we imported sgl_schema in the normal manner (with the import statement), the import would happen
before the schema was dropped, and so the schema would not get declared on the database.

@author: sruffner
@created: 04mar2021
"""
import importlib
import os
import shutil
import sys
from pathlib import Path

import datajoint as dj

from config.config import get_config

if __name__ == '__main__':
    print("reset.py: Drops the Lisberger lab database...", file=sys.stdout, flush=True)

    print("==> Attempting to connect to the database...", file=sys.stdout, flush=True)
    cfg = get_config()
    if not cfg.init_database_connection():
        print("====> ERROR: Failed to connect to the database for 60+ seconds. Giving up.", file=sys.stdout, flush=True)
        exit(1)

    if 'sgl' in dj.list_schemas():
        print("==> Found 'sgl' schema in database. Dropping it...", file=sys.stdout, flush=True)
        try:
            dj.schema('sgl').drop()
        except Exception as err:
            print(f"====> ERROR: Failed to drop the database - {str(err)}.", file=sys.stdout, flush=True)
            exit(1)

    repo_path = Path(os.environ['DJDEV_ROOT_REPO'])
    if repo_path.is_dir():
        delete_repo = input("==> Found backing repository. Delete ONLY if you will NOT reconstruct database "
                            "from existing repository. Delete it? (y/n) >> ")
        if delete_repo == 'y':
            try:
                shutil.rmtree(repo_path)
                print("==> Backing repository successfully removed.", file=sys.stdout, flush=True)
            except Exception as e:
                print(f"====> ERROR: Operation failed. You must remove backing repository manually ({str(e)}",
                      file=sys.stdout, flush=True)

    print("==> Creating empty 'sgl' database...", file=sys.stdout, flush=True)
    try:
        sgl_module = importlib.import_module('.sgl_schema', package='database')
    except Exception as err:
        print(f"====> ERROR: Failed to import sgl_schema.py - {str(err)}.", flush=True)

    print("BYE!", flush=True)
    exit(0)
