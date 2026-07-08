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
	"testing"

	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/client"
	"github.com/matrix-org/complement/helpers"
	"github.com/matrix-org/gomatrixserverlib/spec"
)

// Stub test to ensure that homeservers can communicate with each other (federation works correctly).
//
// TODO: This test will disappear once we have other real Synapse specific tests in
// place. This is simply here as an example without bloating the PR with some specific
// new tests.
func TestFederation(t *testing.T) {
	// Create two homeservers
	deployment := complement.Deploy(t, 2)
	defer deployment.Destroy(t)

	alice := deployment.Register(t, "hs1", helpers.RegistrationOpts{})
	bob := deployment.Register(t, "hs2", helpers.RegistrationOpts{})

	aliceRoomID := alice.MustCreateRoom(t, map[string]any{
		"preset": "public_chat",
	})
	bobRoomID := bob.MustCreateRoom(t, map[string]any{
		"preset": "public_chat",
	})

	t.Run("parallel", func(t *testing.T) {
		t.Run("HS1 -> HS2", func(t *testing.T) {
			t.Parallel()

			alice.MustJoinRoom(t, bobRoomID, []spec.ServerName{
				deployment.GetFullyQualifiedHomeserverName(t, "hs2"),
			})

			bob.MustSyncUntil(t, client.SyncReq{}, client.SyncJoinedTo(alice.UserID, bobRoomID))
		})

		t.Run("HS2 -> HS1", func(t *testing.T) {
			t.Parallel()

			bob.MustJoinRoom(t, aliceRoomID, []spec.ServerName{
				deployment.GetFullyQualifiedHomeserverName(t, "hs1"),
			})

			alice.MustSyncUntil(t, client.SyncReq{}, client.SyncJoinedTo(bob.UserID, aliceRoomID))
		})
	})
}
