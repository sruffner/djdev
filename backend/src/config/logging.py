"""
logging.py: Application logging configuration

CREDITS: https://fangpenlin.com/posts/2012/08/26/good-logging-practice-in-python/

@created: sep2021
@author: sruffner
"""
import os
import logging.config

import yaml


def setup_logging(cfg_file_env: str = 'LOG_CFG', cfg_file: str = 'logging.yaml', level: int = logging.INFO) -> None:
    """
    Configure application logging from a specified YAML file.

    Args:
        cfg_file_env: Environment key holding file path for YAML file. Default = 'LOG_CFG'.
        cfg_file: File path to YAML file. Ignored if environment key exists. Default = 'logging.yaml'.
        level: If no YAML file is found, the method minimally configures the root logger with a stream handler to
            stdout, setting the logging level to this value. Default = logging.INFO.
    """
    path = cfg_file
    value = os.getenv(cfg_file_env, None)
    if value:
        path = value
    if os.path.exists(path):
        with open(path, 'rt') as f:
            config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=level)
