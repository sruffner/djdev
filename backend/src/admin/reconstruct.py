"""
reconstruct.py: A script that reconstructs the lab database by processing all entries in the database operations log.

If the database is corrupted, return it to its empty state with the reset.py script, then reconstruct its content from
the database update log by running this script. Obviously, this script could take quite some time to run if the database
contains data from a large number of experiment sessions. If the database reconstruction fails, manual reconstruction
will be required. In this scenario, the log output from this script may be useful.

This script is simply a wrapper for the function reconstruct_database() in module database.commit_ops.

Usage - when deployed on local development machine using Docker Compose:
    1) docker-compose up  ==> Starts the portal application in the usual manner.
    2) docker-compose stop backend  ==> Stop the Dash/Flask backend server.
    3) docker-compose run backend python -m admin.reset  ==> Run this script to ensure database is reset and empty.
    4) docker-compose run backend python -m admin.reconstruct  ==> Run this script to perform the reconstruction.
    4) docker-compose restart backend  ==> To resume normal operation.

@author: sruffner
@created: 19may2021
"""
import sys


from config.config import get_config

# configure DataJoint and connect to MySQL server. Must abort if connection is not established!
cfg = get_config()
if not cfg.init_database_connection():
    raise RuntimeError('Unable to connect to database!')

# We have to put this import AFTER configuring DJ and connecting to the database, since it will trigger a DB query
from database.commit_ops import reconstruct_database


if __name__ == '__main__':
    print("reconstruct.py: Rebuild Lisberger lab portal database from the database operation log and the"
          "raw data repository...\n\n",
          file=sys.stdout, flush=True)

    reconstruct_database()

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
