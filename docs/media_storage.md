# Media Storage

*Synapse implementation-specific details for `synapse/media/media_storage.py`*

`MediaStorage` reads and writes media files (originals and thumbnails) to the
local media store and to the configured
[storage providers](usage/configuration/config_documentation.md#media_storage_providers).

## Local and non-local Storages

`local_provider` is only set when `enable_local_media_storage` is `true` and
`media_store_path` is set. Its directory is `MediaStorage.local_media_directory`.
Each storage provider is given the same relative path as the local store.

## Paths

Two methods turn a `FileInfo` into relative paths:

* `_get_relative_path(file_info, identifier=None)` builds one path from an
  identifier, `file_info.file_id` by default. It picks the local, remote, URL
  cache or thumbnail layout from `file_info`.
* `_file_info_to_path(file_info)` returns every path the file may be stored at,
  in lookup order:
  1. the SHA-256 path, if `file_info.sha256` is set;
  2. the media-ID (`file_id`) path.

URL cache files (`file_info.url_cache`) only ever use the media-ID path, because
they are expired by date.

For remote thumbnails, reads also try the older thumbnail path without the
thumbnailing method (`remote_media_thumbnail_rel_legacy`).

## Writing

A SHA-256 path may be shared by several media IDs. It is never opened with `wb`,
because that would truncate a file other media is being served from.

Media-ID paths belong to one media ID, so they can be written in place, as before
deduplication.

For a new upload or a remote download, the SHA-256 is not yet known and the
destination depends on it, so the content is written to a temporary file and
hashed while it is being written.

When the block exits, the temporary file is moved onto its final path with
`_rename_if_not_exists`. If that path already exists, the temporary file is
deleted and the existing file is kept. The file is only sent to the storage
providers when this call created the local file, so media with the same content
as earlier media is not sent to them again. Without a local store, the file is
always sent to the storage providers.

Only two cases write straight to the final file in place: URL cache files,
and thumbnails of media with no recorded SHA-256 when a local store is present.
Both of those are addressed by media ID, which is why the destination is available
before the write begins.

`MediaRepository` passes the SHA-256 from the database row in the thumbnail's
`FileInfo`. Where the original file is does not matter: a thumbnail of media
with a recorded SHA-256 always goes under the SHA-256 path, even when the
original is still at its media-ID path, for example in a storage provider.

### Thumbnail reuse

When new media is uploaded or downloaded, `MediaRepository._generate_thumbnails`
first calls `copy_thumbnails_with_sha256`. If other media with the same SHA-256
(and, for remote media, the same origin) already has thumbnail rows, those rows
are copied to the new media and the image is not decoded again. The thumbnail
files are shared through the SHA-256 path. URL cache media is excluded.

If the source media was thumbnailed before deduplication, its files are at its
media-ID path, so the copied rows point at SHA-256 thumbnail files that do not
exist yet. Each such thumbnail is generated on its first request, as for any
thumbnail missing on disk.

## Reading

Files are read from where they are stored. Media stored before deduplication
stays at its media-ID path: nothing is moved or rewritten on read, in the local
media store or in storage providers. The SHA-256 from the database row is
trusted, so the bytes are not hashed again.

The only exception is remote media whose file is missing everywhere: it is
downloaded again onto its SHA-256 path. See
[Media Repository](media_repository.md#compatibility-with-media-id-paths).

### `fetch_media(file_info)`

It tries the local store first, then each storage provider, asking each one
for every path from `_file_info_to_path`, in order. URL cache files are only
read from the local store.

### `ensure_media_is_in_local_cache(file_info)`

Async context manager that yields a local path to read from, for example for
thumbnailing:

1. With a local store, each path from `_file_info_to_path` (and the legacy
   remote thumbnail path) is checked on disk. The first that exists is yielded.
2. Otherwise each storage provider is asked for each path, in order.
   `_fetch_into_temp_file` downloads the file into a temporary file.
3. With a local store, the temporary file is moved with `_rename_if_not_exists`
   onto the same path the provider had, and that path is yielded. Without a
   local store, the temporary file itself is yielded and deleted when the block
   exits.
4. `NotFoundError` is raised if no provider has the file.

No lock is taken here. Files in storage providers are trusted, like the SHA-256
in the database, and their bytes are not hashed again. A concurrent writer to the
same path is therefore assumed to produce an identical file, and
`_rename_if_not_exists` keeps whichever file is already there.

### Thumbnail lookup

Thumbnails are looked up by the SHA-256 recorded in the database, not by where
the original file is. For media with a recorded SHA-256, `_file_info_to_path`
returns two candidate thumbnail paths, and `fetch_media` tries both, on local
disk first and then in each storage provider.

Remote thumbnails also try the older file name without the method. Media without
a recorded SHA-256 only has the media-ID candidate. Existing thumbnails are never
moved.

## Temporary files

Created by `_create_temp_file` with the prefix `synapse-media-`, in the root of
the local media store. That keeps them on the same filesystem, so moving one
into place is a rename rather than a copy. Without a local store, the system
temp directory is used.
A file left behind by a crash is removed by
`cleanup_stale_temporary_files`. `MediaRepository` runs it every hour on the
worker that runs background tasks. It removes files in the media store root
older than 24 hours that start with `synapse-media-`, or with Python's default
`tmp` prefix used by older versions.

## Deleting

`MediaStorage` has no delete method. `MediaRepository` removes files from the
local media store, and only removes a SHA-256 path once no media references it.
Storage providers are never asked to delete files. See
[Media Repository](media_repository.md#compatibility-with-media-id-paths) for
the rules.
