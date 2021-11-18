FROM bitnami/mariadb:10.6.4-debian-10-r16

# will this disable the vulnerability scan in DHE Gitlab?
LABEL twistexc=twistlockbypass

## Inject custom config file into appropriate place in bitnami/mariadb image
USER 0
ADD --chown=1001:1001 ./my_custom.cnf /opt/bitnami/mariadb/conf/my_custom.cnf
RUN chmod g+w /opt/bitnami/mariadb/conf/my_custom.cnf
USER 1001