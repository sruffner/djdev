"""
config.py: Application configuration

This module collects application configuration information in one place, so that it will be easier to modify when
needed. While some configuration parameters are unlikely to change, others may be different depending on the deployment
scenario (development vs production), and others need to be safeguarded in some fashion -- like the secret key for
Flask sessions or the username/password for the MySQL server.

@created: aug2021
@author: sruffner
"""
from __future__ import annotations  # Needed in Python 3.7 to type-hint a method with the type of enclosing class

import logging
import logging.config
import yaml
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional


_LOG_CFG_FILE = 'config/logging.yaml'


def get_application_logger() -> logging.Logger:
    """
    Get the singleton logger used to emit all logs from application code in the portal server app. On the first call,
    application-side logger is configured, along with the 'app' log that the Dash/Flask library uses, and the root
    logger. The root logger is configured only to emit logs at "WARNING" level and above -- so that we don't get too
    much crap from third-party libraries. The logging configuration is in config/logging.yaml.

    Returns:
        The logger to use in all portal server application code.
    Raises:
        RuntimeError: If unable to configure the application-wide logger on first invocation.
    """
    if not ('portal' in logging.root.manager.loggerDict):
        try:
            with open(_LOG_CFG_FILE, 'rt') as f:
                config = yaml.safe_load(f.read())
            logging.config.dictConfig(config)
            if not ('portal' in logging.root.manager.loggerDict):
                raise Exception('The "portal" logger not found after configuration')
        except Exception as e:
            raise RuntimeError(f"Failed to create application-wide logger for portal server: {str(e)}")

    return logging.getLogger('portal')


import datajoint as dj
from redis import Redis, RedisError


def get_config() -> AppConfig:
    """
    Get the application's configuration information, which includes some configuration parameters for Dash, Flask, and
    DataJoint/MySQL. Some configuration parameters are "application secrets" that need to be safeguarded froom
    exposure in the codebase.

    Returns:
        An AppConfig object encapsulating configuration parameters needed for the Lisberger lab data portal.
    """
    logger = get_application_logger()
    if not hasattr(get_config, 'config'):
        if 'DJDEV_ROOT_REPO' not in os.environ:
            raise RuntimeError('The environment variable DJDEV_ROOT_REPO is required.')
        repo_root = Path(os.environ['DJDEV_ROOT_REPO'])
        upload_dir = Path(os.environ['DJDEV_ROOT_REPO'], 'staging')
        if 'MARIADB_ROOT_PASSWORD' not in os.environ:
            raise RuntimeError('The environment variable MARIADB_ROOT_PASSWORD is required.')
        db_password = os.environ['MARIADB_ROOT_PASSWORD']
        if 'MARIADB_HOSTNAME' not in os.environ:
            raise RuntimeError('The environment variable MARIADB_HOSTNAME is required.')
        db_host = os.environ['MARIADB_HOSTNAME']
        if 'FLASK_SECRET_KEY' not in os.environ:
            raise RuntimeError('The environment variable FLASK_SECRET_KEY is required.')
        secret_key = os.environ['FLASK_SECRET_KEY']
        if ('REDIS_HOST' not in os.environ) or ('REDIS_PORT' not in os.environ):
            raise RuntimeError('The environment variables REDIS_HOST and REDIS_PORT are required.')
        conn = Redis(host=os.environ['REDIS_HOST'], port=os.environ['REDIS_PORT'])
        try:
            conn.ping()
        except RedisError as e:
            logger.debug(str(e), exc_info=True)

        get_config.config = AppConfig(repo_root=repo_root, dash_upload_dir=upload_dir, dj_database_host=db_host,
                                      dj_database_password=db_password, flask_secret_key=secret_key, redis_conn=conn)
        if ('AWS_ACCESS_KEY_ID' in os.environ) and ('AWS_ACCESS_KEY_SECRET' in os.environ) and \
           ('AWS_REGION_NAME' in os.environ) and ('REPO_S3_BUCKET_NAME' in os.environ):
            get_config.config.aws_access_key_id = os.environ['AWS_ACCESS_KEY_ID']
            get_config.config.aws_access_key_secret = os.environ['AWS_ACCESS_KEY_SECRET']
            get_config.config.aws_region_name = os.environ['AWS_REGION_NAME']
            get_config.config.repo_bucket = os.environ['REPO_S3_BUCKET_NAME']
        else:
            raise RuntimeError('Missing environment variable(s) specifying AWS access information for repo bucket')

    return get_config.config


@dataclass
class AppConfig:
    """
    Application configuration.
    """
    repo_root: str
    """ Relative or absolute path string identifying the root directory for lab portal's backing repository. """
    dash_upload_dir: str
    """ Relative or absolute path string identifying folder where application uploads are stored. """
    dj_database_host: str
    """ The database host name. """
    dj_database_password: str
    """ The password for the 'root' user of the database. """
    flask_secret_key: str
    """ 
    Flask-Login library uses sessions for authentication, so the Flask secret key must be set. NOTE that this should be
    set from a secret, not set to a new value every time the app is started -- as that will invalidate existing Flask
    sessions.
    """
    redis_conn: Redis
    """ 
    Connection to the Redis server used to cache state so that backend server can remain 'stateless' and thus
    permit replication in a cloud deployment. 
    """
    dash_suppress_callback_exceptions: bool = True
    """ Dash configuration parameter. Set to True b/c app dynamically inserts elements into layout. """
    dj_database_user: str = 'root'
    """ The database username for DataJoint. We stick with the 'root' user """
    dj_safemode: bool = False
    """ Enable/disable DataJoint's safe mode which, for example, will query console to confirm deletes. """
    dj_enable_python_native_blobs: bool = True
    """ Enable/disable python native blobs in DataJoint. """
    flask_permanent_session_lifetime: timedelta = timedelta(hours=24)
    """ Flask session lifetime. Flask-Login uses this to timeout client login sessions. """

    aws_access_key_id: Optional[str] = None
    """ Amazon Web Services (AWS) access key ID for IAM user with access privileges to S3."""
    aws_access_key_secret: Optional[str] = None
    """ AWS secret access key for IAM user with access privileges to S3. """
    aws_region_name: Optional[str] = None
    """ AWS region name that should be used for AWS access. """
    repo_bucket: Optional[str] = None
    """ Name of S3 bucket in which portal backing repository is maintained. """

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
        logger = get_application_logger()
        dj.config['database.host'] = self.dj_database_host
        dj.config['database.user'] = self.dj_database_user
        dj.config['database.password'] = self.dj_database_password
        dj.config['safemode'] = self.dj_safemode
        dj.config['enable_python_native_blobs'] = self.dj_enable_python_native_blobs

        n_tries = 0
        db_connection: Optional[dj.Connection] = None
        while n_tries < 12:
            n_tries = n_tries + 1
            try:
                db_connection = dj.conn()
                break
            except Exception as err:
                if n_tries < 12:
                    logger.warning(f"Failed to connect to database server. Trying again in 5s. [{str(err)}]")
                    time.sleep(5)
        if db_connection is None:
            logger.error(f"Failed to establish connection to database server after {n_tries} attempts. Giving up.")
        else:
            logger.info(f"Connected to database server after {n_tries} attempts.")
        return db_connection is not None
