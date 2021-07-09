"""
app.py: Create and configure the Dash application instance for the Lisberger lab data portal.

TODO: Configuration really needs work. The DataJoint configuration appears in multiple files -- see reset.py,
 reconstruct.py, sgl_schema.py, sgl_auth.py. We also need to be smarter about setting up the initial database
 connection.

@created: oct2020
@author: sruffner
"""
from typing import Dict, Optional

import dash
import dash_bootstrap_components as dbc
import datajoint as dj
import dash_uploader as du
import os
from pathlib import Path
import flask_login

# bootstrap theme
ext_ss = [dbc.themes.SPACELAB]

app = dash.Dash(__name__, external_stylesheets=ext_ss)

server = app.server
app.config.suppress_callback_exceptions = True

# configure Dash uploader to upload to staging directory in backend container
if 'DJDEV_ROOT_REPO' not in os.environ:
    raise RuntimeError('The environment variable DJDEV_ROOT_REPO is required.')
upload_dir = Path(os.environ['DJDEV_ROOT_REPO'], 'staging')
du.configure_upload(app, str(upload_dir))

# DataJoint configuration.
# TODO: This and the initial DB connection attempt needs to go in a separate file. I repeat this code in some other
#  places -- see sgl_auth.py, reconstruct.py, reset.py.
dj.config['database.host'] = 'db'
dj.config['database.user'] = 'root'
dj.config['safemode'] = False
dj.config['enable_python_native_blobs'] = True
if 'MYSQL_ROOT_PASSWORD' not in os.environ:
    raise RuntimeError('The environment variable MYSQL_ROOT_PASSWORD is required.')
dj.config['database.password'] = os.environ['MYSQL_ROOT_PASSWORD']

# We have to put this AFTER configuring DJ, as importing manager.py will trigger initiating the DB connection
from database.manager import DataBaseManager
import database.sgl_auth as sgl_auth

# Setup for Flask-Login.
# TODO: We need to work on app configuration and put the SECRET_KEY in a safe place. One idea is to generate it on
#  first use and store in a file that is always git-ignored....
server.config.update(SECRET_KEY=os.urandom(12))
login_manager = flask_login.LoginManager()
login_manager.init_app(server)


class PortalUser(flask_login.UserMixin):
    def __init__(self, user_record: Dict[str, str]):
        self.user_record = user_record
        """User account information as queried from the database (see sgl_auth.py). """
        self.id = user_record['username']
        """ The username on the account (required by flask_login.UserMixin implementation). """

    def first_name(self) -> str:
        return self.user_record['full_name'].split()[0]

    def is_admin(self) -> bool:
        return self.user_record['access'] == 'admin'

    def can_curate_database(self) -> bool:
        return self.user_record['access'] in sgl_auth.CURATE_ACCESS

    def can_commit_to_database(self) -> bool:
        return self.user_record['access'] in sgl_auth.CONTRIBUTE_ACCESS


@login_manager.user_loader
def load_authorized_user(username: str) -> Optional[PortalUser]:
    """
    This function loads the user by user id.

    Args:
        username: The user ID
    Returns:
        The user object. Returns None if not found.
    """
    user_record = DataBaseManager().get_portal_user_record(username)
    return None if isinstance(user_record, str) else PortalUser(user_record)
