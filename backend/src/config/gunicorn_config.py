# configuration settings for the GUnicorn WSGI server that runs the Dash-based portal backend
from random import random

from database.commit_ops import queue_task_to_clean_commit_staging_areas

bind = ['0.0.0.0:8050']
# uncomment next line only when debugging -- every client acccess request is logged to stdou
# accesslog = '-'
worker_class = 'sync'
workers = 5
threads = 1
timeout = 120
worker_tmp_dir = '/dev/shm'

import config.app_logging as app_log
from database.log_ops import schedule_log_backup_if_necessary


# noinspection PyUnusedLocal
def when_ready(server):
    app_log.get_application_logger().info(f"The GUnicorn-served backend has started.")
    schedule_log_backup_if_necessary(soon=True)
    app_log.push_orphaned_application_message_log_to_repo()
    queue_task_to_clean_commit_staging_areas(delay_minutes=10+4*random())


# noinspection PyUnusedLocal
def post_fork(server, worker):
    app_log.get_application_logger().info(f"GUnicorn worker ({str(worker)} is starting up.")


# noinspection PyUnusedLocal
def worker_exit(server, worker) -> None:
    app_log.get_application_logger().info(f"GUnicorn worker ({str(worker)}) exited.")


# noinspection PyUnusedLocal
def on_exit(server) -> None:
    app_log.get_application_logger().info(f"The GUnicorn-served backend is exiting.")
    app_log.force_flush_application_message_log()
