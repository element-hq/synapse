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
	"net/url"
	"strings"
	"testing"

	dockerClient "github.com/docker/docker/client"
	"github.com/element-hq/synapse/tests/internal/dockerutil"
	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/client"
	"github.com/matrix-org/complement/match"
	"github.com/matrix-org/complement/must"
)

const OIDC_HOMESERVER_CONFIG string = `
oidc_providers:
 - idp_id: "test_provider"
   idp_name: "Test OIDC Provider"
   issuer: "https://example.invalid"
   client_id: "test_client_id"
   client_secret: "test_secret"
   scopes: ["openid"]
   discover: true
   user_mapping_provider:
     module: "synapse.handlers.oidc.JinjaOidcMappingProvider"
     config:
       display_name_template: "{{ user.given_name }}"
       email_template: "{{ user.email }}"
`

// Test that Synapse still starts up when configured with an OIDC provider that is unavailable.
//
// This is a regression test: Synapse previously would fail to start up
// at all if the OIDC provider was down on startup.
// https://github.com/element-hq/synapse/issues/8088
//
// Now instead of failing to start, Synapse will produce a 503 response on the
// `/_matrix/client/v3/login/sso/redirect/oidc-test_provider` endpoint.
func TestOIDCProviderUnavailable(t *testing.T) {
	// Deploy a single homeserver
	deployment := complement.Deploy(t, 1)
	defer deployment.Destroy(t)

	// Get Docker client to manipulate container
	dc, err := dockerClient.NewClientWithOpts(
		dockerClient.FromEnv,
		dockerClient.WithAPIVersionNegotiation(),
	)
	must.NotError(t, "failed creating docker client", err)

	// Configure the OIDC Provider by writing a config fragment
	err = dockerutil.WriteFileIntoContainer(
		t,
		dc,
		deployment.ContainerID(t, "hs1"),
		"/conf/homeserver.d/oidc_provider.yaml",
		[]byte(OIDC_HOMESERVER_CONFIG),
	)
	if err != nil {
		t.Fatalf("Failed to write updated config to container: %v", err)
	}

	// Restart the homeserver to apply the new config
	deployment.StopServer(t, "hs1")
	// Careful: port number changes here
	deployment.StartServer(t, "hs1")
	// Must get after the restart so the port number is correct
	unauthedClient := deployment.UnauthenticatedClient(t, "hs1")

	// Test that trying to log in with an OIDC provider that is down
	// causes an HTML error page to be shown to the user.
	// (This replaces the redirect that would happen if the provider was
	// up.)
	//
	// More importantly, implicitly tests that Synapse can start up
	// and answer requests even though an OIDC provider is down.
	t.Run("/login/sso/redirect shows HTML error", func(t *testing.T) {
		// Build a request to the /redirect/ endpoint, that would normally be navigated to
		// by the user's browser in order to start the login flow.
		queryParams := url.Values{}
		queryParams.Add("redirectUrl", "http://redirect.invalid/redirect")
		res := unauthedClient.Do(
			t,
			"GET",
			[]string{"_matrix", "client", "v3", "login", "sso", "redirect", "oidc-test_provider"},
			client.WithQueries(queryParams),
		)

		body := must.MatchResponse(t, res, match.HTTPResponse{
			// Should get a 503
			StatusCode: http.StatusServiceUnavailable,

			Headers: map[string]string{
				// Should get an HTML page explaining the problem to the user
				"Content-Type": "text/html; charset=utf-8",
			},
		})

		bodyText := string(body)

		// The HTML page contains phrases from the template we expect
		if !strings.Contains(bodyText, "login provider is unavailable right now") {
			t.Fatalf("Keyword not found in HTML error page, got %s", bodyText)
		}
	})
}
