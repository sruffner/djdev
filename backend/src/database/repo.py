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
string key. To create a file system-like folder hierarchy for the portal repository, we use path-like keys for all files
stored in it. All keys start with a forward slash, and addtional forward slashes separate the folders in the path-like
key. The portal repository contains 3 folders under the root: The /logs folder contains the backup of the database
operations log (and could be the location for error logs or similar files in the future). The /downloads folder is a
temporary location for data files prepared in response to download requests. After preparing a data file in reponse to
a download request, the file is uploaded to /downloads and a presigned URL generated so that the user can download the
file directly from the S3 bucket (without needing the requisite access credentials). The presigned URL expires after an
hour, and any file in the /downloads folder expires after 1 day (IAW a lifecycle configuration rule defined on the S3
bucket).

Finally, the /repo folder holds the ZIP archives for all experiment sessions that have been committed to the portal's
database. The key format for any particular session archive illustrates how the archives are organized under this
folder: /repo/<exp>/<subj>_<date>_<sfx>.zip, where: <exp> is the username of the experimenter, <subj> is the ID of the
experiment subject, <date> is the experiment date in the format 'YYYY-MM-DD', and <sfx> is the integer session suffix
(1-9). These four attributes form the primary key that uniquely identifies an experiment session in the database.

This module includes public methods to list repository contents, upload a file to or download a file from the
repository, delete a file in the repository, or obtain a presigned URL to a data file in the '/downloads' folder so
an external user can download that file directly from S3. It also includes the private methods that implement the
repository's storage in the provisioned S3 bucket. A __main__ entrypoint is available for test and diagnostic purposes.

@author: sruffner
@created: 15feb2022
"""
import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List, Union, Dict, Any

from boto3 import Session
from boto3.s3.transfer import TransferConfig

from config.config import get_config, get_application_logger
from utils.common import MB, GB, size_with_units


def listing() -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """
    Retrieve a semi-structured listing of the contents of the portal repository.

    Returns:
        A dictionary of pseudo folder paths (eg, '/repo/username') in the repository that contain one or more files.
            Each key is a folder path, and the corresponding value is a list of file information objects, one per file
            in the path. Each file information object is a dictionary with the following keys: 'name' is the file name,
            'last_modified' is a datetime object indicating the object's creation time in S3, 'storage_class' is the
            object's S3 storage class, and 'size' is its total size in bytes. Each folder's file list is sorted in
            reverse chronological order by creation time. If the repository is empty, returns an empty dictionary. If
            an error occurs, returns None. Consult the application log for the error description.
    """
    contents = _bucket_contents(get_config().repo_bucket)
    if contents is None:
        return None
    folders: Dict[str, List[Dict[str, str]]] = dict()
    for o in contents:
        pos = o.key.rfind('/')
        if pos <= 0:
            folder_key = '/'
            file_name = o.key if pos == -1 else o.key[1:]
        else:
            folder_key = o.key[0:pos]
            file_name = o.key[pos+1:]
        if not (folder_key in folders):
            folders[folder_key] = list()
        folders[folder_key].append(
            dict(name=file_name, last_modified=o.last_modified, storage_class=o.storage_class, size=o.size))
    for folder_key in folders:
        folders[folder_key].sort(key=lambda x: x['last_modified'], reverse=True)
    return folders


def upload_file(file_path: Path, key: str, log: bool = True) -> bool:
    """
    Upload the specified file to the portal's backing repository.

    All files are stored in the repository under file path-like keys and must match one of these formats: '/logs/*' for
    log files, '/downloads/*' for experiment data files prepared in response to download requests, and '/repo/*/*' for
    session archive ZIP files.

    Args:
        file_path: File system path for the target file. Must exist.
        key: The S3 object key under which the file should be stored. Must satisfy portal constraints on key format.
        log: If True, progress updates are posted to the portal application log once the upload begins and after 50%
            completion. Else a progress message is updated in-place on STDOUT. Default = True.
    Returns:
        True if successful, False otherwise. Check application log for error desciription.
    Raises:
        ValueError: If object key violates expected format, or target file does not exist.
    """
    if not (_validate_key_format(key) and file_path.is_file()):
        raise ValueError("Bad repository file object key, or target file not found")
    return _upload_file_to_bucket(file_path, get_config().repo_bucket, key, log)


def _validate_key_format(key: str) -> bool:
    """
    Check that specified S3 object key conforms to the format expected for any file stored in the portal repository. By
    convention, the key must always start with a '/logs', '/downloads', or '/repo'. Keys under 'repo' will have 3 path
    parts (/repo/username/file.zip), while keys under the other 2 folders have 2 path parts.

    Args:
        key: The object key.
    Returns:
        True if key conforms to the format expected of a file in the portal repository, else False.
    """
    ok = False
    try:
        parts = key.split('/')
        n, p1 = len(parts), parts[1]
        ok = (parts[0] == '') and (((n == 3) and (p1 in ['logs', 'downloads'])) or ((n == 4) and (p1 == 'repo')))
    except Exception:
        pass
    return ok


def download_file(key: str, dst: Path, log: bool = True) -> bool:
    """
    Download a file stored in the portal repository.

    Args:
        key: The file object key.
        dst: The file system destination path for the file object.
        log: If True, progress updates are posted to the portal application log once the download begins and after 50%
            completion. Else a progress message is updated in-place on STDOUT. Default = True.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    return _download_file_from_bucket(get_config().repo_bucket, key, dst, log)


def download_url_for(key: str) -> Optional[str]:
    """
    Generate a URL by which a data file previously prepared in response to an experiment data download request may be
    downloaded from the portal repository.

    Data files prepared in response to a download request are stored for 1 day in the '/downloads' node in the
    repository. Since the repository is maintained in a private Amazon S3 bucket, a presigned URL must be supplied so
    that the external user that made the request can download the file directly to their machine.

    This method may not be used to generate a download URL for files elsewhere in the portal repository

    Args:
        key: The file object key.
    Returns:
        The URL string. The URL will expire in 1 hour. Returns None if an error occurs (consult application logs).
    Raises:
        ValueError: If key does not start with '/downloads'.
    """
    if not key.startswith('/downloads'):
        raise ValueError("Download URL only available for files in the /downloads folder!")
    return _presigned_url_for_file(get_config().repo_bucket, key)


def file_size(key: str) -> int:
    """
    Return the size of a file stored in the portal repository.

    Args:
        key: The file object's key.
    Returns: The file's size in bytes. Returns 0 if file not found or an internal error occurred.
    """
    return _file_size_in_bucket(get_config().repo_bucket, key)


def delete_file(key: str) -> bool:
    """
    Permanently delete a file stored in the portal repository

    Args:
        key: The file object's key.
    Returns:
        True if successful or object not found; False otherwise. Error message is written to the portal application log.
    """
    return _delete_file_in_bucket(get_config().repo_bucket, key)


def _aws_session() -> Optional[Session]:
    """
    Generate an authenticated AWS session object using the authentication credentials from application configuration.

    Returns:
        The session object.
    Raises:
        Exception: If access credentials are missing from application configuration
    """
    cfg = get_config()
    if (not cfg.aws_access_key_id) or (not cfg.aws_access_key_secret) or (not cfg.aws_region_name):
        raise Exception("Cannot open AWS session - Missing access credentials.")
    return Session(cfg.aws_access_key_id, cfg.aws_access_key_secret, region_name=cfg.aws_region_name)


def _bucket_exists(bucket_name: str) -> bool:
    """
    Test that the specified bucket exists in the app's AWS S3 account.

    Args:
        bucket_name: The name of the bucket.
    Returns:
        True if bucket exists, else False.
    """
    try:
        session = _aws_session()
        s3_client = session.client('s3')
        response = s3_client.head_bucket(Bucket=bucket_name)
        get_application_logger().debug(f"response to head_bucket: {response}")
        return True
    except Exception as e:
        get_application_logger().warning(f"S3 bucket {bucket_name} not found: {str(e)}")
        return False


def _upload_file_to_bucket(file_path: Path, bucket_name: str, key: str, log: bool = True) -> bool:
    """
    Upload a file to the specified key in the specified bucket in AWS S3.

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
        session = _aws_session()
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


def _presigned_url_for_file(bucket_name: str, key: str, expires: int = 3600) -> Optional[str]:
    """
    Generate a presigned URL by which the specified file may be downloaded from the specified S3 bucket.

    Args:
        bucket_name: THe bucket name.
        key: The file object key.
        expires: Expiration time for the URL, in seconds. Range 1-86400 (24 hours). Default = 3600 (1 hour).
    Returns:
        The URL string, or None if an error occurred.
    """
    try:
        session = _aws_session()
        s3_client = session.client('s3')
        url = s3_client.generate_presigned_url(ClientMethod='get_object', Params={'Bucket': bucket_name, 'Key': key},
                                               ExpiresIn=expires)
        get_application_logger().info(
            f"Generated presigned URL for {key} in S3 bucket {bucket_name}. Expiring in {expires} seconds.")
        return url
    except Exception:
        get_application_logger().error(f"Failed to generate presigned URL for {key} in S3 bucket {bucket_name}",
                                       exc_info=True)
        return None


def _download_file_from_bucket(bucket_name: str, key: str, dst: Path, log: bool = True) -> bool:
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
        session = _aws_session()
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


def _file_size_in_bucket(bucket_name: str, key: str) -> int:
    """
    Return the size of the file at the specified key in the specified AWS S3 bucket.

    Args:
        bucket_name:  The name of the bucket.
        key: The file object's key.

    Returns: The file's size in bytes. Returns 0 if file not found or an internal error occurred.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        obj_summary = s3_resource.ObjectSummary(bucket_name, key)
        obj_summary.load()
        return obj_summary.size
    except Exception:
        return 0


def _file_exists_in_bucket(bucket_name: str, key: str) -> bool:
    """
    Does a file exist at the specified key in the specified AWS S3 bucket?

    Args:
        bucket_name: The name of the bucket.
        key: The file object's key.

    Returns: True if file exists; False otherwise.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        obj_summary = s3_resource.ObjectSummary(bucket_name, key)
        obj_summary.load()
        return True
    except Exception:
        return False


def _delete_file_in_bucket(bucket_name: str, key: str) -> bool:
    """
    Permanently delete an object stored in an AWS S3 bucket.

    Args:
        bucket_name: The name of the S3 bucket containing the object.
        key: The object's key.
    Returns:
        True if successful or object not found; False otherwise. Error message is written to the portal application log.
    """
    if not _file_exists_in_bucket(bucket_name, key):
        get_application_logger().info(f"Attempt to delete non-existent object {key} from S3 bucket {bucket_name}")
        return True
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        s3_resource.Object(bucket_name, key).delete()
        get_application_logger().info(f"Successfully deleted object {key} from S3 bucket {bucket_name}.")
        return True
    except Exception:
        get_application_logger().error(f"Failed to delete object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def _bucket_contents(bucket_name: str) -> Optional[List]:
    """
    Retrieve the object listing for the specified bucket in AWS S3.

    Args:
        bucket_name: The name of the S3 bucket.

    Returns:
        A list of objects containing metadata on each object in the S3 bucket, or None if operation failed. In the
            latter case, an error message is written to the portal application log. Each element is an S3 ObjectSummary.
    """
    try:
        session = _aws_session()
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


def _get_lifecycle_configuration_rules_for_bucket(bucket_name: str) -> Union[List, str]:
    """
    Get the current lifecycle configuration rules for the named bucket. For administrative purposes only.

    Args:
        bucket_name: The name of the S3 bucket.
    Returns:
        List of lifecycle configuration rules (each of which is a dictionary), or an error message on failure.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        lifecycle_cfg = bucket.LifecycleConfiguration()
        lifecycle_cfg.load()
        return lifecycle_cfg.rules
    except Exception as e:
        return str(e)


def _print_usage() -> None:
    print("\nAvailable commands:\n"
          "   l = List all file objects in bucket.\n"
          "   c = Display current lifecycle configuration for bucket.\n"
          "   u = Upload a file object to bucket.\n"
          "   d = Download a file object from bucket.\n"
          "   g = Generate a presigned URL to download a file object from bucket.\n"
          "   x = Delete a file object in bucket.\n"
          "   h = Print this usage message.\n"
          "   q = Quit.\n\n", file=sys.stdout, flush=True)


def _process_command(bucket_name: str) -> bool:
    command = input(f"[{bucket_name}] Enter command (l, c, u, d, g, x, h, q) > ")
    error_msg = None
    if command == 'l':
        contents = _bucket_contents(bucket_name)
        if contents is None:
            error_msg = "Unable to list bucket contents"
        elif len(contents) == 0:
            print("*** The bucket is empty! ***")
        else:
            print(f"{'KEY':50} {'STORAGE CLASS':30} {'SIZE':15} {'LAST_MODIFIED':30}")
            print(f"{'---':50} {'-------------':30} {'----':15} {'-------------':30}")
            for o in contents:
                print(f"{o.key:50} {o.storage_class:30} "
                      f"{size_with_units(o.size):15} {o.last_modified.strftime('%m-%d-%Y %H:%M:%s %Z'):30}")
    elif command == 'c':
        rules = _get_lifecycle_configuration_rules_for_bucket(bucket_name)
        if isinstance(rules, str):
            error_msg = rules
        else:
            print(f"Bucket Lifecycle Configuration for {bucket_name}:")
            print(json.dumps(rules, indent=3), file=sys.stdout, flush=True)
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
            ok = _upload_file_to_bucket(file_path, bucket_name, f"{prefix}{file_path.name}", log=False)
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
            ok = _download_file_from_bucket(bucket_name, obj_key, dst_file, log=False)
            t = time.time() - t_start
            if ok:
                print(f"\nDone. {dst_file.stat().st_size/MB:.1f}MB downloaded in {t:.3f} seconds.")
            else:
                error_msg = "Download failed."
    elif command == 'g':
        obj_key = input('Enter object key in full > ')
        ok, url = _presigned_url_for_file(bucket_name, obj_key)
        if ok:
            print(f"\nDownload URL is: {url}")
        else:
            error_msg = url
    elif command == 'x':
        obj_key = input('Enter object key in full > ')
        if not _delete_file_in_bucket(bucket_name, obj_key):
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
    _bucket_name = input('Enter name of S3 bucket > ')
    if not _bucket_exists(_bucket_name):
        print(" Bucket not found! ... Exiting.\n", file=sys.stdout, flush=True)
        exit(-1)

    _print_usage()

    done = False
    while not done:
        done = _process_command(_bucket_name)

    print("\n\nBYE!", file=sys.stdout, flush=True)
