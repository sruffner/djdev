# This is just a simple Dockerfile so that I can grab Redis image from Docker Hub and cache it in the container
# registry for the sgl-portal Gitlab project. I do change the user for the container image, as recommended. Further
# specialized configurations for our Redis instance could go here?
#
# This file is not needed for the local Docker Compose nor the single-pod Kubernetes deployment of the portal app.
FROM redis:6.2.5-alpine

USER 10001