#!/usr/bin/env python3
"""
Cloudflare R2 存储模块（可选）
用于将教材 PDF 同步至云存储
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class R2Storage:
    def __init__(self):
        self.endpoint_url = os.environ.get("R2_ENDPOINT_URL")
        self.access_key_id = os.environ.get("R2_ACCESS_KEY_ID")
        self.secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY")
        self.bucket_name = os.environ.get("R2_BUCKET_NAME")
        self.public_url = os.environ.get("R2_PUBLIC_URL", "")
        self.client = None

        if self._is_configured():
            self._init_client()

    def _is_configured(self) -> bool:
        return all([
            self.endpoint_url,
            self.access_key_id,
            self.secret_access_key,
            self.bucket_name,
        ])

    def _init_client(self):
        try:
            import boto3
            self.client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",
            )
            logger.info("R2 存储已初始化")
        except Exception as e:
            logger.error(f"R2 初始化失败: {e}")
            self.client = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def upload_file(self, local_path: str, remote_key: str) -> str | None:
        if not self.enabled:
            return None
        try:
            self.client.upload_file(local_path, self.bucket_name, remote_key)
            url = f"{self.public_url}/{remote_key}" if self.public_url else None
            logger.info(f"已上传至 R2: {remote_key}")
            return url
        except Exception as e:
            logger.error(f"上传失败 [{remote_key}]: {e}")
            return None

    def upload_pdf(self, local_path: str, filename: str) -> str | None:
        key = f"textbooks/{filename}"
        return self.upload_file(local_path, key)

    def upload_cover(self, local_path: str, filename: str) -> str | None:
        key = f"covers/{filename}"
        return self.upload_file(local_path, key)

    def file_exists(self, remote_key: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=remote_key)
            return True
        except Exception:
            return False

    def get_public_url(self, remote_key: str) -> str:
        return f"{self.public_url}/{remote_key}" if self.public_url else ""
