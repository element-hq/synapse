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
	"net/http"
	"os/exec"
	"strings"
	"testing"

	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/match"
	"github.com/matrix-org/complement/must"
	"github.com/tidwall/gjson"
)

func TestSynapseVersion(t *testing.T) {
	deployment := complement.Deploy(t, 1)
	defer deployment.Destroy(t)

	unauthedClient := deployment.UnauthenticatedClient(t, "hs1")

	// Sanity check that the version of Synapse used in the `COMPLEMENT_BASE_IMAGE`
	// matches the same git commit we have checked out. This ensures that the image being
	// used in Complement is the one that we just built locally with `complement.sh`
	// instead of accidentally pulling in some remote one.
	//
	// This test is expected to pass if you use `complement.sh`.
	//
	// If this test fails, it probably means that Complement is using an image that
	// doesn't encompass the changes you have checked out (unexpected). We want to yell
	// loudly and point out what's wrong instead of silently letting your PRs pass
	// without actually being tested.
	t.Run("Synapse version matches current git checkout", func(t *testing.T) {
		// Get the Synapse version details of the current git checkout
		checkoutSynapseVersion := runCommand(
			t,
			[]string{
				"poetry",
				"run",
				"python",
				"-c",
				"from synapse.util import SYNAPSE_VERSION; print(SYNAPSE_VERSION)",
			},
		)

		// Find the version details of the Synapse instance deployed from the Docker image
		res := unauthedClient.MustDo(t, "GET", []string{"_matrix", "federation", "v1", "version"})
		body := must.MatchResponse(t, res, match.HTTPResponse{
			StatusCode: http.StatusOK,
			JSON: []match.JSON{
				match.JSONKeyPresent("server"),
			},
		})
		rawSynapseVersionString := gjson.GetBytes(body, "server.version").Str
		t.Logf(
			"Synapse version string from federation version endpoint: %s",
			rawSynapseVersionString,
		)

		must.Equal(
			t,
			rawSynapseVersionString,
			checkoutSynapseVersion,
			"Synapse version in the checkout doesn't match the Synapse version that Complement is running. "+
				"If this test fails, it probably means that Complement is using an image that "+
				"doesn't encompass the changes you have checked out (unexpected). We want to yell "+
				"loudly and point out what's wrong instead of silently letting your PRs pass "+
				"without actually being tested.",
		)
	})
}

// runCommand will run the given command and return the stdout (whitespace
// trimmed).
func runCommand(t *testing.T, commandPieces []string) string {
	t.Helper()

	// Then run our actual command
	cmd := exec.Command(commandPieces[0], commandPieces[1:]...)
	output, err := cmd.Output()
	if err != nil {
		t.Fatalf(
			"runCommand: failed to run command (%s): %v",
			strings.Join(commandPieces, " "),
			err,
		)
	}

	return strings.TrimSpace(string(output))
}
