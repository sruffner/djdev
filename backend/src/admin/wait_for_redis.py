"""
wait_for_redis.py: A script that pings the Redis server to see if it's alive.

This script was written to implement a Kubernetes "initContainer" for the deployment of the Redis Queue worker pods in
the portal application. Since the RQ workers depend on the Redis server for polling queues and posting results, there
is no point in starting the RQ workers until Redis is ready. The script pings the server at REDIS_HOST:REDIS_PORT every
2 seconds untils it gets a valid response, giving up after 60 attempts.

To package as an initContainer, simply use the Docker image for the portal backend (admittedly overkill!) and run
the command "python -m admin.wait_for_redis.

@author: sruffner
@created: 13dec2021
"""
import logging
from time import sleep

from redis import RedisError

from config.config import get_config

logger = logging.getLogger(__name__)


if __name__ == '__main__':
    cfg = get_config()
    logger.info("Checking if Redis server is available...")
    for i in range(60):
        try:
            res = cfg.redis_conn.ping()
            if res == 'PONG':
                exit(0)
            else:
                logger.debug(f"Redis.ping() returned {res}, instead of PONG")
        except RedisError as e:
            logger.debug(f"Failed to ping Redis server: {str(e)}")
        sleep(2)

    logger.error("Failed to ping Redis server for ~2 minutes. Giving up.")
    exit(1)
