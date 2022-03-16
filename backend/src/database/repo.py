"""
repo.py: Implementation of the portal backing repository in a single bucket in AWS Single Storage Service (S3).

For cost-efficiency's sake, the portal backing repository is implemented using Amazon Web Services' S3. Duke IT has
provisioned an S3 bucket (actually two, one for production and one for development) for the Lisberger lab. Versioning
is disabled on the bucket (we don't need it), and access is granted for the operations we'll need: "s3:GetObject" for
downloading and querying file objects, "s3:PutObject" for uploading file objects, "s3:DeleteObject" to delete any
object in the bucket, and "s3:ListBucket" to traverse all objects in the bucket.

The portal server must supply the AWS Access Key ID and Secret in order to use the Boto3 S3 SDK to perform any actions
on the S3 bucket. These secrets are part of application configuration -- see config.AppConfig.

The S3 bucket can contain any number of file objects and is non-hiearchical storage. Each object is stored under a
string key. However, by design, we use path-like keys for all objects uploaded to the bucket, resulting in a file
system-like folder hierarchy.  TODO - Have methods in this module enforce object key structure?

TODO: Make methods more portal-specific? EG: move_session_archive_to_repo(path), push_data_download_file_to_repo(path)
    [returns presigned URL], etc.???

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


def aws_session() -> Optional[Session]:
    """
    Generate an authenticated AWS session object for accessing AWS services like S3.

    Returns:
        The session object, or None if no authentication credentials found.
    """
    cfg = get_config()
    if (not cfg.aws_access_key_id) or (not cfg.aws_access_key_secret) or (not cfg.aws_region_name):
        get_application_logger().error("Cannot open AWS session - Missing access credentials.")
        return None
    return Session(cfg.aws_access_key_id, cfg.aws_access_key_secret, region_name=cfg.aws_region_name)


def bucket_exists(bucket_name: str) -> bool:
    """
    Test that the specified bucket exists in the app's AWS S3 account.

    Args:
        bucket_name: The name of the bucket.
    Returns:
        True if bucket exists, else False.
    """
    try:
        session = aws_session()
        s3_client = session.client('s3')
        response = s3_client.head_bucket(Bucket=bucket_name)
        get_application_logger().debug(f"response to head_bucket: {response}")
        return True
    except Exception as e:
        get_application_logger().warning(f"S3 bucket {bucket_name} not found: {str(e)}")
        return False


def upload_file_to_bucket(file_path: Path, bucket_name: str, key: str, log: bool = True) -> bool:
    """
    Upload a file to the specified key in the specified bucket in AWS S3 account.

    Args:
        file_path: Path to file. Must exist.
        bucket_name: The name of the target S3 bucket.
        key: The key under which the file object should be stored.
        log: If True, progress updates are posted to the portal application log once the upload begins and after 50%
            completion. Else a progress message is updated in-place on STDOUT (only for testing). Default = True.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    xfer_cfg = TransferConfig(multipart_threshold=50*MB, multipart_chunksize=50*MB)
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        if log:
            get_application_logger().info(f"Starting upload: {file_path.name} to S3 bucket {bucket_name} at {key}")
        bucket.upload_file(Filename=str(file_path), Key=key,
                           Callback=_TransferProgressCallback(file_path, log=log), Config=xfer_cfg)
        if log:
            get_application_logger().info(f"Successfully uploaded {file_path.name} to S3.")
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
        get_application_logger().info(
            f"Generated presigned URL for {key} in S3 bucket {bucket_name}. Expiring in {expires} seconds.")
        return True, url
    except Exception:
        get_application_logger().error(f"Failed to generate presigned URL for {key} in S3 bucket {bucket_name}",
                                       exc_info=True)
        return False, "Unable to generate download URL - file does not exist or internal error"


def download_file_from_bucket(bucket_name: str, key: str, dst: Path, log: bool = True) -> bool:
    """
    Download a file from the specified key in the specified bucket in AWS S3.

    Args:
        bucket_name: The name of the source S3 bucket.
        key: The key under which the file object is stored within that bucket.
        dst: The file system destination path for the file object.
        log: If True, progress updates are posted to the portal application log once the download begins and after 50%
            completion. Else a progress message is updated in-place on STDOUT (only for testing). Default = True.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    xfer_cfg = TransferConfig(multipart_threshold=50*MB, multipart_chunksize=50*MB)
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        obj = s3_resource.Object(bucket_name, key)
        obj.load()
        if log:
            get_application_logger().info(f"Starting download from S3 bucket {bucket_name} at {key} to {dst.name}")
        s3_resource.Object(bucket_name, key).download_file(
            Filename=str(dst),
            Callback=_TransferProgressCallback(dst, log=log, download_size=obj.content_length),
            Config=xfer_cfg
        )
        if log:
            get_application_logger().info(f"Successfully downloaded S3 object at {key}.")
        if dst.is_file():
            return True
        else:
            get_application_logger().error(
                f"File downloaded from S3 successfully, but NOT found at specified destination {str(dst)}")
            return False
    except Exception:
        get_application_logger().error(f"Failed to download object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def delete_file_in_bucket(bucket_name: str, key: str) -> bool:
    """
    Permanently delete an object stored in an AWS S3 bucket.

    Args:
        bucket_name: The name of the S3 bucket containing the object.
        key: The object's key.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        s3_resource.Object(bucket_name, key).delete()
        get_application_logger().info(f"Successfully deleted object {key} from S3 bucket {bucket_name}.")
        return True
    except Exception:
        get_application_logger().error(f"Failed to delete object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def bucket_contents(bucket_name: str) -> Optional[List]:
    """
    Retrieve the object listing for the specified bucket in AWS S3.

    Args:
        bucket_name: The name of the S3 bucket.

    Returns:
        A list of objects containing metadata on each object in the S3 bucket, or None if operation failed. In the
            latter case, an error message is written to the portal application log.
    """
    try:
        session = aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        obj_list = [obj for obj in bucket.objects.all()]
        return obj_list
    except Exception:
        get_application_logger().error(f"Failed to get object listing for S3 bucket {bucket_name}", exc_info=True)
        return None


class _TransferProgressCallback(object):
    """
    Callback object reports progress for an S3 object tranfer -- either upload or download. The callback may be
    configured to overwrite a progress message to STDOUT (which is appropriate only in the __main__ test script, when no
    other threads/processes are writing to the console), or to write to the portal application log once after the
    transfer has started and once after the transfer surpasses 50% completion.
    """
    def __init__(self, file_path: Path,  log: bool = True, download_size: Optional[int] = None):
        """
        Initialize the S3 object transfer callback.

        Args:
            file_path: The path of file being uploaded (must exist), or the location to which file is downloaded.
            log: If True, a progress message is written to the portal application log shortly after the transfer has
                started, and again once it surpasses 50% completion. If False, progress is reported by overwriting a
                line on STDOUT each time the callback is invoked. Default = True.
            download_size: If specified, the transfer is a download and this specifies the download file size. Else,
                the transfer is an upload and file_path must exist. Default = None.
        """
        self._path: Path = file_path
        self._to_log: bool = log
        self._num_updates = 0
        self._msg_prefix = "Downloading" if isinstance(download_size, int) else "Uploading"
        self._size = download_size if isinstance(download_size, int) else file_path.stat().st_size
        self._size_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, num_bytes):
        with self._lock:
            self._size_so_far += num_bytes
            percentage = (self._size_so_far / float(self._size)) * 100
            if self._to_log:
                if (self._num_updates == 0) or ((self._num_updates == 1) and (percentage >= 50)):
                    get_application_logger().info(
                        f"{self._msg_prefix} {self._path.name}  {self._size_so_far}/{self._size} ({percentage:.2f}%)")
                    self._num_updates += 1
            else:
                sys.stdout.write(
                    f"\r{self._msg_prefix} {self._path.name}  {self._size_so_far}/{self._size} ({percentage:.2f}%)")
                sys.stdout.flush()


def _print_usage() -> None:
    print("\nAvailable commands:\n"
          "   l = List all file objects in bucket.\n"
          "   u = Upload a file object to bucket.\n"
          "   d = Download a file object from bucket.\n"
          "   g = Generate a presigned URL to download a file object from bucket.\n"
          "   x = Delete a file object in bucket.\n"
          "   h = Print this usage message.\n"
          "   q = Quit.\n\n", file=sys.stdout, flush=True)


def _process_command() -> bool:
    command = input('Enter command (l, u, d, g, x, h, q) > ')
    error_msg = None
    bucket_name = get_config().repo_bucket
    if command == 'l':
        contents = bucket_contents(bucket_name)
        if contents is None:
            error_msg = "Unable to list bucket contents"
        elif len(contents) == 0:
            print("*** The bucket is empty! ***")
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
            ok = upload_file_to_bucket(file_path, bucket_name, f"{prefix}{file_path.name}", log=False)
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
            ok = download_file_from_bucket(bucket_name, obj_key, dst_file, log=False)
            t = time.time() - t_start
            if ok:
                print(f"\nDone. {dst_file.stat().st_size/MB:.1f}MB downloaded in {t:.3f} seconds.")
            else:
                error_msg = "Download failed."
    elif command == 'g':
        obj_key = input('Enter object key in full > ')
        ok, url = presigned_url_for_file(bucket_name, obj_key)
        if ok:
            print(f"\nDownload URL is: {url}")
        else:
            error_msg = url
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
    print(f"Verifying S3 bucket '{get_config().repo_bucket}' that holds portal backing repository...")
    if bucket_exists(get_config().repo_bucket):
        print(" OK.\n\n", file=sys.stdout, flush=True)
    else:
        print(" Bucket not found! ... Exiting.\n", file=sys.stdout, flush=True)
        exit(-1)

    _print_usage()

    done = False
    while not done:
        done = _process_command()

    print("\n\nBYE!", file=sys.stdout, flush=True)
