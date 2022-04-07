# configuration settings for the GUnicorn WSGI server that runs the Dash-based portal backend
bind = ['0.0.0.0:8050']
# uncomment next line only when debugging -- every client acccess request is logged to stdou
# accesslog = '-'
worker_class = 'sync'
workers = 5
threads = 1
timeout = 120
worker_tmp_dir = '/dev/shm'

import config.app_logging as app_log


# noinspection PyUnusedLocal
def when_ready(server):
    app_log.get_application_logger().info(f"The GUnicorn-served backend has started.")


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
