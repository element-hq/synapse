
# WebPush

## Setup & configuration

In the synapse virtualenv, generate the server key pair by running
`vapid --gen --applicationServerKey`. This will generate a `private_key.pem`
(which you'll refer to in the config file with `vapid_private_key`) 
and `public_key.pem` file, and also a string labeled `Application Server Key`.

You'll copy the Application Server Key to `vapid_app_server_key` so that
web applications can fetch it through `/capabilities` and use it to subscribe
to the push manager:

```js
serviceWorkerRegistration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: "...",
});
```

You also need to set an e-mail address in `vapid_contact_email` in the config file,
where the push server operator can reach you in case they need to notify you
about your usage of their API.

Since for webpush, the push server endpoint is variable and comes from the browser
through the push data, you may not want to have your synapse instance connect to any
random addressable server.
You can use the global options `ip_range_blacklist` and `ip_range_allowlist` to manage that.

A default time-to-live of 15 minutes is set for webpush, but you can adjust this by setting
the `ttl: <number of seconds>` configuration option for the pusher.
If notifications can't be delivered by the push server aftet this time, they are dropped.

## Push key and expected push data

In your web application, [the push manager subscribe method](https://developer.mozilla.org/en-US/docs/Web/API/PushManager/subscribe)
will return
[a subscription](https://developer.mozilla.org/en-US/docs/Web/API/PushSubscription) 
with an `endpoint` and `keys` property, the latter containing a `p256dh` and `auth` 
property. The `p256dh` key is used as the push key, and the push data must contain
`endpoint` and `auth`. You can also set `default_payload` in the push data;
any properties set in it will be present in the push messages you receive, 
so it can be used to pass identifiers specific to your client
(like which account the notification is for).

### events_only

As of the time of writing, all webpush-supporting browsers require you to set 
`userVisibleOnly: true` when calling (`pushManager.subscribe`)
[https://developer.mozilla.org/en-US/docs/Web/API/PushManager/subscribe], to 
(prevent abusing webpush to track users)[https://goo.gl/yqv4Q4] without their 
knowledge. With this (mandatory) flag, the browser will show a "site has been 
updated in the background" notification if no notifications are visible after
your service worker processes a `push` event. This can easily happen when synapse
sends a push message to clear the unread count, which is not specific
to an event. With `events_only: true` in the pusher data, synapse won't forward
any push message without a event id. This prevents your service worker being
forced to show a notification to push messages that clear the unread count.

### only_last_per_room

You can opt in to only receive the last notification per room by setting
`only_last_per_room: true` in the push data. Note that if the first notification
can be delivered before the second one is sent, you will still get both;
it only has an effect when notifications are queued up on the gateway.

### Multiple pushers on one origin

Also note that because you can only have one push subscription per service worker,
and hence per origin, you might create pushers for different accounts with the same 
p256dh push key. To prevent the server from removing other pushers with the same 
push key for your other users, you should set `append` to `true` when uploading 
your pusher.

## Notification format

The notification as received by your web application will contain the following keys 
(assuming non-null values were sent by the homeserver). These are the
same as specified in [the push gateway spec](https://matrix.org/docs/spec/push_gateway/r0.1.0#post-matrix-push-v1-notify),
but the sub-keys of `counts` (`unread` and `missed_calls`) are flattened into
the notification object.

```
room_id
room_name
room_alias
membership
event_id
sender
sender_display_name
user_is_target
type
content
unread
missed_calls
```
