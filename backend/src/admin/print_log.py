"""
print_log.py: A script that prints a summary of all entries in the database update logs.

For now, a single pickle file in the database backup repository, $REPO_HOME/logs/update_log, contains the entire history
of operations on the database. The database can be reconstructed from scratch by executing the operations stored in the
log file. Of course, to execute a session commit job, the requisite files must also be located in the data repository.

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.print_log'. Once the script completes, resume the normal
backend service with 'docker-compose restart backend'.

@author: sruffner
@created: 04mar2021
"""
import pickle
import sys
from pathlib import Path
import os


if __name__ == '__main__':
    print("print_log.py: Summary of update log history for the Lisberger lab database...\n",
          file=sys.stdout, flush=True)

    print("\n****** Update log history ******\n", file=sys.stdout, flush=True)
    log_file_path = Path(os.environ['DJDEV_ROOT_REPO'], 'logs', 'update_log')
    if not log_file_path.is_file():
        print(f"=====> Error: No log file found at {str(log_file_path)}", file=sys.stdout, flush=True)
    else:
        try:
            num_entries = 0
            with open(log_file_path, 'rb') as file:
                while True:
                    try:
                        entry = pickle.load(file)
                        num_entries += 1
                        print(f"{num_entries:04}:  {entry}", file=sys.stdout, flush=True)
                    except EOFError:
                        break
        except Exception as e:
            print(f"=====> Error occurred while printing update log history: {str(e)}", file=sys.stdout, flush=True)

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
