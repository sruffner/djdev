FROM bitnami/mariadb:10.6.5-debian-10-r7

## Inject custom config file into appropriate place in bitnami/mariadb image
USER 0
ADD --chown=1001:1001 ./my_custom.cnf /opt/bitnami/mariadb/conf/my_custom.cnf
RUN chmod g+w /opt/bitnami/mariadb/conf/my_custom.cnf
USER 1001
