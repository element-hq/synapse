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
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/match"
	"github.com/matrix-org/complement/must"
)

func TestSynapseVersion(t *testing.T) {
	deployment := complement.Deploy(t, 1)
	defer deployment.Destroy(t)

	unauthedClient := deployment.UnauthenticatedClient(t, "hs1");

	// Sanity check that the version of Synapse used in the `COMPLEMENT_BASE_IMAGE`
	// matches the same git commit we have checked out. This ensures that the image being
	// used in Complement is the one that we just built locally with `complement.sh`
	// instead of accidentally pulling in some remote one.
	//
	// This test is expected to pass if you use `complement.sh`.
	//
	// If this test fails, it probably means that Complement is using an image that
	// doesn't encompass the changes you have checked out (unexpected). We want to yell
	// loudly and point out what's wrong instead of silently letting your PR's pass
	// without actually being tested.
	t.Run("Synapse version matches current git checkout", func(t *testing.T) {
		// TODO: Pull the `version` from `pyproject.toml`.

		// TODO: Get the details of the current git checkout

		// Find the version details of the Synapse instance deployed from the Docker image
		res := unauthedClient.MustDo(t, "GET", []string{"_matrix", "federation", "v1", "version"})
		// Cheeky way to get the response body
		responseBody := must.MatchResponse(t, res, match.HTTPResponse{})
		synapseVersion, err := parseSynapseVersionString(string(responseBody))
		if err != nil {
			t.Fatalf("Failed to parse Synapse version string: %v", err)
		}

		// TODO: Compare
		_ = synapseVersion
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

// parseSynapseVersionString parses Synapse version strings (from
// `GET /_matrix/federation/v1/version`) in the format:
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
	synapseVersionString string,
) (*SynapseVersion, error) {
	// We're trying to separate "1.147.1" and "(...)"
	parts := strings.SplitN(synapseVersionString, " ", 2)

	version := parts[0]
	gitString := ""
	if len(parts) == 2 {
		gitString = parts[1]
		// Remove the surrounding parenthesis (...)
		gitString = strings.TrimPrefix(gitString, "(")
		gitString = strings.TrimSuffix(gitString, ")")
	}

	branch := ""
	tag := ""
	commit := ""
	dirty := false
	gitParts := strings.Split(gitString, ",")

	// Go through piece by piece and try to match it up as best as possible. The pieces
	// should always be in order (branch, tag, commit, dirty) according to the source code
	// that generates them.
	for _, gitPart := range gitParts {
		// Match the most specific key=value pairs first
		if branch == "" && strings.HasPrefix(gitPart, "b=") {
			branch = strings.TrimPrefix(gitPart, "b=")
		} else if tag == "" && strings.HasPrefix(gitPart, "t=") {
			tag = strings.TrimPrefix(gitPart, "t=")
		} else {
			// Otherwise, we have to match remaining the bare pieces.
			//
			// The first bare piece should be the commit
			if commit == "" {
				commit = gitPart
			} else if gitPart == "dirty" {
				dirty = true
			} else {
				return nil, fmt.Errorf("parseSynapseVersionString: Unexpected format of git details (%s)", synapseVersionString)
			}
		}
	}

	return &SynapseVersion{
		Version:      version,
		Branch: branch,
		Tag: tag,
		Commit: commit,
		Dirty: dirty,
	}, nil
}


func TestParseSynapseVersionString(t *testing.T) {
	testCases := []struct {
		name     string
		input    string
		expected *SynapseVersion
		wantErr  bool
	}{
		{
			name:  "simple version",
			input: "1.147.1",
			expected: &SynapseVersion{
				Version: "1.147.1",
				Dirty:   false,
			},
		},
		{
			name:  "version with branch and commit",
			input: "1.147.1 (b=develop,b80774efb2)",
			expected: &SynapseVersion{
				Version: "1.147.1",
				Branch:  "develop",
				Commit:  "b80774efb2",
				Dirty:   false,
			},
		},
		{
			name:  "version with branch, commit, and dirty",
			input: "1.147.1 (b=develop,b80774efb2,dirty)",
			expected: &SynapseVersion{
				Version: "1.147.1",
				Branch:  "develop",
				Commit:  "b80774efb2",
				Dirty:   true,
			},
		},
		{
			name:  "version with HEAD branch, tag, and commit",
			input: "1.147.1 (b=HEAD,t=v1.147.1,7ff8687653)",
			expected: &SynapseVersion{
				Version: "1.147.1",
				Branch:  "HEAD",
				Tag:     "v1.147.1",
				Commit:  "7ff8687653",
				Dirty:   false,
			},
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			got, err := parseSynapseVersionString(testCase.input)
			if err != nil {
				t.Errorf("parseSynapseVersionString(\"%s\") failed to parse input, error: %v", testCase.input, err)
			}

			if !reflect.DeepEqual(got, testCase.expected) {
				t.Errorf("parseSynapseVersionString(\"%s\") got %v, want %v", testCase.input, got, testCase.expected)
			}
		})
	}
}
