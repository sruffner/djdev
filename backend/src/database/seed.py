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
    { "table": "User", "entry": {"username": "sruffner", "full_name": "Scott A Ruffner",
      "contact_email": "sruffner@srscicomp.com", "role": "Administrator"}

In typical usage, this script will be invoked immediately after resetting the database with reset.py. It will take no
action if the database is not empty. The database is considered empty if the User table is empty.

Usage: Bring up the Docker Compose application that includes the 'db' and 'backend' services in the normal way. Stop
the 'backend' service with 'docker-compose stop backend'. Run this script as a one-time command against the 'backend'
service: 'docker-compose run backend python -m database.seed'. Once the script completes, resume the normal backend
service with 'docker-compose restart backend'.

@author: sruffner
@created: 22jul2021
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
from database.table_info import DBTable

# here is the seed data IAW the current lab database schema defined in sgl_schema.py
_seed_list = [
    {"table": "User", "entry": {"username": "sruffner", "full_name": "Scott A Ruffner",
                                "contact_email": "sruffner@srscicomp.com", "role": "Administrator"}},
    {"table": "User", "entry": {"username": "dherzfeld", "full_name": "David J Herzfeld",
                                "contact_email": "david.herzfeld@duke.edu", "role": "Post Doctoral Researcher"}},
    {"table": "User", "entry": {"username": "nhall", "full_name": "Nathan Hall",
                                "contact_email": "nathan.halld@duke.edu", "role": "Post Doctoral Researcher"}},
    {"table": "User", "entry": {"username": "sgl", "full_name": "Stephen G Lisberger",
                                "contact_email": "lisberger@neuro.duke.edu", "role": "Principal Investigator"}},

    {"table": "Rig", "entry": {"rig_id": "Rig 1", "rig_loc": "Vivarium (right front)"}},
    {"table": "Rig", "entry": {"rig_id": "Rig 2", "rig_loc": "Vivarium (right back)"}},
    {"table": "Rig", "entry": {"rig_id": "Rig 3", "rig_loc": "Vivarium (left front)"}},
    {"table": "Rig", "entry": {"rig_id": "Rig 4", "rig_loc": "Vivarium (left back)"}},
    {"table": "Rig", "entry": {"rig_id": "Rig H", "rig_loc": "Human studies setup (Bryan 327B)"}},

    {"table": "Subject", "entry": {"subj_id": "jojobe", "species": "Macaca mulatta", "dob": "2017-12-08", "sex": "M"}},
    {"table": "Subject", "entry": {"subj_id": "dandy", "species": "Macaca mulatta", "dob": "2015-05-06", "sex": "M"}},
    {"table": "Subject", "entry": {"subj_id": "yolanda", "species": "Macaca mulatta", "dob": "2019-04-18", "sex": "F"}},
    {"table": "Subject", "entry": {"subj_id": "yoda", "species": "Macaca mulatta", "dob": "2016-05-22", "sex": "M"}},

    {"table": "SubjectImplant", "entry": {"subj_id": "jojobe", "implant_date": "2018-07-22", "st_ap": "4",
                                          "st_ml": "3", "st_dv": "5", "ap_angle": "10", "ml_angle": "0"}},
    {"table": "SubjectImplant", "entry": {"subj_id": "dandy", "implant_date": "2017-04-28", "st_ap": "2.5",
                                          "st_ml": "3", "st_dv": "0", "ap_angle": "0", "ml_angle": "0"}},
    {"table": "SubjectImplant", "entry": {"subj_id": "dandy", "implant_date": "2019-09-15", "st_ap": "4",
                                          "st_ml": "5.6", "st_dv": "0", "ap_angle": "10", "ml_angle": "5"}},
    {"table": "SubjectImplant", "entry": {"subj_id": "yolanda", "implant_date": "2019-12-10", "st_ap": "1.6",
                                          "st_ml": "1.2", "st_dv": "1", "ap_angle": "4", "ml_angle": "8"}},
    {"table": "SubjectImplant", "entry": {"subj_id": "yolanda", "implant_date": "2020-01-08", "st_ap": "4",
                                          "st_ml": "3", "st_dv": "5", "ap_angle": "8.6", "ml_angle": "15.2"}},
    {"table": "SubjectImplant", "entry": {"subj_id": "yolanda", "implant_date": "2019-06-09", "st_ap": "12",
                                          "st_ml": "14", "st_dv": "8", "ap_angle": "0", "ml_angle": "11"}},

    {"table": "BrainArea", "entry": {"ba_name": "Cortex"}},
    {"table": "BrainArea", "entry": {"ba_name": "Amygdala"}},
    {"table": "BrainArea", "entry": {"ba_name": "Medulla oblongata"}},
    {"table": "BrainArea", "entry": {"ba_name": "Flocculus"}},
    {"table": "BrainArea", "entry": {"ba_name": "Hippocampus"}},
    {"table": "BrainArea", "entry": {"ba_name": "Vermis"}},

    {"table": "NeuronType", "entry": {"nt_name": "Golgi cell"}},
    {"table": "NeuronType", "entry": {"nt_name": "Granule cell"}},
    {"table": "NeuronType", "entry": {"nt_name": "Purkinje cell"}},
    {"table": "NeuronType", "entry": {"nt_name": "Stellate"}},
    {"table": "NeuronType", "entry": {"nt_name": "Unipolar brush"}},
    {"table": "NeuronType", "entry": {"nt_name": "Unspecified"}},

    {"table": "Study",
     "entry": {"study_title": "Pursuit with deficit", "study_lead": "sruffner",
               "study_desc": "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
                             "incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
                             "exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute "
                             "irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla "
                             "pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia "
                             "deserunt mollit anim id est laborum."}},
    {"table": "Study",
     "entry": {"study_title": "Search with cues", "study_lead": "dherzfeld",
               "study_desc": "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
                             "incididunt ut labore et dolore magna aliqua. Felis eget nunc lobortis mattis. Leo vel "
                             "orci porta non pulvinar neque laoreet. Quis blandit turpis cursus in. Non curabitur "
                             "gravida arcu ac tortor dignissim convallis. Non diam phasellus vestibulum lorem sed. "
                             "Sed pulvinar proin gravida hendrerit lectus a. At quis risus sed vulputate odio ut enim "
                             "blandit. Pulvinar elementum integer enim neque. Quis viverra nibh cras pulvinar mattis. "
                             "Odio pellentesque diam volutpat commodo sed egestas. Venenatis a condimentum vitae "
                             "sapien pellentesque. Felis eget velit aliquet sagittis. Fermentum iaculis eu non diam "
                             "phasellus. Ornare massa eget egestas purus viverra accumsan in nisl nisi. Sapien et "
                             "ligula ullamcorper malesuada proin. Nulla facilisi etiam dignissim diam quis. Tempor "
                             "commodo ullamcorper a lacus. Sit amet tellus cras adipiscing enim eu turpis egestas "
                             "pretium. Quis auctor elit sed vulputate.\n\nNulla facilisi cras fermentum odio. In hac "
                             "habitasse platea dictumst vestibulum rhoncus est. Sapien et ligula ullamcorper "
                             "malesuada proin libero nunc. Consectetur purus ut faucibus pulvinar. At urna "
                             "condimentum mattis pellentesque id nibh tortor. Pulvinar elementum integer enim neque "
                             "volutpat ac tincidunt vitae. Ultricies lacus sed turpis tincidunt id aliquet risus "
                             "feugiat in. Vitae nunc sed velit dignissim sodales."}},

    {"table": "Publication",
     "entry": {"doi": "https://doi.org/10.1152/jn.00261.2018",
               "citation": "Hall, Nathan J., Yan Yang, and Stephen G. Lisberger. \u0022Multiple components in "
                           "direction learning in smooth pursuit eye movements of monkeys.\u0022 J Neurophysiol 120, "
                           "no. 4 (October 1, 2018): 2020–35."}},
    {"table": "Publication",
     "entry": {"doi": "https://doi.org/10.7554/eLife.55217",
               "citation": "Herzfeld, David J., Nathan J. Hall, Marios Tringides, and Stephen G. Lisberger. "
                           "\u0022Principles of operation of a cerebellar learning circuit.\u0022 Elife 9 (April 30, "
                           "2020)."}},
    {"table": "Publication",
     "entry": {"doi": "https://doi.org/10.1152/jn.00710.2019",
               "citation": "Behling, Stuart, and Stephen G. Lisberger. \u0022Different mechanisms for modulation of "
                           "the initiation and steady-state of smooth pursuit eye movements.\u0022 J Neurophysiol "
                           "123, no. 3 (March 1, 2020): 1265–76."}},
    {"table": "Publication",
     "entry": {"doi": "https://doi.org/10.1152/jn.00209.2017",
               "citation": "Raghavan, Ramanujan T, and Stephen G Lisberger. \u0022Responses of Purkinje cells in the "
                           "oculomotor vermis of monkeys during smooth pursuit eye movements and saccades: comparison "
                           "with floccular complex.\u0022 J Neurophysiol 118, no. 2 (August 1, 2017): 986–1001."}},
    {"table": "Publication",
     "entry": {"doi": "https://doi.org/10.109/cercor/bhz294",
               "citation": "Lee, Joonyeol, Timothy R. Darlington, and Stephen G. Lisberger. \u0022The Neural Basis "
                           "for Response Latency in a Sensory-Motor Behavior.\u0022 Cereb Cortex 30, no. 5 (May 14, "
                           "2020): 3055–73."}}
]


if __name__ == '__main__':
    print("seed.py: Seed empty Lisberger lab database with some initial table entries (DEV USE ONLY)...\n\n",
          file=sys.stdout, flush=True)

    name_to_table_id = {
        "User": DBTable.USER, "Subject": DBTable.SUBJECT, "SubjectImplant": DBTable.IMPLANT,
        "Rig": DBTable.RIG, "BrainArea": DBTable.BRAIN_AREA, "NeuronType": DBTable.NEURON_TYPE,
        "Study": DBTable.STUDY, "Publication": DBTable.PUB
    }
    db_mgr = DataBaseManager()
    if db_mgr.num_table_rows(DBTable.USER) > 0:
        print("The database is not empty (at least one user found). No action taken!", file=sys.stdout, flush=True)
    try:
        for add_dict in _seed_list:
            if isinstance(add_dict, dict) and ("table" in add_dict) and ("entry" in add_dict) \
                    and (add_dict["table"] in name_to_table_id):
                err_msg = db_mgr.insert_into_table(name_to_table_id[add_dict["table"]], add_dict["entry"])
                if err_msg:
                    raise Exception(err_msg)
        print(f"Done. Database seeded with {len(_seed_list)} table entries.", file=sys.stdout, flush=True)
    except Exception as e:
        print(f"Failed to seed database: {e}", file=sys.stdout, flush=True)

    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
