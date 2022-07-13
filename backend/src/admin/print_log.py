"""
print_log.py: A script that prints a summary of all entries in the database operations or API requests log.

This script is simple wrapper for `:py:func:database.log_ops.dump_log`, printing all entries in the specified log to
STDOUT.

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
    yes_or_no = input('Dump contents of database operations history (y) or API requests log (N) > ')
    dump_log(sys.stdout, is_api_log=(yes_or_no != 'y'))
    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
