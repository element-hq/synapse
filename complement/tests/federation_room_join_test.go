package synapse_tests

import (
	"context"
	"io"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/matrix-org/complement"

	"github.com/matrix-org/gomatrixserverlib"

	"github.com/tidwall/gjson"

	"github.com/matrix-org/complement/b"
	"github.com/matrix-org/complement/federation"
	"github.com/matrix-org/complement/helpers"
	"github.com/matrix-org/complement/must"
)

// This test verifies that events sent into a room between a /make_join and
// /send_join are not lost to the joining server. When an event is created
// during the join handshake, the join event's prev_events (set at make_join
// time) won't reference it, creating two forward extremities. The server
// handling the join should ensure the joining server can discover the missed
// event, for example by sending a follow-up event that references both
// extremities, prompting the joining server to backfill.
//
// See https://github.com/element-hq/synapse/pull/19390
//
// This test lives as a in-repo Synapse Complement test because the spec doesn't mandate
// which events should be resolvable after the `/make_join`/`/send_join` dance (or that
// a homeserver should send `m.dummy` events to tie things together).
//
// To be clear, resolving the events in the `/make_join`/`/send_join` gap would happen
// naturally as soon as someone else sends an event who is on a homeserver aware of the
// events in the gap (tie it into the DAG). The goal of the automatic dummy events is to
// make this happen more immediately by sending a `m.dummy` event that ties things in
// instead of waiting for another event to be sent naturally.
func TestEventBetweenMakeJoinAndSendJoinIsNotLost(t *testing.T) {
	deployment := complement.Deploy(t, 1)
	defer deployment.Destroy(t)

	alice := deployment.Register(t, "hs1", helpers.RegistrationOpts{})

	// We track the message event ID sent between make_join and send_join.
	// After send_join, we wait for hs1 to send us either:
	//  - the message event itself, or
	//  - any event whose prev_events reference the message (e.g. a dummy event)
	//
	// atomic.Value is used because messageEventID is written on the main goroutine and
	// read on the HTTP handler goroutine, and needs synchronization (without
	// synchronization, writes are not guaranteed to be observed by other goroutines)
	var messageEventID atomic.Value
	messageEventID.Store("")
	messageDiscoverableWaiter := helpers.NewWaiter()

	srv := federation.NewServer(t, deployment,
		// hs1 fetches our signing keys via /_matrix/key/v2/server to verify our
		// identity before accepting federation requests. Without this handler,
		// make_join is rejected with 401 M_UNAUTHORIZED.
		federation.HandleKeyRequests(),
	)
	// After send_join, hs1 will start sending us federation transactions via
	// /_matrix/federation/v1/send/{txnID}. Since we handle /send manually
	// below, any other requests (e.g. key fetches) that arrive unexpectedly
	// should be tolerated rather than treated as test failures.
	srv.UnexpectedRequestsAreErrors = false

	// Custom /send handler: hs1 will push new room events to us via federation
	// transactions once we've joined. We use a raw handler because the
	// Complement server is not fully in the room until send_join completes, so
	// we can't use HandleTransactionRequests (which requires the room in
	// srv.rooms). Instead we parse the raw transaction body ourselves.
	srv.Mux().Handle("/_matrix/federation/v1/send/{transactionID}", http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		body, err := io.ReadAll(req.Body)
		must.NotError(t, "failed to read request body in /send handler: %v", err)
		txn := gjson.ParseBytes(body)
		txn.Get("pdus").ForEach(func(_, pdu gjson.Result) bool {
			eventID := pdu.Get("event_id").String()
			eventType := pdu.Get("type").String()
			t.Logf("Received PDU via /send: type=%s id=%s", eventType, eventID)

			// messageEventID is set after make_join but before send_join.
			// Transactions can arrive before that window, so skip PDUs that
			// arrive before we know which event to look for.
			msgID, _ := messageEventID.Load().(string)
			if msgID == "" {
				return true
			}

			// Check if this IS the message event (server pushed it directly).
			if eventID == msgID {
				messageDiscoverableWaiter.Finish()
				return true
			}

			// Check if this event's prev_events directly reference the message (e.g. a dummy
			// event tying the two forward extremities together). If so, the joining server
			// can backfill from that event and will discover the message.
			//
			// XXX: We only check one level of prev_events: if the reference is deeper in the
			// DAG, it's valid and the joining server can still reach the message through
			// backfill but our checks don't account for that yet (feel free to edit this
			// assertion if you run into this)
			pdu.Get("prev_events").ForEach(func(_, prevEvent gjson.Result) bool {
				if prevEvent.String() == msgID {
					messageDiscoverableWaiter.Finish()
					return false
				}
				return true
			})

			return true
		})
		w.WriteHeader(200)
		// Respond with an empty PDU error map, which is the federation /send
		// success response format: each key would be a PDU ID whose processing
		// failed; an empty object means all PDUs were accepted.
		w.Write([]byte(`{"pdus":{}}`))
	})).Methods("PUT")

	cancel := srv.Listen()
	defer cancel()

	// Alice creates a room on hs1.
	roomID := alice.MustCreateRoom(t, map[string]interface{}{
		"preset": "public_chat",
	})

	charlie := srv.UserID("charlie")
	origin := srv.ServerName()
	fedClient := srv.FederationClient(deployment)

	// Step 1: make_join, hs1 returns a join event template whose prev_events
	// reflect the current room DAG tips.
	makeJoinResp, err := fedClient.MakeJoin(
		context.Background(), origin,
		deployment.GetFullyQualifiedHomeserverName(t, "hs1"),
		roomID, charlie,
	)
	must.NotError(t, "MakeJoin", err)

	// Step 2: Alice sends a message on hs1. This advances the DAG past the
	// point captured by make_join's prev_events. The Complement server is not
	// yet in the room, so it won't receive this event via normal federation.
	messageEventID.Store(alice.SendEventSynced(t, roomID, b.Event{
		Type: "m.room.message",
		Content: map[string]interface{}{
			"msgtype": "m.text",
			"body":    "Message sent between make_join and send_join",
		},
	}))
	t.Logf("Alice sent message %s between make_join and send_join", messageEventID.Load())

	// Step 3: Build and sign the join event, then send_join.
	// The join event's prev_events are from step 1 (before the message),
	// so persisting it on hs1 creates two forward extremities: the message
	// and the join.
	verImpl, err := gomatrixserverlib.GetRoomVersion(makeJoinResp.RoomVersion)
	must.NotError(t, "GetRoomVersion", err)
	eb := verImpl.NewEventBuilderFromProtoEvent(&makeJoinResp.JoinEvent)
	joinEvent, err := eb.Build(time.Now(), srv.ServerName(), srv.KeyID, srv.Priv)
	must.NotError(t, "Build join event", err)

	_, err = fedClient.SendJoin(
		context.Background(), origin,
		deployment.GetFullyQualifiedHomeserverName(t, "hs1"),
		joinEvent,
	)
	must.NotError(t, "SendJoin", err)

	// Step 4: hs1 should make the missed message discoverable to the joining
	// server. We accept either receiving the message event directly, or
	// receiving any event whose prev_events reference it (allowing the
	// joining server to backfill).
	messageDiscoverableWaiter.Waitf(t, 5*time.Second,
		"Timed out waiting for message event %s to become discoverable — "+
			"the event sent between make_join and send_join was lost to the "+
			"joining server", messageEventID.Load(),
	)
}
