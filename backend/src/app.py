"""
app.py: Create and configure the Dash application instance for the Lisberger lab data portal.

@created: oct2020
@author: sruffner
"""
from typing import Dict, Optional

import dash
import dash_bootstrap_components as dbc
import dash_uploader as du
import flask_login
from config import get_config, AppConfig

cfg: AppConfig = get_config()
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.SPACELAB])
server = app.server

app.config.suppress_callback_exceptions = cfg.dash_suppress_callback_exceptions

# configure Dash uploader to upload to staging directory in backend container
du.configure_upload(app, cfg.dash_upload_dir)

# configure DataJoint and connect to MySQL server. Must abort if connection is not established!
if not cfg.init_database_connection():
    raise RuntimeError('Unable to connect to database!')

# We have to put this import AFTER configuring DJ and connecting to the database, since it will trigger a DB query
from database.manager import DataBaseManager, ADMIN_ACCESS, COMMIT_ACCESS

# Setup for Flask-Login
server.permanent_session_lifetime = cfg.flask_permanent_session_lifetime
server.config.update(SECRET_KEY=cfg.flask_secret_key)
login_manager = flask_login.LoginManager()
login_manager.init_app(server)
login_manager.login_view = '/explore'
login_manager.refresh_view = '/explore'
login_manager.needs_refresh_message = "Session timed out, please login again."
login_manager.needs_refresh_message_category = "info"


class PortalUser(flask_login.UserMixin):
    def __init__(self, user_record: Dict[str, str]):
        self.user_record = user_record
        """User account information as queried from the database (see sgl_auth.py). """
        self.id = user_record['username']
        """ The username on the account (required by flask_login.UserMixin implementation). """

    def first_name(self) -> str:
        return self.user_record['full_name'].split()[0]

    def is_admin(self) -> bool:
        return self.user_record['access'] == ADMIN_ACCESS

    def can_commit_to_database(self) -> bool:
        return self.user_record['access'] in COMMIT_ACCESS

    def full_name(self) -> str:
        return self.user_record['full_name']

    def contact_email(self) -> str:
        return self.user_record['contact_email']

    def title(self) -> str:
        return self.user_record['title']

    def organization(self) -> str:
        return self.user_record['organization']


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
