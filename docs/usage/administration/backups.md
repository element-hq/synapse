# How to back up a Synapse homeserver

It is critical to maintain good backups of your server, to guard against
hardware failure as well as potential corruption due to bugs or administrator
error.

This page documents the things you will need to consider backing up as part of
a Synapse installation.

## Configuration files

Keep a copy of your configuration file (`homeserver.yaml`), as well as any
auxiliary config files it refers to such as the
[`log_config`](../configuration/config_documentation.md#log_config) file,
[`app_service_config_files`](../configuration/config_documentation.md#app_service_config_files).
Often, all such config files will be kept in a single directory such as
`/etc/synapse`, which will make this easier.

## Server signing key

Your server has a [signing
key](../configuration/config_documentation.md#signing_key_path) which it uses
to sign events and outgoing federation requests. It is easiest to back it up
with your configuration files, but an alternative is to have Synapse create a
new signing key if you have to restore.

If you do decide to replace the signing key, you should add the old *public*
key to
[`old_signing_keys`](../configuration/config_documentation.md#old_signing_keys).

## Database

Synapse's support for SQLite is only suitable for testing purposes, so for the
purposes of this document, we'll assume you are using
[PostgreSQL](../../postgres.md).

A full discussion of backup strategies for PostgreSQL is out of scope for this
document; see the [PostgreSQL
documentation](https://www.postgresql.org/docs/current/backup.html) for
detailed information.

### Synapse-specfic details

 * Be very careful not to restore into a database that already has tables
   present. At best, this will error; at worst, it will lead to subtle database
   inconsistencies.

 * The `e2e_one_time_keys_json` table should **not** be backed up, or if it is
   backed up, should be
   [`TRUNCATE`d](https://www.postgresql.org/docs/current/sql-truncate.html)
   after restoring the database before Synapse is started.

   [Background: restoring the database to an older backup can cause
   used one-time-keys to be re-issued, causing subsequent [message decryption
   errors](https://github.com/element-hq/element-meta/issues/2155). Clearing
   all one-time-keys from the database ensures that this cannot happen, and
   will prompt clients to generate and upload new one-time-keys.]

### Quick and easy database backup and restore

Typically, the easiest solution is to use `pg_dump` to take a copy of the whole
database. We recommend `pg_dump`'s custom dump format, as it produces
significantly smaller backup files.

```shell
sudo -u postgres pg_dump -Fc --exclude-table-data e2e_one_time_keys_json synapse > synapse.dump
```

There is no need to stop Postgres or Synapse while `pg_dump` is running: it
will take a consistent snapshot of the databse.

To restore, you will need to recreate the database as described in [Using
Postgres](../../postgres.md#set-up-database),
then load the dump into it with `pg_restore`:

```shell
sudo -u postgres createdb --encoding=UTF8 --locale=C --template=template0 --owner=synapse_user synapse
sudo -u postgres pg_restore -d synapse < synapse.dump
```

(If you forgot to exclude `e2e_one_time_keys_json` during `pg_dump`, remember
to connect to the new database and `TRUNCATE e2e_one_time_keys_json;` before
starting Synapse.)

To reiterate: do **not** restore a dump over an existing database.

Again, if you plan to run your homeserver at any sort of production level, we
recommend studying the PostgreSQL documentation on backup options.

## Media store

Synapse keeps a copy of media uploaded by users, including avatars and message
attachments, in its [Media
store](../configuration/config_documentation.md#media-store).

It is a directory on the local disk, containing the following directories:

 * `local_content`: this is content uploaded by your local users. As a general
   rule, you should back this up: it may represent the only copy of those
   media files anywhere in the federation, and if they are lost, users will
   see errors when viewing user or room avatars, and messages with attachments.

 * `local_thumbnails`: "thumbnails" of images uploaded by your users. If
   [`dynamic_thumbnails`](../configuration/config_documentation.md#dynamic_thumbnails)
   is enabled, these will be regenerated if they are removed from the disk, and
   there is therefore no need to back them up.

   If `dynamic_thumbnails` is *not* enabled (the default): although this can
   theoretically be regenerated from `local_content`, there is no tooling to do
   so. We recommend that these are backed up too.

 * `remote_content`: this is a cache of content that was uploaded by a user on
    another server, and has since been requested by a user on your own server.

    Typically there is no need to back up this directory: if a file in this directory
    is removed, Synapse will attempt to fetch it again from the remote
    server.

 * `remote_thumbnails`: thumbnails of images uploaded by users on other
    servers. As with `remote_content`, there is normally no need to back this
    up.

 * `url_cache`, `url_cache_thumbnails`: temporary caches of files downloaded
   by the [URL previews](../../setup/installation.md#url-previews) feature.
   These do not need to be backed up.
