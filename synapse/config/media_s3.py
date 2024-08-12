from typing import Any

from synapse.types import JsonDict

from ._base import Config


class MediaS3Config(Config):
    section = "media_s3"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        s3_config = config.get("s3", None)
        if s3_config:
            self.aws_access_key_id = s3_config["aws_access_key_id"]
            self.aws_secret_access_key = s3_config["aws_secret_access_key"]
            self.region_name = s3_config["region_name"]
            self.bucket_name = s3_config["bucket_name"]

        else:
            self.aws_access_key_id = None
            self.aws_secret_access_key = None
            self.region_name = None
            self.bucket_name = None

