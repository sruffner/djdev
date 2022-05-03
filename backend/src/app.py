"""
app.py: Create and configure the Dash application instance for the Lisberger lab data portal.

@created: oct2020
@author: sruffner
"""
from typing import Dict, Optional

# set up the application configuration and logging as early as possible
from flask_jwt_extended import JWTManager

import config.config
import config.app_logging

_cfg: config.config.AppConfig = config.config.get_config()
config.app_logging.get_application_logger()


from dash import Dash
import dash_bootstrap_components as dbc
import dash_uploader as du
import flask_login
from database.upload_handler import UploadHandler
from database.user_ops import get_portal_user_record, ADMIN_ACCESS, COMMIT_ACCESS


app = Dash(__name__, external_stylesheets=[dbc.themes.SPACELAB, dbc.icons.BOOTSTRAP])

app.config.suppress_callback_exceptions = _cfg.dash_suppress_callback_exceptions

# configure Dash uploader to upload to staging directory in backend container and to use a custom upload handler
# that serves our purpose
du.configure_upload(app, _cfg.dash_upload_dir, http_request_handler=UploadHandler)

# Setup for Flask-Login
app.server.permanent_session_lifetime = _cfg.flask_permanent_session_lifetime
app.server.config.update(SECRET_KEY=_cfg.flask_secret_key)
login_manager = flask_login.LoginManager()
login_manager.init_app(app.server)
login_manager.login_view = '/explore'
login_manager.refresh_view = '/explore'
login_manager.needs_refresh_message = "Session timed out, please login again."
login_manager.needs_refresh_message_category = "info"

# Setup for Flask-JWT-Extended
app.server.config.update(JWT_SECRET_KEY=_cfg.jwt_secret_key, JWT_ACCESS_TOKEN_EXPIRES=_cfg.jwt_access_token_lifetime)
jwt = JWTManager(app.server)


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
    user_record = get_portal_user_record(username)
    if isinstance(user_record, str):
        config.app_logging.get_application_logger().warning(f"Authentication error: {user_record}")
        return None
    return PortalUser(user_record)


# here we define the Flask API endpoints for programmatic retrieval of database content (read-only access)
# noinspection PyUnresolvedReferences
from api import endpoints
