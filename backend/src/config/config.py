"""
config.py: Application configuration

This module collects application configuration information in one place, so that it will be easier to modify when
needed. While some configuration parameters are unlikely to change, others may be different depending on the deployment
scenario (development vs production), and others need to be safeguarded in some fashion -- like the secret key for
Flask sessions or the username/password for the MySQL server.

TODO: Implement a secure way to get the Flask secret key (right now we generate a new one each time the app starts),
    the database user/password for DataJoint.

@created: aug2021
@author: sruffner
"""
from __future__ import annotations  # Needed in Python 3.7 to type-hint a method with the type of enclosing class

import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

import datajoint as dj


def get_config() -> AppConfig:
    """
    Get the application's configuration information, which includes some configuration parameters for Dash, Flask, and
    DataJoint/MySQL. Some configuration parameters are "application secrets" that need to be safeguarded froom
    exposure in the codebase.

    Returns:
        An AppConfig object encapsulating configuration parameters needed for the Lisberger lab data portal.
    """
    if not hasattr(get_config, 'config'):
        if 'DJDEV_ROOT_REPO' not in os.environ:
            raise RuntimeError('The environment variable DJDEV_ROOT_REPO is required.')
        upload_dir = Path(os.environ['DJDEV_ROOT_REPO'], 'staging')
        if 'MYSQL_ROOT_PASSWORD' not in os.environ:
            raise RuntimeError('The environment variable MYSQL_ROOT_PASSWORD is required.')
        mysql_password = os.environ['MYSQL_ROOT_PASSWORD']
        get_config.config = AppConfig(dash_upload_dir=upload_dir, dj_database_password=mysql_password)
    return get_config.config


@dataclass
class AppConfig:
    """
    Application configuration.
    """
    dash_suppress_callback_exceptions: bool = True
    """ Dash configuration parameter. Set to True b/c app dynamically inserts elements into layout. """
    dash_upload_dir: str = '/'
    """ Relative or absolute path string identifying folder where application uploads are stored. """
    dj_database_host: str = 'db'
    """ The MySQL database host for DataJoint. It is the name of the Docker service running the MySQL daemon. """
    dj_database_user: str = 'root'
    """ The database username for DataJoint. """
    dj_database_password: str = ''
    """ The database password for DataJoint. """
    dj_safemode: bool = False
    """ Enable/disable DataJoint's safe mode which, for example, will query console to confirm deletes. """
    dj_enable_python_native_blobs: bool = True
    """ Enable/disable python native blobs in DataJoint. """
    flask_permanent_session_lifetime: timedelta = timedelta(hours=24)
    """ Flask session lifetime. Flask-Login uses this to timeout client login sessions. """
    flask_secret_key: bytes = os.urandom(12)
    """ Flask-Login library uses sessions for authentication, so the Flask secret key must be set. """

    def init_database_connection(self) -> bool:
        """
        Initialize DataJoint's persistent connection to the MySQL server that houses the Lisberger lab database. This
        should be called during application startup, before any attempt to access the database! The connection is shared
        across modules and is not thread-safe.

        The method will make up to 12 attempts -- waiting 5 seconds between attempts -- to establish the connection
        before giving up. A message is written to the console each time a connection attempt fails.

        Returns:
            True if connection was established; else False, in which case the application should exit.
        """
        dj.config['database.host'] = self.dj_database_host
        dj.config['database.user'] = self.dj_database_user
        dj.config['database.password'] = self.dj_database_password
        dj.config['safemode'] = self.dj_safemode
        dj.config['enable_python_native_blobs'] = self.dj_enable_python_native_blobs

        logger = logging.getLogger(__name__)
        n_tries = 0
        db_connection: Optional[dj.Connection] = None
        while n_tries < 12:
            n_tries = n_tries + 1
            try:
                db_connection = dj.conn()
                break
            except Exception as err:
                if n_tries < 12:
                    logger.warning(f"Failed to connect to MySQL server. Trying again in 5s. [{str(err)}]")
                    time.sleep(5)
        if db_connection is None:
            logger.error(f"Failed to establish connection to MySQL server after {n_tries} attempts. Giving up.")
        else:
            logger.info(f"Connected to MySQL server after {n_tries} attempts.")
        return db_connection is not None
