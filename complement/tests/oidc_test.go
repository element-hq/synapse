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
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"

	dockerClient "github.com/docker/docker/client"
	"github.com/element-hq/synapse/tests/internal/dockerutil"
	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/client"
	"github.com/matrix-org/complement/must"
)

const OIDC_CONFIG string = `
public_baseurl: "http://localhost:8008"
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
// Instead of failing to start, Synapse will produce a 503 response on the
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

	containerID := deployment.ContainerID(t, "hs1")

	// Configure the OIDC Provider by writing a config fragment
	err = dockerutil.WriteFileIntoContainer(
		t,
		dc,
		containerID,
		"/conf/homeserver.d/oidc_provider.yaml",
		[]byte(OIDC_CONFIG),
	)
	if err != nil {
		t.Fatalf("Failed to write updated config to container: %v", err)
	}

	// Wait for the homeserver to be ready again
	unauthedClient := deployment.UnauthenticatedClient(t, "hs1")

	res := unauthedClient.Do(t, "GET", []string{"_matrix", "client", "versions"})
	if res != nil && res.StatusCode == http.StatusOK {
		res.Body.Close()
	}

	// Restart the homeserver to apply the new config
	deployment.StopServer(t, "hs1")
	// Careful: port number changes here
	deployment.StartServer(t, "hs1")
	// Must get after the restart so the port number is correct
	unauthedClient = deployment.UnauthenticatedClient(t, "hs1")

	// Test that trying to log in with an OIDC provider that is down
	// causes an HTML error page to be shown to the user instead of the redirect.
	//
	// More importantly, implicitly tests that Synapse can start up
	// and answer requests even though an OIDC provider is down.
	t.Run("login_sso_redirect_shows_html_error", func(t *testing.T) {
		t.Parallel()

		// Build a request to the /redirect/ endpoint, that would normally be navigated to
		// by the user's browser in order to start the login flow.
		queryParams := url.Values{}
		queryParams.Add("redirectUrl", "http://redirect.invalid/redirect")
		res := unauthedClient.Do(t, "GET", []string{"_matrix", "client", "v3", "login", "sso", "redirect", "oidc-test_provider"},
			client.WithQueries(queryParams),
		)

		// Should get a 503
		if res.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("Expected 503 Service Unavailable, got %d.", res.StatusCode)
		}

		// Should get a HTML page
		if res.Header.Get("Content-Type") != "text/html; charset=utf-8" {
			t.Fatalf("Expected Content-Type of 'text/html; charset=utf-8', got %s.", res.Header.Get("Content-Type"))
		}

		body, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatalf("Couldn't read body: %s", err)
		}
		bodyText := string(body)

		// The HTML page contains phrases from the template we expect
		if !strings.Contains(bodyText, "login provider is unavailable right now") {
			t.Fatalf("Keyword not found in HTML error page, got %s", bodyText)
		}
	})
}
