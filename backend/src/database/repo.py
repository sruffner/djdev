"""
repo.py: Implementation of the portal backing repository in a single bucket in AWS Single Storage Service (S3).

TODO: UNDER DEVELOPMENT - Testing access to an S3 bucket I created using my own AWS Free Tier account.
  Update 2/16/22 -- Got basic functionality working: list, download, upload, delete. Once Duke S3 bucket is made
  available, need to replace relevant environment variables with the appropriate values for accessing that bucket.
  See environment/.env-compose

@author: sruffner
@created: 15feb2022
"""
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List, Tuple

from boto3 import Session
from boto3.s3.transfer import TransferConfig

from config.config import get_config, get_application_logger

MB = 1024 ** 2
""" Number of bytes in a megabyte. """
GB = 1024 ** 3
""" Number of bytes in a gigabyte. """


def aws_session(region_name: str = 'us-west-1') -> Optional[Session]:
    """
    Generate an authenticated AWS session object for accessing AWS services like S3.

    Args:
        region_name: AWS region name. Default: 'us-west-1'.
    Returns:
        The session object, or None if no authentication credentials found.
    """
    cfg = get_config()
    if (not cfg.aws_access_key_id) or (not cfg.aws_access_key_secret):
        get_application_logger().error("Cannot open AWS session - Missing access credentials.")
        return None
    return Session(cfg.aws_access_key_id, cfg.aws_access_key_secret, region_name=region_name)


def existing_buckets() -> None:
    """
    Print the names of all available buckets in S3 to STDOUT.
    """
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        print('Existing buckets: ')
        for bucket in s3_resource.buckets.all():
            print(f"  {bucket.name}")
        print('\n\n', flush=True)
    except Exception:
        get_application_logger().error("ERROR: Failed to list existing buckets in S3 account", exc_info=True)
        print('Sorry, an error occurred.\n\n', flush=True)


def upload_file_to_bucket(file_path: Path, bucket_name: str, key: str) -> bool:
    xfer_cfg = TransferConfig(multipart_threshold=50*MB, multipart_chunksize=50*MB)
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        bucket.upload_file(Filename=str(file_path), Key=key,
                           Callback=ProgressToConsole(file_path), Config=xfer_cfg)
        return True
    except Exception:
        get_application_logger().error(f"Failed to upload file {file_path} to S3 bucket {bucket_name}", exc_info=True)
        return False


def presigned_url_for_file(bucket_name: str, key: str, expires: int = 3600) -> Tuple[bool, str]:
    """
    Generate a presigned URL by which the specified file may be downloaded from the specified S3 bucket.

    Args:
        bucket_name: THe bucket name.
        key: The file object key.
        expires: Expiration time for the URL, in seconds. Range 1-86400 (24 hours). Default = 3600 (1 hour).
    Returns:
        A 2-tuple: (False, error message) if operation fails; (True, URL string) otherwise.
    """
    try:
        session = aws_session()
        s3_client = session.client('s3')
        url = s3_client.generate_presigned_url(ClientMethod='get_object', Params={'Bucket': bucket_name, 'Key': key},
                                               ExpiresIn=expires)
        return True, url
    except Exception:
        get_application_logger().error(f"Failed to generate presigned URL for {key} in S3 bucket {bucket_name}",
                                       exc_info=True)
        return False, "Unable to generate download URL - file does not exist or internal error"


def download_file_from_bucket(bucket_name: str, key: str, dst: Path) -> bool:
    xfer_cfg = TransferConfig(multipart_threshold=50*MB, multipart_chunksize=50*MB)
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        obj = s3_resource.Object(bucket_name, key)
        obj.load()
        s3_resource.Object(bucket_name, key).download_file(
            Filename=str(dst), Callback=ProgressToConsole(dst, download_size=obj.content_length), Config=xfer_cfg
        )
        if dst.is_file():
            return True
        else:
            get_application_logger().error(
                f"File downloaded from S3 successfully not found at specified destination {str(dst)}")
            return False
    except Exception:
        get_application_logger().error(f"Failed to download object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def delete_file_in_bucket(bucket_name: str, key: str) -> bool:
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        s3_resource.Object(bucket_name, key).delete()
        return True
    except Exception:
        get_application_logger().error(f"Failed to delete object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def bucket_contents(bucket_name: str) -> Optional[List]:
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        obj_list = [obj for obj in bucket.objects.all()]
        return obj_list
    except Exception:
        get_application_logger().error(f"Failed to get object listing for S3 bucket {bucket_name}", exc_info=True)
        return None


class ProgressToConsole(object):
    def __init__(self, file_path: Path, download_size: Optional[float] = None):
        self._path: Path = file_path
        self._size = download_size if download_size else file_path.stat().st_size
        self._size_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, num_bytes):
        with self._lock:
            self._size_so_far += num_bytes
            percentage = (self._size_so_far / float(self._size)) * 100
            sys.stdout.write(f"\r{self._path.name}  {self._size_so_far}/{self._size} ({percentage:.2f}%)")
            sys.stdout.flush()


def _print_usage() -> None:
    print("\nAvailable commands:\n"
          "   l = List all file objects in bucket.\n"
          "   u = Upload a file object to bucket.\n"
          "   d = Download a file object from bucket.\n"
          "   x = Delete o file object in bucket.\n"
          "   h = Print this usage message.\n"
          "   q = Quit.\n\n", file=sys.stdout, flush=True)


def _process_command() -> bool:
    command = input('Enter command (l, u, d, x, h, q) > ')
    error_msg = None
    bucket_name = get_config().repo_bucket
    if command == 'l':
        contents = bucket_contents(bucket_name)
        if not contents:
            error_msg = "Unable to list bucket contents"
        else:
            print(f"{'KEY':50} {'STORAGE CLASS':30} {'SIZE (MiB)':15} {'LAST_MODIFIED':30}")
            print(f"{'---':50} {'-------------':30} {'----------':15} {'-------------':30}")
            for o in contents:
                print(f"{o.key:50} {o.storage_class:30} {float(o.size)/MB:<15.1f} "
                      f"{o.last_modified.strftime('%m-%d-%Y %H:%M:%S %Z'):30}")
    elif command == 'u':
        file_path = Path(input('Enter full path to file to be uploaded > '))
        prefix = input('Enter path-like prefix, eg "/repo/folder1" (can be empty string) > ')
        if prefix is None:
            prefix = ""
        if (len(prefix) > 0) and not prefix.endswith('/'):
            prefix += "/"
        if not file_path.is_file():
            error_msg = 'Bad file path.'
        elif file_path.stat().st_size > 3*GB:
            error_msg = 'Sorry, file size must be less than 3GB'
        else:
            t_start = time.time()
            ok = upload_file_to_bucket(file_path, bucket_name, f"{prefix}{file_path.name}")
            t = time.time() - t_start
            if ok:
                print(f"\nDone. {file_path.stat().st_size/MB:.1f}MB uploaded in {t:.3f} seconds.")
            else:
                error_msg = "Upload failed."
    elif command == 'd':
        dst_file = Path(input('Enter destination path (including filename) for download > '))
        obj_key = input('Enter object key in full > ')
        if dst_file.exists() or not dst_file.parent.is_dir():
            error_msg = 'Cannot overwrite existing file, or parent directory does not exist'
        else:
            t_start = time.time()
            ok = download_file_from_bucket(bucket_name, obj_key, dst_file)
            t = time.time = t_start
            if ok:
                print(f"\nDone. {dst_file.stat().st_size/MB:.1f}MB downloaded in {t:.3f} seconds.")
            else:
                error_msg = "Download failed."
    elif command == 'x':
        obj_key = input('Enter object key in full > ')
        if not delete_file_in_bucket(bucket_name, obj_key):
            error_msg = "Delete operation failed."
    elif command == 'h':
        _print_usage()
    elif command == 'q':
        return True
    else:
        error_msg = f"Unrecognized command: {command}. Try again."

    print(f"ERROR: {error_msg}\n\n" if isinstance(error_msg, str) else "OK.\n\n", file=sys.stdout, flush=True)
    return False


# To run this module on the backend container: 'docker-compose run backend python -m database.repo
if __name__ == '__main__':
    print("Testing programmatic access to S3...\n")
    existing_buckets()
    _print_usage()

    done = False
    while not done:
        done = _process_command()

    print("\n\nBYE!", file=sys.stdout, flush=True)
