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
)

func TestSynapseVersion(t *testing.T) {
	deployment := complement.Deploy(t, 1)
	defer deployment.Destroy(t)

	unauthedClient := deployment.UnauthenticatedClient();

	// Sanity check that the version of Synapse used in the `COMPLEMENT_BASE_IMAGE`
	// matches the same commit we have checked out. This ensures that the image being used
	// in Complement is the one that we just built locally with `complement.sh` instead of
	// accidentally pulling some remote one.
	t.Run("Synapse version matches current git checkout", func(t *testing.T) {
		res := unauthedClient.MustDo(t, "GET", []string{"_matrix", "federation", "v1", "version"})
		// Cheeky way to get the response body
		responseBody := must.MatchResponse(t, res, match.HTTPResponse{})
		synapseVersion := parseSynapseVersionString(string(responseBody))

		// TODO: Get the details of the current checkout

		// TODO: Compare
	})
}

type SynapseVersion struct {
	// TODO
	Version string
	// TODO
	Branch string
	// TODO
	Tag string
	// TODO
	Commit string
	// TODO
	Dirty bool
}

// parseSynapseVersionString parses Synapse version strings in the format:
//
// - "1.147.1"
// - "1.147.1 (b=develop,b80774efb2)"
// - "1.147.1 (b=develop,b80774efb2,dirty)"
// - "1.147.1 (b=HEAD,t=v1.147.1,7ff8687653)"
//
// See
// https://github.com/matrix-org/matrix-python-common/blob/4084b21af839c50f775447d02ca4f1854e2e6191/src/matrix_common/versionstring.py
// for how Synapse generates these version strings.
func parseSynapseVersionString(
	synapseVersionString string
) (*SynapseVersion, error) {
	// TODO
}
