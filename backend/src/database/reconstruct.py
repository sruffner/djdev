"""
reconstruct.py: A script that reconstructs the lab database by processing all entries in the database update log.

For now, a single pickle file in the database backup repository, $REPO_HOME/logs/update_log, contains the entire history
of operations on the database. The database can be reconstructed from scratch by executing the operations stored in the
log file. Of course, to execute a session commit job, the requisite files must also be located in the data repository.

It is ESSENTIAL that the database be empty when the script is called -- that is it's assumed state just before the
first operation in update_log. The operations should be logged in chronological order, and this script simply executes
each operation in the log in the same order, thereby reconstructing the database content. No user intervention is
required, as the update log and the raw data repository stores everything that is needed to repopulate the database.

If the database is corrupted, return it to its empty state with the reset.py script, then reconstruct its content from
the database update log by running this script. Obviously, this script could take quite some time to run if the database
contains data from a large number of experiment sessions.

If the database reconstruction fails, manual reconstruction will be required. In this scenario, the log output from this
script may be useful

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.reconstruct'. Once the script completes, resume the normal
backend service with 'docker-compose restart backend'.

@author: sruffner
@created: 19may2021
"""
import sys
import os
import datajoint as dj

# DataJoint configuration parameters required to connect to the database. In the portal backend server, these are
# found in app.py. THESE NEED TO BE SETUP BEFORE THE CONNECTION ATTEMPT THAT OCCURS WHEN IMPORTING sgl_schema.py
# via the from...import statement that follows
dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['safemode'] = False
dj.config['enable_python_native_blobs'] = True
if 'MYSQL_ROOT_PASSWORD' not in os.environ:
    print("====> ERROR: The environment variable MYSQL_ROOT_PASSWORD is missing!", flush=True)
dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']

from database.manager import DataBaseManager

if __name__ == '__main__':
    print("reconstruct.py: Rebuild Lisberger lab database from the update log history and raw data repository...\n\n",
          file=sys.stdout, flush=True)

    db_mgr = DataBaseManager()
    db_mgr.reconstruct_database_from_log()

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
