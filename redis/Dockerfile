# This is just a simple Dockerfile so that I can grab Redis image from Docker Hub and cache it in the container
# registry for the sgl-portal Gitlab project.
#
# OpenShift Platform fix: In the default configuration, Redis will do a background save of its in-memory data to the
# directory /data. The Redis alpine image sets up this directory and sets its owner and group to 'redis'. However,
# OpenShift runs containers using an arbitrary user ID for security reasons. So when the Redis server (running as this
# arbitrary user ID) tries to do a background save, the operation fails on "Permission Denied".
#
# According to OpenShift docs, the way around this is to ensure that any directories and files that may be written to
# by processes in the container should belong to the root group (id=0) -- which doesn't have the same privileges as the
# root user -- and be read/writable by that group. Hence the RUN command in this Dockerfile.
#
# Further specialized configurations for our Redis instance could go here?
#
# This file is not needed for the local Docker Compose nor the single-pod Kubernetes deployment of the portal app. In
# these deployment scenarios, the "Permission Denied" issue described above did not arise.
FROM redis:6.2.5-alpine

RUN chgrp -R 0 /data && chmod -R g=u /data