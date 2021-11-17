"""
print_log.py: A script that prints a summary of all entries in the database operations log.

For now, a single pickle file in the database backup repository, $REPO_HOME/logs/update_log, contains the entire history
of operations on the database. The database can be reconstructed from scratch by executing the operations stored in the
log file. Of course, to execute a session commit job, the requisite files must also be located in the data repository.

Usage - when deployed on local development machine using Docker Compose:
    1) docker-compose up  ==> Starts the portal application in the usual manner.
    2) docker-compose stop backend  ==> Stop the Dash/Flask backend server.
    3) docker-compose run backend python -m admin.print_log  ==> Run this script.
    4) docker-compose restart backend  ==> To resume normal operation.

@author: sruffner
@created: 04mar2021
"""
import sys

from database.log_ops import dump_log

if __name__ == '__main__':
    print("print_log.py: Summary of database operations log history for the Lisberger lab data portal...\n",
          file=sys.stdout, flush=True)
    dump_log(sys.stdout)
    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
