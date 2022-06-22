"""
seed.py: A script that seeds the lab database with a few manual table entries if the database is empty. Intended
    only for use during development.

During development of the lab portal, we have frequently had to change the lab database's DataJoint schema design. This
involves dropping the current incarnation of the database entirely on the MySQL server, so it is useful to be able to
repopulate the empty database with at least some data in the manual tables like User, Subject, and so on.

This script serves that purpose. The seed data is hard-coded in this file as a list of dictionaries. Each dictionary
object defines an entity to be added to the lab database. It has the following format:

    { "table": "<table name>", "entry": {<entry definition>}}

 The <table name> must exactly match one of eight manual tables in the SGL database schema: "User", "Subject",
 "SubjectImplant", "Rig", "BrainArea", "NeuronType", "Study", and "Publication". The <entry definition> is the set of
 attribute name-value pairs defining the new table "row". For example, to add a new user:
    { "table": "User", "entry": {"username": "sruffner", "access": "admin", "full_name": "Scott A Ruffner",
      "contact_email": "sruffner@srscicomp.com"}

In typical usage, this script will be invoked immediately after resetting the database with reset.py. It will take no
action if the database is not empty.

The script will require user input for each entry added to the User table. That table stores the encrypted password of
each registered portal user, and we do not want to store plain-text passwords in any code or other file that may end up
in the project Gitlab repository. For each user registered by the script, it will prompt for that user's password.

Usage - when deployed on local development machine using Docker Compose:
    1) docker-compose up  ==> Starts the portal application in the usual manner.
    2) docker-compose stop backend  ==> Stop the Dash/Flask backend server.
    3) docker-compose run backend python -m admin.reset  ==> Run this script to ensure database is reset and empty.
    4) docker-compose run backend python -m admin.seed  ==> Run this script to seed the database.
    4) docker-compose restart backend  ==> To resume normal operation.

@author: sruffner
@created: 22jul2021
"""
import sys
from typing import List, Dict, Any

from config.config import get_config

# configure DataJoint and connect to MySQL server. Must abort if connection is not established!
cfg = get_config()
if not cfg.init_database_connection():
    raise RuntimeError('Unable to connect to database!')

# We have to put these imports AFTER configuring DJ and connecting to the database, since it will trigger a DB query
from database.table_ops import database_empty, insert_into_table
from database.user_ops import register_new_portal_user, prompt_for_password
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

    dict(table=DBTable.IMPLANT, entry=dict(subj_id='Dandy', implant_date='2017-04-28', st_ap=0.0, st_ml=-11.0, st_dv=0.0,
                                           ap_angle=-26.0, ml_angle=0.0)),
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


if __name__ == '__main__':
    print("seed.py: Seed empty Lisberger lab database with some initial table entries (DEV USE ONLY)...\n\n",
          file=sys.stdout, flush=True)

    err_msg = database_empty()
    if err_msg is not None:
        print(f"ERROR: {str(err_msg)}.\n  The database must be completely empty prior to seeding. Aborting...",
              file=sys.stdout, flush=True)
        exit(0)

    try:
        for add_dict in _seed_list:
            if isinstance(add_dict, dict) and ("table" in add_dict) and ("entry" in add_dict):
                table_id: DBTable = add_dict['table']
                entry = add_dict['entry']
                # special case: Registering a new user. Need to prompt for password.
                if table_id == DBTable.USER:
                    password = prompt_for_password(entry['username'])
                    if password is None:
                        raise Exception(f"Aborted script on request.")
                    err_msg = register_new_portal_user(
                        entry['username'], password, entry['access'], entry['full_name'], entry['contact_email'],
                        entry['title'], entry['organization'])
                else:
                    err_msg = insert_into_table(table_id, entry)
                if err_msg:
                    raise Exception(err_msg)
        print(f"Done. Database seeded with {len(_seed_list)} table entries.", file=sys.stdout, flush=True)
    except Exception as e:
        print(f"Failed to seed database: {e}", file=sys.stdout, flush=True)

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
