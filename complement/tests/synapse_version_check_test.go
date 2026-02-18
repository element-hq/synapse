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
	"net/http"
	"os"
	"os/exec"
	"reflect"
	"regexp"
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
		// Pull the `version` from `pyproject.toml`.
		pyprojectVersion := getVersionFromPyproject(t)

		// Get the details of the current git checkout
		//
		// Commands match https://github.com/matrix-org/matrix-python-common/blob/4084b21af839c50f775447d02ca4f1854e2e6191/src/matrix_common/versionstring.py#L87C45-L95
		gitBranch := runGitCommand(t, []string{"git", "rev-parse", "--abbrev-ref", "HEAD"})
		gitTag := runGitCommand(t, []string{"git", "describe", "--exact-match"})
		gitCommit := runGitCommand(t, []string{"git", "rev-parse", "--short", "HEAD"})
		gitDirty := strings.HasSuffix(
			runGitCommand(t, []string{"git", "describe", "--dirty=-this_is_a_dirty_checkout"}),
			"-this_is_a_dirty_checkout",
		)

		// Assemble our checkout details into a `SynapseVersion` we can easily compare with
		checkoutSynapseVersion := SynapseVersion {
			Version: pyprojectVersion,
			Branch: gitBranch,
			Tag: gitTag,
			Commit: gitCommit,
			Dirty: gitDirty,
		}

		// Find the version details of the Synapse instance deployed from the Docker image
		res := unauthedClient.MustDo(t, "GET", []string{"_matrix", "federation", "v1", "version"})
		body := must.MatchResponse(t, res, match.HTTPResponse{
			StatusCode: http.StatusOK,
			JSON: []match.JSON{
				match.JSONKeyPresent("server"),
			},
		})
		rawSynapseVersionString := gjson.GetBytes(body, "server.version").Str
		t.Logf("Synapse version string from federation version endpoint: %s", rawSynapseVersionString)
		synapseVersion, err := parseSynapseVersionString(rawSynapseVersionString)
		if err != nil {
			t.Fatalf("Failed to parse Synapse version string: %v", err)
		}

		// Compare
		if !reflect.DeepEqual(synapseVersion, checkoutSynapseVersion) {
			t.Fatalf(
				"Synapse version in the checkout doesn't match the Synapse version that Complement is running. " +
				"From the Checkout: %+v, From the Complement image: %+v\n\n" +
				"If this test fails, it probably means that Complement is using an image that " +
				"doesn't encompass the changes you have checked out (unexpected). We want to yell " +
				"loudly and point out what's wrong instead of silently letting your PR's pass " +
				"without actually being tested.", checkoutSynapseVersion, synapseVersion,
			)
		}
	})
}


// Helper function to get `version` from pyproject.toml
func getVersionFromPyproject(t *testing.T) string {
	// Read pyproject.toml file
	//
	// Log the current working directory so it's easier to understand what's going on here
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getVersionFromPyproject: failed to get current working directory: %v", err)
	}
	t.Logf("getVersionFromPyproject: currenty working directory is %s", cwd)
	// This path is relative to this file
	fileContents, err := os.ReadFile("../../pyproject.toml")
	if err != nil {
		t.Fatalf("getVersionFromPyproject: failed to read `pyproject.toml`: %v", err)
	}

	// Instead of parsing toml, we just do a dirty find of the first `version = "1.147.1"`
	re := regexp.MustCompile(
		`(?m)^version = "(?P<Version>.*?)"\s?$`,
	)
	match := re.FindStringSubmatch(string(fileContents))
	if len(match) == 0 {
		t.Fatalf("getVersionFromPyproject: Unable to find `version` in `pyproject.toml`")
	}

	return match[re.SubexpIndex("Version")]
}

// runGitCommand will run the given git command and return the stdout (whitespace
// trimmed).
//
// Errors will be logged but this function will just return a blank string (for
// ease-of-use in the tests)
func runGitCommand(t *testing.T, commandPieces []string) string {
	t.Helper()

	// Then run our actual command
	cmd := exec.Command(commandPieces[0], commandPieces[1:]...)
	output, err := cmd.Output()
	if err != nil {
		t.Logf("runGitCommand: failed to run command (%s) (this may be expected depending on the command): %v", strings.Join(commandPieces, " "), err)
		return ""
	}

	return strings.TrimSpace(string(output))
}

type SynapseVersion struct {
	// Synapse version
	Version string
	// git branch
	Branch string
	// git tag
	Tag string
	// git commit
	Commit string
	// Whether the git repo has uncommitted changes in it
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

// Sanity check that our `parseSynapseVersionString` utility works as expected
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
