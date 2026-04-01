# syntax=docker/dockerfile:1
# This dockerfile builds on top of 'docker/Dockerfile-workers' in element-hq/synapse
# by including a built-in postgres instance, as well as setting up the homeserver so
# that it is ready for testing via Complement.
#
# Instructions for building this image from those it depends on is detailed in this guide:
# https://github.com/element-hq/synapse/blob/develop/docker/README-testing.md#testing-with-postgresql-and-single-or-multi-process-synapse

ARG SYNAPSE_VERSION=latest
# This is an intermediate image, to be built locally (not pulled from a registry).
ARG FROM=matrixdotorg/synapse-workers:$SYNAPSE_VERSION
ARG DEBIAN_VERSION=trixie

# Initialize postgres in a separate stage so that it is cached independently
# of synapse changes. This saves ~1.8s on every rebuild.
FROM docker.io/library/postgres:14-${DEBIAN_VERSION} AS postgres_setup

ENV PGDATA=/var/lib/postgresql/data

RUN gosu postgres initdb --locale=C --encoding=UTF-8 --auth-host password
RUN echo "ALTER USER postgres PASSWORD 'somesecret'" | gosu postgres postgres --single
RUN echo "CREATE DATABASE synapse" | gosu postgres postgres --single

FROM $FROM
# Copy postgres server binaries from the official postgres image.
#
# This trick only works because we use a postgres image based on the same
# debian version as Synapse's docker image (so the versions of the shared
# libraries match). Any missing libraries need to be added to either the
# Synapse image or docker/Dockerfile-workers.
RUN adduser --system --uid 999 postgres --home /var/lib/postgresql
COPY --from=postgres_setup /usr/lib/postgresql /usr/lib/postgresql
COPY --from=postgres_setup /usr/share/postgresql /usr/share/postgresql
COPY --from=postgres_setup --chown=postgres /var/run/postgresql /var/run/postgresql
ENV PATH="${PATH}:/usr/lib/postgresql/14/bin"
ENV PGDATA=/var/lib/postgresql/data

# Copy the pre-initialized database from the postgres_setup stage.
COPY --from=postgres_setup --chown=postgres ${PGDATA} ${PGDATA}

# Extend the shared homeserver config to disable rate-limiting,
# set Complement's static shared secret, enable registration, amongst other
# tweaks to get Synapse ready for testing.
# To do this, we copy the old template out of the way and then include it
# with Jinja2.
RUN mv /conf/shared.yaml.j2 /conf/shared-orig.yaml.j2
COPY conf/workers-shared-extra.yaml.j2 /conf/shared.yaml.j2

WORKDIR /data

COPY conf/postgres.supervisord.conf /etc/supervisor/conf.d/postgres.conf

# Copy the entrypoint
COPY conf/start_for_complement.sh /

# Expose nginx's listener ports
EXPOSE 8008 8448

ENTRYPOINT ["/start_for_complement.sh"]

# Update the healthcheck to have a shorter check interval
HEALTHCHECK --start-period=5s --interval=1s --timeout=1s \
    CMD ["/healthcheck.sh"]
