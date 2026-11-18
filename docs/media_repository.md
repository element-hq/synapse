# Media Repository

*Synapse implementation-specific details for the media repository*

The media repository
 * stores avatars, attachments and their thumbnails for media uploaded by local
   users.
 * caches avatars, attachments and their thumbnails for media uploaded by remote
   users.
 * caches resources and thumbnails used for URL previews.

All media in Matrix can be identified by a unique
[MXC URI](https://spec.matrix.org/latest/client-server-api/#matrix-content-mxc-uris),
consisting of a server name and media ID:
```
mxc://<server-name>/<media-id>
```

## Local Media
Synapse generates 24 character media IDs for content uploaded by local users.
These media IDs consist of upper and lowercase letters and are case-sensitive.
Other homeserver implementations may generate media IDs differently.

Local media is recorded in the `local_media_repository` table, which includes
metadata such as MIME types, upload times and file sizes.
Note that this table is shared by the URL cache, which has a different media ID
scheme.

### Paths
A file with media ID `aabbcccccccccccccccccccc` and its `128x96` `image/jpeg`
thumbnail, created by scaling, would be stored at:
```
local_content/aa/bb/cccccccccccccccccccc
local_thumbnails/aa/bb/cccccccccccccccccccc/128-96-image-jpeg-scale
```

## Remote Media
When media from a remote homeserver is requested from Synapse, it is assigned
a local `filesystem_id`, with the same format as locally-generated media IDs,
as described above.

A record of remote media is stored in the `remote_media_cache` table, which
can be used to map remote MXC URIs (server names and media IDs) to local
`filesystem_id`s.

### Paths
A file from `matrix.org` with `filesystem_id` `aabbcccccccccccccccccccc` and its
`128x96` `image/jpeg` thumbnail, created by scaling, would be stored at:
```
remote_content/matrix.org/aa/bb/cccccccccccccccccccc
remote_thumbnail/matrix.org/aa/bb/cccccccccccccccccccc/128-96-image-jpeg-scale
```
Older thumbnails may omit the thumbnailing method:
```
remote_thumbnail/matrix.org/aa/bb/cccccccccccccccccccc/128-96-image-jpeg
```

Note that `remote_thumbnail/` does not have an `s`.

## Content-addressed (SHA-256) Paths

New local uploads and remote downloads are stored under the SHA-256 of their
content instead of their media ID or `filesystem_id`. Media with identical
content then share one file on disk. This is an internal storage detail: MXC
URIs and media IDs are unchanged, and there is no configuration option for it.

Local and remote media never share a file, and remote media is only shared
between media from the same origin. URL preview resources are not
content-addressed and keep the paths described under [URL Previews](#url-previews).

For a SHA-256 of `ebf4f635a17...` (64 hex characters), the file and its `128x96`
`image/jpeg` thumbnail, created by scaling, would be stored at:

```sh
local_content/eb/f4/f635...
local_thumbnails/eb/f4/f635.../128-96-image-jpeg-scale

remote_content/famedly.de/eb/f4/f635...
remote_thumbnail/famedly.de/eb/f4/f635.../128-96-image-jpeg-scale
```

The storage provider and the local store use the same relative path. Only the base directory differs. A file is only sent to the storage providers when it is first
written to its SHA-256 path in the local media store. Later media with the same
content reuses the local file and is not sent again.

There is case when that relative path is differ.:

* Media stored before deduplication: the provider copy may still be at the
  media-ID path. If such remote media is downloaded again (the re-download
  case), the local file ends up at the SHA-256 path.
* Deletes: they only remove local files. A provider can keep copies at both
  paths after the local ones are gone.

However, the read order (SHA-256 path, then media-ID path, local store first) still finds the file wherever it is in both cases.

### Locking

Renaming onto a SHA-256 path must not race with a delete that is deciding the
same file is unused. `MediaRepository` takes the cross-worker lock
`SHA256_FILES_LOCK_NAME` (key`"{server_name or ''}/{sha256}"`,
see `_lock_sha256_files`):

* Callers creating new media hold it from before the `store_into_file` block
  exits until the database row is inserted.
* Deletes hold it while checking whether any media still references the SHA-256
  and removing the files if not.

### Compatibility with media-ID paths

Existing media keeps working at its media-ID paths. There is no migration:
existing files are not moved or rewritten, in the local media store or in
storage providers. There is SHA-256 backfill can happen while re-downloading
remote media.

* **Reading:** if the SHA-256 of the media is known, its SHA-256 path is tried
  first, then its media-ID path. Media without a recorded SHA-256 is only read
  from its media-ID path. The local media store is checked first, then each
  storage provider, with the same relative paths in the same order. Media served
  from a storage provider is streamed from it and not cached locally. Only
  generating a thumbnail (including for URL previews) copies the original from
  a storage provider into the local media store, at the same path the provider
  has.

* **Re-downloading remote media:** if the file of remote media is missing from
  the local media store and from all storage providers, it is downloaded again
  and stored at its SHA-256 path, like a new download. The media already has a
  database row, so the row is kept. If the row has no SHA-256 recorded, the
  SHA-256 of the new download is recorded on it, so the media is read from the
  SHA-256 path from then on. This is the only case where existing media moves to
  a SHA-256 path. Local media has no equivalent.

* **Writing:** the content is written to a temporary file in the media store and
  hashed. If the SHA-256 path does not exist yet, the temporary file is moved
  there. Otherwise the temporary file is deleted and the media references the
  existing file. A failed spam check only deletes the temporary file. Media
  upload limits are checked before anything is written.

* **Thumbnails:** the path of a thumbnail depends on the SHA-256 recorded in the
  database, not on where the original file is. Thumbnails at media-ID paths are
  left in place and are still found.
  * New media reuses the thumbnail rows of other media with the same hash. If
    those were generated before deduplication, the thumbnail is regenerated on
    its first request.
  * Media with a recorded SHA-256 stores new thumbnails under its SHA-256 path,
    even if its original is still at its media-ID path.
  * Media without a recorded SHA-256 stores thumbnails under its media-ID path.
  * Lookups for media with a recorded SHA-256 try the SHA-256 thumbnail path
    first, then the media-ID thumbnail path, so thumbnails created before this
    change are still found. Existing thumbnails are never moved.
  * The SHA-256 recorded in the database is trusted. Files are not hashed again
    when they are read.

* **Deleting:** on delete, the media-ID path and its thumbnails are always
  removed from the local media store. The SHA-256 path and its thumbnails are
  removed only when no other media references the same SHA-256. For remote media,
  only media from the same origin counts.
  * If the media-ID file cannot be removed, the database rows are kept and the
    SHA-256 path is left in place, so the delete can be retried.
  * Deleting media never removes files from storage providers. Copies at both the
    media-ID and SHA-256 paths remain there and must be cleaned up out of band.

## URL Previews

When generating previews for URLs, Synapse may download and cache various
resources, including images. These resources are assigned temporary media IDs
of the form `yyyy-mm-dd_aaaaaaaaaaaaaaaa`, where `yyyy-mm-dd` is the current
date and `aaaaaaaaaaaaaaaa` is a random sequence of 16 case-sensitive letters.

The metadata for these cached resources is stored in the
`local_media_repository` and `local_media_repository_url_cache` tables.

Resources for URL previews are deleted after a few days.

### Paths
The file with media ID `yyyy-mm-dd_aaaaaaaaaaaaaaaa` and its `128x96`
`image/jpeg` thumbnail, created by scaling, would be stored at:
```
url_cache/yyyy-mm-dd/aaaaaaaaaaaaaaaa
url_cache_thumbnails/yyyy-mm-dd/aaaaaaaaaaaaaaaa/128-96-image-jpeg-scale
```
