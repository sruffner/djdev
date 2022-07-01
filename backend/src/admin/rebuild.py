"""
rebuild.py: A script that rebuilds the Lisberberger lab database from scratch.

There are two use cases for this administrative script, which should only be run when the portal is not in use:
    1) During development, whenever the database schema in sgl_schema.py changes in any way that changes the structure
       of existing database tables, we must drop the 'sgl' schema from the database server entirely, create an empty
       database with the new schema, then 'seed' the database with prescribed manual table content (users, rigs,
       subjects, and so on).
    2) If the lab database should ever become corrupted during normal operation and cannot be recovered using the
       MariaDB's own backup facilities, we would like to be able to restore the database content to its state prior to
       the catastrophic failure. To that end, a log of every database operation is maintained in a dedicated file in the
       portal workspace directory (that log file is occasionally saved to the portal's backup repository on AWS S3). The
       archive file of every session committed to the database is also saved in the backup repostory. Thus, it is
       possible to reconstruct the database, with minimal user interaction, by "playing back" every operation recorded
       in the database operations log in chronological order.

The script first drops the 'sgl' database in its entirety, then recreates it as an empty database. The script then
checks the portal workspace for an existing database operations log file. If no file is present, the script seeds
various manual tables in the database with known lab metadata that is hard-coded in this file. If it is present, the
user has the option of either reconstructing the database or only seeding it. If the user elects not to reconstruct, the
database operations log is removed and the seed entries are inserted into the database (and these insertions are
recorded in a new database operations log!).

Obviously, this script could take quite some time to run when reconstructing a database that contains data from a large
number of experiment sessions (but, it is much faster than recommitting the session manually via the portal!). If the
database reconstruction fails, manual reconstruction will be required. In this scenario, the console output from this
script may be useful.

The script will prompt for an initial password that will apply to every registered portal user added to database, as we
do not want to store plain-text passwords in any code or other file that may end up in the project Gitlab repository. Of
course, it is important to inform all users to update their password once the database is rebuilt!

Usage - when deployed on local development machine using Docker Compose:
    1) docker-compose up  ==> Starts the portal application in the usual manner.
    2) docker-compose stop backend  ==> Stop the Dash/Flask backend server.
    3) docker-compose run backend python -m admin.rebuild  ==> Run this script.
    4) docker-compose down; docker-compose up  ==> To resume normal operation. Don't use docker-compose restart backend,
       as this will also restart the container created in step (3)!

NOTE: The main method dynamically imports the database schema in 'sgl_schema.py' after dropping the schema from the
database. The schema is declared on the database the FIRST time the sgl_schema module is imported in a running
python shell. If we imported sgl_schema in the normal manner (with the import statement), the import would happen
before the schema was dropped, and so the schema would not get declared on the database.

@author: sruffner
@created: 30jun2022
"""
import importlib
import sys
from getpass import getpass
from typing import List, Dict, Any

import datajoint as dj

from config.config import get_config
from database.table_info import DBTable


# here is the seed data IAW the current lab database schema defined in sgl_schema.py
_seed_list: List[Dict[str, Any]] = [
    dict(table=DBTable.USER,
         entry=dict(username='sruffner', access='admin', full_name='Scott A Ruffner',
                    contact_email='sruffner@srscicomp.com', title='Programming Support',
                    organization='Scott Ruffner Scientific Computing')),
    dict(table=DBTable.USER,
         entry=dict(username='dherzfeld', access='admin', full_name='David J Herzfeld',
                    contact_email='david.herzfeld@duke.edu', title='Postdoctoral Associate',
                    organization='Duke University - Lisberger Laboratory')),
    dict(table=DBTable.USER,
         entry=dict(username='sgl', access='download', full_name='Stephen G Lisberger',
                    contact_email='lisberger@neuro.duke.edu', title='Primary Investigator',
                    organization='Duke University - Dept of Neurobiology')),
    dict(table=DBTable.USER,
         entry=dict(username='nhall', access='commit', full_name='Nathan Hall',
                    contact_email='nathan.hall@duke.edu', title='Postdoctoral Associate',
                    organization='Duke University - Lisberger Laboratory')),
    dict(table=DBTable.USER,
         entry=dict(username='tdarlington', access='commit', full_name='Timothy R Darlington',
                    contact_email='timothy.darlington@duke.edu', title='Graduate Student',
                    organization='Duke University - Lisberger Laboratory')),
    dict(table=DBTable.USER,
         entry=dict(username='sbehling', access='commit', full_name='Stuart Behling',
                    contact_email='stuart.behling@duke.edu', title='Graduate Student',
                    organization='Duke University - Lisberger Laboratory')),
    dict(table=DBTable.USER,
         entry=dict(username='segger', access='commit', full_name='Seth W Egger',
                    contact_email='seth.egger@duke.edu', title='Postdoctoral Associate',
                    organization='Duke University - Lisberger Laboratory')),

    dict(table=DBTable.RIG,
         entry=dict(rig_id='Rig A', rig_loc="Vivarium (right back)")),
    dict(table=DBTable.RIG,
         entry=dict(rig_id='Rig B', rig_loc="Vivarium (right front)")),
    dict(table=DBTable.RIG,
         entry=dict(rig_id='Rig C', rig_loc="Vivarium (left front)")),
    dict(table=DBTable.RIG,
         entry=dict(rig_id='Rig D', rig_loc="Vivarium (left back)")),
    dict(table=DBTable.RIG,
         entry=dict(rig_id='Rig H', rig_loc="Human studies setup (Bryan 327B)")),

    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Aristotle', species='Macaca mulatta', dob='2010-05-01', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Batman', species='Macaca mulatta', dob='2011-01-01', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Dandy', species='Macaca mulatta', dob='2013-03-30', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Diogenes', species='Macaca mulatta', dob='2016-05-22', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Edgar', species='Macaca mulatta', dob='2015-05-11', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Fredrick', species='Macaca mulatta', dob='2015-05-17', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Grogu', species='Macaca mulatta', dob='2016-07-17', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Reggie', species='Macaca mulatta', dob='2005-03-29', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Vernon', species='Macaca mulatta', dob='2004-04-25', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Xtra', species='Macaca mulatta', dob='2007-03-16', sex='M')),
    dict(table=DBTable.SUBJECT,
         entry=dict(subj_id='Yoda', species='Macaca mulatta', dob='2007-03-11', sex='M')),

    dict(table=DBTable.IMPLANT, entry=dict(subj_id='Dandy', implant_date='2017-04-28', st_ap=0.0, st_ml=-11.0,
                                           st_dv=0.0, ap_angle=-26.0, ml_angle=0.0)),
    dict(table=DBTable.IMPLANT, entry=dict(subj_id='Edgar', implant_date='2016-03-22', st_ap=0.0, st_ml=11.0, st_dv=0.0,
                                           ap_angle=-26.0, ml_angle=0.0)),
    dict(table=DBTable.IMPLANT, entry=dict(subj_id='Xtra', implant_date='2016-03-22', st_ap=3.5, st_ml=1.0, st_dv=8.0,
                                           ap_angle=0, ml_angle=20.0)),
    dict(table=DBTable.IMPLANT, entry=dict(subj_id='Yoda', implant_date='2016-03-22', st_ap=0.0, st_ml=11.0, st_dv=0.0,
                                           ap_angle=-26.0, ml_angle=0.0)),
    dict(table=DBTable.IMPLANT, entry=dict(subj_id='Yoda', implant_date='2016-04-22', st_ap=3.5, st_ml=1.0, st_dv=8.0,
                                           ap_angle=0.0, ml_angle=20.0)),

    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='Flocculus')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='Medial Temporal - MT')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='Medial Superior Temporal - MST')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='OMV')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='FEFsem')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='Abducens')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='Vestibular nucleus')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='Oculomotor nucleus')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='NRTP')),
    dict(table=DBTable.BRAIN_AREA, entry=dict(ba_name='DLPN')),

    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Golgi cell')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Granule cell')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Purkinje cell')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Molecular layer interneuron')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Unipolar brush cell')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Mossy fiber')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Climbing fiber response')),
    dict(table=DBTable.NEURON_TYPE, entry=dict(nt_name='Unspecified')),

    dict(table=DBTable.STUDY,
         entry=dict(study_title='Single-trial pursuit learning', study_lead='dherzfeld',
                    study_desc='Single-trial learning with visually stabilized probes.')),
    dict(table=DBTable.STUDY,
         entry=dict(study_title='Long-term pursuit learning', study_lead='nhall',
                    study_desc='Long-term pursuit learning with intermixed probes.')),
    dict(table=DBTable.STUDY,
         entry=dict(study_title='Miscellany', study_lead='sruffner', study_desc='** For testing purposes only. **')),

    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.1152/jn.00261.2018",
                    citation="Hall, Nathan J., Yan Yang, and Stephen G. Lisberger. \u0022Multiple components in "
                             "direction learning in smooth pursuit eye movements of monkeys.\u0022 J Neurophysiol 120, "
                             "no. 4 (October 1, 2018): 2020–35.")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.7554/eLife.55217",
                    citation="Herzfeld, David J., Nathan J. Hall, Marios Tringides, and Stephen G. Lisberger. "
                             "\u0022Principles of operation of a cerebellar learning circuit.\u0022 Elife 9 (April 30, "
                             "2020).")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.1152/jn.00710.2019",
                    citation="Behling, Stuart, and Stephen G. Lisberger. \u0022Different mechanisms for modulation of "
                             "the initiation and steady-state of smooth pursuit eye movements.\u0022 J Neurophysiol "
                             "123, no. 3 (March 1, 2020): 1265–76.")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.1152/jn.00209.2017",
                    citation="Raghavan, Ramanujan T, and Stephen G Lisberger. \u0022Responses of Purkinje cells in the "
                             "oculomotor vermis of monkeys during smooth pursuit eye movements and saccades: comparison"
                             " with floccular complex.\u0022 J Neurophysiol 118, no. 2 (August 1, 2017): 986–1001.")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.1093/cercor/bhz294",
                    citation="Lee, Joonyeol, Timothy R. Darlington, and Stephen G. Lisberger. \u0022The Neural Basis "
                             "for Response Latency in a Sensory-Motor Behavior.\u0022 Cereb Cortex 30, no. 5 (May 14, "
                             "2020): 3055–73.")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.1038/s41467-022-29457-4",
                    citation="Egger, Seth W., and Stephen G. Lisberger. \u0022Neural structure of a sensory decoder "
                             "for motor control.\u0022 Nat Commun 13, no. 1 (April 5, 2022): 1829.")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.1152/jn.00047.2021",
                    citation="Hall, Nathan J., David J. Herzfeld, and Stephen G. Lisberger. \u0022Evaluation and "
                             "resolution of many challenges of neural spike sorting: a new sorter.\u0022 "
                             "J Neurophysiol 126, no. 6 (December 1, 2021): 2065–90.")),
    dict(table=DBTable.PUB,
         entry=dict(doi="https://doi.org/10.7554/eLife.50962",
                    citation="Darlington, Timothy R., and Stephen G. Lisberger. \u0022Mechanisms that allow cortical "
                             "preparatory activity without inappropriate movement.\u0022 "
                             "Elife 9 (February 21, 2020)."))
]


def _connect_to_database() -> bool:
    print("==> Attempting to connect to the database...", file=sys.stdout, flush=True)
    if not get_config().init_database_connection():
        print("====> ERROR: Failed to connect to database server for 60+ seconds.", file=sys.stdout, flush=True)
        return False
    return True


def _reset_database() -> bool:
    from config.app_logging import get_application_logger

    if 'sgl' in dj.list_schemas():
        print("==> Found 'sgl' schema in database. Dropping it...", file=sys.stdout, flush=True)
        try:
            dj.schema('sgl').drop()
        except Exception as e:
            print(f"====> ERROR: Failed to drop the database - {str(e)}.", file=sys.stdout, flush=True)
            return False

    print("==> Creating empty 'sgl' database...", file=sys.stdout, flush=True)
    try:
        importlib.import_module('.sgl_schema', package='database')
    except Exception as e:
        print(f"====> ERROR: Failed to import sgl_schema.py - {str(e)}.", file=sys.stdout, flush=True)
        return False

    get_application_logger().info("Successfully reset 'sgl' database")
    return True


def _prompt_for_initial_password() -> str:
    """
    Prompt for the initial password that will apply to all portal user accounts added while seeding or reconstructing
    the portal database. The method will prompt for the password twice to guard against accidental typos and verify that
    it meets requirements. If not, it will prompt again until an acceptable password is entered.

    Returns:
        A valid password for a portal user account.
    """
    # we cannot put this at top of file b/c it directly or indirectly imports sgl_schema. See NOTE in module header.
    from database.user_ops import validate_password

    while True:
        new_password = getpass(f"Enter a valid initial password for all registered portal users > ")
        confirm_new = getpass('Reenter password to confirm > ')
        if confirm_new != new_password:
            print("   Password mismatch... Try again.", file=sys.stdout, flush=True)
        else:
            res = validate_password(new_password)
            if res is None:
                return new_password
            else:
                print(f"   {str(res)}... Try again.", file=sys.stdout, flush=True)


def _rebuild() -> None:
    # we cannot put these at top of file b/c they directly or indirectly import sgl_schema. See NOTE in module header.
    from database.log_ops import log_file_path
    from database.commit_ops import reconstruct_database
    from database.table_ops import insert_into_table
    from database.user_ops import register_new_portal_user
    from config.app_logging import get_application_logger

    do_reconstruct = False
    log_path = log_file_path()
    if log_path.is_file():
        ans = input("A database operations log exists. Reconstruct database content "
                    "from log and repository files? (y or N) > ")
        do_reconstruct = (ans == 'y')
        if not do_reconstruct:
            log_path.unlink(missing_ok=True)
            msg = "Deleted stale database operations log prior to reseeding database."
            print(msg, file=sys.stdout, flush=True)
            get_application_logger().info(msg)

    initial_password = _prompt_for_initial_password()

    if do_reconstruct:
        reconstruct_database(initial_password)
    else:
        msg = f"Seeding empty database with {len(_seed_list)} metadata table entries..."
        print(msg, file=sys.stdout, flush=True)
        get_application_logger().info(msg)
        try:
            for add_dict in _seed_list:
                if isinstance(add_dict, dict) and ("table" in add_dict) and ("entry" in add_dict):
                    table_id: DBTable = add_dict['table']
                    entry = add_dict['entry']
                    if table_id == DBTable.USER:
                        err_msg = register_new_portal_user(
                            entry['username'], initial_password, entry['access'], entry['full_name'],
                            entry['contact_email'], entry['title'], entry['organization']
                        )
                    else:
                        err_msg = insert_into_table(table_id, entry)
                    if err_msg:
                        raise Exception(err_msg)
            print(f"...Done!", file=sys.stdout, flush=True)
        except Exception as e:
            msg = f"Failed to seed database: {e}"
            print(msg, file=sys.stdout, flush=True)
            get_application_logger().error(msg, exc_info=True)


if __name__ == '__main__':
    print("rebuild.py: Reset Lisberger lab portal database and reseed/reconstruct...", file=sys.stdout, flush=True)

    if not _connect_to_database():
        exit(1)

    yes_or_no = input('Are you sure you want to reset the database. This operation cannot be undone (y or N) > ')
    if yes_or_no != 'y':
        print("Operation cancelled. BYE!", file=sys.stdout, flush=True)
        exit(0)

    # the reset must happen before any imports that implicitly import sgl_schema.py. See NOTE in module header.
    if not _reset_database():
        exit(1)

    _rebuild()

    print("BYE!", file=sys.stdout, flush=True)
    exit(0)
