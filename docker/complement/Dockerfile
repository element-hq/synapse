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
ARG DEBIAN_VERSION=bookworm

FROM docker.io/library/postgres:13-${DEBIAN_VERSION} AS postgres

FROM $FROM
# First of all, we copy postgres server from the official postgres image,
# since for repeated rebuilds, this is much faster than apt installing
# postgres each time.

# This trick only works because we use a postgres image based on the same debian
# version as Synapse's docker image (so the versions of the shared libraries
# match).

# sh is needed by initdb & start_for_complement.sh. Copy it now to get access to shell form instructions
COPY --from=postgres /bin/sh /bin/sh
# Choose useradd over adduser since the latter requires perl
RUN --mount=type=bind,from=postgres,source=/sbin/useradd,target=/sbin/useradd \
    useradd --system --user-group --uid 999 --home-dir /var/lib/postgresql --shell /usr/sbin/nologin postgres
COPY --from=postgres /usr/lib /usr/lib
COPY --from=postgres /usr/share/postgresql /usr/share/postgresql
COPY --from=postgres --chown=postgres /var/lib/postgresql /var/lib/postgresql
COPY --from=postgres --chown=postgres /var/run/postgresql /var/run/postgresql
ENV PATH="${PATH}:/usr/lib/postgresql/13/bin"
ENV PGDATA=/var/lib/postgresql/data

# We also initialize the database at build time, rather than runtime, so that it's faster to spin up the image.
RUN gosu postgres initdb --locale=C --encoding=UTF-8 --auth-host password

# Configure a password and create a database for Synapse
RUN echo "ALTER USER postgres PASSWORD 'somesecret'" | gosu postgres postgres --single
RUN echo "CREATE DATABASE synapse" | gosu postgres postgres --single

# Extend the shared homeserver config to disable rate-limiting,
# set Complement's static shared secret, enable registration, amongst other
# tweaks to get Synapse ready for testing.
# To do this, we copy the old template out of the way and then include it
# with Jinja2.
RUN ["python", "-c", "import os; os.rename('/conf/shared.yaml.j2', '/conf/shared-orig.yaml.j2')"]
COPY conf/workers-shared-extra.yaml.j2 /conf/shared.yaml.j2

WORKDIR /data

COPY conf/postgres.supervisord.conf /etc/supervisor/conf.d/postgres.conf

# Copy the entrypoint
COPY conf/start_for_complement.sh /
# Copy utilities used by the entrypoint
COPY --from=postgres /bin/openssl /bin/date /bin/
COPY --from=postgres /etc/ssl /etc/ssl

# Expose nginx's listener ports
EXPOSE 8008 8448

ENTRYPOINT ["/start_for_complement.sh"]

# Update the healthcheck to have a shorter check interval
HEALTHCHECK --start-period=5s --interval=1s --timeout=1s \
    CMD ["/healthcheck.py"]
