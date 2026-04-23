Add support for "MSC4452 Preview URL capabilities API" which exposes a `io.element.msc4452.preview_url` capability.
If `experimental_features.msc4452_enabled` is `true`, the `/_matrix/(client/v1/media|media/v3)/preview_url` endpoint
now responds with a 403 status code when the capability is disabled.
