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
	"testing"

	dockerClient "github.com/docker/docker/client"
	"github.com/element-hq/synapse/tests/internal/dockerutil"
	"github.com/matrix-org/complement"
	"github.com/matrix-org/complement/client"
	"github.com/matrix-org/complement/helpers"
	"github.com/matrix-org/complement/match"
	"github.com/matrix-org/complement/must"
)

const REFRESH_TOKEN_CONFIG string = `
refreshable_access_token_lifetime: 1h
refresh_token_lifetime: 26w
`

// TestLogoutInvalidatesRefreshableAccessToken logs in, gets a refreshable
// access token, calls /sync, logs the device out, then tries to call /sync
// again.
// 
// It should fail with a 401.
func TestLogoutInvalidatesRefreshableAccessToken(t *testing.T) {
	deployment := deployWithRefreshTokens(t)
	defer deployment.Destroy(t)

	user, _ := loginWithRefreshToken(t, deployment, "refreshable-access-token-logout")

	// Populate the access token lookup cache before logging out.
	user.MustDo(t, "GET", []string{"_matrix", "client", "v3", "sync"})

	user.MustDo(t, "POST", []string{"_matrix", "client", "v3", "logout"})

	res := user.Do(t, "GET", []string{"_matrix", "client", "v3", "sync"})
	must.MatchResponse(t, res, match.HTTPResponse{
		StatusCode: http.StatusUnauthorized,
		JSON: []match.JSON{
			match.JSONKeyEqual("errcode", "M_UNKNOWN_TOKEN"),
		},
	})
}

// TestLougoutInvalidatesRefreshTokens logs in and gets a refresh token and a
// refreshable access token.
//
// It then logs the token out, and tries to refresh the token. Refreshing should
// fail with a 401.
func TestLogoutInvalidatesRefreshToken(t *testing.T) {
	deployment := deployWithRefreshTokens(t)
	defer deployment.Destroy(t)

	user, refreshToken := loginWithRefreshToken(t, deployment, "refresh-token-logout")

	// Populate the access token lookup cache before logging out.
	user.MustDo(t, "GET", []string{"_matrix", "client", "v3", "sync"})

	user.MustDo(t, "POST", []string{"_matrix", "client", "v3", "logout"})

	unauthedClient := deployment.UnauthenticatedClient(t, "hs1")
	res := unauthedClient.Do(
		t,
		"POST",
		[]string{"_matrix", "client", "v3", "refresh"},
		client.WithJSONBody(t, map[string]interface{}{
			"refresh_token": refreshToken,
		}),
	)
	must.MatchResponse(t, res, match.HTTPResponse{
		StatusCode: http.StatusUnauthorized,
		JSON: []match.JSON{
			match.JSONKeyEqual("errcode", "M_UNKNOWN_TOKEN"),
		},
	})
}

func deployWithRefreshTokens(t *testing.T) complement.Deployment {
	t.Helper()

	deployment := complement.Deploy(t, 1)

	dc, err := dockerClient.NewClientWithOpts(
		dockerClient.FromEnv,
		dockerClient.WithAPIVersionNegotiation(),
	)
	must.NotError(t, "failed creating docker client", err)

	err = dockerutil.WriteFileIntoContainer(
		t,
		dc,
		deployment.ContainerID(t, "hs1"),
		"/conf/homeserver.d/refresh_tokens.yaml",
		[]byte(REFRESH_TOKEN_CONFIG),
	)
	must.NotError(t, "failed writing refresh token config", err)

	deployment.StopServer(t, "hs1")
	deployment.StartServer(t, "hs1")

	return deployment
}

func loginWithRefreshToken(
	t *testing.T,
	deployment complement.Deployment,
	localpartSuffix string,
) (*client.CSAPI, string) {
	t.Helper()

	password := "complement_meets_min_password_req"
	user := deployment.Register(t, "hs1", helpers.RegistrationOpts{
		LocalpartSuffix: localpartSuffix,
		Password:        password,
	})

	var refreshToken string
	user.UserID, user.AccessToken, refreshToken, user.DeviceID, _ = user.LoginUserWithRefreshToken(
		t,
		user.UserID,
		password,
	)

	return user, refreshToken
}
