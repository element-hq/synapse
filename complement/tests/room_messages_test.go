// This file is licensed under the Affero General Public License (AGPL) version 3.
//
// Copyright (C) 2026 Element Creations Ltd
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// See the GNU Affero General Public License for more details:
// <https://www.gnu.org/licenses/agpl-3.0.html>.

package synapse_tests

import (
	"encoding/json"
	"fmt"
	"net/url"
	"slices"
	"strings"
	"testing"

	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/b"
	"github.com/matrix-org/complement/client"
	"github.com/matrix-org/complement/helpers"
	"github.com/matrix-org/gomatrixserverlib/spec"
	"github.com/tidwall/gjson"
)

func TestMessagesOverFederation(t *testing.T) {
	deployment := complement.Deploy(t, 2)
	defer deployment.Destroy(t)

	alice := deployment.Register(t, "hs1", helpers.RegistrationOpts{
		LocalpartSuffix: "alice",
	})
	bob := deployment.Register(t, "hs2", helpers.RegistrationOpts{
		LocalpartSuffix: "bob",
	})

	// The typical convention to find backfill points is from the backward extremities in
	// the DAG. Backward extremities are the oldest events we know of in the room but we
	// only know of them because some other event referenced them by prev_event and aren't
	// known to the homeserver yet (meaning we don't know their depth specifically). So we
	// can only do approximate depth comparisons (use the depth of the known events
	// they're connected to). And we don't know if those backward extremities point to a
	// long chain/fork of history that could stretch back far enough to be visible.
	//
	// This means a naive homeserver implementation that looks for backward extremities <=
	// depth of the `/messages?dir=b&from=xxx` token may overlook a backfill point that could
	// reveal more history in the window the user is currently paginating in.
	//
	// This could be a near miss as this test is specifically stressing or a more deep miss
	// as the backward extremity could reveal an entire fork of history that stretches
	// back far enough to be visible.
	//
	// In Synapse, we consider "nearby" as anything within range of the `limit` specified
	// in `/messages?dir=b&from=xxx&limit=xxx`.
	//
	// This test lives in our in-repo Complement tests for Synapse because the Matrix spec
	// doesn't have any rules for how a homeserver should backfill. Practically speaking,
	// homeservers that don't do anything for this problem will just hide messages from
	// clients. This underscores the fact why it's necessary for homeservers to indicate that
	// there is a gap (using MSC3871) at the very least.
	//
	// --------------------------------------------------
	//
	// Even with MSC3871 gaps, the tested behavior here is necessary as the gap prev/next
	// tokens point before/after the event (remember: tokens are positions between
	// events), so if you use `/messages?dir=b&from=<gap prev_pagination_token>`, we can't
	// rely on naive depth comparison. MSC3871 Complement tests will also exercise this.
	// Example:
	//
	// t0      t1       t2       t3               t4
	//    [A] <--- [B] <--- [C] <--- [bob join 4]
	//
	// When Bob calls `/messages?dir=b&backfill=false`, he sees a gap (`{ event_id: "bob
	// join 4", prev_pagination_token: "t3", next_pagination_token: "t4" }`) and tries to
	// fill it in with `/messages?dir=b&from=t3&limit=10&backfill=true`. To find backfill
	// points, Synapse will compare `t3` with the backward extremity at an approximate
	// depth of 4. Which is why we take `t3`, add the `limit=10` and then do the
	// comparison (find any backfill points with an approximate depth <= 13).
	t.Run("Backfill from nearby backward extremities past token", func(t *testing.T) {
		// Alice creates the room
		roomID := alice.MustCreateRoom(t, map[string]interface{}{
			// The `public_chat` preset includes `history_visibility: "shared"` ("Previous
			// events are always accessible to newly joined members. All events in the
			// room are accessible, even those sent when the member was not a part of the
			// room."), which is what we want to test.
			"preset": "public_chat",
		})

		// Keep track of the order
		eventIDs := make([]string, 0)
		// Map from event_id to event info
		eventMap := make(map[string]EventInfo)

		// Send some message history into the room
		numberOfMessagesToSend := 3
		messageDrafts := make([]MessageDraft, 0, numberOfMessagesToSend)
		for i := 0; i < numberOfMessagesToSend; i++ {
			messageDrafts = append(
				messageDrafts,
				MessageDraft{alice, fmt.Sprintf("message history %d", i+1)},
			)
		}
		sendAndTrackMessages(t, roomID, messageDrafts, &eventIDs, &eventMap)

		// Bob joins the room
		bob.MustJoinRoom(t, roomID, []spec.ServerName{
			deployment.GetFullyQualifiedHomeserverName(t, "hs1"),
		})
		bobJoinEventID := getStateID(t, bob, roomID, "m.room.member", bob.UserID)

		// Make it easy to cross-reference the events being talked about in the logs
		for eventIndex, eventID := range eventIDs {
			t.Logf("Message %d -> event_id=%s", eventIndex, eventID)
		}

		// Use a `/context` request to get a pagination token just before Bob's join event
		// (remember: tokens are positions between events)
		//
		// Usually a client would just use `/messages?dir=b` to start getting history
		// after joining but this is valid as well. To illustrate a more real example of
		// this, someone can use `/timestamp_to_event` to jump back in history and
		// `/context` to start paginating history.
		contextRes := bob.MustDo(
			t,
			"GET",
			[]string{"_matrix", "client", "v3", "rooms", roomID, "context", bobJoinEventID},
			client.WithContentType("application/json"),
			client.WithQueries(url.Values{
				"limit": []string{"0"},
			}),
		)
		contextResResBody := client.ParseJSON(t, contextRes)
		// > `start`: A token that can be used to paginate backwards with.
		// > - https://spec.matrix.org/v1.17/client-server-api/#get_matrixclientv3roomsroomidcontexteventid
		paginationToken := client.GetJSONFieldStr(t, contextResResBody, "start")

		// Paginate backwards from the join event
		messagesRes := bob.MustDo(
			t,
			"GET",
			[]string{"_matrix", "client", "v3", "rooms", roomID, "messages"},
			client.WithContentType("application/json"),
			client.WithQueries(url.Values{
				"dir":   []string{"b"},
				"limit": []string{"100"},
				"from":  []string{paginationToken},
			}),
		)
		messagesResBody := client.ParseJSON(t, messagesRes)

		// Since `dir=b`, these will be in reverse chronological order
		actualEventIDsFromRequest := extractEventIDsFromMessagesResponse(t, messagesResBody)

		// Put them in chronological order to match the expected list
		chronologicalActualEventIds := slices.Clone(actualEventIDsFromRequest)
		slices.Reverse(chronologicalActualEventIds)

		// Assert timeline order
		assertEventsInOrder(t, chronologicalActualEventIds, eventIDs)
	})

	// TODO: Backfill test to make sure we backfill from forks when viewing history (see
	// docstring above).
	//
	//  1. Alice (hs1, engineered homeserver) creates a room with events A, B
	//  1. Bob (hs2) joins the room
	//  1. Bob leaves the room
	//  1. Alice creates a fork from A with some history (1, 2, 3) and connects it back with a new event C
	//  1. Bob joins back
	//  1. Bob paginates `/messages?dir=b&from=<token-after-b>`
	//  1. Ensure Bob sees events: B, 2, 1, A
	//
	//   1 <--- 2 <----- 3
	//  /                 \
	// A <------- B ▲ <--- C <-- D
	//              |
	//              Paginate backwards from this point
	// t.Run("Backfill from nearby backward extremities past token (fork)", func(t *testing.T) {
}

// These utilities match what we're using in the Complement repo (see
// `matrix-org/complement` -> `tests/csapi/room_messages_test.go`)

type MessageDraft struct {
	Sender  *client.CSAPI
	Message string
}

type EventInfo struct {
	MessageDraft MessageDraft
	EventID      string
}

func sendMessageDrafts(
	t *testing.T,
	roomID string,
	messageDrafts []MessageDraft,
) []string {
	t.Helper()

	eventIDs := make([]string, len(messageDrafts))
	for messageDraftIndex, messageDraft := range messageDrafts {
		eventID := messageDraft.Sender.SendEventSynced(t, roomID, b.Event{
			Type: "m.room.message",
			Content: map[string]interface{}{
				"msgtype": "m.text",
				"body":    messageDraft.Message,
			},
		})
		eventIDs[messageDraftIndex] = eventID
	}

	return eventIDs
}

// sendAndTrackMessages sends the given message drafts to the room, keeping track of the
// new events in the list of `eventIDs` and `eventMap`. Returns the list of new event
// IDs that were sent.
func sendAndTrackMessages(
	t *testing.T,
	roomID string,
	messageDrafts []MessageDraft,
	eventIDs *[]string,
	eventMap *map[string]EventInfo,
) []string {
	t.Helper()

	newEventIDs := sendMessageDrafts(t, roomID, messageDrafts)

	*eventIDs = append(*eventIDs, newEventIDs...)
	for i, eventID := range newEventIDs {
		(*eventMap)[eventID] = EventInfo{
			MessageDraft: messageDrafts[i],
			EventID:      eventID,
		}
	}

	return newEventIDs
}

// extractEventIDsFromMessagesResponse extracts the event IDs from the given
// `/messages` response body.
func extractEventIDsFromMessagesResponse(
	t *testing.T,
	messagesResBody json.RawMessage,
) []string {
	t.Helper()

	wantKey := "chunk"
	keyRes := gjson.GetBytes(messagesResBody, wantKey)
	if !keyRes.Exists() {
		t.Fatalf("extractEventIDsFromMessagesResponse: missing key '%s'", wantKey)
	}
	if !keyRes.IsArray() {
		t.Fatalf(
			"extractEventIDsFromMessagesResponse: key '%s' is not an array (was %s)",
			wantKey,
			keyRes.Type,
		)
	}

	var eventIDs []string
	actualEvents := keyRes.Array()
	for _, event := range actualEvents {
		eventIDs = append(eventIDs, event.Get("event_id").Str)
	}

	return eventIDs
}

func filterEventIDs(t *testing.T, actualEventIDs []string, expectedEventIDs []string) []string {
	t.Helper()

	relevantActualEventIDs := make([]string, 0, len(expectedEventIDs))
	for _, eventID := range actualEventIDs {
		if slices.Contains(expectedEventIDs, eventID) {
			relevantActualEventIDs = append(relevantActualEventIDs, eventID)
		}
	}

	return relevantActualEventIDs
}

// assertEventsInOrder asserts all `actualEventIDs` are present and in order according
// to `expectedEventIDs`. Other unrelated events can be in between.
func assertEventsInOrder(t *testing.T, actualEventIDs []string, expectedEventIDs []string) {
	t.Helper()

	relevantActualEventIDs := filterEventIDs(t, actualEventIDs, expectedEventIDs)

	if len(relevantActualEventIDs) != len(expectedEventIDs) {
		t.Fatalf(
			"expected %d events in timeline (got %d relevant events filtered down from %d events)\n%s",
			len(expectedEventIDs),
			len(relevantActualEventIDs),
			len(actualEventIDs),
			generateEventOrderDiffString(relevantActualEventIDs, expectedEventIDs),
		)
	}

	for i, eventID := range relevantActualEventIDs {
		if eventID != expectedEventIDs[i] {
			t.Fatalf(
				"expected event ID %s (got %s) at index %d\n%s",
				expectedEventIDs[i],
				eventID,
				i,
				generateEventOrderDiffString(relevantActualEventIDs, expectedEventIDs),
			)
		}
	}
}

func generateEventOrderDiffString(actualEventIDs []string, expectedEventIDs []string) string {
	expectedLines := make([]string, len(expectedEventIDs))
	for i, expectedEventID := range expectedEventIDs {
		isExpectedInActual := slices.Contains(actualEventIDs, expectedEventID)
		isMissingIndicatorString := " "
		if !isExpectedInActual {
			isMissingIndicatorString = "?"
		}

		expectedLines[i] = fmt.Sprintf("%2d: %s  %s", i, isMissingIndicatorString, expectedEventID)
	}
	expectedDiffString := strings.Join(expectedLines, "\n")

	actualLines := make([]string, len(actualEventIDs))
	for actualEventIndex, actualEventID := range actualEventIDs {
		isActualInExpected := slices.Contains(expectedEventIDs, actualEventID)
		isActualInExpectedIndicatorString := " "
		if isActualInExpected {
			isActualInExpectedIndicatorString = "+"
		}

		expectedIndex := slices.Index(expectedEventIDs, actualEventID)
		expectedIndexString := ""
		if actualEventIndex != expectedIndex {
			expectedDirectionString := "⬆️"
			if expectedIndex > actualEventIndex {
				expectedDirectionString = "⬇️"
			}

			expectedIndexString = fmt.Sprintf(
				" (expected index %d %s)",
				expectedIndex,
				expectedDirectionString,
			)
		}

		actualLines[actualEventIndex] = fmt.Sprintf("%2d: %s  %s%s",
			actualEventIndex, isActualInExpectedIndicatorString, actualEventID, expectedIndexString,
		)
	}
	actualDiffString := strings.Join(actualLines, "\n")

	return fmt.Sprintf(
		"Actual events ('+' = found expected items):\n%s\nExpected events ('?' = missing expected items):\n%s",
		actualDiffString,
		expectedDiffString,
	)
}

func getStateID(
	t *testing.T,
	c *client.CSAPI,
	roomID string,
	stateType string,
	stateKey string,
) string {
	t.Helper()

	stateRes := c.MustDo(t, "GET", []string{"_matrix", "client", "v3", "rooms", roomID, "state"})
	stateResBody := client.ParseJSON(t, stateRes)
	eventJSON := gjson.ParseBytes(stateResBody)
	if !eventJSON.IsArray() {
		t.Fatalf("expected array of state events but found %s", eventJSON.Type)
	}

	events := eventJSON.Array()

	for _, event := range events {
		if event.Get("type").Str == stateType && event.Get("state_key").Str == stateKey {
			return event.Get("event_id").Str
		}
	}

	t.Fatalf("Unable to find state event for (%s, %s). Room state: %s", stateType, stateKey, events)
	return ""
}
