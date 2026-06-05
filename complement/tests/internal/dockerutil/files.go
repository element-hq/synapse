package dockerutil

import (
	"archive/tar"
	"bytes"
	"fmt"
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
)

// Write `data` as a file into a container at the given `path`.
//
// Internally, produces an uncompressed single-file tape archive (tar) that is sent to the docker
// daemon to be unpacked into the container filesystem.
func WriteFileIntoContainer(
	t *testing.T,
	docker *client.Client,
	containerID string,
	path string,
	data []byte,
) error {
	// Create a fake/virtual tar file in memory that we can copy to the container
	// via https://stackoverflow.com/a/52131297/796832
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	err := tw.WriteHeader(&tar.Header{
		Name: path,
		Mode: 0777,
		Size: int64(len(data)),
	})
	if err != nil {
		return fmt.Errorf(
			"WriteIntoContainer: failed to write tarball header for %s: %v",
			path,
			err,
		)
	}
	_, err = tw.Write([]byte(data))
	if err != nil {
		return fmt.Errorf("WriteIntoContainer: failed to write tarball data for %s: %w", path, err)
	}

	err = tw.Close()
	if err != nil {
		return fmt.Errorf(
			"WriteIntoContainer: failed to close tarball writer for %s: %w",
			path,
			err,
		)
	}

	// Put our new fake file in the container volume
	err = docker.CopyToContainer(
		t.Context(),
		containerID,
		"/",
		&buf,
		container.CopyToContainerOptions{
			AllowOverwriteDirWithFile: false,
		},
	)
	if err != nil {
		return fmt.Errorf("WriteIntoContainer: failed to copy: %s", err)
	}
	return nil
}
